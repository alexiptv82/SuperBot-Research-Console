"""Focused tests for the SAFE OLD36 missing-RAW recovery path.

Coverage
--------

1. OLD36 server-side identity enforcement — filename is UX only.
2. Disk preflight on retained /api/uploads/init (BLOCKED_DISK_SPACE).
3. Pre-retention disk recheck inside _process_source.
4. Physical RAW verification via _verify_physical_raw() —
     rejects missing / zero-size / non-file / unreadable paths and
     accepts only when every deterministic condition is met.
5. raw_ready derivation: 10/11 verified → False, 11/11 → True.
6. Non-OLD36 ingestion (checkpoint_hint != OLD36_REFERENCE) is
   unaffected by the identity guard.
7. Invariants: NEW36 firewall + engine + milestones unchanged.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from collections import namedtuple
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from checkpoint_registry import (
    NEW36_SESSION_IDS,
    OLD36_REFERENCE_NOMINAL_HOURS,
    OLD36_REFERENCE_SESSIONS,
)
from checkpoints import _verify_physical_raw
from constants import (
    CHECKPOINT_OLD36,
    CHECKPOINT_NEW12,
    CHECKPOINT_NEW36,
    CHECKPOINT_TOTAL48,
    CHECKPOINT_TOTAL72,
    FROZEN_COLLECTOR_SHA256,
)
from database import SessionLocal, engine
from frozen_engine import current_status
from models import BundleJob, QARun, RawFile
from models import Session as SessionModel  # noqa: E402
from recovery.allowlist import NEW36QuantitativeFirewallError, assert_recovery_allowed

sys.path.insert(0, "/app/backend/tests")
from fixtures import VALID_PARQUET  # noqa: E402


DiskUsage = namedtuple("usage", "total used free")


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
        conn.execute(text("DELETE FROM bundle_jobs"))
    yield


def _make_old36_zip(sid: str, hours: int) -> bytes:
    """Build a minimal valid OLD36 reference ZIP with a canonical
    filename, wrapped so QA passes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "session_id": sid,
            "exit_code": 0,
            "watchdog": False,
            "writer_errors": 0,
            "reconnect_summary": [],
            "collector_sha256": FROZEN_COLLECTOR_SHA256,
            "start_time": "2026-09-06T09:25:48Z",
            "end_time": "2026-09-06T12:25:48Z",
            "duration_hours": float(hours),
        }
        zf.writestr("manifest.json", json.dumps(manifest))
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            for i in range(2):
                zf.writestr(f"{d}/part-{i:03d}.parquet", VALID_PARQUET)
    return buf.getvalue()


def _canonical_zip_name(sid: str, hours: int) -> str:
    return f"Bitget_MultiVenue_Microstructure_V2_SESSION_{sid}_{hours}H.zip"


# ---------------------------------------------------------------------------
# 1. Disk preflight (Patch1 merge)
# ---------------------------------------------------------------------------


class TestRetainedUploadDiskPreflight:
    def test_retained_init_blocks_before_reserve_breach(self, client, monkeypatch):
        import server as srv

        monkeypatch.setattr(srv.shutil, "disk_usage", lambda _p: DiskUsage(10_000, 9_500, 500))
        monkeypatch.setattr(srv, "MIN_FREE_RESERVE_BYTES", 400)
        r = client.post(
            "/api/uploads/init",
            json={
                "filename": "missing-old36.zip",
                "total_size": 200,
                "chunk_size": 100,
                "retain_raw": True,
                "checkpoint_hint": "OLD36_REFERENCE",
            },
        )
        assert r.status_code == 507, r.text
        detail = r.json()["detail"]
        assert "BLOCKED_DISK_SPACE" in detail
        assert "limiting_path" in detail

    def test_non_retained_upload_ignores_reserve(self, client, monkeypatch):
        import server as srv

        monkeypatch.setattr(srv.shutil, "disk_usage", lambda _p: DiskUsage(10_000, 9_900, 100))
        monkeypatch.setattr(srv, "MIN_FREE_RESERVE_BYTES", 400)
        r = client.post(
            "/api/uploads/init",
            json={
                "filename": "diag.zip",
                "total_size": 2,
                "chunk_size": 1,
                "retain_raw": False,
            },
        )
        assert r.status_code == 200, r.text
        client.delete(f"/api/uploads/{r.json()['upload_id']}")

    def test_limits_exposes_disk_snapshot(self, client, monkeypatch):
        import server as srv

        monkeypatch.setattr(srv.shutil, "disk_usage", lambda _p: DiskUsage(10_000, 4_000, 6_000))
        monkeypatch.setattr(srv, "MIN_FREE_RESERVE_BYTES", 777)
        r = client.get("/api/uploads/limits")
        assert r.status_code == 200
        d = r.json()["disk"]
        assert d["free_bytes"] == 6000
        assert d["min_free_reserve_bytes"] == 777
        assert d["safe"] is True
        assert d["limiting_path"]


# ---------------------------------------------------------------------------
# 2. OLD36 server-side identity enforcement
# ---------------------------------------------------------------------------


class TestOld36IdentityEnforcement:
    def test_filename_lie_rejected_by_qa_session_id(self, client):
        """A ZIP whose FILENAME claims to be an OLD36 reference file
        but whose internal manifest session_id is not in the frozen
        registry must be rejected with a deterministic
        ``OLD36_IDENTITY_REJECTED`` error and MUST NOT create a
        session/QA row."""
        rogue_id = "20991231T000000Z_deadbeef"
        payload = _make_old36_zip(rogue_id, 3)
        # Filename SPOOFS a valid OLD36 session id.
        canonical_id = next(iter(OLD36_REFERENCE_SESSIONS))
        filename = _canonical_zip_name(canonical_id, 3)
        r = client.post(
            "/api/sessions/upload",
            files={"files": (filename, payload, "application/zip")},
            data={"checkpoint_hint": "OLD36_REFERENCE"},
        )
        assert r.status_code == 400, r.text
        assert "OLD36_IDENTITY_REJECTED" in r.json()["detail"]
        # No poisoned session row.
        db = SessionLocal()
        try:
            assert (
                db.query(SessionModel).filter_by(session_id=rogue_id).first() is None
            )
            audit_hits = db.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE event_type='old36.identity.reject'"
                )
            ).scalar()
            assert audit_hits >= 1
        finally:
            db.close()

    def test_valid_old36_id_accepted(self, client):
        sid = next(iter(OLD36_REFERENCE_SESSIONS))
        hours = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
        payload = _make_old36_zip(sid, hours)
        r = client.post(
            "/api/sessions/upload",
            files={
                "files": (_canonical_zip_name(sid, hours), payload, "application/zip"),
            },
            data={"checkpoint_hint": "OLD36_REFERENCE"},
        )
        assert r.status_code == 200, r.text

    def test_non_old36_ingestion_unaffected(self, client):
        """A random session id must be accepted under a non-OLD36
        checkpoint mode without any identity gate firing."""
        rogue_id = "20991231T000000Z_deadbeef"
        payload = _make_old36_zip(rogue_id, 3)
        r = client.post(
            "/api/sessions/upload",
            files={
                "files": (
                    "random-session-xyz-3H.zip",
                    payload,
                    "application/zip",
                ),
            },
        )
        # Do NOT assert on status code — QA / dedup may reject on
        # unrelated grounds. Only require the failure is not the
        # OLD36 identity gate.
        if r.status_code == 400:
            assert "OLD36_IDENTITY_REJECTED" not in r.text


# ---------------------------------------------------------------------------
# 3. Pre-retention disk recheck
# ---------------------------------------------------------------------------


class TestPreRetentionRecheck:
    def test_retention_time_recheck_blocks_new_write(self, client, monkeypatch):
        """Ingest passes preflight, but shutil.disk_usage() is
        monkey-patched to a starved value before retention. The
        request must fail with 507 BLOCKED_DISK_SPACE and NO
        raw_files row should be committed."""
        import server as srv

        sid = next(iter(OLD36_REFERENCE_SESSIONS))
        hours = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
        payload = _make_old36_zip(sid, hours)

        # Preflight sees generous space (safe=True).
        state = {"phase": "preflight"}

        def _fake_du(_p):
            if state["phase"] == "preflight":
                return DiskUsage(10_000, 100, 9_900)
            return DiskUsage(10_000, 9_990, 10)

        monkeypatch.setattr(srv.shutil, "disk_usage", _fake_du)
        monkeypatch.setattr(srv, "MIN_FREE_RESERVE_BYTES", 5_000)

        state["phase"] = "recheck"
        r = client.post(
            "/api/sessions/upload",
            files={
                "files": (_canonical_zip_name(sid, hours), payload, "application/zip"),
            },
            data={"checkpoint_hint": "OLD36_REFERENCE", "retain_raw": "true"},
        )
        assert r.status_code == 507, r.text
        assert "BLOCKED_DISK_SPACE" in r.json()["detail"]

        db = SessionLocal()
        try:
            assert db.query(RawFile).count() == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 4. Physical RAW verification helper
# ---------------------------------------------------------------------------


class TestVerifyPhysicalRaw:
    def _rf(self, path: str | None, retained: bool = True):
        # Lightweight duck-typed stub to avoid a real DB round-trip.
        class _RF:
            pass
        rf = _RF()
        rf.retained = retained
        rf.stored_path = path
        return rf

    def test_missing_file_rejected(self, tmp_path):
        rf = self._rf(str(tmp_path / "does-not-exist.zip"))
        ok, why = _verify_physical_raw(rf, "sid")
        assert ok is False and "does not exist" in why

    def test_zero_size_rejected(self, tmp_path):
        p = tmp_path / "empty.zip"
        p.write_bytes(b"")
        ok, why = _verify_physical_raw(self._rf(str(p)), "sid")
        assert ok is False and "zero-size" in why

    def test_directory_rejected(self, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        ok, why = _verify_physical_raw(self._rf(str(d)), "sid")
        assert ok is False and "regular file" in why

    def test_retained_flag_false_rejected(self, tmp_path):
        p = tmp_path / "z.zip"
        p.write_bytes(b"payload")
        ok, why = _verify_physical_raw(self._rf(str(p), retained=False), "sid")
        assert ok is False and "retained" in why

    def test_null_stored_path_rejected(self):
        ok, why = _verify_physical_raw(self._rf(None), "sid")
        assert ok is False and "stored_path" in why

    def test_valid_file_accepted(self, tmp_path):
        p = tmp_path / "z.zip"
        p.write_bytes(b"a" * 8192)
        ok, why = _verify_physical_raw(self._rf(str(p)), "sid")
        assert ok is True and why is None


# ---------------------------------------------------------------------------
# 5. raw_ready derivation
# ---------------------------------------------------------------------------


class TestRawReadyDerivation:
    def _seed(self, db, sid, retained_ok: bool, tmp_path: Path):
        """Insert session + qa_run + raw_file rows. When
        ``retained_ok`` is True, create a real 4 KiB file so the
        physical verifier passes."""
        # Create a session row.
        s = SessionModel(session_id=sid, checkpoint_hint="OLD36_REFERENCE")
        db.add(s)
        db.flush()
        qa = QARun(
            session_pk=s.id,
            session_id=sid,
            original_filename=f"{sid}.zip",
            source_file_sha256="ab" * 32,
            operational_status="PASS",
            duplicate_status="NEW",
            duration_hours=3.0,
            validated_hours=3.0,
        )
        db.add(qa)
        db.flush()
        s.current_qa_run_id = qa.id

        if retained_ok:
            blob = tmp_path / f"{sid}.zip"
            blob.write_bytes(b"x" * 4096)
            rf = RawFile(
                qa_run_id=qa.id,
                stored_path=str(blob),
                size_bytes=4096,
                retained=True,
                retention_reason="user_toggle",
            )
            db.add(rf)
        db.commit()

    def test_10_of_11_verified_returns_false(self, client, tmp_path):
        db = SessionLocal()
        try:
            all_ids = list(OLD36_REFERENCE_SESSIONS)
            for sid in all_ids[:-1]:
                self._seed(db, sid, retained_ok=True, tmp_path=tmp_path)
            self._seed(db, all_ids[-1], retained_ok=False, tmp_path=tmp_path)
        finally:
            db.close()
        r = client.get("/api/reference/old36").json()
        assert r["raw_retained_sessions"] == 10
        assert r["raw_ready"] is False

    def test_11_of_11_verified_returns_true(self, client, tmp_path):
        db = SessionLocal()
        try:
            for sid in OLD36_REFERENCE_SESSIONS:
                self._seed(db, sid, retained_ok=True, tmp_path=tmp_path)
        finally:
            db.close()
        r = client.get("/api/reference/old36").json()
        assert r["raw_retained_sessions"] == 11
        assert r["raw_ready"] is True


# ---------------------------------------------------------------------------
# 6. Global invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_new36_firewall(self):
        for sid in list(NEW36_SESSION_IDS)[:3]:
            with pytest.raises(NEW36QuantitativeFirewallError):
                assert_recovery_allowed(sid)

    def test_engine_not_configured(self):
        st = current_status()
        assert st.status == "NOT_CONFIGURED"
        assert st.accepts_input is False

    def test_milestones_targets_unchanged(self, client):
        """OLD36 baseline is a fixed constant; NEW12/NEW36 are session-
        registered so they depend on runtime DB state. Assert the
        contract that MUST hold at every DB state: OLD36 baseline stays
        at 36 h and the TOTAL72 target formula stays intact."""
        cp = client.get("/api/checkpoints").json()["checkpoints"]
        assert cp[CHECKPOINT_OLD36]["hours"] == 36.0
        assert cp[CHECKPOINT_OLD36]["target"] == 36.0
        assert cp[CHECKPOINT_NEW12]["target"] == 12.0
        assert cp[CHECKPOINT_NEW36]["target"] == 36.0
        assert cp[CHECKPOINT_TOTAL48]["target"] == 48.0
        assert cp[CHECKPOINT_TOTAL72]["target"] == 72.0
