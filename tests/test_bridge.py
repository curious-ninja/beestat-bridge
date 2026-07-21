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


def test_admin_mode_override(client):
    assert client.get("/admin/status").json()["effective_mode"] == "local"
    response = client.post("/admin/mode", json={"mode": "cloud"})
    assert response.json()["effective_mode"] == "cloud"
    response = client.post("/admin/mode", json={"mode": None})
    assert response.json()["effective_mode"] == "local"


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
