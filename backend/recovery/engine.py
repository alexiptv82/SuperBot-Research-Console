"""RECONSTRUCTION_V1.1 — frozen methodology engine.

Implements the methodology frozen in:
    backend/recovery/specs/RECONSTRUCTION_V1.1_SPEC.txt
    SHA256: 02ecaf121ed9ef5f90f30b9db203889ecda114f8547d83244f5f0f7c27453875

SAFETY INVARIANTS (never remove):
- This module does NOT execute at import time.
- It does NOT auto-run on server start, app start, or checkpoint load.
- It does NOT read NEW36 raw/parquet data (firewall enforced by caller).
- It does NOT set FrozenAnalysisEngine.accepts_input = True.
- It does NOT compare against golden numeric outputs internally.
- Any invocation is explicit via reconstruct_block() or
  reconstruct_session_asset().

All rule references below are to frozen spec lines in
RECONSTRUCTION_V1.1_SPEC.txt.  No behavior is added beyond what the
spec states.  NEW_ASSUMPTION rules are implemented exactly as frozen.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Spec constants (frozen — do not modify)
# ---------------------------------------------------------------------------

# horizons_simple_ms
HORIZONS_SIMPLE_MS: tuple[int, ...] = (100, 200, 500, 1000, 2000, 5000, 10000, 30000)

# horizons_composite_ms
HORIZONS_COMPOSITE_MS: tuple[int, ...] = (1000, 2000, 5000, 10000, 30000)

# quantiles
QUANTILES: tuple[float, ...] = (0.80, 0.90, 0.95)

# grid_ms
GRID_MS: int = 100

# overlap_spacing_steps: max(10, k) where k = H / GRID_MS
# computed per horizon at use

# feature_map: analysis_feature_name -> grid_column_name
FEATURE_MAP: dict[str, str] = {
    "bitget_ofi":          "bitget_ofi_norm_l1",
    "bitget_trade_flow":   "bitget_trade_imbalance_window",
    "depth_imbalance_l1":  "bitget_depth_imbalance_l1",
    "depth_imbalance_l5":  "bitget_depth_imbalance_l5",
    "external_ofi":        "external_ofi_consensus_l1",
    "external_trade_flow": "external_trade_imbalance_consensus",
    "fair_accel_100ms":    "fair_accel_100ms_bps",
    "fair_gap_reversion":  "bitget_gap_to_fair_bps",
    "leader_gap_100ms":    "leader_gap_100ms_bps",
    "leader_gap_200ms":    "leader_gap_200ms_bps",
    "leader_gap_500ms":    "leader_gap_500ms_bps",
    "leader_gap_1000ms":   "leader_gap_1000ms_bps",
}

# Simple features ordered canonically
SIMPLE_FEATURES: tuple[str, ...] = tuple(FEATURE_MAP.keys())

# Composite features ordered canonically
COMPOSITE_FEATURES: tuple[str, ...] = (
    "depthBoth",
    "depthL1_extOFI",
    "gap_depthBoth",
    "gap_depthL1",
    "gap_depth_extOFI",
    "gap_extOFI",
    "gap_leader1000",
    "gap_localOFI",
    "gap_localTrade",
    "leader1000_extOFI",
)

# ---------------------------------------------------------------------------
# Hard-fail sentinels
# ---------------------------------------------------------------------------

CORRUPT_GRID = "CORRUPT_GRID"


class CorruptGridError(ValueError):
    """Raised when a hard-fail invariant is violated (spec §invalid_block_conditions)."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BlockMetrics:
    """Per (feature, horizon_ms, q) result for one (session_id, asset) block.

    Undefined fields (N=0 case) are stored as None.
    dispersion sub-metrics appear in ``dispersion_states`` only for
    fair_gap_reversion.
    """
    session_id: str
    asset: str
    feature: str
    horizon_ms: int
    q: float
    N: int
    threshold: float | None        # None when undefined (< 2 finite values)
    mean_signed_bps: float | None  # None when N=0
    hit_rate: float | None         # None when N=0
    median_signed_bps: float | None  # None when N=0, simple features only
    mean_abs_move: float | None    # None when no eligible rows, simple features only
    # Dispersion sub-metrics (fair_gap_reversion only). Each entry is
    # {"state": "low"|"mid"|"high", "N": int, "mean_signed_bps": float|None,
    #  "hit_rate": float|None, "disp_lo": float|None, "disp_hi": float|None}
    dispersion_states: list[dict] = field(default_factory=list)


@dataclass
class BlockSummary:
    """All per-block metrics for one (session_id, asset) block, plus
    structural metadata."""
    session_id: str
    asset: str
    valid: bool               # False if INVALID block (hard fail)
    invalid_reason: str | None
    metrics: list[BlockMetrics] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 0: grid validation and canonical grid_pos assignment
# ---------------------------------------------------------------------------


def build_canonical_grid(df: pd.DataFrame) -> pd.DataFrame:
    """Sort the grid, validate duplicate-key policy, assign immutable grid_pos.

    Spec lines:
        row_sort_primary=local_ts_ms_asc
        row_sort_secondary=sample_monotonic_ns_asc
        row_sort_algorithm=stable
        row_sort_tie_on_both_keys=CORRUPT_GRID_HARD_FAIL
        duplicate_key_policy=CORRUPT_GRID_HARD_FAIL_on(local_ts_ms,sample_monotonic_ns)_exact_duplicate
        duplicate_timestamp_different_monotonic_ns=VALID_NOT_A_VIOLATION
        grid_pos_assignment=once_immediately_after_stable_sort;range=0..L-1;immutable
        invalid_block_conditions=[...duplicate_sort_key_exact, duplicate_full_row]

    Returns a new DataFrame with an immutable ``grid_pos`` column (0-based
    integer index into the canonical sorted array). Raises CorruptGridError
    on hard-fail conditions.
    """
    if df.empty:
        raise CorruptGridError("Empty DataFrame supplied to build_canonical_grid")

    # Stable sort on (local_ts_ms asc, sample_monotonic_ns asc)
    out = df.sort_values(
        by=["local_ts_ms", "sample_monotonic_ns"],
        ascending=[True, True],
        kind="stable",
    ).reset_index(drop=True)

    # Check for exact duplicate sort key (local_ts_ms, sample_monotonic_ns)
    dup_key = out.duplicated(subset=["local_ts_ms", "sample_monotonic_ns"], keep=False)
    if dup_key.any():
        raise CorruptGridError(
            f"CORRUPT_GRID: duplicate (local_ts_ms, sample_monotonic_ns) key found — "
            f"{int(dup_key.sum())} affected rows"
        )

    # Check for exact duplicate full rows
    all_cols = list(out.columns)
    dup_full = out.duplicated(subset=all_cols, keep=False)
    if dup_full.any():
        raise CorruptGridError(
            f"CORRUPT_GRID: exact duplicate full rows found — "
            f"{int(dup_full.sum())} affected rows"
        )

    # Assign immutable grid_pos: 0 .. L-1
    # Must be done once immediately after stable sort.
    out = out.copy()
    out["grid_pos"] = np.arange(len(out), dtype=np.int64)

    return out


# ---------------------------------------------------------------------------
# Phase 0: quality gate
# ---------------------------------------------------------------------------


def _quality_admissible_mask(df: pd.DataFrame) -> pd.Series:
    """Return a boolean mask of quality-admissible rows.

    Spec: quality_gate=(adjusted_fair_venue_count>=2)
                       AND(bitget_book_age_recv_ms<=1000)
                       AND(bitget_mid finite)AND(bitget_mid>0)
    quality_gate_scope=base_row_only;NOT_reapplied_at_forward_endpoint
    """
    col_mid  = "bitget_mid"
    col_age  = "bitget_book_age_recv_ms"
    col_fvc  = "adjusted_fair_venue_count"

    mid_finite_pos = (
        df[col_mid].notna() & np.isfinite(df[col_mid]) & (df[col_mid] > 0.0)
    )
    age_ok = df[col_age].notna() & (df[col_age] <= 1000.0)
    fvc_ok = df[col_fvc].notna() & (df[col_fvc] >= 2.0)

    return mid_finite_pos & age_ok & fvc_ok


# ---------------------------------------------------------------------------
# Phase A: rank-signed-uniform component normalization
# ---------------------------------------------------------------------------


def _rank_signed_uniform(series: pd.Series, quality_mask: pd.Series) -> pd.Series:
    """Compute per-block rank-signed-uniform transform for one component.

    Spec:
        component_normalization=per_block_rank_signed_uniform:
          n=count(finite(component)_among_quality_admissible_rows);
          z=2*(rank_avgties/(n+1))-1;
          computed_per_component_independently
        rank_tie_definition=each_of_m_tied_values_receives_arithmetic_mean_of_the_m_integer_ranks

    Only quality-admissible rows with finite component values receive a
    z-score.  All other rows get NaN.
    """
    eligible = quality_mask & series.notna() & np.isfinite(series.astype(float))
    out = pd.Series(np.nan, index=series.index, dtype=float)
    sub = series[eligible].astype(float)
    if len(sub) == 0:
        return out
    n = len(sub)
    # average rank (1-based) — ties receive arithmetic mean of their ranks
    ranks = sub.rank(method="average")
    z = 2.0 * (ranks / (n + 1.0)) - 1.0
    out.loc[eligible] = z
    return out


# ---------------------------------------------------------------------------
# Phase A: type-7 quantile
# ---------------------------------------------------------------------------


def _quantile_type7(values: np.ndarray, q: float) -> float | None:
    """Compute a single type-7 (linear-interpolation) quantile.

    Spec: quantile_method=linear_type7
         threshold_undefined_if_fewer_than_2_finite_values_in_relevant_domain=>N=0

    Returns None if fewer than 2 values.
    """
    finite = values[np.isfinite(values)]
    if len(finite) < 2:
        return None
    return float(np.quantile(finite, q, method="linear"))


# ---------------------------------------------------------------------------
# Phase A/B: forward return array
# ---------------------------------------------------------------------------


def _build_forward_return_array(
    canonical_mid: np.ndarray,
    k: int,
) -> np.ndarray:
    """Build forward log-return array (bps) for every grid_pos.

    Spec:
        target=ln(canonical_mid[grid_pos+H/100]/canonical_mid[grid_pos])*10000
        forward_endpoint_validity_rule=canonical_mid[grid_pos+k]finite_AND_gt_0_only;
            full_gate_not_reapplied;NEW_ASSUMPTION
        session_end=drop_if(grid_pos+k>last_index);per_horizon_independent

    Returns array of length L (same as canonical_mid).
    NaN at grid_pos i if:
      - grid_pos + k > last_index  (session-end truncation)
      - canonical_mid[grid_pos+k] is not finite or <= 0
      - canonical_mid[grid_pos] is not finite or <= 0 (base)
    """
    L = len(canonical_mid)
    ret = np.full(L, np.nan, dtype=float)
    last_index = L - 1

    # Base mid for quality rows (quality gate NOT re-applied at endpoint)
    base_mid = canonical_mid.copy().astype(float)

    for i in range(L):
        ep = i + k
        if ep > last_index:
            continue  # session_end drop
        m0 = base_mid[i]
        m1 = base_mid[ep]
        if (
            math.isfinite(m0) and m0 > 0.0
            and math.isfinite(m1) and m1 > 0.0
        ):
            ret[i] = math.log(m1 / m0) * 10000.0

    return ret


# ---------------------------------------------------------------------------
# Phase B: greedy earliest-first overlap filter
# ---------------------------------------------------------------------------


def _greedy_overlap_filter(
    event_grid_positions: np.ndarray,
    spacing: int,
) -> np.ndarray:
    """Apply greedy earliest-first non-overlap filter.

    Spec:
        overlap_ordering=greedy_earliest_first;iteration_order=ascending_canonical_grid_pos
        overlap_spacing_steps=max(10,k)   # inclusive gap>=spacing

    Input: sorted array of grid_pos values of candidate events.
    Returns: boolean mask (same length as input) — True if accepted.
    """
    n = len(event_grid_positions)
    accepted = np.zeros(n, dtype=bool)
    last_pos = -spacing - 1  # ensures first candidate always accepted

    for i in range(n):
        pos = event_grid_positions[i]
        if pos - last_pos >= spacing:
            accepted[i] = True
            last_pos = pos

    return accepted


# ---------------------------------------------------------------------------
# Phase B: simple feature block computation
# ---------------------------------------------------------------------------


def _compute_simple_feature(
    grid_pos: np.ndarray,          # immutable grid positions (length L)
    quality_mask: np.ndarray,      # bool, length L
    signal: np.ndarray,            # raw feature values, length L, NaN allowed
    forward_ret: np.ndarray,       # bps, length L, NaN where invalid
    fwd_eligible: np.ndarray,      # bool: session_end_eligible AND forward_endpoint_valid
    k: int,
    q: float,
    is_gap_reversion: bool = False,
    dispersion_values: np.ndarray | None = None,
    disp_lo: float | None = None,
    disp_hi: float | None = None,
) -> dict:
    """Compute block metrics for one simple feature × horizon × q.

    Spec line references throughout inline below.
    Returns a dict matching BlockMetrics fields (minus session_id/asset/feature).
    """
    spacing = max(10, k)

    # ── Phase A: simple_domain = quality_admissible AND finite(signal) ──────
    simple_domain_mask = quality_mask & np.isfinite(signal.astype(float))

    # sign convention
    if is_gap_reversion:
        # sign_fair_gap_reversion=-sign(gap)
        raw_dir = -np.sign(signal.astype(float))
    else:
        # sign_simple=+sign(signal)
        raw_dir = np.sign(signal.astype(float))

    # Threshold: quantile of abs(signal) over simple_domain (PhaseA, horizon_independent)
    thresh_vals = np.abs(signal.astype(float)[simple_domain_mask])
    threshold = _quantile_type7(thresh_vals, q)

    # ── Phase B ──────────────────────────────────────────────────────────────
    if threshold is None:
        # fewer than 2 finite values → N=0
        N = 0
        mean_sret = None
        hit_rate  = None
        median_sret = None
    else:
        # event_rule: within simple_domain AND gate_series!=0 AND |gate|>=threshold
        gate = np.abs(signal.astype(float))
        event_mask = (
            simple_domain_mask
            & (signal.astype(float) != 0.0)   # exclusive zero
            & (gate >= threshold)               # inclusive threshold
        )

        # forward_return validity (PhaseB)
        event_mask = event_mask & np.isfinite(forward_ret)

        # session_end already encoded in forward_ret as NaN; redundant but explicit
        candidate_positions = grid_pos[event_mask]
        candidate_returns   = forward_ret[event_mask]
        candidate_dirs      = raw_dir[event_mask]

        # greedy earliest-first overlap
        if len(candidate_positions) == 0:
            N = 0
            mean_sret   = None
            hit_rate    = None
            median_sret = None
        else:
            accept_mask = _greedy_overlap_filter(candidate_positions, spacing)
            accepted_returns = candidate_returns[accept_mask]
            accepted_dirs    = candidate_dirs[accept_mask]
            signed_returns   = accepted_dirs * accepted_returns
            N = int(len(signed_returns))
            if N == 0:
                mean_sret   = None
                hit_rate    = None
                median_sret = None
            else:
                mean_sret   = float(np.mean(signed_returns))
                hit_rate    = float(np.mean(signed_returns > 0.0))
                median_sret = float(np.median(signed_returns))

    # ── mean_abs_move (simple only) ──────────────────────────────────────────
    # Spec: mean_abs_move_eligible_rows = quality_admissible AND
    #       session_end_eligible(H) AND forward_endpoint_valid(H)
    #       regardless of any feature threshold crossing
    #       mean_abs_move_requires_feature_signal_finite=false
    mean_abs_move: float | None = None
    if fwd_eligible.any():
        mam_vals = np.abs(forward_ret[fwd_eligible])
        finite_mam = mam_vals[np.isfinite(mam_vals)]
        if len(finite_mam) > 0:
            mean_abs_move = float(np.mean(finite_mam))

    # ── Dispersion sub-metrics (fair_gap_reversion only) ─────────────────────
    dispersion_states: list[dict] = []
    if (
        is_gap_reversion
        and dispersion_values is not None
        and disp_lo is not None
        and disp_hi is not None
        and N > 0
        and threshold is not None
    ):
        # Re-collect accepted events to build dispersion splits
        gate2 = np.abs(signal.astype(float))
        event_mask2 = (
            simple_domain_mask
            & (signal.astype(float) != 0.0)
            & (gate2 >= threshold)
            & np.isfinite(forward_ret)
        )
        cpos2    = grid_pos[event_mask2]
        cret2    = forward_ret[event_mask2]
        cdir2    = raw_dir[event_mask2]
        cdisp2   = dispersion_values[event_mask2]

        if len(cpos2) > 0:
            acc2  = _greedy_overlap_filter(cpos2, spacing)
            adisp = cdisp2[acc2]
            aret2 = cret2[acc2]
            adir2 = cdir2[acc2]

            for state in ("low", "mid", "high"):
                if state == "low":
                    state_mask = adisp < disp_lo
                elif state == "high":
                    state_mask = adisp >= disp_hi
                else:
                    state_mask = (adisp >= disp_lo) & (adisp < disp_hi)

                srets = (adir2 * aret2)[state_mask]
                sN = int(np.sum(state_mask))
                dispersion_states.append({
                    "state": state,
                    "N": sN,
                    "mean_signed_bps": float(np.mean(srets)) if sN > 0 else None,
                    "hit_rate": float(np.mean(srets > 0)) if sN > 0 else None,
                    "disp_lo": disp_lo,
                    "disp_hi": disp_hi,
                })

    return {
        "N": N,
        "threshold": threshold,
        "mean_signed_bps": mean_sret,
        "hit_rate": hit_rate,
        "median_signed_bps": median_sret,
        "mean_abs_move": mean_abs_move,
        "dispersion_states": dispersion_states,
    }


# ---------------------------------------------------------------------------
# Phase B: derived (standalone) composite feature
# ---------------------------------------------------------------------------


def _compute_derived_composite(
    grid_pos: np.ndarray,
    quality_mask: np.ndarray,
    z1: np.ndarray,           # rank-signed-uniform of component_1
    z2: np.ndarray,           # rank-signed-uniform of component_2
    forward_ret: np.ndarray,
    k: int,
    q: float,
) -> dict:
    """depthBoth or depthL1_extOFI.

    Spec:
        derived_domain = quality_admissible AND finite(component_1) AND finite(component_2)
        D = mean(z1, z2)
        dir = +sign(D)
        threshold = quantile(|D|, q) over derived_domain
        event: within derived_domain AND D!=0 AND |D|>=threshold AND forward finite
    """
    spacing = max(10, k)

    # derived_domain: quality_admissible AND finite(z1) AND finite(z2)
    # z1/z2 are NaN outside quality_admissible or where component is non-finite
    derived_domain = quality_mask & np.isfinite(z1) & np.isfinite(z2)

    D = (z1 + z2) / 2.0  # arithmetic mean of z-scores (equal weights)
    D = np.where(derived_domain, D, np.nan)

    # Threshold: quantile of |D| over derived_domain
    thresh_vals = np.abs(D[derived_domain])
    threshold = _quantile_type7(thresh_vals, q)

    if threshold is None:
        return {
            "N": 0, "threshold": None,
            "mean_signed_bps": None, "hit_rate": None,
            "median_signed_bps": None, "mean_abs_move": None,
            "dispersion_states": [],
        }

    # event_rule
    dir_arr = np.sign(D)
    event_mask = (
        derived_domain
        & (D != 0.0)
        & (np.abs(D) >= threshold)
        & np.isfinite(forward_ret)
    )

    cpos  = grid_pos[event_mask]
    cret  = forward_ret[event_mask]
    cdir  = dir_arr[event_mask]

    if len(cpos) == 0:
        return {
            "N": 0, "threshold": threshold,
            "mean_signed_bps": None, "hit_rate": None,
            "median_signed_bps": None, "mean_abs_move": None,
            "dispersion_states": [],
        }

    acc = _greedy_overlap_filter(cpos, spacing)
    signed_returns = cret[acc] * cdir[acc]
    N = int(len(signed_returns))

    return {
        "N": N,
        "threshold": threshold,
        "mean_signed_bps": float(np.mean(signed_returns)) if N > 0 else None,
        "hit_rate": float(np.mean(signed_returns > 0)) if N > 0 else None,
        "median_signed_bps": None,   # composite only — not computed for composites
        "mean_abs_move": None,       # composite only
        "dispersion_states": [],
    }


# ---------------------------------------------------------------------------
# Phase B: gap_* composite features
# ---------------------------------------------------------------------------


def _compute_gap_composite(
    grid_pos: np.ndarray,
    quality_mask: np.ndarray,
    gap: np.ndarray,              # bitget_gap_to_fair_bps
    forward_ret: np.ndarray,
    k: int,
    q: float,
    gap_threshold: float | None,  # pre-computed (shared across all gap_*)
    confirmation_finite_mask: np.ndarray,  # True where confirmation_component is finite/valid
    alignment_mask: np.ndarray,            # True where alignment condition holds
) -> dict:
    """Generic gap_* composite processor.

    Spec:
        gap_threshold_domain = quality_admissible AND finite(gap)
        gap_event_domain = gap_threshold_domain AND finite(confirmation_component)
        threshold = quantile(|gap|, q) over gap_threshold_domain (pre-computed)
        dir = -sign(gap)
        event: within gap_event_domain AND gap!=0 AND |gap|>=threshold
               AND alignment AND forward finite
    """
    spacing = max(10, k)

    if gap_threshold is None:
        return {
            "N": 0, "threshold": None,
            "mean_signed_bps": None, "hit_rate": None,
            "median_signed_bps": None, "mean_abs_move": None,
            "dispersion_states": [],
        }

    # gap_event_domain
    gap_threshold_domain = quality_mask & np.isfinite(gap.astype(float))
    gap_event_domain = gap_threshold_domain & confirmation_finite_mask

    dir_arr = -np.sign(gap.astype(float))

    event_mask = (
        gap_event_domain
        & (gap.astype(float) != 0.0)
        & (np.abs(gap.astype(float)) >= gap_threshold)
        & alignment_mask
        & np.isfinite(forward_ret)
    )

    cpos = grid_pos[event_mask]
    cret = forward_ret[event_mask]
    cdir = dir_arr[event_mask]

    if len(cpos) == 0:
        return {
            "N": 0, "threshold": gap_threshold,
            "mean_signed_bps": None, "hit_rate": None,
            "median_signed_bps": None, "mean_abs_move": None,
            "dispersion_states": [],
        }

    acc = _greedy_overlap_filter(cpos, spacing)
    signed_returns = cret[acc] * cdir[acc]
    N = int(len(signed_returns))

    return {
        "N": N,
        "threshold": gap_threshold,
        "mean_signed_bps": float(np.mean(signed_returns)) if N > 0 else None,
        "hit_rate": float(np.mean(signed_returns > 0)) if N > 0 else None,
        "median_signed_bps": None,
        "mean_abs_move": None,
        "dispersion_states": [],
    }


# ---------------------------------------------------------------------------
# Phase B: leader1000_extOFI composite
# ---------------------------------------------------------------------------


def _compute_leader1000_extofi(
    grid_pos: np.ndarray,
    quality_mask: np.ndarray,
    leader1000: np.ndarray,       # leader_gap_1000ms_bps
    ext_ofi: np.ndarray,          # external_ofi_consensus_l1
    forward_ret: np.ndarray,
    k: int,
    q: float,
    leader_threshold: float | None,  # pre-computed from leader_threshold_domain
) -> dict:
    """leader1000_extOFI composite.

    Spec:
        leader_threshold_domain = quality_admissible AND finite(leader_gap_1000ms)
        leader_event_domain = leader_threshold_domain AND finite(external_ofi)
        threshold = quantile(|leader_gap_1000ms|, q) over leader_threshold_domain
        dir = +sign(leader_gap_1000ms_bps)
        event: within leader_event_domain AND leader1000!=0 AND |leader1000|>=threshold
               AND sign(external_ofi)==sign(leader_gap_1000ms) AND external_ofi!=0 AND forward finite
    """
    spacing = max(10, k)

    if leader_threshold is None:
        return {
            "N": 0, "threshold": None,
            "mean_signed_bps": None, "hit_rate": None,
            "median_signed_bps": None, "mean_abs_move": None,
            "dispersion_states": [],
        }

    leader_threshold_domain = quality_mask & np.isfinite(leader1000.astype(float))
    leader_event_domain = (
        leader_threshold_domain & np.isfinite(ext_ofi.astype(float))
    )

    dir_arr = np.sign(leader1000.astype(float))

    # alignment: sign(external_ofi)==sign(leader_gap_1000ms) AND both nonzero
    alignment = (
        (np.sign(ext_ofi.astype(float)) == np.sign(leader1000.astype(float)))
        & (ext_ofi.astype(float) != 0.0)
        & (leader1000.astype(float) != 0.0)
    )

    event_mask = (
        leader_event_domain
        & (leader1000.astype(float) != 0.0)
        & (np.abs(leader1000.astype(float)) >= leader_threshold)
        & alignment
        & np.isfinite(forward_ret)
    )

    cpos = grid_pos[event_mask]
    cret = forward_ret[event_mask]
    cdir = dir_arr[event_mask]

    if len(cpos) == 0:
        return {
            "N": 0, "threshold": leader_threshold,
            "mean_signed_bps": None, "hit_rate": None,
            "median_signed_bps": None, "mean_abs_move": None,
            "dispersion_states": [],
        }

    acc = _greedy_overlap_filter(cpos, spacing)
    signed_returns = cret[acc] * cdir[acc]
    N = int(len(signed_returns))

    return {
        "N": N,
        "threshold": leader_threshold,
        "mean_signed_bps": float(np.mean(signed_returns)) if N > 0 else None,
        "hit_rate": float(np.mean(signed_returns > 0)) if N > 0 else None,
        "median_signed_bps": None,
        "mean_abs_move": None,
        "dispersion_states": [],
    }


# ---------------------------------------------------------------------------
# Main block-level entry point
# ---------------------------------------------------------------------------


def reconstruct_block(
    session_id: str,
    asset: str,
    df: pd.DataFrame,
) -> BlockSummary:
    """Reconstruct all metrics for one (session_id, asset) block.

    ``df`` must already be the concatenated, filename-ascending-ordered
    DataFrame for this block (part_concat_order / part_coverage_policy
    are the CALLER's responsibility — see sandbox.py / harness.py).

    This function:
    1. Validates and sorts the grid (CORRUPT_GRID hard-fail conditions).
    2. Assigns immutable grid_pos.
    3. Applies quality gate.
    4. Computes all simple-feature × horizon × q combinations.
    5. Computes all composite-feature × horizon × q combinations.
    6. Returns a BlockSummary (valid=True/False).

    SAFETY: does NOT auto-execute at import time, does NOT set
    accepts_input, does NOT compare against golden values.
    """
    # ── Grid validation ──────────────────────────────────────────────────────
    try:
        grid = build_canonical_grid(df)
    except CorruptGridError as exc:
        return BlockSummary(
            session_id=session_id,
            asset=asset,
            valid=False,
            invalid_reason=str(exc),
        )

    L = len(grid)
    gpos = grid["grid_pos"].to_numpy(dtype=np.int64)
    canonical_mid = grid["bitget_mid"].to_numpy(dtype=float)

    # ── Quality gate ─────────────────────────────────────────────────────────
    quality_mask = _quality_admissible_mask(grid).to_numpy(dtype=bool)

    # ── Feature columns (raw) ────────────────────────────────────────────────
    def _col(col_name: str) -> np.ndarray:
        if col_name not in grid.columns:
            return np.full(L, np.nan, dtype=float)
        return grid[col_name].to_numpy(dtype=float)

    gap_raw        = _col("bitget_gap_to_fair_bps")
    leader1000_raw = _col("leader_gap_1000ms_bps")
    ext_ofi_raw    = _col("external_ofi_consensus_l1")
    di_l1_raw      = _col("bitget_depth_imbalance_l1")
    di_l5_raw      = _col("bitget_depth_imbalance_l5")
    fair_ofi_align = _col("bitget_fair_ofi_alignment")
    fair_trd_align = _col("bitget_fair_trade_alignment")
    disp_raw       = _col("external_perp_dispersion_bps")

    # ── Component z-scores (computed once, per component, independently) ─────
    # Spec: component_normalization computed_per_component_independently
    z_di_l1 = _rank_signed_uniform(
        pd.Series(di_l1_raw, dtype=float), pd.Series(quality_mask, dtype=bool)
    ).to_numpy(dtype=float)
    z_di_l5 = _rank_signed_uniform(
        pd.Series(di_l5_raw, dtype=float), pd.Series(quality_mask, dtype=bool)
    ).to_numpy(dtype=float)
    z_ext_ofi = _rank_signed_uniform(
        pd.Series(ext_ofi_raw, dtype=float), pd.Series(quality_mask, dtype=bool)
    ).to_numpy(dtype=float)

    # ── Gap threshold (shared across all gap_* composites) ───────────────────
    # gap_threshold_domain = quality_admissible AND finite(gap)
    gap_thresh_domain = quality_mask & np.isfinite(gap_raw)
    gap_thresh_per_q: dict[float, float | None] = {}
    for q_val in QUANTILES:
        gap_thresh_per_q[q_val] = _quantile_type7(
            np.abs(gap_raw[gap_thresh_domain]), q_val
        )

    # ── Leader1000 threshold (shared for leader1000_extOFI) ──────────────────
    # leader_threshold_domain = quality_admissible AND finite(leader_gap_1000ms)
    leader_thresh_domain = quality_mask & np.isfinite(leader1000_raw)
    leader_thresh_per_q: dict[float, float | None] = {}
    for q_val in QUANTILES:
        leader_thresh_per_q[q_val] = _quantile_type7(
            np.abs(leader1000_raw[leader_thresh_domain]), q_val
        )

    # ── Dispersion tertile boundaries (per-block, PhaseA) ────────────────────
    # dispersion_n = count(finite_among_quality_admissible_rows)
    disp_domain = quality_mask & np.isfinite(disp_raw)
    disp_valid = disp_raw[disp_domain]
    if len(disp_valid) >= 2:
        disp_lo = float(np.quantile(disp_valid, 1.0 / 3.0, method="linear"))
        disp_hi = float(np.quantile(disp_valid, 2.0 / 3.0, method="linear"))
    else:
        disp_lo = None
        disp_hi = None

    # ── Collect results ───────────────────────────────────────────────────────
    metrics: list[BlockMetrics] = []

    # ── Simple features ───────────────────────────────────────────────────────
    for feat in SIMPLE_FEATURES:
        col_name = FEATURE_MAP[feat]
        signal = _col(col_name)
        is_gap_rev = feat == "fair_gap_reversion"

        for H in HORIZONS_SIMPLE_MS:
            k = H // GRID_MS  # steps

            # Forward return array for this horizon
            fwd = _build_forward_return_array(canonical_mid, k)

            # mean_abs_move eligible rows:
            # quality_admissible AND session_end_eligible(H) AND forward_endpoint_valid(H)
            # (regardless of feature signal finiteness)
            fwd_eligible = quality_mask & np.isfinite(fwd)

            for q_val in QUANTILES:
                r = _compute_simple_feature(
                    grid_pos=gpos,
                    quality_mask=quality_mask,
                    signal=signal,
                    forward_ret=fwd,
                    fwd_eligible=fwd_eligible,
                    k=k,
                    q=q_val,
                    is_gap_reversion=is_gap_rev,
                    dispersion_values=disp_raw if is_gap_rev else None,
                    disp_lo=disp_lo if is_gap_rev else None,
                    disp_hi=disp_hi if is_gap_rev else None,
                )
                metrics.append(BlockMetrics(
                    session_id=session_id,
                    asset=asset,
                    feature=feat,
                    horizon_ms=H,
                    q=q_val,
                    N=r["N"],
                    threshold=r["threshold"],
                    mean_signed_bps=r["mean_signed_bps"],
                    hit_rate=r["hit_rate"],
                    median_signed_bps=r["median_signed_bps"],
                    mean_abs_move=r["mean_abs_move"],
                    dispersion_states=r["dispersion_states"],
                ))

    # ── Composite features (horizons_composite_ms only) ───────────────────────
    for H in HORIZONS_COMPOSITE_MS:
        k = H // GRID_MS
        fwd = _build_forward_return_array(canonical_mid, k)

        for q_val in QUANTILES:

            # ── depthBoth ────────────────────────────────────────────────────
            r = _compute_derived_composite(
                grid_pos=gpos,
                quality_mask=quality_mask,
                z1=z_di_l1,
                z2=z_di_l5,
                forward_ret=fwd,
                k=k,
                q=q_val,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="depthBoth", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None,
                mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── depthL1_extOFI ───────────────────────────────────────────────
            r = _compute_derived_composite(
                grid_pos=gpos,
                quality_mask=quality_mask,
                z1=z_di_l1,
                z2=z_ext_ofi,
                forward_ret=fwd,
                k=k,
                q=q_val,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="depthL1_extOFI", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None,
                mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── gap_* composites ─────────────────────────────────────────────
            gap_t = gap_thresh_per_q[q_val]

            # Shared: gap_threshold_domain
            gap_thresh_dom_mask = quality_mask & np.isfinite(gap_raw)

            # D_depthBoth (used in gap_depthBoth and gap_depth_extOFI)
            D_depthBoth = np.where(
                np.isfinite(z_di_l1) & np.isfinite(z_di_l5),
                (z_di_l1 + z_di_l5) / 2.0,
                np.nan,
            )
            D_depth_extOFI = np.where(
                np.isfinite(z_di_l1) & np.isfinite(z_ext_ofi),
                (z_di_l1 + z_ext_ofi) / 2.0,
                np.nan,
            )

            # ── gap_depthBoth ────────────────────────────────────────────────
            # confirmation_component = D_depthBoth
            # alignment: sign(D_depthBoth) == sign(-sign(gap)) AND D_depthBoth != 0
            conf_fin = np.isfinite(D_depthBoth)
            alg = (
                (np.sign(D_depthBoth) == -np.sign(gap_raw))
                & (D_depthBoth != 0.0)
                & np.isfinite(D_depthBoth)
            )
            r = _compute_gap_composite(
                gpos, quality_mask, gap_raw, fwd, k, q_val,
                gap_t, conf_fin, alg,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="gap_depthBoth", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── gap_depthL1 ──────────────────────────────────────────────────
            # confirmation_component = depth_imbalance_l1 (raw)
            # alignment: sign(di_l1) == sign(-gap) AND di_l1 != 0
            conf_fin = np.isfinite(di_l1_raw)
            alg = (
                (np.sign(di_l1_raw) == -np.sign(gap_raw))
                & (di_l1_raw != 0.0)
            )
            r = _compute_gap_composite(
                gpos, quality_mask, gap_raw, fwd, k, q_val,
                gap_t, conf_fin, alg,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="gap_depthL1", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── gap_depth_extOFI ─────────────────────────────────────────────
            # confirmation_component = mean(z(di_l1), z(ext_ofi))
            # alignment: sign(D_depth_extOFI) == sign(-gap) AND D != 0
            conf_fin = np.isfinite(D_depth_extOFI)
            alg = (
                (np.sign(D_depth_extOFI) == -np.sign(gap_raw))
                & (D_depth_extOFI != 0.0)
                & np.isfinite(D_depth_extOFI)
            )
            r = _compute_gap_composite(
                gpos, quality_mask, gap_raw, fwd, k, q_val,
                gap_t, conf_fin, alg,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="gap_depth_extOFI", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── gap_extOFI ───────────────────────────────────────────────────
            # confirmation_component = external_ofi (raw)
            # alignment: sign(ext_ofi) == sign(-gap) AND ext_ofi != 0
            conf_fin = np.isfinite(ext_ofi_raw)
            alg = (
                (np.sign(ext_ofi_raw) == -np.sign(gap_raw))
                & (ext_ofi_raw != 0.0)
            )
            r = _compute_gap_composite(
                gpos, quality_mask, gap_raw, fwd, k, q_val,
                gap_t, conf_fin, alg,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="gap_extOFI", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── gap_leader1000 ───────────────────────────────────────────────
            # confirmation_component = leader_gap_1000ms (raw)
            # alignment: sign(leader1000) == sign(-gap) AND leader1000 != 0
            conf_fin = np.isfinite(leader1000_raw)
            alg = (
                (np.sign(leader1000_raw) == -np.sign(gap_raw))
                & (leader1000_raw != 0.0)
            )
            r = _compute_gap_composite(
                gpos, quality_mask, gap_raw, fwd, k, q_val,
                gap_t, conf_fin, alg,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="gap_leader1000", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── gap_localOFI ─────────────────────────────────────────────────
            # confirmation_component = bitget_fair_ofi_alignment flag
            # gap_event_domain = gap_threshold_domain AND (fair_ofi_align is not NaN)
            # alignment: bitget_fair_ofi_alignment == +1
            conf_fin = np.isfinite(fair_ofi_align)
            alg = (fair_ofi_align == 1.0)
            r = _compute_gap_composite(
                gpos, quality_mask, gap_raw, fwd, k, q_val,
                gap_t, conf_fin, alg,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="gap_localOFI", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── gap_localTrade ───────────────────────────────────────────────
            # confirmation_component = bitget_fair_trade_alignment flag
            # alignment: bitget_fair_trade_alignment == +1
            conf_fin = np.isfinite(fair_trd_align)
            alg = (fair_trd_align == 1.0)
            r = _compute_gap_composite(
                gpos, quality_mask, gap_raw, fwd, k, q_val,
                gap_t, conf_fin, alg,
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="gap_localTrade", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

            # ── leader1000_extOFI ────────────────────────────────────────────
            r = _compute_leader1000_extofi(
                grid_pos=gpos,
                quality_mask=quality_mask,
                leader1000=leader1000_raw,
                ext_ofi=ext_ofi_raw,
                forward_ret=fwd,
                k=k,
                q=q_val,
                leader_threshold=leader_thresh_per_q[q_val],
            )
            metrics.append(BlockMetrics(
                session_id=session_id, asset=asset,
                feature="leader1000_extOFI", horizon_ms=H, q=q_val,
                N=r["N"], threshold=r["threshold"],
                mean_signed_bps=r["mean_signed_bps"],
                hit_rate=r["hit_rate"],
                median_signed_bps=None, mean_abs_move=None,
                dispersion_states=[],
            ))

    return BlockSummary(
        session_id=session_id,
        asset=asset,
        valid=True,
        invalid_reason=None,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Aggregation across blocks
# ---------------------------------------------------------------------------


@dataclass
class AggregateMetrics:
    """Feature-level aggregate across (session_id, asset) blocks.

    Spec: aggregate rules applied per (feature, horizon_ms, q).
    """
    feature: str
    horizon_ms: int
    q: float
    total_blocks: int
    aggregate_N: int
    n_gt_0_blocks: int
    feature_mean: float | None    # unweighted mean of mean_signed_bps over n_gt_0_blocks
    avg_hit_rate: float | None
    positive_blocks_count: int
    positive_share: float | None
    min_block: tuple[str, str] | None  # (session_id, asset)
    max_block: tuple[str, str] | None
    min_block_mean: float | None
    max_block_mean: float | None


def aggregate_blocks(summaries: Sequence[BlockSummary]) -> list[AggregateMetrics]:
    """Aggregate per-block results across sessions/assets.

    Spec:
        total_blocks = count of structurally VALID blocks reaching PhaseB
        n_gt_0_blocks = subset with N>0
        feature_mean = unweighted_mean(mean_signed_bps) over n_gt_0_blocks
        avg_hit_rate = unweighted_mean(hit_rate) over n_gt_0_blocks
        positive_blocks_count = count(n_gt_0_blocks where mean_signed_bps>0)
        positive_share = positive_blocks_count / count(n_gt_0_blocks)
        min_block/max_block: tie_break=ascending_lexical(session_id,asset)
        btc_eth = independent_blocks; no_cross_asset_weighting

    SAFETY: does NOT read golden values; does NOT compare against golden outputs.
    """
    # Index: (feature, horizon_ms, q) -> list of BlockMetrics
    from collections import defaultdict
    key_map: dict[tuple, list[BlockMetrics]] = defaultdict(list)

    for s in summaries:
        if not s.valid:
            continue  # invalid blocks excluded from all aggregation
        for m in s.metrics:
            key_map[(m.feature, m.horizon_ms, m.q)].append(m)

    results: list[AggregateMetrics] = []

    for (feat, H, q), block_list in key_map.items():
        total_blocks = len(block_list)
        aggregate_N  = sum(m.N for m in block_list)

        n_gt_0 = [m for m in block_list if m.N > 0]
        n_gt_0_count = len(n_gt_0)

        if n_gt_0_count == 0:
            feature_mean = None
            avg_hit_rate = None
            positive_blocks_count = 0
            positive_share = None
            min_block = None
            max_block = None
            min_block_mean = None
            max_block_mean = None
        else:
            means     = [m.mean_signed_bps for m in n_gt_0]
            hit_rates = [m.hit_rate        for m in n_gt_0]
            feature_mean = float(np.mean([x for x in means if x is not None]))
            avg_hit_rate = float(np.mean([x for x in hit_rates if x is not None]))
            positive_blocks_count = sum(
                1 for m in n_gt_0
                if m.mean_signed_bps is not None and m.mean_signed_bps > 0
            )
            positive_share = (
                positive_blocks_count / n_gt_0_count if n_gt_0_count > 0 else None
            )

            # min/max: find the extreme numeric mean FIRST, then among
            # blocks exactly tied at that extreme value choose the
            # lexicographically SMALLEST (session_id, asset).
            #
            # ISSUE 4 FIX: the previous implementation sorted ascending
            # on (mean, session_id, asset) and took sorted[-1] for
            # max_block. On an exact tie at the maximum mean, that
            # selects the lexicographically LARGEST tied block (the
            # last element of an ascending sort), which directly
            # violates tie_break=ascending_lexical(session_id,asset).
            # Both min and max must independently pick the extreme
            # value first, then break ties by ascending lexical order.
            means_present = [m for m in n_gt_0 if m.mean_signed_bps is not None]
            if means_present:
                min_val = min(m.mean_signed_bps for m in means_present)
                max_val = max(m.mean_signed_bps for m in means_present)
                min_tied = sorted(
                    (m for m in means_present if m.mean_signed_bps == min_val),
                    key=lambda m: (m.session_id, m.asset),
                )
                max_tied = sorted(
                    (m for m in means_present if m.mean_signed_bps == max_val),
                    key=lambda m: (m.session_id, m.asset),
                )
                min_m = min_tied[0]
                max_m = max_tied[0]
                min_block = (min_m.session_id, min_m.asset)
                max_block = (max_m.session_id, max_m.asset)
                min_block_mean = min_val
                max_block_mean = max_val
            else:
                min_block = None
                max_block = None
                min_block_mean = None
                max_block_mean = None

        results.append(AggregateMetrics(
            feature=feat,
            horizon_ms=H,
            q=q,
            total_blocks=total_blocks,
            aggregate_N=aggregate_N,
            n_gt_0_blocks=n_gt_0_count,
            feature_mean=feature_mean,
            avg_hit_rate=avg_hit_rate,
            positive_blocks_count=positive_blocks_count,
            positive_share=positive_share,
            min_block=min_block,
            max_block=max_block,
            min_block_mean=min_block_mean,
            max_block_mean=max_block_mean,
        ))

    return results


# ---------------------------------------------------------------------------
# ISSUE 3 FIX: dispersion sub-state aggregation
# ---------------------------------------------------------------------------
#
# Spec: dispersion_aggregation=identical_rules_as_main_metrics_applied_
#       independently_per_state
#
# Each dispersion state (low/mid/high) of fair_gap_reversion is
# aggregated with EXACTLY the same rules as aggregate_blocks() above,
# computed independently per state — states are never pooled.

DISPERSION_STATES: tuple[str, ...] = ("low", "mid", "high")


@dataclass
class DispersionAggregateMetrics:
    """fair_gap_reversion dispersion-state aggregate.

    One instance per (horizon_ms, q, state). Fields mirror
    AggregateMetrics exactly, applied independently per state.
    """
    feature: str          # always "fair_gap_reversion"
    horizon_ms: int
    q: float
    state: str             # "low" | "mid" | "high"
    total_blocks: int
    aggregate_N: int
    n_gt_0_blocks: int
    feature_mean: float | None
    avg_hit_rate: float | None
    positive_blocks_count: int
    positive_share: float | None
    min_block: tuple[str, str] | None
    max_block: tuple[str, str] | None
    min_block_mean: float | None
    max_block_mean: float | None


def aggregate_dispersion_states(
    summaries: Sequence[BlockSummary],
) -> list[DispersionAggregateMetrics]:
    """Aggregate fair_gap_reversion dispersion sub-metrics independently
    per state (low/mid/high).

    A block counts toward ``total_blocks`` for a given (horizon_ms, q,
    state) whenever the block is structurally VALID and reached
    PhaseB for fair_gap_reversion at that (horizon_ms, q) — identical
    population to the base fair_gap_reversion AggregateMetrics
    total_blocks (spec: a block missing only one feature/sub-state is
    still VALID and still counted). If the block's dispersion_states
    list has no entry for a state (because the base block's N was 0 or
    the dispersion tertile boundaries were undefined), that state is
    treated as N=0/undefined for that block — it is NOT excluded from
    total_blocks, exactly mirroring how a missing feature column still
    counts a block as VALID with N=0 for that feature.

    States are aggregated INDEPENDENTLY: n_gt_0_blocks, feature_mean,
    avg_hit_rate, positive_blocks_count, positive_share, min_block and
    max_block are all computed separately per state — never pooled.
    """
    from collections import defaultdict

    # key: (horizon_ms, q) -> list of (session_id, asset, BlockMetrics)
    by_hq: dict[tuple, list[tuple[str, str, BlockMetrics]]] = defaultdict(list)

    for s in summaries:
        if not s.valid:
            continue
        for m in s.metrics:
            if m.feature != "fair_gap_reversion":
                continue
            by_hq[(m.horizon_ms, m.q)].append((s.session_id, s.asset, m))

    results: list[DispersionAggregateMetrics] = []

    for (H, q), block_metric_list in by_hq.items():
        for state in DISPERSION_STATES:
            # Per-block (session_id, asset, N, mean_signed_bps, hit_rate)
            # for this state, defaulting to N=0/undefined when the base
            # block never populated dispersion_states.
            entries: list[tuple[str, str, int, float | None, float | None]] = []
            for sid, asset, m in block_metric_list:
                state_entry = next(
                    (d for d in m.dispersion_states if d.get("state") == state),
                    None,
                )
                if state_entry is None:
                    entries.append((sid, asset, 0, None, None))
                else:
                    entries.append((
                        sid, asset,
                        int(state_entry["N"]),
                        state_entry["mean_signed_bps"],
                        state_entry["hit_rate"],
                    ))

            total_blocks = len(entries)
            aggregate_N = sum(e[2] for e in entries)
            n_gt_0 = [e for e in entries if e[2] > 0]
            n_gt_0_count = len(n_gt_0)

            if n_gt_0_count == 0:
                feature_mean = None
                avg_hit_rate = None
                positive_blocks_count = 0
                positive_share = None
                min_block = None
                max_block = None
                min_block_mean = None
                max_block_mean = None
            else:
                means_present = [e for e in n_gt_0 if e[3] is not None]
                hit_rates = [e[4] for e in n_gt_0 if e[4] is not None]
                feature_mean = (
                    float(np.mean([e[3] for e in means_present]))
                    if means_present else None
                )
                avg_hit_rate = float(np.mean(hit_rates)) if hit_rates else None
                positive_blocks_count = sum(
                    1 for e in means_present if e[3] > 0
                )
                positive_share = positive_blocks_count / n_gt_0_count

                if means_present:
                    min_val = min(e[3] for e in means_present)
                    max_val = max(e[3] for e in means_present)
                    # extreme-value-first, then ascending lexical
                    # (session_id, asset) tie-break — same rule fix
                    # as aggregate_blocks() (ISSUE 4).
                    min_tied = sorted(
                        (e for e in means_present if e[3] == min_val),
                        key=lambda e: (e[0], e[1]),
                    )
                    max_tied = sorted(
                        (e for e in means_present if e[3] == max_val),
                        key=lambda e: (e[0], e[1]),
                    )
                    min_block = (min_tied[0][0], min_tied[0][1])
                    max_block = (max_tied[0][0], max_tied[0][1])
                    min_block_mean = min_val
                    max_block_mean = max_val
                else:
                    min_block = None
                    max_block = None
                    min_block_mean = None
                    max_block_mean = None

            results.append(DispersionAggregateMetrics(
                feature="fair_gap_reversion",
                horizon_ms=H,
                q=q,
                state=state,
                total_blocks=total_blocks,
                aggregate_N=aggregate_N,
                n_gt_0_blocks=n_gt_0_count,
                feature_mean=feature_mean,
                avg_hit_rate=avg_hit_rate,
                positive_blocks_count=positive_blocks_count,
                positive_share=positive_share,
                min_block=min_block,
                max_block=max_block,
                min_block_mean=min_block_mean,
                max_block_mean=max_block_mean,
            ))

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "HORIZONS_SIMPLE_MS",
    "HORIZONS_COMPOSITE_MS",
    "QUANTILES",
    "GRID_MS",
    "FEATURE_MAP",
    "SIMPLE_FEATURES",
    "COMPOSITE_FEATURES",
    "CORRUPT_GRID",
    "CorruptGridError",
    "BlockMetrics",
    "BlockSummary",
    "AggregateMetrics",
    "DispersionAggregateMetrics",
    "DISPERSION_STATES",
    "build_canonical_grid",
    "reconstruct_block",
    "aggregate_blocks",
    "aggregate_dispersion_states",
    # Lower-level helpers exposed for testing
    "_quality_admissible_mask",
    "_rank_signed_uniform",
    "_quantile_type7",
    "_build_forward_return_array",
    "_greedy_overlap_filter",
    "_compute_simple_feature",
    "_compute_derived_composite",
    "_compute_gap_composite",
    "_compute_leader1000_extofi",
]
