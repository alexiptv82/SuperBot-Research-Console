"""OLD36 bundle-import tests.

Synthetic fixtures only \u2014 no real 2.3 GB bundle is used. Every
scenario builds a small nested-ZIP structure with the same
naming/pattern the real bundle uses so we exercise the full
validation + dispatch code path deterministically.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from bundle_import import (
    BundleImportError,
    import_bundle,
    validate_bundle,
)
from checkpoint_registry import (
    CHECKPOINT_OLD36_REFERENCE,
    NEW36_SESSION_IDS,
    OLD36_REFERENCE_NOMINAL_HOURS,
    OLD36_REFERENCE_SESSIONS,
)
from constants import (
    CHECKPOINT_NEW12,
    CHECKPOINT_NEW36,
    CHECKPOINT_OLD36,
    CHECKPOINT_TOTAL48,
    CHECKPOINT_TOTAL72,
    FROZEN_COLLECTOR_SHA256,
)
from database import engine
from frozen_engine import current_status
from recovery.allowlist import NEW36QuantitativeFirewallError, assert_recovery_allowed

sys.path.insert(0, "/app/backend/tests")
from fixtures import VALID_PARQUET  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic session-ZIP builder
# ---------------------------------------------------------------------------


def _inner_zip_bytes(session_id: str, duration_hours: float, salt: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "session_id": session_id,
            "exit_code": 0,
            "watchdog": False,
            "writer_errors": 0,
            "reconnect_summary": [],
            "collector_sha256": FROZEN_COLLECTOR_SHA256,
            "start_time": "2026-09-05T07:38:18Z",
            "end_time": "2026-09-05T13:38:18Z",
            "duration_hours": duration_hours,
        }
        if salt:
            manifest["_salt"] = salt.hex()
        zf.writestr("manifest.json", json.dumps(manifest))
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            for i in range(2):
                zf.writestr(f"{d}/part-{i:03d}.parquet", VALID_PARQUET)
    return buf.getvalue()


def _inner_name(session_id: str, hours: int) -> str:
    return f"Bitget_MultiVenue_Microstructure_V2_SESSION_{session_id}_{hours}H.zip"


def _build_bundle(
    entries: list[tuple[str, bytes]] | None = None,
    *,
    include_all: bool = True,
) -> bytes:
    """Build an outer bundle ZIP.

    Default behavior: put all 11 OLD36 inner ZIPs at the top level with
    the canonical filenames.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        if include_all:
            for sid in OLD36_REFERENCE_SESSIONS:
                h = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
                zf.writestr(
                    _inner_name(sid, h),
                    _inner_zip_bytes(sid, float(h)),
                )
        if entries:
            for name, body in entries:
                zf.writestr(name, body)
    return out.getvalue()


def _write_temp(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from server import app
    import server as srv

    monkeypatch.setattr(srv, "RAW_DIR", tmp_path / "raw_zips")
    (tmp_path / "raw_zips").mkdir()
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"password": os.environ["SUPERBOT_PASSWORD"]})
    assert r.status_code == 200
    return c


@pytest.fixture(autouse=True)
def _clean_db():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM raw_files"))
        conn.execute(text("UPDATE sessions SET current_qa_run_id = NULL"))
        conn.execute(text("DELETE FROM qa_runs"))
        conn.execute(text("DELETE FROM sessions"))
        conn.execute(text("DELETE FROM audit_log"))
    yield


# ---------------------------------------------------------------------------
# Unit: outer-archive validation
# ---------------------------------------------------------------------------


class TestValidateBundle:
    def _open(self, data: bytes) -> zipfile.ZipFile:
        return zipfile.ZipFile(io.BytesIO(data), "r")

    def test_valid_bundle_passes(self):
        v = validate_bundle(self._open(_build_bundle()))
        assert v.is_ok()
        assert len(v.accepted) == 11
        assert {sid for _, sid in v.accepted} == set(OLD36_REFERENCE_SESSIONS)

    def test_missing_inner_zip_is_reported(self):
        # Build a valid bundle then drop one entry.
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for sid in OLD36_REFERENCE_SESSIONS[:-1]:
                h = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
                zf.writestr(_inner_name(sid, h), _inner_zip_bytes(sid, float(h)))
        v = validate_bundle(self._open(out.getvalue()))
        assert not v.is_ok()
        assert v.missing_session_ids == [OLD36_REFERENCE_SESSIONS[-1]]

    def test_unexpected_inner_zip_is_reported(self):
        # Add a session_id that is NOT in OLD36_REFERENCE.
        extra = _inner_name("20260101T000000Z_deadbeef", 3)
        v = validate_bundle(
            self._open(
                _build_bundle(
                    entries=[(extra, _inner_zip_bytes("20260101T000000Z_deadbeef", 3.0))],
                )
            )
        )
        assert not v.is_ok()
        assert "20260101T000000Z_deadbeef" in v.unexpected_session_ids

    def test_duplicate_inner_zip_is_reported(self):
        # Add the same first-slot inner ZIP a second time.
        sid = OLD36_REFERENCE_SESSIONS[0]
        h = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
        name = _inner_name(sid, h)
        v = validate_bundle(
            self._open(
                _build_bundle(entries=[(name, _inner_zip_bytes(sid, float(h)))])
            )
        )
        assert not v.is_ok()
        assert name in v.duplicates

    def test_nested_path_is_refused(self):
        v = validate_bundle(
            self._open(_build_bundle(entries=[("subdir/anything.zip", b"x")]))
        )
        assert not v.is_ok()
        assert v.nested_entries

    def test_absolute_path_is_refused(self):
        v = validate_bundle(
            self._open(_build_bundle(entries=[("/etc/passwd.zip", b"x")]))
        )
        assert not v.is_ok()
        assert v.traversal_entries

    def test_dotdot_is_refused(self):
        v = validate_bundle(
            self._open(_build_bundle(entries=[("../evil.zip", b"x")]))
        )
        assert not v.is_ok()
        assert v.traversal_entries

    def test_backslash_is_refused(self):
        v = validate_bundle(
            self._open(_build_bundle(entries=[("foo\\bar.zip", b"x")]))
        )
        assert not v.is_ok()
        assert v.traversal_entries

    def test_non_zip_top_level_is_refused(self):
        v = validate_bundle(
            self._open(_build_bundle(entries=[("README.txt", b"hello")]))
        )
        assert not v.is_ok()
        assert v.non_zip_entries == ["README.txt"]

    def test_non_conforming_zip_name_is_refused(self):
        v = validate_bundle(
            self._open(_build_bundle(entries=[("random.zip", b"x")]))
        )
        assert not v.is_ok()
        assert v.non_conforming_entries == ["random.zip"]


# ---------------------------------------------------------------------------
# Integration: end-to-end endpoint
# ---------------------------------------------------------------------------


def _upload_bundle_via_endpoint(client, filename: str, data: bytes) -> dict:
    init = client.post(
        "/api/bundles/init",
        json={"filename": filename, "total_size": len(data)},
    )
    assert init.status_code == 200, init.text
    uid = init.json()["upload_id"]
    chunk_size = init.json()["chunk_size"]
    for i in range(0, len(data), chunk_size):
        idx = i // chunk_size
        blob = data[i : i + chunk_size]
        r = client.post(f"/api/bundles/{uid}/chunk/{idx}", content=blob)
        assert r.status_code == 200, r.text
    final = client.post(f"/api/bundles/{uid}/complete", json={})
    return final.json() if final.status_code == 200 else {"__status": final.status_code, "__text": final.text}


class TestEndpoint:
    def test_valid_bundle_imports_all_11_sessions(self, client):
        cp0 = client.get("/api/checkpoints").json()["checkpoints"]
        resp = _upload_bundle_via_endpoint(
            client, "OLD36_REFERENCE_BUNDLE.zip", _build_bundle()
        )
        assert "results" in resp, resp
        assert resp["results"]["ok"] == 11
        assert resp["results"]["failed"] == 0
        # Every result must be OK and every session must be tagged OLD36_REFERENCE.
        for entry in resp["results"]["sessions"]:
            assert entry["ok"]
            assert entry["result"]["duplicate_status"] in ("NEW",)
        for sid in OLD36_REFERENCE_SESSIONS:
            r = client.get(f"/api/sessions/{sid}").json()
            assert r["checkpoint_hint"] == CHECKPOINT_OLD36_REFERENCE
        # Milestones untouched.
        cp1 = client.get("/api/checkpoints").json()["checkpoints"]
        for k in (CHECKPOINT_OLD36, CHECKPOINT_NEW12, CHECKPOINT_NEW36,
                  CHECKPOINT_TOTAL48, CHECKPOINT_TOTAL72):
            assert cp1[k] == cp0[k]
        # Raw-reference availability = 11/11.
        ref = client.get("/api/reference/old36").json()
        assert ref["present_sessions"] == 11
        assert ref["milestone_impact_hours"] == 0.0

    def test_missing_inner_zip_rejects_bundle(self, client):
        # Bundle with 10 of 11.
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for sid in OLD36_REFERENCE_SESSIONS[:-1]:
                h = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
                zf.writestr(_inner_name(sid, h), _inner_zip_bytes(sid, float(h)))
        resp = _upload_bundle_via_endpoint(client, "b.zip", out.getvalue())
        assert resp.get("__status") == 400, resp

    def test_unexpected_inner_zip_rejects_bundle(self, client):
        extra = _inner_name("20260101T000000Z_deadbeef", 3)
        data = _build_bundle(
            entries=[(extra, _inner_zip_bytes("20260101T000000Z_deadbeef", 3.0))]
        )
        resp = _upload_bundle_via_endpoint(client, "b.zip", data)
        assert resp.get("__status") == 400, resp

    def test_path_traversal_rejects_bundle(self, client):
        data = _build_bundle(entries=[("../evil.zip", b"junk")])
        resp = _upload_bundle_via_endpoint(client, "b.zip", data)
        assert resp.get("__status") == 400, resp

    def test_nested_zip_rejects_bundle(self, client):
        data = _build_bundle(entries=[("sub/x.zip", b"junk")])
        resp = _upload_bundle_via_endpoint(client, "b.zip", data)
        assert resp.get("__status") == 400, resp

    def test_non_zip_rejects_bundle(self, client):
        data = _build_bundle(entries=[("readme.txt", b"note")])
        resp = _upload_bundle_via_endpoint(client, "b.zip", data)
        assert resp.get("__status") == 400, resp

    def test_corrupt_inner_zip_flags_one_session_others_ok(self, client, tmp_path):
        # Build a bundle where ONE inner is corrupt.
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for i, sid in enumerate(OLD36_REFERENCE_SESSIONS):
                h = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
                if i == 3:
                    body = b"not a zip file at all"
                else:
                    body = _inner_zip_bytes(sid, float(h))
                zf.writestr(_inner_name(sid, h), body)
        resp = _upload_bundle_via_endpoint(client, "b.zip", out.getvalue())
        assert "results" in resp, resp
        # Ten succeed, one gets a non-PASS verdict OR a UNRESOLVED/FAIL
        # (the QA engine surfaces the bad inner ZIP through its own
        # verdict channel; we care that the OTHER ten succeeded).
        assert resp["results"]["total"] == 11
        # The bad slot's dict "ok" flag reflects only whether the QA
        # pipeline crashed; a non-PASS verdict is still ok=True. So we
        # check that the corrupt slot is not verdict PASS.
        entries_by_sid = {e["session_id"]: e for e in resp["results"]["sessions"]}
        bad_sid = OLD36_REFERENCE_SESSIONS[3]
        assert entries_by_sid[bad_sid]["result"]["verdict"] != "PASS"
        # And that at least ten others are PASS or PASS_WITH_WARNING.
        good = [
            e for sid, e in entries_by_sid.items()
            if sid != bad_sid
            and e["result"] is not None
            and e["result"]["verdict"] in ("PASS", "PASS_WITH_WARNING")
        ]
        assert len(good) == 10


class TestRetryAndDedup:
    def test_exact_duplicate_bundle_does_not_double_store(self, client, tmp_path, monkeypatch):
        import server as srv

        raw_dir = tmp_path / "raw_zips"
        raw_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(srv, "RAW_DIR", raw_dir)

        data = _build_bundle()
        resp1 = _upload_bundle_via_endpoint(client, "b.zip", data)
        assert resp1["results"]["ok"] == 11
        first_files = sorted(p.name for p in raw_dir.glob("*.zip"))
        assert len(first_files) == 11

        resp2 = _upload_bundle_via_endpoint(client, "b.zip", data)
        # Second bundle: every inner is EXACT_DUPLICATE, storage unchanged.
        for e in resp2["results"]["sessions"]:
            assert e["result"]["duplicate_status"] == "EXACT_DUPLICATE"
            assert e["result"]["validated_hours"] == 0.0
        second_files = sorted(p.name for p in raw_dir.glob("*.zip"))
        assert first_files == second_files

    def test_partial_failure_leaves_successful_sessions_committed(self, client):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for i, sid in enumerate(OLD36_REFERENCE_SESSIONS):
                h = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
                body = b"garbage" if i == 5 else _inner_zip_bytes(sid, float(h))
                zf.writestr(_inner_name(sid, h), body)
        _upload_bundle_via_endpoint(client, "b.zip", out.getvalue())
        # 10 sessions should now be present, 1 (index 5) may be present
        # with verdict != PASS or absent depending on QA outcome. The
        # OLD36_REFERENCE tag must apply to every session that DID land.
        ref = client.get("/api/reference/old36").json()
        assert ref["present_sessions"] >= 10


class TestTempCleanup:
    def test_outer_bundle_deleted_after_success(self, client, tmp_path):
        # BUNDLE_UPLOAD_DIR default is real; snapshot before/after.
        import server as srv

        before = set(srv.BUNDLE_UPLOAD_DIR.glob("*.part"))
        _upload_bundle_via_endpoint(client, "b.zip", _build_bundle())
        after = set(srv.BUNDLE_UPLOAD_DIR.glob("*.part"))
        assert after == before, "outer bundle .part file must not linger"

    def test_outer_bundle_deleted_after_validation_failure(self, client):
        import server as srv

        before = set(srv.BUNDLE_UPLOAD_DIR.glob("*.part"))
        _upload_bundle_via_endpoint(
            client,
            "b.zip",
            _build_bundle(entries=[("readme.txt", b"junk")]),
        )
        after = set(srv.BUNDLE_UPLOAD_DIR.glob("*.part"))
        assert after == before, "outer bundle .part file must not linger on reject"


class TestInvariantsAfterBundleImport:
    def test_new36_firewall_still_blocks_all_new36_ids(self, client):
        _upload_bundle_via_endpoint(client, "b.zip", _build_bundle())
        for sid in NEW36_SESSION_IDS:
            with pytest.raises(NEW36QuantitativeFirewallError):
                assert_recovery_allowed(sid)

    def test_frozen_engine_stays_not_configured(self, client):
        _upload_bundle_via_endpoint(client, "b.zip", _build_bundle())
        s = current_status()
        assert s.status == "NOT_CONFIGURED"
        assert s.accepts_input is False

    def test_single_session_upload_still_works(self, client):
        # Non-registered id via the regular /api/sessions/upload path.
        sid = "20260101T000000Z_smoke"
        zbytes = _inner_zip_bytes(sid, 3.0)
        r = client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["verdict"] in ("PASS", "PASS_WITH_WARNING")


class TestInterruptedUpload:
    def test_missing_chunk_blocks_finalize(self, client):
        data = _build_bundle()
        init = client.post(
            "/api/bundles/init",
            json={"filename": "b.zip", "total_size": len(data)},
        )
        uid = init.json()["upload_id"]
        chunk_size = init.json()["chunk_size"]
        chunks = [(i // chunk_size, data[i : i + chunk_size])
                  for i in range(0, len(data), chunk_size)]
        # Upload everything EXCEPT the middle chunk.
        for idx, blob in chunks:
            if idx == len(chunks) // 2:
                continue
            client.post(f"/api/bundles/{uid}/chunk/{idx}", content=blob)
        r = client.post(f"/api/bundles/{uid}/complete", json={})
        assert r.status_code == 400
        assert "Missing chunks" in r.text

    def test_retry_after_interruption_then_finalize(self, client):
        data = _build_bundle()
        init = client.post(
            "/api/bundles/init",
            json={"filename": "b.zip", "total_size": len(data)},
        )
        uid = init.json()["upload_id"]
        chunk_size = init.json()["chunk_size"]
        chunks = [(i // chunk_size, data[i : i + chunk_size])
                  for i in range(0, len(data), chunk_size)]
        # First pass: skip idx 0.
        for idx, blob in chunks[1:]:
            client.post(f"/api/bundles/{uid}/chunk/{idx}", content=blob)
        # Now resume by sending idx 0.
        client.post(f"/api/bundles/{uid}/chunk/0", content=chunks[0][1])
        r = client.post(f"/api/bundles/{uid}/complete", json={})
        assert r.status_code == 200, r.text
        assert r.json()["results"]["ok"] == 11
