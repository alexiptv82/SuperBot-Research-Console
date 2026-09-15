"""Pytest scenarios for §14.2 of the SuperBot handoff.

Synthetic fixtures only. Executes in under a second.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SUPERBOT_DB_PATH", "/tmp/superbot-test.db")
os.environ.setdefault("SUPERBOT_PASSWORD", "unit-test-pw")
os.environ.setdefault("SUPERBOT_SESSION_SECRET", "unit-test-secret-32-chars-minimum")
os.environ.setdefault("SUPERBOT_DATA_DIR", "/tmp/superbot-test-data")

from qa_engine import run_qa  # noqa: E402
from constants import (  # noqa: E402
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_PASS_WITH_WARNING,
    VERDICT_UNRESOLVED,
)
from fixtures import BuildOptions, build_zip  # noqa: E402


def test_valid_session_passes():
    data = build_zip(BuildOptions())
    r = run_qa(data, "valid.zip")
    assert r.verdict == VERDICT_PASS, r.failure_reasons
    assert r.session_id == "20260910T123759Z_abc"


def test_corrupted_zip_fails():
    data = build_zip(BuildOptions(corrupt_zip_bytes=True))
    r = run_qa(data, "corrupt.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("CRC" in x or "Bad ZIP" in x for x in r.failure_reasons)


def test_wrong_collector_hash_fails():
    data = build_zip(BuildOptions(collector_sha256="a" * 64))
    r = run_qa(data, "wrong_hash.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("Collector SHA256 mismatch" in x for x in r.failure_reasons)


def test_missing_collector_is_unresolved():
    data = build_zip(BuildOptions(collector_sha256=None))
    r = run_qa(data, "no_hash.zip")
    assert r.verdict == VERDICT_UNRESOLVED
    assert "collector_sha256" in r.missing_fields


def test_exact_duplicate_file_same_hash():
    data = build_zip(BuildOptions())
    r1 = run_qa(data, "a.zip")
    r2 = run_qa(data, "b.zip")
    # Same content => same SHA256
    assert r1.file_sha256 == r2.file_sha256


def test_same_session_id_different_file_hash():
    data1 = build_zip(BuildOptions(exit_code=0))
    data2 = build_zip(BuildOptions(exit_code=0, writer_errors=1))  # different manifest
    r1 = run_qa(data1, "a.zip")
    r2 = run_qa(data2, "b.zip")
    assert r1.session_id == r2.session_id
    assert r1.file_sha256 != r2.file_sha256


def test_missing_parquet_part_fails():
    data = build_zip(BuildOptions(missing_dir="normalized_trades"))
    r = run_qa(data, "missing.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("normalized_trades" in x for x in r.failure_reasons)


def test_duplicate_parquet_part_fails():
    data = build_zip(BuildOptions(duplicate_part=True))
    r = run_qa(data, "dupe.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("duplicate parts inside ZIP" in x for x in r.failure_reasons)


def test_corrupted_parquet_metadata_fails():
    data = build_zip(BuildOptions(corrupt_parquet=True))
    r = run_qa(data, "badpq.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("parquet magic missing" in x for x in r.failure_reasons)


def test_watchdog_true_fails():
    data = build_zip(BuildOptions(watchdog=True))
    r = run_qa(data, "wd.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("watchdog" in x for x in r.failure_reasons)


def test_nonzero_exit_code_fails():
    data = build_zip(BuildOptions(exit_code=137))
    r = run_qa(data, "exit.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("exit_code=137" in x for x in r.failure_reasons)


def test_writer_error_fails():
    data = build_zip(BuildOptions(writer_errors=3))
    r = run_qa(data, "we.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("writer_errors=3" in x for x in r.failure_reasons)


def test_recovered_reconnect_is_warning_not_fail():
    data = build_zip(
        BuildOptions(reconnect_summary=[{"venue": "bitget", "count": 2, "recovered": True}])
    )
    r = run_qa(data, "rr.zip")
    assert r.verdict == VERDICT_PASS_WITH_WARNING
    assert any("recovered reconnect" in w for w in r.warnings)


def test_unresolved_reconnect_fails():
    data = build_zip(
        BuildOptions(reconnect_summary=[{"venue": "binance", "count": 1, "recovered": False}])
    )
    r = run_qa(data, "ur.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("unrecovered reconnect" in x for x in r.failure_reasons)


def test_zip_slip_path_rejected():
    data = build_zip(BuildOptions(zip_slip=True))
    r = run_qa(data, "slip.zip")
    assert r.verdict == VERDICT_FAIL
    assert any("zip-slip" in x.lower() or "unsafe" in x.lower() for x in r.failure_reasons)


def test_unknown_sides_present_produces_warning():
    data = build_zip(BuildOptions(unknown_sides=5))
    r = run_qa(data, "us.zip")
    assert r.verdict == VERDICT_PASS_WITH_WARNING
    assert any("unknown_side_count=5" in w for w in r.warnings)
