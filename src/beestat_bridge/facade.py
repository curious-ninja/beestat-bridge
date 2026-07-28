"""The fake ecobee API surface beestat talks to, plus /admin.

Matches what beestat's api/ecobee.php actually sends:
  GET  /authorize?response_type=code&client_id=...&redirect_uri=...&state=...
  POST /token           (grant_type=authorization_code | refresh_token)
  GET  /1/thermostat?body=<json>&client_id=...
  GET  /1/runtimeReport?body=<json>&client_id=...

Beestat treats in-band status.code 14 as "refresh and retry", which is how
its token dance stays exercised against the bridge.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import traceback
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from . import ecobee_auth, login, settings as settings_module, tokens, ui
from .recorder import refresh_snapshots
from .sources.cloud import CloudAuthDead
from .sources.local import (
    EQUIPMENT_SECONDS_COLUMNS,
    RUNTIME_COLUMNS,
    LocalSource,
    status_envelope,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _context(request: Request) -> Any:
    return request.app.state.context


def _authorized(request: Request) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    context = _context(request)
    return tokens.verify_access_token(
        context.store.install_secret(), header.removeprefix("Bearer ")
    )


def _token_response(context: Any) -> dict[str, Any]:
    return {
        "access_token": tokens.mint_access_token(
            context.store.install_secret(), context.store.bridge_account_id()
        ),
        "token_type": "Bearer",
        "expires_in": tokens.ACCESS_TOKEN_LIFETIME,
        "refresh_token": tokens.mint_refresh_token(),
        "scope": "smartRead",
    }


# -- oauth ------------------------------------------------------------------

@router.get("/authorize")
async def authorize(
    redirect_uri: str,
    state: str | None = None,
    response_type: str | None = None,
    client_id: str | None = None,
    scope: str | None = None,
) -> RedirectResponse:
    """No consent screen: the bridge serves exactly one household. Bounce
    straight back to beestat with a code."""
    params = {"code": "bridge-code"}
    if state is not None:
        params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


@router.post("/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    code: str | None = Form(None),
    refresh_token: str | None = Form(None),
) -> JSONResponse:
    context = _context(request)
    if grant_type not in ("authorization_code", "refresh_token"):
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    # Single-household facade: any code/refresh token we issued is acceptable;
    # identity is constant (the stable bridge account id).
    return JSONResponse(_token_response(context))


# -- data endpoints ---------------------------------------------------------

# On-grab snapshot refresh: collapse beestat's near-simultaneous thermostat +
# sensor grabs into one cloud call, and bound how long a serve waits on the cloud.
_SNAPSHOT_REFRESH_DEBOUNCE = 60.0
_SNAPSHOT_REFRESH_TIMEOUT = 8.0


async def _maybe_refresh_snapshots(context: Any) -> None:
    """Best-effort, debounced cloud snapshot refresh in the serve path so
    comfort/inUse are current on each grab. Never raises: a dead cloud
    (CloudAuthDead) or a slow one (timeout) just leaves the last snapshot in
    place. On any outcome we record the attempt time so a dead cloud isn't
    retried on every single request."""
    now = time.monotonic()
    if now - getattr(context, "snapshot_refresh_at", 0.0) < _SNAPSHOT_REFRESH_DEBOUNCE:
        return
    async with context.snapshot_refresh_lock:
        if time.monotonic() - context.snapshot_refresh_at < _SNAPSHOT_REFRESH_DEBOUNCE:
            return
        try:
            await asyncio.wait_for(
                refresh_snapshots(context.cloud), timeout=_SNAPSHOT_REFRESH_TIMEOUT
            )
        except CloudAuthDead:
            logger.debug("on-grab snapshot refresh skipped: not connected to ecobee")
        except Exception:
            logger.debug("on-grab snapshot refresh failed", exc_info=True)
        finally:
            context.snapshot_refresh_at = time.monotonic()


async def _serve(request: Request, endpoint: str, body: str | None) -> PlainTextResponse:
    context = _context(request)
    if not _authorized(request):
        # In-band expired-token signal; beestat refreshes and retries.
        return PlainTextResponse(
            json.dumps(status_envelope(14, "Authentication token has expired.")),
            media_type="application/json",
        )
    try:
        parsed_body = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return PlainTextResponse(
            json.dumps(status_envelope(4, "Bad request body.")), media_type="application/json"
        )

    mode = context.mode_manager.effective_mode()
    try:
        if mode == "cloud":
            handler = getattr(
                context.cloud, "thermostat" if endpoint == "thermostat" else "runtime_report"
            )
            payload = await handler(parsed_body)
        else:
            # Comfort mode and sensor inUse are cloud-only and live in the
            # /1/thermostat object. Refresh the snapshot from the cloud right
            # before serving it so beestat gets them current on each grab
            # (debounced + best-effort: a slow or dead cloud never blocks or
            # breaks the local serve).
            if endpoint == "thermostat":
                await _maybe_refresh_snapshots(context)
            handler = getattr(
                context.local, "thermostat" if endpoint == "thermostat" else "runtime_report"
            )
            payload = handler(parsed_body)
    except CloudAuthDead as error:
        await context.mode_manager.mark_cloud_dead(context.ha, str(error))
        if context.settings.auto_failover:
            handler = getattr(
                context.local, "thermostat" if endpoint == "thermostat" else "runtime_report"
            )
            payload = handler(parsed_body)
        else:
            payload = json.dumps(status_envelope(2, f"Cloud auth failed: {error}"))
    except Exception as error:
        # Never answer a crash with ecobee status 3 "Processing error.": beestat
        # maps that message to its benign code 10512 ("pretend it worked and
        # move on") and advances its runtime-sync cursor past the window with no
        # data — so a persistent bug here freezes the graphs while every sync
        # reports success. Status 4 maps to a hard beestat-side error (10504)
        # that holds the cursor, shows up in the beestat-sync log, and is
        # retried on the next tick; the traceback logged above has the cause.
        logger.exception("%s failed (mode=%s)", endpoint, mode)
        message = f"Bridge {endpoint} serve failed (mode={mode}): {error!r}"
        payload = json.dumps(status_envelope(4, message[:300]))

    return PlainTextResponse(payload, media_type="application/json")


@router.get("/1/thermostat")
async def thermostat(request: Request, body: str | None = Query(None)) -> PlainTextResponse:
    return await _serve(request, "thermostat", body)


@router.get("/1/runtimeReport")
async def runtime_report(request: Request, body: str | None = Query(None)) -> PlainTextResponse:
    return await _serve(request, "runtimeReport", body)


# -- admin ------------------------------------------------------------------

@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/admin/status")
async def admin_status(request: Request) -> dict[str, Any]:
    context = _context(request)
    return {
        "effective_mode": context.mode_manager.effective_mode(),
        "configured_mode": context.settings.mode,
        "mode_override": context.store.mode_override(),
        "cloud_failed_over": context.mode_manager.failed_over,
        "ecobee_tokens_present": context.store.ecobee_tokens() is not None,
        "snapshots": context.store.snapshot_identifiers(),
        "thermostats": [thermostat.serial for thermostat in context.settings.thermostats],
        "recorder_running": context.recorder_running,
    }


@router.post("/admin/mode")
async def admin_set_mode(request: Request) -> dict[str, Any]:
    context = _context(request)
    payload = await request.json()
    context.mode_manager.set_override(payload.get("mode"))  # null clears override
    return {"effective_mode": context.mode_manager.effective_mode()}


@router.post("/admin/ecobee/tokens")
async def admin_set_ecobee_tokens(request: Request) -> dict[str, Any]:
    """Escape hatch: store a refresh token obtained elsewhere."""
    context = _context(request)
    payload = await request.json()
    if not payload.get("refresh_token"):
        return {"error": "refresh_token required"}
    context.store.set_ecobee_tokens(
        refresh_token=payload["refresh_token"],
        access_token=payload.get("access_token"),
    )
    context.mode_manager.failed_over = False
    return {"stored": True}


# -- setup UI + interactive ecobee login ------------------------------------

@router.get("/")
async def setup_page() -> HTMLResponse:
    return HTMLResponse(ui.PAGE)


async def _finish_login(context: Any, body: dict[str, str]) -> dict[str, Any]:
    context.store.set_ecobee_tokens(
        refresh_token=body["refresh_token"], access_token=body.get("access_token")
    )
    context.mode_manager.failed_over = False
    if context.ecobee_login is not None:
        await context.ecobee_login.close()
        context.ecobee_login = None
    logger.info("ecobee consumer login succeeded; cloud path connected")
    return {"connected": True}


@router.post("/admin/ecobee/login")
async def admin_ecobee_login(request: Request) -> dict[str, Any]:
    """Run the consumer login (Auth0 universal login + PKCE). Credentials are
    used for this one exchange and never persisted."""
    context = _context(request)
    payload = await request.json()
    email, password = payload.get("email"), payload.get("password")
    if not email or not password:
        return {"error": "email and password required"}

    if context.ecobee_login is not None:  # Drop any stale half-done attempt.
        await context.ecobee_login.close()
    context.ecobee_login = login.LoginSession(context.settings.ecobee_client_id)
    try:
        body = await context.ecobee_login.start(email, password)
    except ecobee_auth.EcobeeMfaRequired as challenge:
        return {"mfa_required": True, "challenge_type": challenge.challenge_type}
    except ecobee_auth.EcobeeAuthError as error:
        await context.ecobee_login.close()
        context.ecobee_login = None
        return {"error": str(error)}
    return await _finish_login(context, body)


@router.post("/admin/ecobee/mfa")
async def admin_ecobee_mfa(request: Request) -> dict[str, Any]:
    context = _context(request)
    payload = await request.json()
    if context.ecobee_login is None:
        return {"error": "no login in progress; start over"}
    if not payload.get("code"):
        return {"error": "code required"}
    try:
        body = await context.ecobee_login.submit_mfa(payload["code"])
    except ecobee_auth.EcobeeAuthError as error:
        return {"error": str(error)}
    return await _finish_login(context, body)


# -- runtime configuration (the bridge's own config UI) ----------------------

@router.get("/admin/config")
async def admin_get_config(request: Request) -> dict[str, Any]:
    context = _context(request)
    return {
        "config": settings_module.editable_config(context.settings),
        "system_types": list(settings_module.VALID_SYSTEM_TYPES),
        "equipment_source_keys": list(settings_module.EQUIPMENT_SOURCE_KEYS),
        "ui_saved": context.store.runtime_config() is not None,
    }


@router.post("/admin/config")
async def admin_set_config(request: Request) -> dict[str, Any]:
    """Validate, persist, and apply in place — no restart needed."""
    context = _context(request)
    payload = await request.json()
    try:
        settings_module.apply_editable_config(context.settings, payload)
    except (ValueError, KeyError, TypeError) as error:
        return {"error": str(error)}
    context.store.set_runtime_config(settings_module.editable_config(context.settings))
    logger.info(
        "runtime config saved via UI: %d thermostat(s)", len(context.settings.thermostats)
    )
    return {"saved": True, "config": settings_module.editable_config(context.settings)}


@router.get("/admin/thermostats")
async def admin_thermostats(request: Request) -> dict[str, Any]:
    """Live view for the config UI: each configured thermostat's most recent
    reading plus its auto-discovered remote sensors, straight from the recorder
    store. Lets you confirm a thermostat is the right one and see what sensors
    were found — no manual mapping."""
    context = _context(request)
    now = int(time.time())
    thermostats = []
    for thermostat in context.settings.thermostats:
        serial = thermostat.serial
        samples = context.store.samples(serial, now - 3600, now + 1)
        latest = samples[-1] if samples else None
        current = None
        if latest is not None:
            current = {
                "temperature": latest.get("temperature"),
                "humidity": latest.get("humidity"),
                "hvac_mode": latest.get("hvac_mode"),
                "hvac_action": latest.get("hvac_action"),
                "setpoint_heat": latest.get("setpoint_heat"),
                "setpoint_cool": latest.get("setpoint_cool"),
                "ts": latest.get("ts"),
            }
        local = LocalSource(context.settings, context.store)
        names = local.reconciled_sensor_names(serial)
        freshness = local.snapshot_freshness(serial)
        stale = freshness["stale"]
        # Comfort mode is cloud-only (HomeKit doesn't expose it); once the
        # snapshot is stale we don't know it, so hide it rather than show frozen
        # info. Same for each sensor's inUse.
        if current is not None:
            current["comfort"] = None if stale else local.current_comfort(serial)
        sensors = []
        for meta in context.store.sensor_meta(serial):
            if meta["sensor_id"] == "ei:0":  # the thermostat's own device
                continue
            reading = context.store.latest_sensor_sample(serial, meta["sensor_id"])
            display_name, in_use = names.get(meta["sensor_id"], (meta["name"], True))
            sensors.append(
                {
                    "name": display_name,
                    "in_use": None if stale else in_use,
                    "type": meta["type"],
                    "temperature": reading.get("temperature") if reading else None,
                    "occupancy": None
                    if not reading or reading.get("occupancy") is None
                    else bool(reading["occupancy"]),
                }
            )
        thermostats.append(
            {
                "serial": serial,
                "homekit_entity": thermostat.homekit_entity,
                "current": current,
                "sensors": sensors,
                "cloud": {"stale": stale, "age": freshness["age"]},
            }
        )
    return {"thermostats": thermostats}


@router.get("/admin/discover")
async def admin_discover(request: Request) -> dict[str, Any]:
    """Troubleshooting: for each thermostat, the raw device/entity topology the
    discovery sees plus what it managed to classify. Used by the UI's Diagnose
    button when no sensors show up."""
    context = _context(request)
    if context.ha is None:
        return {"error": "Home Assistant is not connected"}
    thermostats = []
    for thermostat in context.settings.thermostats:
        thermostats.append(
            {
                "serial": thermostat.serial,
                "homekit_entity": thermostat.homekit_entity,
                "classified": await context.ha.discover_sensors(thermostat.homekit_entity),
                "raw": await context.ha.discover_sensors_debug(thermostat.homekit_entity),
            }
        )
    return {"thermostats": thermostats}


@router.get("/admin/archive/sensors")
async def admin_archive_sensors(request: Request) -> dict[str, Any]:
    """Truth-check for 'why weren't my remote sensors in the cloud graph': inspect
    the last archived cloud responses to see exactly what ecobee returned. If the
    remoteSensors listed the sensors but the runtimeReport sensorList did not, the
    gap is ecobee's history, not registration; if both listed them, it's a beestat
    sync/registration issue a re-sync can fix."""
    context = _context(request)

    def summarize_thermostat(archive: dict[str, Any] | None) -> Any:
        if archive is None:
            return {"found": False, "note": "no archived cloud /1/thermostat yet"}
        body = json.loads(archive["response"])
        out = []
        for stat in body.get("thermostatList", []):
            remote = stat.get("remoteSensors") or []
            out.append({
                "identifier": stat.get("identifier"),
                "remote_sensor_count": len(remote),
                "remote_sensors": [
                    {"id": s.get("id"), "name": s.get("name"), "type": s.get("type"),
                     "inUse": s.get("inUse")}
                    for s in remote
                ],
            })
        return {"found": True, "age_seconds": int(time.time()) - archive["ts"], "thermostats": out}

    def summarize_runtime(archive: dict[str, Any] | None) -> Any:
        if archive is None:
            return {"found": False, "note": "no archived cloud /1/runtimeReport yet"}
        body = json.loads(archive["response"])
        out = []
        for entry in body.get("sensorList", []):
            columns = entry.get("columns") or []
            # Sensor columns look like "<sensorId>:<capabilityId>"; the thermostat's
            # own is "ei:0:*". Anything else is a remote sensor.
            sensor_ids = sorted({c.rsplit(":", 1)[0] for c in columns if ":" in c})
            out.append({
                "identifier": entry.get("thermostatIdentifier"),
                "column_count": len(columns),
                "sensor_ids_in_columns": sensor_ids,
                "remote_sensor_ids": [s for s in sensor_ids if not s.startswith("ei:")],
                "data_rows": len(entry.get("data") or []),
            })
        return {"found": True, "age_seconds": int(time.time()) - archive["ts"],
                "sensor_lists": out}

    return {
        "thermostat_remoteSensors": summarize_thermostat(context.store.latest_archive("thermostat")),
        "runtimeReport_sensorList": summarize_runtime(context.store.latest_archive("runtimeReport")),
    }


@router.get("/admin/sensors/identity")
async def admin_sensors_identity(request: Request) -> dict[str, Any]:
    """Side-by-side identifiers so we can pick a stable key to match a Home
    Assistant sensor to its ecobee-cloud sensor instead of relying on names:
    ecobee's id/code/name (from the snapshot) vs HA's serial/model/name (from the
    device registry)."""
    context = _context(request)
    thermostats = []
    for thermostat in context.settings.thermostats:
        serial = thermostat.serial
        snapshot = context.store.snapshot(serial) or {}
        ecobee = [
            {"id": s.get("id"), "code": s.get("code"), "name": s.get("name"),
             "type": s.get("type")}
            for s in (snapshot.get("remoteSensors") or [])
        ]
        ha_sensors = []
        if context.ha is not None:
            try:
                ha_sensors = [
                    {"name": s.get("name"), "serial": s.get("serial"),
                     "model": s.get("model"), "is_stat": s.get("is_stat")}
                    for s in await context.ha.discover_sensors(thermostat.homekit_entity)
                ]
            except Exception:  # diagnostics must never throw
                logger.debug("identity discovery failed", exc_info=True)
        thermostats.append({
            "serial": serial,
            "homekit_entity": thermostat.homekit_entity,
            "ecobee_sensors": ecobee,
            "ha_sensors": ha_sensors,
            # How each recorded sensor is actually being emitted right now
            # (emitted_as == an ecobee rs2:* id means the match fired).
            "resolved": LocalSource(context.settings, context.store).sensor_identity_map(serial),
        })
    return {"thermostats": thermostats}


@router.get("/admin/selftest/runtime")
async def admin_selftest_runtime(request: Request) -> dict[str, Any]:
    """One-click truth-check for 'the beestat graphs are frozen': serve the same
    runtimeReport beestat's sync requests (last ~24h, its exact column set)
    in-process and report what came back — or the exact traceback if it failed.
    Needed because a failure here can be invisible in beestat's own log: its
    sync may swallow the error envelope and report success with no data."""
    context = _context(request)
    local = LocalSource(context.settings, context.store)
    serials = [thermostat.serial for thermostat in context.settings.thermostats]
    if not serials:
        return {"ok": False, "error": "no thermostats configured for local data"}

    # Build the window exactly the way beestat does: UTC wall-time labels
    # (its PHP is pinned to UTC), so this test exercises the same request
    # interpretation as a real sync — a window bug can't hide from it.
    now_utc = dt.datetime.now(dt.timezone.utc)
    body = {
        "selection": {"selectionType": "thermostats", "selectionMatch": ",".join(serials)},
        "startDate": (now_utc - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        "endDate": now_utc.strftime("%Y-%m-%d"),
        "startInterval": 0,
        "endInterval": 287,
        "columns": ",".join(RUNTIME_COLUMNS),
        "includeSensors": True,
    }

    try:
        report = json.loads(local.runtime_report(body))
    except Exception:
        return {
            "ok": False,
            "request": body,
            "error": traceback.format_exc(),
            "note": "This exact failure breaks beestat's runtime sync — the "
                    "thermostat and sensor graphs cannot advance until it is fixed.",
        }

    status = report.get("status") or {}
    result: dict[str, Any] = {
        "ok": status.get("code") == 0,
        "effective_mode": context.mode_manager.effective_mode(),
        "status": status,
        "request": body,
        "thermostats": [],
        "sensor_lists": [],
    }

    columns = [c for c in str(report.get("columns", "")).split(",") if c]
    # A row only lands on the graph if beestat accepts it: it discards any row
    # with a blank (null) cell in these columns (api/runtime.php).
    required = set(EQUIPMENT_SECONDS_COLUMNS) | {"HVACmode", "zoneAveTemp", "zoneHumidity"}
    required_indexes = [i for i, c in enumerate(columns) if c in required]
    for entry in report.get("reportList", []):
        rows = entry.get("rowList") or []
        non_blank = accepted = 0
        last_accepted = None
        for row in rows:
            cells = row.split(",")
            data_cells = cells[2:]
            if any(cell != "" for cell in data_cells):
                non_blank += 1
                if all(
                    i + 2 < len(cells) and cells[i + 2] != "" for i in required_indexes
                ):
                    accepted += 1
                    last_accepted = cells[0] + " " + cells[1]
        result["thermostats"].append({
            "identifier": entry.get("thermostatIdentifier"),
            "rows": len(rows),
            "rows_with_data": non_blank,
            "rows_beestat_accepts": accepted,
            "last_accepted_row": last_accepted,
        })

    for entry in report.get("sensorList", []):
        sensor_columns = entry.get("columns") or []
        data = entry.get("data") or []
        non_blank = 0
        last_non_blank = None
        for row in data:
            cells = row.split(",")
            if any(cell != "" for cell in cells[2:]):
                non_blank += 1
                last_non_blank = cells[0] + " " + cells[1]
        result["sensor_lists"].append({
            "identifier": entry.get("thermostatIdentifier"),
            "columns": sensor_columns,
            "rows": len(data),
            "rows_with_data": non_blank,
            "last_row_with_data": last_non_blank,
        })

    return result


@router.get("/admin/ha/entities")
async def admin_ha_entities(request: Request) -> dict[str, list[str]]:
    """Entity ids for the config UI's pickers, grouped by how they're used."""
    context = _context(request)
    groups: dict[str, list[str]] = {"climate": [], "binary_sensor": [], "outdoor": []}
    if context.ha is None:
        return groups
    try:
        states = await context.ha.get_states()
    except Exception:
        logger.exception("could not list HA entities")
        return groups
    for state in states:
        entity_id = state.get("entity_id", "")
        domain = entity_id.split(".", 1)[0]
        if domain == "climate":
            groups["climate"].append(entity_id)
        elif domain == "binary_sensor":
            groups["binary_sensor"].append(entity_id)
        elif domain in ("weather", "sensor"):
            groups["outdoor"].append(entity_id)
    for group in groups.values():
        group.sort()
    return groups
