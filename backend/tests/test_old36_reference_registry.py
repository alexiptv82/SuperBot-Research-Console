"""OLD36_REFERENCE registry invariants + import-mode behavior."""
from __future__ import annotations

import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from checkpoint_registry import (
    CHECKPOINT_OLD36_REFERENCE,
    NEW36_SESSION_IDS,
    OLD36_REFERENCE_NOMINAL_HOURS,
    OLD36_REFERENCE_SESSIONS,
    OLD36_VALIDATED_HOURS,
    checkpoint_batch_for,
    is_old36_reference,
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

from fixtures import VALID_PARQUET


class TestOld36Registry:
    def test_size_and_uniqueness(self):
        assert len(OLD36_REFERENCE_SESSIONS) == 11
        assert len(set(OLD36_REFERENCE_SESSIONS)) == 11

    def test_nominal_hours_sum_to_36(self):
        total = sum(OLD36_REFERENCE_NOMINAL_HOURS[s] for s in OLD36_REFERENCE_SESSIONS)
        assert total == OLD36_VALIDATED_HOURS == 36.0

    def test_disjoint_from_new36(self):
        assert set(OLD36_REFERENCE_SESSIONS).isdisjoint(set(NEW36_SESSION_IDS))

    def test_labels(self):
        for sid in OLD36_REFERENCE_SESSIONS:
            assert is_old36_reference(sid)
            assert checkpoint_batch_for(sid) == CHECKPOINT_OLD36_REFERENCE
        for sid in NEW36_SESSION_IDS:
            assert not is_old36_reference(sid)
            assert checkpoint_batch_for(sid) == CHECKPOINT_NEW36
        assert checkpoint_batch_for("20301231T000000Z_unknown") == "UNASSIGNED"
        assert checkpoint_batch_for(None) == "UNASSIGNED"


def _mini_zip(session_id: str) -> bytes:
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
            "duration_hours": 6.0,
        }
        zf.writestr("manifest.json", json.dumps(manifest))
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            for i in range(2):
                zf.writestr(f"{d}/part-{i:03d}.parquet", VALID_PARQUET)
    return buf.getvalue()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from server import app
    import server as srv

    monkeypatch.setattr(srv, "RAW_DIR", tmp_path)
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


class TestHistoricalImportMode:
    def test_import_auto_tags_old36_reference(self, client):
        sid = OLD36_REFERENCE_SESSIONS[0]  # 6h session
        r = client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", _mini_zip(sid), "application/zip")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["verdict"] in ("PASS", "PASS_WITH_WARNING")

        r = client.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["checkpoint_hint"] == CHECKPOINT_OLD36_REFERENCE

    def test_import_does_not_shift_milestone_totals(self, client):
        cp0 = client.get("/api/checkpoints").json()["checkpoints"]
        for sid in OLD36_REFERENCE_SESSIONS[:3]:
            client.post(
                "/api/sessions/upload",
                files={"files": (f"{sid}.zip", _mini_zip(sid), "application/zip")},
            )
        cp1 = client.get("/api/checkpoints").json()["checkpoints"]
        assert cp1[CHECKPOINT_OLD36] == cp0[CHECKPOINT_OLD36]
        assert cp1[CHECKPOINT_NEW12] == cp0[CHECKPOINT_NEW12]
        assert cp1[CHECKPOINT_NEW36] == cp0[CHECKPOINT_NEW36]
        assert cp1[CHECKPOINT_TOTAL48] == cp0[CHECKPOINT_TOTAL48]
        assert cp1[CHECKPOINT_TOTAL72] == cp0[CHECKPOINT_TOTAL72]

    def test_old36_reference_section_reports_availability(self, client):
        # Import 2 of the 11 historical sessions.
        for sid in OLD36_REFERENCE_SESSIONS[:2]:
            client.post(
                "/api/sessions/upload",
                files={"files": (f"{sid}.zip", _mini_zip(sid), "application/zip")},
            )
        ref = client.get("/api/reference/old36").json()
        assert ref["expected_sessions"] == 11
        assert ref["present_sessions"] == 2
        assert ref["milestone_impact_hours"] == 0.0
        expected_hours = (
            OLD36_REFERENCE_NOMINAL_HOURS[OLD36_REFERENCE_SESSIONS[0]]
            + OLD36_REFERENCE_NOMINAL_HOURS[OLD36_REFERENCE_SESSIONS[1]]
        )
        assert ref["present_nominal_hours"] == expected_hours

    def test_unknown_historical_id_stays_unassigned(self, client):
        sid = "20260901T000000Z_ghost"
        client.post(
            "/api/sessions/upload",
            files={"files": (f"{sid}.zip", _mini_zip(sid), "application/zip")},
        )
        r = client.get(f"/api/sessions/{sid}").json()
        assert r["checkpoint_hint"] == "UNASSIGNED"
