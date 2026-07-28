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
runtimeReport responses once the cloud tee has data to compare with. (The read
side is confirmed against beestat's PHP — temps in tenths, occupancy 1/0 — but
not yet checked empirically against a captured real response.)

runtimeReport buckets are labeled in the thermostat's location.timeZone (from
the cloud snapshot), falling back to the container's local time when no snapshot
exists yet.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..settings import Settings, Thermostat
from ..store import Store

logger = logging.getLogger(__name__)

BUCKET_SECONDS = 300

# Cloud-only fields (sensor inUse, current comfort) come from the last cloud
# snapshot. If nothing has refreshed it in this long, treat it as dead so the
# config UI can hide those values instead of showing frozen, possibly-false info.
SNAPSHOT_STALE_SECONDS = 24 * 3600

# Deterministic capability ids by type, so the thermostat object's remoteSensors
# and the runtimeReport sensorList agree on the "<sensor_id>:<capability_id>"
# column keys beestat matches sensor history against.
CAPABILITY_ID = {"temperature": "1", "humidity": "2", "occupancy": "3"}

# The exact column set beestat requests (api/runtime.php).
RUNTIME_COLUMNS = [
    "compCool1", "compCool2", "compHeat1", "compHeat2",
    "auxHeat1", "auxHeat2", "fan",
    "humidifier", "dehumidifier", "ventilator", "economizer",
    "HVACmode", "zoneAveTemp", "zoneHumidity",
    "outdoorTemp", "outdoorHumidity",
    "zoneCalendarEvent", "zoneClimate", "zoneCoolTemp", "zoneHeatTemp",
]

# ecobee returns the column requested as 'hvacMode' under the capitalized name
# 'HVACmode', and beestat reads it by the RESPONSE name. Map requested names to
# the names ecobee actually returns so the local source matches.
RESPONSE_COLUMN_NAMES = {"hvacMode": "HVACmode"}

# Equipment-runtime columns beestat requires to be NON-null on every row: its
# sync (api/runtime.php) maps an empty CSV cell to null and then throws the whole
# row away if any of these is null. Real ecobee reports 0 (not blank) for idle
# equipment, so a row with real data must carry 0 here, never "".
EQUIPMENT_SECONDS_COLUMNS = [
    "compCool1", "compCool2", "compHeat1", "compHeat2",
    "auxHeat1", "auxHeat2", "fan",
    "humidifier", "dehumidifier", "ventilator", "economizer",
]

# HA climate hvac_mode -> ecobee HVACmode. beestat looks the result up in a fixed
# map and requires it non-null, so unknown modes fall back to a valid value.
HVAC_MODE_MAP = {
    "heat": "heat", "cool": "cool", "heat_cool": "auto", "auto": "auto", "off": "off",
}

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

    def _known_serials(self) -> list[str]:
        """Every thermostat we can serve in local mode: those configured for live
        local data first, then any others we still hold a cloud snapshot for.
        The latter are served as their last-known snapshot (old data) so beestat
        keeps them visible and selectable — the thermostat-swap control needs more
        than one thermostat to appear."""
        serials = [thermostat.serial for thermostat in self._settings.thermostats]
        for serial in self._store.snapshot_identifiers():
            if serial not in serials:
                serials.append(serial)
        return serials

    def _resolve_serials(self, selection: dict[str, Any]) -> list[str]:
        known = self._known_serials()
        if selection.get("selectionType") == "thermostats":
            requested = [
                serial.strip()
                for serial in str(selection.get("selectionMatch", "")).split(",")
                if serial.strip()
            ]
            known_set = set(known)
            return [serial for serial in requested if serial in known_set]
        # selectionType "registered": everything we can serve.
        return known

    def _latest_sample(self, serial: str) -> dict[str, Any] | None:
        import time

        now = int(time.time())
        samples = self._store.samples(serial, now - 3600, now + 1)
        return samples[-1] if samples else None

    @staticmethod
    def _ensure_runtime_keys(api_thermostat: dict[str, Any]) -> dict[str, Any]:
        """beestat's ecobee_thermostat sync dereferences these runtime keys with
        no isset() guard (api/ecobee_thermostat.php), so a missing one aborts its
        whole sync. Guarantee they exist; where we have no real value, use
        out-of-range sentinels beestat maps to null (temps /10 outside its bounds,
        humidity outside 0-100)."""
        runtime = api_thermostat.setdefault("runtime", {})
        runtime.setdefault("actualTemperature", -10000)
        runtime.setdefault("actualHumidity", -1)
        runtime.setdefault("desiredHeat", -10000)
        runtime.setdefault("desiredCool", -10000)
        runtime.setdefault("firstConnected", "")
        return api_thermostat

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
                # Not configured for local data — serve the last cloud snapshot
                # verbatim (old data), so it stays visible and selectable even
                # though we record nothing new for it.
                snapshot = self._store.snapshot(serial)
                if snapshot is not None:
                    thermostat_list.append(self._ensure_runtime_keys(snapshot))
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

            api_thermostat["remoteSensors"] = self._remote_sensors(serial, api_thermostat, sample)
            thermostat_list.append(self._ensure_runtime_keys(api_thermostat))

        return json.dumps(
            {
                "page": {"page": 1, "totalPages": 1, "pageSize": len(thermostat_list),
                         "total": len(thermostat_list)},
                "thermostatList": thermostat_list,
                **status_envelope(),
            }
        )

    @staticmethod
    def _capability(type_: str, value: Any) -> dict[str, str]:
        return {"id": CAPABILITY_ID[type_], "type": type_, "value": str(value)}

    @staticmethod
    def _normalize_name(name: str) -> str:
        return "".join(ch for ch in (name or "").lower() if ch.isalnum())

    def _stat_prefix(self, serial: str, fallback: str = "") -> str:
        """The thermostat's HA/HomeKit device name — HomeKit prefixes each remote
        sensor's name with it (e.g. "Upstairs Bedroom"), which we strip so local
        names line up with ecobee's ("Bedroom")."""
        for meta in self._store.sensor_meta(serial):
            if meta["sensor_id"] == "ei:0" and meta.get("name"):
                return meta["name"]
        snapshot = self._store.snapshot(serial) or {}
        return snapshot.get("name") or fallback

    def _strip_prefix(self, name: str, prefix: str) -> str:
        if prefix and name.lower().startswith(prefix.lower()):
            rest = name[len(prefix):].lstrip(" -_:")
            if rest:
                return rest
        return name

    def _official_sensors(self, serial: str) -> dict[str, dict[str, Any]]:
        """Index of the thermostat's ecobee-official remote sensors from the last
        cloud snapshot, keyed by "id:<id>" and "name:<normalized>", so local
        readings can borrow the real name and inUse flag. Empty if this install
        never synced from the cloud — callers then keep the Home Assistant name."""
        snapshot = self._store.snapshot(serial) or {}
        index: dict[str, dict[str, Any]] = {}
        for sensor in snapshot.get("remoteSensors") or []:
            entry = {
                "id": sensor.get("id"),
                "name": sensor.get("name"),
                "inUse": bool(sensor.get("inUse")),
            }
            if sensor.get("id"):
                index["id:" + str(sensor["id"])] = entry
            # ecobee's per-sensor pairing code — a stable physical id we can match
            # against the HA device serial without relying on names.
            if sensor.get("code"):
                index["code:" + str(sensor["code"])] = entry
            if sensor.get("name"):
                index["name:" + self._normalize_name(sensor["name"])] = entry
        return index

    def snapshot_freshness(self, serial: str) -> dict[str, Any]:
        """Whether the cloud snapshot is fresh enough to trust its cloud-only
        fields. `stale` is True when we've never synced or the last sync is older
        than SNAPSHOT_STALE_SECONDS."""
        updated_at = self._store.snapshot_updated_at(serial)
        stale = updated_at is None or (int(time.time()) - updated_at) > SNAPSHOT_STALE_SECONDS
        return {"updated_at": updated_at, "stale": stale, "age": (
            None if updated_at is None else int(time.time()) - updated_at
        )}

    def current_comfort(self, serial: str) -> str | None:
        """The thermostat's current comfort setting name (Home/Away/Sleep/…) from
        the last cloud snapshot. This is an ecobee-cloud concept — HomeKit does
        not expose it — so it's only as fresh as the snapshot. None if unknown."""
        program = (self._store.snapshot(serial) or {}).get("program") or {}
        ref = program.get("currentClimateRef")
        for climate in program.get("climates") or []:
            if climate.get("climateRef") == ref:
                return climate.get("name")
        return None

    def reconciled_sensor_names(self, serial: str) -> dict[str, tuple[str, bool]]:
        """Public: each stored sensor_id -> (display name, inUse), preferring the
        ecobee-official values. Used by the config UI so it names sensors the same
        way beestat does."""
        official = self._official_sensors(serial)
        prefix = self._stat_prefix(serial)
        return {
            meta["sensor_id"]: self._reconcile_sensor(
                meta["sensor_id"], meta["name"], prefix, official, meta.get("serial")
            )[1:]
            for meta in self._store.sensor_meta(serial)
        }

    def sensor_identity_map(self, serial: str) -> list[dict[str, Any]]:
        """Diagnostics: how each stored sensor resolves — its internal storage id
        and HA serial, and the ecobee id it's emitted under (so it's visible
        whether the cloud/local identity match actually fired)."""
        official = self._official_sensors(serial)
        prefix = self._stat_prefix(serial)
        out = []
        for meta in self._store.sensor_meta(serial):
            emit_id, name, in_use = self._reconcile_sensor(
                meta["sensor_id"], meta["name"], prefix, official, meta.get("serial")
            )
            out.append({
                "stored_id": meta["sensor_id"],
                "ha_name": meta["name"],
                "serial": meta.get("serial"),
                "emitted_as": emit_id,
                "resolved_name": name,
                "matched_cloud": emit_id != meta["sensor_id"] or meta["sensor_id"] == "ei:0",
            })
        return out

    def _reconcile_sensor(
        self, sensor_id: str, ha_name: str, prefix: str, official: dict[str, dict[str, Any]],
        serial: str | None = None,
    ) -> tuple[str, str, bool]:
        """Map a stored sensor to its ecobee-official (id, name, inUse). Prefer a
        stable-id match — the ecobee id, or the HA device serial against ecobee's
        pairing code/id — and only fall back to name (thermostat prefix stripped)
        when no id lines up. Emitting the ecobee id — not our HA-derived one —
        keeps a sensor's identity the same in local and cloud mode, so beestat
        attaches local data to the same sensor (and its history) instead of a
        duplicate. Falls back to the stored id / HA name / inUse=True with no
        cloud match."""
        ha_name = ha_name or sensor_id
        keys = ["id:" + sensor_id]
        if serial:
            keys += ["code:" + serial, "id:" + serial]
        keys += [
            "name:" + self._normalize_name(self._strip_prefix(ha_name, prefix)),
            "name:" + self._normalize_name(ha_name),
        ]
        match = next((official[k] for k in keys if k in official), None)
        if match:
            return (
                str(match.get("id") or sensor_id),
                match.get("name") or ha_name,
                match.get("inUse", True),
            )
        return sensor_id, ha_name, True

    def _remote_sensors(
        self, serial: str, api_thermostat: dict[str, Any], sample: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Build ecobee remoteSensors from auto-discovered HA sensors. ecobee
        encodes temperature in tenths of a degree and occupancy as "true"/"false"
        strings, all as strings — match that exactly so beestat parses it."""
        name = api_thermostat.get("name") or serial
        official = self._official_sensors(serial)
        prefix = self._stat_prefix(serial, fallback=name)
        sensors: list[dict[str, Any]] = []

        # Built-in thermostat sensor: temp/humidity come from the climate entity's
        # own sample; occupancy (if any) from the thermostat device's sensor.
        built: list[dict[str, str]] = []
        if sample is not None and sample.get("temperature") is not None:
            built.append(self._capability("temperature", round(sample["temperature"] * 10)))
        if sample is not None and sample.get("humidity") is not None:
            built.append(self._capability("humidity", round(sample["humidity"])))
        stat = self._store.latest_sensor_sample(serial, "ei:0")
        if stat is not None and stat.get("occupancy") is not None:
            built.append(self._capability("occupancy", "true" if stat["occupancy"] else "false"))
        if built:
            _, stat_name, stat_in_use = self._reconcile_sensor("ei:0", name, prefix, official)
            sensors.append(
                {"id": "ei:0", "name": stat_name, "type": "thermostat",
                 "inUse": stat_in_use, "capability": built}
            )

        # Auto-discovered remote sensors.
        for meta in self._store.sensor_meta(serial):
            if meta["sensor_id"] == "ei:0":
                continue
            latest = self._store.latest_sensor_sample(serial, meta["sensor_id"])
            capability: list[dict[str, str]] = []
            if latest is not None and latest.get("temperature") is not None:
                capability.append(self._capability("temperature", round(latest["temperature"] * 10)))
            if latest is not None and latest.get("humidity") is not None:
                capability.append(self._capability("humidity", round(latest["humidity"])))
            if latest is not None and latest.get("occupancy") is not None:
                capability.append(
                    self._capability("occupancy", "true" if latest["occupancy"] else "false")
                )
            emit_id, display_name, in_use = self._reconcile_sensor(
                meta["sensor_id"], meta["name"], prefix, official, meta.get("serial")
            )
            sensors.append(
                {
                    "id": emit_id,
                    "name": display_name,
                    "type": meta["type"],
                    "inUse": in_use,
                    "capability": capability,
                }
            )
        return sensors

    def _report_tz(self, serials: list[str]) -> ZoneInfo | None:
        """The thermostat's IANA time zone for labeling runtimeReport buckets in
        its local time (which is how beestat reads them back). Prefer the cloud
        snapshot's location.timeZone; fall back to the home's HA time zone
        (recorded by the recorder) so local mode stays correct even without a
        snapshot; finally None -> the container's local time."""
        candidates = []
        for serial in serials:
            snapshot = self._store.snapshot(serial) or {}
            candidates.append((snapshot.get("location") or {}).get("timeZone"))
        candidates.append(self._store.ha_time_zone())
        for tz_name in candidates:
            if tz_name:
                try:
                    return ZoneInfo(tz_name)
                except (ZoneInfoNotFoundError, ValueError):
                    continue
        return None

    def _sensor_list(
        self, serial: str, begin_ts: int, end_ts: int, tz: ZoneInfo | None = None
    ) -> dict[str, Any] | None:
        """Build the runtimeReport sensorList for one thermostat from recorded
        remote-sensor samples: columns "<sensor_id>:<capability_id>" (temperature
        in degrees, ×10'd by beestat on store; occupancy 1/0), 5-minute buckets."""
        official = self._official_sensors(serial)
        prefix = self._stat_prefix(serial)
        metas = [m for m in self._store.sensor_meta(serial) if m["sensor_id"] != "ei:0"]
        columns = ["date", "time"]
        specs: list[tuple[str, str]] = []  # (internal sensor_id, capability_type)
        buckets_by_sensor: dict[str, dict[int, list[dict[str, Any]]]] = {}
        present: list[dict[str, str]] = []  # {sensorId (emitted), sensorName, sensorType}
        for meta in metas:
            sensor_id = meta["sensor_id"]  # internal storage key
            samples = self._store.sensor_samples(serial, sensor_id, begin_ts, end_ts)
            if not samples:
                continue
            # Emit the ecobee-official id/name so this sensor's local history lands
            # on the same beestat sensor as its cloud history.
            emit_id, emit_name, _ = self._reconcile_sensor(
                sensor_id, meta["name"], prefix, official, meta.get("serial")
            )
            buckets: dict[int, list[dict[str, Any]]] = {}
            has_occupancy = False
            for sample in samples:
                bucket = (sample["ts"] // BUCKET_SECONDS) * BUCKET_SECONDS
                buckets.setdefault(bucket, []).append(sample)
                if sample["occupancy"] is not None:
                    has_occupancy = True
            buckets_by_sensor[sensor_id] = buckets
            present.append(
                {"sensorId": emit_id, "sensorName": emit_name, "sensorType": meta["type"],
                 "sensorUsage": "monitor"}
            )
            columns.append(emit_id + ":" + CAPABILITY_ID["temperature"])
            specs.append((sensor_id, "temperature"))
            if has_occupancy:
                columns.append(emit_id + ":" + CAPABILITY_ID["occupancy"])
                specs.append((sensor_id, "occupancy"))

        if not specs:
            return None

        data = []
        for bucket_start in range(
            (begin_ts // BUCKET_SECONDS) * BUCKET_SECONDS, end_ts, BUCKET_SECONDS
        ):
            local = dt.datetime.fromtimestamp(bucket_start, tz)
            cells = [local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")]
            for sensor_id, capability in specs:
                samples = buckets_by_sensor[sensor_id].get(bucket_start, [])
                if capability == "temperature":
                    values = [s["temperature"] for s in samples if s["temperature"] is not None]
                    cells.append(str(round(sum(values) / len(values), 1)) if values else "")
                else:  # occupancy — occupied if any sample in the bucket is occupied
                    occ = [s["occupancy"] for s in samples if s["occupancy"] is not None]
                    cells.append(("1" if any(occ) else "0") if occ else "")
            data.append(",".join(cells))

        return {
            "thermostatIdentifier": serial,
            "sensors": present,
            "columns": columns,
            "data": data,
        }

    # -- /1/runtimeReport ---------------------------------------------------

    def _bucket_row(
        self,
        thermostat: Thermostat,
        bucket_start: int,
        samples: list[dict[str, Any]],
        columns: list[str],
        tz: ZoneInfo | None = None,
    ) -> str:
        local = dt.datetime.fromtimestamp(bucket_start, tz)
        values: dict[str, Any] = {column: "" for column in columns}

        if samples:
            # Floor every required equipment column at 0 so beestat doesn't
            # discard the row (a blank cell becomes null, and any null equipment
            # column throws the whole row away). Measured/mapped seconds below
            # override these.
            for column in EQUIPMENT_SECONDS_COLUMNS:
                if column in values:
                    values[column] = 0

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
            values["HVACmode"] = HVAC_MODE_MAP.get(samples[-1].get("hvac_mode") or "", "off")
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
        # Snapshot-only thermostats (visible in local mode but with no local run
        # data) have no runtime to report. beestat requests runtimeReport one
        # thermostat at a time and dereferences reportList[0].rowList[0] with no
        # guard, so an empty report would crash it and blank rows would overwrite
        # its old history. Instead signal a benign "processing error" (ecobee
        # status 3), which beestat swallows (code 10512 -> "pretend it worked and
        # move on"), leaving the thermostat's existing graph data intact.
        serials = [s for s in serials if self._settings.thermostat_by_serial(s) is not None]
        if not serials:
            return json.dumps(status_envelope(3, "Processing error."))
        columns = [column for column in str(body.get("columns", "")).split(",") if column]
        if not columns:
            columns = list(RUNTIME_COLUMNS)
        # Emit the column names ecobee actually returns (e.g. hvacMode -> HVACmode),
        # which is what beestat reads them back by.
        columns = [RESPONSE_COLUMN_NAMES.get(column, column) for column in columns]

        start_date = body.get("startDate")
        end_date = body.get("endDate")
        start_interval = int(body.get("startInterval", 0))
        end_interval = int(body.get("endInterval", 287))

        # The REQUEST window is UTC: beestat pins PHP to UTC
        # (api/index.php date_default_timezone_set('UTC')), so the
        # startDate/startInterval labels it sends are UTC wall time — and the
        # real ecobee API interprets the runtimeReport request window as UTC
        # too. Only the RESPONSE rows are labeled in the thermostat's local
        # time (which beestat converts back using thermostat.time_zone) — that
        # is what tz below is for. Parsing the request window as thermostat
        # time shifted every served window by the UTC offset into the future,
        # so beestat's forward sync ([data_end-3h .. now]) was answered with
        # blank future buckets, ingested nothing, and the graphs froze while
        # every sync reported success.
        tz = self._report_tz(serials)
        begin_utc = dt.datetime.strptime(start_date, "%Y-%m-%d").replace(
            tzinfo=dt.timezone.utc
        ) + dt.timedelta(seconds=start_interval * BUCKET_SECONDS)
        end_utc = dt.datetime.strptime(end_date, "%Y-%m-%d").replace(
            tzinfo=dt.timezone.utc
        ) + dt.timedelta(seconds=(end_interval + 1) * BUCKET_SECONDS)
        begin_ts = int(begin_utc.timestamp())
        end_ts = int(end_utc.timestamp())

        report_list = []
        sensor_list = []
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
            row_error_logged = False
            for bucket_start in range(
                (begin_ts // BUCKET_SECONDS) * BUCKET_SECONDS, end_ts, BUCKET_SECONDS
            ):
                # One poisoned bucket must not take down the whole report (and
                # with it the thermostat graph); emit it blank — beestat
                # discards blank rows — and log the first failure per serve.
                try:
                    row = self._bucket_row(
                        thermostat, bucket_start, by_bucket.get(bucket_start, []), columns, tz
                    )
                except Exception:
                    if not row_error_logged:
                        logger.exception(
                            "%s: bucket row failed at ts=%d; emitting blank row",
                            serial, bucket_start,
                        )
                        row_error_logged = True
                    local_dt = dt.datetime.fromtimestamp(bucket_start, tz)
                    row = ",".join(
                        [local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M:%S")]
                        + [""] * len(columns)
                    )
                row_list.append(row)
            report_list.append({"thermostatIdentifier": serial, "rowList": row_list})

            # The sensorList is an add-on to the report; a failure building it
            # must not blank the thermostat rows too. Serve without it and log.
            try:
                sensors = self._sensor_list(serial, begin_ts, end_ts, tz)
            except Exception:
                logger.exception(
                    "%s: sensorList build failed; serving report without it", serial
                )
                sensors = None
            if sensors is not None:
                sensor_list.append(sensors)

        return json.dumps(
            {
                "startDate": start_date, "startInterval": start_interval,
                "endDate": end_date, "endInterval": end_interval,
                "columns": ",".join(columns),
                "reportList": report_list,
                "sensorList": sensor_list,
                **status_envelope(),
            }
        )
