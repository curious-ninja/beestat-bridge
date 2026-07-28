"""Smoke tests: token contract with beestat, facade auth dance, local source."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from beestat_bridge.main import create_app
from beestat_bridge.settings import Settings, Thermostat


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[
            Thermostat(serial="123456789012", homekit_entity="climate.test")
        ],
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _get_tokens(client):
    response = client.post("/token", data={"grant_type": "authorization_code", "code": "x"})
    assert response.status_code == 200
    return response.json()


def test_authorize_redirects_with_code_and_state(client):
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "https://beestat.local/api/ecobee_initialize.php",
            "state": "s123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://beestat.local/api/ecobee_initialize.php?")
    assert "code=" in location and "state=s123" in location


def test_token_satisfies_beestat_jwt_contract(client):
    """beestat's ecobee_token.php: 3-part JWT, sub == '<x>|<36 chars>'."""
    tokens = _get_tokens(client)
    parts = tokens["access_token"].split(".")
    assert len(parts) == 3
    payload = parts[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    prefix, account_id = claims["sub"].split("|")
    assert len(account_id) == 36
    # Account id must be stable across grants — beestat keys the user to it.
    account_id_2 = json.loads(
        base64.urlsafe_b64decode(
            (p := _get_tokens(client)["access_token"].split(".")[1]) + "=" * (-len(p) % 4)
        )
    )["sub"].split("|")[1]
    assert account_id == account_id_2


def test_data_endpoint_requires_token_and_signals_code_14(client):
    response = client.get("/1/thermostat", params={"body": "{}"})
    assert response.json()["status"]["code"] == 14


def test_local_thermostat_serves_synthetic_without_snapshot(client):
    tokens = _get_tokens(client)
    response = client.get(
        "/1/thermostat",
        params={"body": json.dumps({"selection": {"selectionType": "registered"}})},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    body = response.json()
    assert body["status"]["code"] == 0
    assert body["thermostatList"][0]["identifier"] == "123456789012"


def test_local_runtime_report_shape(client):
    tokens = _get_tokens(client)
    response = client.get(
        "/1/runtimeReport",
        params={
            "body": json.dumps(
                {
                    "selection": {
                        "selectionType": "thermostats",
                        "selectionMatch": "123456789012",
                    },
                    "startDate": "2026-07-20",
                    "endDate": "2026-07-20",
                    "startInterval": 0,
                    "endInterval": 11,
                    "columns": "compCool1,zoneAveTemp",
                    "includeSensors": True,
                }
            )
        },
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    body = response.json()
    assert body["status"]["code"] == 0
    assert body["columns"] == "compCool1,zoneAveTemp"
    rows = body["reportList"][0]["rowList"]
    assert len(rows) == 12  # intervals 0..11 inclusive
    date, time_, comp, temp = rows[0].split(",")
    assert date == "2026-07-20" and time_ == "00:00:00"


def test_local_remote_sensors_from_store(tmp_path):
    """Local source emits ecobee-shaped remoteSensors: a built-in thermostat
    sensor (temp/humidity from the climate sample) plus discovered remotes, with
    temperatures in tenths of a degree and occupancy as "true"/"false" strings."""
    import time

    from beestat_bridge.sources.local import LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="123456789012", homekit_entity="climate.test")],
    )
    store = Store(settings.db_path)
    ts = int(time.time())
    store.insert_sample("123456789012", ts, {"temperature": 72.4, "humidity": 45})
    store.upsert_sensor_meta("123456789012", "rs:abc", "Bedroom", "ecobee3_remote_sensor")
    store.insert_sensor_sample("123456789012", "rs:abc", ts, {"temperature": 70.1, "occupancy": True})

    result = json.loads(LocalSource(settings, store).thermostat({"selection": {"selectionType": "registered"}}))
    by_id = {s["id"]: s for s in result["thermostatList"][0]["remoteSensors"]}

    built = {c["type"]: c["value"] for c in by_id["ei:0"]["capability"]}
    assert by_id["ei:0"]["type"] == "thermostat"
    assert built["temperature"] == "724"
    assert built["humidity"] == "45"

    remote = by_id["rs:abc"]
    caps = {c["type"]: c["value"] for c in remote["capability"]}
    assert remote["type"] == "ecobee3_remote_sensor"
    assert remote["name"] == "Bedroom"
    assert caps["temperature"] == "701"
    assert caps["occupancy"] == "true"
    store.close()


def test_local_sensors_prefer_official_names_and_in_use(tmp_path):
    """When a cloud snapshot exists, remoteSensors borrow the ecobee-official
    name and inUse flag instead of the (thermostat-prefixed) HomeKit name."""
    import time

    from beestat_bridge.sources.local import LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="123456789012", homekit_entity="climate.test")],
    )
    store = Store(settings.db_path)
    # A prior cloud sync recorded the official sensor names / inUse.
    store.upsert_snapshot(
        "123456789012",
        {
            "identifier": "123456789012",
            "name": "Upstairs",
            "remoteSensors": [
                {"id": "ei:0", "name": "Upstairs", "type": "thermostat", "inUse": True},
                {"id": "rs:100", "name": "Bedroom", "type": "ecobee3_remote_sensor",
                 "inUse": False},
            ],
        },
    )
    ts = int(time.time())
    store.insert_sample("123456789012", ts, {"temperature": 72.4, "humidity": 45})
    # HomeKit prefixes the thermostat name onto the sensor's device name.
    store.upsert_sensor_meta(
        "123456789012", "rs:abc", "Upstairs Bedroom", "ecobee3_remote_sensor"
    )
    store.insert_sensor_sample("123456789012", "rs:abc", ts, {"temperature": 70.1})

    result = json.loads(
        LocalSource(settings, store).thermostat({"selection": {"selectionType": "registered"}})
    )
    by_id = {s["id"]: s for s in result["thermostatList"][0]["remoteSensors"]}
    # The HomeKit "Upstairs Bedroom" resolves to the official "Bedroom", inUse false.
    assert by_id["rs:abc"]["name"] == "Bedroom"
    assert by_id["rs:abc"]["inUse"] is False
    store.close()


def test_local_sensors_fall_back_to_ha_names_without_snapshot(tmp_path):
    """With no cloud snapshot (never connected), keep the Home Assistant name and
    default inUse to True."""
    import time

    from beestat_bridge.sources.local import LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="123456789012", homekit_entity="climate.test")],
    )
    store = Store(settings.db_path)
    ts = int(time.time())
    store.insert_sample("123456789012", ts, {"temperature": 72.4})
    store.upsert_sensor_meta(
        "123456789012", "rs:abc", "Upstairs Bedroom", "ecobee3_remote_sensor"
    )
    store.insert_sensor_sample("123456789012", "rs:abc", ts, {"temperature": 70.1})

    result = json.loads(
        LocalSource(settings, store).thermostat({"selection": {"selectionType": "registered"}})
    )
    by_id = {s["id"]: s for s in result["thermostatList"][0]["remoteSensors"]}
    assert by_id["rs:abc"]["name"] == "Upstairs Bedroom"
    assert by_id["rs:abc"]["inUse"] is True
    store.close()


def test_snapshot_freshness_and_comfort(tmp_path):
    """Comfort resolves from the snapshot's program; freshness flips to stale
    once the snapshot is older than the threshold."""
    import time

    from beestat_bridge.sources.local import SNAPSHOT_STALE_SECONDS, LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="123456789012", homekit_entity="climate.test")],
    )
    store = Store(settings.db_path)
    source = LocalSource(settings, store)

    # No snapshot yet -> stale, comfort unknown.
    assert source.snapshot_freshness("123456789012")["stale"] is True
    assert source.current_comfort("123456789012") is None

    store.upsert_snapshot(
        "123456789012",
        {
            "identifier": "123456789012",
            "program": {
                "currentClimateRef": "sleep",
                "climates": [
                    {"climateRef": "home", "name": "Home"},
                    {"climateRef": "sleep", "name": "Sleep"},
                ],
            },
        },
    )
    assert source.snapshot_freshness("123456789012")["stale"] is False
    assert source.current_comfort("123456789012") == "Sleep"

    # Age the snapshot past the threshold -> stale.
    store._conn.execute(
        "UPDATE snapshots SET updated_at = ? WHERE identifier = ?",
        (int(time.time()) - SNAPSHOT_STALE_SECONDS - 60, "123456789012"),
    )
    store._conn.commit()
    assert source.snapshot_freshness("123456789012")["stale"] is True
    store.close()


def test_admin_thermostats_hides_stale_cloud_fields(client):
    """/admin/thermostats surfaces comfort + inUse when the snapshot is fresh,
    and hides both (with a stale flag) once it ages out."""
    import time

    context = client.app.state.context
    store = context.store
    ts = int(time.time())
    store.insert_sample("123456789012", ts, {"temperature": 71.0, "humidity": 44})
    store.upsert_sensor_meta("123456789012", "rs:abc", "Bedroom", "ecobee3_remote_sensor")
    store.insert_sensor_sample("123456789012", "rs:abc", ts, {"temperature": 70.0})
    store.upsert_snapshot(
        "123456789012",
        {
            "identifier": "123456789012",
            "name": "Upstairs",
            "program": {
                "currentClimateRef": "home",
                "climates": [{"climateRef": "home", "name": "Home"}],
            },
            "remoteSensors": [
                {"id": "rs:100", "name": "Bedroom", "type": "ecobee3_remote_sensor",
                 "inUse": False},
            ],
        },
    )

    fresh = client.get("/admin/thermostats").json()["thermostats"][0]
    assert fresh["cloud"]["stale"] is False
    assert fresh["current"]["comfort"] == "Home"
    assert fresh["sensors"][0]["in_use"] is False

    # Age the snapshot out -> cloud-only fields hidden.
    from beestat_bridge.sources.local import SNAPSHOT_STALE_SECONDS

    store._conn.execute(
        "UPDATE snapshots SET updated_at = ? WHERE identifier = ?",
        (ts - SNAPSHOT_STALE_SECONDS - 60, "123456789012"),
    )
    store._conn.commit()
    stale = client.get("/admin/thermostats").json()["thermostats"][0]
    assert stale["cloud"]["stale"] is True
    assert stale["current"]["comfort"] is None
    assert stale["sensors"][0]["in_use"] is None


def test_local_runtime_report_sensor_list(tmp_path):
    """runtimeReport sensorList: '<sensor_id>:<capability_id>' columns (temp id 1,
    occupancy id 3), CSV data rows with temperature in degrees and occupancy 1/0."""
    import datetime as dt

    from beestat_bridge.sources.local import LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="123456789012", homekit_entity="climate.test")],
    )
    store = Store(settings.db_path)
    store.upsert_sensor_meta("123456789012", "rs:abc", "Bedroom", "ecobee3_remote_sensor")
    date = "2026-07-20"
    base = int(dt.datetime.strptime(date, "%Y-%m-%d").timestamp())
    ts = base + 10 * 3600 + 130  # 10:02:10 local
    store.insert_sensor_sample("123456789012", "rs:abc", ts, {"temperature": 70.5, "occupancy": True})

    result = json.loads(
        LocalSource(settings, store).runtime_report(
            {
                "selection": {"selectionType": "registered"},
                "startDate": date, "startInterval": 0,
                "endDate": date, "endInterval": 287,
                "columns": "zoneAveTemp",
            }
        )
    )
    entry = result["sensorList"][0]
    assert entry["columns"][:2] == ["date", "time"]
    assert "rs:abc:1" in entry["columns"]  # temperature = capability id 1
    assert "rs:abc:3" in entry["columns"]  # occupancy = capability id 3
    assert entry["sensors"][0]["sensorId"] == "rs:abc"
    temp_col = entry["columns"].index("rs:abc:1")
    assert any(row.split(",")[temp_col] == "70.5" for row in entry["data"])
    store.close()


def test_local_runtime_report_uses_ecobee_response_column_names(tmp_path):
    """beestat requests 'hvacMode' but reads the response column back as
    'HVACmode' (ecobee capitalizes it). The local source must emit that name."""
    from beestat_bridge.sources.local import LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="123456789012", homekit_entity="climate.test")],
    )
    store = Store(settings.db_path)
    result = json.loads(
        LocalSource(settings, store).runtime_report(
            {
                "selection": {"selectionType": "registered"},
                "startDate": "2026-07-20", "startInterval": 0,
                "endDate": "2026-07-20", "endInterval": 287,
                "columns": "compCool1,hvacMode,zoneAveTemp",
            }
        )
    )
    columns = result["columns"].split(",")
    assert "HVACmode" in columns
    assert "hvacMode" not in columns
    store.close()


def test_local_serves_snapshot_only_thermostats(tmp_path):
    """A thermostat that isn't configured for local data but has a cloud snapshot
    stays visible in local mode (served verbatim as old data) so beestat keeps it
    selectable — the thermostat-swap control needs more than one thermostat."""
    from beestat_bridge.sources.local import LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="111111111111", homekit_entity="climate.upstairs")],
    )
    store = Store(settings.db_path)
    # Main floor is cloud-only (not in the local config) but was synced before.
    store.upsert_snapshot(
        "222222222222",
        {"identifier": "222222222222", "name": "Main Floor",
         "runtime": {"actualTemperature": 705, "actualHumidity": 40,
                     "desiredHeat": 680, "desiredCool": 740, "firstConnected": "2020-01-01 00:00:00"}},
    )
    source = LocalSource(settings, store)

    result = json.loads(source.thermostat({"selection": {"selectionType": "registered"}}))
    ids = [t["identifier"] for t in result["thermostatList"]]
    assert "111111111111" in ids  # configured (synthetic/live)
    assert "222222222222" in ids  # snapshot-only, still served

    # Selecting the snapshot-only one directly also works.
    picked = json.loads(source.thermostat(
        {"selection": {"selectionType": "thermostats", "selectionMatch": "222222222222"}}
    ))
    assert picked["thermostatList"][0]["name"] == "Main Floor"

    # Its runtimeReport is a benign processing error (beestat keeps old history).
    report = json.loads(source.runtime_report(
        {"selection": {"selectionType": "thermostats", "selectionMatch": "222222222222"},
         "startDate": "2026-07-20", "endDate": "2026-07-20",
         "startInterval": 0, "endInterval": 287, "columns": "zoneAveTemp"}
    ))
    assert report["status"]["code"] == 3
    assert "reportList" not in report
    store.close()


def test_local_thermostat_always_has_runtime_keys_beestat_reads(tmp_path):
    """beestat dereferences runtime.firstConnected et al. with no guard; the
    synthetic (no snapshot, no sample) path must still provide them."""
    from beestat_bridge.sources.local import LocalSource
    from beestat_bridge.store import Store

    settings = Settings(
        mode="local",
        data_dir=tmp_path,
        thermostats=[Thermostat(serial="123456789012", homekit_entity="climate.test")],
    )
    store = Store(settings.db_path)
    result = json.loads(
        LocalSource(settings, store).thermostat({"selection": {"selectionType": "registered"}})
    )
    runtime = result["thermostatList"][0]["runtime"]
    for key in ("actualTemperature", "actualHumidity", "desiredHeat", "desiredCool", "firstConnected"):
        assert key in runtime
    store.close()


def test_local_thermostat_grab_refreshes_cloud_snapshot(client):
    """Grabbing /1/thermostat in local mode refreshes the cloud snapshot first
    (so comfort/inUse are current), collapses a burst via the debounce, and does
    not fire for runtimeReport grabs."""
    context = client.app.state.context
    calls = []

    async def fake_thermostat(body):
        calls.append(body)
        return json.dumps({"thermostatList": [], "status": {"code": 0}})

    context.cloud.thermostat = fake_thermostat
    context.snapshot_refresh_at = 0.0

    tokens = _get_tokens(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    t_body = json.dumps({"selection": {"selectionType": "registered"}})

    client.get("/1/thermostat", params={"body": t_body}, headers=headers)
    assert len(calls) == 1  # the grab triggered a cloud refresh

    # A second grab inside the debounce window reuses it — no extra cloud call.
    client.get("/1/thermostat", params={"body": t_body}, headers=headers)
    assert len(calls) == 1

    # A runtimeReport grab never triggers the refresh (comfort/inUse aren't there).
    r_body = json.dumps({
        "selection": {"selectionType": "registered"},
        "startDate": "2026-07-20", "endDate": "2026-07-20",
        "startInterval": 0, "endInterval": 11, "columns": "zoneAveTemp",
    })
    client.get("/1/runtimeReport", params={"body": r_body}, headers=headers)
    assert len(calls) == 1


def test_admin_mode_override(client):
    assert client.get("/admin/status").json()["effective_mode"] == "local"
    response = client.post("/admin/mode", json={"mode": "cloud"})
    assert response.json()["effective_mode"] == "cloud"
    response = client.post("/admin/mode", json={"mode": None})
    assert response.json()["effective_mode"] == "local"


def test_setup_page_inline_script_parses(client):
    """The whole page hangs at "..." if the inline <script> has a syntax error
    (e.g. a Python-escaped apostrophe landing inside a single-quoted JS string).
    Syntax-check the emitted script with node so that regresses loudly."""
    import re
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to syntax-check inline JS")

    page = client.get("/").text
    match = re.search(r"<script>(.*)</script>", page, re.S)
    assert match, "setup page has no inline <script>"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=True) as handle:
        handle.write(match.group(1))
        handle.flush()
        result = subprocess.run(
            [node, "--check", handle.name], capture_output=True, text=True
        )
    assert result.returncode == 0, f"inline JS syntax error:\n{result.stderr}"


def test_setup_page_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Beestat Bridge" in response.text
    # Ingress compatibility: the page must not reference absolute paths.
    assert "fetch('/" not in response.text


def test_login_endpoint_validates_input(client):
    response = client.post("/admin/ecobee/login", json={"email": "a@b.c"})
    assert response.json() == {"error": "email and password required"}
    response = client.post("/admin/ecobee/mfa", json={"code": "123456"})
    assert response.json() == {"error": "no login in progress; start over"}


def test_config_roundtrip_applies_live(client):
    new_config = {
        "thermostats": [
            {
                "serial": "999888777666",
                "homekit_entity": "climate.new_stat",
                "system_type": "furnace",
                "hvac_action_mapping": True,
                "equipment_sources": {"fan": "binary_sensor.hvac_g"},
            }
        ],
        "outdoor_temperature": "sensor.outdoor",
        "poll_interval": 30,
        "mode_entity": None,
        "auto_failover": True,
    }
    response = client.post("/admin/config", json=new_config)
    assert response.json()["saved"] is True

    # Applied immediately: status and the local source see the new thermostat.
    assert client.get("/admin/status").json()["thermostats"] == ["999888777666"]
    tokens = client.post("/token", data={"grant_type": "refresh_token"}).json()
    body = client.get(
        "/1/thermostat",
        params={"body": json.dumps({"selection": {"selectionType": "registered"}})},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    ).json()
    assert body["thermostatList"][0]["identifier"] == "999888777666"

    # Round-trips through GET, including the wire-sensor mapping.
    config = client.get("/admin/config").json()["config"]
    assert config["thermostats"][0]["equipment_sources"]["fan"] == "binary_sensor.hvac_g"
    assert config["auto_failover"] is True


def test_config_validation_rejects_bad_input(client):
    bad = {"thermostats": [{"serial": "1", "homekit_entity": "sensor.nope"}]}
    assert "climate.*" in client.post("/admin/config", json=bad).json()["error"]
    dupes = {
        "thermostats": [
            {"serial": "1", "homekit_entity": "climate.a"},
            {"serial": "1", "homekit_entity": "climate.b"},
        ]
    }
    assert "duplicate" in client.post("/admin/config", json=dupes).json()["error"]
    # A rejected save must not clobber the running config.
    assert client.get("/admin/status").json()["thermostats"] == ["123456789012"]


def test_ha_entities_endpoint_degrades_without_ha(client):
    assert client.get("/admin/ha/entities").json() == {
        "climate": [], "binary_sensor": [], "outdoor": []
    }


def test_ingress_guard_blocks_admin_without_header(tmp_path, monkeypatch):
    # As an HA app (SUPERVISOR_TOKEN present), the UI/admin require the
    # X-Ingress-Path header; facade endpoints stay open.
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    settings = Settings(mode="local", data_dir=tmp_path)
    with TestClient(create_app(settings)) as guarded:
        assert guarded.get("/").status_code == 403
        assert guarded.get("/admin/status").status_code == 403
        # With the Ingress header, the UI is reachable.
        assert guarded.get("/", headers={"X-Ingress-Path": "/x"}).status_code == 200
        assert guarded.get("/admin/status", headers={"X-Ingress-Path": "/x"}).status_code == 200
        # Facade token endpoint is never guarded (beestat calls it directly).
        assert guarded.post("/token", data={"grant_type": "refresh_token"}).status_code == 200
