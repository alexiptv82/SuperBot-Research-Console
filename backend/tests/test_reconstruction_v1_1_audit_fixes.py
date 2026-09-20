"""Focused deterministic tests for the RECONSTRUCTION_V1.1 independent-
audit fixes (Issues 1-5).

ALL fixtures are synthetic — zero OLD36 golden numeric outputs are used
as test expectations or oracles anywhere in this file (no_tuning_on_
mismatch=true). This file never opens a raw ZIP through the sandbox,
never queries the runtime database, and never calls
``recovery.harness.run_regression()`` (the OLD36 golden regression is
NOT executed by this test suite, per explicit instruction).

Test coverage (mapped to the 5 audit issues):
  T01  median_signed_bps validation comparison            (Issue 1)
  T02  mean_abs_move validation comparison                 (Issue 1)
  T03  disp_lo / disp_hi validation comparison              (Issue 1)
  T04  dispersion_state row lookup                          (Issue 2)
  T05  dispersion low/mid/high independent aggregation      (Issue 3)
  T06  positive_blocks aggregate comparison                 (Issue 1)
  T07  positive_share aggregate comparison                  (Issue 1)
  T08  feature_mean aggregate comparison                    (Issue 1)
  T09  total_blocks aggregate comparison                    (Issue 1)
  T10  min_block exact identity comparison                  (Issue 1)
  T11  max_block exact identity comparison                  (Issue 1)
  T12  max-block exact-tie chooses lexicographically smallest (Issue 4)
  T13  missing sync_grid parquet part => HARD FAIL           (Issue 5)
  T14  duplicate sync_grid part => HARD FAIL                 (Issue 5)
  T15  truncated/corrupt sync_grid parquet => HARD FAIL      (Issue 5)
  T16  complete grid => accepted                             (Issue 5)
  T17  authoritative manifest part-count mismatch => HARD FAIL (Issue 5, bonus)
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import zipfile

import numpy as np
import pandas as pd
import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("SUPERBOT_ENV", "test")

from recovery import validation
from recovery.engine import (
    BlockMetrics,
    BlockSummary,
    aggregate_blocks,
    aggregate_dispersion_states,
    reconstruct_block,
)
from recovery.harness import (
    _block_cache,
    _evaluate_sync_grid_coverage,
    _sync_grid_coverage_from_path,
    reproduce_row,
)


@pytest.fixture(autouse=True)
def _clean_block_cache():
    """Every test using harness._block_cache must not leak state."""
    _block_cache.clear()
    yield
    _block_cache.clear()


# ===========================================================================
# Synthetic grid builder (mirrors test_reconstruction_v1_1.py's _base_grid)
# ===========================================================================

def _base_grid(n: int = 60, seed: int = 42) -> pd.DataFrame:
    ts_start, ts_step, mid_base = 1_700_000_000_000, 100, 30_000.0
    ts  = [ts_start + i * ts_step for i in range(n)]
    mns = [ts_start * 1_000_000 + i * ts_step * 1_000_000 for i in range(n)]
    mid = [mid_base + float(i) * 0.01 for i in range(n)]

    df = pd.DataFrame({
        "local_ts_ms":               ts,
        "sample_monotonic_ns":       mns,
        "bitget_mid":                mid,
        "bitget_book_age_recv_ms":   [100.0] * n,
        "adjusted_fair_venue_count": [3.0] * n,
    })
    rng = np.random.default_rng(seed)
    for col in [
        "bitget_ofi_norm_l1", "bitget_trade_imbalance_window",
        "bitget_depth_imbalance_l1", "bitget_depth_imbalance_l5",
        "external_ofi_consensus_l1", "external_trade_imbalance_consensus",
        "fair_accel_100ms_bps", "bitget_gap_to_fair_bps",
        "leader_gap_100ms_bps", "leader_gap_200ms_bps",
        "leader_gap_500ms_bps", "leader_gap_1000ms_bps",
    ]:
        df[col] = rng.uniform(-1.0, 1.0, n).tolist()
    df["bitget_fair_ofi_alignment"]   = rng.choice([-1.0, 1.0, np.nan], n).tolist()
    df["bitget_fair_trade_alignment"] = rng.choice([-1.0, 1.0, np.nan], n).tolist()
    df["external_perp_dispersion_bps"] = rng.uniform(0.0, 5.0, n).tolist()
    return df


# ===========================================================================
# T01 — median_signed_bps validation comparison (ISSUE 1)
# ===========================================================================
class TestT01MedianSignedBpsComparison:
    def test_within_tolerance_passes(self):
        golden = {"N": "10", "median_signed_bps": "1.500000"}
        candidate = {"N": 10, "median_signed_bps": 1.5000000001}
        fails = validation.compare_row(golden, candidate, feature="bitget_ofi")
        assert fails.get("median_signed_bps") is False

    def test_beyond_tolerance_fails(self):
        golden = {"N": "10", "median_signed_bps": "1.5"}
        candidate = {"N": 10, "median_signed_bps": 1.6}
        fails = validation.compare_row(golden, candidate, feature="bitget_ofi")
        assert fails.get("median_signed_bps") is True

    def test_not_evaluated_for_composite_feature(self):
        """simple_features_only — composites never carry this field."""
        golden = {"N": "10", "median_signed_bps": "1.5"}
        candidate = {"N": 10, "median_signed_bps": 999.0}
        fails = validation.compare_row(golden, candidate, feature="gap_depthBoth")
        assert "median_signed_bps" not in fails

    def test_definedness_mismatch_fails(self):
        golden = {"N": "0", "median_signed_bps": ""}
        candidate = {"N": 0, "median_signed_bps": None}
        fails = validation.compare_row(golden, candidate, feature="bitget_ofi")
        # N=0 on both sides -> N passes but n_gt_0_both is False, so
        # median_signed_bps is not even evaluated (applies_only_if).
        assert fails.get("N") is False
        assert "median_signed_bps" not in fails


# ===========================================================================
# T02 — mean_abs_move validation comparison (ISSUE 1)
# ===========================================================================
class TestT02MeanAbsMoveComparison:
    def test_within_tolerance_passes(self):
        golden = {"N": "5", "mean_abs_move": "0.025"}
        candidate = {"N": 5, "mean_abs_move": 0.0250000001}
        fails = validation.compare_row(golden, candidate, feature="depth_imbalance_l1")
        assert fails.get("mean_abs_move") is False

    def test_beyond_tolerance_fails(self):
        golden = {"N": "5", "mean_abs_move": "0.025"}
        candidate = {"N": 5, "mean_abs_move": 0.030}
        fails = validation.compare_row(golden, candidate, feature="depth_imbalance_l1")
        assert fails.get("mean_abs_move") is True

    def test_not_gated_by_n_gt_0(self):
        """mean_abs_move has no golden_N>0 applies_only_if condition —
        it is evaluated regardless of N (it is independent of the
        feature's own threshold-crossing count)."""
        golden = {"N": "0", "mean_abs_move": "0.01"}
        candidate = {"N": 0, "mean_abs_move": 0.01}
        fails = validation.compare_row(golden, candidate, feature="bitget_ofi")
        assert fails.get("mean_abs_move") is False

    def test_not_evaluated_for_composite_feature(self):
        golden = {"N": "5", "mean_abs_move": "0.01"}
        candidate = {"N": 5, "mean_abs_move": 999.0}
        fails = validation.compare_row(golden, candidate, feature="leader1000_extOFI")
        assert "mean_abs_move" not in fails


# ===========================================================================
# T03 — disp_lo / disp_hi validation comparison (ISSUE 1)
# ===========================================================================
class TestT03DispLoHiComparison:
    def test_within_tolerance_passes(self):
        golden = {"N": "5", "disp_lo": "0.30", "disp_hi": "0.70"}
        candidate = {"N": 5, "disp_lo": 0.3000000001, "disp_hi": 0.6999999999}
        fails = validation.compare_row(golden, candidate, feature="fair_gap_reversion")
        assert fails.get("disp_lo") is False
        assert fails.get("disp_hi") is False

    def test_beyond_tolerance_fails(self):
        golden = {"N": "5", "disp_lo": "0.30", "disp_hi": "0.70"}
        candidate = {"N": 5, "disp_lo": 0.31, "disp_hi": 0.70}
        fails = validation.compare_row(golden, candidate, feature="fair_gap_reversion")
        assert fails.get("disp_lo") is True
        assert fails.get("disp_hi") is False

    def test_scoped_to_fair_gap_reversion_only(self):
        golden = {"N": "5", "disp_lo": "0.30", "disp_hi": "0.70"}
        candidate = {"N": 5, "disp_lo": 999.0, "disp_hi": 999.0}
        fails = validation.compare_row(golden, candidate, feature="bitget_ofi")
        assert "disp_lo" not in fails
        assert "disp_hi" not in fails


# ===========================================================================
# T04 — dispersion_state row lookup (ISSUE 2)
# ===========================================================================
class TestT04DispersionStateRowLookup:
    def _seed_summary(self):
        metric = BlockMetrics(
            session_id="SID_D", asset="BTC", feature="fair_gap_reversion",
            horizon_ms=5000, q=0.90,
            N=40, threshold=0.5, mean_signed_bps=1.0, hit_rate=0.6,
            median_signed_bps=0.9, mean_abs_move=0.2,
            dispersion_states=[
                {"state": "low",  "N": 10, "mean_signed_bps": 0.10,
                 "hit_rate": 0.40, "disp_lo": 0.3, "disp_hi": 0.7},
                {"state": "mid",  "N": 15, "mean_signed_bps": 0.20,
                 "hit_rate": 0.50, "disp_lo": 0.3, "disp_hi": 0.7},
                {"state": "high", "N": 15, "mean_signed_bps": 0.30,
                 "hit_rate": 0.60, "disp_lo": 0.3, "disp_hi": 0.7},
            ],
        )
        summary = BlockSummary(
            session_id="SID_D", asset="BTC", valid=True, invalid_reason=None,
            metrics=[metric],
        )
        _block_cache[("SID_D", "BTC")] = summary
        return metric

    def test_dispersion_state_returns_substate_not_base(self):
        base_metric = self._seed_summary()
        golden_row = {
            "session_id": "SID_D", "asset": "BTC",
            "state": "high", "horizon_ms": "5000",
        }
        result = reproduce_row(golden_row)
        assert result["status"] == "MATCHED"
        # Must be the "high" substate values, NOT the base block values.
        assert result["N"] == 15
        assert result["mean_signed_bps"] == pytest.approx(0.30)
        assert result["hit_rate"] == pytest.approx(0.60)
        assert result["disp_lo"] == pytest.approx(0.3)
        assert result["disp_hi"] == pytest.approx(0.7)
        # Base block N=40/mean=1.0 must NEVER leak into a dispersion row.
        assert result["N"] != base_metric.N
        assert result["mean_signed_bps"] != base_metric.mean_signed_bps

    def test_dispersion_state_low_and_mid_are_distinct(self):
        self._seed_summary()
        r_low = reproduce_row({"session_id": "SID_D", "asset": "BTC",
                                "state": "low", "horizon_ms": "5000"})
        r_mid = reproduce_row({"session_id": "SID_D", "asset": "BTC",
                                "state": "mid", "horizon_ms": "5000"})
        assert r_low["N"] == 10
        assert r_mid["N"] == 15
        assert r_low["mean_signed_bps"] != r_mid["mean_signed_bps"]

    def test_unknown_dispersion_state_is_failed_not_pending(self):
        """ISSUE 4 (Message 222 audit fix): an unrecognized dispersion
        sub-state is a HARD FAIL (reason_code=DISPERSION_STATE_MISSING),
        never PENDING and never a silent SKIPPED bucket."""
        self._seed_summary()
        result = reproduce_row({"session_id": "SID_D", "asset": "BTC",
                                 "state": "nonexistent", "horizon_ms": "5000"})
        assert result["status"] == "FAILED"
        assert result["reason_code"] == "DISPERSION_STATE_MISSING"

    def test_non_dispersion_row_still_returns_base_metrics(self):
        """Regression guard: rows WITHOUT a dispersion_state must
        still return the base block metrics exactly as before."""
        self._seed_summary()
        result = reproduce_row({"session_id": "SID_D", "asset": "BTC",
                                 "feature": "fair_gap_reversion",
                                 "horizon_ms": "5000", "q": "0.9"})
        assert result["status"] == "MATCHED"
        assert result["N"] == 40
        assert result["mean_signed_bps"] == pytest.approx(1.0)


# ===========================================================================
# T05 — dispersion low/mid/high independent aggregation (ISSUE 3)
# ===========================================================================
class TestT05DispersionIndependentAggregation:
    def test_states_are_not_pooled(self):
        n = 200
        s1 = reconstruct_block("SID_DA_1", "BTC", _base_grid(n=n, seed=1))
        s2 = reconstruct_block("SID_DA_2", "BTC", _base_grid(n=n, seed=1))
        assert s1.valid and s2.valid

        disp_agg = aggregate_dispersion_states([s1, s2])
        assert len(disp_agg) > 0

        by_hq: dict = {}
        for a in disp_agg:
            by_hq.setdefault((a.horizon_ms, a.q), {})[a.state] = a

        # Every (horizon, q) key must carry independent entries for
        # low/mid/high — never a single pooled entry.
        for hq, states in by_hq.items():
            assert set(states.keys()) == {"low", "mid", "high"}

        # At least one (horizon, q) combination must show states with
        # genuinely different feature_mean / n_gt_0_blocks (proving
        # the split is not just three copies of one pooled result).
        varied = False
        for hq, states in by_hq.items():
            ns = {s: v.n_gt_0_blocks for s, v in states.items()}
            if len(set(ns.values())) > 1:
                varied = True
                break
        assert varied

    def test_total_blocks_matches_valid_block_population(self):
        """A dispersion state's total_blocks must equal the count of
        structurally VALID blocks reaching PhaseB for fair_gap_reversion
        at that (horizon,q) — identical population to the base
        aggregate, never a subset filtered by whether that state fired."""
        s1 = reconstruct_block("SID_DA_3", "BTC", _base_grid(n=150, seed=2))
        s2 = reconstruct_block("SID_DA_4", "ETH", _base_grid(n=150, seed=3))
        assert s1.valid and s2.valid

        disp_agg = aggregate_dispersion_states([s1, s2])
        main_agg = aggregate_blocks([s1, s2])
        base_total = {
            (a.horizon_ms, a.q): a.total_blocks
            for a in main_agg if a.feature == "fair_gap_reversion"
        }
        for a in disp_agg:
            assert a.total_blocks == base_total[(a.horizon_ms, a.q)]


# ===========================================================================
# T06-T11 — aggregate-level comparisons (ISSUE 1)
# ===========================================================================
class TestT06PositiveBlocksAggregateComparison:
    def test_exact_match_passes(self):
        golden = {"positive_blocks": "18"}
        candidate = {"positive_blocks_count": 18}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["positive_blocks"] is False

    def test_mismatch_fails(self):
        golden = {"positive_blocks": "18"}
        candidate = {"positive_blocks_count": 17}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["positive_blocks"] is True


class TestT07PositiveShareAggregateComparison:
    def test_within_tolerance_passes(self):
        golden = {"positive_share": "0.818181818182"}
        candidate = {"positive_share": 18 / 22}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["positive_share"] is False

    def test_beyond_tolerance_fails(self):
        golden = {"positive_share": "0.9"}
        candidate = {"positive_share": 18 / 22}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["positive_share"] is True


class TestT08FeatureMeanAggregateComparison:
    def test_within_tolerance_passes(self):
        golden = {"mean_signed_bps": "0.12925370221105711"}
        candidate = {"feature_mean": 0.12925370221105711}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["feature_mean"] is False

    def test_beyond_tolerance_fails(self):
        golden = {"feature_mean": "0.5"}
        candidate = {"feature_mean": 0.6}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["feature_mean"] is True


class TestT09TotalBlocksAggregateComparison:
    def test_exact_match_passes_via_blocks_column(self):
        golden = {"blocks": "22"}
        candidate = {"total_blocks": 22}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["total_blocks"] is False

    def test_mismatch_fails(self):
        golden = {"total_blocks": "22"}
        candidate = {"total_blocks": 21}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["total_blocks"] is True


class TestT10MinBlockIdentityComparison:
    def test_exact_identity_match_passes(self):
        golden = {"min_block": "SESSION_A,BTC"}
        candidate = {"min_block": ("SESSION_A", "BTC")}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["min_block"] is False

    def test_identity_mismatch_fails(self):
        golden = {"min_block": "SESSION_A,BTC"}
        candidate = {"min_block": ("SESSION_B", "BTC")}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["min_block"] is True

    def test_non_identity_golden_value_is_not_silently_passed(self):
        """A bare numeric mean under the min_block column (as some
        existing CP24/CP36 artifacts store) is NOT comparable as an
        identity and must never be reported as a match (True) — it is
        None (not comparable), which the caller must never count as a
        pass."""
        golden = {"min_block": "0.012760544513006756"}
        candidate = {"min_block": ("SESSION_A", "BTC")}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["min_block"] is None
        assert validation.aggregate_failed(fails) is False  # None != True


class TestT11MaxBlockIdentityComparison:
    def test_exact_identity_match_passes(self):
        golden = {"max_block": "SESSION_B,ETH"}
        candidate = {"max_block": ("SESSION_B", "ETH")}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["max_block"] is False

    def test_identity_mismatch_fails(self):
        golden = {"max_block": "SESSION_B,ETH"}
        candidate = {"max_block": ("SESSION_A", "BTC")}
        fails = validation.compare_aggregate(golden, candidate)
        assert fails["max_block"] is True


# ===========================================================================
# T12 — max-block exact-tie chooses lexicographically smallest (ISSUE 4)
# ===========================================================================
class TestT12MaxBlockTieBreak:
    def test_max_block_tie_picks_lexically_smallest_identity(self):
        """Three blocks built from IDENTICAL underlying data (same
        seed) produce EXACTLY tied mean_signed_bps for every
        (feature, horizon, q). Under the pre-fix implementation,
        aggregate_blocks() sorted ascending by (mean, session_id,
        asset) and took the LAST element as max_block — on an exact
        tie this selects the LEXICALLY LARGEST identity, violating
        tie_break=ascending_lexical(session_id,asset). This test
        FAILS under that buggy implementation and PASSES with the fix
        (extreme value first, then ascending-lexical tie-break)."""
        df = _base_grid(n=80, seed=7)
        s_a_btc = reconstruct_block("SESSION_A", "BTC", df)
        s_a_eth = reconstruct_block("SESSION_A", "ETH", df)
        s_b_btc = reconstruct_block("SESSION_B", "BTC", df)
        assert s_a_btc.valid and s_a_eth.valid and s_b_btc.valid

        agg = aggregate_blocks([s_b_btc, s_a_eth, s_a_btc])  # order shuffled

        checked_any = False
        for a in agg:
            if a.n_gt_0_blocks < 3:
                continue  # need all three tied blocks contributing
            checked_any = True
            # All three blocks are built from identical data -> tied mean.
            expected_identity = min(
                [("SESSION_A", "BTC"), ("SESSION_A", "ETH"), ("SESSION_B", "BTC")]
            )
            assert a.max_block == expected_identity, (
                f"feature={a.feature} H={a.horizon_ms} q={a.q}: "
                f"max_block={a.max_block} expected {expected_identity} "
                f"(tied mean={a.max_block_mean})"
            )
            assert a.min_block == expected_identity
        assert checked_any, "no (feature,horizon,q) had all 3 tied blocks with N>0"


# ===========================================================================
# T13-T17 — sync_grid_100ms parquet part-coverage HARD FAIL (ISSUE 5)
# ===========================================================================

def _valid_parquet_bytes(n_rows: int = 3) -> bytes:
    df = pd.DataFrame({"a": list(range(n_rows))})
    buf = io.BytesIO()
    df.to_parquet(buf, engine="pyarrow")
    return buf.getvalue()


def _build_sync_grid_zip(tmp_path, entries: dict[str, bytes]) -> str:
    zpath = tmp_path / "session.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for name, data in entries.items():
            zf.writestr(f"sync_grid_100ms/{name}", data)
    return str(zpath)


class TestT13MissingPartHardFail:
    def test_missing_middle_part_hard_fails(self, tmp_path):
        entries = {
            f"sync_grid_100ms_SID_{i:07d}.parquet": _valid_parquet_bytes()
            for i in (0, 1, 3)  # index 2 missing
        }
        zpath = _build_sync_grid_zip(tmp_path, entries)
        ok, reason = _sync_grid_coverage_from_path(zpath)
        assert ok is False
        assert reason is not None and "sync_grid_100ms" in reason


class TestT14DuplicatePartHardFail:
    def test_duplicate_part_index_hard_fails(self, tmp_path):
        good = _valid_parquet_bytes()
        entries = {
            "sync_grid_100ms_AAA_0000000.parquet": good,
            "sync_grid_100ms_BBB_0000000.parquet": good,  # duplicate index 0
            "sync_grid_100ms_AAA_0000001.parquet": good,
        }
        zpath = _build_sync_grid_zip(tmp_path, entries)
        ok, reason = _sync_grid_coverage_from_path(zpath)
        assert ok is False
        assert reason is not None and "sync_grid_100ms" in reason


class TestT15CorruptPartHardFail:
    def test_truncated_corrupt_part_hard_fails(self, tmp_path):
        entries = {
            "sync_grid_100ms_SID_0000000.parquet": _valid_parquet_bytes(),
            "sync_grid_100ms_SID_0000001.parquet": b"not a real parquet payload!!",
        }
        zpath = _build_sync_grid_zip(tmp_path, entries)
        ok, reason = _sync_grid_coverage_from_path(zpath)
        assert ok is False
        assert reason is not None and "sync_grid_100ms" in reason


class TestT16CompleteGridAccepted:
    def test_complete_contiguous_grid_is_accepted(self, tmp_path):
        entries = {
            f"sync_grid_100ms_SID_{i:07d}.parquet": _valid_parquet_bytes()
            for i in range(5)
        }
        zpath = _build_sync_grid_zip(tmp_path, entries)
        ok, reason = _sync_grid_coverage_from_path(zpath, expected_count=5)
        assert ok is True
        assert reason is None


class TestT17AuthoritativeCountMismatchHardFail:
    """Bonus: proves the fix does NOT guess expected part counts from
    the observed contiguous range alone — a fully sequential, gap-free
    grid can still be a whole-tail truncation relative to the
    authoritative manifest count."""

    def test_authoritative_manifest_mismatch_hard_fails(self, tmp_path):
        entries = {
            f"sync_grid_100ms_SID_{i:07d}.parquet": _valid_parquet_bytes()
            for i in range(3)  # contiguous 0..2, sequence_ok=True
        }
        zpath = _build_sync_grid_zip(tmp_path, entries)
        ok, reason = _sync_grid_coverage_from_path(zpath, expected_count=5)
        assert ok is False
        assert reason is not None and "manifest expects 5" in reason

    def test_no_authoritative_count_does_not_invent_one(self, tmp_path):
        """Without an authoritative count, a contiguous gap-free
        sequence is accepted (we do not invent an expected count)."""
        entries = {
            f"sync_grid_100ms_SID_{i:07d}.parquet": _valid_parquet_bytes()
            for i in range(3)
        }
        zpath = _build_sync_grid_zip(tmp_path, entries)
        ok, reason = _sync_grid_coverage_from_path(zpath, expected_count=None)
        assert ok is True
        assert reason is None
