"""Focused deterministic tests for the RECONSTRUCTION_V1.1 "FINAL
HARNESS AUDIT FIX" (Message 222).

Covers the 6 fixes requested:
  ISSUE 1  session_audit_*.csv exact METADATA_ONLY allowlist
  ISSUE 2  wide-row quantitative-instance expansion reporting
  ISSUE 3  packed "A/B" positive_blocks parser, scoped to ONE file
  ISSUE 4  SKIPPED -> FAILED (STRUCTURAL_INVALID /
           EXPECTED_METRIC_MISSING / DISPERSION_STATE_MISSING),
           PENDING reserved for "raw not imported yet" only
  ISSUE 5  GOLDEN_SOURCE_HASH_MISMATCH preflight, pinned 24 golden
           CSV hashes, runs before anything else in run_regression()
  ISSUE 6  stray git bundles removed (verified by a filesystem check,
           not a regression test — see the deployment/manifest step)

ALL fixtures are synthetic or read ONLY the golden CP24/CP36 CSVs
(never a raw OLD36 ZIP, never NEW36 data). The
TestRunRegressionInstanceAccounting class calls
``recovery.harness.run_regression()`` with the runtime DB cleared of
OLD36 rows — this is IDENTICAL to the existing, already-accepted
``tests/test_recovery_sandbox_firewall.py`` pattern. With the DB
empty, ``raw_ready`` is always False, so run_regression() NEVER
reproduces a single candidate value and NEVER performs any numeric
comparison against an OLD36 golden output — it only exercises the
PENDING/METADATA_ONLY/instance-accounting bookkeeping this audit fix
targets. The OLD36 golden regression (comparing reconstructed values
against real golden numeric outputs) is NOT executed anywhere in this
file, per explicit instruction.
"""
from __future__ import annotations

import csv
import os
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("SUPERBOT_ENV", "test")

from recovery import validation as _val
from recovery import GOLDENS_DIR
from recovery.engine import BlockMetrics, BlockSummary
from recovery.goldens import list_cp24_csvs, list_cp36_csvs
from recovery.harness import (
    DISPERSION_STATE_MISSING,
    EXPECTED_METRIC_MISSING,
    FAILED,
    METADATA_ONLY_FILES,
    PENDING,
    STRUCTURAL_INVALID,
    GoldenSourceHashMismatchError,
    _block_cache,
    _instances_per_source_row,
    _iter_aggregate_row_views,
    _load_recovered_metadata,
    _verify_golden_source_hashes,
    reproduce_row,
    run_regression,
)

PACKED_FILE = "CP36/selected_q90_30s_comparison_24h_new12_all36.csv"


@pytest.fixture(autouse=True)
def _clean_block_cache():
    _block_cache.clear()
    yield
    _block_cache.clear()


# ===========================================================================
# ISSUE 1 — exact METADATA_ONLY allowlist
# ===========================================================================
class TestT18MetadataOnlyAllowlist:
    def test_exact_three_paths_only(self):
        assert METADATA_ONLY_FILES == frozenset({
            "CP24/session_audit_24h.csv",
            "CP36/session_audit_36h.csv",
            "CP36/session_audit_new12.csv",
        })

    def test_no_other_golden_csv_is_in_the_allowlist(self):
        all_rel = {
            p.relative_to(GOLDENS_DIR).as_posix()
            for p in list_cp24_csvs() + list_cp36_csvs()
        }
        assert METADATA_ONLY_FILES.issubset(all_rel)
        assert len(METADATA_ONLY_FILES) == 3
        # Every other golden CSV (21 of the 24) must NOT be classified
        # metadata-only, even ones with superficially similar names.
        assert "CP24/simple_aggregates_24h.csv" not in METADATA_ONLY_FILES


class TestRunRegressionInstanceAccounting:
    """Calls run_regression() with the DB cleared -> raw_ready=False
    -> zero candidate values reproduced, zero numeric golden
    comparisons performed. Only PENDING/METADATA_ONLY/instance
    bookkeeping is exercised (ISSUES 1 and 2)."""

    @pytest.fixture(autouse=True)
    def _clear_old36_db(self):
        from database import engine as db_engine
        from sqlalchemy import text

        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM raw_files"))
            conn.execute(text("UPDATE sessions SET current_qa_run_id = NULL"))
            conn.execute(text("DELETE FROM qa_runs"))
            conn.execute(text("DELETE FROM sessions"))
        yield

    def _summary_rows(self) -> dict[str, dict]:
        # NOTE: intentionally reads the in-memory ``out["outcomes"]``
        # list rather than re-opening out["summary_path"] from disk —
        # REPORTS_DIR is a fixed shared filesystem path, not
        # per-xdist-worker, so re-reading the file could race against
        # a concurrent run_regression() call from another parallel
        # worker's test overwriting the same path.
        out = run_regression()
        assert out["raw_ready"] is False
        return {r["source_file"]: r for r in out["outcomes"]}

    def test_metadata_only_files_excluded_from_all_counts(self):
        rows = self._summary_rows()
        expected_row_counts = {
            "CP24/session_audit_24h.csv": 7,
            "CP36/session_audit_36h.csv": 11,
            "CP36/session_audit_new12.csv": 4,
        }
        for rel, expected_rows in expected_row_counts.items():
            r = rows[rel]
            assert r["file_kind"] == "METADATA_ONLY"
            assert int(r["total_rows"]) == expected_rows
            assert int(r["total_instances"]) == 0
            assert int(r["matched"]) == 0
            assert int(r["failed"]) == 0
            assert int(r["pending"]) == 0
            assert int(r["skipped_aggregate_scope"]) == 0

    def test_selected_q90_wide_file_expands_11_rows_to_33_instances(self):
        r = self._summary_rows()[PACKED_FILE]
        assert int(r["total_rows"]) == 11
        assert int(r["total_instances"]) == 33
        total = (
            int(r["matched"]) + int(r["failed"])
            + int(r["pending"]) + int(r["skipped_aggregate_scope"])
        )
        assert total == 33

    def test_weekend_vs_weekday_wide_file_expands_to_3x_instances(self):
        r = self._summary_rows()["CP24/weekend_vs_weekday_selected_30s.csv"]
        assert int(r["total_instances"]) == int(r["total_rows"]) * 3
        total = (
            int(r["matched"]) + int(r["failed"])
            + int(r["pending"]) + int(r["skipped_aggregate_scope"])
        )
        assert total == int(r["total_instances"])

    def test_narrow_and_block_level_files_instances_equal_rows(self):
        rows = self._summary_rows()
        for rel in (
            "CP24/simple_aggregates_24h.csv",
            "CP24/simple_q80_q90_q95_block_level_24h.csv",
            "CP36/fair_gap_dispersion_all36.csv",
        ):
            r = rows[rel]
            assert int(r["total_instances"]) == int(r["total_rows"])

    def test_run_regression_produces_the_five_report_files(self):
        out = run_regression()
        for k in (
            "summary_path", "failures_path", "rules_path",
            "provenance_path", "report_path",
        ):
            assert pathlib.Path(out[k]).exists()


# ===========================================================================
# ISSUE 2 (pure helper) — instances-per-row is read from the SAME
# static routing registry used everywhere else, never guessed.
# ===========================================================================
class TestT19InstancesPerSourceRowHelper:
    def test_block_level_is_always_one_instance(self):
        assert _instances_per_source_row(
            "CP24/simple_q80_q90_q95_block_level_24h.csv", True
        ) == 1

    def test_narrow_aggregate_is_one_instance(self):
        assert _instances_per_source_row("CP24/simple_aggregates_24h.csv", False) == 1

    def test_wide_columns_is_three_instances(self):
        assert _instances_per_source_row(
            "CP24/weekend_vs_weekday_selected_30s.csv", False
        ) == 3

    def test_wide_columns_prefixed_is_three_instances(self):
        assert _instances_per_source_row(PACKED_FILE, False) == 3

    def test_unrouted_file_defaults_to_one(self):
        assert _instances_per_source_row("CP99/not_a_real_file.csv", False) == 1


# ===========================================================================
# ISSUE 3 — packed "A/B" positive_blocks parser, scoped to ONE file
# ===========================================================================
class TestT20PackedPositiveBlocksParsing:
    def test_parse_valid_packed_string(self):
        assert _val.parse_packed_positive_blocks("14/14") == (14, 14)
        assert _val.parse_packed_positive_blocks(" 8 / 8 ") == (8, 8)
        assert _val.parse_packed_positive_blocks("0/22") == (0, 22)

    @pytest.mark.parametrize("bad", ["14", "14/14/14", "a/b", "-1/2", "14.5/14", "", None])
    def test_rejects_malformed_values(self, bad):
        with pytest.raises(_val.PackedFieldParseError):
            _val.parse_packed_positive_blocks(bad)

    def test_real_golden_row_is_parsed_into_ints_only_for_the_pinned_file(self):
        golden = {
            "feature": "fair_gap_reversion", "type": "simple",
            "old24_mean_bps": "0.6496771033575105", "old24_positive_blocks": "14/14",
            "new12_mean_bps": "0.6756517239349551", "new12_positive_blocks": "8/8",
            "all36_mean_bps": "0.659933696636439", "all36_positive_blocks": "22/22",
            "all36_N": "5146",
        }
        views = {v[0]: v[4] for v in _iter_aggregate_row_views(PACKED_FILE, golden)}
        assert set(views) == {"OLD24", "NEW12", "ALL36"}
        assert views["OLD24"]["positive_blocks"] == 14
        assert views["OLD24"]["total_blocks"] == 14
        assert views["NEW12"]["positive_blocks"] == 8
        assert views["NEW12"]["total_blocks"] == 8
        assert views["ALL36"]["positive_blocks"] == 22
        assert views["ALL36"]["total_blocks"] == 22
        for sub in views.values():
            assert isinstance(sub["positive_blocks"], int)
            assert isinstance(sub["total_blocks"], int)

    def test_malformed_packed_value_hard_fails_on_the_pinned_file(self):
        golden = {
            "feature": "fair_gap_reversion", "type": "simple",
            "old24_mean_bps": "0.5", "old24_positive_blocks": "not-a-number",
            "new12_mean_bps": "0.6", "new12_positive_blocks": "8/8",
            "all36_mean_bps": "0.55", "all36_positive_blocks": "22/22",
        }
        with pytest.raises(_val.PackedFieldParseError):
            list(_iter_aggregate_row_views(PACKED_FILE, golden))

    def test_packed_parser_is_not_applied_to_any_other_file(self, monkeypatch):
        """Prove the packed parser is scoped to the exact pinned path,
        never to a generic wide_columns_prefixed heuristic — even a
        hypothetical OTHER file using the identical routing shape and
        identical raw "14/14" string must NOT be unpacked."""
        import recovery.harness as harness_mod

        fake_routing = dict(harness_mod._aggregate_scope_routing())
        fake_routing["CP99/some_other_wide_prefixed_file.csv"] = {
            "scope_source": "wide_columns_prefixed",
            "scope_prefix_map": {"old24": "OLD24"},
        }
        monkeypatch.setattr(harness_mod, "_aggregate_scope_routing", lambda: fake_routing)

        golden = {"feature": "fair_gap_reversion", "old24_mean_bps": "0.5",
                  "old24_positive_blocks": "14/14"}
        views = list(_iter_aggregate_row_views(
            "CP99/some_other_wide_prefixed_file.csv", golden
        ))
        assert len(views) == 1
        sub = views[0][4]
        # NOT parsed: stays the raw packed string, untouched.
        assert sub["positive_blocks"] == "14/14"
        assert "total_blocks" not in sub

    def test_run_regression_treats_packed_parse_error_as_failed_not_crash(self, monkeypatch):
        """A malformed packed value must be a HARD FAIL of that row's
        instances (counted into `failed`), never a crash of the whole
        harness and never silently PENDING."""
        import recovery.harness as harness_mod

        monkeypatch.setattr(harness_mod, "_get_block_summary", lambda *a, **k: None)
        monkeypatch.setattr(
            harness_mod, "availability_snapshot",
            lambda: {
                "present_sessions": 11, "expected_sessions": 11,
                "present_nominal_hours": 36.0, "expected_nominal_hours": 36.0,
                "missing_sessions": [],
            },
        )

        def _boom(_raw):
            raise _val.PackedFieldParseError("forced for test")

        monkeypatch.setattr(_val, "parse_packed_positive_blocks", _boom)

        out = harness_mod.run_regression()
        assert out["raw_ready"] is True

        # NOTE: reads the in-memory outcomes, not out["summary_path"]
        # from disk — REPORTS_DIR is shared across xdist workers.
        target = next(o for o in out["outcomes"] if o["source_file"] == PACKED_FILE)
        assert target["failed"] == target["total_instances"] == 33
        assert target["matched"] == 0
        assert int(target["pending"]) == 0


# ===========================================================================
# ISSUE 4 — SKIPPED -> FAILED; PENDING reserved for raw-not-imported
# ===========================================================================
class TestT21FailedVsPendingDistinction:
    def test_summary_is_none_is_the_only_pending_case(self):
        # A real OLD36_REFERENCE session id that simply has not been
        # imported into the DB yet -> _get_block_summary returns None
        # (genuinely "raw not imported yet"), never a firewall error.
        from checkpoint_registry import OLD36_REFERENCE_SESSIONS

        session_id = next(iter(OLD36_REFERENCE_SESSIONS))
        result = reproduce_row({"session_id": session_id, "asset": "BTC",
                                 "feature": "bitget_ofi", "horizon_ms": "100", "q": "0.8"})
        assert result["status"] == PENDING

    def test_structurally_invalid_block_is_failed_not_pending(self):
        summary = BlockSummary(
            session_id="SID_INV", asset="BTC", valid=False,
            invalid_reason="sync_grid_100ms: missing part", metrics=[],
        )
        _block_cache[("SID_INV", "BTC")] = summary
        result = reproduce_row({"session_id": "SID_INV", "asset": "BTC",
                                 "feature": "bitget_ofi", "horizon_ms": "100", "q": "0.8"})
        assert result["status"] == FAILED
        assert result["reason_code"] == STRUCTURAL_INVALID
        assert "missing part" in result["reason"]

    def test_missing_expected_metric_is_failed_not_pending(self):
        metric = BlockMetrics(
            session_id="SID_MM", asset="BTC", feature="bitget_ofi",
            horizon_ms=100, q=0.8, N=10, threshold=0.1,
            mean_signed_bps=0.5, hit_rate=0.5,
            median_signed_bps=0.4, mean_abs_move=0.2,
        )
        summary = BlockSummary(session_id="SID_MM", asset="BTC", valid=True,
                                invalid_reason=None, metrics=[metric])
        _block_cache[("SID_MM", "BTC")] = summary
        # Requesting a feature/horizon/q combination that does NOT
        # exist in this block's metrics.
        result = reproduce_row({"session_id": "SID_MM", "asset": "BTC",
                                 "feature": "leader_gap_1000ms", "horizon_ms": "30000",
                                 "q": "0.95"})
        assert result["status"] == FAILED
        assert result["reason_code"] == EXPECTED_METRIC_MISSING

    def test_missing_dispersion_state_is_failed_not_pending(self):
        metric = BlockMetrics(
            session_id="SID_DM", asset="BTC", feature="fair_gap_reversion",
            horizon_ms=5000, q=0.90, N=40, threshold=0.5,
            mean_signed_bps=1.0, hit_rate=0.6,
            median_signed_bps=0.9, mean_abs_move=0.2,
            dispersion_states=[
                {"state": "low", "N": 10, "mean_signed_bps": 0.1,
                 "hit_rate": 0.4, "disp_lo": 0.3, "disp_hi": 0.7},
            ],
        )
        summary = BlockSummary(session_id="SID_DM", asset="BTC", valid=True,
                                invalid_reason=None, metrics=[metric])
        _block_cache[("SID_DM", "BTC")] = summary
        result = reproduce_row({"session_id": "SID_DM", "asset": "BTC",
                                 "state": "high", "horizon_ms": "5000"})
        assert result["status"] == FAILED
        assert result["reason_code"] == DISPERSION_STATE_MISSING

    def test_valid_block_with_metric_present_is_still_matched(self):
        """Regression guard: the FAILED reason-code paths must not
        have broken the ordinary MATCHED path."""
        metric = BlockMetrics(
            session_id="SID_OK", asset="BTC", feature="bitget_ofi",
            horizon_ms=100, q=0.8, N=10, threshold=0.1,
            mean_signed_bps=0.5, hit_rate=0.5,
            median_signed_bps=0.4, mean_abs_move=0.2,
        )
        summary = BlockSummary(session_id="SID_OK", asset="BTC", valid=True,
                                invalid_reason=None, metrics=[metric])
        _block_cache[("SID_OK", "BTC")] = summary
        result = reproduce_row({"session_id": "SID_OK", "asset": "BTC",
                                 "feature": "bitget_ofi", "horizon_ms": "100", "q": "0.8"})
        assert result["status"] == "MATCHED"
        assert result["N"] == 10

    def test_run_regression_counts_structural_invalid_as_failed_not_pending(self, monkeypatch):
        """End-to-end (still zero real numeric golden comparison —
        every block is forced structurally invalid): proves
        run_regression() routes a STRUCTURAL_INVALID reproduce_row()
        result into `failed`, never into `pending`."""
        import recovery.harness as harness_mod

        def _always_invalid(session_id, asset):
            return BlockSummary(session_id=session_id, asset=asset, valid=False,
                                 invalid_reason="forced invalid for test", metrics=[])

        monkeypatch.setattr(harness_mod, "_get_block_summary", _always_invalid)
        monkeypatch.setattr(
            harness_mod, "availability_snapshot",
            lambda: {
                "present_sessions": 11, "expected_sessions": 11,
                "present_nominal_hours": 36.0, "expected_nominal_hours": 36.0,
                "missing_sessions": [],
            },
        )

        out = harness_mod.run_regression()
        # NOTE: reads the in-memory outcomes, not out["summary_path"]
        # from disk — REPORTS_DIR is shared across xdist workers.
        block_level_row = next(
            o for o in out["outcomes"]
            if o["source_file"] == "CP24/simple_q80_q90_q95_block_level_24h.csv"
        )
        assert block_level_row["pending"] == 0
        assert block_level_row["failed"] == block_level_row["total_instances"]


# ===========================================================================
# ISSUE 5 — GOLDEN_SOURCE_HASH_MISMATCH preflight
# ===========================================================================
class TestT22GoldenSourceHashPreflight:
    def test_preflight_passes_with_unmodified_goldens(self):
        _verify_golden_source_hashes()  # must not raise

    def test_pinned_registry_covers_exactly_24_files(self):
        meta = _load_recovered_metadata()
        assert len(meta["golden_csv_sha256"]) == 24

    def test_preflight_raises_on_tampered_hash(self, monkeypatch):
        import recovery.harness as harness_mod

        real_meta = harness_mod._load_recovered_metadata()
        tampered = dict(real_meta)
        bad_hashes = dict(real_meta["golden_csv_sha256"])
        some_key = next(iter(bad_hashes))
        bad_hashes[some_key] = "0" * 64
        tampered["golden_csv_sha256"] = bad_hashes
        monkeypatch.setattr(harness_mod, "_load_recovered_metadata", lambda: tampered)

        with pytest.raises(GoldenSourceHashMismatchError) as exc:
            harness_mod._verify_golden_source_hashes()
        assert "GOLDEN_SOURCE_HASH_MISMATCH" in str(exc.value)

    def test_preflight_raises_when_a_file_is_missing_from_registry(self, monkeypatch):
        import recovery.harness as harness_mod

        real_meta = harness_mod._load_recovered_metadata()
        tampered = dict(real_meta)
        shrunk = dict(real_meta["golden_csv_sha256"])
        del shrunk[next(iter(shrunk))]
        tampered["golden_csv_sha256"] = shrunk
        monkeypatch.setattr(harness_mod, "_load_recovered_metadata", lambda: tampered)

        with pytest.raises(GoldenSourceHashMismatchError) as exc:
            harness_mod._verify_golden_source_hashes()
        assert "GOLDEN_SOURCE_HASH_MISMATCH" in str(exc.value)

    def test_preflight_raises_when_registry_absent(self, monkeypatch):
        import recovery.harness as harness_mod

        real_meta = harness_mod._load_recovered_metadata()
        tampered = {k: v for k, v in real_meta.items() if k != "golden_csv_sha256"}
        monkeypatch.setattr(harness_mod, "_load_recovered_metadata", lambda: tampered)

        with pytest.raises(GoldenSourceHashMismatchError) as exc:
            harness_mod._verify_golden_source_hashes()
        assert "GOLDEN_SOURCE_HASH_MISMATCH" in str(exc.value)

    def test_run_regression_stops_before_availability_check_on_hash_mismatch(self, monkeypatch):
        """ADJUSTMENT #2: the preflight MUST run before any candidate
        RAW is opened or any quantitative comparison is attempted —
        proven here by making availability_snapshot() explode if it
        is ever reached."""
        import recovery.harness as harness_mod

        real_meta = harness_mod._load_recovered_metadata()
        tampered = dict(real_meta)
        bad_hashes = dict(real_meta["golden_csv_sha256"])
        some_key = next(iter(bad_hashes))
        bad_hashes[some_key] = "f" * 64
        tampered["golden_csv_sha256"] = bad_hashes
        monkeypatch.setattr(harness_mod, "_load_recovered_metadata", lambda: tampered)

        def _must_not_be_called():
            raise AssertionError(
                "availability_snapshot() must not be called before the "
                "golden-source-hash preflight"
            )

        monkeypatch.setattr(harness_mod, "availability_snapshot", _must_not_be_called)

        with pytest.raises(GoldenSourceHashMismatchError):
            harness_mod.run_regression()
