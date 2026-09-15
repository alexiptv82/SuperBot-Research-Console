"""Authentication regression tests.

Covers:
- successful login
- authenticated request
- session persistence across multiple requests (simulates refresh)
- navigation while authenticated (multiple protected endpoints)
- invalid password
- explicit logout
- unauthenticated access rejection
- cookie flag policy (SameSite / Secure) matches request scheme
"""
from __future__ import annotations

import os

# All env + init handled by conftest.py.

from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402

client = TestClient(app)


def _pw() -> str:
    return os.environ["SUPERBOT_PASSWORD"]


def test_health_no_auth():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_policy_no_auth():
    r = client.get("/api/policy")
    assert r.status_code == 200
    assert r.json()["collector_sha256"].startswith("e924edc1")


def test_login_invalid_password():
    r = client.post("/api/auth/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert "session" not in r.cookies


def test_unauthenticated_protected_rejected():
    fresh = TestClient(app)
    for path in ["/api/overview", "/api/sessions", "/api/checkpoints", "/api/audit"]:
        r = fresh.get(path)
        assert r.status_code == 401, path


def test_login_success_and_cookie_flags():
    fresh = TestClient(app)
    r = fresh.post("/api/auth/login", json={"password": _pw()})
    assert r.status_code == 200
    assert r.json() == {"authenticated": True}
    # Cookie should be set with HttpOnly + SameSite (case may vary)
    raw = r.headers.get("set-cookie", "")
    assert "superbot_session=" in raw
    assert "HttpOnly" in raw
    assert "Path=/" in raw
    # Localhost / http => SameSite=lax and no Secure by default
    assert "samesite=lax" in raw.lower()
    assert "Secure" not in raw


def test_cookie_upgrades_to_secure_none_on_https():
    fresh = TestClient(app)
    r = fresh.post(
        "/api/auth/login",
        json={"password": _pw()},
        headers={"x-forwarded-proto": "https"},
    )
    assert r.status_code == 200
    raw = r.headers.get("set-cookie", "")
    assert "samesite=none" in raw.lower()
    assert "Secure" in raw


def test_authenticated_request_and_session_persistence_across_requests():
    """Simulates a browser refresh: same cookie jar, multiple requests."""
    fresh = TestClient(app)
    r = fresh.post("/api/auth/login", json={"password": _pw()})
    assert r.status_code == 200
    # Multiple navigations
    for _ in range(3):
        assert fresh.get("/api/auth/me").json() == {"authenticated": True, "actor": "owner"}
        assert fresh.get("/api/overview").status_code == 200
        assert fresh.get("/api/sessions").status_code == 200
        assert fresh.get("/api/checkpoints").status_code == 200
        assert fresh.get("/api/audit").status_code == 200


def test_auth_me_when_not_logged_in():
    fresh = TestClient(app)
    r = fresh.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


def test_explicit_logout_clears_session():
    fresh = TestClient(app)
    fresh.post("/api/auth/login", json={"password": _pw()})
    assert fresh.get("/api/overview").status_code == 200
    r = fresh.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False}
    # After logout, protected endpoints must reject
    assert fresh.get("/api/overview").status_code == 401
    assert fresh.get("/api/auth/me").json()["authenticated"] is False


def test_bad_cookie_rejected():
    fresh = TestClient(app)
    fresh.cookies.set("superbot_session", "not-a-valid-signed-token")
    assert fresh.get("/api/overview").status_code == 401
