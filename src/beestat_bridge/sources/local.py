"""Local source: serve the ecobee API surface from Home Assistant data.

Thermostat objects = last archived cloud snapshot (if any) overlaid with live
values from the recorder; a minimal synthetic object if ecobee died before a
snapshot was ever captured.

Runtime reports = recorder samples aggregated into ecobee's 5-minute buckets.
Equipment runtime columns are measurements or blank, never guesses:

  1. equipment_sources binary sensors (ESPHome 24VAC monitor) when configured;
  2. deterministic hvac_action mapping ONLY where the declared system type
     makes it unambiguous (single-stage cooling; furnace heat);
  3. otherwise blank.

TODO(bridge): verify value scaling (temps, seconds) against archived real
runtimeReport responses once the cloud tee has data to compare with.
TODO(bridge): timezone handling — buckets currently use the container's local
time; should honor the thermostat snapshot's location.timeZone.
TODO(bridge): sensorList from HomeKit remote-sensor entities.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from ..settings import Settings, Thermostat
from ..store import Store

logger = logging.getLogger(__name__)

BUCKET_SECONDS = 300

# The exact column set beestat requests (api/runtime.php).
RUNTIME_COLUMNS = [
    "compCool1", "compCool2", "compHeat1", "compHeat2",
    "auxHeat1", "auxHeat2", "fan",
    "humidifier", "dehumidifier", "ventilator", "economizer",
    "hvacMode", "zoneAveTemp", "zoneHumidity",
    "outdoorTemp", "outdoorHumidity",
    "zoneCalendarEvent", "zoneClimate", "zoneCoolTemp", "zoneHeatTemp",
]

# hvac_action -> column, but only where the system type leaves no ambiguity.
# Heat on a heat pump (compressor vs aux vs stages) is exactly the thing we
# refuse to guess.
UNAMBIGUOUS_HEAT_COLUMN = {
    "furnace": "auxHeat1",     # ecobee reports furnace burn as auxHeat1
    "ac_furnace": "auxHeat1",
    "heat_pump": None,
    "heat_pump_electric_aux": None,
    "heat_pump_dual_fuel": None,
}

EQUIPMENT_COLUMN_MAP = {
    "comp_stage_1": "compCool1",   # cooling call; heat pumps heat with it too —
    "comp_stage_2": "compCool2",   # resolved per-bucket using hvac_action below.
    "aux_commanded": "auxHeat1",
    "fan": "fan",
}


def status_envelope(code: int = 0, message: str = "") -> dict[str, Any]:
    return {"status": {"code": code, "message": message}}


class LocalSource:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store

    # -- /1/thermostat ------------------------------------------------------

    def _resolve_serials(self, selection: dict[str, Any]) -> list[str]:
        if selection.get("selectionType") == "thermostats":
            requested = [
                serial.strip()
                for serial in str(selection.get("selectionMatch", "")).split(",")
                if serial.strip()
            ]
            configured = {thermostat.serial for thermostat in self._settings.thermostats}
            return [serial for serial in requested if serial in configured]
        # selectionType "registered": every configured thermostat.
        return [thermostat.serial for thermostat in self._settings.thermostats]

    def _latest_sample(self, serial: str) -> dict[str, Any] | None:
        import time

        now = int(time.time())
        samples = self._store.samples(serial, now - 3600, now + 1)
        return samples[-1] if samples else None

    def _synthetic_thermostat(self, thermostat: Thermostat) -> dict[str, Any]:
        """Bare-minimum object for the ecobee-died-before-first-sync case."""
        return {
            "identifier": thermostat.serial,
            "name": thermostat.homekit_entity.split(".", 1)[-1].replace("_", " ").title(),
            "modelNumber": "unknown",
            "utcTime": "",
            "runtime": {},
            "extendedRuntime": {}, "electricity": {}, "settings": {},
            "location": {}, "program": {"climates": [], "schedule": []},
            "events": [], "devices": [], "technician": {}, "utility": {},
            "management": {}, "alerts": [], "weather": {"forecasts": []},
            "houseDetails": {}, "oemCfg": {}, "equipmentStatus": "",
            "notificationSettings": {"emailAddresses": []},
            "privacy": {}, "version": {}, "remoteSensors": [], "audio": {},
        }

    def thermostat(self, body: dict[str, Any]) -> str:
        serials = self._resolve_serials(body.get("selection", {}))
        thermostat_list = []
        for serial in serials:
            thermostat = self._settings.thermostat_by_serial(serial)
            if thermostat is None:
                continue
            api_thermostat = self._store.snapshot(serial) or self._synthetic_thermostat(thermostat)

            sample = self._latest_sample(serial)
            if sample is not None:
                runtime = api_thermostat.setdefault("runtime", {})
                # TODO(bridge): unit handling — assumes HA reports °F.
                if sample["temperature"] is not None:
                    runtime["actualTemperature"] = round(sample["temperature"] * 10)
                if sample["humidity"] is not None:
                    runtime["actualHumidity"] = round(sample["humidity"])
                if sample["setpoint_heat"] is not None:
                    runtime["desiredHeat"] = round(sample["setpoint_heat"] * 10)
                if sample["setpoint_cool"] is not None:
                    runtime["desiredCool"] = round(sample["setpoint_cool"] * 10)
                runtime["connected"] = True
                api_thermostat["utcTime"] = dt.datetime.now(dt.timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                action = sample.get("hvac_action")
                api_thermostat["equipmentStatus"] = {
                    "cooling": "compCool1",
                    "heating": "heatPump" if thermostat.system_type.startswith("heat_pump") else "auxHeat1",
                    "fan": "fan",
                }.get(action, "")

            thermostat_list.append(api_thermostat)

        return json.dumps(
            {
                "page": {"page": 1, "totalPages": 1, "pageSize": len(thermostat_list),
                         "total": len(thermostat_list)},
                "thermostatList": thermostat_list,
                **status_envelope(),
            }
        )

    # -- /1/runtimeReport ---------------------------------------------------

    def _bucket_row(
        self,
        thermostat: Thermostat,
        bucket_start: int,
        samples: list[dict[str, Any]],
        columns: list[str],
    ) -> str:
        local = dt.datetime.fromtimestamp(bucket_start)
        values: dict[str, Any] = {column: "" for column in columns}

        if samples:
            def average(key: str) -> float | None:
                present = [sample[key] for sample in samples if sample[key] is not None]
                return sum(present) / len(present) if present else None

            def action_seconds(action: str) -> int:
                matching = sum(1 for sample in samples if sample.get("hvac_action") == action)
                return round(BUCKET_SECONDS * matching / len(samples))

            temperature = average("temperature")
            humidity = average("humidity")
            outdoor = average("outdoor_temperature")
            heat_setpoint = average("setpoint_heat")
            cool_setpoint = average("setpoint_cool")

            if temperature is not None:
                values["zoneAveTemp"] = round(temperature, 1)
            if humidity is not None:
                values["zoneHumidity"] = round(humidity)
            if outdoor is not None:
                values["outdoorTemp"] = round(outdoor, 1)
            if heat_setpoint is not None:
                values["zoneHeatTemp"] = round(heat_setpoint, 1)
            if cool_setpoint is not None:
                values["zoneCoolTemp"] = round(cool_setpoint, 1)
            values["hvacMode"] = {
                "heat": "heat", "cool": "cool", "heat_cool": "auto", "off": "off",
            }.get(samples[-1].get("hvac_mode") or "", "")
            values["zoneClimate"] = (samples[-1].get("preset") or "").capitalize()
            values["zoneCalendarEvent"] = ""

            # 1) Measured equipment sources (wire sensors) — authoritative.
            measured: dict[str, int] = {}
            with_equipment = [sample for sample in samples if sample.get("equipment")]
            for source_key, column in EQUIPMENT_COLUMN_MAP.items():
                if thermostat.equipment_sources.get(source_key) is None or not with_equipment:
                    continue
                on_count = sum(
                    1 for sample in with_equipment if sample["equipment"].get(source_key)
                )
                measured[column] = round(BUCKET_SECONDS * on_count / len(with_equipment))
            # Heat pump: a Y call while hvac_action is heating is compHeat, not
            # compCool. Attribute compressor seconds by dominant action.
            if measured and thermostat.system_type.startswith("heat_pump"):
                if action_seconds("heating") >= action_seconds("cooling"):
                    for cool_column, heat_column in (
                        ("compCool1", "compHeat1"), ("compCool2", "compHeat2"),
                    ):
                        if cool_column in measured:
                            measured[heat_column] = measured.pop(cool_column)
            values.update(measured)

            # 2) Deterministic hvac_action mapping, only where unambiguous and
            #    only for columns without a measured source.
            if thermostat.hvac_action_mapping:
                if "compCool1" not in measured and "compHeat1" not in measured:
                    cooling = action_seconds("cooling")
                    if cooling:
                        values["compCool1"] = cooling
                    heat_column = UNAMBIGUOUS_HEAT_COLUMN[thermostat.system_type]
                    heating = action_seconds("heating")
                    if heat_column is not None and heating:
                        values[heat_column] = heating
                if "fan" not in measured:
                    fan = (
                        action_seconds("fan")
                        + action_seconds("cooling")
                        + action_seconds("heating")
                    )
                    if fan:
                        values["fan"] = min(fan, BUCKET_SECONDS)

        cells = [local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")]
        cells += [str(values[column]) for column in columns]
        return ",".join(cells)

    def runtime_report(self, body: dict[str, Any]) -> str:
        selection = body.get("selection", {})
        serials = self._resolve_serials(selection)
        columns = [column for column in str(body.get("columns", "")).split(",") if column]
        if not columns:
            columns = list(RUNTIME_COLUMNS)

        start_date = body.get("startDate")
        end_date = body.get("endDate")
        start_interval = int(body.get("startInterval", 0))
        end_interval = int(body.get("endInterval", 287))

        begin_local = dt.datetime.strptime(start_date, "%Y-%m-%d") + dt.timedelta(
            seconds=start_interval * BUCKET_SECONDS
        )
        end_local = dt.datetime.strptime(end_date, "%Y-%m-%d") + dt.timedelta(
            seconds=(end_interval + 1) * BUCKET_SECONDS
        )
        begin_ts = int(begin_local.timestamp())
        end_ts = int(end_local.timestamp())

        report_list = []
        for serial in serials:
            thermostat = self._settings.thermostat_by_serial(serial)
            if thermostat is None:
                continue
            samples = self._store.samples(serial, begin_ts, end_ts)
            by_bucket: dict[int, list[dict[str, Any]]] = {}
            for sample in samples:
                bucket = (sample["ts"] // BUCKET_SECONDS) * BUCKET_SECONDS
                by_bucket.setdefault(bucket, []).append(sample)

            row_list = []
            for bucket_start in range(
                (begin_ts // BUCKET_SECONDS) * BUCKET_SECONDS, end_ts, BUCKET_SECONDS
            ):
                row_list.append(
                    self._bucket_row(
                        thermostat, bucket_start, by_bucket.get(bucket_start, []), columns
                    )
                )
            report_list.append({"thermostatIdentifier": serial, "rowList": row_list})

        return json.dumps(
            {
                "startDate": start_date, "startInterval": start_interval,
                "endDate": end_date, "endInterval": end_interval,
                "columns": ",".join(columns),
                "reportList": report_list,
                "sensorList": [],
                **status_envelope(),
            }
        )
