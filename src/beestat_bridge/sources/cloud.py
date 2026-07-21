"""Cloud source: authenticated passthrough to the real ecobee API.

Every response is teed into the local archive, and every /1/thermostat
response updates the per-identifier snapshot that local mode serves later.
The archive is the part of this system that can never be recreated once
ecobee access dies — it is written before the response is returned.

TODO(bridge): interactive consumer login (Auth0 PKCE per ha-ecobee, or
pyecobee's username/password flow as used by HA core >= 2026.3). Until then,
bootstrap with POST /admin/ecobee/tokens {"refresh_token": "..."}.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..settings import Settings
from ..store import Store

logger = logging.getLogger(__name__)


class CloudAuthDead(RuntimeError):
    """Raised when refresh conclusively fails; triggers auto-failover."""


class CloudSource:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._client = httpx.AsyncClient(timeout=30.0)

    async def _refresh(self) -> str:
        tokens = self._store.ecobee_tokens()
        if tokens is None:
            raise CloudAuthDead("no ecobee tokens stored; bootstrap via /admin/ecobee/tokens")

        # TODO(bridge): confirm the refresh endpoint for consumer-grant tokens.
        # Classic dev-key tokens refresh at {api}/token; Auth0-issued consumer
        # tokens may refresh at {auth_domain}/oauth/token. Try classic first,
        # fall back to Auth0 form.
        for url, payload in (
            (
                f"{self._settings.ecobee_api_base_url}/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": self._settings.ecobee_client_id,
                    "ecobee_type": "jwt",
                },
            ),
            (
                f"{self._settings.ecobee_auth_domain}/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": self._settings.ecobee_client_id,
                },
            ),
        ):
            try:
                response = await self._client.post(url, data=payload)
                body = response.json()
            except (httpx.HTTPError, json.JSONDecodeError):
                continue
            if "access_token" in body:
                self._store.set_ecobee_tokens(
                    refresh_token=body.get("refresh_token", tokens["refresh_token"]),
                    access_token=body["access_token"],
                )
                return body["access_token"]

        raise CloudAuthDead("ecobee token refresh failed on all known endpoints")

    async def _request(self, endpoint: str, body: dict[str, Any], retry: bool = True) -> str:
        tokens = self._store.ecobee_tokens()
        access_token = tokens["access_token"] if tokens else None
        if access_token is None:
            access_token = await self._refresh()

        response = await self._client.get(
            f"{self._settings.ecobee_api_base_url}/1/{endpoint}",
            params={"format": "json", "body": json.dumps(body)},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        text = response.text

        # ecobee signals expired auth in-band: status.code 14 (expired) / 1.
        try:
            parsed = json.loads(text)
            code = parsed.get("status", {}).get("code")
        except json.JSONDecodeError:
            parsed, code = None, None
        if code in (1, 14) and retry:
            await self._refresh()
            return await self._request(endpoint, body, retry=False)

        # Tee to the permanent archive before anything else can go wrong.
        self._store.archive_response(endpoint, body, text)
        if endpoint == "thermostat" and parsed is not None:
            for api_thermostat in parsed.get("thermostatList", []):
                self._store.upsert_snapshot(api_thermostat["identifier"], api_thermostat)

        return text

    async def thermostat(self, body: dict[str, Any]) -> str:
        return await self._request("thermostat", body)

    async def runtime_report(self, body: dict[str, Any]) -> str:
        return await self._request("runtimeReport", body)

    async def close(self) -> None:
        await self._client.aclose()
