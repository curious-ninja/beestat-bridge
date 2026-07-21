"""Bridge-minted tokens.

Beestat never sees real ecobee tokens; it holds tokens minted here, so its
notion of the connected account survives mode switches and the permanent loss
of ecobee cloud auth.

Shape constraints come from beestat's api/ecobee_token.php: a three-part JWT
whose payload carries `sub` of the form "<anything>|<36-char account id>".
Beestat never verifies the signature, but we sign (HS256, per-install secret)
anyway so the facade can reject garbage.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

ACCESS_TOKEN_LIFETIME = 3600


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_access_token(secret: str, account_id: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "sub": f"beestat-bridge|{account_id}",
                "iat": now,
                "exp": now + ACCESS_TOKEN_LIFETIME,
                "iss": "beestat-bridge",
            }
        ).encode()
    )
    signature = _b64url(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


def mint_refresh_token() -> str:
    return "bridge-refresh-" + secrets.token_urlsafe(32)


def verify_access_token(secret: str, token: str) -> bool:
    try:
        header, payload, signature = token.split(".")
    except ValueError:
        return False
    expected = _b64url(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        claims = json.loads(_b64url_decode(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    return claims.get("exp", 0) > time.time()
