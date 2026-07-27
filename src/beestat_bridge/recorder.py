"""Always-on local recorder.

Polls Home Assistant on an interval and persists one raw sample per
thermostat per poll — in BOTH modes, so the local dataset is warm and proven
long before it is ever needed. Aggregation into ecobee-style 5-minute buckets
happens at read time in sources/local.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .ha import HomeAssistant
from .settings import Settings, Thermostat
from .sources.cloud import CloudAuthDead, CloudSource
from .store import Store

logger = logging.getLogger(__name__)

# Even while serving local, refresh the cloud snapshot on this cadence so the
# cloud-only fields beestat shows (sensor inUse, current comfort, location
# timeZone) stay fresh. Comfortably under SNAPSHOT_STALE_SECONDS so a single
# failed refresh doesn't trip the "stale" flag.
SNAPSHOT_REFRESH_INTERVAL = 6 * 3600

# Ask for exactly the cloud-only objects local mode can't derive from HA.
_SNAPSHOT_REFRESH_BODY = {
    "selection": {
        "selectionType": "registered",
        "selectionMatch": "",
        "includeSensors": True,
        "includeProgram": True,
        "includeLocation": True,
        "includeEquipmentStatus": True,
        "includeSettings": True,
    }
}


async def run_snapshot_refresh(settings: Settings, store: Store, cloud: CloudSource) -> None:
    """Keep the per-thermostat cloud snapshot fresh so beestat's cloud-only
    fields don't silently freeze while we serve local. Uses whatever ecobee
    tokens exist; once they're gone the snapshot ages and the UI marks it
    stale (the user opted to keep beestat showing last-known values)."""
    while True:
        try:
            await cloud.thermostat(_SNAPSHOT_REFRESH_BODY)
            logger.info("cloud snapshot refreshed")
        except CloudAuthDead:
            logger.debug("cloud snapshot refresh skipped: not connected to ecobee")
        except Exception:  # Never let this loop die; it is best-effort.
            logger.exception("cloud snapshot refresh failed")
        await asyncio.sleep(SNAPSHOT_REFRESH_INTERVAL)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _read_outdoor(ha: HomeAssistant, entity_id: str | None) -> float | None:
    if entity_id is None:
        return None
    state = await ha.get_state(entity_id)
    if state is None:
        return None
    if entity_id.startswith("weather."):
        return _float_or_none(state.get("attributes", {}).get("temperature"))
    return _float_or_none(state.get("state"))


async def _read_thermostat(ha: HomeAssistant, thermostat: Thermostat) -> dict[str, Any] | None:
    state = await ha.get_state(thermostat.homekit_entity)
    if state is None or state.get("state") in ("unavailable", "unknown"):
        return None
    attributes = state.get("attributes", {})

    # Equipment binary sensors (future ESPHome 24VAC monitor). Recorded
    # verbatim; never inferred.
    equipment: dict[str, bool] = {}
    for column, entity_id in thermostat.equipment_sources.items():
        if entity_id is None:
            continue
        source_state = await ha.get_state(entity_id)
        if source_state is not None:
            equipment[column] = source_state.get("state") == "on"

    return {
        "temperature": _float_or_none(attributes.get("current_temperature")),
        "humidity": _float_or_none(attributes.get("current_humidity")),
        "setpoint_heat": _float_or_none(
            attributes.get("target_temp_low", attributes.get("temperature"))
        ),
        "setpoint_cool": _float_or_none(
            attributes.get("target_temp_high", attributes.get("temperature"))
        ),
        "hvac_mode": state.get("state"),
        "hvac_action": attributes.get("hvac_action"),
        "preset": attributes.get("preset_mode"),
        "equipment": equipment or None,
    }


SENSOR_DISCOVERY_TTL = 3600  # Re-scan the registry for sensors once an hour.


async def _read_sensor(ha: HomeAssistant, sensor: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {"temperature": None, "humidity": None, "occupancy": None}
    if sensor.get("temperature_entity"):
        state = await ha.get_state(sensor["temperature_entity"])
        if state is not None:
            values["temperature"] = _float_or_none(state.get("state"))
    if sensor.get("humidity_entity"):
        state = await ha.get_state(sensor["humidity_entity"])
        if state is not None:
            values["humidity"] = _float_or_none(state.get("state"))
    if sensor.get("occupancy_entity"):
        state = await ha.get_state(sensor["occupancy_entity"])
        if state is not None and state.get("state") not in ("unavailable", "unknown"):
            values["occupancy"] = state.get("state") == "on"
    return values


async def run_recorder(settings: Settings, store: Store, ha: HomeAssistant) -> None:
    logger.info(
        "recorder started: %d thermostat(s), every %ds",
        len(settings.thermostats),
        settings.ha_poll_interval,
    )
    # Cache of auto-discovered remote sensors per thermostat serial, refreshed
    # every SENSOR_DISCOVERY_TTL so registry changes are picked up without a
    # per-poll registry scan.
    discovered: dict[str, list[dict[str, Any]]] = {}
    discovered_at: dict[str, float] = {}
    while True:
        ts = int(time.time())
        try:
            outdoor = await _read_outdoor(ha, settings.outdoor_temperature)
            for thermostat in settings.thermostats:
                values = await _read_thermostat(ha, thermostat)
                if values is None:
                    logger.warning("entity %s unavailable", thermostat.homekit_entity)
                    continue
                values["outdoor_temperature"] = outdoor
                store.insert_sample(thermostat.serial, ts, values)

                # Remote sensors: refresh discovery on TTL, then sample each.
                if ts - discovered_at.get(thermostat.serial, 0) > SENSOR_DISCOVERY_TTL:
                    try:
                        sensors = await ha.discover_sensors(thermostat.homekit_entity)
                        discovered[thermostat.serial] = sensors
                        discovered_at[thermostat.serial] = ts
                        for sensor in sensors:
                            store.upsert_sensor_meta(
                                thermostat.serial,
                                sensor["sensor_id"],
                                sensor["name"],
                                "thermostat" if sensor["is_stat"] else "ecobee3_remote_sensor",
                            )
                        logger.info(
                            "%s: discovered %d remote sensor(s)",
                            thermostat.homekit_entity,
                            len([s for s in sensors if not s["is_stat"]]),
                        )
                    except Exception:
                        logger.exception("sensor discovery failed for %s", thermostat.homekit_entity)
                for sensor in discovered.get(thermostat.serial, []):
                    store.insert_sensor_sample(
                        thermostat.serial, sensor["sensor_id"], ts, await _read_sensor(ha, sensor)
                    )
        except Exception:  # Recorder must never die; it is the fallback's lifeline.
            logger.exception("recorder poll failed")
        await asyncio.sleep(settings.ha_poll_interval)
