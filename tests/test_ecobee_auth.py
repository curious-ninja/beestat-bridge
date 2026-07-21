"""The Auth0 universal-login flow against a mock tenant that reproduces the
redirect chain documented in ecobee_auth.py (per ha-ecobee)."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from beestat_bridge import ecobee_auth
from beestat_bridge.ecobee_auth import (
    REDIRECT_URI,
    EcobeeAuthenticator,
    EcobeeAuthError,
    EcobeeMfaRequired,
)

CLIENT_ID = "test-client"


class MockTenant:
    """Simulates auth.ecobee.com. Set mfa=True to inject an OTP prompt."""

    def __init__(self, mfa: bool = False, password: str = "hunter2") -> None:
        self.mfa = mfa
        self.password = password
        self.challenge: str | None = None
        self.verifier_checked = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = parse_qs(request.url.query.decode())
        form = parse_qs(request.content.decode()) if request.content else {}

        if path == "/authorize":
            assert query["code_challenge_method"] == ["S256"]
            assert query["redirect_uri"] == [REDIRECT_URI]
            self.challenge = query["code_challenge"][0]
            return httpx.Response(
                302, headers={"Location": "/u/login/identifier?state=s1"}
            )
        if path == "/u/login/identifier":
            assert form["username"]
            return httpx.Response(302, headers={"Location": "/u/login/password?state=s2"})
        if path == "/u/login/password":
            if form["password"] != [self.password]:
                return httpx.Response(200, text="Wrong email or password")
            return httpx.Response(302, headers={"Location": "/authorize/resume?state=s3"})
        if path == "/authorize/resume":
            if self.mfa:
                self.mfa = False  # One challenge, then proceed.
                return httpx.Response(302, headers={"Location": "/u/mfa-otp?state=s4"})
            return httpx.Response(
                302, headers={"Location": f"{REDIRECT_URI}?code=authcode&state=s3"}
            )
        if path == "/u/mfa-otp":
            if form.get("code") != ["123456"]:
                return httpx.Response(200, text="code expired")
            return httpx.Response(302, headers={"Location": "/authorize/resume?state=s5"})
        if path == "/oauth/token":
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["authcode"]
                # PKCE: the verifier must hash to the challenge sent earlier.
                digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
                expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                assert expected == self.challenge
                self.verifier_checked = True
                return httpx.Response(
                    200,
                    json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
                )
            if form["grant_type"] == ["refresh_token"]:
                if form["refresh_token"] == ["rt"]:
                    return httpx.Response(
                        200, json={"access_token": "at2", "refresh_token": "rt2"}
                    )
                return httpx.Response(400, json={"error": "invalid_grant"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


@pytest.mark.anyio
async def test_login_without_mfa():
    tenant = MockTenant()
    auth = EcobeeAuthenticator(CLIENT_ID, transport=tenant.transport())
    tokens = await auth.start("a@b.c", "hunter2")
    assert tokens["refresh_token"] == "rt"
    assert tenant.verifier_checked
    await auth.close()


@pytest.mark.anyio
async def test_login_with_mfa():
    tenant = MockTenant(mfa=True)
    auth = EcobeeAuthenticator(CLIENT_ID, transport=tenant.transport())
    with pytest.raises(EcobeeMfaRequired) as challenge:
        await auth.start("a@b.c", "hunter2")
    assert challenge.value.challenge_type == "otp"
    tokens = await auth.submit_mfa("123456")
    assert tokens["access_token"] == "at"
    await auth.close()


@pytest.mark.anyio
async def test_wrong_password_is_a_clear_error():
    tenant = MockTenant(password="other")
    auth = EcobeeAuthenticator(CLIENT_ID, transport=tenant.transport())
    with pytest.raises(EcobeeAuthError, match="password"):
        await auth.start("a@b.c", "hunter2")
    await auth.close()


@pytest.mark.anyio
async def test_wrong_mfa_code_is_a_clear_error():
    tenant = MockTenant(mfa=True)
    auth = EcobeeAuthenticator(CLIENT_ID, transport=tenant.transport())
    with pytest.raises(EcobeeMfaRequired):
        await auth.start("a@b.c", "hunter2")
    with pytest.raises(EcobeeAuthError, match="rejected"):
        await auth.submit_mfa("000000")
    await auth.close()


@pytest.mark.anyio
async def test_refresh_and_invalid_grant():
    tenant = MockTenant()
    body = await ecobee_auth.refresh_tokens(CLIENT_ID, "rt", transport=tenant.transport())
    assert body["access_token"] == "at2"
    with pytest.raises(EcobeeAuthError, match="invalid_grant"):
        await ecobee_auth.refresh_tokens(CLIENT_ID, "revoked", transport=tenant.transport())


@pytest.fixture
def anyio_backend():
    return "asyncio"
