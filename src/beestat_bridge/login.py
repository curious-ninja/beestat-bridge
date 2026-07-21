"""Consumer-login strategy: pyecobee first, in-tree flow as fallback.

pyecobee (python-ecobee-api) is the library Home Assistant core's ecobee
integration uses for keyless auth. Depending on it means that when ecobee
changes the login flow, the fix ships upstream (their entire user base
breaks at once) and we adopt it by bumping the pinned version — no code
copying. It is synchronous, so calls run in a worker thread.

The in-tree implementation (ecobee_auth.py, same wire protocol) remains as
an automatic fallback: if pyecobee is missing, crashes, or has not caught up
with an upstream change yet, the bridge silently tries the built-in flow
before reporting failure. Wrong credentials do NOT trigger the fallback —
both implementations drive the same Auth0 tenant, so a rejected password is
final.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import ecobee_auth
from .ecobee_auth import EcobeeAuthError, EcobeeMfaRequired

logger = logging.getLogger(__name__)


class LoginSession:
    """One interactive login attempt, MFA pause included."""

    def __init__(self, client_id: str, transport: Any | None = None) -> None:
        self._client_id = client_id
        self._transport = transport  # Test hook for the in-tree fallback.
        self._pyecobee: Any | None = None
        self._pyecobee_challenge: Any | None = None
        self._fallback: ecobee_auth.EcobeeAuthenticator | None = None

    async def close(self) -> None:
        if self._fallback is not None:
            await self._fallback.close()

    @staticmethod
    def _tokens_of(ecobee: Any) -> dict[str, str]:
        return {
            "access_token": ecobee.access_token,
            "refresh_token": ecobee.refresh_token,
        }

    async def start(self, email: str, password: str) -> dict[str, str]:
        try:
            import pyecobee

            ecobee = pyecobee.Ecobee(
                config={
                    pyecobee.ECOBEE_USERNAME: email,
                    pyecobee.ECOBEE_PASSWORD: password,
                }
            )
            if await asyncio.to_thread(ecobee.request_tokens_web):
                self._pyecobee = ecobee
                return self._tokens_of(ecobee)
            raise EcobeeAuthError("pyecobee login returned no tokens")
        except EcobeeAuthError:
            raise
        except Exception as error:
            outcome = self._classify_pyecobee_error(error)
            if outcome == "mfa":
                self._pyecobee = ecobee
                self._pyecobee_challenge = error.args[0]
                challenge_type = getattr(self._pyecobee_challenge, "mfa_type", None) or "otp"
                raise EcobeeMfaRequired(challenge_type) from error
            if outcome == "rejected":
                # Same Auth0 tenant either way; don't retry bad credentials.
                raise EcobeeAuthError("ecobee rejected the email or password") from error
            logger.warning(
                "pyecobee login failed (%s: %s); trying the built-in flow",
                type(error).__name__,
                error,
            )

        self._fallback = ecobee_auth.EcobeeAuthenticator(
            self._client_id, transport=self._transport
        )
        return await self._fallback.start(email, password)

    async def submit_mfa(self, code: str) -> dict[str, str]:
        if self._pyecobee is not None and self._pyecobee_challenge is not None:
            challenge, self._pyecobee_challenge = self._pyecobee_challenge, None
            try:
                ok = await asyncio.to_thread(
                    self._pyecobee.submit_mfa_code, challenge, code
                )
            except Exception as error:
                if self._classify_pyecobee_error(error) == "rejected":
                    raise EcobeeAuthError("MFA code rejected (invalid or expired)") from error
                raise EcobeeAuthError(f"MFA submission failed: {error}") from error
            if not ok:
                raise EcobeeAuthError("MFA code rejected (invalid or expired)")
            return self._tokens_of(self._pyecobee)
        if self._fallback is not None:
            return await self._fallback.submit_mfa(code)
        raise EcobeeAuthError("no MFA prompt pending")

    @staticmethod
    def _classify_pyecobee_error(error: Exception) -> str:
        """By name, not import: keeps the classification working even if
        pyecobee itself failed to import."""
        name = type(error).__name__
        if name == "EcobeeAuthMfaRequiredError" and error.args:
            return "mfa"
        if name == "EcobeeAuthFailedError":
            return "rejected"
        return "other"


async def refresh(
    client_id: str, refresh_token: str, transport: Any | None = None
) -> dict[str, str]:
    """Refresh with the same strategy order. Raises EcobeeAuthError only when
    the refresh token is conclusively dead on the in-tree path too."""
    try:
        import pyecobee

        ecobee = pyecobee.Ecobee(
            config={
                pyecobee.ECOBEE_REFRESH_TOKEN: refresh_token,
                pyecobee.ECOBEE_AUTH0_TOKEN: True,
            }
        )
        if await asyncio.to_thread(ecobee.refresh_tokens):
            return {
                "access_token": ecobee.access_token,
                "refresh_token": ecobee.refresh_token or refresh_token,
            }
        logger.warning("pyecobee refresh returned false; trying the built-in flow")
    except Exception as error:
        logger.warning(
            "pyecobee refresh failed (%s: %s); trying the built-in flow",
            type(error).__name__,
            error,
        )

    return await ecobee_auth.refresh_tokens(client_id, refresh_token, transport=transport)
