"""Password-gate auth using a signed session cookie (itsdangerous).

- Single owner. No registration.
- Password from ``SUPERBOT_PASSWORD`` env var.
- Session cookie signed with ``SUPERBOT_SESSION_SECRET``.
- All /api routes except /api/auth/login and /api/health require auth.
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Callable

from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

SESSION_COOKIE = "superbot_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours

_secret = os.environ.get("SUPERBOT_SESSION_SECRET") or secrets.token_hex(32)
_signer = TimestampSigner(_secret)


def check_password(password: str) -> bool:
    expected = os.environ.get("SUPERBOT_PASSWORD", "")
    if not expected:
        # Never allow login if no password is configured.
        return False
    # Constant-time comparison
    if len(password) != len(expected):
        return False
    result = 0
    for a, b in zip(password, expected):
        result |= ord(a) ^ ord(b)
    return result == 0


def issue_session_cookie(response: Response) -> None:
    token = _signer.sign(f"owner:{int(time.time())}").decode("utf-8")
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Preview uses HTTPS via ingress; cookie still works over lax
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


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
