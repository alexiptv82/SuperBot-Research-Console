"""Real-schema parser regression tests.

Uses inline synthetic copies of the three JSON artifacts observed in the
first real MultiVenue 3H session ZIP (v20260911, session
``20260911T040213Z_43a798b7``). These tests prove that:

1. The parser reads manifest.json + runtime_status.json +
   fast_collection_summary.json and merges them.
2. The three critical fields (exit_code, watchdog, collector_sha256)
   resolve from ``fast_collection_summary.json``.
3. Runtime derivations produce correct aggregates.
4. Missing any critical field still yields UNRESOLVED (no fabrication).
"""
from __future__ import annotations

import io
import json
import zipfile

from manifest_parser import (
    CRITICAL_FIELDS,
    parse_manifest_from_source,
)
from qa_engine import run_qa
from zip_security import inspect_zip

_MANIFEST = {
    "collector": "Bitget_MultiVenue_Microstructure_Collector_V2",
    "session_id": "20260911T040213Z_43a798b7",
    "created_utc": "2026-09-11T04:02:13.082860+00:00",
    "run_minutes": 180.0,
    "sample_ms": 100,
    "venues": ["bitget", "binance", "okx", "bybit"],
}

_RUNTIME = {
    "updated_utc": "2026-09-11T07:02:18.179066+00:00",
    "session_id": "20260911T040213Z_43a798b7",
    "counters": {
        "errors": {"bitget": 1, "binance_public": 0, "okx": 0, "bybit": 1},
        "reconnects": {"bitget": 1, "binance_public": 0, "okx": 0, "bybit": 1},
        "unknown_side": {"bitget": 0, "binance": 0, "okx": 0, "bybit": 0},
        "sampler_missed_ticks_total": 2,
        "sampler_lag_events_gt50ms": 16,
        "writer_queue_max": 7,
        "writer_errors": 0,
    },
    "buffers": {"grid": 0, "book": 0, "trade": 0, "logs": 0},
    "parts": {"grid": 360, "book": 360, "trade": 360},
}

_SUMMARY = {
    "session_id": "20260911T040213Z_43a798b7",
    "collector_sha256": "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3",
    "target_hours": 3.0,
    "wrapper_elapsed_hours": 3.0018990085522335,
    "exit_code": 0,
    "watchdog_triggered": False,
    "sync_grid_100ms_files": 360,
    "normalized_books_files": 360,
    "normalized_trades_files": 360,
    "logs_files": 1,
}

PAR1 = b"PAR1"
MINI_PARQUET = PAR1 + b"x" * 32 + PAR1


def _build_real_shape_zip(
    include_manifest=True,
    include_runtime=True,
    include_summary=True,
    override_summary=None,
) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        if include_manifest:
            zf.writestr("manifest.json", json.dumps(_MANIFEST))
        if include_runtime:
            zf.writestr("runtime_status.json", json.dumps(_RUNTIME))
        if include_summary:
            summary = dict(_SUMMARY)
            if override_summary:
                summary.update(override_summary)
            zf.writestr("fast_collection_summary.json", json.dumps(summary))
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            for i in range(2):
                zf.writestr(f"{d}/{d}_20260911T040213Z_43a798b7_{i:07d}.parquet", MINI_PARQUET)
    return out.getvalue()


def test_real_schema_all_three_files_present_yields_pass():
    z = _build_real_shape_zip()
    r = run_qa(z, "real.zip")
    assert r.verdict == "PASS", (r.failure_reasons, r.missing_fields)
    assert r.session_id == "20260911T040213Z_43a798b7"
    assert r.fields["collector_sha256"] == "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3"
    assert r.fields["exit_code"] == 0
    assert r.fields["watchdog"] is False
    assert r.fields["writer_errors"] == 0
    assert r.fields["missed_ticks"] == 2
    assert r.fields["lag_gt_50ms"] == 16
    assert r.fields["reconnect_count"] == 2
    assert r.fields["websocket_errors"] == 2
    # unknown_side_count is only recorded when >0 (0 case: absent from fields).
    assert r.fields.get("unknown_side_count", 0) == 0
    assert r.fields["final_buffer_status"] == "CLEAN"
    # duration_hours prefers wrapper_elapsed_hours
    assert abs(r.fields["duration_hours"] - 3.0018990085522335) < 1e-9


def test_real_schema_reconnects_without_recovered_do_not_fail():
    z = _build_real_shape_zip()
    r = run_qa(z, "real.zip")
    # Two reconnect entries exist but recovered is unknown (None), so per
    # \u00a712.6 they must not trigger FAIL. verdict stays PASS.
    assert r.verdict == "PASS"
    rs = r.fields.get("reconnect_summary")
    assert isinstance(rs, list) and len(rs) == 2
    for e in rs:
        assert e.get("recovered") is None


def test_missing_summary_leaves_critical_fields_unresolved():
    z = _build_real_shape_zip(include_summary=False)
    r = run_qa(z, "no_summary.zip")
    assert r.verdict == "UNRESOLVED"
    for critical in CRITICAL_FIELDS:
        assert critical in r.missing_fields, critical


def test_wrong_collector_sha_in_summary_fails():
    z = _build_real_shape_zip(override_summary={"collector_sha256": "b" * 64})
    r = run_qa(z, "wrong.zip")
    assert r.verdict == "FAIL"
    assert any("Collector SHA256 mismatch" in x for x in r.failure_reasons)


def test_watchdog_true_in_summary_fails():
    z = _build_real_shape_zip(override_summary={"watchdog_triggered": True})
    r = run_qa(z, "wd.zip")
    assert r.verdict == "FAIL"
    assert any("watchdog" in x for x in r.failure_reasons)


def test_nonzero_exit_code_in_summary_fails():
    z = _build_real_shape_zip(override_summary={"exit_code": 137})
    r = run_qa(z, "exit.zip")
    assert r.verdict == "FAIL"
    assert any("exit_code=137" in x for x in r.failure_reasons)


def test_writer_errors_in_runtime_fails():
    z_out = io.BytesIO()
    runtime = dict(_RUNTIME)
    runtime["counters"] = dict(_RUNTIME["counters"], writer_errors=3)
    with zipfile.ZipFile(z_out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(_MANIFEST))
        zf.writestr("runtime_status.json", json.dumps(runtime))
        zf.writestr("fast_collection_summary.json", json.dumps(_SUMMARY))
        for d in ("sync_grid_100ms", "normalized_books", "normalized_trades"):
            zf.writestr(f"{d}/part-000.parquet", MINI_PARQUET)
    r = run_qa(z_out.getvalue(), "we.zip")
    assert r.verdict == "FAIL"
    assert any("writer_errors=3" in x for x in r.failure_reasons)


def test_parser_reports_entries_found():
    z = _build_real_shape_zip()
    insp = inspect_zip(z)
    mr = parse_manifest_from_source(z, insp)
    assert set(mr.entries_found) == {
        "manifest.json",
        "runtime_status.json",
        "fast_collection_summary.json",
    }
    # Derivations should have been recorded
    assert "reconnect_summary" in mr.derivations
    assert "final_buffer_status" in mr.derivations
