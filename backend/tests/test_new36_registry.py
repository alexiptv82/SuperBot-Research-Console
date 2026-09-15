"""Tests for the frozen NEW36 registry + milestone accounting."""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from checkpoint_registry import (
    NEW12_SESSION_IDS,
    NEW36_SESSION_IDS,
    OLD36_VALIDATED_HOURS,
    SESSION_NOMINAL_HOURS,
    checkpoint_batch_for,
    is_new12_registered,
    is_new36_registered,
    new36_index,
)
from constants import (
    CHECKPOINT_NEW12,
    CHECKPOINT_NEW36,
    CHECKPOINT_OLD36,
    CHECKPOINT_TOTAL48,
    CHECKPOINT_TOTAL72,
    FROZEN_COLLECTOR_SHA256,
    PARQUET_MAGIC,
)
from database import SessionLocal, engine
from models import Session as SessionModel, QARun


class TestRegistryConstants:
    def test_new36_is_exactly_12_unique(self):
        assert len(NEW36_SESSION_IDS) == 12
        assert len(set(NEW36_SESSION_IDS)) == 12

    def test_new12_is_first_four_of_new36(self):
        assert NEW12_SESSION_IDS == NEW36_SESSION_IDS[:4]

    def test_old36_baseline_is_36(self):
        assert OLD36_VALIDATED_HOURS == 36.0

    def test_nominal_session_hours_is_3(self):
        assert SESSION_NOMINAL_HOURS == 3.0

    @pytest.mark.parametrize("sid", NEW36_SESSION_IDS)
    def test_membership_helpers_registered(self, sid: str):
        assert is_new36_registered(sid)
        assert checkpoint_batch_for(sid) == CHECKPOINT_NEW36
        assert new36_index(sid) is not None

    def test_new12_helpers_only_for_first_four(self):
        for sid in NEW12_SESSION_IDS:
            assert is_new12_registered(sid)
        for sid in NEW36_SESSION_IDS[4:]:
            assert not is_new12_registered(sid)

    def test_unknown_session_is_unassigned(self):
        assert not is_new36_registered("20991231T000000Z_deadbeef")
        assert not is_new12_registered("20991231T000000Z_deadbeef")
        assert checkpoint_batch_for("20991231T000000Z_deadbeef") == "UNASSIGNED"
        assert checkpoint_batch_for(None) == "UNASSIGNED"

    def test_no_filename_or_timestamp_inference(self):
        """Explicit non-heuristic guarantee: names that LOOK like they
        belong to NEW36 but are not in the frozen list must be
        UNASSIGNED. Timestamps and filename patterns must not steer
        checkpoint assignment.
        """
        assert checkpoint_batch_for("20260910T123759Z_wrongsuffix") == "UNASSIGNED"
        assert checkpoint_batch_for("NEW36_looks_like_it_but_not_registered") == "UNASSIGNED"


# ---------------------------------------------------------------------------
# End-to-end: uploading a mini registered session credits nominal 3.0h,
# not the manifest's wrapper_elapsed_hours.
# ---------------------------------------------------------------------------


MINI_PARQUET = PARQUET_MAGIC + b"x" * 32 + PARQUET_MAGIC


def _build_mini_zip(session_id: str, duration_hours: float | None = 3.001899) -> bytes:
    """Tiny synthetic ZIP that passes deterministic QA on the mini-magic
    parquet path. Only used inside pytest against the /tmp DB."""
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
        }
        if duration_hours is not None:
            manifest["duration_hours"] = duration_hours
        zf.writestr("manifest.json", json.dumps(manifest))
        # We only need pyarrow-valid parquet bytes to satisfy the new
        # all-files validator. Build the smallest possible valid file.
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            tbl = pa.table({"x": pa.array([1], type=pa.int64())})
            sink = io.BytesIO()
            pq.write_table(tbl, sink)
            valid_parquet = sink.getvalue()
        except Exception:
            valid_parquet = MINI_PARQUET
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            for i in range(2):
                zf.writestr(f"{d}/part-{i:03d}.parquet", valid_parquet)
    return buf.getvalue()


@pytest.fixture()
def client():
    from server import app

    c = TestClient(app)
    r = c.post("/api/auth/login", json={"password": "test-pw-123"})
    assert r.status_code == 200
    return c


@pytest.fixture(autouse=True)
def _clean_db():
    """Empty the sessions/qa_runs/audit tables between tests in this module."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM raw_files"))
        conn.execute(text("UPDATE sessions SET current_qa_run_id = NULL"))
        conn.execute(text("DELETE FROM qa_runs"))
        conn.execute(text("DELETE FROM sessions"))
        conn.execute(text("DELETE FROM audit_log"))
    yield


class TestAutoCheckpointAssignment:
    def test_registered_session_is_auto_tagged_new36(self, client):
        sid = NEW36_SESSION_IDS[5]  # some registered id
        zbytes = _build_mini_zip(sid)
        r = client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["verdict"] in ("PASS", "PASS_WITH_WARNING")

        r = client.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["checkpoint_hint"] == CHECKPOINT_NEW36

    def test_unregistered_session_is_unassigned(self, client):
        sid = "20301231T000000Z_notregistered"
        zbytes = _build_mini_zip(sid)
        r = client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        assert r.status_code == 200
        r = client.get(f"/api/sessions/{sid}")
        assert r.json()["checkpoint_hint"] == "UNASSIGNED"

    def test_filename_containing_new36_does_not_trigger(self, client):
        """Historical bug: filename-based inference used to auto-tag any
        ZIP whose name contained 'new36'. That heuristic is now removed:
        only registered IDs count.
        """
        sid = "20301231T000000Z_new36ish"
        zbytes = _build_mini_zip(sid)
        r = client.post(
            "/api/sessions/upload",
            files={"files": (f"new36_looking_but_fake_{sid}.zip", zbytes, "application/zip")},
        )
        assert r.status_code == 200
        r = client.get(f"/api/sessions/{sid}")
        assert r.json()["checkpoint_hint"] == "UNASSIGNED"


class TestMilestoneMath:
    def test_baseline_only(self, client):
        r = client.get("/api/checkpoints")
        assert r.status_code == 200
        cp = r.json()["checkpoints"]
        assert cp[CHECKPOINT_OLD36]["hours"] == 36.0
        assert cp[CHECKPOINT_NEW12]["hours"] == 0.0
        assert cp[CHECKPOINT_NEW36]["hours"] == 0.0
        assert cp[CHECKPOINT_TOTAL48]["hours"] == 36.0
        assert cp[CHECKPOINT_TOTAL72]["hours"] == 36.0

    def test_one_registered_session_credits_nominal_three_hours(self, client):
        # Upload the first NEW12 slot with a wrapper_elapsed_hours of
        # 3.001899. Milestone math must credit 3.0, not 3.001899.
        sid = NEW12_SESSION_IDS[0]
        zbytes = _build_mini_zip(sid, duration_hours=3.001899)
        client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        r = client.get("/api/checkpoints").json()
        cp = r["checkpoints"]
        assert cp[CHECKPOINT_NEW12]["hours"] == 3.0
        assert cp[CHECKPOINT_NEW36]["hours"] == 3.0
        assert cp[CHECKPOINT_TOTAL48]["hours"] == 39.0
        assert cp[CHECKPOINT_TOTAL72]["hours"] == 39.0
        # Telemetry is preserved separately.
        assert cp[CHECKPOINT_NEW12]["telemetry_wrapper_hours"] == pytest.approx(
            3.001899, rel=1e-6
        )

    def test_duplicate_upload_does_not_double_count(self, client):
        sid = NEW12_SESSION_IDS[1]
        zbytes = _build_mini_zip(sid)
        client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        # Second upload of the exact same bytes -> EXACT_DUPLICATE.
        client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        cp = client.get("/api/checkpoints").json()["checkpoints"]
        assert cp[CHECKPOINT_NEW12]["hours"] == 3.0
        assert cp[CHECKPOINT_NEW36]["hours"] == 3.0

    def test_unregistered_session_does_not_count(self, client):
        sid = "20991231T000000Z_ghost"
        zbytes = _build_mini_zip(sid)
        client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", zbytes, "application/zip")},
        )
        cp = client.get("/api/checkpoints").json()["checkpoints"]
        assert cp[CHECKPOINT_NEW12]["hours"] == 0.0
        assert cp[CHECKPOINT_NEW36]["hours"] == 0.0

    def test_full_new12_fills_nominal_12h(self, client):
        for sid in NEW12_SESSION_IDS:
            zbytes = _build_mini_zip(sid)
            client.post(
                "/api/sessions/upload",
                files={"files": (f"{sid}.zip", zbytes, "application/zip")},
            )
        cp = client.get("/api/checkpoints").json()["checkpoints"]
        assert cp[CHECKPOINT_NEW12]["hours"] == 12.0
        assert cp[CHECKPOINT_TOTAL48]["hours"] == 48.0

    def test_fail_verdict_slot_is_not_valid_and_cannot_be_substituted(self, client):
        """A failed slot must NOT be silently substituted by a later session."""
        sid_fail = NEW12_SESSION_IDS[2]
        # Force a FAIL by using a non-frozen collector sha.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "session_id": sid_fail,
                "exit_code": 0,
                "watchdog": False,
                "writer_errors": 0,
                "reconnect_summary": [],
                # Wrong collector -> hard FAIL.
                "collector_sha256": "0" * 64,
                "start_time": "2026-09-10T12:00:00Z",
                "end_time": "2026-09-10T15:00:00Z",
                "duration_hours": 3.0,
            }
            zf.writestr("manifest.json", json.dumps(manifest))
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
                tbl = pa.table({"x": pa.array([1], type=pa.int64())})
                sink = io.BytesIO()
                pq.write_table(tbl, sink)
                pbytes = sink.getvalue()
            except Exception:
                pbytes = MINI_PARQUET
            for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
                zf.writestr(f"{d}/part-000.parquet", pbytes)
        client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid_fail}.zip", buf.getvalue(), "application/zip")},
        )
        # Upload a valid session that is NOT in NEW12: it must not
        # substitute for the failed NEW12 slot.
        sid_extra = NEW36_SESSION_IDS[7]
        client.post(
            "/api/sessions/upload",
            files={
                "files": (
                    f"{sid_extra}.zip",
                    _build_mini_zip(sid_extra),
                    "application/zip",
                )
            },
        )
        cp = client.get("/api/checkpoints").json()["checkpoints"]
        # NEW12 still 0 (only the failed slot has been attempted).
        assert cp[CHECKPOINT_NEW12]["hours"] == 0.0
        # NEW36 gets the extra valid slot (position 8).
        assert cp[CHECKPOINT_NEW36]["hours"] == 3.0
