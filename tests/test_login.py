"""LoginSession strategy: pyecobee primary, in-tree fallback."""

import sys
import types

import pytest

from beestat_bridge import login
from beestat_bridge.ecobee_auth import EcobeeAuthError, EcobeeMfaRequired
from test_ecobee_auth import MockTenant


def fake_pyecobee(monkeypatch, ecobee_class):
    module = types.ModuleType("pyecobee")
    module.ECOBEE_USERNAME = "username"
    module.ECOBEE_PASSWORD = "password"
    module.ECOBEE_REFRESH_TOKEN = "refresh_token"
    module.ECOBEE_AUTH0_TOKEN = "auth0_token"
    module.Ecobee = ecobee_class

    class EcobeeAuthMfaRequiredError(Exception):
        pass

    class EcobeeAuthFailedError(Exception):
        pass

    class EcobeeAuthUnknownError(Exception):
        pass

    module.EcobeeAuthMfaRequiredError = EcobeeAuthMfaRequiredError
    module.EcobeeAuthFailedError = EcobeeAuthFailedError
    module.EcobeeAuthUnknownError = EcobeeAuthUnknownError
    monkeypatch.setitem(sys.modules, "pyecobee", module)
    return module


@pytest.mark.anyio
async def test_pyecobee_success_is_primary(monkeypatch):
    class Ecobee:
        def __init__(self, config):
            assert config == {"username": "a@b.c", "password": "pw"}
            self.access_token, self.refresh_token = "py-at", "py-rt"

        def request_tokens_web(self):
            return True

    fake_pyecobee(monkeypatch, Ecobee)
    session = login.LoginSession("client")
    tokens = await session.start("a@b.c", "pw")
    assert tokens == {"access_token": "py-at", "refresh_token": "py-rt"}
    await session.close()


@pytest.mark.anyio
async def test_pyecobee_mfa_roundtrip(monkeypatch):
    class Challenge:
        mfa_type = "sms"

    module_holder = {}

    class Ecobee:
        def __init__(self, config):
            self.access_token = self.refresh_token = None

        def request_tokens_web(self):
            raise module_holder["m"].EcobeeAuthMfaRequiredError(Challenge())

        def submit_mfa_code(self, challenge, code):
            assert isinstance(challenge, Challenge) and code == "42"
            self.access_token, self.refresh_token = "py-at", "py-rt"
            return True

    module_holder["m"] = fake_pyecobee(monkeypatch, Ecobee)
    session = login.LoginSession("client")
    with pytest.raises(EcobeeMfaRequired) as challenge:
        await session.start("a@b.c", "pw")
    assert challenge.value.challenge_type == "sms"
    tokens = await session.submit_mfa("42")
    assert tokens["refresh_token"] == "py-rt"
    await session.close()


@pytest.mark.anyio
async def test_rejected_credentials_do_not_fall_back(monkeypatch):
    module_holder = {}

    class Ecobee:
        def __init__(self, config):
            pass

        def request_tokens_web(self):
            raise module_holder["m"].EcobeeAuthFailedError("bad password")

    module_holder["m"] = fake_pyecobee(monkeypatch, Ecobee)
    session = login.LoginSession("client")  # No transport: fallback would crash.
    with pytest.raises(EcobeeAuthError, match="rejected"):
        await session.start("a@b.c", "pw")
    assert session._fallback is None
    await session.close()


@pytest.mark.anyio
async def test_pyecobee_breakage_falls_back_to_in_tree(monkeypatch):
    module_holder = {}

    class Ecobee:
        def __init__(self, config):
            pass

        def request_tokens_web(self):
            raise module_holder["m"].EcobeeAuthUnknownError("flow changed upstream")

    module_holder["m"] = fake_pyecobee(monkeypatch, Ecobee)
    tenant = MockTenant()
    session = login.LoginSession("client", transport=tenant.transport())
    tokens = await session.start("a@b.c", "hunter2")
    assert tokens["refresh_token"] == "rt"  # Served by the in-tree flow.
    assert tenant.verifier_checked
    await session.close()


@pytest.mark.anyio
async def test_refresh_falls_back_when_pyecobee_returns_false(monkeypatch):
    class Ecobee:
        def __init__(self, config):
            assert config["refresh_token"] == "rt"
            self.access_token = self.refresh_token = None

        def refresh_tokens(self):
            return False

    fake_pyecobee(monkeypatch, Ecobee)
    tenant = MockTenant()
    body = await login.refresh("client", "rt", transport=tenant.transport())
    assert body["access_token"] == "at2"  # In-tree refresh answered.


@pytest.fixture
def anyio_backend():
    return "asyncio"
