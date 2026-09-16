"""Multipart-bundle reconstruction tests.

Synthetic fixtures only. We monkeypatch the frozen manifest constants
(``EXPECTED_PARTS``, ``EXPECTED_BUNDLE_TOTAL_SIZE``,
``EXPECTED_BUNDLE_SHA256``) so the tests exercise the exact same
code path against a small ~200 KiB bundle instead of the real 2.15
GiB one. The bundle payload is a valid outer OLD36 bundle built by
the existing single-bundle test fixture.
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
from database import engine
from frozen_engine import current_status
from recovery.allowlist import NEW36QuantitativeFirewallError, assert_recovery_allowed

sys.path.insert(0, "/app/backend/tests")
from fixtures import VALID_PARQUET  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic outer bundle (reused from the single-bundle tests)
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
            zf.writestr(
                _inner_name(sid, h),
                _inner_zip_bytes(sid, float(h)),
            )
    return out.getvalue()


# ---------------------------------------------------------------------------
# Manifest patching \u2014 rewrite EXPECTED_PARTS / SHA / SIZE per test
# ---------------------------------------------------------------------------


def _split_five(data: bytes) -> list[tuple[str, int, bytes]]:
    """Split ``data`` into 5 approximately-equal parts, mirroring the
    real bundle layout (parts 0..3 identical size, part 4 remainder)."""
    n = len(data)
    per = (n + 4) // 5  # ceil to keep last part smaller
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
    """Rewrite the frozen manifest constants to match a fresh synthetic
    outer bundle. All tests run against this patched manifest."""
    outer = _build_valid_outer_bundle()
    parts = _split_five(outer)
    exp_parts = tuple((n, s) for n, s, _ in parts)
    exp_sha = hashlib.sha256(outer).hexdigest()
    exp_size = len(outer)
    monkeypatch.setattr(mp_mod, "EXPECTED_PARTS", exp_parts)
    monkeypatch.setattr(mp_mod, "EXPECTED_BUNDLE_TOTAL_SIZE", exp_size)
    monkeypatch.setattr(mp_mod, "EXPECTED_BUNDLE_SHA256", exp_sha)

    # Rebuild the server controller with these constants in effect.
    import server as srv

    srv.multipart_controller = mp_mod.MultipartBundleController(
        upload_manager=srv.bundle_manager,
        base_dir=srv.MULTIPART_WORK_DIR,
    )
    yield {
        "outer": outer,
        "parts": parts,
        "expected_sha": exp_sha,
        "expected_size": exp_size,
    }


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
# Helpers
# ---------------------------------------------------------------------------


def _init(client, parts):
    return client.post(
        "/api/bundles/multipart/init",
        json={"parts": [{"name": n, "size": s} for n, s, _ in parts]},
    )


def _upload_part(client, slot, blob, *, chunk_size=None):
    upload_id = slot["upload_id"]
    total = slot["expected_size"]
    if chunk_size is None:
        chunk_size = slot.get("chunk_size") or total
    for offset in range(0, total, chunk_size):
        idx = offset // chunk_size
        piece = blob[offset:offset + chunk_size]
        r = client.post(
            f"/api/bundles/{upload_id}/chunk/{idx}", content=piece
        )
        assert r.status_code == 200, r.text


def _full_upload(client, mp_session, parts):
    for slot, (_, _, blob) in zip(mp_session["slots"], parts):
        _upload_part(client, slot, blob)


# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


class TestManifest:
    def test_publish_manifest(self, patched_manifest, client):
        r = client.get("/api/bundles/multipart/manifest").json()
        assert r["bundle_name"] == mp_mod.EXPECTED_BUNDLE_NAME
        assert r["bundle_total_size"] == patched_manifest["expected_size"]
        assert r["bundle_sha256"] == patched_manifest["expected_sha"]
        assert [p["name"] for p in r["parts"]] == [
            n for n, _ in mp_mod.EXPECTED_PARTS
        ]


# ---------------------------------------------------------------------------
# Init-time validation
# ---------------------------------------------------------------------------


class TestInit:
    def test_valid_init(self, patched_manifest, client):
        r = _init(client, patched_manifest["parts"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["slots"]) == 5
        assert body["expected_sha256"] == patched_manifest["expected_sha"]

    def test_wrong_count_rejected(self, patched_manifest, client):
        parts = patched_manifest["parts"][:4]
        r = _init(client, parts)
        assert r.status_code == 400
        assert "expected 5 parts" in r.text

    def test_extra_part_rejected(self, patched_manifest, client):
        parts = list(patched_manifest["parts"])
        parts.append(("OLD36_REFERENCE_BUNDLE.zip.part-05", 10, b"x" * 10))
        r = _init(client, parts)
        assert r.status_code == 400

    def test_wrong_name_rejected(self, patched_manifest, client):
        parts = list(patched_manifest["parts"])
        n0, s0, b0 = parts[0]
        parts[0] = ("wrong_name.part-00", s0, b0)
        r = _init(client, parts)
        assert r.status_code == 400
        assert "expected part name" in r.text

    def test_wrong_size_rejected(self, patched_manifest, client):
        parts = list(patched_manifest["parts"])
        n0, s0, b0 = parts[0]
        parts[0] = (n0, s0 + 1, b0)
        r = _init(client, parts)
        assert r.status_code == 400
        assert "expected size" in r.text

    def test_duplicate_part_name_rejected(self, patched_manifest, client):
        parts = list(patched_manifest["parts"])
        parts[1] = parts[0]  # duplicate name in declared list
        r = _init(client, parts)
        assert r.status_code == 400
        assert "duplicate part name" in r.text

    def test_wrong_ordering_rejected(self, patched_manifest, client):
        parts = list(patched_manifest["parts"])
        parts[0], parts[1] = parts[1], parts[0]
        r = _init(client, parts)
        assert r.status_code == 400
        assert "expected part name" in r.text


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


class TestReconstruction:
    def test_valid_5_part_reconstruction_all_11_sessions(
        self, patched_manifest, client
    ):
        cp0 = client.get("/api/checkpoints").json()["checkpoints"]
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["results"]["ok"] == 11
        assert body["results"]["failed"] == 0
        assert body["multipart_reassembly"]["bundle_sha256"] == patched_manifest["expected_sha"]
        # OLD36 raw availability 11/11.
        ref = client.get("/api/reference/old36").json()
        assert ref["present_sessions"] == 11
        assert ref["milestone_impact_hours"] == 0.0
        # Milestones untouched.
        cp1 = client.get("/api/checkpoints").json()["checkpoints"]
        for k in (CHECKPOINT_OLD36, CHECKPOINT_NEW12, CHECKPOINT_NEW36,
                  CHECKPOINT_TOTAL48, CHECKPOINT_TOTAL72):
            assert cp1[k] == cp0[k]

    def test_missing_part_blocks_assemble(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        # Upload only 4 of 5 parts.
        for slot, (_, _, blob) in zip(mp["slots"][:-1], patched_manifest["parts"][:-1]):
            _upload_part(client, slot, blob)
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 400
        assert "chunk(s) missing" in r.text

    def test_corrupt_part_causes_sha_mismatch(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        parts = list(patched_manifest["parts"])
        # Flip one byte in the middle of part 2, keeping the size intact.
        name, size, blob = parts[2]
        blob = bytearray(blob)
        blob[len(blob) // 2] ^= 0xFF
        parts[2] = (name, size, bytes(blob))
        for slot, (_, _, b) in zip(mp["slots"], parts):
            _upload_part(client, slot, b)
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 400
        assert "SHA256 mismatch" in r.text

    def test_retry_after_completed_reconstruction_idempotent(
        self, patched_manifest, client, tmp_path, monkeypatch
    ):
        import server as srv

        raw_dir = tmp_path / "raw_zips"
        raw_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(srv, "RAW_DIR", raw_dir)

        # First run.
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        first = sorted(p.name for p in raw_dir.glob("*.zip"))
        assert len(first) == 11

        # Second run \u2014 identical parts.
        mp2 = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp2, patched_manifest["parts"])
        r = client.post(f"/api/bundles/multipart/{mp2['multipart_id']}/assemble")
        for e in r.json()["results"]["sessions"]:
            assert e["result"]["duplicate_status"] == "EXACT_DUPLICATE"
            assert e["result"]["validated_hours"] == 0.0
        second = sorted(p.name for p in raw_dir.glob("*.zip"))
        assert first == second


class TestInterruptedResume:
    def test_missing_part_then_full_resume_via_new_session(
        self, patched_manifest, client
    ):
        """Interrupted upload (one part missing) blocks assemble;
        aborting + re-initing + sending all parts again succeeds. This
        mirrors the browser-side resume workflow."""
        mp = _init(client, patched_manifest["parts"]).json()
        # Fully upload parts 0,1,2,4. Skip part 3 entirely.
        for idx, (slot, (_, _, blob)) in enumerate(
            zip(mp["slots"], patched_manifest["parts"])
        ):
            if idx == 3:
                continue
            _upload_part(client, slot, blob)
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 400
        assert "chunk(s) missing" in r.text
        # Abort and re-init; upload all parts fully.
        client.delete(f"/api/bundles/multipart/{mp['multipart_id']}")
        mp2 = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp2, patched_manifest["parts"])
        r = client.post(f"/api/bundles/multipart/{mp2['multipart_id']}/assemble")
        assert r.status_code == 200, r.text
        assert r.json()["results"]["ok"] == 11


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_no_multipart_workdir_after_success(self, patched_manifest, client):
        import server as srv

        mp = _init(client, patched_manifest["parts"]).json()
        wd = Path(srv.MULTIPART_WORK_DIR) / mp["multipart_id"]
        _full_upload(client, mp, patched_manifest["parts"])
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 200
        assert not wd.exists(), f"multipart workdir must be wiped: {wd}"

    def test_no_part_files_left_after_success(self, patched_manifest, client):
        import server as srv

        # Snapshot .part files before.
        before = set(Path(srv.BUNDLE_UPLOAD_DIR).glob("*.part"))
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        after = set(Path(srv.BUNDLE_UPLOAD_DIR).glob("*.part"))
        assert after == before, "part .part files must be unlinked"

    def test_abort_clears_multipart(self, patched_manifest, client):
        import server as srv

        mp = _init(client, patched_manifest["parts"]).json()
        # Upload one part partially.
        _upload_part(
            client, mp["slots"][0], patched_manifest["parts"][0][2]
        )
        client.delete(f"/api/bundles/multipart/{mp['multipart_id']}")
        # No part files, no workdir.
        wd = Path(srv.MULTIPART_WORK_DIR) / mp["multipart_id"]
        assert not wd.exists()

    def test_sha_mismatch_deletes_reassembled_tmp(self, patched_manifest, client):
        import server as srv

        mp = _init(client, patched_manifest["parts"]).json()
        parts = list(patched_manifest["parts"])
        name, size, blob = parts[0]
        parts[0] = (name, size, bytes(bytearray(blob) or b"x") if False else b"\x00" * size)
        for slot, (_, _, b) in zip(mp["slots"], parts):
            _upload_part(client, slot, b)
        r = client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        assert r.status_code == 400
        # Reassembled tmp must be gone.
        wd = Path(srv.MULTIPART_WORK_DIR) / mp["multipart_id"]
        reassembled = wd / (f"{mp_mod.EXPECTED_BUNDLE_NAME}.part-reassembled")
        assert not reassembled.exists()


# ---------------------------------------------------------------------------
# Global invariants
# ---------------------------------------------------------------------------


class TestInvariantsAfterMultipart:
    def test_firewall_intact(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        for sid in NEW36_SESSION_IDS:
            with pytest.raises(NEW36QuantitativeFirewallError):
                assert_recovery_allowed(sid)

    def test_engine_not_configured(self, patched_manifest, client):
        mp = _init(client, patched_manifest["parts"]).json()
        _full_upload(client, mp, patched_manifest["parts"])
        client.post(f"/api/bundles/multipart/{mp['multipart_id']}/assemble")
        s = current_status()
        assert s.status == "NOT_CONFIGURED"
        assert s.accepts_input is False

    def test_single_session_upload_still_works(self, patched_manifest, client):
        sid = "20260101T000000Z_smoke"
        zbytes = _inner_zip_bytes(sid, 3.0)
        r = client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        assert r.status_code == 200
        assert r.json()["results"][0]["verdict"] in ("PASS", "PASS_WITH_WARNING")

    def test_single_bundle_upload_still_works(self, patched_manifest, client):
        # Direct single-bundle path (non-multipart) unchanged.
        outer = patched_manifest["outer"]
        init = client.post(
            "/api/bundles/init",
            json={"filename": "OLD36_REFERENCE_BUNDLE.zip", "total_size": len(outer)},
        )
        uid = init.json()["upload_id"]
        chunk_size = init.json()["chunk_size"]
        for i in range(0, len(outer), chunk_size):
            idx = i // chunk_size
            client.post(
                f"/api/bundles/{uid}/chunk/{idx}", content=outer[i:i + chunk_size]
            )
        r = client.post(f"/api/bundles/{uid}/complete", json={})
        assert r.status_code == 200
        assert r.json()["results"]["ok"] == 11
