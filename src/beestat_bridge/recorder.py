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
from .store import Store

logger = logging.getLogger(__name__)


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


async def run_recorder(settings: Settings, store: Store, ha: HomeAssistant) -> None:
    logger.info(
        "recorder started: %d thermostat(s), every %ds",
        len(settings.thermostats),
        settings.ha_poll_interval,
    )
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
        except Exception:  # Recorder must never die; it is the fallback's lifeline.
            logger.exception("recorder poll failed")
        await asyncio.sleep(settings.ha_poll_interval)
