"""Test for partial-import atomicity fix (ENOSPC scenario).

This test verifies that when an inner-session pipeline fails (e.g., due to
ENOSPC after processing some sessions), the bundle job is marked FAILED
(NOT COMPLETE) with a clear error message indicating the partial failure.

This addresses the bug where job cb87152f-3ecd-4ca6-8d6e-9b280b1b9619 was
marked COMPLETE despite 9/11 sessions failing with ENOSPC.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import multipart_bundle as mp_mod
from checkpoint_registry import (
    OLD36_REFERENCE_NOMINAL_HOURS,
    OLD36_REFERENCE_SESSIONS,
)
from constants import FROZEN_COLLECTOR_SHA256
from database import SessionLocal, engine

sys.path.insert(0, "/app/backend/tests")
from fixtures import VALID_PARQUET  # noqa: E402


def _inner_zip_bytes(session_id: str, duration_hours: float) -> bytes:
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
        zf.writestr("manifest.json", json.dumps(manifest))
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            for i in range(2):
                zf.writestr(f"{d}/part-{i:03d}.parquet", VALID_PARQUET)
    return buf.getvalue()


def _inner_name(sid: str, hours: int) -> str:
    return f"Bitget_MultiVenue_Microstructure_V2_SESSION_{sid}_{hours}H.zip"


def _build_valid_outer_bundle() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for sid in OLD36_REFERENCE_SESSIONS:
            h = int(OLD36_REFERENCE_NOMINAL_HOURS[sid])
            zf.writestr(_inner_name(sid, h), _inner_zip_bytes(sid, float(h)))
    return out.getvalue()


def _split_five(data: bytes) -> list[tuple[str, int, bytes]]:
    n = len(data)
    per = (n + 4) // 5
    parts: list[tuple[str, int, bytes]] = []
    offset = 0
    for i in range(5):
        chunk = data[offset:offset + per]
        name = f"OLD36_REFERENCE_BUNDLE.zip.part-0{i}"
        parts.append((name, len(chunk), chunk))
        offset += len(chunk)
    return parts


@pytest.fixture()
def patched_manifest(monkeypatch):
    outer = _build_valid_outer_bundle()
    parts = _split_five(outer)
    exp_parts = tuple((n, s) for n, s, _ in parts)
    exp_sha = hashlib.sha256(outer).hexdigest()
    exp_size = len(outer)
    monkeypatch.setattr(mp_mod, "EXPECTED_PARTS", exp_parts)
    monkeypatch.setattr(mp_mod, "EXPECTED_BUNDLE_TOTAL_SIZE", exp_size)
    monkeypatch.setattr(mp_mod, "EXPECTED_BUNDLE_SHA256", exp_sha)

    import server as srv

    srv.multipart_controller = mp_mod.MultipartBundleController(
        upload_manager=srv.bundle_manager,
        base_dir=srv.MULTIPART_WORK_DIR,
    )
    from bundle_job_worker import BundleJobManager

    srv.bundle_job_manager = BundleJobManager(
        multipart_controller=srv.multipart_controller,
        import_runner=srv._bundle_import_runner,
        max_workers=1,
    )
    yield {
        "outer": outer,
        "parts": parts,
        "expected_sha": exp_sha,
        "expected_size": exp_size,
    }
    srv.bundle_job_manager.shutdown(wait=False)


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


def _init(client, parts):
    return client.post(
        "/api/bundles/multipart/init",
        json={"parts": [{"name": n, "size": s} for n, s, _ in parts]},
    )


def _upload_part(client, slot, blob):
    upload_id = slot["upload_id"]
    total = slot["expected_size"]
    r = client.post(f"/api/bundles/{upload_id}/chunk/0", content=blob[:total])
    assert r.status_code == 200, r.text


def _full_upload(client, mp, parts):
    for slot, (_, _, blob) in zip(mp["slots"], parts):
        _upload_part(client, slot, blob)


def _wait_terminal(client, job_id, *, timeout=60):
    from server import bundle_job_manager

    bundle_job_manager.wait(job_id, timeout=timeout)
    return client.get(f"/api/bundles/jobs/{job_id}").json()["job"]


class TestPartialImportAtomicity:
    """Test that partial bundle imports are marked FAILED, not COMPLETE."""

    def test_enospc_style_failure_marks_job_failed_not_complete(
        self, patched_manifest, client, monkeypatch
    ):
        """Simulate ENOSPC: first 2 sessions succeed, remaining 9 fail.
        
        The bundle job MUST end in status=FAILED with an error message
        containing 'bundle finalize partial: ok=2 failed=9 total=11'.
        
        This is the atomic-bundle fix: partial success is NOT success.
        """
        import server as srv
        
        # Track how many times _process_source is called
        call_count = [0]
        original_process_source = srv._process_source
        
        def _failing_process_source(db, source, filename, retain_raw, checkpoint_hint_override):
            """Allow first 2 calls to succeed, fail the rest with OSError (ENOSPC)."""
            call_count[0] += 1
            if call_count[0] <= 2:
                # Let the first 2 sessions succeed
                return original_process_source(db, source, filename, retain_raw, checkpoint_hint_override)
            else:
                # Simulate ENOSPC for sessions 3-11
                raise OSError(28, "No space left on device")
        
        monkeypatch.setattr(srv, "_process_source", _failing_process_source)
        
        # Upload the bundle
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        
        # Wait for terminal state
        final = _wait_terminal(client, job_id)
        
        # CRITICAL: The job MUST be FAILED, not COMPLETE
        assert final["status"] == "FAILED", (
            f"Expected status=FAILED for partial import, got {final['status']}"
        )
        
        # The error message MUST indicate the partial failure
        error_msg = final.get("error_message", "")
        assert "bundle finalize partial" in error_msg, (
            f"Error message should contain 'bundle finalize partial', got: {error_msg}"
        )
        assert "ok=2" in error_msg, f"Error should show ok=2, got: {error_msg}"
        assert "failed=9" in error_msg, f"Error should show failed=9, got: {error_msg}"
        assert "total=11" in error_msg, f"Error should show total=11, got: {error_msg}"
        
        # Verify counters
        assert final["sessions_processed"] == 11, (
            f"Should have attempted all 11 sessions, got {final['sessions_processed']}"
        )
        assert final["sessions_passed"] == 2, (
            f"Should have 2 passed sessions, got {final['sessions_passed']}"
        )
        assert final["sessions_failed"] == 9, (
            f"Should have 9 failed sessions, got {final['sessions_failed']}"
        )
        
        print("✅ Partial-import atomicity test PASSED")
        print(f"   Status: {final['status']}")
        print(f"   Error: {error_msg[:100]}...")
        print(f"   Counters: {final['sessions_passed']}/{final['sessions_processed']} passed")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
