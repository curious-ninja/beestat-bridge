"""SQLite persistence.

Write volume is tiny (one thermostat ~= 50 KB/day), so plain sqlite3 in WAL
mode behind a lock is deliberate — no ORM, no TSDB.

Tables:
  meta                key/value: install secret, bridge account id, mode override
  ecobee_oauth        the single real ecobee token pair (cloud mode)
  snapshots           last full /1/thermostat object per identifier (cloud tee)
  archive             every cloud response, verbatim, forever
  samples             recorder output: one row per poll per thermostat
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ecobee_oauth (
                  id INTEGER PRIMARY KEY CHECK (id = 1),
                  access_token TEXT,
                  refresh_token TEXT NOT NULL,
                  obtained_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                  identifier TEXT PRIMARY KEY,
                  body TEXT NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archive (
                  archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts INTEGER NOT NULL,
                  endpoint TEXT NOT NULL,
                  request TEXT NOT NULL,
                  response TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS archive_endpoint_ts
                  ON archive (endpoint, ts);
                CREATE TABLE IF NOT EXISTS samples (
                  identifier TEXT NOT NULL,
                  ts INTEGER NOT NULL,
                  temperature REAL,
                  humidity REAL,
                  setpoint_heat REAL,
                  setpoint_cool REAL,
                  hvac_mode TEXT,
                  hvac_action TEXT,
                  preset TEXT,
                  outdoor_temperature REAL,
                  equipment TEXT,
                  PRIMARY KEY (identifier, ts)
                );
                """
            )
            self._conn.commit()

    # -- meta ---------------------------------------------------------------

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def install_secret(self) -> str:
        """HMAC secret for bridge-minted tokens; created once per install."""
        with self._lock:
            secret = self._get_meta("install_secret")
            if secret is None:
                secret = secrets.token_urlsafe(32)
                self._set_meta("install_secret", secret)
            return secret

    def bridge_account_id(self) -> str:
        """Stable fake ecobee_account_id (uuid4, 36 chars — beestat requires
        exactly 36). Beestat keys the user account to this, so it must never
        change for the life of the install."""
        with self._lock:
            account_id = self._get_meta("bridge_account_id")
            if account_id is None:
                account_id = str(uuid.uuid4())
                self._set_meta("bridge_account_id", account_id)
            return account_id

    def mode_override(self) -> str | None:
        with self._lock:
            return self._get_meta("mode_override")

    def set_mode_override(self, mode: str | None) -> None:
        with self._lock:
            if mode is None:
                self._conn.execute("DELETE FROM meta WHERE key = 'mode_override'")
                self._conn.commit()
            else:
                self._set_meta("mode_override", mode)

    # -- ecobee oauth -------------------------------------------------------

    def ecobee_tokens(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM ecobee_oauth WHERE id = 1").fetchone()
            return dict(row) if row else None

    def set_ecobee_tokens(self, refresh_token: str, access_token: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ecobee_oauth (id, access_token, refresh_token, obtained_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET access_token = excluded.access_token, "
                "refresh_token = excluded.refresh_token, obtained_at = excluded.obtained_at",
                (access_token, refresh_token, int(time.time())),
            )
            self._conn.commit()

    # -- snapshots / archive ------------------------------------------------

    def upsert_snapshot(self, identifier: str, body: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (identifier, body, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (identifier) DO UPDATE SET body = excluded.body, "
                "updated_at = excluded.updated_at",
                (identifier, json.dumps(body), int(time.time())),
            )
            self._conn.commit()

    def snapshot(self, identifier: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM snapshots WHERE identifier = ?", (identifier,)
            ).fetchone()
            return json.loads(row["body"]) if row else None

    def snapshot_identifiers(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT identifier FROM snapshots").fetchall()
            return [row["identifier"] for row in rows]

    def archive_response(self, endpoint: str, request: dict[str, Any], response: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO archive (ts, endpoint, request, response) VALUES (?, ?, ?, ?)",
                (int(time.time()), endpoint, json.dumps(request), response),
            )
            self._conn.commit()

    # -- samples ------------------------------------------------------------

    def insert_sample(self, identifier: str, ts: int, values: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO samples (identifier, ts, temperature, humidity, "
                "setpoint_heat, setpoint_cool, hvac_mode, hvac_action, preset, "
                "outdoor_temperature, equipment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (identifier, ts) DO NOTHING",
                (
                    identifier,
                    ts,
                    values.get("temperature"),
                    values.get("humidity"),
                    values.get("setpoint_heat"),
                    values.get("setpoint_cool"),
                    values.get("hvac_mode"),
                    values.get("hvac_action"),
                    values.get("preset"),
                    values.get("outdoor_temperature"),
                    json.dumps(values.get("equipment")) if values.get("equipment") else None,
                ),
            )
            self._conn.commit()

    def samples(self, identifier: str, begin_ts: int, end_ts: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM samples WHERE identifier = ? AND ts >= ? AND ts < ? "
                "ORDER BY ts",
                (identifier, begin_ts, end_ts),
            ).fetchall()
        out = []
        for row in rows:
            sample = dict(row)
            sample["equipment"] = json.loads(sample["equipment"]) if sample["equipment"] else None
            out.append(sample)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
