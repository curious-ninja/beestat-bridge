"""ecobee consumer login: Auth0 universal-login + PKCE, no developer key.

Flow ported from the Apache-2.0 licensed ha-ecobee integration
(https://github.com/pjordanandrsn/ha-ecobee) — see NOTICE. This is the same
route ecobee's own web app uses, driven server-side:

  GET  /authorize                 (PKCE challenge)         -> 302 identifier
  POST /u/login/identifier        (email)                  -> 302 password
  POST /u/login/password          (password)               -> 302 resume
  GET  /authorize/resume          (loop over Auth0 prompts; MFA pauses here)
  ...                             -> 302 to the registered callback with ?code=
  POST /oauth/token               (code + verifier)        -> tokens

The callback redirect is never followed; the code is lifted from the
Location header.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

AUTH_DOMAIN = "https://auth.ecobee.com"
REDIRECT_URI = "https://www.ecobee.com/home/authCallback"
SCOPE = "openid smartRead smartWrite piiRead piiWrite offline_access"
AUDIENCE = "https://prod.ecobee.com/api/v1"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
MFA_PATH = re.compile(r"/u/mfa-(otp|sms|recovery-code)")
MAX_PROMPT_HOPS = 8


class EcobeeAuthError(RuntimeError):
    pass


class EcobeeMfaRequired(Exception):
    """Login paused on an MFA prompt; call submit_mfa() with the code."""

    def __init__(self, challenge_type: str) -> None:
        super().__init__(f"MFA required ({challenge_type})")
        self.challenge_type = challenge_type


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _state_of(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("state", [])
    if not values:
        raise EcobeeAuthError(f"no state parameter in {url}")
    return values[0]


class EcobeeAuthenticator:
    """One login attempt. Holds Auth0 session cookies and, when MFA fires,
    the pending prompt so the code can be submitted afterwards."""

    def __init__(self, client_id: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client_id = client_id
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=30.0,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        )
        self._verifier = _b64url(secrets.token_bytes(32))
        self._pending_prompt: dict[str, str] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    def _absolute(self, location: str) -> str:
        return urljoin(AUTH_DOMAIN, location)

    async def _expect_redirect(self, response: httpx.Response, step: str) -> str:
        if response.status_code not in (302, 303):
            raise EcobeeAuthError(
                f"{step}: expected redirect, got HTTP {response.status_code} "
                f"(wrong credentials, expired code, or a flow change upstream)"
            )
        return response.headers["Location"]

    async def start(self, email: str, password: str) -> dict[str, str]:
        """Run the login. Returns tokens, or raises EcobeeMfaRequired with the
        session parked on the MFA prompt."""
        challenge = _b64url(hashlib.sha256(self._verifier.encode()).digest())
        response = await self._client.get(
            f"{AUTH_DOMAIN}/authorize",
            params={
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "audience": AUDIENCE,
                "state": _b64url(secrets.token_bytes(32)),
                "client_id": self._client_id,
                "prompt": "login",
            },
        )
        location = await self._expect_redirect(response, "authorize")
        state = _state_of(location)

        response = await self._client.post(
            f"{AUTH_DOMAIN}/u/login/identifier",
            params={"state": state},
            data={
                "state": state,
                "username": email,
                "js-available": "true",
                "webauthn-available": "true",
                "is-brave": "false",
                "webauthn-platform-available": "true",
                "action": "default",
            },
        )
        location = await self._expect_redirect(response, "identifier")
        state = _state_of(location)

        response = await self._client.post(
            f"{AUTH_DOMAIN}/u/login/password",
            params={"state": state},
            data={
                "state": state,
                "username": email,
                "password": password,
                "action": "default",
            },
        )
        location = await self._expect_redirect(response, "password")
        return await self._resume(location)

    async def _resume(self, location: str) -> dict[str, str]:
        """Follow the Auth0 prompt chain until the callback code appears."""
        for _ in range(MAX_PROMPT_HOPS):
            if location.startswith(REDIRECT_URI):
                code = parse_qs(urlparse(location).query).get("code", [None])[0]
                if code is None:
                    raise EcobeeAuthError(f"callback reached without a code: {location}")
                return await self._exchange(code)

            url = self._absolute(location)
            path = urlparse(url).path
            state = _state_of(url)

            mfa = MFA_PATH.search(path)
            if mfa is not None:
                self._pending_prompt = {"url": url, "state": state}
                raise EcobeeMfaRequired(mfa.group(1))

            if path.startswith("/u/"):
                # Consent / terms / other interstitial: acknowledge and go on.
                response = await self._client.post(
                    url, data={"state": state, "action": "default"}
                )
            else:
                response = await self._client.get(url)
            location = await self._expect_redirect(response, path)

        raise EcobeeAuthError("login did not converge (too many Auth0 prompts)")

    async def submit_mfa(self, code: str) -> dict[str, str]:
        if self._pending_prompt is None:
            raise EcobeeAuthError("no MFA prompt pending")
        prompt = self._pending_prompt
        self._pending_prompt = None
        response = await self._client.post(
            prompt["url"], data={"state": prompt["state"], "code": code}
        )
        if response.status_code == 200:
            raise EcobeeAuthError("MFA code rejected (invalid or expired)")
        location = await self._expect_redirect(response, "mfa")
        return await self._resume(location)

    async def _exchange(self, code: str) -> dict[str, str]:
        response = await self._client.post(
            f"{AUTH_DOMAIN}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "code": code,
                "code_verifier": self._verifier,
                "redirect_uri": REDIRECT_URI,
            },
        )
        if response.status_code != 200:
            raise EcobeeAuthError(f"token exchange failed: HTTP {response.status_code}")
        body = response.json()
        if "access_token" not in body or "refresh_token" not in body:
            raise EcobeeAuthError("token exchange returned no tokens")
        return body


async def refresh_tokens(
    client_id: str,
    refresh_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """Refresh consumer tokens. Raises EcobeeAuthError on conclusive
    invalid_grant (revoked/expired refresh token)."""
    async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
        response = await client.post(
            f"{AUTH_DOMAIN}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
        )
    if response.status_code == 200:
        return response.json()
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    raise EcobeeAuthError(
        f"refresh failed: HTTP {response.status_code}"
        + (f" ({error})" if error else "")
    )
