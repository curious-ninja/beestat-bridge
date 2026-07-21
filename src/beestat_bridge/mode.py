"""Mode selection: which source answers the facade.

Precedence: runtime override (admin endpoint or the watched HA input_select)
> configured default. Auto-failover flips serving to local on conclusive
cloud auth death without touching the stored override, and notifies HA.
"""

from __future__ import annotations

import asyncio
import logging

from .ha import HomeAssistant
from .settings import VALID_MODES, Settings
from .store import Store

logger = logging.getLogger(__name__)


class ModeManager:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self.failed_over = False

    def effective_mode(self) -> str:
        override = self._store.mode_override()
        if override in VALID_MODES:
            return override
        if self.failed_over and self._settings.auto_failover:
            return "local"
        return self._settings.mode

    def set_override(self, mode: str | None) -> None:
        if mode is not None and mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}")
        self._store.set_mode_override(mode)
        logger.info("mode override set to %s (effective: %s)", mode, self.effective_mode())

    async def mark_cloud_dead(self, ha: HomeAssistant | None, reason: str) -> None:
        if self.failed_over:
            return
        self.failed_over = True
        logger.error("cloud auth conclusively failed: %s", reason)
        if ha is not None and self._settings.auto_failover:
            await ha.notify(
                "beestat-bridge: cloud path down",
                f"ecobee cloud auth failed ({reason}); serving beestat from "
                "local Home Assistant data. History already synced is safe.",
            )

    async def watch_ha_entity(self, ha: HomeAssistant) -> None:
        """Poll the configured input_select so the mode can be flipped from an
        HA dashboard. Poll (not subscribe) is fine at this frequency."""
        entity_id = self._settings.ha_mode_entity
        if entity_id is None:
            return
        last_seen: str | None = None
        while True:
            try:
                state = await ha.get_state(entity_id)
                value = state.get("state") if state else None
                if value in VALID_MODES and value != last_seen:
                    if last_seen is not None:  # Skip initial sync-up.
                        self.set_override(value)
                    last_seen = value
            except Exception:
                logger.exception("mode entity poll failed")
            await asyncio.sleep(15)
