"""Minimal Home Assistant REST client.

Works both as an HA app (Supervisor networking + SUPERVISOR_TOKEN, no user
setup at all) and standalone (url + long-lived access token from config).
"""

from __future__ import annotations

import json
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

    async def get_states(self) -> list[dict[str, Any]]:
        """All entity states; used to populate the config UI's pickers."""
        response = await self._client.get("/states")
        response.raise_for_status()
        return response.json()

    async def render_template(self, template: str) -> str:
        """Render a Jinja template server-side. This is the only REST route that
        can reach the device/entity registry (device_id, device_attr,
        device_entities), which the states API does not expose. On a non-200,
        include HA's response body -- for /template a 400 carries the actual
        Jinja error message, which is what we need to debug it."""
        response = await self._client.post("/template", json={"template": template})
        if response.status_code != 200:
            raise HomeAssistantError(
                "template render failed (" + str(response.status_code) + "): "
                + response.text[:600]
            )
        return response.text

    def _discovery_template(self, climate_entity: str) -> str:
        """Template that returns one row per sensor/binary_sensor entity that
        lives on a device 'connected to' the thermostat (via the registry
        `via_device_id` relationship — the "connected to Upstairs" link).

        We iterate the sensor/binary_sensor STATES and resolve each to its
        device, rather than enumerating devices(): the `/api/template` endpoint
        does not expose the `devices()` / `device_entities()` helpers (they raise
        "'devices' is undefined"), but `device_id` / `device_attr` / `states` are
        available."""
        return (
            "{%- set stat = device_id('" + climate_entity + "') -%}"
            "{%- set parent = device_attr(stat, 'via_device_id') if stat else none -%}"
            "{%- set ns = namespace(rows=[]) -%}"
            "{%- for state in (states.sensor | list) + (states.binary_sensor | list) -%}"
            "{%- set e = state.entity_id -%}"
            "{%- set d = device_id(e) -%}"
            "{%- set vd = device_attr(d, 'via_device_id') if d else none -%}"
            "{%- if stat and d and (d == stat or vd == stat or (parent and vd == parent)) -%}"
            "{%- set ns.rows = ns.rows + [{"
            "'device': d, 'is_stat': d == stat,"
            "'device_name': device_attr(d, 'name_by_user') or device_attr(d, 'name'),"
            "'entity': e, 'domain': e.split('.')[0],"
            "'device_class': state_attr(e, 'device_class'),"
            "'unit': state_attr(e, 'unit_of_measurement')}] -%}"
            "{%- endif -%}"
            "{%- endfor -%}"
            "{{ {'stat_device': stat, 'parent': parent, 'entities': ns.rows} | to_json }}"
        )

    async def discover_sensors(self, climate_entity: str) -> list[dict[str, Any]]:
        """Auto-discover the remote sensors 'connected to' a thermostat, with no
        manual mapping. Returns one dict per sensor: {sensor_id, name, is_stat,
        temperature_entity, humidity_entity, occupancy_entity}. `is_stat` marks
        the thermostat's own device (its built-in sensor)."""
        try:
            data = json.loads(await self.render_template(self._discovery_template(climate_entity)))
        except (httpx.HTTPError, HomeAssistantError, json.JSONDecodeError, ValueError):
            return []

        # Group entity rows by device, classifying each device's entities.
        devices: dict[str, dict[str, Any]] = {}
        for row in data.get("entities", []):
            device = devices.setdefault(
                row["device"],
                {
                    "name": row.get("device_name") or row["device"],
                    "is_stat": bool(row.get("is_stat")),
                    "temperature": None,
                    "humidity": None,
                    "occupancy": None,
                },
            )
            domain = row.get("domain")
            device_class = row.get("device_class")
            unit = row.get("unit")
            entity = row.get("entity", "")
            # Prefer device_class, fall back to unit / entity-id naming since not
            # every HomeKit sensor sets a device_class.
            if domain == "sensor" and device["temperature"] is None and (
                device_class == "temperature" or unit in ("°F", "°C", "K") or "temperature" in entity
            ):
                device["temperature"] = entity
            elif domain == "sensor" and device["humidity"] is None and (
                device_class == "humidity" or "humidity" in entity
            ):
                device["humidity"] = entity
            elif domain == "binary_sensor" and device["occupancy"] is None and (
                device_class in ("occupancy", "motion", "presence")
                or "occupancy" in entity
                or "motion" in entity
            ):
                device["occupancy"] = entity

        sensors: list[dict[str, Any]] = []
        for device_id_, device in devices.items():
            is_stat = device["is_stat"]
            # A remote sensor must have a temperature; the thermostat's own device
            # is kept only for its occupancy (temp/humidity come from the climate
            # entity's own sample).
            if not is_stat and device["temperature"] is None:
                continue
            if is_stat and device["occupancy"] is None:
                continue
            sensors.append(
                {
                    "sensor_id": "ei:0" if is_stat else "rs:" + str(device_id_),
                    "name": device["name"],
                    "is_stat": is_stat,
                    "temperature_entity": device["temperature"],
                    "humidity_entity": device["humidity"],
                    "occupancy_entity": device["occupancy"],
                }
            )
        return sensors

    async def discover_sensors_debug(self, climate_entity: str) -> dict[str, Any]:
        """Raw discovery view for troubleshooting: the thermostat's device, its
        via_device parent, and every sensor/binary_sensor entity on the related
        devices with its domain / device_class / unit, plus any template error."""
        try:
            raw = await self.render_template(self._discovery_template(climate_entity))
        except (httpx.HTTPError, HomeAssistantError) as error:
            return {"error": str(error)}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"error": "template did not return JSON", "raw": raw[:800]}

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
