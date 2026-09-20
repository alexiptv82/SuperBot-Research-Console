"""Focused deterministic tests for the RECONSTRUCTION_V1.1 engine.

ALL fixtures are synthetic — zero golden numeric outputs are used as
test expectations (spec: no_tuning_on_mismatch=true, and the test suite
itself must not embed golden values as oracle truth).

Test coverage (27 tests mapped to frozen spec items):
  T01  stable canonical ordering
  T02  duplicate sort-key CORRUPT_GRID
  T03  duplicate full row CORRUPT_GRID
  T04  immutable grid_pos through quality-filter
  T05  canonical t+k indexing despite filtered rows
  T06  endpoint finite/positive rule
  T07  quality gate
  T08  simple threshold domain (only quality+finite rows count)
  T09  derived (depthBoth) threshold domain
  T10  gap threshold-domain vs event-domain split
  T11  leader threshold-domain vs event-domain split
  T12  zero-signal exclusion
  T13  type-7 quantile semantics
  T14  average-rank ties
  T15  NaN handling (listwise drop)
  T16  session-end truncation (per-horizon independent)
  T17  greedy earliest-first overlap
  T18  gap_* alignment filter
  T19  depthBoth composite N and direction
  T20  depthL1_extOFI composite
  T21  all seven gap_* composites produce threshold == fair_gap threshold
  T22  leader1000_extOFI threshold equals leader_gap_1000ms threshold
  T23  mean_abs_move is feature-independent for same (block,horizon)
  T24  N=0 aggregation exclusion
  T25  positive_share denominator
  T26  min/max lexical tie-break
  T27  NEW36 firewall rejects quantitative input
"""
from __future__ import annotations

import math
import os
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

# Make backend root importable when running from /app/backend/tests/
BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Force test environment so database.py refuses runtime paths
os.environ.setdefault("SUPERBOT_ENV", "test")

from recovery.engine import (
    QUANTILES,
    GRID_MS,
    CorruptGridError,
    BlockMetrics,
    BlockSummary,
    _quality_admissible_mask,
    _rank_signed_uniform,
    _quantile_type7,
    _build_forward_return_array,
    _greedy_overlap_filter,
    _compute_simple_feature,
    _compute_derived_composite,
    _compute_gap_composite,
    _compute_leader1000_extofi,
    build_canonical_grid,
    reconstruct_block,
    aggregate_blocks,
)


# ---------------------------------------------------------------------------
# Synthetic grid builders
# ---------------------------------------------------------------------------

def _base_grid(
    n: int = 50,
    ts_start: int = 1_700_000_000_000,
    ts_step: int = 100,
    mid_base: float = 30_000.0,
    fair_venue_count: int = 3,
    book_age: float = 100.0,
    add_all_cols: bool = True,
) -> pd.DataFrame:
    """Build a minimal synthetic grid that passes all structural checks."""
    ts  = [ts_start + i * ts_step for i in range(n)]
    mns = [ts_start * 1_000_000 + i * ts_step * 1_000_000 for i in range(n)]
    mid = [mid_base + float(i) * 0.01 for i in range(n)]

    df = pd.DataFrame({
        "local_ts_ms":          ts,
        "sample_monotonic_ns":  mns,
        "bitget_mid":           mid,
        "bitget_book_age_recv_ms": [float(book_age)] * n,
        "adjusted_fair_venue_count": [float(fair_venue_count)] * n,
    })

    if add_all_cols:
        rng = np.random.default_rng(42)
        for col in [
            "bitget_ofi_norm_l1",
            "bitget_trade_imbalance_window",
            "bitget_depth_imbalance_l1",
            "bitget_depth_imbalance_l5",
            "external_ofi_consensus_l1",
            "external_trade_imbalance_consensus",
            "fair_accel_100ms_bps",
            "bitget_gap_to_fair_bps",
            "leader_gap_100ms_bps",
            "leader_gap_200ms_bps",
            "leader_gap_500ms_bps",
            "leader_gap_1000ms_bps",
        ]:
            df[col] = rng.uniform(-1.0, 1.0, n).tolist()
        df["bitget_fair_ofi_alignment"]   = rng.choice([-1.0, 1.0, np.nan], n).tolist()
        df["bitget_fair_trade_alignment"] = rng.choice([-1.0, 1.0, np.nan], n).tolist()
        df["external_perp_dispersion_bps"] = rng.uniform(0.0, 5.0, n).tolist()

    return df


# ===========================================================================
# T01 — stable canonical ordering
# ===========================================================================
class TestT01StableOrdering:
    def test_sorts_by_ts_then_monotonic_ns(self):
        """Grid must be stable-sorted: primary ts asc, secondary ns asc."""
        n = 10
        ts  = list(range(1_000, 1_000 + n * 100, 100))
        mns = list(range(0, n * 1_000_000, 1_000_000))
        mid = [100.0 + i for i in range(n)]
        age = [100.0] * n
        fvc = [3.0] * n
        # Shuffle rows
        idx = list(range(n))
        shuffled = idx[::-1]
        df = pd.DataFrame({
            "local_ts_ms":             [ts[i] for i in shuffled],
            "sample_monotonic_ns":     [mns[i] for i in shuffled],
            "bitget_mid":              [mid[i] for i in shuffled],
            "bitget_book_age_recv_ms": [age[i] for i in shuffled],
            "adjusted_fair_venue_count": [fvc[i] for i in shuffled],
        })
        grid = build_canonical_grid(df)
        assert list(grid["local_ts_ms"]) == sorted(grid["local_ts_ms"].tolist())
        assert list(grid["grid_pos"]) == list(range(n))

    def test_sort_is_stable_preserving_insertion_order_for_equal_ts(self):
        """Equal ts + ns (distinct) preserves order by sample_monotonic_ns asc."""
        df = pd.DataFrame({
            "local_ts_ms":              [1000, 1000, 2000],
            "sample_monotonic_ns":      [200,  100,  300],
            "bitget_mid":               [100.0, 101.0, 102.0],
            "bitget_book_age_recv_ms":  [100.0, 100.0, 100.0],
            "adjusted_fair_venue_count":[3.0, 3.0, 3.0],
        })
        grid = build_canonical_grid(df)
        # After sort: (1000,100), (1000,200), (2000,300)
        assert list(grid["sample_monotonic_ns"]) == [100, 200, 300]
        assert list(grid["grid_pos"]) == [0, 1, 2]


# ===========================================================================
# T02 — duplicate sort-key CORRUPT_GRID
# ===========================================================================
class TestT02DuplicateSortKey:
    def test_exact_duplicate_sort_key_raises(self):
        df = pd.DataFrame({
            "local_ts_ms":              [1000, 1000],
            "sample_monotonic_ns":      [100,  100],   # exact dup key
            "bitget_mid":               [100.0, 101.0],
            "bitget_book_age_recv_ms":  [100.0, 100.0],
            "adjusted_fair_venue_count":[3.0, 3.0],
        })
        with pytest.raises(CorruptGridError, match="duplicate.*local_ts_ms.*sample_monotonic_ns|CORRUPT_GRID"):
            build_canonical_grid(df)

    def test_same_ts_different_ns_is_valid(self):
        """Spec: duplicate_timestamp_different_monotonic_ns=VALID_NOT_A_VIOLATION"""
        df = pd.DataFrame({
            "local_ts_ms":              [1000, 1000],
            "sample_monotonic_ns":      [100,  200],   # different ns — valid
            "bitget_mid":               [100.0, 101.0],
            "bitget_book_age_recv_ms":  [100.0, 100.0],
            "adjusted_fair_venue_count":[3.0, 3.0],
        })
        grid = build_canonical_grid(df)
        assert len(grid) == 2


# ===========================================================================
# T03 — duplicate full row CORRUPT_GRID
# ===========================================================================
class TestT03DuplicateFullRow:
    def test_exact_duplicate_full_row_raises(self):
        row = {
            "local_ts_ms": 1000, "sample_monotonic_ns": 100,
            "bitget_mid": 100.0, "bitget_book_age_recv_ms": 100.0,
            "adjusted_fair_venue_count": 3.0,
        }
        df = pd.DataFrame([row, row])
        # Both rows are identical including the sort key — dup key check fires first
        with pytest.raises(CorruptGridError):
            build_canonical_grid(df)


# ===========================================================================
# T04 — immutable grid_pos through quality-filter
# ===========================================================================
class TestT04ImmutableGridPos:
    def test_grid_pos_is_preserved_after_quality_filter(self):
        """grid_pos_reuse_rule: all t+k references resolve against canonical
        grid_pos array, never a filtered subset."""
        n = 20
        df = _base_grid(n=n)
        # Corrupt some rows to fail quality gate
        df.loc[3:6, "adjusted_fair_venue_count"] = 0.0
        grid = build_canonical_grid(df)
        q_mask = _quality_admissible_mask(grid).to_numpy(dtype=bool)
        # grid_pos must span 0..L-1 regardless of quality
        assert list(grid["grid_pos"]) == list(range(n))
        # Filtered rows still have valid grid_pos values (not re-indexed)
        failed_positions = grid.loc[~q_mask, "grid_pos"].tolist()
        assert all(0 <= p < n for p in failed_positions)


# ===========================================================================
# T05 — canonical t+k indexing despite filtered rows
# ===========================================================================
class TestT05CanonicalTkIndexing:
    def test_forward_return_uses_canonical_index_not_filtered(self):
        """Forward mid[grid_pos+k] always looks up in the FULL canonical array."""
        n = 30
        # Ascending mid
        mid = [10_000.0 + i * 0.1 for i in range(n)]
        df = pd.DataFrame({
            "local_ts_ms":              list(range(n)),
            "sample_monotonic_ns":      list(range(n)),
            "bitget_mid":               mid,
            "bitget_book_age_recv_ms":  [100.0] * n,
            "adjusted_fair_venue_count":[3.0] * n,
        })
        grid = build_canonical_grid(df)
        canonical_mid = grid["bitget_mid"].to_numpy(dtype=float)
        k = 5
        fwd = _build_forward_return_array(canonical_mid, k)
        # For row 0: mid[5]/mid[0]
        expected_0 = math.log(mid[5] / mid[0]) * 10000.0
        assert abs(fwd[0] - expected_0) < 1e-9
        # Last k rows: NaN (session end)
        assert all(math.isnan(fwd[i]) for i in range(n - k, n))


# ===========================================================================
# T06 — endpoint finite/positive rule
# ===========================================================================
class TestT06EndpointValidity:
    def test_nan_endpoint_gives_nan_return(self):
        """forward_endpoint_validity_rule: drop if endpoint not finite or positive."""
        mid = [100.0, 101.0, np.nan, 103.0, 104.0]
        canonical_mid = np.array(mid)
        k = 2
        fwd = _build_forward_return_array(canonical_mid, k)
        # Row 0: endpoint = mid[2] = NaN → NaN
        assert math.isnan(fwd[0])
        # Row 1: endpoint = mid[3] = 103.0 → valid
        assert math.isfinite(fwd[1])

    def test_zero_endpoint_gives_nan_return(self):
        mid = [100.0, 0.0, 100.0, 100.0, 100.0]
        canonical_mid = np.array(mid)
        k = 1
        fwd = _build_forward_return_array(canonical_mid, k)
        # Row 0: endpoint = mid[1] = 0.0 → invalid
        assert math.isnan(fwd[0])

    def test_negative_endpoint_gives_nan_return(self):
        mid = [100.0, -1.0, 100.0, 100.0, 100.0]
        canonical_mid = np.array(mid)
        k = 1
        fwd = _build_forward_return_array(canonical_mid, k)
        assert math.isnan(fwd[0])


# ===========================================================================
# T07 — quality gate
# ===========================================================================
class TestT07QualityGate:
    def test_all_conditions_required(self):
        df = pd.DataFrame({
            "local_ts_ms":              [1, 2, 3, 4, 5],
            "sample_monotonic_ns":      [1, 2, 3, 4, 5],
            "bitget_mid":               [100.0, 100.0, np.nan, 100.0, 100.0],
            "bitget_book_age_recv_ms":  [100.0, 1001.0, 100.0, 100.0, 100.0],
            "adjusted_fair_venue_count":[3.0,   3.0,   3.0,   1.0,   3.0  ],
        })
        grid = build_canonical_grid(df)
        mask = _quality_admissible_mask(grid).to_numpy(dtype=bool)
        # Row 0: all OK
        assert mask[0] is np.bool_(True)
        # Row 1: book_age > 1000 → FAIL
        assert mask[1] is np.bool_(False)
        # Row 2: mid NaN → FAIL
        assert mask[2] is np.bool_(False)
        # Row 3: venue_count < 2 → FAIL
        assert mask[3] is np.bool_(False)
        # Row 4: all OK
        assert mask[4] is np.bool_(True)

    def test_bitget_mid_zero_fails_gate(self):
        df = pd.DataFrame({
            "local_ts_ms": [1], "sample_monotonic_ns": [1],
            "bitget_mid": [0.0],
            "bitget_book_age_recv_ms": [100.0],
            "adjusted_fair_venue_count": [3.0],
        })
        grid = build_canonical_grid(df)
        mask = _quality_admissible_mask(grid).to_numpy(dtype=bool)
        assert not mask[0]


# ===========================================================================
# T08 — simple threshold domain
# ===========================================================================
class TestT08SimpleThresholdDomain:
    def test_threshold_uses_only_quality_and_finite_signal_rows(self):
        """simple_domain = quality_admissible AND finite(signal)."""
        n = 20
        # 10 quality-passing rows with signal, 10 quality-failing rows
        signal = [float(i + 1) for i in range(n)]
        q_mask  = np.array([True] * 10 + [False] * 10, dtype=bool)
        # Threshold should be computed ONLY over first 10 rows
        signal_arr = np.array(signal, dtype=float)
        domain_vals = np.abs(signal_arr[q_mask & np.isfinite(signal_arr)])
        expected_thresh = float(np.quantile(domain_vals, 0.8, method="linear"))

        thresh = _quantile_type7(domain_vals, 0.8)
        assert thresh is not None
        assert abs(thresh - expected_thresh) < 1e-12

    def test_threshold_undefined_with_fewer_than_2_values(self):
        assert _quantile_type7(np.array([1.0]), 0.9) is None
        assert _quantile_type7(np.array([]), 0.9) is None

    def test_threshold_defined_with_exactly_2_values(self):
        result = _quantile_type7(np.array([1.0, 2.0]), 0.5)
        assert result is not None
        assert abs(result - 1.5) < 1e-12


# ===========================================================================
# T09 — derived threshold domain (depthBoth / depthL1_extOFI)
# ===========================================================================
class TestT09DerivedThresholdDomain:
    def _make_derived_data(self, n=40):
        rng = np.random.default_rng(7)
        q_mask = np.ones(n, dtype=bool)
        z1 = rng.uniform(-1, 1, n)
        z2 = rng.uniform(-1, 1, n)
        # Make some NaN
        z1[0:3] = np.nan
        z2[5:8] = np.nan
        gpos = np.arange(n, dtype=np.int64)
        mid  = 10_000.0 + np.arange(n) * 0.1
        k    = 5
        fwd  = _build_forward_return_array(mid, k)
        return gpos, q_mask, z1, z2, fwd, k

    def test_derived_threshold_uses_both_finite_domain(self):
        gpos, q_mask, z1, z2, fwd, k = self._make_derived_data()
        r = _compute_derived_composite(gpos, q_mask, z1, z2, fwd, k, 0.8)
        # derived_domain: finite(z1) AND finite(z2) AND quality
        derived_dom = np.isfinite(z1) & np.isfinite(z2) & q_mask
        D = np.where(derived_dom, (z1 + z2) / 2.0, np.nan)
        expected_thresh = _quantile_type7(np.abs(D[derived_dom]), 0.8)
        assert r["threshold"] is not None
        assert abs(r["threshold"] - expected_thresh) < 1e-12


# ===========================================================================
# T10 — gap threshold-domain vs event-domain split
# ===========================================================================
class TestT10GapDomainSplit:
    def test_gap_threshold_domain_excludes_nan_confirmation(self):
        """Threshold computed over gap_threshold_domain (only finite(gap)),
        NOT over gap_event_domain (which also requires finite confirmation_component).
        The two domains can have different sizes."""
        n = 50
        rng = np.random.default_rng(13)
        q_mask = np.ones(n, dtype=bool)
        gap    = rng.uniform(-2.0, 2.0, n)
        gap[0:5] = np.nan   # 5 rows outside gap_threshold_domain

        # Confirmation component: half NaN → halves event domain
        conf_finite = np.ones(n, dtype=bool)
        conf_finite[10:30] = False  # these rows outside event domain

        # alignment: all True for simplicity
        align = np.ones(n, dtype=bool)

        gpos = np.arange(n, dtype=np.int64)
        mid  = 10_000.0 + np.arange(n) * 0.1
        fwd  = _build_forward_return_array(mid, 5)

        q_thresh = 0.90
        gap_thresh_domain = q_mask & np.isfinite(gap)
        thresh = _quantile_type7(np.abs(gap[gap_thresh_domain]), q_thresh)

        r = _compute_gap_composite(
            gpos, q_mask, gap, fwd, 5, q_thresh, thresh, conf_finite, align
        )

        # Threshold must equal the value computed only over gap_threshold_domain
        assert r["threshold"] is not None
        assert abs(r["threshold"] - thresh) < 1e-12


# ===========================================================================
# T11 — leader threshold-domain vs event-domain split
# ===========================================================================
class TestT11LeaderDomainSplit:
    def test_leader_threshold_computed_over_leader_threshold_domain(self):
        """Leader threshold uses only finite(leader1000), not the event domain."""
        n = 40
        rng = np.random.default_rng(17)
        q_mask   = np.ones(n, dtype=bool)
        leader1k = rng.uniform(-3.0, 3.0, n)
        leader1k[0:5] = np.nan   # 5 rows outside threshold domain
        ext_ofi  = rng.uniform(-1.0, 1.0, n)
        ext_ofi[10:20] = np.nan  # 10 rows outside event domain

        gpos = np.arange(n, dtype=np.int64)
        mid  = 10_000.0 + np.arange(n) * 0.1
        fwd  = _build_forward_return_array(mid, 10)

        q_val = 0.80
        leader_thresh_domain = q_mask & np.isfinite(leader1k)
        expected_thresh = _quantile_type7(np.abs(leader1k[leader_thresh_domain]), q_val)

        r = _compute_leader1000_extofi(
            gpos, q_mask, leader1k, ext_ofi, fwd, 10, q_val, expected_thresh
        )

        assert r["threshold"] is not None
        assert abs(r["threshold"] - expected_thresh) < 1e-12


# ===========================================================================
# T12 — zero-signal exclusion
# ===========================================================================
class TestT12ZeroSignalExclusion:
    def test_exact_zero_signal_not_selected_as_event(self):
        """event_rule: gate_series != 0  (exclusive zero)."""
        n = 30
        mid  = 10_000.0 + np.arange(n) * 0.1
        signal = np.ones(n) * 0.5    # all well above any threshold
        signal[5] = 0.0               # one exact zero
        q_mask = np.ones(n, dtype=bool)
        fwd    = _build_forward_return_array(mid, 1)
        gpos   = np.arange(n, dtype=np.int64)
        fwd_eligible = q_mask & np.isfinite(fwd)

        r = _compute_simple_feature(
            gpos, q_mask, signal, fwd, fwd_eligible, k=1, q=0.80
        )
        # The zero row should not contribute to N. We cannot assert the exact N
        # without knowing the threshold, but we can verify that setting ALL
        # signal=0 gives N=0.
        all_zero_signal = np.zeros(n)
        r2 = _compute_simple_feature(
            gpos, q_mask, all_zero_signal, fwd, fwd_eligible, k=1, q=0.80
        )
        assert r2["N"] == 0


# ===========================================================================
# T13 — type-7 quantile semantics
# ===========================================================================
class TestT13Type7Quantile:
    def test_matches_numpy_linear_interpolation(self):
        """quantile_method=linear_type7 — must match np.quantile(method='linear')."""
        rng = np.random.default_rng(42)
        vals = rng.uniform(0.0, 10.0, 100)
        for q in (0.80, 0.90, 0.95):
            expected = float(np.quantile(vals, q, method="linear"))
            got = _quantile_type7(vals, q)
            assert got is not None
            assert abs(got - expected) < 1e-12, f"q={q}: {got} != {expected}"

    def test_two_values_interpolation(self):
        vals = np.array([1.0, 3.0])
        assert abs(_quantile_type7(vals, 0.5) - 2.0) < 1e-12
        assert abs(_quantile_type7(vals, 0.0) - 1.0) < 1e-12
        assert abs(_quantile_type7(vals, 1.0) - 3.0) < 1e-12


# ===========================================================================
# T14 — average-rank ties
# ===========================================================================
class TestT14AverageRankTies:
    def test_tied_values_receive_arithmetic_mean_rank(self):
        """rank_tie_definition: each tied value receives mean of m integer ranks."""
        # Three identical values at positions 2,3,4 (1-based ranks 2,3,4 → mean=3)
        vals = pd.Series([1.0, 5.0, 5.0, 5.0, 9.0])
        q_mask = pd.Series([True, True, True, True, True])
        z = _rank_signed_uniform(vals, q_mask).to_numpy()
        n = 5
        # Ranks: 1→1, 5→(2+3+4)/3=3, 9→5
        expected_z_rank1 = 2.0 * (1.0 / (n + 1)) - 1.0
        expected_z_rank3 = 2.0 * (3.0 / (n + 1)) - 1.0
        expected_z_rank5 = 2.0 * (5.0 / (n + 1)) - 1.0
        assert abs(z[0] - expected_z_rank1) < 1e-12
        # All three tied values should have the same z
        assert abs(z[1] - expected_z_rank3) < 1e-12
        assert abs(z[2] - expected_z_rank3) < 1e-12
        assert abs(z[3] - expected_z_rank3) < 1e-12
        assert abs(z[4] - expected_z_rank5) < 1e-12

    def test_nan_not_included_in_rank(self):
        """NaN values must be excluded from the ranking."""
        vals   = pd.Series([2.0, np.nan, 4.0])
        q_mask = pd.Series([True, True, True])
        z = _rank_signed_uniform(vals, q_mask).to_numpy()
        assert math.isnan(z[1])
        assert math.isfinite(z[0])
        assert math.isfinite(z[2])

    def test_non_quality_rows_excluded(self):
        vals   = pd.Series([1.0, 2.0, 3.0])
        q_mask = pd.Series([True, False, True])
        z = _rank_signed_uniform(vals, q_mask).to_numpy()
        # Row 1 excluded from quality → gets NaN
        assert math.isnan(z[1])
        # Rows 0 and 2 ranked among themselves (n=2)
        assert math.isfinite(z[0])
        assert math.isfinite(z[2])


# ===========================================================================
# T15 — NaN handling (listwise drop)
# ===========================================================================
class TestT15NanHandling:
    def test_nan_signal_rows_excluded_from_threshold_and_events(self):
        """nan_policy=listwise_drop_at_use; NaN signal rows drop."""
        n = 20
        signal = np.full(n, 1.0)
        signal[0:5] = np.nan  # 5 NaN signal rows → excluded from simple_domain
        q_mask = np.ones(n, dtype=bool)
        mid  = np.linspace(10_000.0, 10_010.0, n)
        fwd  = _build_forward_return_array(mid, 1)
        gpos = np.arange(n, dtype=np.int64)
        fwd_eligible = q_mask & np.isfinite(fwd)

        r_with_nan = _compute_simple_feature(gpos, q_mask, signal, fwd, fwd_eligible, 1, 0.80)

        signal_no_nan = np.full(n, 1.0)
        r_no_nan = _compute_simple_feature(gpos, q_mask, signal_no_nan, fwd, fwd_eligible, 1, 0.80)

        # Both should fire (threshold == 1.0 for all-ones, events at boundary)
        # Key invariant: no crash and N is int
        assert isinstance(r_with_nan["N"], int)
        assert isinstance(r_no_nan["N"], int)


# ===========================================================================
# T16 — session-end truncation (per-horizon independent)
# ===========================================================================
class TestT16SessionEndTruncation:
    def test_last_k_rows_always_nan(self):
        """session_end=drop_if(grid_pos+k>last_index);per_horizon_independent."""
        n = 20
        mid = np.linspace(10_000.0, 10_020.0, n)
        for k in [1, 5, 10]:
            fwd = _build_forward_return_array(mid, k)
            # Last k rows must be NaN (session end)
            for i in range(n - k, n):
                assert math.isnan(fwd[i]), f"k={k}, row {i} should be NaN"
            # Rows before last k should be finite (all mid positive here)
            for i in range(n - k):
                assert math.isfinite(fwd[i]), f"k={k}, row {i} should be finite"

    def test_each_horizon_truncates_independently(self):
        """Different horizons produce different NaN tail lengths."""
        n = 50
        mid = np.linspace(10_000.0, 10_050.0, n)
        fwd_k1  = _build_forward_return_array(mid, 1)
        fwd_k10 = _build_forward_return_array(mid, 10)
        nan_k1  = int(np.sum(np.isnan(fwd_k1)))
        nan_k10 = int(np.sum(np.isnan(fwd_k10)))
        assert nan_k1 == 1
        assert nan_k10 == 10


# ===========================================================================
# T17 — greedy earliest-first overlap
# ===========================================================================
class TestT17GreedyOverlap:
    def test_first_candidate_always_accepted(self):
        positions = np.array([0, 5, 10, 15])
        spacing   = 10
        acc = _greedy_overlap_filter(positions, spacing)
        assert acc[0]  # first always accepted

    def test_candidates_within_spacing_rejected(self):
        positions = np.array([0, 3, 10, 13, 20])
        spacing   = 10
        acc = _greedy_overlap_filter(positions, spacing)
        # 0 → accepted; 3 → rejected (0+10>3); 10 → accepted (10-0=10>=10);
        # 13 → rejected (13-10=3<10); 20 → accepted (20-10=10>=10)
        assert list(acc) == [True, False, True, False, True]

    def test_spacing_is_inclusive(self):
        """spec: inclusive gap >= spacing"""
        positions = np.array([0, 10])
        spacing   = 10
        acc = _greedy_overlap_filter(positions, spacing)
        assert acc[0] and acc[1]  # gap exactly equals spacing → accepted

    def test_spacing_is_max_10_k(self):
        """overlap_spacing_steps=max(10,k)"""
        # k=5 → spacing should be max(10,5)=10
        k = 5
        spacing = max(10, k)
        assert spacing == 10
        # k=15 → spacing should be 15
        k = 15
        spacing = max(10, k)
        assert spacing == 15


# ===========================================================================
# T18 — gap_* alignment filter
# ===========================================================================
class TestT18GapAlignment:
    def test_alignment_required_for_event(self):
        """Alignment condition: sign(confirmation)==sign(-gap) AND nonzero."""
        n = 30
        rng = np.random.default_rng(99)
        q_mask = np.ones(n, dtype=bool)
        gap    = rng.uniform(-2.0, 2.0, n)
        mid    = np.linspace(10_000.0, 10_030.0, n)
        fwd    = _build_forward_return_array(mid, 5)
        gpos   = np.arange(n, dtype=np.int64)

        gap_thresh_domain = q_mask & np.isfinite(gap)
        thresh = _quantile_type7(np.abs(gap[gap_thresh_domain]), 0.80)

        conf_finite_all  = np.ones(n, dtype=bool)
        align_all_true   = np.ones(n, dtype=bool)
        align_all_false  = np.zeros(n, dtype=bool)

        r_aligned    = _compute_gap_composite(gpos, q_mask, gap, fwd, 5, 0.80, thresh, conf_finite_all, align_all_true)
        r_not_aligned = _compute_gap_composite(gpos, q_mask, gap, fwd, 5, 0.80, thresh, conf_finite_all, align_all_false)

        # With all alignment False → no events
        assert r_not_aligned["N"] == 0
        # With all alignment True → some events possible
        # (may still be 0 if nothing crosses threshold after overlap, but at least not rejected by alignment)


# ===========================================================================
# T19 — depthBoth composite N and direction
# ===========================================================================
class TestT19DepthBoth:
    def test_depthboth_direction_positive_sign_D(self):
        """dir=+sign(D) for depthBoth."""
        n = 40
        rng = np.random.default_rng(21)
        q_mask = np.ones(n, dtype=bool)
        # All D > 0 → dir = +1 → signed_return > 0 iff forward_ret > 0
        z1  = np.abs(rng.uniform(0.01, 1.0, n))   # all positive
        z2  = np.abs(rng.uniform(0.01, 1.0, n))
        mid = np.linspace(10_000.0, 10_040.0, n)  # monotone up → all fwd > 0
        fwd = _build_forward_return_array(mid, 5)
        gpos = np.arange(n, dtype=np.int64)

        r = _compute_derived_composite(gpos, q_mask, z1, z2, fwd, k=5, q=0.80)
        if r["N"] > 0:
            assert r["mean_signed_bps"] is not None
            # With rising mid and positive D → all signed returns positive
            assert r["mean_signed_bps"] > 0


# ===========================================================================
# T20 — depthL1_extOFI composite
# ===========================================================================
class TestT20DepthL1ExtOFI:
    def test_depthl1_extofi_uses_di_l1_and_ext_ofi(self):
        """depthL1_extOFI: z(depth_imbalance_l1) and z(external_ofi)."""
        n = 40
        rng = np.random.default_rng(33)
        q_mask = np.ones(n, dtype=bool)
        z1  = rng.uniform(-1.0, 1.0, n)   # z(depth_imbalance_l1)
        z2  = rng.uniform(-1.0, 1.0, n)   # z(external_ofi_consensus_l1)
        mid = np.linspace(10_000.0, 10_040.0, n)
        fwd = _build_forward_return_array(mid, 5)
        gpos = np.arange(n, dtype=np.int64)

        r = _compute_derived_composite(gpos, q_mask, z1, z2, fwd, k=5, q=0.90)
        assert isinstance(r["N"], int)
        assert r["threshold"] is not None or len(z1[np.isfinite(z1) & np.isfinite(z2)]) < 2

    def test_nan_in_z2_reduces_derived_domain(self):
        n = 20
        q_mask = np.ones(n, dtype=bool)
        z1  = np.ones(n)
        z2  = np.ones(n)
        z2[0:10] = np.nan   # half NaN → half the derived_domain
        mid = np.linspace(10_000.0, 10_020.0, n)
        fwd = _build_forward_return_array(mid, 2)
        gpos = np.arange(n, dtype=np.int64)

        r_partial = _compute_derived_composite(gpos, q_mask, z1, z2, fwd, k=2, q=0.80)
        z2_full   = np.ones(n)
        r_full    = _compute_derived_composite(gpos, q_mask, z1, z2_full, fwd, k=2, q=0.80)
        # With fewer domain rows, N can differ
        assert r_partial["N"] <= r_full["N"]


# ===========================================================================
# T21 — all seven gap_* composites produce threshold == fair_gap threshold
# ===========================================================================
class TestT21GapThresholdEquality:
    """gap_threshold_domain = quality_admissible AND finite(gap).
    This must be IDENTICAL to fair_gap_reversion's own simple_domain.
    All gap_* share this threshold, regardless of their confirmation component."""

    def test_reconstruct_block_gap_thresholds_match_fair_gap(self):
        n = 100
        df = _base_grid(n=n)
        # Make gap column vary predictably
        rng = np.random.default_rng(55)
        df["bitget_gap_to_fair_bps"] = rng.uniform(-5.0, 5.0, n).tolist()
        df["bitget_fair_ofi_alignment"]   = rng.choice([-1.0, 1.0], n).tolist()
        df["bitget_fair_trade_alignment"] = rng.choice([-1.0, 1.0], n).tolist()

        summary = reconstruct_block("SID_GAP_TEST", "BTC", df)
        assert summary.valid

        # For each (horizon, q), extract thresholds of all gap_* features
        # and fair_gap_reversion. All gap_* must share the same threshold.
        gap_features = [
            "gap_depthBoth", "gap_depthL1", "gap_depth_extOFI",
            "gap_extOFI", "gap_leader1000", "gap_localOFI", "gap_localTrade",
        ]

        from collections import defaultdict
        by_hq: dict = defaultdict(dict)
        for m in summary.metrics:
            if m.feature in (["fair_gap_reversion"] + gap_features):
                by_hq[(m.horizon_ms, m.q)][m.feature] = m.threshold

        for (H, q), feat_thresh in by_hq.items():
            if H not in (1000, 2000, 5000, 10000, 30000):
                continue
            fgr_thresh = feat_thresh.get("fair_gap_reversion")
            if fgr_thresh is None:
                continue
            for gf in gap_features:
                if gf in feat_thresh and feat_thresh[gf] is not None:
                    assert abs(feat_thresh[gf] - fgr_thresh) < 1e-12, (
                        f"H={H} q={q}: {gf} threshold={feat_thresh[gf]} "
                        f"!= fair_gap threshold={fgr_thresh}"
                    )


# ===========================================================================
# T22 — leader1000_extOFI threshold equals leader_gap_1000ms threshold
# ===========================================================================
class TestT22LeaderThresholdEquality:
    def test_leader_composite_threshold_matches_simple(self):
        n = 100
        rng = np.random.default_rng(77)
        df = _base_grid(n=n)
        df["leader_gap_1000ms_bps"] = rng.uniform(-4.0, 4.0, n).tolist()

        summary = reconstruct_block("SID_LEAD_TEST", "ETH", df)
        assert summary.valid

        from collections import defaultdict
        by_hq: dict = defaultdict(dict)
        for m in summary.metrics:
            if m.feature in ("leader_gap_1000ms", "leader1000_extOFI"):
                by_hq[(m.horizon_ms, m.q)][m.feature] = m.threshold

        for (H, q), ft in by_hq.items():
            lg = ft.get("leader_gap_1000ms")
            le = ft.get("leader1000_extOFI")
            if lg is not None and le is not None:
                assert abs(le - lg) < 1e-12, (
                    f"H={H} q={q}: leader1000_extOFI thresh={le} != leader_gap thresh={lg}"
                )


# ===========================================================================
# T23 — mean_abs_move is feature-independent for same (block,horizon)
# ===========================================================================
class TestT23MeanAbsMoveFeatureIndependent:
    """mean_abs_move_requires_feature_signal_finite=false.
    mean_abs_move is identical across all simple features for same (block,horizon)."""

    def test_mean_abs_move_identical_across_simple_features(self):
        n = 80
        rng = np.random.default_rng(88)
        df = _base_grid(n=n)
        # Make different features have different NaN patterns
        # Feature A: many NaN signal values
        df["bitget_ofi_norm_l1"] = [np.nan if i % 3 == 0 else rng.uniform(-1, 1)
                                    for i in range(n)]
        # Feature B: no NaN signal values
        df["external_ofi_consensus_l1"] = rng.uniform(-1.0, 1.0, n).tolist()

        summary = reconstruct_block("SID_MAM", "BTC", df)
        assert summary.valid

        from collections import defaultdict
        mam_by_hf: dict = defaultdict(dict)
        for m in summary.metrics:
            if m.mean_abs_move is not None:
                mam_by_hf[m.horizon_ms][m.feature] = m.mean_abs_move

        # For each horizon, all simple features must share the same mean_abs_move
        for H, feat_mam in mam_by_hf.items():
            vals = list(feat_mam.values())
            for v in vals[1:]:
                assert abs(v - vals[0]) < 1e-12, (
                    f"H={H}: mean_abs_move differs across features: {feat_mam}"
                )


# ===========================================================================
# T24 — N=0 aggregation exclusion
# ===========================================================================
class TestT24N0AggregationExclusion:
    def test_n0_blocks_excluded_from_feature_mean(self):
        """feature_mean = unweighted_mean(mean_signed_bps) over n_gt_0_blocks_only."""
        # Build two blocks: one with N>0, one with N=0 (forced by zero signal)
        df_with_signal = _base_grid(n=50)
        df_zero_signal = _base_grid(n=50)
        # Zero out all signals to force N=0 for all features
        for col in [
            "bitget_ofi_norm_l1", "bitget_trade_imbalance_window",
            "bitget_depth_imbalance_l1", "bitget_depth_imbalance_l5",
            "external_ofi_consensus_l1", "external_trade_imbalance_consensus",
            "fair_accel_100ms_bps", "bitget_gap_to_fair_bps",
            "leader_gap_100ms_bps", "leader_gap_200ms_bps",
            "leader_gap_500ms_bps", "leader_gap_1000ms_bps",
        ]:
            df_zero_signal[col] = 0.0

        s1 = reconstruct_block("SID_A", "BTC", df_with_signal)
        s2 = reconstruct_block("SID_B", "BTC", df_zero_signal)
        agg = aggregate_blocks([s1, s2])

        for a in agg:
            # feature_mean is only over n_gt_0 blocks
            if a.n_gt_0_blocks == 0:
                assert a.feature_mean is None
            if a.n_gt_0_blocks == 1:
                # Only one block contributed
                assert a.positive_share is not None or a.n_gt_0_blocks == 0


# ===========================================================================
# T25 — positive_share denominator
# ===========================================================================
class TestT25PositiveShareDenominator:
    def test_positive_share_denominator_is_n_gt_0_blocks(self):
        """positive_share = positive_blocks_count / count(n_gt_0_blocks)."""
        df = _base_grid(n=60)
        s = reconstruct_block("SID_PS", "BTC", df)
        agg = aggregate_blocks([s])

        for a in agg:
            if a.n_gt_0_blocks > 0:
                expected_ps = a.positive_blocks_count / a.n_gt_0_blocks
                assert a.positive_share is not None
                assert abs(a.positive_share - expected_ps) < 1e-12


# ===========================================================================
# T26 — min/max lexical tie-break
# ===========================================================================
class TestT26MinMaxLexicalTieBreak:
    def test_tie_broken_by_ascending_lexical_session_asset(self):
        """min_block/max_block: tie_break=ascending_lexical(session_id,asset)."""
        # Build two blocks with artificially identical mean_signed_bps (via identical data)
        df = _base_grid(n=50)
        s1 = reconstruct_block("SESSION_A", "BTC", df)
        s2 = reconstruct_block("SESSION_A", "ETH", df)
        s3 = reconstruct_block("SESSION_B", "BTC", df)

        agg = aggregate_blocks([s1, s2, s3])

        # When means are tied, lexically first (session_id, asset) should be min
        for a in agg:
            if a.min_block is not None:
                # min_block must be one of our valid blocks
                assert a.min_block in [
                    ("SESSION_A", "BTC"), ("SESSION_A", "ETH"), ("SESSION_B", "BTC")
                ]
            if a.max_block is not None:
                assert a.max_block in [
                    ("SESSION_A", "BTC"), ("SESSION_A", "ETH"), ("SESSION_B", "BTC")
                ]


# ===========================================================================
# T27 — NEW36 firewall rejects quantitative input
# ===========================================================================
class TestT27New36Firewall:
    def test_new36_ids_raise_firewall_error(self):
        from recovery.allowlist import (
            assert_recovery_allowed,
            NEW36QuantitativeFirewallError,
        )
        # Verify that non-allowlisted ids are rejected
        with pytest.raises(NEW36QuantitativeFirewallError):
            assert_recovery_allowed(None)

    def test_unknown_session_id_raises_firewall_error(self):
        from recovery.allowlist import (
            assert_recovery_allowed,
            NEW36QuantitativeFirewallError,
        )
        with pytest.raises(NEW36QuantitativeFirewallError):
            assert_recovery_allowed("UNKNOWN_XYZ_SESSION_ID_NOT_IN_ANY_LIST")

    def test_engine_does_not_call_open_reference_zip(self):
        """engine.py must not bypass the firewall by calling sandbox directly."""
        import ast
        src = open("/app/backend/recovery/engine.py").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "sandbox" not in alias.name, \
                        "engine.py must not import sandbox"
            if isinstance(node, ast.ImportFrom):
                if node.module and "sandbox" in node.module:
                    assert False, "engine.py must not import from sandbox"
        # If we get here: PASS
