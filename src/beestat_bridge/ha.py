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
        device_entities), which the states API does not expose."""
        response = await self._client.post("/template", json={"template": template})
        response.raise_for_status()
        return response.text

    async def discover_sensors(self, climate_entity: str) -> list[dict[str, Any]]:
        """Auto-discover the remote sensors 'connected to' a thermostat, with no
        manual mapping. HomeKit models each ecobee remote sensor as its own HA
        device whose registry `via_device_id` points at the thermostat's device
        (that's the "connected to Upstairs" relationship). Find those devices,
        then classify each one's entities by device_class.

        Returns one dict per sensor: {sensor_id, name, is_stat, temperature_entity,
        humidity_entity, occupancy_entity}. `is_stat` marks the thermostat's own
        device (its built-in sensor)."""
        template = (
            "{%- set stat = device_id('" + climate_entity + "') -%}"
            "{%- set parent = device_attr(stat, 'via_device_id') if stat else none -%}"
            "{%- set ns = namespace(rows=[]) -%}"
            "{%- for d in devices() -%}"
            "{%- set vd = device_attr(d, 'via_device_id') -%}"
            "{%- if stat and (d == stat or vd == stat or (parent and vd == parent)) -%}"
            "{%- set ns.rows = ns.rows + [{"
            "'device': d, 'is_stat': d == stat,"
            "'name': device_attr(d, 'name_by_user') or device_attr(d, 'name'),"
            "'entities': device_entities(d)}] -%}"
            "{%- endif -%}"
            "{%- endfor -%}"
            "{{ ns.rows | to_json }}"
        )
        try:
            devices = json.loads(await self.render_template(template))
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return []

        sensors: list[dict[str, Any]] = []
        for device in devices:
            temperature = humidity = occupancy = None
            for entity_id in device.get("entities", []):
                state = await self.get_state(entity_id)
                if state is None:
                    continue
                domain = entity_id.split(".", 1)[0]
                device_class = state.get("attributes", {}).get("device_class")
                if domain == "sensor" and device_class == "temperature" and temperature is None:
                    temperature = entity_id
                elif domain == "sensor" and device_class == "humidity" and humidity is None:
                    humidity = entity_id
                elif (
                    domain == "binary_sensor"
                    and device_class in ("occupancy", "motion", "presence")
                    and occupancy is None
                ):
                    occupancy = entity_id
            is_stat = bool(device.get("is_stat"))
            # A remote sensor must have a temperature; the thermostat's own
            # device is kept only for its occupancy (temp/humidity come from the
            # climate entity's own sample).
            if not is_stat and temperature is None:
                continue
            if is_stat and occupancy is None:
                continue
            sensors.append(
                {
                    "sensor_id": "ei:0" if is_stat else "rs:" + str(device["device"]),
                    "name": device.get("name") or str(device["device"]),
                    "is_stat": is_stat,
                    "temperature_entity": temperature,
                    "humidity_entity": humidity,
                    "occupancy_entity": occupancy,
                }
            )
        return sensors

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
