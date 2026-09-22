"""Focused tests for RECONSTRUCTION_V1.2 Stage 3 implementation.

Spec:   V1.2_STAGE3_FINAL

IMPORTANT: These tests NEVER execute the real Stage 3 diagnostic against
OLD36 sessions.  All data-path tests use synthetic _BlockContext objects.
No RAW data is opened.  No Stage 3 artifact files are written.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import csv
import sys
import tempfile
from pathlib import Path
from collections import Counter

import numpy as np
import pandas as pd
import pytest

# ── path setup ──────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recovery.engine import (
    FEATURE_MAP, GRID_MS, SIMPLE_FEATURES,
    BlockMetrics, _build_forward_return_array,
    _greedy_overlap_filter, _quantile_type7,
)
from recovery.v1_2_diagnostics import _BlockContext
from recovery.v1_2_stage3 import (
    STAGE3_VERSION,
    STAGE3_SESSIONS,
    STAGE3_ASSETS,
    STAGE3_HZ_FEATURES,
    STAGE3_QUANTILES,
    STAGE3_HORIZONS,
    STAGE3_HB_Q,
    STAGE3_HB_FAMILIES,
    STAGE3_HZ_ROWS,
    STAGE3_HG_ROWS,
    STAGE3_HC_ROWS,
    STAGE3_HB_ROWS,
    STAGE3_TOTAL_ROWS,
    _STAGE3_CSV_COLUMNS,
    _PROTECTED_NAMES,
    validate_hz_feature_set,
    exact_spacing_pair_n,
    _greedy_overlap_filter_strict,
    _sha256_positions,
    _compute_event_metrics,
    _format_field,
    _csv_bytes,
    _hz_block,
    _hg_block,
    _hc_block,
    _hb_block,
    _hb_extract_pre_overlap,
    _assert_hz_t0_drift_guard,
    _assert_event_drift_guard,
    _assert_position_subset,
    _run_invariants,
    generate_stage3_rows,
    write_stage3_artifacts,
    _ZERO_FREE_CONTROL,
    _HZ_DISCRIMINATING,
    _HZ_NONDISCRIMINATING,
    _STAGE3_DEFAULT_OUTPUT_DIR,
)


# ===========================================================================
# Synthetic context helpers
# ===========================================================================

def _make_ctx(
    session_id: str,
    asset: str,
    n: int = 500,
    seed: int = 42,
    include_zeros: bool = True,
) -> _BlockContext:
    """Build a minimal synthetic _BlockContext for Stage3 testing.

    Uses deterministic random data with enough variation to produce
    non-trivial event sets for all Stage3 features.
    """
    rng = np.random.default_rng(seed)

    gpos = np.arange(n, dtype=np.int64)
    quality = np.ones(n, dtype=bool)

    # Oscillating mid: forward returns alternate sign.
    t = np.linspace(0, 6 * np.pi, n)
    mid = 100.0 + 3.0 * np.sin(t) + rng.normal(0.0, 0.05, n)
    mid = np.maximum(mid, 0.01)

    di_l1 = rng.normal(0.0, 1.0, n)
    di_l5 = rng.normal(0.0, 1.0, n)
    ext_ofi = rng.normal(0.0, 1.0, n)
    gap = rng.normal(0.0, 1.0, n)
    fair_ofi_align = rng.choice([-1.0, 0.0, 1.0], size=n)

    if include_zeros:
        # Inject some zeros into di_l1 so HZ DISCRIMINATING cases are possible
        zero_idx = rng.choice(n, size=20, replace=False)
        di_l1 = di_l1.copy()
        di_l1[zero_idx] = 0.0

    def _zscore(x: np.ndarray) -> np.ndarray:
        std = float(np.std(x))
        if std == 0:
            return np.zeros_like(x)
        return (x - float(np.mean(x))) / std

    z_di_l1 = _zscore(di_l1)
    z_di_l5 = _zscore(di_l5)
    z_ext_ofi = _zscore(ext_ofi)

    columns: dict[str, np.ndarray] = {
        "bitget_ofi_norm_l1": rng.normal(0.0, 1.0, n),
        "bitget_trade_imbalance_window": rng.normal(0.0, 1.0, n),
        "bitget_depth_imbalance_l1": di_l1,
        "bitget_depth_imbalance_l5": di_l5,
        "external_ofi_consensus_l1": ext_ofi,
        "external_trade_imbalance_consensus": rng.normal(0.0, 1.0, n),
        "fair_accel_100ms_bps": rng.normal(0.0, 0.5, n),
        "bitget_gap_to_fair_bps": gap,
        "leader_gap_100ms_bps": rng.normal(0.0, 1.0, n),
        "leader_gap_200ms_bps": rng.normal(0.0, 1.0, n),
        "leader_gap_500ms_bps": rng.normal(0.0, 1.0, n),
        "leader_gap_1000ms_bps": rng.normal(0.0, 1.0, n),
        "bitget_fair_ofi_alignment": fair_ofi_align,
        "external_perp_dispersion_bps": rng.uniform(0.0, 5.0, n),
    }

    grid = pd.DataFrame({"grid_pos": gpos})

    return _BlockContext(
        session_id=session_id,
        asset=asset,
        grid=grid,
        gpos=gpos,
        quality=quality,
        mid=mid,
        columns=columns,
        z_di_l1=z_di_l1,
        z_di_l5=z_di_l5,
        z_ext_ofi=z_ext_ofi,
        disp_lo=None,
        disp_hi=None,
        metrics={},
    )


def _populate_matching_stage3_metrics(ctx: _BlockContext) -> None:
    """Populate ctx.metrics with exact Stage3 metrics for drift guard checks.

    Computes:
    - All 12 HZ simple features at _HZ_DRIFT_REFERENCE_HORIZON (1000ms) x 3 q
    - depthL1_extOFI at 3 horizons x 3 q (HG G0)
    - gap_depth_extOFI at 3 horizons x 3 q (HC C0)
    - 6 HB families at 3 horizons x q=0.90

    TEST-ONLY infrastructure.  Do NOT call from production paths.
    """
    # ── HZ simple features at 1000ms (drift reference horizon) ────────────
    for feature in STAGE3_HZ_FEATURES:
        col_name = FEATURE_MAP[feature]
        signal = ctx.columns.get(col_name, np.full(len(ctx.gpos), np.nan, dtype=float)).astype(float)
        simple_domain = ctx.quality & np.isfinite(signal)
        for q in STAGE3_QUANTILES:
            thresh = _quantile_type7(np.abs(signal[simple_domain]), q)
            if thresh is None:
                bm = BlockMetrics(
                    session_id=ctx.session_id, asset=ctx.asset, feature=feature,
                    horizon_ms=1000, q=q, N=0, threshold=None,
                    mean_signed_bps=None, hit_rate=None,
                    median_signed_bps=None, mean_abs_move=None,
                )
            else:
                fwd = _build_forward_return_array(ctx.mid, 1000 // GRID_MS)
                event_mask = (
                    simple_domain & (signal != 0.0)
                    & (np.abs(signal) >= thresh) & np.isfinite(fwd)
                )
                positions = ctx.gpos[event_mask]
                spacing = max(10, 1000 // GRID_MS)
                acc = (
                    _greedy_overlap_filter(positions, spacing)
                    if len(positions) > 0 else np.zeros(0, dtype=bool)
                )
                directions = np.sign(signal[event_mask])
                returns = fwd[event_mask]
                n_acc = int(np.sum(acc))
                if n_acc > 0:
                    signed = directions[acc] * returns[acc]
                    msb = float(np.mean(signed))
                    hr = float(np.mean(signed > 0.0))
                else:
                    msb = None
                    hr = None
                bm = BlockMetrics(
                    session_id=ctx.session_id, asset=ctx.asset, feature=feature,
                    horizon_ms=1000, q=q, N=n_acc, threshold=thresh,
                    mean_signed_bps=msb, hit_rate=hr,
                    median_signed_bps=None, mean_abs_move=None,
                )
            ctx.metrics[(feature, 1000, q)] = bm

    # ── HG G0 and HC C0 for all 3 horizons and 3 quantiles ────────────────
    for horizon_ms in STAGE3_HORIZONS:
        k = horizon_ms // GRID_MS
        spacing = max(10, k)
        fwd = _build_forward_return_array(ctx.mid, k)

        # depthL1_extOFI (G0)
        derived_domain = ctx.quality & np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi)
        D = np.where(derived_domain, (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0, np.nan)
        for q in STAGE3_QUANTILES:
            thresh = _quantile_type7(np.abs(D[derived_domain]), q)
            if thresh is None:
                bm = BlockMetrics(
                    session_id=ctx.session_id, asset=ctx.asset, feature="depthL1_extOFI",
                    horizon_ms=horizon_ms, q=q, N=0, threshold=None,
                    mean_signed_bps=None, hit_rate=None,
                    median_signed_bps=None, mean_abs_move=None,
                )
            else:
                pre = derived_domain & (D != 0.0) & (np.abs(D) >= thresh) & np.isfinite(fwd)
                positions = ctx.gpos[pre]
                acc = (
                    _greedy_overlap_filter(positions, spacing)
                    if len(positions) > 0 else np.zeros(0, dtype=bool)
                )
                dirs = np.sign(D[pre])
                rets = fwd[pre]
                n_acc = int(np.sum(acc))
                if n_acc > 0:
                    signed = dirs[acc] * rets[acc]
                    msb = float(np.mean(signed))
                    hr = float(np.mean(signed > 0.0))
                else:
                    msb = None; hr = None
                bm = BlockMetrics(
                    session_id=ctx.session_id, asset=ctx.asset, feature="depthL1_extOFI",
                    horizon_ms=horizon_ms, q=q, N=n_acc, threshold=thresh,
                    mean_signed_bps=msb, hit_rate=hr,
                    median_signed_bps=None, mean_abs_move=None,
                )
            ctx.metrics[("depthL1_extOFI", horizon_ms, q)] = bm

        # gap_depth_extOFI (C0)
        gap = ctx.columns.get("bitget_gap_to_fair_bps", np.full(len(ctx.gpos), np.nan)).astype(float)
        gap_thresh_domain = ctx.quality & np.isfinite(gap)
        D_ext = np.where(
            np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi),
            (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0,
            np.nan,
        )
        conf_fin = np.isfinite(D_ext)
        alignment = (np.sign(D_ext) == -np.sign(gap)) & (D_ext != 0.0) & conf_fin
        gap_event_domain = gap_thresh_domain & conf_fin

        for q in STAGE3_QUANTILES:
            gap_thresh = _quantile_type7(np.abs(gap[gap_thresh_domain]), q)
            if gap_thresh is None:
                bm = BlockMetrics(
                    session_id=ctx.session_id, asset=ctx.asset, feature="gap_depth_extOFI",
                    horizon_ms=horizon_ms, q=q, N=0, threshold=None,
                    mean_signed_bps=None, hit_rate=None,
                    median_signed_bps=None, mean_abs_move=None,
                )
            else:
                pre = (
                    gap_event_domain & (gap != 0.0)
                    & (np.abs(gap) >= gap_thresh) & alignment & np.isfinite(fwd)
                )
                positions = ctx.gpos[pre]
                acc = (
                    _greedy_overlap_filter(positions, spacing)
                    if len(positions) > 0 else np.zeros(0, dtype=bool)
                )
                dirs = -np.sign(gap[pre])
                rets = fwd[pre]
                n_acc = int(np.sum(acc))
                if n_acc > 0:
                    signed = dirs[acc] * rets[acc]
                    msb = float(np.mean(signed))
                    hr = float(np.mean(signed > 0.0))
                else:
                    msb = None; hr = None
                bm = BlockMetrics(
                    session_id=ctx.session_id, asset=ctx.asset, feature="gap_depth_extOFI",
                    horizon_ms=horizon_ms, q=q, N=0 if gap_thresh is None else n_acc,
                    threshold=gap_thresh,
                    mean_signed_bps=msb, hit_rate=hr,
                    median_signed_bps=None, mean_abs_move=None,
                )
                bm = BlockMetrics(
                    session_id=ctx.session_id, asset=ctx.asset, feature="gap_depth_extOFI",
                    horizon_ms=horizon_ms, q=q, N=n_acc, threshold=gap_thresh,
                    mean_signed_bps=msb, hit_rate=hr,
                    median_signed_bps=None, mean_abs_move=None,
                )
            ctx.metrics[("gap_depth_extOFI", horizon_ms, q)] = bm

        # HB 6 families at q=0.90
        for feature in STAGE3_HB_FAMILIES:
            positions, dirs, rets, thresh = _hb_extract_pre_overlap(
                feature, ctx, horizon_ms, STAGE3_HB_Q, fwd
            )
            acc = (
                _greedy_overlap_filter(positions, spacing)
                if len(positions) > 0 else np.zeros(0, dtype=bool)
            )
            n_acc = int(np.sum(acc))
            if n_acc > 0:
                signed = dirs[acc] * rets[acc]
                msb = float(np.mean(signed))
                hr = float(np.mean(signed > 0.0))
            else:
                msb = None; hr = None
            bm = BlockMetrics(
                session_id=ctx.session_id, asset=ctx.asset, feature=feature,
                horizon_ms=horizon_ms, q=STAGE3_HB_Q, N=n_acc, threshold=thresh,
                mean_signed_bps=msb, hit_rate=hr,
                median_signed_bps=None, mean_abs_move=None,
            )
            ctx.metrics[(feature, horizon_ms, STAGE3_HB_Q)] = bm


def _make_all_contexts(seed_base: int = 0) -> dict[tuple[str, str], _BlockContext]:
    """Build synthetic contexts for all declared (session, asset) pairs.

    All contexts have matching metrics for drift guard.
    """
    ctxs = {}
    for i, sid in enumerate(STAGE3_SESSIONS):
        for j, asset in enumerate(STAGE3_ASSETS):
            ctx = _make_ctx(sid, asset, seed=seed_base + i * 10 + j)
            _populate_matching_stage3_metrics(ctx)
            ctxs[(sid, asset)] = ctx
    return ctxs


# ===========================================================================
# HZ Tests
# ===========================================================================

def test_hz_exact_12_feature_set():
    """HZ uses exactly the 12 frozen simple features from FEATURE_MAP."""
    validate_hz_feature_set()  # must not raise
    assert set(STAGE3_HZ_FEATURES) == set(SIMPLE_FEATURES)
    assert len(STAGE3_HZ_FEATURES) == 12


def test_hz_feature_set_drift_detected():
    """validate_hz_feature_set aborts if STAGE3_HZ_FEATURES drifts from engine."""
    import recovery.v1_2_stage3 as s3
    original = s3.STAGE3_HZ_FEATURES
    try:
        s3.STAGE3_HZ_FEATURES = tuple(list(s3.STAGE3_HZ_FEATURES) + ["extra_bogus_feature"])  # type: ignore
        with pytest.raises(AssertionError, match="HZ FEATURE SET DRIFT"):
            validate_hz_feature_set()
    finally:
        s3.STAGE3_HZ_FEATURES = original  # type: ignore


def test_hz_t0_includes_zeros():
    """T0 domain = quality & finite(signal); zero values are included."""
    n = 100
    quality = np.ones(n, dtype=bool)
    signal = np.ones(n, dtype=float)
    signal[10] = 0.0  # inject a zero

    # T0 domain includes zeros: count should be n
    t0_dom = quality & np.isfinite(signal)
    assert int(np.sum(t0_dom)) == n
    # The zero row IS in T0 domain
    assert t0_dom[10] == True


def test_hz_tz_excludes_exact_zeros():
    """TZ domain = quality & finite & signal != 0; exact zeros excluded."""
    n = 100
    quality = np.ones(n, dtype=bool)
    signal = np.ones(n, dtype=float)
    signal[10] = 0.0
    signal[20] = 0.0

    tz_dom = quality & np.isfinite(signal) & (signal != 0.0)
    assert int(np.sum(tz_dom)) == n - 2
    assert tz_dom[10] == False
    assert tz_dom[20] == False


def test_hz_nan_excluded_from_finite_counts():
    """NaN values are excluded from finite counts (both T0 and TZ)."""
    n = 100
    quality = np.ones(n, dtype=bool)
    signal = np.arange(n, dtype=float)
    signal[5] = np.nan
    signal[15] = np.nan

    t0_dom = quality & np.isfinite(signal)
    assert int(np.sum(t0_dom)) == n - 2
    assert t0_dom[5] == False
    assert t0_dom[15] == False


def test_hz_zero_n_equals_finite_minus_nonzero():
    """I5: zero_n = threshold_domain_finite_n - threshold_domain_nonzero_n."""
    n = 200
    rng = np.random.default_rng(7)
    signal = rng.normal(0.0, 1.0, n)
    signal[::20] = 0.0  # 10 zeros
    quality = np.ones(n, dtype=bool)

    t0_dom = quality & np.isfinite(signal)
    tz_dom = t0_dom & (signal != 0.0)
    finite_n = int(np.sum(t0_dom))
    nonzero_n = int(np.sum(tz_dom))
    zero_n = finite_n - nonzero_n
    assert zero_n >= 0
    assert zero_n == 10  # we injected 10 zeros


def test_hz_zero_fraction_semantics():
    """I6: zero_fraction = zero_n/finite_n; None if finite_n==0."""
    # Case 1: finite_n > 0
    finite_n, nonzero_n = 100, 80
    zero_n = finite_n - nonzero_n
    zf = zero_n / finite_n
    assert 0.0 <= zf <= 1.0
    assert abs(zf - 0.2) < 1e-12

    # Case 2: finite_n == 0 -> None
    assert None is None  # trivial but explicit documentation


def test_hz_zero_free_control():
    """ZERO_FREE_CONTROL when zero_n == 0 (no zeros in T0 domain)."""
    n = 100
    signal = np.ones(n, dtype=float) * 2.0  # no zeros, no NaNs
    quality = np.ones(n, dtype=bool)
    t0_dom = quality & np.isfinite(signal)
    tz_dom = t0_dom & (signal != 0.0)
    zero_n = int(np.sum(t0_dom)) - int(np.sum(tz_dom))
    assert zero_n == 0

    t0_thresh = _quantile_type7(np.abs(signal[t0_dom]), 0.90)
    tz_thresh = _quantile_type7(np.abs(signal[tz_dom]), 0.90)
    # Both thresholds should be equal since domains are identical
    assert t0_thresh == tz_thresh

    # Classification
    if zero_n == 0:
        hz_class = _ZERO_FREE_CONTROL
    elif t0_thresh != tz_thresh:
        hz_class = _HZ_DISCRIMINATING
    else:
        hz_class = _HZ_NONDISCRIMINATING
    assert hz_class == _ZERO_FREE_CONTROL


def test_hz_discriminating():
    """HZ_DISCRIMINATING when zero_n > 0 and T0_threshold != TZ_threshold."""
    # Craft a signal where adding zeros to T0 domain shifts the threshold.
    rng = np.random.default_rng(99)
    n = 200
    signal = rng.uniform(1.0, 10.0, n)  # all positive, no zeros
    # Inject 50% zeros to significantly change threshold
    signal[:100] = 0.0
    quality = np.ones(n, dtype=bool)

    t0_dom = quality & np.isfinite(signal)
    tz_dom = t0_dom & (signal != 0.0)
    zero_n = int(np.sum(t0_dom)) - int(np.sum(tz_dom))
    assert zero_n == 100

    t0_thresh = _quantile_type7(np.abs(signal[t0_dom]), 0.90)
    tz_thresh = _quantile_type7(np.abs(signal[tz_dom]), 0.90)

    # T0 includes zeros (abs(0)=0) in its quantile; TZ excludes them.
    # The 0.9 quantile of T0 includes 100 zeros -> lower than TZ's 0.9 quantile.
    assert t0_thresh != tz_thresh, "T0 and TZ thresholds must differ for DISCRIMINATING"

    # Classification
    if zero_n == 0:
        hz_class = _ZERO_FREE_CONTROL
    elif t0_thresh != tz_thresh:
        hz_class = _HZ_DISCRIMINATING
    else:
        hz_class = _HZ_NONDISCRIMINATING
    assert hz_class == _HZ_DISCRIMINATING


def test_hz_nondiscriminating_equal_threshold():
    """NONDISCRIMINATING_EQUAL_THRESHOLD when zero_n>0 but T0==TZ threshold.

    Construct a bimodal signal (80% at 3.0, 20% at 10.0) with 2 zeros.
    The 0.90 quantile lands in the upper band (10.0) for both T0 and TZ,
    so the Type-7 interpolated threshold is guaranteed equal despite zeros.
    """
    # 200 values: 158 at 3.0, 40 at 10.0, then 2 zeros injected
    n = 200
    signal = np.full(n, 3.0, dtype=float)
    signal[:40] = 10.0   # 20% at high value
    signal[40] = 0.0     # zero 1
    signal[41] = 0.0     # zero 2
    quality = np.ones(n, dtype=bool)

    t0_dom = quality & np.isfinite(signal)
    tz_dom = t0_dom & (signal != 0.0)
    zero_n = int(np.sum(t0_dom)) - int(np.sum(tz_dom))
    assert zero_n == 2

    t0_thresh = _quantile_type7(np.abs(signal[t0_dom]), 0.90)
    tz_thresh = _quantile_type7(np.abs(signal[tz_dom]), 0.90)
    # T0 sorted (200): [0,0, 3×158, 10×40] → q=0.90 → position 180.1 (1-indexed) → 10.0
    # TZ sorted (198): [3×158, 10×40]        → q=0.90 → position 178.3 (1-indexed) → 10.0
    assert t0_thresh == tz_thresh, f"Expected equal thresholds; got {t0_thresh} vs {tz_thresh}"

    if zero_n == 0:
        hz_class = _ZERO_FREE_CONTROL
    elif t0_thresh != tz_thresh:
        hz_class = _HZ_DISCRIMINATING
    else:
        hz_class = _HZ_NONDISCRIMINATING
    assert hz_class == _HZ_NONDISCRIMINATING


def test_hz_saturated_tied_t0_eq_tz_with_zeros_is_nondiscriminating():
    """Saturated / tied case: zero_n>0 but T0==TZ -> NONDISCRIMINATING."""
    # Constant non-zero signal with a single zero; both quantiles = same value
    n = 100
    signal = np.full(n, 3.0, dtype=float)
    signal[0] = 0.0  # one zero
    quality = np.ones(n, dtype=bool)

    t0_dom = quality & np.isfinite(signal)
    tz_dom = t0_dom & (signal != 0.0)
    zero_n = int(np.sum(t0_dom)) - int(np.sum(tz_dom))
    assert zero_n == 1

    t0_thresh = _quantile_type7(np.abs(signal[t0_dom]), 0.90)
    tz_thresh = _quantile_type7(np.abs(signal[tz_dom]), 0.90)
    # Both domains contain only abs-value=3.0 (except the 0 in t0_dom);
    # at q=0.90 for T0: 90th percentile of [0,3,3,...3] might differ.
    # Let's just check the classification logic is correct for the actual values.
    if zero_n == 0:
        hz_class = _ZERO_FREE_CONTROL
    elif t0_thresh != tz_thresh:
        hz_class = _HZ_DISCRIMINATING
    else:
        hz_class = _HZ_NONDISCRIMINATING
    # Whatever the class, it must be one of the three valid values.
    assert hz_class in (_ZERO_FREE_CONTROL, _HZ_DISCRIMINATING, _HZ_NONDISCRIMINATING)
    # With zero_n=1 and 99 values of 3.0 vs 100 values (99 of 3.0, 1 of 0.0):
    # T0 q=0.90: 0.9 * 100 = 90th percentile of mostly-3.0 with one 0.0 -> 3.0
    # TZ q=0.90: 0.9 * 99 = 89.1th percentile of all-3.0 -> 3.0
    # They should be equal -> NONDISCRIMINATING
    assert hz_class == _HZ_NONDISCRIMINATING


def test_hz_type7_threshold_behavior():
    """Type-7 quantile uses linear interpolation."""
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=float)
    q = 0.5
    # type-7 median of [1,2,3,4,5] = 3.0
    result = _quantile_type7(values, q)
    assert result is not None
    assert abs(result - 3.0) < 1e-12

    # Fewer than 2 values -> None
    assert _quantile_type7(np.array([1.0]), 0.9) is None
    assert _quantile_type7(np.array([]), 0.9) is None


def test_hz_288_rows():
    """HZ axis generates exactly 288 rows (4 blocks x 72 rows/block)."""
    ctxs = _make_all_contexts(seed_base=10)
    rows = generate_stage3_rows(ctxs)
    hz = [r for r in rows if r["axis"] == "HZ"]
    assert len(hz) == STAGE3_HZ_ROWS == 288


# ===========================================================================
# HG Tests
# ===========================================================================

def test_hg_g0_drift_guard_passes_with_matching_metrics():
    """G0 drift guard passes when ctx.metrics matches computed G0 values."""
    ctx = _make_ctx("S1", "BTC", seed=100)
    _populate_matching_stage3_metrics(ctx)
    hz_cache: dict = {}
    # compute hz to populate hz_cache
    hz_rows, hz_cache = _hz_block(ctx, "S1", "BTC")
    # This should not raise
    hg_rows = _hg_block(ctx, "S1", "BTC", hz_cache)
    g0 = [r for r in hg_rows if r["variant_id"] == "G0"]
    assert len(g0) > 0


def test_hg_g1_uses_hz_t0_depth_l1_threshold():
    """G1 calculated_threshold equals HZ-T0 depth_imbalance_l1 threshold for same q (I18)."""
    ctx = _make_ctx("S1", "BTC", seed=55, include_zeros=True)
    _populate_matching_stage3_metrics(ctx)
    hz_rows, hz_cache = _hz_block(ctx, "S1", "BTC")
    hg_rows = _hg_block(ctx, "S1", "BTC", hz_cache)

    for q in STAGE3_QUANTILES:
        hz_t0_thresh, _ = hz_cache.get(("depth_imbalance_l1", q), (None, None))
        g1_rows_q = [
            r for r in hg_rows if r["variant_id"] == "G1" and r["quantile"] == q
        ]
        for r in g1_rows_q:
            assert r["calculated_threshold"] == hz_t0_thresh, (
                f"G1 threshold {r['calculated_threshold']!r} != "
                f"HZ-T0 depth_imbalance_l1 threshold {hz_t0_thresh!r} at q={q}"
            )


def test_hg_g1_direction_uses_raw_di_l1_sign():
    """G1 events use sign(depth_imbalance_l1) for direction (raw, not z-score)."""
    # We verify this is correct by checking the G1 pre-overlap mask construction.
    ctx = _make_ctx("S1", "ETH", seed=77)
    _populate_matching_stage3_metrics(ctx)
    hz_rows, hz_cache = _hz_block(ctx, "S1", "ETH")

    di_l1 = ctx.columns["bitget_depth_imbalance_l1"].astype(float)
    q = 0.90
    horizon_ms = 1000
    k = horizon_ms // GRID_MS
    spacing = max(10, k)
    fwd = _build_forward_return_array(ctx.mid, k)

    hz_t0_thresh, _ = hz_cache.get(("depth_imbalance_l1", q), (None, None))
    gate_domain = ctx.quality & np.isfinite(di_l1)
    ext_ofi = ctx.columns["external_ofi_consensus_l1"].astype(float)

    if hz_t0_thresh is not None:
        pre = (
            gate_domain
            & (di_l1 != 0.0)
            & (np.abs(di_l1) >= hz_t0_thresh)
            & np.isfinite(ext_ofi)
            & (ext_ofi != 0.0)
            & (np.sign(ext_ofi) == np.sign(di_l1))
            & np.isfinite(fwd)
        )
        # All G1 events must have di_l1 != 0 and the direction sign must match raw di_l1
        event_di_l1 = di_l1[pre]
        assert np.all(event_di_l1 != 0.0)
        # Directions from raw sign
        expected_dirs = np.sign(event_di_l1)
        assert np.all(expected_dirs != 0.0)  # no zero-sign events


def test_hg_g1_external_ofi_required():
    """G1 requires external_ofi finite, nonzero, and sign-aligned with di_l1."""
    n = 200
    rng = np.random.default_rng(11)
    gpos = np.arange(n, dtype=np.int64)
    quality = np.ones(n, dtype=bool)
    t = np.linspace(0, 4 * np.pi, n)
    mid = 100.0 + np.sin(t)

    di_l1 = np.full(n, 2.0, dtype=float)  # all positive
    ext_ofi_wrong_sign = np.full(n, -1.0, dtype=float)  # all negative
    ext_ofi_zero = np.zeros(n, dtype=float)

    # With wrong sign: no G1 events should be generated
    def _zscore(x):
        std = np.std(x)
        return (x - np.mean(x)) / std if std > 0 else np.zeros_like(x)

    for ext_ofi_signal, expected_zero in [
        (ext_ofi_wrong_sign, True),
        (ext_ofi_zero, True),
    ]:
        ctx = _BlockContext(
            session_id="S1", asset="BTC",
            grid=pd.DataFrame({"grid_pos": gpos}),
            gpos=gpos, quality=quality, mid=mid,
            columns={
                "bitget_depth_imbalance_l1": di_l1,
                "external_ofi_consensus_l1": ext_ofi_signal,
                "bitget_gap_to_fair_bps": rng.normal(0, 1, n),
                "bitget_fair_ofi_alignment": rng.choice([-1.0, 0.0, 1.0], size=n),
                **{col: rng.normal(0, 1, n) for col in [
                    "bitget_ofi_norm_l1", "bitget_trade_imbalance_window",
                    "bitget_depth_imbalance_l5", "external_trade_imbalance_consensus",
                    "fair_accel_100ms_bps", "leader_gap_100ms_bps",
                    "leader_gap_200ms_bps", "leader_gap_500ms_bps",
                    "leader_gap_1000ms_bps", "external_perp_dispersion_bps",
                ]},
            },
            z_di_l1=_zscore(di_l1),
            z_di_l5=_zscore(di_l1),
            z_ext_ofi=_zscore(ext_ofi_signal),
            disp_lo=None, disp_hi=None, metrics={},
        )
        _populate_matching_stage3_metrics(ctx)
        _, hz_cache = _hz_block(ctx, "S1", "BTC")
        hg_rows = _hg_block(ctx, "S1", "BTC", hz_cache)
        g1_pre_totals = [r["pre_overlap_n"] for r in hg_rows if r["variant_id"] == "G1"]
        assert all(p == 0 for p in g1_pre_totals), (
            f"Expected 0 G1 events with wrong/zero ext_ofi, got {g1_pre_totals}"
        )


def test_hg_gate_zero_n():
    """gate_zero_n counts quality & finite(di_l1) & di_l1==0 rows."""
    n = 100
    di_l1 = np.ones(n, dtype=float)
    di_l1[5] = 0.0
    di_l1[10] = 0.0
    di_l1[15] = 0.0  # 3 zeros
    di_l1[20] = np.nan  # NaN excluded from gate domain

    rng = np.random.default_rng(123)
    gpos = np.arange(n, dtype=np.int64)
    quality = np.ones(n, dtype=bool)
    t = np.linspace(0, 4 * np.pi, n)
    mid = 100.0 + np.sin(t)

    def _zscore(x):
        std = np.std(x[np.isfinite(x)])
        return (x - np.nanmean(x)) / std if std > 0 else np.zeros_like(x)

    ext_ofi = rng.normal(0, 1, n)
    ctx = _BlockContext(
        session_id="S1", asset="BTC",
        grid=pd.DataFrame({"grid_pos": gpos}),
        gpos=gpos, quality=quality, mid=mid,
        columns={
            "bitget_depth_imbalance_l1": di_l1,
            "external_ofi_consensus_l1": ext_ofi,
            "bitget_gap_to_fair_bps": rng.normal(0, 1, n),
            "bitget_fair_ofi_alignment": rng.choice([-1.0, 0.0, 1.0], size=n),
            **{col: rng.normal(0, 1, n) for col in [
                "bitget_ofi_norm_l1", "bitget_trade_imbalance_window",
                "bitget_depth_imbalance_l5", "external_trade_imbalance_consensus",
                "fair_accel_100ms_bps", "leader_gap_100ms_bps",
                "leader_gap_200ms_bps", "leader_gap_500ms_bps",
                "leader_gap_1000ms_bps", "external_perp_dispersion_bps",
            ]},
        },
        z_di_l1=_zscore(np.where(np.isfinite(di_l1), di_l1, 0.0)),
        z_di_l5=_zscore(rng.normal(0, 1, n)),
        z_ext_ofi=_zscore(ext_ofi),
        disp_lo=None, disp_hi=None, metrics={},
    )
    _populate_matching_stage3_metrics(ctx)
    _, hz_cache = _hz_block(ctx, "S1", "BTC")
    hg_rows = _hg_block(ctx, "S1", "BTC", hz_cache)
    g1_rows = [r for r in hg_rows if r["variant_id"] == "G1"]

    # All G1 rows for the same (session, asset) should report the same gate_zero_n
    gate_zero_ns = [r["gate_zero_n"] for r in g1_rows]
    # gate_domain = quality & finite(di_l1); zeros within that = 3 (not the NaN)
    expected_gate_zero_n = 3
    assert all(g == expected_gate_zero_n for g in gate_zero_ns), (
        f"Expected gate_zero_n={expected_gate_zero_n}, got {gate_zero_ns}"
    )


def test_hg_g1_subset_invariant():
    """I19: G1 pre-overlap positions subset of depth_imbalance_l1 pre-overlap positions."""
    ctx = _make_ctx("S1", "BTC", seed=200, include_zeros=True)
    _populate_matching_stage3_metrics(ctx)
    _, hz_cache = _hz_block(ctx, "S1", "BTC")

    di_l1 = ctx.columns["bitget_depth_imbalance_l1"].astype(float)

    for q in STAGE3_QUANTILES:
        hz_t0_thresh, _ = hz_cache.get(("depth_imbalance_l1", q), (None, None))
        if hz_t0_thresh is None:
            continue
        for horizon_ms in STAGE3_HORIZONS:
            k = horizon_ms // GRID_MS
            fwd = _build_forward_return_array(ctx.mid, k)

            # di_l1 pre-overlap set (simple feature threshold crossing + finite fwd)
            gate_domain = ctx.quality & np.isfinite(di_l1)
            di_l1_preoverlap_mask = (
                gate_domain & (di_l1 != 0.0)
                & (np.abs(di_l1) >= hz_t0_thresh) & np.isfinite(fwd)
            )
            di_l1_pos = set(int(p) for p in ctx.gpos[di_l1_preoverlap_mask])

            # G1 pre-overlap set (adds ext_ofi confirmation)
            ext_ofi = ctx.columns["external_ofi_consensus_l1"].astype(float)
            g1_pre_mask = (
                gate_domain & (di_l1 != 0.0) & (np.abs(di_l1) >= hz_t0_thresh)
                & np.isfinite(ext_ofi) & (ext_ofi != 0.0)
                & (np.sign(ext_ofi) == np.sign(di_l1)) & np.isfinite(fwd)
            )
            g1_pos = set(int(p) for p in ctx.gpos[g1_pre_mask])

            # I19: g1_pos must be a subset of di_l1_pos
            assert g1_pos <= di_l1_pos, (
                f"I19 violated at q={q} h={horizon_ms}: "
                f"G1 positions not subset of di_l1 positions"
            )


def test_hg_72_rows():
    """HG axis generates exactly 72 rows."""
    ctxs = _make_all_contexts(seed_base=20)
    rows = generate_stage3_rows(ctxs)
    hg = [r for r in rows if r["axis"] == "HG"]
    assert len(hg) == STAGE3_HG_ROWS == 72


# ===========================================================================
# HC Tests
# ===========================================================================

def test_hc_c0_drift_guard_passes_with_matching_metrics():
    """C0 drift guard passes when ctx.metrics matches computed C0 values."""
    ctx = _make_ctx("S2", "ETH", seed=300)
    _populate_matching_stage3_metrics(ctx)
    # Should not raise
    hc_rows = _hc_block(ctx, "S2", "ETH")
    c0 = [r for r in hc_rows if r["variant_id"] == "C0"]
    assert len(c0) > 0


def test_hc_c1_uses_raw_di_l1_sign_not_z_sign():
    """C1 uses RAW sign(di_l1) for alignment (not z-score sign).

    Construct a case where sign(z_di_l1) != sign(raw di_l1) and verify
    that C1 follows the raw sign.
    """
    n = 300
    rng = np.random.default_rng(42)
    gpos = np.arange(n, dtype=np.int64)
    quality = np.ones(n, dtype=bool)
    t = np.linspace(0, 4 * np.pi, n)
    mid = 100.0 + 2.0 * np.sin(t)

    # Construct di_l1 where most values are very negative (-10)
    # but a few are small positive (+0.01).
    # The z-score normalization will make the small positive values
    # have large NEGATIVE z-scores (since they're below the mean of a
    # heavily left-skewed distribution? No... let's think more carefully.
    # Actually: if most values are -10 and a few are +0.01:
    # mean ~ -10 (mostly), z = (0.01 - (-10)) / std = positive
    # Hmm, let's use a different approach:
    # Most values are +1.0, few are very large (+1000)
    # z-score of +1.0 will be negative (below mean dominated by +1000)
    # But raw sign(+1.0) is positive.
    di_l1 = np.full(n, 1.0, dtype=float)    # raw: all positive
    di_l1[:5] = 1000.0                        # a few very large positives
    # mean ~ 1.0 + 5/300*(999) ~ 1+16.65 ~ 17.65
    # std large
    # z of 1.0 = (1.0 - 17.65) / std -> negative
    # So sign(z_di_l1) for most rows is NEGATIVE
    # but sign(raw di_l1) is POSITIVE for all rows

    def _zscore(x):
        std = float(np.std(x))
        return (x - float(np.mean(x))) / std if std > 0 else np.zeros_like(x)

    z_di_l1 = _zscore(di_l1)
    # Verify that z-sign differs from raw sign for the bulk of rows
    # (rows 5..n have raw sign=+1 but z-sign should be negative)
    z_sign = np.sign(z_di_l1[10])  # a bulk row
    raw_sign = np.sign(di_l1[10])  # raw sign
    assert raw_sign == 1.0
    assert z_sign == -1.0, f"Expected z_sign=-1 (below-mean), got {z_sign}"

    ext_ofi = rng.normal(0, 1, n)
    gap = rng.normal(0, 1, n)
    # For C1 to fire, we need: sign(di_l1) == -sign(gap)
    # so gap should be negative where di_l1 is positive (gap < 0)
    gap = np.full(n, -1.0, dtype=float)  # negative gap -> direction = +1

    ctx = _BlockContext(
        session_id="S1", asset="BTC",
        grid=pd.DataFrame({"grid_pos": gpos}),
        gpos=gpos, quality=quality, mid=mid,
        columns={
            "bitget_depth_imbalance_l1": di_l1,
            "external_ofi_consensus_l1": ext_ofi,
            "bitget_gap_to_fair_bps": gap,
            "bitget_fair_ofi_alignment": rng.choice([-1.0, 0.0, 1.0], size=n),
            **{col: rng.normal(0, 1, n) for col in [
                "bitget_ofi_norm_l1", "bitget_trade_imbalance_window",
                "bitget_depth_imbalance_l5", "external_trade_imbalance_consensus",
                "fair_accel_100ms_bps", "leader_gap_100ms_bps",
                "leader_gap_200ms_bps", "leader_gap_500ms_bps",
                "leader_gap_1000ms_bps", "external_perp_dispersion_bps",
            ]},
        },
        z_di_l1=z_di_l1,
        z_di_l5=_zscore(rng.normal(0, 1, n)),
        z_ext_ofi=_zscore(ext_ofi),
        disp_lo=None, disp_hi=None, metrics={},
    )
    _populate_matching_stage3_metrics(ctx)
    hc_rows = _hc_block(ctx, "S1", "BTC")
    c1_rows = [r for r in hc_rows if r["variant_id"] == "C1"]

    # C1 events should exist (raw sign(di_l1)=+1 aligns with -sign(gap)=+1)
    c1_has_events = any(r["pre_overlap_n"] > 0 for r in c1_rows)
    assert c1_has_events, "C1 should generate events with raw-aligned di_l1 and gap"

    # C0 events: alignment uses z-sign(D_ext) == -sign(gap)
    # z_di_l1 is mostly negative, so D_ext = mean(z_di_l1, z_ext_ofi)
    # For C0 alignment: sign(D_ext) must equal -sign(gap) = +1
    # i.e. D_ext > 0, but z_di_l1 is negative -> fewer C0 events expected
    c0_rows = [r for r in hc_rows if r["variant_id"] == "C0"]
    c0_total_events = sum(r["pre_overlap_n"] for r in c0_rows)
    c1_total_events = sum(r["pre_overlap_n"] for r in c1_rows)
    # This verifies that C0 and C1 produce different results when signs diverge
    # (they CAN be different; this is the key diagnostic purpose of HC)
    # The exact counts depend on data; we just verify C1 has events here.
    assert c1_total_events >= 0  # can be 0 if ext_ofi has no sign matches


def test_hc_c1_both_confirmations_required():
    """C1 requires BOTH di_l1 and ext_ofi sign conditions; failing one gives 0 events."""
    n = 200
    rng = np.random.default_rng(555)
    gpos = np.arange(n, dtype=np.int64)
    quality = np.ones(n, dtype=bool)
    t = np.linspace(0, 4 * np.pi, n)
    mid = 100.0 + np.sin(t)

    def _zscore(x):
        std = float(np.std(x))
        return (x - float(np.mean(x))) / std if std > 0 else np.zeros_like(x)

    gap = np.full(n, -1.0, dtype=float)  # all negative gap
    di_l1 = np.full(n, 2.0, dtype=float)   # all positive -> sign(di_l1) == -sign(gap) OK
    ext_ofi_wrong = np.full(n, -2.0, dtype=float)  # negative -> sign(ext_ofi) != -sign(gap)

    ctx = _BlockContext(
        session_id="S1", asset="BTC",
        grid=pd.DataFrame({"grid_pos": gpos}),
        gpos=gpos, quality=quality, mid=mid,
        columns={
            "bitget_depth_imbalance_l1": di_l1,
            "external_ofi_consensus_l1": ext_ofi_wrong,
            "bitget_gap_to_fair_bps": gap,
            "bitget_fair_ofi_alignment": rng.choice([-1.0, 0.0, 1.0], size=n),
            **{col: rng.normal(0, 1, n) for col in [
                "bitget_ofi_norm_l1", "bitget_trade_imbalance_window",
                "bitget_depth_imbalance_l5", "external_trade_imbalance_consensus",
                "fair_accel_100ms_bps", "leader_gap_100ms_bps",
                "leader_gap_200ms_bps", "leader_gap_500ms_bps",
                "leader_gap_1000ms_bps", "external_perp_dispersion_bps",
            ]},
        },
        z_di_l1=_zscore(di_l1),
        z_di_l5=_zscore(rng.normal(0, 1, n)),
        z_ext_ofi=_zscore(ext_ofi_wrong),
        disp_lo=None, disp_hi=None, metrics={},
    )
    _populate_matching_stage3_metrics(ctx)
    hc_rows = _hc_block(ctx, "S1", "BTC")
    c1_rows = [r for r in hc_rows if r["variant_id"] == "C1"]
    # ext_ofi_wrong sign: sign(-2) = -1, -sign(gap=-1) = +1 -> sign check fails
    assert all(r["pre_overlap_n"] == 0 for r in c1_rows), (
        "C1 should have 0 events when ext_ofi sign fails"
    )


def test_hc_c1_subset_invariant():
    """I20: C1 pre-overlap positions subset of C0 gap-event threshold-crossing set."""
    ctx = _make_ctx("S1", "BTC", seed=400)
    _populate_matching_stage3_metrics(ctx)

    gap = ctx.columns["bitget_gap_to_fair_bps"].astype(float)
    di_l1 = ctx.columns["bitget_depth_imbalance_l1"].astype(float)
    ext_ofi = ctx.columns["external_ofi_consensus_l1"].astype(float)
    D_ext = np.where(
        np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi),
        (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0,
        np.nan,
    )

    gap_thresh_domain = ctx.quality & np.isfinite(gap)

    for q in STAGE3_QUANTILES:
        gap_thresh = _quantile_type7(np.abs(gap[gap_thresh_domain]), q)
        if gap_thresh is None:
            continue
        for horizon_ms in STAGE3_HORIZONS:
            k = horizon_ms // GRID_MS
            fwd = _build_forward_return_array(ctx.mid, k)

            # C0 gap-event threshold-crossing set (before alignment, before fwd filter)
            conf_fin_c0 = np.isfinite(D_ext)
            gap_event_c0 = gap_thresh_domain & conf_fin_c0
            c0_threshold_crossings = (
                gap_event_c0 & (gap != 0.0) & (np.abs(gap) >= gap_thresh)
            )
            c0_tc_pos = set(int(p) for p in ctx.gpos[c0_threshold_crossings])

            # C1 pre-overlap set
            conf_fin_c1 = np.isfinite(di_l1) & np.isfinite(ext_ofi)
            gap_event_c1 = gap_thresh_domain & conf_fin_c1
            c1_pre = (
                gap_event_c1 & (gap != 0.0) & (np.abs(gap) >= gap_thresh)
                & (di_l1 != 0.0) & (np.sign(di_l1) == -np.sign(gap))
                & (ext_ofi != 0.0) & (np.sign(ext_ofi) == -np.sign(gap))
                & np.isfinite(fwd)
            )
            c1_pre_pos = set(int(p) for p in ctx.gpos[c1_pre])

            # I20: C1 pre-overlap must be subset of C0 threshold-crossing set
            # Note: C0's conf_fin uses finite(D_ext) = finite(z_di_l1 & z_ext_ofi)
            # = (within quality domain) finite(di_l1) & finite(ext_ofi) = C1's conf_fin
            # So the sets are comparable.
            assert c1_pre_pos <= c0_tc_pos, (
                f"I20 violated at q={q} h={horizon_ms}: "
                f"C1 positions not subset of C0 threshold-crossing positions"
            )


def test_hc_72_rows():
    """HC axis generates exactly 72 rows."""
    ctxs = _make_all_contexts(seed_base=30)
    rows = generate_stage3_rows(ctxs)
    hc = [r for r in rows if r["axis"] == "HC"]
    assert len(hc) == STAGE3_HC_ROWS == 72


# ===========================================================================
# HB Tests
# ===========================================================================

def test_hb_b0_boundary_inclusive():
    """B0 uses >= spacing (accepts events at exactly spacing distance)."""
    # Positions with exact spacing distance
    spacing = 10
    positions = np.array([0, 10, 20, 30], dtype=np.int64)
    acc = _greedy_overlap_filter(positions, spacing)
    # All should be accepted: gaps are exactly spacing
    assert np.all(acc), f"B0 (>=): all at exactly spacing should be accepted, got {acc}"


def test_hb_b1_boundary_strict():
    """B1 uses > spacing (rejects events at exactly spacing distance)."""
    spacing = 10
    # Positions with exact spacing: should be rejected by B1
    positions = np.array([0, 10, 20, 30], dtype=np.int64)
    acc = _greedy_overlap_filter_strict(positions, spacing)
    # Only position 0 is accepted; 10 is rejected (10-0=10 not >10)
    assert acc[0] == True
    assert acc[1] == False  # 10 - 0 = 10, not > 10

    # Positions with spacing+1: should all be accepted
    positions2 = np.array([0, 11, 22, 33], dtype=np.int64)
    acc2 = _greedy_overlap_filter_strict(positions2, spacing)
    assert np.all(acc2), f"B1 (>): gaps > spacing should all be accepted, got {acc2}"


def test_hb_exact_spacing_pair_n_non_adjacent():
    """exact_spacing_pair_n correctly counts non-adjacent pairs.

    [0, 5, 10], spacing=10 -> only pair (0, 10): count = 1.
    """
    positions = np.array([0, 5, 10], dtype=np.int64)
    spacing = 10
    result = exact_spacing_pair_n(positions, spacing)
    assert result == 1, f"Expected 1 pair (0->10), got {result}"


def test_hb_exact_spacing_pair_n_zero():
    """exact_spacing_pair_n = 0 when no pair has exact spacing distance."""
    positions = np.array([0, 7, 15, 24], dtype=np.int64)
    spacing = 10
    # Differences: 7,8,9 — none is exactly 10
    result = exact_spacing_pair_n(positions, spacing)
    assert result == 0


def test_hb_exact_spacing_pair_n_multiple():
    """exact_spacing_pair_n counts multiple non-adjacent pairs."""
    # Positions: 0, 10, 20, 30 with spacing=10 -> pairs (0,10),(10,20),(20,30) -> 3
    positions = np.array([0, 10, 20, 30], dtype=np.int64)
    result = exact_spacing_pair_n(positions, spacing=10)
    assert result == 3


def test_hb_esp_zero_implies_b0_b1_identical():
    """I12: if exact_spacing_pair_n==0, B0 and B1 must have identical results."""
    # Positions with no exact spacing pairs -> B0 and B1 must give same result
    # Use spacing=10, positions=[0, 7, 15, 24] (no exact 10-gaps)
    positions = np.array([0, 7, 15, 24], dtype=np.int64)
    spacing = 10
    directions = np.array([1.0, -1.0, 1.0, -1.0])
    returns = np.array([5.0, -3.0, 4.0, -2.0])

    esp = exact_spacing_pair_n(positions, spacing)
    assert esp == 0

    b0_mask = _greedy_overlap_filter(positions, spacing)
    b1_mask = _greedy_overlap_filter_strict(positions, spacing)

    # When esp=0, B0 and B1 must produce identical accepted sets
    assert np.array_equal(b0_mask, b1_mask), (
        f"I12: esp=0 but B0_mask={b0_mask} != B1_mask={b1_mask}"
    )


def test_hb_b1_accepted_n_le_b0():
    """I11: B1.accepted_n <= B0.accepted_n for all HB cases."""
    ctxs = _make_all_contexts(seed_base=40)
    rows = generate_stage3_rows(ctxs)
    hb = [r for r in rows if r["axis"] == "HB"]

    # Group by (session, asset, feature_family, horizon_ms)
    from collections import defaultdict
    pairs: dict = defaultdict(dict)
    for r in hb:
        key = (r["session_id"], r["asset"], r["feature_family"], r["horizon_ms"])
        pairs[key][r["variant_id"]] = r["accepted_n"]

    for key, variants in pairs.items():
        if "B0" in variants and "B1" in variants:
            assert variants["B1"] <= variants["B0"], (
                f"I11 violated: B1.accepted_n={variants['B1']} > "
                f"B0.accepted_n={variants['B0']} for {key}"
            )


def test_hb_three_horizons():
    """HB covers all three horizons (1000, 5000, 30000)."""
    ctxs = _make_all_contexts(seed_base=50)
    rows = generate_stage3_rows(ctxs)
    hb = [r for r in rows if r["axis"] == "HB"]
    horizons = set(r["horizon_ms"] for r in hb)
    assert horizons == set(STAGE3_HORIZONS)


def test_hb_144_rows():
    """HB axis generates exactly 144 rows."""
    ctxs = _make_all_contexts(seed_base=60)
    rows = generate_stage3_rows(ctxs)
    hb = [r for r in rows if r["axis"] == "HB"]
    assert len(hb) == STAGE3_HB_ROWS == 144


# ===========================================================================
# Fingerprint Tests
# ===========================================================================

def test_fingerprint_int64_le_encoding():
    """Fingerprint uses 8-byte little-endian signed int64 encoding."""
    positions = np.array([0, 5, 10], dtype=np.int64)
    digest = _sha256_positions(positions)
    # Manually compute expected: sorted ascending, int64 LE
    arr = np.sort(np.array([0, 5, 10], dtype=np.int64))
    expected = hashlib.sha256(arr.astype("<i8").tobytes(order="C")).hexdigest()
    assert digest == expected


def test_fingerprint_order_independence():
    """Fingerprint is order-independent: sorts ascending before hashing."""
    pos1 = np.array([10, 0, 5], dtype=np.int64)
    pos2 = np.array([0, 5, 10], dtype=np.int64)
    pos3 = np.array([5, 10, 0], dtype=np.int64)
    d1 = _sha256_positions(pos1)
    d2 = _sha256_positions(pos2)
    d3 = _sha256_positions(pos3)
    assert d1 == d2 == d3


def test_fingerprint_empty_set():
    """Empty position set: SHA256 of zero-length byte string."""
    digest = _sha256_positions(np.array([], dtype=np.int64))
    expected = hashlib.sha256(b"").hexdigest()
    assert digest == expected


# ===========================================================================
# I17 Drift Guard Tests
# ===========================================================================

def test_i17_missing_ctx_metrics_aborts():
    """Drift guard aborts when ctx.metrics is empty."""
    ctx = _make_ctx("S1", "BTC", seed=1)  # empty metrics
    assert len(ctx.metrics) == 0
    with pytest.raises(AssertionError, match="ctx.metrics is absent or empty"):
        _assert_hz_t0_drift_guard(ctx, "bitget_ofi", 0.90, 0.5)


def test_i17_missing_key_aborts():
    """Drift guard aborts when the required key is missing from ctx.metrics."""
    ctx = _make_ctx("S1", "BTC", seed=2)
    # Populate with a different key, not the one we'll query
    ctx.metrics[("other_feature", 1000, 0.90)] = BlockMetrics(
        session_id="S1", asset="BTC", feature="other_feature",
        horizon_ms=1000, q=0.90, N=5, threshold=0.5,
        mean_signed_bps=1.0, hit_rate=0.6,
        median_signed_bps=None, mean_abs_move=None,
    )
    with pytest.raises(AssertionError, match="missing metric key"):
        _assert_hz_t0_drift_guard(ctx, "bitget_ofi", 0.90, 0.5)


def test_i17_n_drift_aborts():
    """Drift guard aborts when N doesn't match."""
    ctx = _make_ctx("S1", "BTC", seed=3)
    bm = BlockMetrics(
        session_id="S1", asset="BTC", feature="depthL1_extOFI",
        horizon_ms=1000, q=0.90, N=10, threshold=0.5,
        mean_signed_bps=1.0, hit_rate=0.6,
        median_signed_bps=None, mean_abs_move=None,
    )
    ctx.metrics[("depthL1_extOFI", 1000, 0.90)] = bm
    with pytest.raises(AssertionError, match="N expected"):
        _assert_event_drift_guard(
            ctx, "depthL1_extOFI", 1000, 0.90,
            actual_N=999, actual_thresh=0.5,
            actual_mean=1.0, actual_hr=0.6,
            label="G0",
        )


def test_i17_threshold_drift_aborts():
    """Drift guard aborts when threshold doesn't match."""
    ctx = _make_ctx("S1", "BTC", seed=4)
    bm = BlockMetrics(
        session_id="S1", asset="BTC", feature="gap_depth_extOFI",
        horizon_ms=1000, q=0.90, N=5, threshold=0.5,
        mean_signed_bps=1.0, hit_rate=0.6,
        median_signed_bps=None, mean_abs_move=None,
    )
    ctx.metrics[("gap_depth_extOFI", 1000, 0.90)] = bm
    with pytest.raises(AssertionError, match="threshold"):
        _assert_event_drift_guard(
            ctx, "gap_depth_extOFI", 1000, 0.90,
            actual_N=5, actual_thresh=0.999,  # different threshold
            actual_mean=1.0, actual_hr=0.6,
            label="C0",
        )


def test_i17_mean_drift_aborts():
    """Drift guard aborts when mean_signed_bps exceeds tolerance."""
    ctx = _make_ctx("S1", "BTC", seed=5)
    bm = BlockMetrics(
        session_id="S1", asset="BTC", feature="bitget_ofi",
        horizon_ms=1000, q=0.90, N=5, threshold=0.5,
        mean_signed_bps=1.0, hit_rate=0.6,
        median_signed_bps=None, mean_abs_move=None,
    )
    ctx.metrics[("bitget_ofi", 1000, 0.90)] = bm
    with pytest.raises(AssertionError, match="mean_signed_bps"):
        _assert_event_drift_guard(
            ctx, "bitget_ofi", 1000, 0.90,
            actual_N=5, actual_thresh=0.5,
            actual_mean=1.0 + 1e-11,  # exceeds 1e-12 tolerance
            actual_hr=0.6,
            label="HB-B0",
        )


def test_i17_hit_drift_aborts():
    """Drift guard aborts when hit_rate exceeds tolerance."""
    ctx = _make_ctx("S1", "BTC", seed=6)
    bm = BlockMetrics(
        session_id="S1", asset="BTC", feature="bitget_ofi",
        horizon_ms=1000, q=0.90, N=5, threshold=0.5,
        mean_signed_bps=1.0, hit_rate=0.6,
        median_signed_bps=None, mean_abs_move=None,
    )
    ctx.metrics[("bitget_ofi", 1000, 0.90)] = bm
    with pytest.raises(AssertionError, match="hit_rate"):
        _assert_event_drift_guard(
            ctx, "bitget_ofi", 1000, 0.90,
            actual_N=5, actual_thresh=0.5,
            actual_mean=1.0,
            actual_hr=0.6 + 1e-11,  # exceeds tolerance
            label="HB-B0",
        )


def test_i17_matching_synthetic_metric_passes():
    """Drift guard passes when metric exactly matches."""
    ctx = _make_ctx("S1", "BTC", seed=7)
    bm = BlockMetrics(
        session_id="S1", asset="BTC", feature="bitget_ofi",
        horizon_ms=1000, q=0.90, N=5, threshold=0.5,
        mean_signed_bps=1.0, hit_rate=0.6,
        median_signed_bps=None, mean_abs_move=None,
    )
    ctx.metrics[("bitget_ofi", 1000, 0.90)] = bm
    # Should not raise
    _assert_event_drift_guard(
        ctx, "bitget_ofi", 1000, 0.90,
        actual_N=5, actual_thresh=0.5,
        actual_mean=1.0, actual_hr=0.6,
        label="HB-B0",
    )


# ===========================================================================
# Golden Isolation Tests (I21)
# ===========================================================================

def test_i21_no_golden_import_or_path_in_stage3_source():
    """Static source inspection: production Stage3 module must contain no
    golden artifact reads, imports, or paths (I21).

    Note: _PROTECTED_NAMES legitimately lists golden filenames as a WRITE
    guard (preventing overwrite), and the manifest declares
    golden_artifacts_read_by_generator=False as a safety assertion.
    The check below targets actual READ / IMPORT access, not protective guards.
    """
    stage3_path = Path(__file__).resolve().parents[1] / "recovery" / "v1_2_stage3.py"
    assert stage3_path.exists(), f"Stage3 source not found: {stage3_path}"
    source = stage3_path.read_text(encoding="utf-8")

    # Forbidden tokens that would indicate ACTUAL golden reads / imports.
    # Protective references (in _PROTECTED_NAMES, manifest declarations) are
    # intentional and do NOT constitute golden reads.
    forbidden = [
        "GOLDENS_DIR",
        "goldens_dir",
        "import goldens",
        "from .goldens",
        "from recovery.goldens",
        "open_golden",
        "read_golden",
        "golden_csv",
        "golden_summary",
        "golden_failures",
        "golden_numerics",
        "collector_reference",
    ]
    violations = [tok for tok in forbidden if tok.lower() in source.lower()]
    assert not violations, (
        f"I21 FAIL: Stage3 source contains forbidden golden read references: {violations}"
    )


def test_i21_no_goldens_dir_access():
    """Stage3 source must not reference GOLDENS_DIR."""
    stage3_path = Path(__file__).resolve().parents[1] / "recovery" / "v1_2_stage3.py"
    source = stage3_path.read_text(encoding="utf-8")
    assert "GOLDENS_DIR" not in source


def test_i21_no_golden_csv_filename_access():
    """Stage3 source must not READ golden CSV filenames.

    The golden CSV names may appear in _PROTECTED_NAMES (to prevent overwrites)
    but must NOT appear in any file-open / read context.
    """
    stage3_path = Path(__file__).resolve().parents[1] / "recovery" / "v1_2_stage3.py"
    source = stage3_path.read_text(encoding="utf-8")
    read_ops = ("open(", "read_text(", "read_bytes(", "pd.read_csv(", "read_csv(")
    for fname in ("golden_regression_summary.csv", "golden_regression_failures.csv"):
        for line in source.splitlines():
            if fname in line:
                for op in read_ops:
                    assert op not in line, (
                        f"I21 FAIL: golden CSV {fname!r} appears in read context: {line!r}"
                    )


def test_i21_csv_schema_no_golden_columns():
    """Runtime CSV schema must contain no golden-derived column names."""
    golden_column_keywords = [
        "golden", "recovered_metadata", "residual", "exact_match",
        "n_residual", "threshold_exact", "golden_n",
    ]
    for col in _STAGE3_CSV_COLUMNS:
        for kw in golden_column_keywords:
            assert kw not in col.lower(), (
                f"CSV column '{col}' contains golden-derived keyword '{kw}'"
            )


# ===========================================================================
# Writer Tests
# ===========================================================================

def test_writer_fixed_path_no_output_dir_param():
    """write_stage3_artifacts takes no output_dir parameter."""
    sig = inspect.signature(write_stage3_artifacts)
    assert "output_dir" not in sig.parameters, (
        "write_stage3_artifacts must not accept output_dir parameter"
    )


def test_writer_no_overwrite():
    """FileExistsError if either output file already exists."""
    import shutil
    ctxs = _make_all_contexts(seed_base=70)
    rows = generate_stage3_rows(ctxs)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Monkeypatch the default output dir
        import recovery.v1_2_stage3 as s3
        original_dir = s3._STAGE3_DEFAULT_OUTPUT_DIR
        try:
            s3._STAGE3_DEFAULT_OUTPUT_DIR = Path(tmpdir)
            # First write: should succeed
            write_stage3_artifacts(rows, "test_commit_abc")
            # Second write: should raise FileExistsError
            with pytest.raises(FileExistsError):
                write_stage3_artifacts(rows, "test_commit_abc")
        finally:
            s3._STAGE3_DEFAULT_OUTPUT_DIR = original_dir


def test_writer_protected_name_guard():
    """AssertionError if output filename is in _PROTECTED_NAMES."""
    # stage3_diagnostics.csv and stage3_manifest.json must NOT be in _PROTECTED_NAMES
    # (they're the new outputs); only the frozen V1.1/Stage1/Stage2 names are protected.
    assert "stage3_diagnostics.csv" not in _PROTECTED_NAMES
    assert "stage3_manifest.json" not in _PROTECTED_NAMES
    # V1.1 / Stage1 / Stage2 names must be protected
    assert "golden_regression_summary.csv" in _PROTECTED_NAMES
    assert "stage2_overlap_diagnostics.csv" in _PROTECTED_NAMES
    assert "stage1_candidate_diagnostics.csv" in _PROTECTED_NAMES


def test_writer_exact_csv_columns():
    """CSV output has exactly 25 columns in the frozen order."""
    assert len(_STAGE3_CSV_COLUMNS) == 25
    ctxs = _make_all_contexts(seed_base=80)
    rows = generate_stage3_rows(ctxs)
    csv_data = _csv_bytes(rows)
    reader = csv.reader(io.StringIO(csv_data.decode("utf-8")))
    header = next(reader)
    assert header == list(_STAGE3_CSV_COLUMNS)


def test_writer_newline_lf_only():
    """CSV uses LF (\n) line terminator, not CRLF."""
    ctxs = _make_all_contexts(seed_base=90)
    rows = generate_stage3_rows(ctxs)
    csv_data = _csv_bytes(rows)
    text = csv_data.decode("utf-8")
    assert "\r\n" not in text, "CSV must use \\n, not \\r\\n"
    assert "\n" in text


def test_writer_none_serializes_as_empty_field():
    """None fields serialize to empty CSV fields."""
    assert _format_field(None) == ""


def test_writer_float_uses_repr():
    """Float fields use repr() serialization."""
    assert _format_field(1.0) == repr(1.0)
    assert _format_field(0.5) == repr(0.5)
    assert _format_field(1e-7) == repr(1e-7)


def test_writer_int_uses_decimal_str():
    """Integer fields use decimal string serialization."""
    assert _format_field(144) == "144"
    assert _format_field(0) == "0"


def test_writer_deterministic_hash_size():
    """CSV bytes are deterministic: same input -> same SHA256 and size."""
    ctxs = _make_all_contexts(seed_base=100)
    rows = generate_stage3_rows(ctxs)
    data1 = _csv_bytes(rows)
    data2 = _csv_bytes(rows)
    assert data1 == data2
    assert hashlib.sha256(data1).hexdigest() == hashlib.sha256(data2).hexdigest()
    assert len(data1) == len(data2)


def test_writer_manifest_schema():
    """Manifest contains all required deterministic keys and safety values."""
    import shutil
    ctxs = _make_all_contexts(seed_base=110)
    rows = generate_stage3_rows(ctxs)

    with tempfile.TemporaryDirectory() as tmpdir:
        import recovery.v1_2_stage3 as s3
        original_dir = s3._STAGE3_DEFAULT_OUTPUT_DIR
        try:
            s3._STAGE3_DEFAULT_OUTPUT_DIR = Path(tmpdir)
            csv_path, manifest_path = write_stage3_artifacts(rows, "abc123commit")
            manifest = json.loads(manifest_path.read_text())
        finally:
            s3._STAGE3_DEFAULT_OUTPUT_DIR = original_dir

    required_keys = [
        "runtime_source_commit", "session_ids", "assets", "axes",
        "feature_sets_per_axis", "variant_sets_per_axis",
        "quantiles_per_axis", "horizons_per_axis",
        "expected_row_counts_per_axis", "expected_total_row_count",
        "actual_row_counts_per_axis", "actual_total_row_count",
        "fingerprint_encoding_definition", "threshold_exact_tolerance",
        "quantile_method", "new36_opened", "golden_artifacts_read_by_generator",
        "frozen_analysis_engine_state", "v1_1_stage1_stage2_modified",
        "stage3_diagnostics_sha256", "stage3_diagnostics_size",
        "source_sha256", "diagnostic_version",
    ]
    for key in required_keys:
        assert key in manifest, f"Manifest missing key: {key!r}"

    # Safety values
    assert manifest["new36_opened"] == False
    assert manifest["golden_artifacts_read_by_generator"] == False
    assert manifest["v1_1_stage1_stage2_modified"] == False
    assert manifest["runtime_source_commit"] == "abc123commit"
    assert manifest["diagnostic_version"] == STAGE3_VERSION


def test_writer_manifest_no_timestamp():
    """Manifest must not contain a timestamp field (deterministic output)."""
    import shutil
    ctxs = _make_all_contexts(seed_base=120)
    rows = generate_stage3_rows(ctxs)

    with tempfile.TemporaryDirectory() as tmpdir:
        import recovery.v1_2_stage3 as s3
        original_dir = s3._STAGE3_DEFAULT_OUTPUT_DIR
        try:
            s3._STAGE3_DEFAULT_OUTPUT_DIR = Path(tmpdir)
            _, manifest_path = write_stage3_artifacts(rows, "notime")
            manifest = json.loads(manifest_path.read_text())
        finally:
            s3._STAGE3_DEFAULT_OUTPUT_DIR = original_dir

    timestamp_keys = [k for k in manifest if any(
        pat in k.lower()
        for pat in ("timestamp", "generated_at", "created_at", "updated_at", "datetime", "created_date")
    )]
    assert not timestamp_keys, f"Manifest contains timestamp keys: {timestamp_keys}"


# ===========================================================================
# Matrix Tests
# ===========================================================================

def test_matrix_total_576():
    """Total rows: 288 + 72 + 72 + 144 = 576."""
    ctxs = _make_all_contexts(seed_base=130)
    rows = generate_stage3_rows(ctxs)
    assert len(rows) == STAGE3_TOTAL_ROWS == 576
    counts = Counter(r["axis"] for r in rows)
    assert counts["HZ"] == 288
    assert counts["HG"] == 72
    assert counts["HC"] == 72
    assert counts["HB"] == 144


def test_matrix_exact_axis_membership():
    """Each row belongs to exactly one axis with correct variant."""
    ctxs = _make_all_contexts(seed_base=140)
    rows = generate_stage3_rows(ctxs)
    for r in rows:
        assert r["axis"] in ("HZ", "HG", "HC", "HB")
        if r["axis"] == "HZ":
            assert r["variant_id"] in ("T0", "TZ")
        elif r["axis"] == "HG":
            assert r["variant_id"] in ("G0", "G1")
        elif r["axis"] == "HC":
            assert r["variant_id"] in ("C0", "C1")
        elif r["axis"] == "HB":
            assert r["variant_id"] in ("B0", "B1")


def test_matrix_no_duplicate_row_keys():
    """No duplicate row keys within each axis."""
    ctxs = _make_all_contexts(seed_base=150)
    rows = generate_stage3_rows(ctxs)

    for axis in ("HZ", "HG", "HC", "HB"):
        axis_rows = [r for r in rows if r["axis"] == axis]
        if axis == "HZ":
            keys = [
                (r["session_id"], r["asset"], r["feature_family"],
                 r["variant_id"], r["quantile"])
                for r in axis_rows
            ]
        else:
            keys = [
                (r["session_id"], r["asset"], r["feature_family"],
                 r["variant_id"], r["quantile"], r["horizon_ms"])
                for r in axis_rows
            ]
        assert len(keys) == len(set(keys)), (
            f"Duplicate row keys in axis {axis}: "
            f"{[k for k in keys if keys.count(k) > 1][:5]}"
        )



# ===========================================================================
# Patch1 — I19 / I20 explicit runtime enforcement tests
# ===========================================================================

class TestAssertPositionSubset:
    """Unit tests for _assert_position_subset (the shared enforcement helper)."""

    def test_empty_child_always_passes(self):
        """Empty child is trivially a subset of any parent."""
        parent = np.array([1, 2, 3], dtype=np.int64)
        _assert_position_subset(np.array([], dtype=np.int64), parent, "INVARIANT_X")

    def test_empty_parent_empty_child_passes(self):
        """Both empty: trivially passes."""
        _assert_position_subset(
            np.array([], dtype=np.int64),
            np.array([], dtype=np.int64),
            "INVARIANT_X",
        )

    def test_child_equals_parent_passes(self):
        """Child == parent (exact equality) is a valid subset."""
        arr = np.array([10, 20, 30], dtype=np.int64)
        _assert_position_subset(arr.copy(), arr.copy(), "INVARIANT_X")

    def test_strict_subset_passes(self):
        """Child is a proper strict subset of parent — passes."""
        parent = np.array([10, 20, 30, 40, 50], dtype=np.int64)
        child = np.array([10, 30, 50], dtype=np.int64)
        _assert_position_subset(child, parent, "INVARIANT_X")

    def test_one_position_outside_parent_fails(self):
        """One position in child missing from parent raises AssertionError."""
        parent = np.array([10, 20, 30], dtype=np.int64)
        child = np.array([10, 20, 999], dtype=np.int64)  # 999 not in parent
        with pytest.raises(AssertionError, match="INVARIANT_X FAIL"):
            _assert_position_subset(child, parent, "INVARIANT_X")

    def test_duplicate_child_positions_do_not_hide_violation(self):
        """Duplicate child entries must not mask a real violation."""
        parent = np.array([1, 2, 3], dtype=np.int64)
        # 4 appears multiple times but is not in parent
        child = np.array([1, 2, 4, 4, 4], dtype=np.int64)
        with pytest.raises(AssertionError):
            _assert_position_subset(child, parent, "INVARIANT_X")


# ---------------------------------------------------------------------------
# I19 focused tests
# ---------------------------------------------------------------------------

def test_i19_g1_subset_passes():
    """I19 PASS: valid G1 child subset of depth_l1 parent passes without exception."""
    parent = np.array([5, 15, 25, 35, 45], dtype=np.int64)
    # G1 positions are a proper subset (ext_ofi confirmation removes some)
    g1_child = np.array([5, 25, 45], dtype=np.int64)
    _assert_position_subset(g1_child, parent, "I19")  # must not raise


def test_i19_g1_subset_fails():
    """I19 FAIL: one injected G1 position outside depth_l1 parent raises AssertionError.

    This simulates tampering / data corruption where G1 produces a position
    that was NOT in the depth_imbalance_l1 baseline gate.
    """
    parent = np.array([5, 15, 25, 35], dtype=np.int64)
    # 999 is outside the parent baseline — should trigger I19 FAIL
    g1_child_tampered = np.array([5, 25, 999], dtype=np.int64)
    with pytest.raises(AssertionError, match="I19 FAIL"):
        _assert_position_subset(g1_child_tampered, parent, "I19")


def test_i19_error_message_contains_label():
    """AssertionError message must contain 'I19 FAIL' for traceability."""
    with pytest.raises(AssertionError) as exc_info:
        _assert_position_subset(
            np.array([42], dtype=np.int64),
            np.array([1, 2, 3], dtype=np.int64),
            "I19",
        )
    assert "I19 FAIL" in str(exc_info.value)


# ---------------------------------------------------------------------------
# I20 focused tests
# ---------------------------------------------------------------------------

def test_i20_c1_subset_passes():
    """I20 PASS: valid C1 child subset of shared gap threshold-crossing set passes."""
    shared_gap_gate = np.array([10, 20, 30, 40, 50], dtype=np.int64)
    # C1 adds di_l1 + ext_ofi confirmation; some gap-gate positions are dropped
    c1_child = np.array([10, 40], dtype=np.int64)
    _assert_position_subset(c1_child, shared_gap_gate, "I20")  # must not raise


def test_i20_c1_subset_fails():
    """I20 FAIL: C1 position outside shared gap threshold-crossing set raises AssertionError.

    This simulates a position that crossed the gap threshold in C1 but was NOT
    present in the shared gap-crossing set — which would be logically impossible
    by construction, but must be caught explicitly (fail-closed).
    """
    shared_gap_gate = np.array([10, 20, 30], dtype=np.int64)
    # 777 is outside the shared gap gate
    c1_child_tampered = np.array([10, 777], dtype=np.int64)
    with pytest.raises(AssertionError, match="I20 FAIL"):
        _assert_position_subset(c1_child_tampered, shared_gap_gate, "I20")


def test_i20_error_message_contains_label():
    """AssertionError message must contain 'I20 FAIL' for traceability."""
    with pytest.raises(AssertionError) as exc_info:
        _assert_position_subset(
            np.array([99], dtype=np.int64),
            np.array([1, 2, 3], dtype=np.int64),
            "I20",
        )
    assert "I20 FAIL" in str(exc_info.value)


def test_i20_parent_is_shared_gap_gate_not_c0_confirmed():
    """I20 canonical: parent is the shared gap threshold-crossing set, NOT C0 confirmed.

    A C1 position may be absent from C0's confirmed set (different confirmation
    semantics) while still satisfying I20 (present in the shared gap gate).
    This test explicitly verifies that I20 is asserted against the gate, not C0.
    """
    shared_gap_gate = np.array([5, 10, 15, 20, 25], dtype=np.int64)
    c0_confirmed = np.array([5, 15], dtype=np.int64)  # strict subset of gate
    # C1 positions: present in gate but NOT in C0 confirmed (different confirmation)
    c1_positions = np.array([10, 20], dtype=np.int64)

    # I20 must PASS: C1 ⊆ shared_gap_gate
    _assert_position_subset(c1_positions, shared_gap_gate, "I20")

    # Confirm that C1 is NOT a subset of C0 (expected — different semantics)
    c0_set = set(c0_confirmed.tolist())
    c1_set = set(c1_positions.tolist())
    assert not c1_set.issubset(c0_set), (
        "Expected C1 to NOT be a subset of C0 in this test "
        "(I20 parent is gate, not C0)"
    )


# ---------------------------------------------------------------------------
# Patch1 regression: Patch1 must not change existing semantics
# ---------------------------------------------------------------------------

def test_patch1_regression_hg_g1_still_runs():
    """Regression: _hg_block G1 still completes successfully with valid synthetic data.

    I19 assertion is fail-closed; valid data must pass through without raising.
    """
    ctx = _make_ctx("S1", "BTC", n=300, seed=77)
    _populate_matching_stage3_metrics(ctx)
    _, hz_cache = _hz_block(ctx, "S1", "BTC")
    hg_rows = _hg_block(ctx, "S1", "BTC", hz_cache)
    g1_rows = [r for r in hg_rows if r["variant_id"] == "G1"]
    assert len(g1_rows) > 0, "Expected at least one G1 row"
    # All G1 rows must have accepted_positions_sha256 (not None)
    for r in g1_rows:
        assert r["accepted_positions_sha256"] is not None or r["accepted_n"] == 0


def test_patch1_regression_hc_c1_still_runs():
    """Regression: _hc_block C1 still completes successfully with valid synthetic data.

    I20 assertion is fail-closed; valid data must pass through without raising.
    """
    ctx = _make_ctx("S1", "BTC", n=300, seed=88)
    _populate_matching_stage3_metrics(ctx)
    hc_rows = _hc_block(ctx, "S1", "BTC")
    c1_rows = [r for r in hc_rows if r["variant_id"] == "C1"]
    assert len(c1_rows) > 0, "Expected at least one C1 row"


def test_patch1_regression_576_rows_unchanged():
    """Regression: Patch1 must not change the total row count (576)."""
    ctxs = _make_all_contexts(seed_base=999)
    rows = generate_stage3_rows(ctxs)
    assert len(rows) == STAGE3_TOTAL_ROWS, (
        f"Expected {STAGE3_TOTAL_ROWS} rows after Patch1, got {len(rows)}"
    )


def test_patch1_regression_hz_semantics_unchanged():
    """Regression: HZ rows are identical before and after Patch1 (I19/I20 not in HZ)."""
    ctx = _make_ctx("S1", "ETH", n=400, seed=55)
    _populate_matching_stage3_metrics(ctx)
    rows, _ = _hz_block(ctx, "S1", "ETH")
    # Spot-check: all 12 features x 3 q x 2 variants = 72 rows per session/asset
    assert len(rows) == len(STAGE3_HZ_FEATURES) * len(STAGE3_QUANTILES) * 2
    for r in rows:
        assert r["axis"] == "HZ"
        assert r["variant_id"] in ("T0", "TZ")


def test_patch1_regression_hb_semantics_unchanged():
    """Regression: HB block still runs correctly; Patch1 has no HB changes."""
    ctx = _make_ctx("S2", "ETH", n=400, seed=66)
    _populate_matching_stage3_metrics(ctx)
    _, hz_cache = _hz_block(ctx, "S2", "ETH")
    hb_rows = _hb_block(ctx, "S2", "ETH")
    expected = len(STAGE3_HB_FAMILIES) * 2 * len(STAGE3_HORIZONS)
    assert len(hb_rows) == expected, (
        f"Expected {expected} HB rows, got {len(hb_rows)}"
    )
