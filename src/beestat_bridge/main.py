"""Application wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .facade import router
from .ha import HomeAssistant, HomeAssistantError
from .mode import ModeManager
from .recorder import run_recorder
from .settings import Settings, load_settings
from .sources.cloud import CloudSource
from .sources.local import LocalSource
from .store import Store

logger = logging.getLogger(__name__)

# Requests proxied by HA Ingress arrive from the Supervisor's fixed address.
INGRESS_PROXY_IP = "172.30.32.2"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.db_path)

    context = SimpleNamespace(
        settings=settings,
        store=store,
        mode_manager=ModeManager(settings, store),
        cloud=CloudSource(settings, store),
        local=LocalSource(settings, store),
        ha=None,
        ecobee_login=None,  # In-flight EcobeeAuthenticator (MFA pending).
        recorder_running=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        try:
            context.ha = HomeAssistant(settings)
        except HomeAssistantError as error:
            # Cloud-only operation is legal, but the whole point of the bridge
            # is the local path — be loud about it.
            logger.warning("local recorder DISABLED: %s", error)
        if context.ha is not None and settings.thermostats:
            tasks.append(asyncio.create_task(run_recorder(settings, store, context.ha)))
            context.recorder_running = True
            tasks.append(asyncio.create_task(context.mode_manager.watch_ha_entity(context.ha)))
        logger.info(
            "beestat-bridge up: mode=%s, thermostats=%d",
            context.mode_manager.effective_mode(),
            len(settings.thermostats),
        )
        yield
        for task in tasks:
            task.cancel()
        if context.ecobee_login is not None:
            await context.ecobee_login.close()
        await context.cloud.close()
        if context.ha is not None:
            await context.ha.close()
        store.close()

    app = FastAPI(title="beestat-bridge", lifespan=lifespan)
    app.state.context = context

    @app.middleware("http")
    async def guard_admin(request, call_next):
        # As an HA app, the setup page and /admin/* (mode switching, ecobee
        # credentials) are reachable ONLY through HA Ingress — which
        # authenticates the user and always originates from the Supervisor
        # proxy. The facade endpoints stay open: beestat calls them
        # server-to-server on the exposed port. Outside HA (docker-compose)
        # there is no Supervisor and the LAN is trusted, as documented.
        path = request.url.path
        if (
            (path == "/" or path.startswith("/admin"))
            and settings.supervisor_token is not None
            and request.client is not None
            and request.client.host != INGRESS_PROXY_IP
        ):
            return JSONResponse(
                {"error": "open the bridge from the Home Assistant sidebar"},
                status_code=403,
            )
        return await call_next(request)

    app.include_router(router)
    return app
