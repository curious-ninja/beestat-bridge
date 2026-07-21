"""Configuration loading.

Sources, in precedence order:
  1. Environment variables (HA Supervisor token, explicit overrides).
  2. The config file: $BRIDGE_CONFIG, else /data/options.json (HA app),
     else ./config.yaml.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ECOBEE_API_BASE_URL = "https://api.ecobee.com"
DEFAULT_ECOBEE_AUTH_DOMAIN = "https://auth.ecobee.com"
# ecobee's public web-app client id; the same one ha-ecobee (Apache-2.0)
# authenticates with. Overridable in config.
DEFAULT_ECOBEE_CLIENT_ID = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"

VALID_MODES = ("cloud", "local")
VALID_SYSTEM_TYPES = (
    "furnace",
    "ac_furnace",
    "heat_pump",
    "heat_pump_electric_aux",
    "heat_pump_dual_fuel",
)

EQUIPMENT_SOURCE_KEYS = (
    "comp_stage_1",
    "comp_stage_2",
    "aux_commanded",
    "aux_defrost",
    "fan",
)


@dataclass
class Thermostat:
    serial: str
    homekit_entity: str
    system_type: str = "heat_pump_electric_aux"
    hvac_action_mapping: bool = True
    equipment_sources: dict[str, str | None] = field(
        default_factory=lambda: {key: None for key in EQUIPMENT_SOURCE_KEYS}
    )

    def __post_init__(self) -> None:
        if self.system_type not in VALID_SYSTEM_TYPES:
            raise ValueError(
                f"thermostat {self.serial}: unknown system_type {self.system_type!r}"
            )
        for key in self.equipment_sources:
            if key not in EQUIPMENT_SOURCE_KEYS:
                raise ValueError(
                    f"thermostat {self.serial}: unknown equipment source {key!r}"
                )


@dataclass
class Settings:
    mode: str = "cloud"
    auto_failover: bool = False
    port: int = 8127

    ha_url: str | None = None
    ha_token: str | None = None
    ha_poll_interval: int = 60
    ha_mode_entity: str | None = None

    ecobee_client_id: str = DEFAULT_ECOBEE_CLIENT_ID
    ecobee_auth_domain: str = DEFAULT_ECOBEE_AUTH_DOMAIN
    ecobee_api_base_url: str = DEFAULT_ECOBEE_API_BASE_URL

    outdoor_temperature: str | None = None
    thermostats: list[Thermostat] = field(default_factory=list)

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("BRIDGE_DATA", "./data")))

    @property
    def supervisor_token(self) -> str | None:
        return os.environ.get("SUPERVISOR_TOKEN")

    @property
    def ha_api_url(self) -> str | None:
        """Effective HA API base. Supervisor networking wins when present."""
        if self.supervisor_token is not None:
            return "http://supervisor/core/api"
        if self.ha_url is not None:
            return self.ha_url.rstrip("/") + "/api"
        return None

    @property
    def ha_api_token(self) -> str | None:
        return self.supervisor_token or (self.ha_token or None)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bridge.sqlite3"

    def thermostat_by_serial(self, serial: str) -> Thermostat | None:
        for thermostat in self.thermostats:
            if thermostat.serial == serial:
                return thermostat
        return None


def _find_config_file() -> Path | None:
    explicit = os.environ.get("BRIDGE_CONFIG")
    if explicit:
        return Path(explicit)
    for candidate in (Path("/data/options.json"), Path("config.yaml")):
        if candidate.exists():
            return candidate
    return None


def load_settings() -> Settings:
    path = _find_config_file()
    raw: dict[str, Any] = {}
    if path is not None:
        text = path.read_text()
        raw = json.loads(text) if path.suffix == ".json" else (yaml.safe_load(text) or {})

    ha = raw.get("home_assistant", {}) or {}
    ecobee = raw.get("ecobee", {}) or {}

    mode = raw.get("mode", "cloud")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")

    settings = Settings(
        mode=mode,
        auto_failover=bool(raw.get("auto_failover", False)),
        port=int(raw.get("port", 8127)),
        ha_url=ha.get("url"),
        ha_token=ha.get("token") or None,
        ha_poll_interval=int(ha.get("poll_interval", 60)),
        ha_mode_entity=ha.get("mode_entity"),
        ecobee_client_id=ecobee.get("client_id", DEFAULT_ECOBEE_CLIENT_ID),
        ecobee_auth_domain=ecobee.get("auth_domain", DEFAULT_ECOBEE_AUTH_DOMAIN),
        ecobee_api_base_url=ecobee.get("api_base_url", DEFAULT_ECOBEE_API_BASE_URL),
        outdoor_temperature=raw.get("outdoor_temperature"),
        thermostats=parse_thermostats(raw.get("thermostats", []) or []),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def parse_thermostats(entries: list[dict[str, Any]]) -> list[Thermostat]:
    """Equipment sources come nested (config.yaml, bridge UI) or flat as
    <key>_entity (HA app options.json, whose schema can't express maps)."""
    thermostats = [
        Thermostat(
            serial=str(entry["serial"]).strip(),
            homekit_entity=entry["homekit_entity"],
            system_type=entry.get("system_type", "heat_pump_electric_aux"),
            hvac_action_mapping=bool(entry.get("hvac_action_mapping", True)),
            equipment_sources={
                key: (entry.get("equipment_sources") or {}).get(key)
                or entry.get(f"{key}_entity")
                or None
                for key in EQUIPMENT_SOURCE_KEYS
            },
        )
        for entry in entries
    ]
    serials = [thermostat.serial for thermostat in thermostats]
    if len(set(serials)) != len(serials):
        raise ValueError("duplicate thermostat serial numbers")
    for thermostat in thermostats:
        if not thermostat.serial:
            raise ValueError("thermostat serial must not be empty")
        if not thermostat.homekit_entity.startswith("climate."):
            raise ValueError(
                f"thermostat {thermostat.serial}: homekit_entity must be a climate.* entity"
            )
    return thermostats


# Fields the bridge's own web UI may edit at runtime (persisted in SQLite,
# overriding the file / add-on options; applied in place, no restart).

def editable_config(settings: Settings) -> dict[str, Any]:
    return {
        "auto_failover": settings.auto_failover,
        "outdoor_temperature": settings.outdoor_temperature,
        "poll_interval": settings.ha_poll_interval,
        "mode_entity": settings.ha_mode_entity,
        "thermostats": [
            {
                "serial": thermostat.serial,
                "homekit_entity": thermostat.homekit_entity,
                "system_type": thermostat.system_type,
                "hvac_action_mapping": thermostat.hvac_action_mapping,
                "equipment_sources": dict(thermostat.equipment_sources),
            }
            for thermostat in settings.thermostats
        ],
    }


def apply_editable_config(settings: Settings, raw: dict[str, Any]) -> None:
    """Validate then mutate the live Settings in place. Every component holds
    a reference to this object and reads it per operation, so changes take
    effect immediately — no restart."""
    thermostats = parse_thermostats(raw.get("thermostats", []) or [])
    settings.thermostats = thermostats
    settings.auto_failover = bool(raw.get("auto_failover", settings.auto_failover))
    settings.outdoor_temperature = raw.get("outdoor_temperature") or None
    settings.ha_mode_entity = raw.get("mode_entity") or None
    settings.ha_poll_interval = max(15, int(raw.get("poll_interval", settings.ha_poll_interval)))
