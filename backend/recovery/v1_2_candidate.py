"""RECONSTRUCTION_V1.2 frozen candidate semantics (PRE-NEW36 technical freeze).

This module represents the minimum executable contract for the frozen V1.2
candidate:

    HZ = TZ
    HG = G1        (gate feature = bitget_depth_imbalance_l1)
    HC = C1        (gap feature  = bitget_gap_to_fair_bps)
    HB = B0        (greedy overlap filter with >= spacing)
    G1_THRESHOLD_SOURCE = TZ

Hard safety invariants (never remove):

* No import-time execution.
* No golden artifact reads, imports, opens, or path references.
* No NEW36 quantitative artifact access.
* No modification of engine.py, Stage1, Stage2, Stage3, v1_2_diagnostics.
* No side effects at import.
* FrozenAnalysisEngine remains NOT_CONFIGURED / accepts_input=False.

All numerical semantics defer to the frozen engine helpers where present
(:func:`engine._quantile_type7`, :func:`engine._greedy_overlap_filter`,
:func:`engine._build_forward_return_array`). This module intentionally
re-exposes only the *raw-sign, subset-preserving* composition specific to
the frozen V1.2 candidate.

Spec document:  backend/recovery/specs/V1_2_CANDIDATE_SPEC.txt
Acceptance:     backend/recovery/specs/V1_2_ACCEPTANCE_CRITERIA.txt
"""
from __future__ import annotations

from typing import Final

import numpy as np

from .engine import (
    GRID_MS,
    _greedy_overlap_filter,
    _quantile_type7,
)

CANDIDATE_VERSION: Final[str] = "V1_2_CANDIDATE_FROZEN"

# Frozen axis choices
HZ_VARIANT: Final[str] = "TZ"
HG_VARIANT: Final[str] = "G1"
HC_VARIANT: Final[str] = "C1"
HB_VARIANT: Final[str] = "B0"
G1_THRESHOLD_SOURCE: Final[str] = "TZ"

# Frozen constants
THRESHOLD_EXACTNESS_TOLERANCE: Final[float] = 1e-12
CONTROL_ENVELOPE_MULTIPLIER: Final[float] = 1.0

# Frozen columns (from V1.1 FEATURE_MAP)
G1_GATE_FEATURE_COLUMN: Final[str] = "bitget_depth_imbalance_l1"
G1_EXT_OFI_COLUMN: Final[str] = "external_ofi_consensus_l1"
C1_GAP_FEATURE_COLUMN: Final[str] = "bitget_gap_to_fair_bps"

# Explicitly forbidden columns inside G1 / C1 confirmation logic.
CANDIDATE_FORBIDDEN_CONFIRMATION_COLUMNS: Final[tuple[str, ...]] = (
    "z_di_l1",
    "z_ext_ofi",
    "z_gap_d",
)


# =====================================================================
# TZ threshold (HZ = TZ)
# =====================================================================

def tz_threshold(quality: np.ndarray, signal: np.ndarray, q: float) -> float | None:
    """Compute the frozen HZ=TZ threshold for a raw simple feature.

    Domain:
        quality AND isfinite(signal) AND signal != 0.0

    Threshold:
        type7_quantile( abs(signal), q )

    Returns ``None`` if fewer than 2 finite non-zero values are present in
    the TZ domain (mirroring the frozen V1.1 threshold-None policy which
    forces N=0 downstream).
    """
    if quality.shape != signal.shape:
        raise ValueError(
            f"tz_threshold: shape mismatch quality={quality.shape} "
            f"signal={signal.shape}"
        )
    if not (0.0 < q < 1.0):
        raise ValueError(f"tz_threshold: q must be in (0,1); got {q!r}")
    domain = (
        np.asarray(quality, dtype=bool)
        & np.isfinite(signal)
        & (signal != 0.0)
    )
    vals = np.abs(np.asarray(signal, dtype=float)[domain])
    return _quantile_type7(vals, q)


# =====================================================================
# G1 pre-overlap mask (HG = G1 with G1_THRESHOLD_SOURCE = TZ)
# =====================================================================

def g1_pre_overlap_mask(
    *,
    quality: np.ndarray,
    di_l1: np.ndarray,
    ext_ofi: np.ndarray,
    fwd: np.ndarray,
    tz_thr: float | None,
) -> np.ndarray:
    """Return the boolean pre-overlap event mask for the frozen G1 variant.

    Composition (raw signs only, no z-score confirmation):
        quality
        AND isfinite(di_l1)
        AND di_l1 != 0
        AND abs(di_l1) >= tz_thr
        AND isfinite(ext_ofi)
        AND ext_ofi != 0
        AND sign(ext_ofi) == sign(di_l1)
        AND isfinite(fwd)

    ``tz_thr`` is the TZ threshold of ``depth_imbalance_l1`` for the same
    (session, asset, q) block. If ``tz_thr`` is ``None`` the frozen policy
    forces N=0 (empty mask).
    """
    for name, arr in (("quality", quality), ("di_l1", di_l1),
                      ("ext_ofi", ext_ofi), ("fwd", fwd)):
        if arr.shape != quality.shape:
            raise ValueError(
                f"g1_pre_overlap_mask: {name} shape mismatch {arr.shape} "
                f"vs {quality.shape}"
            )
    if tz_thr is None:
        return np.zeros(quality.shape, dtype=bool)
    q = np.asarray(quality, dtype=bool)
    di = np.asarray(di_l1, dtype=float)
    ofi = np.asarray(ext_ofi, dtype=float)
    fw = np.asarray(fwd, dtype=float)
    return (
        q
        & np.isfinite(di) & (di != 0.0)
        & (np.abs(di) >= float(tz_thr))
        & np.isfinite(ofi) & (ofi != 0.0)
        & (np.sign(ofi) == np.sign(di))
        & np.isfinite(fw)
    )


def g1_parent_gate_mask(
    *,
    quality: np.ndarray,
    di_l1: np.ndarray,
    fwd: np.ndarray,
    tz_thr: float | None,
) -> np.ndarray:
    """Return the parent depth_imbalance_l1 gate mask against which G1 must
    be a subset (I_G1_PARENT_SUBSET).

    Composition:
        quality AND isfinite(di_l1) AND di_l1!=0
        AND abs(di_l1) >= tz_thr AND isfinite(fwd)
    """
    if tz_thr is None:
        return np.zeros(quality.shape, dtype=bool)
    q = np.asarray(quality, dtype=bool)
    di = np.asarray(di_l1, dtype=float)
    fw = np.asarray(fwd, dtype=float)
    return (
        q
        & np.isfinite(di) & (di != 0.0)
        & (np.abs(di) >= float(tz_thr))
        & np.isfinite(fw)
    )


def assert_g1_parent_subset(
    g1_mask: np.ndarray, parent_mask: np.ndarray
) -> None:
    """Fail-closed subset invariant: G1 pre-overlap events must be a subset
    of the parent depth_l1 gate. Raises ``AssertionError`` on violation.
    """
    if g1_mask.shape != parent_mask.shape:
        raise AssertionError(
            f"I_G1_PARENT_SUBSET FAIL: shape mismatch "
            f"g1={g1_mask.shape} parent={parent_mask.shape}"
        )
    violations = np.asarray(g1_mask, dtype=bool) & (
        ~np.asarray(parent_mask, dtype=bool)
    )
    if bool(violations.any()):
        idxs = np.nonzero(violations)[0][:8].tolist()
        raise AssertionError(
            f"I_G1_PARENT_SUBSET FAIL: {int(violations.sum())} G1 events "
            f"outside parent gate; first_indices={idxs}"
        )


def g1_direction(di_l1: np.ndarray) -> np.ndarray:
    """Frozen G1 direction: sign(di_l1). Zero rows are already excluded by
    the pre-overlap mask; caller must apply mask before using direction."""
    return np.sign(np.asarray(di_l1, dtype=float))


# =====================================================================
# C1 pre-overlap mask (HC = C1, raw signs only)
# =====================================================================

def c1_pre_overlap_mask(
    *,
    quality: np.ndarray,
    gap: np.ndarray,
    di_l1: np.ndarray,
    ext_ofi: np.ndarray,
    fwd: np.ndarray,
    gap_thr: float | None,
) -> np.ndarray:
    """Return the boolean pre-overlap event mask for the frozen C1 variant.

    Composition (raw signs only, no z-score confirmation):
        quality
        AND isfinite(gap) AND gap != 0
        AND abs(gap) >= gap_thr
        AND isfinite(di_l1) AND di_l1 != 0
        AND sign(di_l1) == -sign(gap)
        AND isfinite(ext_ofi) AND ext_ofi != 0
        AND sign(ext_ofi) == -sign(gap)
        AND isfinite(fwd)

    ``gap_thr`` is the frozen fair-gap threshold (unchanged by HZ zero rule).
    ``None`` forces N=0.
    """
    for name, arr in (("quality", quality), ("gap", gap),
                      ("di_l1", di_l1), ("ext_ofi", ext_ofi),
                      ("fwd", fwd)):
        if arr.shape != quality.shape:
            raise ValueError(
                f"c1_pre_overlap_mask: {name} shape mismatch {arr.shape} "
                f"vs {quality.shape}"
            )
    if gap_thr is None:
        return np.zeros(quality.shape, dtype=bool)
    q = np.asarray(quality, dtype=bool)
    g = np.asarray(gap, dtype=float)
    di = np.asarray(di_l1, dtype=float)
    ofi = np.asarray(ext_ofi, dtype=float)
    fw = np.asarray(fwd, dtype=float)
    sg = np.sign(g)
    return (
        q
        & np.isfinite(g) & (g != 0.0)
        & (np.abs(g) >= float(gap_thr))
        & np.isfinite(di) & (di != 0.0)
        & (np.sign(di) == -sg)
        & np.isfinite(ofi) & (ofi != 0.0)
        & (np.sign(ofi) == -sg)
        & np.isfinite(fw)
    )


def c1_gap_parent_mask(
    *,
    quality: np.ndarray,
    gap: np.ndarray,
    fwd: np.ndarray,
    gap_thr: float | None,
) -> np.ndarray:
    """Return the shared gap threshold-crossing set against which C1 must
    be a subset (I_C1_GAP_SUBSET)."""
    if gap_thr is None:
        return np.zeros(quality.shape, dtype=bool)
    q = np.asarray(quality, dtype=bool)
    g = np.asarray(gap, dtype=float)
    fw = np.asarray(fwd, dtype=float)
    return (
        q
        & np.isfinite(g) & (g != 0.0)
        & (np.abs(g) >= float(gap_thr))
        & np.isfinite(fw)
    )


def assert_c1_gap_subset(
    c1_mask: np.ndarray, gap_parent_mask: np.ndarray
) -> None:
    """Fail-closed subset invariant: C1 pre-overlap events must be a subset
    of the shared gap threshold-crossing set.
    """
    if c1_mask.shape != gap_parent_mask.shape:
        raise AssertionError(
            f"I_C1_GAP_SUBSET FAIL: shape mismatch "
            f"c1={c1_mask.shape} parent={gap_parent_mask.shape}"
        )
    violations = np.asarray(c1_mask, dtype=bool) & (
        ~np.asarray(gap_parent_mask, dtype=bool)
    )
    if bool(violations.any()):
        idxs = np.nonzero(violations)[0][:8].tolist()
        raise AssertionError(
            f"I_C1_GAP_SUBSET FAIL: {int(violations.sum())} C1 events "
            f"outside gap parent set; first_indices={idxs}"
        )


def c1_direction(gap: np.ndarray) -> np.ndarray:
    """Frozen C1 direction: -sign(gap)."""
    return -np.sign(np.asarray(gap, dtype=float))


# =====================================================================
# HB filters (B0 greedy = candidate; B1 strict = predeclared diagnostic)
# =====================================================================

def spacing_steps_for_horizon(horizon_ms: int) -> int:
    """Frozen spacing_steps = max(10, horizon_ms // GRID_MS)."""
    if horizon_ms <= 0:
        raise ValueError(f"horizon_ms must be positive; got {horizon_ms!r}")
    k = int(horizon_ms) // int(GRID_MS)
    return max(10, k)


def b0_greedy_filter(
    positions: np.ndarray, spacing_steps: int
) -> np.ndarray:
    """Frozen candidate greedy filter.

    Accepts the first event; subsequently accepts iff
    ``pos - last_accepted >= spacing_steps`` (equality accepted).

    Positions must be sorted ascending. Returns a boolean mask.
    """
    pos = np.asarray(positions, dtype=np.int64)
    if pos.ndim != 1:
        raise ValueError("b0_greedy_filter: positions must be 1-D")
    if pos.size > 1 and bool(np.any(pos[1:] < pos[:-1])):
        raise ValueError("b0_greedy_filter: positions must be sorted ascending")
    return _greedy_overlap_filter(pos, int(spacing_steps))


def b1_diagnostic_filter(
    positions: np.ndarray, spacing_steps: int
) -> np.ndarray:
    """Predeclared HB diagnostic filter using strict '>' spacing.

    Never a candidate. Provided solely for the future acceptance evaluator
    to compute discriminating cells (Section G).
    """
    pos = np.asarray(positions, dtype=np.int64)
    if pos.ndim != 1:
        raise ValueError("b1_diagnostic_filter: positions must be 1-D")
    if pos.size > 1 and bool(np.any(pos[1:] < pos[:-1])):
        raise ValueError("b1_diagnostic_filter: positions must be sorted ascending")
    accepted = np.zeros(pos.size, dtype=bool)
    last = -int(spacing_steps) - 1
    s = int(spacing_steps)
    for i in range(pos.size):
        p = int(pos[i])
        if p - last > s:  # strict >
            accepted[i] = True
            last = p
    return accepted


def exact_spacing_pair_n(positions: np.ndarray, spacing_steps: int) -> int:
    """Count of consecutive-in-input position pairs whose gap == spacing_steps.

    A discriminating HB cell requires ``exact_spacing_pair_n > 0`` AND
    B0/B1 accepted-position digests to differ.
    """
    pos = np.asarray(positions, dtype=np.int64)
    if pos.size < 2:
        return 0
    return int(np.sum((pos[1:] - pos[:-1]) == int(spacing_steps)))


def assert_hb_i11(b0_accepted_n: int, b1_accepted_n: int) -> None:
    """I_HB_I11: B1.accepted_n <= B0.accepted_n per paired case."""
    if int(b1_accepted_n) > int(b0_accepted_n):
        raise AssertionError(
            f"I_HB_I11 FAIL: B1.accepted_n={b1_accepted_n} > "
            f"B0.accepted_n={b0_accepted_n}"
        )


def assert_hb_i12(
    exact_spacing_pair_n_value: int,
    b0_accepted: np.ndarray,
    b1_accepted: np.ndarray,
) -> None:
    """I_HB_I12: if exact_spacing_pair_n == 0 then B0 and B1 accepted masks
    are element-wise identical.
    """
    if int(exact_spacing_pair_n_value) != 0:
        return
    if b0_accepted.shape != b1_accepted.shape:
        raise AssertionError(
            "I_HB_I12 FAIL: esp=0 but B0/B1 shape mismatch "
            f"{b0_accepted.shape} vs {b1_accepted.shape}"
        )
    if not bool(np.array_equal(b0_accepted, b1_accepted)):
        diffs = int(np.sum(b0_accepted != b1_accepted))
        raise AssertionError(
            f"I_HB_I12 FAIL: esp=0 but B0 and B1 differ in {diffs} positions"
        )


__all__ = [
    "CANDIDATE_VERSION",
    "HZ_VARIANT", "HG_VARIANT", "HC_VARIANT", "HB_VARIANT",
    "G1_THRESHOLD_SOURCE",
    "THRESHOLD_EXACTNESS_TOLERANCE",
    "CONTROL_ENVELOPE_MULTIPLIER",
    "G1_GATE_FEATURE_COLUMN", "G1_EXT_OFI_COLUMN", "C1_GAP_FEATURE_COLUMN",
    "CANDIDATE_FORBIDDEN_CONFIRMATION_COLUMNS",
    "tz_threshold",
    "g1_pre_overlap_mask", "g1_parent_gate_mask",
    "assert_g1_parent_subset", "g1_direction",
    "c1_pre_overlap_mask", "c1_gap_parent_mask",
    "assert_c1_gap_subset", "c1_direction",
    "spacing_steps_for_horizon",
    "b0_greedy_filter", "b1_diagnostic_filter",
    "exact_spacing_pair_n",
    "assert_hb_i11", "assert_hb_i12",
]
