"""Retention dedup regression tests.

Covers the § "V1 storage hardening" project directive:

- One unique uploaded binary produces ONE canonical retained artifact.
- Reprocessing appends QA runs but never duplicates the raw binary.
- EXACT_DUPLICATE upload does not create a second physical copy and
  does not add validated hours again.
- Same session_id + different SHA256 keeps distinct canonical
  artifacts (dedup is by content, not by session).
- Deleting redundant physical copies from disk never removes QA
  history from the database.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from constants import FROZEN_COLLECTOR_SHA256, PARQUET_MAGIC
from database import engine
from models import QARun, RawFile, Session as SessionModel
from raw_storage import canonical_path, is_valid_sha256, prune_orphan_blobs

sys.path.insert(0, "/app/backend/tests")
from fixtures import VALID_PARQUET  # noqa: E402


def _build_zip(session_id: str, salt: bytes = b"") -> bytes:
    """Build a QA-passing mini session ZIP. ``salt`` lets us vary the
    binary while keeping the session_id fixed, to exercise the
    same-session-different-file dedup branch.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "session_id": session_id,
            "exit_code": 0,
            "watchdog": False,
            "writer_errors": 0,
            "reconnect_summary": [],
            "collector_sha256": FROZEN_COLLECTOR_SHA256,
            "start_time": "2026-09-10T12:00:00Z",
            "end_time": "2026-09-10T15:00:00Z",
            "duration_hours": 3.0,
        }
        if salt:
            manifest["_salt"] = salt.hex()
        zf.writestr("manifest.json", json.dumps(manifest))
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            for i in range(2):
                zf.writestr(f"{d}/part-{i:03d}.parquet", VALID_PARQUET)
    return buf.getvalue()


@pytest.fixture()
def raw_dir(tmp_path, monkeypatch):
    """Give this module its own raw_zips dir under /tmp so we can
    assert on physical file counts without polluting other tests.
    """
    d = tmp_path / "raw_zips"
    d.mkdir()
    # Patch the module-level RAW_DIR the server uses for retention.
    import server as srv

    monkeypatch.setattr(srv, "RAW_DIR", d)
    return d


@pytest.fixture()
def client():
    from server import app

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


def _upload(client, filename: str, zbytes: bytes, retain_raw: bool = True):
    return client.post(
        "/api/sessions/upload",
        files={"files": (filename, zbytes, "application/zip")},
        data={"retain_raw": "true" if retain_raw else "false"},
    )


def _reprocess(client, session_id: str):
    return client.post(f"/api/sessions/{session_id}/reprocess")


class TestCanonicalPath:
    def test_canonical_path_shape(self, tmp_path):
        sha = "a" * 64
        p = canonical_path(tmp_path, sha)
        assert p.name == f"{sha}.zip"
        assert p.parent == tmp_path

    def test_canonical_path_rejects_bad_sha(self, tmp_path):
        for bad in ("", "abcd", "z" * 64, "A" * 64, None):
            with pytest.raises((ValueError, TypeError)):
                canonical_path(tmp_path, bad)  # type: ignore[arg-type]

    def test_is_valid_sha256(self):
        assert is_valid_sha256("0" * 64)
        assert is_valid_sha256("a" * 64)
        assert not is_valid_sha256("A" * 64)
        assert not is_valid_sha256("g" * 64)
        assert not is_valid_sha256("a" * 63)
        assert not is_valid_sha256(None)


class TestRetentionDedup:
    def test_upload_then_reprocess_10_times_yields_one_physical_file(
        self, client, raw_dir
    ):
        sid = "20260910T123759Z_retA"
        zbytes = _build_zip(sid)
        expected_sha = hashlib.sha256(zbytes).hexdigest()

        r = _upload(client, f"{sid}.zip", zbytes, retain_raw=True)
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["retained"] is True

        # Reprocess 10 times.
        for _ in range(10):
            r = _reprocess(client, sid)
            assert r.status_code == 200, r.text
            assert r.json()["retained"] is True

        # DB: 1 session, 11 QA runs, 11 RawFile rows all pointing at
        # the same canonical path.
        with engine.connect() as conn:
            sessions = conn.execute(
                text("SELECT COUNT(*) FROM sessions WHERE session_id = :s"),
                {"s": sid},
            ).scalar()
            assert sessions == 1

            qa_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM qa_runs WHERE session_id = :s"
                ),
                {"s": sid},
            ).scalar()
            assert qa_count == 11, f"expected 11 qa_runs, got {qa_count}"

            paths = conn.execute(
                text(
                    "SELECT DISTINCT r.stored_path FROM raw_files r "
                    "JOIN qa_runs q ON q.id = r.qa_run_id "
                    "WHERE q.session_id = :s"
                ),
                {"s": sid},
            ).scalars().all()
            assert len(paths) == 1, f"expected 1 stored_path, got {paths}"
            assert paths[0].endswith(f"{expected_sha}.zip")

        # Disk: exactly one .zip in the raw dir.
        zips = list(raw_dir.glob("*.zip"))
        assert len(zips) == 1, f"expected 1 physical file, got {zips}"
        assert zips[0].name == f"{expected_sha}.zip"

    def test_exact_duplicate_upload_never_creates_second_physical(
        self, client, raw_dir
    ):
        sid = "20260910T123759Z_retB"
        zbytes = _build_zip(sid)
        expected_sha = hashlib.sha256(zbytes).hexdigest()

        first = _upload(client, f"{sid}.zip", zbytes, retain_raw=True).json()
        assert first["results"][0]["duplicate_status"] == "NEW"
        first_hours = first["results"][0]["validated_hours"]
        assert first_hours == 3.0

        # Second upload: exact same bytes -> EXACT_DUPLICATE.
        second = _upload(client, f"{sid}.zip", zbytes, retain_raw=True).json()
        r2 = second["results"][0]
        assert r2["duplicate_status"] == "EXACT_DUPLICATE"
        assert r2["validated_hours"] == 0.0, (
            "duplicate upload must not add validated hours again"
        )

        # Disk: still one physical file.
        zips = list(raw_dir.glob("*.zip"))
        assert len(zips) == 1
        assert zips[0].name == f"{expected_sha}.zip"

        # Milestone hours never double-count.
        cp = client.get("/api/checkpoints").json()
        # This session_id is not in the frozen NEW36 list, so milestone
        # hours must stay at 0 regardless of duplicate uploads.
        assert cp["checkpoints"]["NEW12"]["hours"] == 0.0

    def test_same_session_different_sha_keeps_distinct_files(
        self, client, raw_dir
    ):
        sid = "20260910T123759Z_retC"
        z1 = _build_zip(sid, salt=b"one")
        z2 = _build_zip(sid, salt=b"two")
        sha1 = hashlib.sha256(z1).hexdigest()
        sha2 = hashlib.sha256(z2).hexdigest()
        assert sha1 != sha2

        r1 = _upload(client, f"{sid}_a.zip", z1, retain_raw=True).json()
        r2 = _upload(client, f"{sid}_b.zip", z2, retain_raw=True).json()

        assert r1["results"][0]["duplicate_status"] == "NEW"
        # Same session_id, different SHA256 -> SAME_SESSION_DIFFERENT_FILE.
        assert r2["results"][0]["duplicate_status"] == "SAME_SESSION_DIFFERENT_FILE"
        assert r2["results"][0]["validated_hours"] == 0.0, (
            "SAME_SESSION_DIFFERENT_FILE must not add hours again"
        )

        zips = {p.name for p in raw_dir.glob("*.zip")}
        assert zips == {f"{sha1}.zip", f"{sha2}.zip"}, zips

    def test_deleting_physical_copies_never_removes_qa_history(
        self, client, raw_dir
    ):
        sid = "20260910T123759Z_retD"
        zbytes = _build_zip(sid)
        _upload(client, f"{sid}.zip", zbytes, retain_raw=True)
        for _ in range(3):
            _reprocess(client, sid)

        # Nuke the on-disk canonical file entirely.
        zips = list(raw_dir.glob("*.zip"))
        assert len(zips) == 1
        zips[0].unlink()

        # QA history is still intact in the DB.
        with engine.connect() as conn:
            qa_count = conn.execute(
                text("SELECT COUNT(*) FROM qa_runs WHERE session_id = :s"),
                {"s": sid},
            ).scalar()
            assert qa_count == 4

            aud_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE session_id = :s"
                ),
                {"s": sid},
            ).scalar()
            # 1 upload + 3 reprocess = 4 audit events at minimum.
            assert aud_count >= 4

        # Registry endpoint still returns the session.
        r = client.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        assert len(r.json()["qa_runs"]) == 4

    def test_prune_orphan_blobs_leaves_referenced_files(self, tmp_path):
        # Set up: two files, one referenced, one orphan.
        d = tmp_path / "raw_zips"
        d.mkdir()
        ref = d / (("a" * 64) + ".zip")
        orphan = d / (("b" * 64) + ".zip")
        ref.write_bytes(b"content-ref")
        orphan.write_bytes(b"content-orphan")

        removed = prune_orphan_blobs(d, [str(ref)])
        assert orphan.name in {Path(p).name for p in removed}
        assert ref.exists()
        assert not orphan.exists()

    def test_prune_orphan_blobs_noop_when_all_referenced(self, tmp_path):
        d = tmp_path / "raw_zips"
        d.mkdir()
        a = d / (("a" * 64) + ".zip")
        b = d / (("c" * 64) + ".zip")
        a.write_bytes(b"x")
        b.write_bytes(b"y")
        removed = prune_orphan_blobs(d, [str(a), str(b)])
        assert removed == []
        assert a.exists() and b.exists()
