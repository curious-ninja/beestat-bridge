"""Minimal Home Assistant REST client.

Works both as an HA app (Supervisor networking + SUPERVISOR_TOKEN, no user
setup at all) and standalone (url + long-lived access token from config).
"""

from __future__ import annotations

from typing import Any

import httpx

from .settings import Settings


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistant:
    def __init__(self, settings: Settings) -> None:
        if settings.ha_api_url is None or settings.ha_api_token is None:
            raise HomeAssistantError(
                "Home Assistant is not configured: set home_assistant.url and "
                "home_assistant.token, or run as a Home Assistant app."
            )
        self._client = httpx.AsyncClient(
            base_url=settings.ha_api_url,
            headers={"Authorization": f"Bearer {settings.ha_api_token}"},
            timeout=15.0,
        )

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        response = await self._client.get(f"/states/{entity_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def notify(self, title: str, message: str) -> None:
        """Persistent notification; used e.g. on cloud auth death."""
        try:
            await self._client.post(
                "/services/persistent_notification/create",
                json={"title": title, "message": message},
            )
        except httpx.HTTPError:
            pass  # Notifications are best-effort by definition.

    async def close(self) -> None:
        await self._client.aclose()
