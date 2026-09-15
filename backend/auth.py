"""Password-gate auth using a signed session cookie (itsdangerous).

- Single owner. No registration.
- Password from ``SUPERBOT_PASSWORD`` env var.
- Session cookie signed with ``SUPERBOT_SESSION_SECRET`` (stable across
  restarts as long as the env var is set).
- All /api routes except /api/auth/*, /api/health and /api/policy require auth.

Cookie policy:
- On HTTPS requests (Preview / Railway behind TLS terminator) we set
  ``SameSite=None; Secure`` so the cookie survives iframe / cross-site
  contexts (Emergent editor overlay, custom domains fronted by CF).
- On plain HTTP requests (local dev) we set ``SameSite=Lax`` without
  Secure, so it still works during ``supervisorctl start`` on localhost.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Callable

from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

logger = logging.getLogger("superbot.auth")

SESSION_COOKIE = "superbot_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours

_secret = os.environ.get("SUPERBOT_SESSION_SECRET")
if not _secret:
    _secret = secrets.token_hex(32)
    logger.warning(
        "SUPERBOT_SESSION_SECRET not set; using a random secret. "
        "This will invalidate all sessions on restart. Set the env var to "
        "keep sessions stable across restarts / reloads."
    )
_signer = TimestampSigner(_secret)


def check_password(password: str) -> bool:
    expected = os.environ.get("SUPERBOT_PASSWORD", "")
    if not expected:
        return False
    if len(password) != len(expected):
        return False
    result = 0
    for a, b in zip(password, expected):
        result |= ord(a) ^ ord(b)
    return result == 0


def _cookie_params(request: Request | None) -> dict:
    """Compute cookie params based on request scheme + forwarded headers.

    We prefer ``SameSite=None; Secure`` on HTTPS so the cookie works when
    the console is loaded inside an iframe (Emergent editor overlay, etc.).
    """
    # Detect HTTPS via direct scheme or proxy header
    is_https = False
    if request is not None:
        if request.url.scheme == "https":
            is_https = True
        else:
            xf = request.headers.get("x-forwarded-proto", "")
            if "https" in xf.lower():
                is_https = True
    # Allow forcing via env for exotic deployments
    force = os.environ.get("SUPERBOT_COOKIE_SECURE", "").lower()
    if force in ("1", "true", "yes"):
        is_https = True
    elif force in ("0", "false", "no"):
        is_https = False

    if is_https:
        return {"samesite": "none", "secure": True}
    return {"samesite": "lax", "secure": False}


def issue_session_cookie(response: Response, request: Request | None = None) -> None:
    token = _signer.sign(f"owner:{int(time.time())}").decode("utf-8")
    params = _cookie_params(request)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        path="/",
        **params,
    )


def clear_session_cookie(response: Response, request: Request | None = None) -> None:
    params = _cookie_params(request)
    # Overwrite with empty + past expiry using the same params so the
    # browser accepts the deletion regardless of SameSite policy.
    response.set_cookie(
        key=SESSION_COOKIE,
        value="",
        max_age=0,
        httponly=True,
        path="/",
        **params,
    )


def verify_session(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        raw = _signer.unsign(token, max_age=SESSION_MAX_AGE).decode("utf-8")
    except SignatureExpired:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    except BadSignature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad session")
    return raw.split(":")[0]


def require_owner() -> Callable:
    def dep(request: Request) -> str:
        return verify_session(request)
    return dep
