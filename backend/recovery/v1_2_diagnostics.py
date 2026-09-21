"""RECONSTRUCTION_V1.2 Stage 1 candidate-only diagnostics.

Development-only instrumentation for a fixed OLD36 matrix.  This module does
not define a V1.2 methodology and does not compare candidate values with any
historical target output.  It reuses the frozen V1.1 engine helpers read-only
and emits candidate-side stage counts, descriptive probes, and event-set
fingerprints.

Safety invariants:
- no import-time execution;
- no modification of backend/recovery/engine.py or frozen V1.1 artifacts;
- RAW access is restricted to the two declared OLD36 sessions and goes through
  the existing recovery sandbox/harness loader;
- no DB/RAW writes;
- NEW36 remains rejected by the existing quantitative firewall;
- FrozenAnalysisEngine must remain NOT_CONFIGURED / accepts_input=False.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from checkpoint_registry import NEW36_SESSION_IDS, OLD36_REFERENCE_SESSIONS
from frozen_engine import current_status as frozen_engine_status

from .allowlist import assert_recovery_allowed
from .engine import (
    FEATURE_MAP,
    GRID_MS,
    HORIZONS_SIMPLE_MS,
    SIMPLE_FEATURES,
    _build_forward_return_array,
    _greedy_overlap_filter,
    _quality_admissible_mask,
    _quantile_type7,
    _rank_signed_uniform,
    build_canonical_grid,
    reconstruct_block,
)
from .harness import _load_grid_for_block, _validate_sync_grid_part_coverage


DIAGNOSTIC_VERSION = "V1.2_STAGE1_DRAFT2"
STAGE0_PATCH_SHA256 = "258a3a20f9d314f8df489468bef672cda9aceeb6f12158e307d7c86a4c3ee2fa"
EXPECTED_ROW_COUNT = 252
ASSETS: tuple[str, ...] = ("BTC", "ETH")
DECLARED_SESSIONS: tuple[str, ...] = (
    "20260905T073818Z_e44d99bd",  # lexicographically first frozen WE OLD24 session
    "20260906T221530Z_db18dc51",  # lexicographically first frozen WD OLD24 session
)
S6_SESSION = DECLARED_SESSIONS[0]

# Protected V1.1 report names. Stage 1 never writes any of these names.
_V1_1_REPORT_NAMES = frozenset({
    "golden_regression_summary.csv",
    "golden_regression_failures.csv",
    "recovery_report.md",
    "recovery_provenance.json",
    "recovery_rules.json",
})


@dataclass(frozen=True)
class DiagnosticCase:
    stratum: str
    session_id: str
    asset: str
    feature: str
    horizon_ms: int
    q: float
    dispersion_state: str | None = None


@dataclass
class _BlockContext:
    session_id: str
    asset: str
    grid: pd.DataFrame
    gpos: np.ndarray
    quality: np.ndarray
    mid: np.ndarray
    columns: dict[str, np.ndarray]
    z_di_l1: np.ndarray
    z_di_l5: np.ndarray
    z_ext_ofi: np.ndarray
    disp_lo: float | None
    disp_hi: float | None
    metrics: dict[tuple[str, int, float], object]


def expand_fixed_matrix() -> list[DiagnosticCase]:
    """Expand the mismatch-blind fixed Stage-1 matrix in deterministic order."""
    rows: list[DiagnosticCase] = []

    # S1: 2 sessions × 2 assets × 1 feature × 8 H = 32
    for sid in DECLARED_SESSIONS:
        for asset in ASSETS:
            for h in HORIZONS_SIMPLE_MS:
                rows.append(DiagnosticCase("S1", sid, asset, "bitget_ofi", h, 0.90))

    # S2: 2 × 2 × 2 features × 8 H = 64
    for sid in DECLARED_SESSIONS:
        for asset in ASSETS:
            for feature in ("depth_imbalance_l1", "depth_imbalance_l5"):
                for h in HORIZONS_SIMPLE_MS:
                    rows.append(DiagnosticCase("S2", sid, asset, feature, h, 0.90))

    # S3: 2 × 2 × 2 features × 3 H = 24
    for sid in DECLARED_SESSIONS:
        for asset in ASSETS:
            for feature in ("depthBoth", "depthL1_extOFI"):
                for h in (1000, 5000, 30000):
                    rows.append(DiagnosticCase("S3", sid, asset, feature, h, 0.90))

    # S4: 2 × 2 × 3 features × 2 H = 24
    for sid in DECLARED_SESSIONS:
        for asset in ASSETS:
            for feature in ("gap_depthL1", "gap_depth_extOFI", "gap_localOFI"):
                for h in (1000, 30000):
                    rows.append(DiagnosticCase("S4", sid, asset, feature, h, 0.90))

    # S5: 2 × 2 × 3 H × 3 states = 36
    for sid in DECLARED_SESSIONS:
        for asset in ASSETS:
            for h in (5000, 10000, 30000):
                for state in ("low", "mid", "high"):
                    rows.append(
                        DiagnosticCase(
                            "S5", sid, asset, "fair_gap_reversion", h, 0.90, state
                        )
                    )

    # S6: 1 × 2 × 12 simple features × 3 q = 72
    for asset in ASSETS:
        for feature in SIMPLE_FEATURES:
            for q in (0.80, 0.90, 0.95):
                rows.append(DiagnosticCase("S6", S6_SESSION, asset, feature, 1000, q))

    if len(rows) != EXPECTED_ROW_COUNT:
        raise AssertionError(
            f"Stage1 fixed matrix expected {EXPECTED_ROW_COUNT} rows, got {len(rows)}"
        )
    return rows


def _col(grid: pd.DataFrame, name: str) -> np.ndarray:
    if name not in grid.columns:
        return np.full(len(grid), np.nan, dtype=float)
    return grid[name].to_numpy(dtype=float)


def _prepare_block(session_id: str, asset: str, df: pd.DataFrame) -> _BlockContext:
    """Build candidate-only reusable block state from one loaded OLD36 grid."""
    grid = build_canonical_grid(df)
    gpos = grid["grid_pos"].to_numpy(dtype=np.int64)
    quality = _quality_admissible_mask(grid).to_numpy(dtype=bool)
    mid = grid["bitget_mid"].to_numpy(dtype=float)

    needed = {
        "bitget_gap_to_fair_bps",
        "external_perp_dispersion_bps",
        "bitget_depth_imbalance_l1",
        "bitget_depth_imbalance_l5",
        "external_ofi_consensus_l1",
        "bitget_fair_ofi_alignment",
    }
    needed.update(FEATURE_MAP.values())
    columns = {name: _col(grid, name) for name in sorted(needed)}

    qseries = pd.Series(quality, dtype=bool)
    z_di_l1 = _rank_signed_uniform(
        pd.Series(columns["bitget_depth_imbalance_l1"], dtype=float), qseries
    ).to_numpy(dtype=float)
    z_di_l5 = _rank_signed_uniform(
        pd.Series(columns["bitget_depth_imbalance_l5"], dtype=float), qseries
    ).to_numpy(dtype=float)
    z_ext_ofi = _rank_signed_uniform(
        pd.Series(columns["external_ofi_consensus_l1"], dtype=float), qseries
    ).to_numpy(dtype=float)

    disp = columns["external_perp_dispersion_bps"]
    disp_domain = quality & np.isfinite(disp)
    disp_vals = disp[disp_domain]
    if len(disp_vals) >= 2:
        disp_lo = _quantile_type7(disp_vals, 1.0 / 3.0)
        disp_hi = _quantile_type7(disp_vals, 2.0 / 3.0)
    else:
        disp_lo = None
        disp_hi = None

    summary = reconstruct_block(session_id, asset, df)
    if not summary.valid:
        raise RuntimeError(
            f"Stage1 block {session_id}/{asset} is structurally invalid: "
            f"{summary.invalid_reason}"
        )
    metrics = {(m.feature, m.horizon_ms, float(m.q)): m for m in summary.metrics}

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
        disp_lo=disp_lo,
        disp_hi=disp_hi,
        metrics=metrics,
    )


def _finite_mean_abs(values: np.ndarray) -> float | None:
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    if len(vals) == 0:
        return None
    return float(np.mean(np.abs(vals)))


def _accepted_positions(gpos: np.ndarray, event_mask: np.ndarray, spacing: int) -> np.ndarray:
    pos = np.asarray(gpos[event_mask], dtype=np.int64)
    if len(pos) == 0:
        return np.asarray([], dtype=np.int64)
    keep = _greedy_overlap_filter(pos, spacing)
    return pos[keep]


def _accepted_index_mask(gpos: np.ndarray, accepted_positions: np.ndarray) -> np.ndarray:
    if len(accepted_positions) == 0:
        return np.zeros(len(gpos), dtype=bool)
    return np.isin(gpos, accepted_positions, assume_unique=True)


def fingerprint_positions(positions: Sequence[int] | np.ndarray) -> dict:
    """Canonical event fingerprint: ascending little-endian signed int64 bytes."""
    arr = np.asarray(list(positions), dtype=np.int64)
    if len(arr):
        arr = np.sort(arr)
    le = arr.astype("<i8", copy=False)
    digest = hashlib.sha256(le.tobytes(order="C")).hexdigest()
    return {
        "accepted_positions_count": int(len(arr)),
        "accepted_positions_sha256": digest,
        "accepted_first_grid_pos": int(arr[0]) if len(arr) else None,
        "accepted_last_grid_pos": int(arr[-1]) if len(arr) else None,
    }


def _base_row(case: DiagnosticCase, ctx: _BlockContext, fwd: np.ndarray) -> dict:
    return {
        "diagnostic_version": DIAGNOSTIC_VERSION,
        "stratum": case.stratum,
        "session_id": case.session_id,
        "asset": case.asset,
        "feature": case.feature,
        "horizon_ms": case.horizon_ms,
        "q": case.q,
        "dispersion_state": case.dispersion_state,
        "source_feature_column": None,
        "component1_column": None,
        "component2_column": None,
        "alignment_column": None,
        "grid_rows": int(len(ctx.grid)),
        "quality_n": int(np.sum(ctx.quality)),
        "forward_eligible_n": int(np.sum(ctx.quality & np.isfinite(fwd))),
        "overlap_spacing_steps": int(max(10, case.horizon_ms // GRID_MS)),
        "candidate_N": None,
        "candidate_threshold": None,
        "candidate_mean_signed_bps": None,
        "candidate_hit_rate": None,
        "candidate_mean_abs_move_v11": None,
        "signal_finite_quality_n": None,
        "signal_nonzero_quality_n": None,
        "threshold_domain_n": None,
        "threshold_crossing_n_before_forward": None,
        "threshold_crossing_n_after_forward": None,
        "accepted_n": None,
        "overlap_dropped_n": None,
        "component1_finite_quality_n": None,
        "component2_finite_quality_n": None,
        "derived_domain_n": None,
        "derived_nonzero_n": None,
        "gap_threshold_domain_n": None,
        "confirmation_finite_quality_n": None,
        "gap_event_domain_n": None,
        "gap_threshold_crossing_n_pre_alignment": None,
        "alignment_true_n_within_threshold_domain": None,
        "gap_event_n_post_alignment_pre_forward": None,
        "dispersion_quality_finite_n": None,
        "disp_lo_v11": None,
        "disp_hi_v11": None,
        "fair_gap_accepted_n": None,
        "accepted_state_n": None,
        "probe_mam_all_forward_eligible": None,
        "probe_mam_feature_finite_forward": None,
        "probe_mam_threshold_crossing_forward": None,
        "probe_mam_accepted_events": None,
        "probe_disp_lo_accepted_events": None,
        "probe_disp_hi_accepted_events": None,
        "probe_state_n_using_event_tertiles": None,
        "accepted_positions_count": None,
        "accepted_positions_sha256": None,
        "accepted_first_grid_pos": None,
        "accepted_last_grid_pos": None,
    }


def _metric(ctx: _BlockContext, case: DiagnosticCase):
    key = (case.feature, case.horizon_ms, float(case.q))
    try:
        return ctx.metrics[key]
    except KeyError as exc:
        raise KeyError(f"V1.1 candidate metric not found for {key!r}") from exc


def _assert_overlap(row: dict) -> None:
    post = row["threshold_crossing_n_after_forward"]
    accepted = row["accepted_n"]
    dropped = row["overlap_dropped_n"]
    if any(v is None for v in (post, accepted, dropped)):
        raise AssertionError("Stage1 overlap accounting fields unexpectedly undefined")
    if dropped < 0 or accepted + dropped != post:
        raise AssertionError(
            f"Stage1 overlap accounting failed: post={post}, accepted={accepted}, dropped={dropped}"
        )


def _assert_candidate_consistency(row: dict, metric, accepted_positions: np.ndarray) -> None:
    rt = row.get("candidate_threshold")
    mt = metric.threshold
    if (rt is None) != (mt is None) or (
        rt is not None and mt is not None and not math.isclose(float(rt), float(mt), rel_tol=0.0, abs_tol=0.0)
    ):
        raise AssertionError(
            f"diagnostic threshold {rt!r} != V1.1 engine threshold {mt!r}"
        )
    if int(row["candidate_N"]) != int(metric.N):
        raise AssertionError(
            f"diagnostic candidate N {row['candidate_N']} != V1.1 engine N {metric.N}"
        )
    if int(row["accepted_n"]) != len(accepted_positions):
        raise AssertionError("accepted_n does not match accepted position count")
    if int(row["candidate_N"]) != len(accepted_positions):
        raise AssertionError("diagnostic accepted event set diverges from V1.1 engine N")


def _diagnose_simple(case: DiagnosticCase, ctx: _BlockContext, fwd: np.ndarray) -> dict:
    row = _base_row(case, ctx, fwd)
    metric = _metric(ctx, case)
    signal = ctx.columns[FEATURE_MAP[case.feature]]
    simple_domain = ctx.quality & np.isfinite(signal)
    threshold = _quantile_type7(np.abs(signal[simple_domain]), case.q)
    pre = (
        simple_domain & (signal != 0.0) & (np.abs(signal) >= threshold)
        if threshold is not None
        else np.zeros(len(ctx.grid), dtype=bool)
    )
    post = pre & np.isfinite(fwd)
    spacing = max(10, case.horizon_ms // GRID_MS)
    accepted = _accepted_positions(ctx.gpos, post, spacing)
    accepted_mask = _accepted_index_mask(ctx.gpos, accepted)

    row.update({
        "source_feature_column": FEATURE_MAP[case.feature],
        "candidate_N": int(metric.N),
        "candidate_threshold": metric.threshold,
        "candidate_mean_signed_bps": metric.mean_signed_bps,
        "candidate_hit_rate": metric.hit_rate,
        "candidate_mean_abs_move_v11": metric.mean_abs_move,
        "signal_finite_quality_n": int(np.sum(simple_domain)),
        "signal_nonzero_quality_n": int(np.sum(simple_domain & (signal != 0.0))),
        "threshold_domain_n": int(np.sum(simple_domain)),
        "threshold_crossing_n_before_forward": int(np.sum(pre)),
        "threshold_crossing_n_after_forward": int(np.sum(post)),
        "accepted_n": int(len(accepted)),
        "overlap_dropped_n": int(np.sum(post) - len(accepted)),
        "probe_mam_all_forward_eligible": _finite_mean_abs(fwd[ctx.quality & np.isfinite(fwd)]),
        "probe_mam_feature_finite_forward": _finite_mean_abs(
            fwd[ctx.quality & np.isfinite(signal) & np.isfinite(fwd)]
        ),
        "probe_mam_threshold_crossing_forward": _finite_mean_abs(fwd[post]),
        "probe_mam_accepted_events": _finite_mean_abs(fwd[accepted_mask]),
    })
    row.update(fingerprint_positions(accepted))
    _assert_overlap(row)
    _assert_candidate_consistency(row, metric, accepted)

    # Probe the alternate event-domain dispersion tertiles for fair-gap rows.
    if case.feature == "fair_gap_reversion":
        disp = ctx.columns["external_perp_dispersion_bps"]
        accepted_disp = disp[accepted_mask]
        finite_disp = accepted_disp[np.isfinite(accepted_disp)]
        if len(finite_disp) >= 2:
            row["probe_disp_lo_accepted_events"] = _quantile_type7(finite_disp, 1.0 / 3.0)
            row["probe_disp_hi_accepted_events"] = _quantile_type7(finite_disp, 2.0 / 3.0)

    return row


def _diagnose_derived(case: DiagnosticCase, ctx: _BlockContext, fwd: np.ndarray) -> dict:
    row = _base_row(case, ctx, fwd)
    metric = _metric(ctx, case)
    if case.feature == "depthBoth":
        z1, z2 = ctx.z_di_l1, ctx.z_di_l5
        c1, c2 = "bitget_depth_imbalance_l1", "bitget_depth_imbalance_l5"
    elif case.feature == "depthL1_extOFI":
        z1, z2 = ctx.z_di_l1, ctx.z_ext_ofi
        c1, c2 = "bitget_depth_imbalance_l1", "external_ofi_consensus_l1"
    else:  # pragma: no cover - fixed matrix guard
        raise ValueError(f"unsupported derived feature {case.feature}")

    derived_domain = ctx.quality & np.isfinite(z1) & np.isfinite(z2)
    D = np.where(derived_domain, (z1 + z2) / 2.0, np.nan)
    threshold = _quantile_type7(np.abs(D[derived_domain]), case.q)
    pre = (
        derived_domain & (D != 0.0) & (np.abs(D) >= threshold)
        if threshold is not None
        else np.zeros(len(ctx.grid), dtype=bool)
    )
    post = pre & np.isfinite(fwd)
    spacing = max(10, case.horizon_ms // GRID_MS)
    accepted = _accepted_positions(ctx.gpos, post, spacing)

    row.update({
        "component1_column": c1,
        "component2_column": c2,
        "candidate_N": int(metric.N),
        "candidate_threshold": metric.threshold,
        "candidate_mean_signed_bps": metric.mean_signed_bps,
        "candidate_hit_rate": metric.hit_rate,
        "component1_finite_quality_n": int(np.sum(ctx.quality & np.isfinite(z1))),
        "component2_finite_quality_n": int(np.sum(ctx.quality & np.isfinite(z2))),
        "derived_domain_n": int(np.sum(derived_domain)),
        "derived_nonzero_n": int(np.sum(derived_domain & (D != 0.0))),
        "threshold_domain_n": int(np.sum(derived_domain)),
        "threshold_crossing_n_before_forward": int(np.sum(pre)),
        "threshold_crossing_n_after_forward": int(np.sum(post)),
        "accepted_n": int(len(accepted)),
        "overlap_dropped_n": int(np.sum(post) - len(accepted)),
    })
    row.update(fingerprint_positions(accepted))
    _assert_overlap(row)
    _assert_candidate_consistency(row, metric, accepted)
    return row


def _gap_components(ctx: _BlockContext, feature: str):
    gap = ctx.columns["bitget_gap_to_fair_bps"]
    if feature == "gap_depthL1":
        conf = ctx.columns["bitget_depth_imbalance_l1"]
        conf_fin = np.isfinite(conf)
        alignment = (np.sign(conf) == -np.sign(gap)) & (conf != 0.0)
        return conf_fin, alignment, "bitget_depth_imbalance_l1", None, None
    if feature == "gap_depth_extOFI":
        D = np.where(
            np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi),
            (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0,
            np.nan,
        )
        conf_fin = np.isfinite(D)
        alignment = (np.sign(D) == -np.sign(gap)) & (D != 0.0) & np.isfinite(D)
        return conf_fin, alignment, "bitget_depth_imbalance_l1", "external_ofi_consensus_l1", None
    if feature == "gap_localOFI":
        flag = ctx.columns["bitget_fair_ofi_alignment"]
        conf_fin = np.isfinite(flag)
        alignment = flag == 1.0
        return conf_fin, alignment, None, None, "bitget_fair_ofi_alignment"
    raise ValueError(f"unsupported Stage1 gap feature {feature}")


def _diagnose_gap(case: DiagnosticCase, ctx: _BlockContext, fwd: np.ndarray) -> dict:
    row = _base_row(case, ctx, fwd)
    metric = _metric(ctx, case)
    gap = ctx.columns["bitget_gap_to_fair_bps"]
    gap_threshold_domain = ctx.quality & np.isfinite(gap)
    threshold = _quantile_type7(np.abs(gap[gap_threshold_domain]), case.q)
    conf_fin, alignment, c1, c2, align_col = _gap_components(ctx, case.feature)
    gap_event_domain = gap_threshold_domain & conf_fin
    pre_alignment = (
        gap_event_domain & (gap != 0.0) & (np.abs(gap) >= threshold)
        if threshold is not None
        else np.zeros(len(ctx.grid), dtype=bool)
    )
    alignment_true_domain = gap_event_domain & alignment
    post_alignment = pre_alignment & alignment
    post_forward = post_alignment & np.isfinite(fwd)
    spacing = max(10, case.horizon_ms // GRID_MS)
    accepted = _accepted_positions(ctx.gpos, post_forward, spacing)

    row.update({
        "source_feature_column": "bitget_gap_to_fair_bps",
        "component1_column": c1,
        "component2_column": c2,
        "alignment_column": align_col,
        "candidate_N": int(metric.N),
        "candidate_threshold": metric.threshold,
        "candidate_mean_signed_bps": metric.mean_signed_bps,
        "candidate_hit_rate": metric.hit_rate,
        "gap_threshold_domain_n": int(np.sum(gap_threshold_domain)),
        "confirmation_finite_quality_n": int(np.sum(ctx.quality & conf_fin)),
        "gap_event_domain_n": int(np.sum(gap_event_domain)),
        "gap_threshold_crossing_n_pre_alignment": int(np.sum(pre_alignment)),
        "alignment_true_n_within_threshold_domain": int(np.sum(alignment_true_domain)),
        "gap_event_n_post_alignment_pre_forward": int(np.sum(post_alignment)),
        "threshold_crossing_n_after_forward": int(np.sum(post_forward)),
        "accepted_n": int(len(accepted)),
        "overlap_dropped_n": int(np.sum(post_forward) - len(accepted)),
    })
    row.update(fingerprint_positions(accepted))
    _assert_overlap(row)
    _assert_candidate_consistency(row, metric, accepted)
    return row


def _dispersion_state_mask(values: np.ndarray, state: str, lo: float, hi: float) -> np.ndarray:
    if state == "low":
        return values < lo
    if state == "high":
        return values >= hi
    if state == "mid":
        return (values >= lo) & (values < hi)
    raise ValueError(f"unknown dispersion state {state!r}")


def _diagnose_dispersion(case: DiagnosticCase, ctx: _BlockContext, fwd: np.ndarray) -> dict:
    row = _base_row(case, ctx, fwd)
    metric = _metric(ctx, case)
    gap = ctx.columns["bitget_gap_to_fair_bps"]
    disp = ctx.columns["external_perp_dispersion_bps"]
    simple_domain = ctx.quality & np.isfinite(gap)
    threshold = _quantile_type7(np.abs(gap[simple_domain]), case.q)
    pre = (
        simple_domain & (gap != 0.0) & (np.abs(gap) >= threshold)
        if threshold is not None
        else np.zeros(len(ctx.grid), dtype=bool)
    )
    post = pre & np.isfinite(fwd)
    spacing = max(10, case.horizon_ms // GRID_MS)
    fair_gap_accepted = _accepted_positions(ctx.gpos, post, spacing)
    fair_gap_mask = _accepted_index_mask(ctx.gpos, fair_gap_accepted)

    # Locate the engine's state candidate for the row.  The frozen engine emits
    # no dispersion-state list at all when the base fair-gap row has N=0 or
    # dispersion tertiles are undefined.  Stage 1 still emits its fixed three
    # state rows in that case, deterministically representing each state as N=0
    # with undefined mean/hit, so the 252-row matrix never silently shrinks.
    state_metric = None
    for state_row in metric.dispersion_states:
        if state_row.get("state") == case.dispersion_state:
            state_metric = state_row
            break

    if ctx.disp_lo is None or ctx.disp_hi is None:
        state_mask_full = np.zeros(len(ctx.grid), dtype=bool)
    else:
        state_mask_full = fair_gap_mask & _dispersion_state_mask(
            disp, case.dispersion_state, ctx.disp_lo, ctx.disp_hi
        )
    state_positions = ctx.gpos[state_mask_full]

    if state_metric is None:
        if int(metric.N) > 0 and ctx.disp_lo is not None and ctx.disp_hi is not None:
            raise AssertionError(
                f"V1.1 engine unexpectedly omitted dispersion state {case.dispersion_state!r} for {case}"
            )
        state_n = 0
        state_mean = None
        state_hit = None
    else:
        state_n = int(state_metric["N"])
        state_mean = state_metric["mean_signed_bps"]
        state_hit = state_metric["hit_rate"]

    row.update({
        "source_feature_column": "bitget_gap_to_fair_bps",
        "candidate_N": state_n,
        "candidate_threshold": metric.threshold,
        "candidate_mean_signed_bps": state_mean,
        "candidate_hit_rate": state_hit,
        "candidate_mean_abs_move_v11": metric.mean_abs_move,
        "dispersion_quality_finite_n": int(np.sum(ctx.quality & np.isfinite(disp))),
        "disp_lo_v11": ctx.disp_lo,
        "disp_hi_v11": ctx.disp_hi,
        "fair_gap_accepted_n": int(len(fair_gap_accepted)),
        "accepted_state_n": int(len(state_positions)),
        "signal_finite_quality_n": int(np.sum(simple_domain)),
        "signal_nonzero_quality_n": int(np.sum(simple_domain & (gap != 0.0))),
        "threshold_domain_n": int(np.sum(simple_domain)),
        "threshold_crossing_n_before_forward": int(np.sum(pre)),
        "threshold_crossing_n_after_forward": int(np.sum(post)),
        "probe_mam_all_forward_eligible": _finite_mean_abs(fwd[ctx.quality & np.isfinite(fwd)]),
        "probe_mam_feature_finite_forward": _finite_mean_abs(
            fwd[ctx.quality & np.isfinite(gap) & np.isfinite(fwd)]
        ),
        "probe_mam_threshold_crossing_forward": _finite_mean_abs(fwd[post]),
        "probe_mam_accepted_events": _finite_mean_abs(fwd[fair_gap_mask]),
    })

    # Event-domain dispersion-tertile probe. Null boundaries/state count when
    # fewer than two finite dispersion values exist on accepted events.
    accepted_disp = disp[fair_gap_mask]
    finite_disp = accepted_disp[np.isfinite(accepted_disp)]
    if len(finite_disp) >= 2:
        probe_lo = _quantile_type7(finite_disp, 1.0 / 3.0)
        probe_hi = _quantile_type7(finite_disp, 2.0 / 3.0)
        row["probe_disp_lo_accepted_events"] = probe_lo
        row["probe_disp_hi_accepted_events"] = probe_hi
        if probe_lo is not None and probe_hi is not None:
            row["probe_state_n_using_event_tertiles"] = int(
                np.sum(_dispersion_state_mask(finite_disp, case.dispersion_state, probe_lo, probe_hi))
            )

    row.update(fingerprint_positions(state_positions))
    if int(row["candidate_N"]) != int(row["accepted_state_n"]):
        raise AssertionError(
            f"dispersion state diagnostic N {row['accepted_state_n']} != engine N {row['candidate_N']}"
        )
    return row


def diagnose_case(case: DiagnosticCase, ctx: _BlockContext) -> dict:
    if case.session_id not in DECLARED_SESSIONS:
        raise PermissionError(f"Stage1 rejects undeclared session {case.session_id!r}")
    if case.asset not in ASSETS:
        raise ValueError(f"Stage1 rejects undeclared asset {case.asset!r}")
    if case.session_id != ctx.session_id or case.asset != ctx.asset:
        raise ValueError("diagnostic case identity does not match loaded block")

    k = case.horizon_ms // GRID_MS
    fwd = _build_forward_return_array(ctx.mid, k)

    if case.stratum == "S5":
        return _diagnose_dispersion(case, ctx, fwd)
    if case.feature in SIMPLE_FEATURES:
        return _diagnose_simple(case, ctx, fwd)
    if case.feature in ("depthBoth", "depthL1_extOFI"):
        return _diagnose_derived(case, ctx, fwd)
    if case.feature in ("gap_depthL1", "gap_depth_extOFI", "gap_localOFI"):
        return _diagnose_gap(case, ctx, fwd)
    raise ValueError(f"Stage1 unsupported diagnostic feature {case.feature!r}")


def _validate_runtime_safety() -> None:
    old = tuple(OLD36_REFERENCE_SESSIONS)
    if any(sid not in old for sid in DECLARED_SESSIONS):
        raise RuntimeError("Stage1 declared session is not in OLD36_REFERENCE_SESSIONS")
    for sid in DECLARED_SESSIONS:
        assert_recovery_allowed(sid)

    # Structural firewall self-check only; this does NOT open NEW36 data.
    if not NEW36_SESSION_IDS:
        raise RuntimeError("NEW36 registry unexpectedly empty")
    try:
        assert_recovery_allowed(NEW36_SESSION_IDS[0])
    except PermissionError:
        pass
    else:  # pragma: no cover - safety belt
        raise RuntimeError("NEW36 quantitative firewall is not rejecting NEW36")

    status = frozen_engine_status()
    if status.status != "NOT_CONFIGURED" or status.accepts_input is not False:
        raise RuntimeError(
            f"FrozenAnalysisEngine unsafe state: status={status.status!r}, "
            f"accepts_input={status.accepts_input!r}"
        )


def generate_stage1_rows(
    contexts: dict[tuple[str, str], _BlockContext] | None = None,
) -> list[dict]:
    """Generate exactly 252 candidate-only rows. Explicit call only.

    Runtime mode (``contexts is None``) deliberately loads and releases one
    (session, asset) block at a time.  This preserves the frozen matrix order
    while avoiding simultaneous in-memory expansion of all four diagnostic
    blocks.  Synthetic tests may pass pre-built contexts directly.
    """
    matrix = expand_fixed_matrix()

    if contexts is not None:
        rows = [
            diagnose_case(case, contexts[(case.session_id, case.asset)])
            for case in matrix
        ]
    else:
        _validate_runtime_safety()
        rows_by_index: list[dict | None] = [None] * len(matrix)
        by_block: dict[tuple[str, str], list[tuple[int, DiagnosticCase]]] = {}
        for idx, case in enumerate(matrix):
            by_block.setdefault((case.session_id, case.asset), []).append((idx, case))

        checked_sessions: set[str] = set()
        for sid in DECLARED_SESSIONS:
            if sid not in checked_sessions:
                coverage_ok, reason = _validate_sync_grid_part_coverage(sid)
                if not coverage_ok:
                    raise RuntimeError(f"Stage1 part coverage failure for {sid}: {reason}")
                checked_sessions.add(sid)
            for asset in ASSETS:
                df = _load_grid_for_block(sid, asset)
                ctx = _prepare_block(sid, asset, df)
                for idx, case in by_block[(sid, asset)]:
                    rows_by_index[idx] = diagnose_case(case, ctx)
                # Drop the potentially large grid/context before opening the next block.
                del ctx
                del df

        if any(row is None for row in rows_by_index):
            raise AssertionError("Stage1 failed to populate every fixed-matrix row")
        rows = [row for row in rows_by_index if row is not None]

    if len(rows) != EXPECTED_ROW_COUNT:
        raise AssertionError(f"Stage1 emitted {len(rows)} rows, expected {EXPECTED_ROW_COUNT}")
    return rows


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        raise ValueError("cannot render empty Stage1 diagnostic rows")
    # Dict insertion order is fixed by _base_row + deterministic updates; use
    # the first row as the canonical field order and assert every row matches.
    fields = list(rows[0].keys())
    for row in rows:
        if set(row.keys()) != set(fields):
            raise AssertionError("Stage1 rows do not share one deterministic schema")
    from io import StringIO

    buf = StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _runtime_git_head() -> str:
    project_root = Path(__file__).resolve().parents[2]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project_root, text=True
        ).strip()
    except Exception as exc:  # pragma: no cover - runtime environment specific
        raise RuntimeError(f"cannot resolve runtime git HEAD: {exc}") from exc


def write_stage1_artifacts(
    rows: list[dict],
    output_dir: Path | str | None = None,
    runtime_source_commit: str | None = None,
) -> dict:
    """Write the two V1.2-only Stage1 artifacts once; never overwrite V1.1."""
    if len(rows) != EXPECTED_ROW_COUNT:
        raise AssertionError(f"refusing artifact write for {len(rows)} rows (expected 252)")
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "reports" / "v1_2"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "stage1_candidate_diagnostics.csv"
    manifest_path = output_dir / "stage1_manifest.json"
    if csv_path.name in _V1_1_REPORT_NAMES or manifest_path.name in _V1_1_REPORT_NAMES:
        raise AssertionError("Stage1 output path collides with a V1.1 report name")
    if csv_path.exists() or manifest_path.exists():
        raise FileExistsError("Stage1 artifacts already exist; refusing silent overwrite")

    data = _csv_bytes(rows)
    csv_path.write_bytes(data)
    csv_sha = hashlib.sha256(data).hexdigest()

    manifest = {
        "diagnostic_spec_version": DIAGNOSTIC_VERSION,
        "runtime_source_commit": runtime_source_commit or _runtime_git_head(),
        "stage0_patch_sha256": STAGE0_PATCH_SHA256,
        "session_ids": list(DECLARED_SESSIONS),
        "strata": {
            "S1": 32,
            "S2": 64,
            "S3": 24,
            "S4": 24,
            "S5": 36,
            "S6": 72,
        },
        "candidate_row_count": len(rows),
        "stage1_candidate_diagnostics_sha256": csv_sha,
        "stage1_candidate_diagnostics_size": len(data),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate_generator_read_historical_target_numerics": False,
        "new36_opened": False,
        "v1_1_reports_modified": False,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    return {
        "row_count": len(rows),
        "csv_path": str(csv_path),
        "csv_sha256": csv_sha,
        "csv_size": len(data),
        "manifest_path": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_size": len(manifest_bytes),
    }


def run_stage1_diagnostics(output_dir: Path | str | None = None) -> dict:
    """Explicit runtime entrypoint: load 4 OLD36 blocks, emit 252 rows, write artifacts."""
    rows = generate_stage1_rows()
    return write_stage1_artifacts(rows, output_dir=output_dir)


__all__ = [
    "ASSETS",
    "DECLARED_SESSIONS",
    "DIAGNOSTIC_VERSION",
    "DiagnosticCase",
    "EXPECTED_ROW_COUNT",
    "expand_fixed_matrix",
    "fingerprint_positions",
    "diagnose_case",
    "generate_stage1_rows",
    "write_stage1_artifacts",
    "run_stage1_diagnostics",
]
