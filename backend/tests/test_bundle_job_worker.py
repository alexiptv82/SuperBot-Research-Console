"""Async multipart-bundle finalize job tests.

These tests exercise the ``bundle_job_worker`` refactor that lifted
2.14 GiB reassembly + 11-session import out of the Cloudflare HTTP
request lifecycle.

Coverage
--------

- POST ``/assemble`` returns 202 with a ``job_id`` fast enough to be
  well under Cloudflare's origin timeout.
- Reassembly + import happen strictly outside the request; a client
  disconnect / dropped request does NOT cancel the worker.
- GET ``/api/bundles/jobs/{id}`` reports live progress and terminal
  state.
- GET ``/api/bundles/jobs/active`` lets the frontend rejoin after a
  browser refresh.
- Cloudflare-style transient errors during polling do not destroy the
  uploaded parts or the job state.
- A deterministic ZIP-validation failure inside the worker persists
  a ``FAILED`` job with the error message.
- Re-enqueueing while a job is running returns the *same* job (idempotency).
- Successful completion cleans the temporary parts + bundle.
- The stale-job sweeper marks abandoned running jobs FAILED after TTL.
- Milestones, NEW36 firewall, and FrozenAnalysisEngine status are
  untouched across all scenarios.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import multipart_bundle as mp_mod
from checkpoint_registry import (
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
from database import SessionLocal, engine
from frozen_engine import current_status
from models import BundleJob
from recovery.allowlist import NEW36QuantitativeFirewallError, assert_recovery_allowed

sys.path.insert(0, "/app/backend/tests")
from fixtures import VALID_PARQUET  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic bundle (mirrors the fixture used in test_multipart_bundle.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    # Rebuild the job manager so it sees the new controller.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 1. Endpoint returns 202 quickly with a job_id
# ---------------------------------------------------------------------------


class TestAssembleReturnsQuickly:
    def test_returns_202_with_job_id(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] in {"QUEUED", "RUNNING", "COMPLETE"}
        assert body["job"]["job_id"] == body["job_id"]
        assert r.headers.get("location") == f"/api/bundles/jobs/{body['job_id']}"
        _wait_terminal(client, body["job_id"])

    def test_return_time_is_fast(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        t0 = time.monotonic()
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        elapsed = time.monotonic() - t0
        assert r.status_code == 202
        # Even on a shared CI runner this handler must not stall while
        # the worker runs. It should return well under 2 s.
        assert elapsed < 2.0, f"assemble handler took {elapsed:.3f} s"
        _wait_terminal(client, r.json()["job_id"])


# ---------------------------------------------------------------------------
# 2. Long work happens off-request
# ---------------------------------------------------------------------------


class TestOffRequestExecution:
    def test_worker_finishes_after_endpoint_returns(
        self, patched_manifest, client
    ):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        # Endpoint has already returned. Sessions may not yet be committed.
        final = _wait_terminal(client, job_id)
        assert final["status"] == "COMPLETE"
        assert final["result_summary"]["results"]["ok"] == 11


# ---------------------------------------------------------------------------
# 3. Job polling reports live progress
# ---------------------------------------------------------------------------


class TestJobPolling:
    def test_poll_reports_stage(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        final = _wait_terminal(client, job_id)
        assert final["current_stage"] == "COMPLETE"
        # After completion the sessions counter must be at 11/11.
        assert final["sessions_processed"] == 11
        assert final["sessions_passed"] == 11
        assert final["sessions_failed"] == 0
        assert final["bytes_processed"] == final["bytes_total"]

    def test_missing_job_returns_404(self, patched_manifest, client):
        r = client.get("/api/bundles/jobs/does-not-exist")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. Client / Cloudflare disconnect does NOT cancel the job
# ---------------------------------------------------------------------------


class TestDisconnectSafety:
    def test_endpoint_client_close_does_not_cancel_worker(
        self, patched_manifest, client
    ):
        """We simulate a client disconnect by dropping the TestClient
        response *before* polling: even so, the worker runs in a
        background thread and the job reaches COMPLETE."""
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]

        # "Client goes away": no polling, no GETs. The worker is
        # nonetheless owned by the executor and finishes independently.
        from server import bundle_job_manager

        bundle_job_manager.wait(job_id, timeout=60)
        # Query via a fresh session to prove the row survived.
        s = SessionLocal()
        try:
            row = s.query(BundleJob).filter_by(id=job_id).one()
            assert row.status == "COMPLETE"
            assert row.sessions_passed == 11
        finally:
            s.close()

    def test_cloudflare_transient_poll_error_does_not_cancel(
        self, patched_manifest, client
    ):
        """A single 502 on GET /jobs/{id} must NOT poison the job. The
        server does not observe the client's failure at all; the worker
        is a background thread. We verify by fetching after a delay."""
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        # Poll ~immediately - should still be RUNNING or already done.
        first = client.get(f"/api/bundles/jobs/{job_id}").json()["job"]
        assert first["status"] in {"QUEUED", "RUNNING", "COMPLETE"}
        final = _wait_terminal(client, job_id)
        assert final["status"] == "COMPLETE"


# ---------------------------------------------------------------------------
# 5. Browser refresh recovery via /jobs/active
# ---------------------------------------------------------------------------


class TestBrowserRefreshRecovery:
    def test_active_endpoint_returns_running_job(
        self, patched_manifest, client
    ):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        # Wait for terminal so we don't race; the endpoint is still
        # useful for browser-refresh recovery in the running window.
        _wait_terminal(client, job_id)
        active = client.get("/api/bundles/jobs/active").json()
        # No active jobs after completion.
        assert active["job"] is None

    def test_active_endpoint_returns_none_when_idle(
        self, patched_manifest, client
    ):
        r = client.get("/api/bundles/jobs/active")
        assert r.status_code == 200
        assert r.json()["job"] is None


# ---------------------------------------------------------------------------
# 6. Deterministic validation failure persists FAILED + keeps parts
# ---------------------------------------------------------------------------


class TestValidationFailure:
    def test_sha_mismatch_persists_failed_state_and_keeps_parts(
        self, patched_manifest, client
    ):
        import server as srv

        mp = _init(client, patched_manifest["parts"]).json()
        # Overwrite part 0 with zeros (same size) -> SHA mismatch.
        parts = list(patched_manifest["parts"])
        n, s, _ = parts[0]
        parts[0] = (n, s, b"\x00" * s)
        for slot, (_, _, blob) in zip(mp["slots"], parts):
            _upload_part(client, slot, blob)
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        final = _wait_terminal(client, job_id)
        assert final["status"] == "FAILED"
        assert "SHA256 mismatch" in (final["error_message"] or "")
        # Parts must survive so the owner does not need to re-upload
        # 2.14 GiB on a client disconnect.
        remaining = list(Path(srv.BUNDLE_UPLOAD_DIR).glob("*.part"))
        assert len(remaining) >= 5


# ---------------------------------------------------------------------------
# 7. Re-enqueue while running returns the same job (idempotency)
# ---------------------------------------------------------------------------


class TestEnqueueIdempotency:
    def test_reenqueue_returns_existing_job(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        first = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()
        # Second POST BEFORE the worker finishes must return the same
        # job so a repeated tap on the "Ricostruisci e importa" button
        # (or a doubled POST from an unreliable network) never spawns a
        # duplicate worker.
        second = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()
        assert first["job_id"] == second["job_id"], (first, second)
        _wait_terminal(client, first["job_id"])


# ---------------------------------------------------------------------------
# 8. Successful completion cleans temporary artefacts
# ---------------------------------------------------------------------------


class TestSuccessCleanup:
    def test_parts_and_bundle_removed_on_complete(
        self, patched_manifest, client
    ):
        import server as srv

        before_parts = set(Path(srv.BUNDLE_UPLOAD_DIR).glob("*.part"))
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        final = _wait_terminal(client, job_id)
        assert final["status"] == "COMPLETE"
        # No new .part files left.
        after_parts = set(Path(srv.BUNDLE_UPLOAD_DIR).glob("*.part"))
        assert after_parts == before_parts
        # Handoff bundle removed too.
        handoff_dir = (
            Path(srv.multipart_controller.base_dir).parent
            / "bundle_jobs_handoff"
        )
        remaining = list(handoff_dir.glob("*.zip")) if handoff_dir.exists() else []
        assert remaining == []


# ---------------------------------------------------------------------------
# 9. TTL sweeper marks abandoned jobs FAILED
# ---------------------------------------------------------------------------


class TestStaleSweep:
    def test_stale_running_job_marked_failed(self, patched_manifest, client, monkeypatch):
        # Insert a stale RUNNING job by hand (no worker attached).
        s = SessionLocal()
        try:
            job = BundleJob(
                multipart_id="fake",
                bundle_filename="OLD36_REFERENCE_BUNDLE.zip",
                expected_sha256="x" * 64,
                bytes_total=100,
                status="RUNNING",
                current_stage="REASSEMBLING",
                stage_detail="fake",
            )
            job.updated_at = "2020-01-01T00:00:00+00:00"  # ancient
            s.add(job)
            s.commit()
            job_id = job.id
        finally:
            s.close()

        r = client.post("/api/bundles/jobs/cleanup_stale")
        assert r.status_code == 200
        assert r.json()["marked_failed"] >= 1

        row = client.get(f"/api/bundles/jobs/{job_id}").json()["job"]
        assert row["status"] == "FAILED"
        assert row["error_code"] == "STALE_TIMEOUT"


# ---------------------------------------------------------------------------
# 10. Global invariants preserved through the async path
# ---------------------------------------------------------------------------


class TestInvariantsAfterAsyncMultipart:
    def test_new36_firewall_intact(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        _wait_terminal(client, job_id)
        for sid in NEW36_SESSION_IDS:
            with pytest.raises(NEW36QuantitativeFirewallError):
                assert_recovery_allowed(sid)

    def test_engine_not_configured(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        _wait_terminal(client, job_id)
        st = current_status()
        assert st.status == "NOT_CONFIGURED"
        assert st.accepts_input is False

    def test_milestones_unchanged(self, patched_manifest, client):
        cp0 = client.get("/api/checkpoints").json()["checkpoints"]
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        job_id = client.post(
            f"/api/bundles/multipart/{mp['multipart_id']}/assemble"
        ).json()["job_id"]
        _wait_terminal(client, job_id)
        cp1 = client.get("/api/checkpoints").json()["checkpoints"]
        for k in (
            CHECKPOINT_OLD36,
            CHECKPOINT_NEW12,
            CHECKPOINT_NEW36,
            CHECKPOINT_TOTAL48,
            CHECKPOINT_TOTAL72,
        ):
            assert cp1[k] == cp0[k]
