"""RECONSTRUCTION_V1.2 Stage 2 policy-comparison diagnostics.

Stage 2 is diagnostic only.  It executes two greedy non-overlap policies
(P0 and P1) over the *same* policy-independent pre-overlap event set for
every unique (session_id, asset, feature, horizon_ms, q) case in the
72-case fixed matrix, and emits 144 policy-comparison rows
(72 unique cases × 2 policies).

Spec:   V1.2_STAGE2_FINAL
Audit:  STAGE2_DRAFT3_AUDIT_PASS_WITH_MINOR_CHANGES

Safety invariants (NEVER REMOVE):
- No import-time execution.
- No modification of engine.py or any frozen V1.1 / Stage1 artifact.
- RAW access restricted to declared OLD36 sessions via the existing sandbox.
- No DB / RAW writes.
- NEW36 remains rejected by the existing quantitative firewall.
- FrozenAnalysisEngine remains NOT_CONFIGURED / accepts_input=False.
- The pre-overlap event set is constructed ONCE per unique case and
  shared identically between P0 and P1 (I3 / I12 invariant).
- No real Stage 2 diagnostic execution until explicitly authorised.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass
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
    _build_forward_return_array,
    _greedy_overlap_filter,
    _quality_admissible_mask,
    _quantile_type7,
    _rank_signed_uniform,
    build_canonical_grid,
    reconstruct_block,
)
from .harness import _load_grid_for_block, _validate_sync_grid_part_coverage
from .v1_2_diagnostics import (
    _BlockContext,
    _col,
    _gap_components,
    _prepare_block,
    fingerprint_positions,
)


# ---------------------------------------------------------------------------
# Stage 2 constants (frozen)
# ---------------------------------------------------------------------------

STAGE2_VERSION: str = "V1.2_STAGE2_FINAL"

STAGE2_SESSIONS: tuple[str, ...] = (
    "20260905T073818Z_e44d99bd",
    "20260906T221530Z_db18dc51",
)
STAGE2_ASSETS: tuple[str, ...] = ("BTC", "ETH")
STAGE2_Q: float = 0.90
STAGE2_HORIZONS: tuple[int, ...] = (1000, 5000, 30000)

STAGE2_PRIMARY_FAMILIES: tuple[str, ...] = (
    "bitget_ofi",
    "depthL1_extOFI",
    "gap_depth_extOFI",
)
STAGE2_CONTROL_FAMILIES: tuple[str, ...] = (
    "depth_imbalance_l1",
    "gap_depthL1",
    "gap_localOFI",
)
STAGE2_FAMILIES: tuple[str, ...] = STAGE2_PRIMARY_FAMILIES + STAGE2_CONTROL_FAMILIES

STAGE2_UNIQUE_CASES: int = 72    # 2 sessions × 2 assets × 6 families × 3 horizons
STAGE2_POLICY_ROWS: int = 144    # 72 × 2 policies

# Protected artifact names Stage 2 must never overwrite.
_PROTECTED_NAMES: frozenset[str] = frozenset({
    "golden_regression_summary.csv",
    "golden_regression_failures.csv",
    "recovery_report.md",
    "recovery_provenance.json",
    "recovery_rules.json",
    "stage1_candidate_diagnostics.csv",
    "stage1_manifest.json",
})

# I12 policy-independent pre-overlap field names (always identical between P0/P1).
_I12_SHARED_FIELDS: tuple[str, ...] = (
    "pre_overlap_event_n",
    "first_event_grid_pos",
    "last_event_grid_pos",
    "pre_overlap_positions_sha256",
    "gap_steps_min",
    "gap_steps_median",
    "gap_steps_max",
    "conflicting_pair_n",
    "fraction_events_with_neighbor_inside_spacing",
    "maximum_local_cluster_size",
)

# Additional gap_depth_extOFI pre-overlap pipeline fields (I12 extension).
_I12_GAP_EXTRA_FIELDS: tuple[str, ...] = (
    "gap_threshold_crossing_n",
    "confirmation_finite_n",
    "alignment_true_n",
    "aligned_forward_valid_n",
    "confirmation_component_positive_n",
    "confirmation_component_negative_n",
    "direction_positive_n",
    "direction_negative_n",
    "aligned_positive_pair_n",
    "aligned_negative_pair_n",
)

# Fixed column order for Stage 2 CSV output (deterministic, never changes).
_STAGE2_CSV_COLUMNS: tuple[str, ...] = (
    "diagnostic_version",
    "session_id",
    "asset",
    "feature",
    "horizon_ms",
    "q",
    "policy",
    "grid_rows",
    "quality_n",
    "forward_eligible_n",
    "overlap_spacing_steps",
    "pre_overlap_event_n",
    "first_event_grid_pos",
    "last_event_grid_pos",
    "pre_overlap_positions_sha256",
    "gap_steps_min",
    "gap_steps_median",
    "gap_steps_max",
    "conflicting_pair_n",
    "fraction_events_with_neighbor_inside_spacing",
    "maximum_local_cluster_size",
    "accepted_n",
    "overlap_dropped_n",
    "accepted_positions_count",
    "accepted_positions_sha256",
    "accepted_first_grid_pos",
    "accepted_last_grid_pos",
    "mean_signed_bps",
    "hit_rate",
    "mean_abs_move",
    "gap_threshold_crossing_n",
    "confirmation_finite_n",
    "alignment_true_n",
    "aligned_forward_valid_n",
    "confirmation_component_positive_n",
    "confirmation_component_negative_n",
    "direction_positive_n",
    "direction_negative_n",
    "aligned_positive_pair_n",
    "aligned_negative_pair_n",
)

# Default output directory for Stage 2 artifacts (backend root).
_STAGE2_DEFAULT_OUTPUT_DIR: Path = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage2Case:
    """One row in the 144-row Stage 2 matrix."""
    session_id: str
    asset: str
    feature: str
    horizon_ms: int
    q: float
    policy: str  # "P0" or "P1"


@dataclass
class _PreOverlapResult:
    """Policy-independent pre-overlap event data shared between P0 and P1.

    All arrays have the same length (pre_overlap_event_n) and are aligned:
    positions[i], directions[i], returns[i] describe the same event.
    """
    positions: np.ndarray      # ascending int64, pre-overlap event grid positions
    directions: np.ndarray     # float, sign (+1.0 / -1.0) per event
    returns: np.ndarray        # float, forward returns in bps (all finite)
    grid_rows: int             # len(ctx.grid)
    quality_n: int             # count of quality-admissible rows
    forward_eligible_n: int    # quality & isfinite(fwd) count
    overlap_spacing_steps: int # max(10, horizon_ms // GRID_MS)
    gap_extra: dict | None     # gap_depth_extOFI additional pre-overlap diagnostics
    threshold: float | None    # quantile threshold used for pre-overlap event gate


# ---------------------------------------------------------------------------
# Matrix expansion
# ---------------------------------------------------------------------------

def expand_stage2_matrix() -> list[Stage2Case]:
    """Expand the 144-row Stage 2 matrix in deterministic order.

    Order: session → asset → family → horizon → policy (P0 before P1).
    Each unique (session, asset, family, horizon) generates exactly two
    adjacent rows: one P0 and one P1.
    """
    cases: list[Stage2Case] = []
    for sid in STAGE2_SESSIONS:
        for asset in STAGE2_ASSETS:
            for feature in STAGE2_FAMILIES:
                for h in STAGE2_HORIZONS:
                    for policy in ("P0", "P1"):
                        cases.append(Stage2Case(sid, asset, feature, h, STAGE2_Q, policy))
    if len(cases) != STAGE2_POLICY_ROWS:
        raise AssertionError(
            f"Stage2 matrix expected {STAGE2_POLICY_ROWS} rows, got {len(cases)}"
        )
    return cases


# ---------------------------------------------------------------------------
# P1 greedy latest-first filter
# ---------------------------------------------------------------------------

def _greedy_latest_first_filter(
    positions: np.ndarray,
    spacing: int,
) -> np.ndarray:
    """Apply greedy latest-first non-overlap filter (P1).

    Spec:
        Process positions descending.
        Accept first processed event unconditionally.
        Accept subsequent event iff:  last_accepted_pos - pos >= spacing_steps
        After selection, accepted positions are sorted ascending for canonical
        reporting, metric computation, and fingerprinting.

    Input:  sorted ascending int64 array of pre-overlap event grid positions.
    Output: boolean mask (same length as input, aligned to ascending order).
            True  = accepted by P1.
    """
    n = len(positions)
    if n == 0:
        return np.zeros(0, dtype=bool)

    accepted = np.zeros(n, dtype=bool)
    # Sentinel: first processed position (largest) is always accepted.
    # positions are bounded grid indices; adding spacing+1 is safe for int64.
    last_pos: int = int(positions[-1]) + int(spacing) + 1

    for i in range(n - 1, -1, -1):   # descending index → descending position
        pos = int(positions[i])
        if last_pos - pos >= spacing:
            accepted[i] = True
            last_pos = pos

    return accepted


# ---------------------------------------------------------------------------
# Position fingerprint (exact Stage 1 convention, duplicated locally)
# ---------------------------------------------------------------------------

def _sha256_positions(positions: np.ndarray) -> str:
    """SHA256 of positions encoded as ascending signed little-endian int64 bytes.

    Convention (exact Stage 1):
        - Validate positions are signed int64.
        - Sort ascending.
        - Encode as raw concatenated 8-byte little-endian signed int64.
        - No delimiter.
        - SHA256 over the byte string.
        - Return lowercase hex digest.
        - Empty set → SHA256 over zero-length byte string.
    """
    arr = np.asarray(positions, dtype=np.int64)
    if len(arr) > 0:
        arr = np.sort(arr)
    le = arr.astype("<i8", copy=False)
    return hashlib.sha256(le.tobytes(order="C")).hexdigest()


# ---------------------------------------------------------------------------
# Pre-overlap diagnostics (policy-independent)
# ---------------------------------------------------------------------------

def compute_pre_overlap_diagnostics(
    positions: np.ndarray,
    spacing: int,
) -> dict:
    """Compute the 10 policy-independent pre-overlap diagnostic fields.

    Parameters
    ----------
    positions : np.ndarray
        Ascending int64 array of pre-overlap event grid positions.
    spacing : int
        overlap_spacing_steps = max(10, horizon_ms // GRID_MS).

    Returns
    -------
    dict with keys:
        pre_overlap_event_n, first_event_grid_pos, last_event_grid_pos,
        pre_overlap_positions_sha256, gap_steps_min, gap_steps_median,
        gap_steps_max, conflicting_pair_n,
        fraction_events_with_neighbor_inside_spacing,
        maximum_local_cluster_size.
    """
    positions = np.asarray(positions, dtype=np.int64)
    n = int(len(positions))
    pre_sha = _sha256_positions(positions)

    # N=0 degenerate case
    if n == 0:
        return {
            "pre_overlap_event_n": 0,
            "first_event_grid_pos": None,
            "last_event_grid_pos": None,
            "pre_overlap_positions_sha256": pre_sha,
            "gap_steps_min": None,
            "gap_steps_median": None,
            "gap_steps_max": None,
            "conflicting_pair_n": 0,
            "fraction_events_with_neighbor_inside_spacing": 0.0,
            "maximum_local_cluster_size": 0,
        }

    # N=1 degenerate case
    if n == 1:
        return {
            "pre_overlap_event_n": 1,
            "first_event_grid_pos": int(positions[0]),
            "last_event_grid_pos": int(positions[0]),
            "pre_overlap_positions_sha256": pre_sha,
            "gap_steps_min": None,
            "gap_steps_median": None,
            "gap_steps_max": None,
            "conflicting_pair_n": 0,
            "fraction_events_with_neighbor_inside_spacing": 0.0,
            "maximum_local_cluster_size": 1,
        }

    # N >= 2: gap statistics over adjacent (consecutive) position differences.
    gaps = np.diff(positions)   # length n-1, all >= 0 since positions ascending
    gap_min = int(np.min(gaps))
    gap_max = int(np.max(gaps))
    sorted_gaps = np.sort(gaps)
    m = len(sorted_gaps)   # = n - 1
    if m % 2 == 1:
        gap_median = float(sorted_gaps[m // 2])
    else:
        gap_median = float(
            (int(sorted_gaps[m // 2 - 1]) + int(sorted_gaps[m // 2])) / 2.0
        )

    # Unordered conflicting pairs: abs(pos_i - pos_j) < spacing.
    # Positions are sorted ascending, so pos_j - pos_i >= 0 for j > i.
    # Use O(n) sliding window: for each i, count j in (i+1 .. right]
    # where positions[right] - positions[i] < spacing.
    conflicting_pair_n = 0
    right = 0
    for i in range(n):
        if right < i:
            right = i
        while right + 1 < n and positions[right + 1] - positions[i] < spacing:
            right += 1
        # Pairs (i, j) for j = i+1, ..., right
        conflicting_pair_n += max(0, right - i)

    # Events having at least one conflicting neighbour.
    # For sorted positions, event i conflicts with a neighbour iff its
    # immediately adjacent element is within distance < spacing.
    # Proof: if pos[i+2]-pos[i] < spacing then pos[i+1]-pos[i] < spacing
    # (since positions sorted; cannot have pos[i+2] < pos[i+1]).
    has_conflict = np.zeros(n, dtype=bool)
    for i in range(n - 1):
        gap_here = int(positions[i + 1]) - int(positions[i])
        if gap_here < spacing:
            has_conflict[i] = True
            has_conflict[i + 1] = True

    fraction = float(np.sum(has_conflict)) / float(n)

    # Maximum connected-component size in the undirected conflict graph.
    # For sorted positions, connected components are exactly the maximal
    # contiguous runs where every consecutive gap is < spacing.
    max_cluster = 1
    current_cluster = 1
    for i in range(1, n):
        if int(positions[i]) - int(positions[i - 1]) < spacing:
            current_cluster += 1
            if current_cluster > max_cluster:
                max_cluster = current_cluster
        else:
            current_cluster = 1

    return {
        "pre_overlap_event_n": n,
        "first_event_grid_pos": int(positions[0]),
        "last_event_grid_pos": int(positions[-1]),
        "pre_overlap_positions_sha256": pre_sha,
        "gap_steps_min": gap_min,
        "gap_steps_median": gap_median,
        "gap_steps_max": gap_max,
        "conflicting_pair_n": int(conflicting_pair_n),
        "fraction_events_with_neighbor_inside_spacing": fraction,
        "maximum_local_cluster_size": int(max_cluster),
    }


# ---------------------------------------------------------------------------
# Pre-overlap extraction (policy-independent)
# ---------------------------------------------------------------------------

def _extract_pre_overlap(
    feature: str,
    ctx: _BlockContext,
    horizon_ms: int,
    q: float,
) -> _PreOverlapResult:
    """Compute the policy-independent pre-overlap event set for one unique case.

    This function replicates the exact pre-overlap pipeline (feature
    calculation, normalisation, threshold, gate, confirmation, alignment,
    forward-valid filter) from the frozen V1.1 engine semantics WITHOUT
    executing the overlap filter.  Both P0 and P1 consume the returned
    result identically, guaranteeing I3 and I12.

    Returns a _PreOverlapResult whose positions, directions, and returns
    arrays are co-indexed (same event at same array index).
    """
    k = horizon_ms // GRID_MS
    spacing = max(10, k)
    fwd = _build_forward_return_array(ctx.mid, k)

    n_grid = int(len(ctx.gpos))
    grid_rows = int(len(ctx.grid))
    quality_n = int(np.sum(ctx.quality))
    forward_eligible_n = int(np.sum(ctx.quality & np.isfinite(fwd)))

    # ── Simple features: bitget_ofi, depth_imbalance_l1 ─────────────────────
    if feature in ("bitget_ofi", "depth_imbalance_l1"):
        signal = ctx.columns[FEATURE_MAP[feature]]
        simple_domain = ctx.quality & np.isfinite(signal)
        threshold = _quantile_type7(np.abs(signal[simple_domain]), q)
        if threshold is None:
            post = np.zeros(n_grid, dtype=bool)
        else:
            post = (
                simple_domain
                & (signal != 0.0)
                & (np.abs(signal) >= threshold)
                & np.isfinite(fwd)
            )
        direction_array = np.sign(signal)
        gap_extra = None

    # ── Derived feature: depthL1_extOFI ──────────────────────────────────────
    elif feature == "depthL1_extOFI":
        z1 = ctx.z_di_l1
        z2 = ctx.z_ext_ofi
        derived_domain = ctx.quality & np.isfinite(z1) & np.isfinite(z2)
        D = np.where(derived_domain, (z1 + z2) / 2.0, np.nan)
        threshold = _quantile_type7(np.abs(D[derived_domain]), q)
        if threshold is None:
            post = np.zeros(n_grid, dtype=bool)
        else:
            post = (
                derived_domain
                & (D != 0.0)
                & (np.abs(D) >= threshold)
                & np.isfinite(fwd)
            )
        direction_array = np.sign(D)
        gap_extra = None

    # ── Gap features: gap_depth_extOFI, gap_depthL1, gap_localOFI ───────────
    elif feature in ("gap_depth_extOFI", "gap_depthL1", "gap_localOFI"):
        gap = ctx.columns["bitget_gap_to_fair_bps"]
        gap_threshold_domain = ctx.quality & np.isfinite(gap)
        threshold = _quantile_type7(np.abs(gap[gap_threshold_domain]), q)
        conf_fin, alignment, _c1, _c2, _align_col = _gap_components(ctx, feature)
        gap_event_domain = gap_threshold_domain & conf_fin
        if threshold is None:
            pre_alignment = np.zeros(n_grid, dtype=bool)
        else:
            pre_alignment = (
                gap_event_domain
                & (gap != 0.0)
                & (np.abs(gap) >= threshold)
            )
        alignment_true_domain = gap_event_domain & alignment
        post_alignment = pre_alignment & alignment
        post = post_alignment & np.isfinite(fwd)
        # Reversion direction: sign(expected return) = -sign(gap)
        direction_array = -np.sign(gap)

        # gap_depth_extOFI: additional pre-overlap pipeline diagnostics (I12 extension).
        if feature == "gap_depth_extOFI":
            # Recompute D for extra diagnostics (matches _gap_components internals).
            D_ext = np.where(
                np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi),
                (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0,
                np.nan,
            )
            gap_extra = {
                # Pre-alignment threshold crossings within gap_event_domain.
                "gap_threshold_crossing_n": int(np.sum(pre_alignment)),
                # Quality rows with finite confirmation component.
                "confirmation_finite_n": int(np.sum(ctx.quality & conf_fin)),
                # Aligned events within gap_event_domain.
                "alignment_true_n": int(np.sum(alignment_true_domain)),
                # Pre-overlap events (all filters satisfied including forward).
                "aligned_forward_valid_n": int(np.sum(post)),
                # Confirmation component sign decomposition over pre-alignment events.
                "confirmation_component_positive_n": int(
                    np.sum(pre_alignment & (D_ext > 0.0))
                ),
                "confirmation_component_negative_n": int(
                    np.sum(pre_alignment & (D_ext < 0.0))
                ),
                # Direction decomposition over pre-overlap events.
                # Since all post events satisfy alignment (sign(D)==-sign(gap)),
                # gap<0 ↔ D>0 ↔ direction=+1 (buy).
                "direction_positive_n": int(np.sum(post & (gap < 0.0))),
                "direction_negative_n": int(np.sum(post & (gap > 0.0))),
                # Aligned pair decomposition over pre-overlap events.
                "aligned_positive_pair_n": int(
                    np.sum(post & (D_ext > 0.0) & (gap < 0.0))
                ),
                "aligned_negative_pair_n": int(
                    np.sum(post & (D_ext < 0.0) & (gap > 0.0))
                ),
            }
        else:
            gap_extra = None

    else:
        raise ValueError(f"Stage2: unsupported feature {feature!r}")

    # Extract co-indexed arrays for the pre-overlap event set.
    positions = np.asarray(ctx.gpos[post], dtype=np.int64)
    # directions at pre-overlap events (finite; gap/D==0 events excluded by gate)
    directions = np.asarray(direction_array[post], dtype=float)
    # forward returns at pre-overlap events (all finite by construction)
    returns = np.asarray(fwd[post], dtype=float)

    return _PreOverlapResult(
        positions=positions,
        directions=directions,
        returns=returns,
        grid_rows=grid_rows,
        quality_n=quality_n,
        forward_eligible_n=forward_eligible_n,
        overlap_spacing_steps=spacing,
        gap_extra=gap_extra,
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# Policy metrics
# ---------------------------------------------------------------------------

def _compute_policy_metrics(
    pre_result: _PreOverlapResult,
    accepted_bool: np.ndarray,
) -> dict:
    """Compute mean_signed_bps, hit_rate, mean_abs_move from accepted events.

    All three metrics are computed ONLY from the policy's accepted event set
    using the frozen return/direction semantics established by Stage 1.5.
    Rejected events do not contribute.

    mean_abs_move uses the accepted-events domain per Stage 1.5 spec.
    """
    n_accepted = int(np.sum(accepted_bool))
    if n_accepted == 0:
        return {
            "mean_signed_bps": None,
            "hit_rate": None,
            "mean_abs_move": None,
        }
    dirs = pre_result.directions[accepted_bool]   # +1.0 or -1.0
    rets = pre_result.returns[accepted_bool]       # all finite (guaranteed by construction)
    signed = dirs * rets
    return {
        "mean_signed_bps": float(np.mean(signed)),
        "hit_rate": float(np.mean(signed > 0.0)),
        "mean_abs_move": float(np.mean(np.abs(rets))),
    }


# ---------------------------------------------------------------------------
# Invariant assertions
# ---------------------------------------------------------------------------

def _assert_pair_invariants(
    pre_result: _PreOverlapResult,
    p0_mask: np.ndarray,
    p1_mask: np.ndarray,
    spacing: int,
    pre_diag: dict,
) -> None:
    """Assert I1–I9 and I12 for one unique (P0, P1) case pair.

    Any failure raises AssertionError with the invariant label.
    Per spec: ABORT-level — no warning downgrade.
    """
    pre_pos = pre_result.positions
    n_pre = len(pre_pos)
    p0_pos = pre_pos[p0_mask]
    p1_pos = pre_pos[p1_mask]

    # I1: pre-overlap positions must be unique.
    if n_pre > 0 and len(np.unique(pre_pos)) != n_pre:
        raise AssertionError("I1 FAIL: pre-overlap positions are not unique")

    # I2: canonical pre-overlap reporting order must be strictly ascending.
    if n_pre > 1 and not bool(np.all(np.diff(pre_pos) > 0)):
        raise AssertionError("I2 FAIL: pre-overlap positions not strictly ascending")

    # I3: P0/P1 must share the same pre_overlap_event_n, sha256, and
    #     the same underlying frozen in-memory sequence (guaranteed by design:
    #     both receive the same pre_result).
    expected_sha = pre_diag["pre_overlap_positions_sha256"]
    if pre_diag["pre_overlap_event_n"] != n_pre:
        raise AssertionError(
            f"I3 FAIL: pre_diag pre_overlap_event_n {pre_diag['pre_overlap_event_n']} "
            f"!= actual {n_pre}"
        )
    # Both P0 and P1 rows share the same pre_diag, so their sha256 are identical
    # by construction.  Verify the sha256 matches the actual positions array.
    actual_sha = _sha256_positions(pre_pos)
    if actual_sha != expected_sha:
        raise AssertionError(
            f"I3 FAIL: pre_overlap_positions_sha256 mismatch: "
            f"actual={actual_sha!r} stored={expected_sha!r}"
        )

    # I4: accepted positions must be unique (for both policies).
    for label, pos in (("P0", p0_pos), ("P1", p1_pos)):
        if len(pos) > 0 and len(np.unique(pos)) != len(pos):
            raise AssertionError(f"I4 FAIL: {label} accepted positions are not unique")

    # I5: canonical accepted positions must be strictly ascending.
    for label, pos in (("P0", p0_pos), ("P1", p1_pos)):
        if len(pos) > 1 and not bool(np.all(np.diff(pos) > 0)):
            raise AssertionError(
                f"I5 FAIL: {label} accepted positions not strictly ascending"
            )

    # I6: pre_overlap_event_n == accepted_n + overlap_dropped_n.
    for label, mask in (("P0", p0_mask), ("P1", p1_mask)):
        n_acc = int(np.sum(mask))
        n_drop = n_pre - n_acc
        if n_drop < 0 or n_acc + n_drop != n_pre:
            raise AssertionError(
                f"I6 FAIL: {label} accounting invariant: "
                f"pre={n_pre} accepted={n_acc} dropped={n_drop}"
            )

    # I7: accepted positions must be a subset of pre-overlap positions.
    pre_set = set(int(p) for p in pre_pos)
    for label, pos in (("P0", p0_pos), ("P1", p1_pos)):
        for p in pos:
            if int(p) not in pre_set:
                raise AssertionError(
                    f"I7 FAIL: {label} accepted position {p!r} not in pre-overlap set"
                )

    # I8: every unordered accepted pair must satisfy abs(pos_i-pos_j) >= spacing.
    # For strictly ascending accepted positions, checking consecutive pairs is
    # sufficient (if min consecutive gap >= spacing then all gaps >= spacing).
    for label, pos in (("P0", p0_pos), ("P1", p1_pos)):
        if len(pos) > 1:
            min_gap = int(np.min(np.diff(pos)))
            if min_gap < spacing:
                raise AssertionError(
                    f"I8 FAIL: {label} accepted positions have minimum gap "
                    f"{min_gap} < spacing {spacing}"
                )

    # I9: accepted_n must equal the count encoded in the fingerprint.
    # Verified inline during row construction; assert structure here.
    for label, mask in (("P0", p0_mask), ("P1", p1_mask)):
        n_acc = int(np.sum(mask))
        acc_pos = pre_pos[mask]
        if n_acc != len(acc_pos):
            raise AssertionError(
                f"I9 FAIL: {label} accepted_n {n_acc} != accepted_positions length"
            )

    # I12: all policy-independent pre-overlap diagnostic values must be
    #      identical between P0 and P1 (guaranteed by design: both rows share
    #      the same pre_diag dict).  Assert at least that the shared dict
    #      contains all required fields with consistent types.
    for field in _I12_SHARED_FIELDS:
        if field not in pre_diag:
            raise AssertionError(f"I12 FAIL: required pre-overlap field '{field}' missing")


def _assert_i10_i11(rows: list[dict]) -> None:
    """Assert I10 (exactly one P0 and one P1 per unique case) and I11 (144 rows)."""
    # I11
    if len(rows) != STAGE2_POLICY_ROWS:
        raise AssertionError(
            f"I11 FAIL: expected {STAGE2_POLICY_ROWS} rows, got {len(rows)}"
        )

    # I10
    from collections import Counter
    counter: Counter = Counter()
    for row in rows:
        key = (
            row["session_id"], row["asset"], row["feature"],
            row["horizon_ms"], row["q"],
        )
        counter[(key, row["policy"])] += 1

    for (key, policy), count in counter.items():
        if count != 1:
            raise AssertionError(
                f"I10 FAIL: case {key!r} policy {policy!r} appears {count} times"
            )

    unique_cases = {k for (k, _) in counter}
    if len(unique_cases) != STAGE2_UNIQUE_CASES:
        raise AssertionError(
            f"I10 FAIL: expected {STAGE2_UNIQUE_CASES} unique cases, "
            f"got {len(unique_cases)}"
        )

    # Verify both P0 and P1 exist for each unique case.
    for key in unique_cases:
        for policy in ("P0", "P1"):
            if counter[(key, policy)] != 1:
                raise AssertionError(
                    f"I10 FAIL: case {key!r} missing policy {policy!r}"
                )


def _assert_i12_actual_field_comparison(row_p0: dict, row_p1: dict) -> None:
    """Assert I12 by comparing actual emitted P0/P1 field values (R2).

    Compares the 10 policy-independent pre-overlap shared fields between the
    two emitted rows.  For gap_depth_extOFI cases, also compares the 10
    additional pre-overlap pipeline diagnostic fields.

    Any mismatch raises AssertionError with the field name and both values,
    per the R2 audit requirement.

    Raises
    ------
    AssertionError("I12 FAIL: field '...' P0=... P1=...")
        If any compared field value differs between P0 and P1.
    """
    # Compare the 10 shared pre-overlap fields.
    for f in _I12_SHARED_FIELDS:
        if row_p0[f] != row_p1[f]:
            raise AssertionError(
                f"I12 FAIL: field '{f}' P0={row_p0[f]!r} P1={row_p1[f]!r}"
            )

    # For gap_depth_extOFI: also compare the 10 additional pipeline fields.
    if row_p0["feature"] == "gap_depth_extOFI":
        for f in _I12_GAP_EXTRA_FIELDS:
            if row_p0[f] != row_p1[f]:
                raise AssertionError(
                    f"I12 FAIL: field '{f}' P0={row_p0[f]!r} P1={row_p1[f]!r}"
                )


def _assert_r3_p0_drift_guard(
    ctx: "_BlockContext",
    feature: str,
    horizon_ms: int,
    p0_row: dict,
    threshold: "float | None",
) -> None:
    """Assert that P0 computed values match the frozen V1.1 engine metrics (R3).

    Compares P0 ``accepted_n``, ``threshold``, ``mean_signed_bps``, and
    ``hit_rate`` against ``ctx.metrics[(feature, horizon_ms, STAGE2_Q)]``.
    Skips silently when the metrics key is absent (synthetic / test contexts
    with empty metrics dicts).

    EXCLUDED from comparison: ``mean_abs_move`` (per R3 spec).

    Field-level comparison rules
    ----------------------------
    N               : exact integer equality.
    threshold       : exact float equality (or None == None);
                      nonfinite float → ABORT.
    mean_signed_bps : both must be finite; absolute tolerance ≤ 1e-12.
    hit_rate        : both must be finite; absolute tolerance ≤ 1e-12.

    Raises
    ------
    AssertionError("STAGE2_PATCH1_FAIL_R3_SCHEMA")
        If the metrics object is missing any required attribute.
    AssertionError("R3 DRIFT: ...")
        If any compared quantity deviates beyond its allowed tolerance, or
        if an unexpected NaN / nonfinite value is encountered in a required
        comparison operand.
    """
    key = (feature, horizon_ms, STAGE2_Q)
    if not ctx.metrics or key not in ctx.metrics:
        return  # No frozen metrics to compare — synthetic / test contexts.

    m = ctx.metrics[key]

    # Schema guard: verify all required attributes are present.
    _required_r3_attrs = ("N", "threshold", "mean_signed_bps", "hit_rate")
    if not all(hasattr(m, attr) for attr in _required_r3_attrs):
        raise AssertionError("STAGE2_PATCH1_FAIL_R3_SCHEMA")

    # ── N: exact integer equality ─────────────────────────────────────────────
    expected_N: int = m.N
    actual_N: int = p0_row["accepted_n"]
    if actual_N != expected_N:
        raise AssertionError(
            f"R3 DRIFT: N expected={expected_N} got={actual_N}"
        )

    # ── threshold: exact equality (float or None); nonfinite float → ABORT ───
    expected_thr = m.threshold
    actual_thr = threshold
    if expected_thr is None and actual_thr is None:
        pass  # Both None — valid (domain has < 2 finite values).
    elif expected_thr is None or actual_thr is None:
        raise AssertionError(
            f"R3 DRIFT: threshold expected={expected_thr!r} got={actual_thr!r}"
        )
    else:
        if not math.isfinite(expected_thr):
            raise AssertionError(
                f"R3 DRIFT: threshold expected value is non-finite: {expected_thr!r}"
            )
        if not math.isfinite(actual_thr):
            raise AssertionError(
                f"R3 DRIFT: threshold actual value is non-finite: {actual_thr!r}"
            )
        if expected_thr != actual_thr:
            raise AssertionError(
                f"R3 DRIFT: threshold expected={expected_thr!r} got={actual_thr!r}"
            )

    # ── mean_signed_bps: finite required; absolute tolerance ≤ 1e-12 ─────────
    expected_msb = m.mean_signed_bps
    actual_msb = p0_row["mean_signed_bps"]
    if expected_msb is None and actual_msb is None:
        pass
    elif expected_msb is None or actual_msb is None:
        raise AssertionError(
            f"R3 DRIFT: mean_signed_bps expected={expected_msb!r} got={actual_msb!r}"
        )
    else:
        if not math.isfinite(expected_msb):
            raise AssertionError(
                f"R3 DRIFT: mean_signed_bps expected value is non-finite: {expected_msb!r}"
            )
        if not math.isfinite(actual_msb):
            raise AssertionError(
                f"R3 DRIFT: mean_signed_bps actual value is non-finite: {actual_msb!r}"
            )
        if abs(actual_msb - expected_msb) > 1e-12:
            raise AssertionError(
                f"R3 DRIFT: mean_signed_bps expected={expected_msb!r} got={actual_msb!r}"
            )

    # ── hit_rate: finite required; absolute tolerance ≤ 1e-12 ────────────────
    expected_hr = m.hit_rate
    actual_hr = p0_row["hit_rate"]
    if expected_hr is None and actual_hr is None:
        pass
    elif expected_hr is None or actual_hr is None:
        raise AssertionError(
            f"R3 DRIFT: hit_rate expected={expected_hr!r} got={actual_hr!r}"
        )
    else:
        if not math.isfinite(expected_hr):
            raise AssertionError(
                f"R3 DRIFT: hit_rate expected value is non-finite: {expected_hr!r}"
            )
        if not math.isfinite(actual_hr):
            raise AssertionError(
                f"R3 DRIFT: hit_rate actual value is non-finite: {actual_hr!r}"
            )
        if abs(actual_hr - expected_hr) > 1e-12:
            raise AssertionError(
                f"R3 DRIFT: hit_rate expected={expected_hr!r} got={actual_hr!r}"
            )


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def _build_row(
    case: Stage2Case,
    pre_result: _PreOverlapResult,
    pre_diag: dict,
    accepted_bool: np.ndarray,
) -> dict:
    """Assemble one Stage 2 output row for a given policy."""
    n_pre = len(pre_result.positions)
    n_accepted = int(np.sum(accepted_bool))
    n_dropped = n_pre - n_accepted
    accepted_positions = pre_result.positions[accepted_bool]

    # Accepted-event fingerprint (exact Stage 1 convention via imported helper).
    fp = fingerprint_positions(accepted_positions)

    # I9: accepted_n must equal fingerprint count.
    if n_accepted != fp["accepted_positions_count"]:
        raise AssertionError(
            f"I9 FAIL: {case.policy} accepted_n {n_accepted} "
            f"!= fingerprint count {fp['accepted_positions_count']}"
        )

    metrics = _compute_policy_metrics(pre_result, accepted_bool)

    row: dict = {
        "diagnostic_version": STAGE2_VERSION,
        "session_id": case.session_id,
        "asset": case.asset,
        "feature": case.feature,
        "horizon_ms": case.horizon_ms,
        "q": case.q,
        "policy": case.policy,
        "grid_rows": pre_result.grid_rows,
        "quality_n": pre_result.quality_n,
        "forward_eligible_n": pre_result.forward_eligible_n,
        "overlap_spacing_steps": pre_result.overlap_spacing_steps,
        # --- Policy-independent pre-overlap diagnostics (I12) ---
        "pre_overlap_event_n": pre_diag["pre_overlap_event_n"],
        "first_event_grid_pos": pre_diag["first_event_grid_pos"],
        "last_event_grid_pos": pre_diag["last_event_grid_pos"],
        "pre_overlap_positions_sha256": pre_diag["pre_overlap_positions_sha256"],
        "gap_steps_min": pre_diag["gap_steps_min"],
        "gap_steps_median": pre_diag["gap_steps_median"],
        "gap_steps_max": pre_diag["gap_steps_max"],
        "conflicting_pair_n": pre_diag["conflicting_pair_n"],
        "fraction_events_with_neighbor_inside_spacing": pre_diag[
            "fraction_events_with_neighbor_inside_spacing"
        ],
        "maximum_local_cluster_size": pre_diag["maximum_local_cluster_size"],
        # --- Policy-specific results ---
        "accepted_n": n_accepted,
        "overlap_dropped_n": n_dropped,
        "accepted_positions_count": fp["accepted_positions_count"],
        "accepted_positions_sha256": fp["accepted_positions_sha256"],
        "accepted_first_grid_pos": fp["accepted_first_grid_pos"],
        "accepted_last_grid_pos": fp["accepted_last_grid_pos"],
        "mean_signed_bps": metrics["mean_signed_bps"],
        "hit_rate": metrics["hit_rate"],
        "mean_abs_move": metrics["mean_abs_move"],
        # --- gap_depth_extOFI pre-overlap pipeline diagnostics (None for others) ---
        "gap_threshold_crossing_n": None,
        "confirmation_finite_n": None,
        "alignment_true_n": None,
        "aligned_forward_valid_n": None,
        "confirmation_component_positive_n": None,
        "confirmation_component_negative_n": None,
        "direction_positive_n": None,
        "direction_negative_n": None,
        "aligned_positive_pair_n": None,
        "aligned_negative_pair_n": None,
    }

    if pre_result.gap_extra is not None:
        row.update(pre_result.gap_extra)

    return row


# ---------------------------------------------------------------------------
# Core row generation
# ---------------------------------------------------------------------------

def _process_unique_case(
    key: tuple,
    ctx: _BlockContext,
    matrix_group: dict[str, int],
    rows_out: list,
) -> None:
    """Process one unique (session, asset, feature, horizon, q) case.

    Constructs the pre-overlap result ONCE, applies both P0 and P1, asserts
    I1–I9 / I12, and writes exactly two rows (P0 then P1) at the positions
    specified by matrix_group.
    """
    sid, asset, feature, horizon_ms, q = key
    spacing = max(10, horizon_ms // GRID_MS)

    # Step 1: construct pre-overlap result (policy-independent).
    pre_result = _extract_pre_overlap(feature, ctx, horizon_ms, q)

    # Step 2: compute policy-independent diagnostics.
    pre_diag = compute_pre_overlap_diagnostics(pre_result.positions, spacing)

    # Step 3: apply P0 (greedy earliest-first, reusing frozen engine helper).
    p0_mask = (
        _greedy_overlap_filter(pre_result.positions, spacing)
        if len(pre_result.positions) > 0
        else np.zeros(0, dtype=bool)
    )

    # Step 4: apply P1 (greedy latest-first, new Stage 2 function).
    p1_mask = _greedy_latest_first_filter(pre_result.positions, spacing)

    # Step 5: assert I1–I9, I12 for this pair (ABORT on any failure).
    _assert_pair_invariants(pre_result, p0_mask, p1_mask, spacing, pre_diag)

    # Step 6: build P0 row.
    case_p0 = Stage2Case(sid, asset, feature, horizon_ms, q, "P0")
    row_p0 = _build_row(case_p0, pre_result, pre_diag, p0_mask)

    # Step 7: R3 — Frozen Engine P0 Drift Guard.
    # Compares P0 accepted_n / threshold / mean_signed_bps / hit_rate against
    # the frozen V1.1 engine metrics stored in ctx.metrics (if populated).
    _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, row_p0, pre_result.threshold)

    # Step 8: build P1 row.
    case_p1 = Stage2Case(sid, asset, feature, horizon_ms, q, "P1")
    row_p1 = _build_row(case_p1, pre_result, pre_diag, p1_mask)

    # Step 9: R2 — I12 actual emitted-field comparison (P0 vs P1).
    # Compares actual values in the emitted rows for the 10 shared
    # pre-overlap fields (and 10 gap extras for gap_depth_extOFI).
    _assert_i12_actual_field_comparison(row_p0, row_p1)

    # Step 10: store rows at their fixed matrix positions.
    rows_out[matrix_group["P0"]] = row_p0
    rows_out[matrix_group["P1"]] = row_p1


def generate_stage2_rows(
    contexts: dict[tuple[str, str], _BlockContext] | None = None,
) -> list[dict]:
    """Generate exactly 144 Stage 2 policy-comparison rows.  Explicit call only.

    Parameters
    ----------
    contexts : dict or None
        Mapping (session_id, asset) → _BlockContext.
        - If not None: uses the supplied (pre-built / synthetic) contexts.
          This path is used by focused tests and never opens RAW data.
        - If None: real execution path — validates safety invariants, loads
          one OLD36 block at a time through the existing sandbox, and calls
          _validate_stage2_safety() before any data access.
          THIS PATH MUST NOT BE INVOKED UNTIL EXPLICITLY AUTHORISED.

    Returns
    -------
    list of 144 dicts, one per (unique_case, policy) pair.
    """
    matrix = expand_stage2_matrix()
    rows_by_index: list[dict | None] = [None] * len(matrix)

    # Build lookup: unique case key → {policy → matrix index}
    unique_key_order: list[tuple] = []
    case_groups: dict[tuple, dict[str, int]] = {}
    for i, case in enumerate(matrix):
        key = (case.session_id, case.asset, case.feature, case.horizon_ms, case.q)
        if key not in case_groups:
            case_groups[key] = {}
            unique_key_order.append(key)
        case_groups[key][case.policy] = i

    if contexts is not None:
        # ── Synthetic / test path ───────────────────────────────────────────
        for key in unique_key_order:
            sid, asset = key[0], key[1]
            ctx = contexts[(sid, asset)]
            _process_unique_case(key, ctx, case_groups[key], rows_by_index)

    else:
        # ── Real execution path (NOT authorised until explicitly permitted) ──
        _validate_stage2_safety()
        checked_sessions: set[str] = set()
        for sid in STAGE2_SESSIONS:
            if sid not in checked_sessions:
                coverage_ok, reason = _validate_sync_grid_part_coverage(sid)
                if not coverage_ok:
                    raise RuntimeError(
                        f"Stage2 part coverage failure for {sid}: {reason}"
                    )
                checked_sessions.add(sid)
            for asset in STAGE2_ASSETS:
                df = _load_grid_for_block(sid, asset)
                ctx = _prepare_block(sid, asset, df)
                # Process all unique cases for this (session, asset) block.
                for key in unique_key_order:
                    if key[0] == sid and key[1] == asset:
                        _process_unique_case(
                            key, ctx, case_groups[key], rows_by_index
                        )
                del ctx
                del df

    if any(r is None for r in rows_by_index):
        raise AssertionError("Stage2 failed to populate every fixed-matrix row")

    rows = [r for r in rows_by_index if r is not None]

    # Assert I10 and I11 over the full output.
    _assert_i10_i11(rows)

    return rows


# ---------------------------------------------------------------------------
# Runtime safety validation (real execution path only)
# ---------------------------------------------------------------------------

def _validate_stage2_safety() -> None:
    """Validate all safety invariants before any real OLD36 data access.

    Called exclusively from the real-execution path of generate_stage2_rows.
    Structural NEW36 firewall self-check does NOT open NEW36 data.
    """
    old = tuple(OLD36_REFERENCE_SESSIONS)
    if any(sid not in old for sid in STAGE2_SESSIONS):
        raise RuntimeError(
            "Stage2 declared session not in OLD36_REFERENCE_SESSIONS"
        )
    for sid in STAGE2_SESSIONS:
        assert_recovery_allowed(sid)

    # Structural firewall self-check: verify NEW36 is still being rejected.
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
            f"FrozenAnalysisEngine unsafe state: "
            f"status={status.status!r}, accepts_input={status.accepts_input!r}"
        )


# ---------------------------------------------------------------------------
# R1: Final artifact writer and orchestrator (deterministic manifest)
# ---------------------------------------------------------------------------

def write_stage2_artifacts(
    rows: list[dict],
    runtime_source_commit: str,
    output_dir: "Path | None" = None,
) -> "tuple[Path, Path]":
    """Write Stage 2 policy diagnostics CSV and deterministic manifest JSON.

    Parameters
    ----------
    rows : list[dict]
        The 144 Stage 2 policy-comparison rows from generate_stage2_rows().
    runtime_source_commit : str
        The git commit hash of the repository at run time.
    output_dir : Path or None
        Directory to write artifacts into.  Defaults to the backend root
        (parent of the recovery package).  Tests should supply tmp_path.

    Returns
    -------
    (csv_path, manifest_path)

    Raises
    ------
    FileExistsError
        If either output file already exists (never overwrites).

    Notes
    -----
    No timestamp is included in the manifest — output is fully deterministic
    given identical inputs.  Manifest JSON is serialised with sort_keys=True.
    """
    out = Path(output_dir) if output_dir is not None else _STAGE2_DEFAULT_OUTPUT_DIR

    csv_path = out / "stage2_policy_diagnostics.csv"
    manifest_path = out / "stage2_manifest.json"

    # Strict existence guard — raise before any computation.
    if csv_path.exists():
        raise FileExistsError(
            f"Stage2 CSV already exists and will not be overwritten: {csv_path}"
        )
    if manifest_path.exists():
        raise FileExistsError(
            f"Stage2 manifest already exists and will not be overwritten: {manifest_path}"
        )

    # ── Serialize CSV (fixed column order, UTF-8; None → empty field) ────────
    df = pd.DataFrame(rows, columns=list(_STAGE2_CSV_COLUMNS))
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    csv_bytes = buf.getvalue().encode("utf-8")
    csv_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    csv_byte_size = len(csv_bytes)

    # ── Derive summary counts ─────────────────────────────────────────────────
    actual_row_count = len(rows)
    actual_unique_case_count = len({
        (r["session_id"], r["asset"], r["feature"], r["horizon_ms"], r["q"])
        for r in rows
    })

    # ── Build deterministic manifest (no timestamp; sorted keys on serialize) ─
    manifest: dict = {
        "GRID_MS": GRID_MS,
        "P0_definition": (
            "greedy_earliest_first: accept next candidate event iff "
            "(pos - last_accepted_pos) >= spacing_steps; "
            "V1.1 frozen engine semantics (_greedy_overlap_filter)"
        ),
        "P1_definition": (
            "greedy_latest_first: process events descending, accept iff "
            "(last_accepted_pos - pos) >= spacing_steps, "
            "output sorted ascending (Stage2 new policy, _greedy_latest_first_filter)"
        ),
        "actual_row_count": actual_row_count,
        "actual_unique_case_count": actual_unique_case_count,
        "assets": list(STAGE2_ASSETS),
        "candidate_generation_frozen_before_golden_comparison": True,
        "csv_byte_size": csv_byte_size,
        "csv_sha256": csv_sha256,
        "diagnostic_version": STAGE2_VERSION,
        "engine_py_modified": False,
        "expected_row_count": STAGE2_POLICY_ROWS,
        "expected_unique_case_count": STAGE2_UNIQUE_CASES,
        "feature_families": {
            "all_six": list(STAGE2_FAMILIES),
            "control": list(STAGE2_CONTROL_FAMILIES),
            "primary": list(STAGE2_PRIMARY_FAMILIES),
        },
        "fingerprint_encoding_definition": (
            "SHA256 of accepted positions sorted ascending, encoded as raw "
            "concatenated 8-byte little-endian signed int64, no delimiter; "
            "empty set = SHA256 of zero-length byte string; lowercase hex digest"
        ),
        "gap_depth_extOFI_diagnostic_counters": {
            "aligned_forward_valid_n": {
                "domain": (
                    "gap_event_domain AND gap!=0 AND |gap|>=gap_threshold "
                    "AND alignment=True AND isfinite(forward_return) "
                    "(final pre-overlap event set)"
                ),
                "scope": "EVENT-SCOPE",
            },
            "aligned_negative_pair_n": {
                "domain": "pre_overlap_events AND D_ext<0 AND gap>0",
                "scope": "EVENT-SCOPE",
            },
            "aligned_positive_pair_n": {
                "domain": "pre_overlap_events AND D_ext>0 AND gap<0",
                "scope": "EVENT-SCOPE",
            },
            "alignment_true_n": {
                "domain": "gap_event_domain AND alignment=True",
                "scope": "EVENT-SCOPE",
            },
            "confirmation_component_negative_n": {
                "domain": "pre_alignment_events AND D_ext<0",
                "scope": "EVENT-SCOPE",
            },
            "confirmation_component_positive_n": {
                "domain": "pre_alignment_events AND D_ext>0",
                "scope": "EVENT-SCOPE",
            },
            "confirmation_finite_n": {
                "domain": "quality_admissible AND isfinite(confirmation_component)",
                "scope": "GRID-SCOPE",
            },
            "direction_negative_n": {
                "domain": "pre_overlap_events AND gap>0 (direction=-1, sell)",
                "scope": "EVENT-SCOPE",
            },
            "direction_positive_n": {
                "domain": "pre_overlap_events AND gap<0 (direction=+1, buy)",
                "scope": "EVENT-SCOPE",
            },
            "gap_threshold_crossing_n": {
                "domain": (
                    "gap_event_domain AND gap!=0 AND |gap|>=gap_threshold "
                    "(pre-alignment threshold crossings within gap_event_domain)"
                ),
                "scope": "EVENT-SCOPE",
            },
        },
        "historical_target_numerics_read_by_generator": False,
        "horizons_ms": list(STAGE2_HORIZONS),
        "k_values": {str(h): h // GRID_MS for h in STAGE2_HORIZONS},
        "mean_abs_move_domain": "accepted_events",
        "new36_opened": False,
        "policy_ids": ["P0", "P1"],
        "q": STAGE2_Q,
        "runtime_source_commit": runtime_source_commit,
        "session_ids": list(STAGE2_SESSIONS),
        "spacing_boundary_semantics": (
            "inclusive: distance >= spacing_steps → accepted (non-conflicting); "
            "strict: distance < spacing_steps → conflicting"
        ),
        "spacing_steps": {str(h): max(10, h // GRID_MS) for h in STAGE2_HORIZONS},
        "stage1_artifacts_modified": False,
        "v1_1_reports_modified": False,
    }

    # ── Write files ───────────────────────────────────────────────────────────
    out.mkdir(parents=True, exist_ok=True)
    csv_path.write_bytes(csv_bytes)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )

    return csv_path, manifest_path


def run_stage2_diagnostics(
    runtime_source_commit: str,
    output_dir: "Path | None" = None,
) -> "tuple[Path, Path]":
    """Top-level Stage 2 orchestrator.  Requires explicit authorisation.

    Calls generate_stage2_rows() (real OLD36 data path) then writes
    artifacts via write_stage2_artifacts().

    THIS MUST NOT BE INVOKED UNTIL EXPLICITLY AUTHORISED.
    """
    rows = generate_stage2_rows(contexts=None)
    return write_stage2_artifacts(rows, runtime_source_commit, output_dir=output_dir)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "STAGE2_VERSION",
    "STAGE2_SESSIONS",
    "STAGE2_ASSETS",
    "STAGE2_Q",
    "STAGE2_HORIZONS",
    "STAGE2_PRIMARY_FAMILIES",
    "STAGE2_CONTROL_FAMILIES",
    "STAGE2_FAMILIES",
    "STAGE2_UNIQUE_CASES",
    "STAGE2_POLICY_ROWS",
    "_STAGE2_CSV_COLUMNS",
    "Stage2Case",
    "expand_stage2_matrix",
    "compute_pre_overlap_diagnostics",
    "generate_stage2_rows",
    "write_stage2_artifacts",
    "run_stage2_diagnostics",
    "_greedy_latest_first_filter",
    "_sha256_positions",
    "_extract_pre_overlap",
    "_compute_policy_metrics",
    "_assert_i12_actual_field_comparison",
    "_assert_r3_p0_drift_guard",
]
