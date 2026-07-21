"""The fake ecobee API surface beestat talks to, plus /admin.

Matches what beestat's api/ecobee.php actually sends:
  GET  /authorize?response_type=code&client_id=...&redirect_uri=...&state=...
  POST /token           (grant_type=authorization_code | refresh_token)
  GET  /1/thermostat?body=<json>&client_id=...
  GET  /1/runtimeReport?body=<json>&client_id=...

Beestat treats in-band status.code 14 as "refresh and retry", which is how
its token dance stays exercised against the bridge.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from . import ecobee_auth, settings as settings_module, tokens, ui
from .sources.cloud import CloudAuthDead
from .sources.local import status_envelope

logger = logging.getLogger(__name__)

router = APIRouter()


def _context(request: Request) -> Any:
    return request.app.state.context


def _authorized(request: Request) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    context = _context(request)
    return tokens.verify_access_token(
        context.store.install_secret(), header.removeprefix("Bearer ")
    )


def _token_response(context: Any) -> dict[str, Any]:
    return {
        "access_token": tokens.mint_access_token(
            context.store.install_secret(), context.store.bridge_account_id()
        ),
        "token_type": "Bearer",
        "expires_in": tokens.ACCESS_TOKEN_LIFETIME,
        "refresh_token": tokens.mint_refresh_token(),
        "scope": "smartRead",
    }


# -- oauth ------------------------------------------------------------------

@router.get("/authorize")
async def authorize(
    redirect_uri: str,
    state: str | None = None,
    response_type: str | None = None,
    client_id: str | None = None,
    scope: str | None = None,
) -> RedirectResponse:
    """No consent screen: the bridge serves exactly one household. Bounce
    straight back to beestat with a code."""
    params = {"code": "bridge-code"}
    if state is not None:
        params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


@router.post("/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    code: str | None = Form(None),
    refresh_token: str | None = Form(None),
) -> JSONResponse:
    context = _context(request)
    if grant_type not in ("authorization_code", "refresh_token"):
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    # Single-household facade: any code/refresh token we issued is acceptable;
    # identity is constant (the stable bridge account id).
    return JSONResponse(_token_response(context))


# -- data endpoints ---------------------------------------------------------

async def _serve(request: Request, endpoint: str, body: str | None) -> PlainTextResponse:
    context = _context(request)
    if not _authorized(request):
        # In-band expired-token signal; beestat refreshes and retries.
        return PlainTextResponse(
            json.dumps(status_envelope(14, "Authentication token has expired.")),
            media_type="application/json",
        )
    try:
        parsed_body = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return PlainTextResponse(
            json.dumps(status_envelope(4, "Bad request body.")), media_type="application/json"
        )

    mode = context.mode_manager.effective_mode()
    try:
        if mode == "cloud":
            handler = getattr(
                context.cloud, "thermostat" if endpoint == "thermostat" else "runtime_report"
            )
            payload = await handler(parsed_body)
        else:
            handler = getattr(
                context.local, "thermostat" if endpoint == "thermostat" else "runtime_report"
            )
            payload = handler(parsed_body)
    except CloudAuthDead as error:
        await context.mode_manager.mark_cloud_dead(context.ha, str(error))
        if context.settings.auto_failover:
            handler = getattr(
                context.local, "thermostat" if endpoint == "thermostat" else "runtime_report"
            )
            payload = handler(parsed_body)
        else:
            payload = json.dumps(status_envelope(2, f"Cloud auth failed: {error}"))
    except Exception:
        logger.exception("%s failed (mode=%s)", endpoint, mode)
        payload = json.dumps(status_envelope(3, "Processing error."))

    return PlainTextResponse(payload, media_type="application/json")


@router.get("/1/thermostat")
async def thermostat(request: Request, body: str | None = Query(None)) -> PlainTextResponse:
    return await _serve(request, "thermostat", body)


@router.get("/1/runtimeReport")
async def runtime_report(request: Request, body: str | None = Query(None)) -> PlainTextResponse:
    return await _serve(request, "runtimeReport", body)


# -- admin ------------------------------------------------------------------

@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/admin/status")
async def admin_status(request: Request) -> dict[str, Any]:
    context = _context(request)
    return {
        "effective_mode": context.mode_manager.effective_mode(),
        "configured_mode": context.settings.mode,
        "mode_override": context.store.mode_override(),
        "cloud_failed_over": context.mode_manager.failed_over,
        "ecobee_tokens_present": context.store.ecobee_tokens() is not None,
        "snapshots": context.store.snapshot_identifiers(),
        "thermostats": [thermostat.serial for thermostat in context.settings.thermostats],
        "recorder_running": context.recorder_running,
    }


@router.post("/admin/mode")
async def admin_set_mode(request: Request) -> dict[str, Any]:
    context = _context(request)
    payload = await request.json()
    context.mode_manager.set_override(payload.get("mode"))  # null clears override
    return {"effective_mode": context.mode_manager.effective_mode()}


@router.post("/admin/ecobee/tokens")
async def admin_set_ecobee_tokens(request: Request) -> dict[str, Any]:
    """Escape hatch: store a refresh token obtained elsewhere."""
    context = _context(request)
    payload = await request.json()
    if not payload.get("refresh_token"):
        return {"error": "refresh_token required"}
    context.store.set_ecobee_tokens(
        refresh_token=payload["refresh_token"],
        access_token=payload.get("access_token"),
    )
    context.mode_manager.failed_over = False
    return {"stored": True}


# -- setup UI + interactive ecobee login ------------------------------------

@router.get("/")
async def setup_page() -> HTMLResponse:
    return HTMLResponse(ui.PAGE)


async def _finish_login(context: Any, body: dict[str, str]) -> dict[str, Any]:
    context.store.set_ecobee_tokens(
        refresh_token=body["refresh_token"], access_token=body.get("access_token")
    )
    context.mode_manager.failed_over = False
    if context.ecobee_login is not None:
        await context.ecobee_login.close()
        context.ecobee_login = None
    logger.info("ecobee consumer login succeeded; cloud path connected")
    return {"connected": True}


@router.post("/admin/ecobee/login")
async def admin_ecobee_login(request: Request) -> dict[str, Any]:
    """Run the consumer login (Auth0 universal login + PKCE). Credentials are
    used for this one exchange and never persisted."""
    context = _context(request)
    payload = await request.json()
    email, password = payload.get("email"), payload.get("password")
    if not email or not password:
        return {"error": "email and password required"}

    if context.ecobee_login is not None:  # Drop any stale half-done attempt.
        await context.ecobee_login.close()
    context.ecobee_login = ecobee_auth.EcobeeAuthenticator(
        context.settings.ecobee_client_id
    )
    try:
        body = await context.ecobee_login.start(email, password)
    except ecobee_auth.EcobeeMfaRequired as challenge:
        return {"mfa_required": True, "challenge_type": challenge.challenge_type}
    except ecobee_auth.EcobeeAuthError as error:
        await context.ecobee_login.close()
        context.ecobee_login = None
        return {"error": str(error)}
    return await _finish_login(context, body)


@router.post("/admin/ecobee/mfa")
async def admin_ecobee_mfa(request: Request) -> dict[str, Any]:
    context = _context(request)
    payload = await request.json()
    if context.ecobee_login is None:
        return {"error": "no login in progress; start over"}
    if not payload.get("code"):
        return {"error": "code required"}
    try:
        body = await context.ecobee_login.submit_mfa(payload["code"])
    except ecobee_auth.EcobeeAuthError as error:
        return {"error": str(error)}
    return await _finish_login(context, body)


# -- runtime configuration (the bridge's own config UI) ----------------------

@router.get("/admin/config")
async def admin_get_config(request: Request) -> dict[str, Any]:
    context = _context(request)
    return {
        "config": settings_module.editable_config(context.settings),
        "system_types": list(settings_module.VALID_SYSTEM_TYPES),
        "equipment_source_keys": list(settings_module.EQUIPMENT_SOURCE_KEYS),
        "ui_saved": context.store.runtime_config() is not None,
    }


@router.post("/admin/config")
async def admin_set_config(request: Request) -> dict[str, Any]:
    """Validate, persist, and apply in place — no restart needed."""
    context = _context(request)
    payload = await request.json()
    try:
        settings_module.apply_editable_config(context.settings, payload)
    except (ValueError, KeyError, TypeError) as error:
        return {"error": str(error)}
    context.store.set_runtime_config(settings_module.editable_config(context.settings))
    logger.info(
        "runtime config saved via UI: %d thermostat(s)", len(context.settings.thermostats)
    )
    return {"saved": True, "config": settings_module.editable_config(context.settings)}


@router.get("/admin/ha/entities")
async def admin_ha_entities(request: Request) -> dict[str, list[str]]:
    """Entity ids for the config UI's pickers, grouped by how they're used."""
    context = _context(request)
    groups: dict[str, list[str]] = {"climate": [], "binary_sensor": [], "outdoor": []}
    if context.ha is None:
        return groups
    try:
        states = await context.ha.get_states()
    except Exception:
        logger.exception("could not list HA entities")
        return groups
    for state in states:
        entity_id = state.get("entity_id", "")
        domain = entity_id.split(".", 1)[0]
        if domain == "climate":
            groups["climate"].append(entity_id)
        elif domain == "binary_sensor":
            groups["binary_sensor"].append(entity_id)
        elif domain in ("weather", "sensor"):
            groups["outdoor"].append(entity_id)
    for group in groups.values():
        group.sort()
    return groups
