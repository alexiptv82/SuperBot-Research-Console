"""RECONSTRUCTION_V1.2 PRE-NEW36 acceptance evaluator (frozen).

Implements the canonical acceptance criteria document
(``backend/recovery/specs/V1_2_ACCEPTANCE_CRITERIA.txt``).

All cell / axis / overall verdict logic is here; NO golden reads,
NO NEW36 access, NO import-time side effects.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

THRESHOLD_EXACTNESS_TOLERANCE: float = 1e-12
CONTROL_ENVELOPE_MULTIPLIER: float = 1.0

# Cell classifications (Section C ordered)
CLS_NOT_APPLICABLE = "NOT_APPLICABLE"
CLS_CATASTROPHIC_STRUCTURAL = "CATASTROPHIC_STRUCTURAL"
CLS_WITHIN = "WITHIN"
CLS_INVALID_RUN = "INVALID_RUN"
CLS_AXIS_UNVERIFIABLE = "AXIS_UNVERIFIABLE"
CLS_FALSIFIED_BASELINE_ANOMALY = "FALSIFIED_BASELINE_ANOMALY"
CLS_CATASTROPHIC = "CATASTROPHIC"
CLS_ELEVATED = "ELEVATED"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class CellInput:
    """One HG/HC/HZ evaluation cell.

    Fields:
        golden_N:            golden pre-overlap N (int) or None (threshold None)
        candidate_N:         candidate pre-overlap N (int) or None (threshold None)
        env:                 control envelope for relN (float >= 0)
        relN:                (candidate_N - golden_N) / max(golden_N, 1) style
                             precomputed relative-N measure supplied by caller
        has_falsified:       whether a falsified baseline is available
        relN_falsified:      relN of the falsified baseline (float; ignored
                             if has_falsified is False)
        control_envelope_available: whether the control envelope is available
    """
    golden_N: int | None
    candidate_N: int | None
    env: float
    relN: float
    has_falsified: bool
    relN_falsified: float
    control_envelope_available: bool = True


@dataclass(frozen=True)
class CellResult:
    classification: str
    r: float  # envelope ratio


def envelope_ratio(rel_n: float, env: float) -> float:
    """Section D envelope ratio r.

    r = abs(relN) / env           if env > 0
    r = 0                         if env == 0 and abs(relN) == 0
    r = +infinity                 if env == 0 and abs(relN) > 0
    """
    a = abs(float(rel_n))
    e = float(env)
    if e > 0.0:
        return a / e
    if a == 0.0:
        return 0.0
    return math.inf


def classify_cell(cell: CellInput) -> CellResult:
    """Section C ordered classification.

    Precedence (top to bottom, first match wins):
        1. NOT_APPLICABLE           (threshold None on either side)
        2. CATASTROPHIC_STRUCTURAL  (structural N=0 asymmetry)
        3. WITHIN                   (0/0)
        4. INVALID_RUN              (falsified baseline missing)
        5. AXIS_UNVERIFIABLE        (control envelope unavailable)
        6. FALSIFIED_BASELINE_ANOMALY  (F <= env)
        7. WITHIN / CATASTROPHIC / ELEVATED (band classification)
    """
    gN = cell.golden_N
    cN = cell.candidate_N
    # 1. NOT_APPLICABLE
    if gN is None or cN is None:
        return CellResult(CLS_NOT_APPLICABLE, r=0.0)
    # 2. CATASTROPHIC_STRUCTURAL
    if (gN == 0 and cN > 0) or (cN == 0 and gN > 0):
        return CellResult(CLS_CATASTROPHIC_STRUCTURAL, r=math.inf)
    # 3. WITHIN 0/0
    if gN == 0 and cN == 0:
        return CellResult(CLS_WITHIN, r=envelope_ratio(cell.relN, cell.env))
    # 4. INVALID_RUN
    if not cell.has_falsified:
        return CellResult(CLS_INVALID_RUN, r=math.inf)
    # 5. AXIS_UNVERIFIABLE
    if not cell.control_envelope_available:
        return CellResult(CLS_AXIS_UNVERIFIABLE, r=math.inf)
    # 6. FALSIFIED_BASELINE_ANOMALY
    F = abs(float(cell.relN_falsified))
    env = float(cell.env)
    if F <= env:
        return CellResult(
            CLS_FALSIFIED_BASELINE_ANOMALY,
            r=envelope_ratio(cell.relN, cell.env),
        )
    # 7. Bands
    a = abs(float(cell.relN))
    r = envelope_ratio(cell.relN, cell.env)
    if a <= env:
        return CellResult(CLS_WITHIN, r=r)
    # CATASTROPHIC band bound = sqrt(env * F)
    bound = math.sqrt(env * F) if env >= 0 and F >= 0 else math.inf
    if a >= bound:
        return CellResult(CLS_CATASTROPHIC, r=r)
    return CellResult(CLS_ELEVATED, r=r)


# ---------------------------------------------------------------------
# Median (Section D)
# ---------------------------------------------------------------------

def median_odd_even(values: Sequence[float]) -> float:
    """odd N: central sorted value; even N: mean of two central values.

    NOT_APPLICABLE cells must be excluded before calling this function.
    +inf values are permitted; if the central value is +inf the median is +inf.
    """
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        raise ValueError("median_odd_even: empty input")
    if n % 2 == 1:
        return float(xs[n // 2])
    mid_lo = xs[n // 2 - 1]
    mid_hi = xs[n // 2]
    if math.isinf(mid_lo) or math.isinf(mid_hi):
        return math.inf
    return (float(mid_lo) + float(mid_hi)) / 2.0


# ---------------------------------------------------------------------
# HG / HC verdict architecture (Sections E, F, K)
# ---------------------------------------------------------------------

def _axis_component1_verdict(results: Sequence[CellResult]) -> str:
    """Component-1 (N reconstruction) verdict for HG/HC axes.

    PASS: no CATASTROPHIC / CATASTROPHIC_STRUCTURAL / FALSIFIED_BASELINE_ANOMALY
          and no INVALID_RUN, and median(r) over applicable cells <= 1.
    FAIL: any FALSIFIED_BASELINE_ANOMALY OR any INVALID_RUN OR CATASTROPHIC.
    Otherwise PARTIAL provided no CATASTROPHIC and median(r) <= 1.
    """
    if any(r.classification == CLS_INVALID_RUN for r in results):
        return VERDICT_FAIL
    if any(r.classification == CLS_FALSIFIED_BASELINE_ANOMALY for r in results):
        return VERDICT_FAIL
    has_catastrophic = any(
        r.classification in (CLS_CATASTROPHIC, CLS_CATASTROPHIC_STRUCTURAL)
        for r in results
    )
    applicable = [r.r for r in results if r.classification != CLS_NOT_APPLICABLE]
    if not applicable:
        # nothing to measure; treat as UNVERIFIABLE component (Section K axis rules)
        return VERDICT_UNVERIFIABLE
    med = median_odd_even(applicable)
    if has_catastrophic:
        return VERDICT_FAIL
    if med <= 1.0:
        # No catastrophic AND median<=1: PASS candidate; PARTIAL if there is
        # any ELEVATED cell (structural degradation without catastrophic).
        if any(r.classification == CLS_ELEVATED for r in results):
            return VERDICT_PARTIAL
        return VERDICT_PASS
    return VERDICT_FAIL  # median(r) > 1 with no catastrophic still fails PASS


def axis_verdict_hg_hc(
    n_reconstruction_cells: Sequence[CellResult],
    threshold_component_verdict: str,
) -> str:
    """HG / HC verdict architecture (Sections E, F).

    - PASS iff N reconstruction PASS AND threshold component PASS.
    - FAIL if either FAILS OR any FALSIFIED_BASELINE_ANOMALY in cells.
    - PARTIAL otherwise, provided no CATASTROPHIC cell AND median(r) <= 1.
    """
    if threshold_component_verdict not in (
        VERDICT_PASS, VERDICT_FAIL, VERDICT_PARTIAL, VERDICT_UNVERIFIABLE
    ):
        raise ValueError(
            f"axis_verdict_hg_hc: invalid threshold verdict "
            f"{threshold_component_verdict!r}"
        )
    comp1 = _axis_component1_verdict(n_reconstruction_cells)
    if comp1 == VERDICT_FAIL or threshold_component_verdict == VERDICT_FAIL:
        return VERDICT_FAIL
    if comp1 == VERDICT_PASS and threshold_component_verdict == VERDICT_PASS:
        return VERDICT_PASS
    # PARTIAL feasibility: no CATASTROPHIC and median(r) <= 1
    applicable = [
        r.r for r in n_reconstruction_cells
        if r.classification != CLS_NOT_APPLICABLE
    ]
    if applicable and median_odd_even(applicable) <= 1.0 and not any(
        r.classification in (CLS_CATASTROPHIC, CLS_CATASTROPHIC_STRUCTURAL)
        for r in n_reconstruction_cells
    ):
        return VERDICT_PARTIAL
    if comp1 == VERDICT_UNVERIFIABLE or \
       threshold_component_verdict == VERDICT_UNVERIFIABLE:
        return VERDICT_UNVERIFIABLE
    return VERDICT_FAIL


def threshold_component_verdict(
    pairs: Sequence[tuple[float | None, float | None]],
    *,
    tolerance: float = THRESHOLD_EXACTNESS_TOLERANCE,
) -> str:
    """Exact threshold prediction verdict (component 2 for HG/HC).

    ``pairs`` is a sequence of (candidate_threshold, golden_threshold);
    ``None`` on either side is NOT_APPLICABLE (does not vote).
    """
    applicable = [
        (c, g) for (c, g) in pairs if c is not None and g is not None
    ]
    if not applicable:
        return VERDICT_UNVERIFIABLE
    for (c, g) in applicable:
        if not math.isfinite(c) or not math.isfinite(g):
            return VERDICT_FAIL
        if abs(float(c) - float(g)) > float(tolerance):
            return VERDICT_FAIL
    return VERDICT_PASS


# ---------------------------------------------------------------------
# HB verdict (Section G)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class HBCellInput:
    exact_spacing_pair_n: int
    b0_positions_sha256: str
    b1_positions_sha256: str
    b0_abs_relN: float
    b1_abs_relN: float
    i11_holds: bool
    i12_holds: bool
    is_control_family: bool = True


def _is_discriminating(cell: HBCellInput) -> bool:
    return (
        cell.exact_spacing_pair_n > 0
        and cell.b0_positions_sha256 != cell.b1_positions_sha256
        and cell.is_control_family
    )


def hb_verdict(cells: Sequence[HBCellInput]) -> str:
    """Section G HB verdict."""
    if any(not c.i11_holds for c in cells):
        return VERDICT_FAIL
    if any(not c.i12_holds for c in cells):
        return VERDICT_FAIL
    disc = [c for c in cells if _is_discriminating(c)]
    if not disc:
        return VERDICT_UNVERIFIABLE
    b1_strictly_better = 0
    b0_strictly_better = 0
    for c in disc:
        if c.b1_abs_relN < c.b0_abs_relN:
            b1_strictly_better += 1
        elif c.b0_abs_relN < c.b1_abs_relN:
            b0_strictly_better += 1
    if b1_strictly_better == len(disc):
        return VERDICT_FAIL
    if b1_strictly_better == 0 and b0_strictly_better >= 1:
        return VERDICT_PASS
    return VERDICT_PARTIAL


# ---------------------------------------------------------------------
# HZ verdict (Section H)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class HZCellInput:
    is_discriminating: bool
    threshold_match: bool           # abs(candidate_thr - golden_thr) <= tolerance
    classification: str             # from classify_cell (used for CAT/FBA)


def hz_verdict(cells: Sequence[HZCellInput], applicable_r: Sequence[float]) -> str:
    """Section H HZ verdict.

    UNVERIFIABLE if no HZ_DISCRIMINATING cell.
    FAIL if any threshold mismatch, any CATASTROPHIC_STRUCTURAL, or any
    FALSIFIED_BASELINE_ANOMALY.
    PASS if every discriminating cell matches and no CATASTROPHIC/FBA and
    median(r) <= 1.
    PARTIAL otherwise.
    """
    disc = [c for c in cells if c.is_discriminating]
    if not disc:
        return VERDICT_UNVERIFIABLE
    if any(not c.threshold_match for c in disc):
        return VERDICT_FAIL
    if any(c.classification == CLS_CATASTROPHIC_STRUCTURAL for c in cells):
        return VERDICT_FAIL
    if any(c.classification == CLS_FALSIFIED_BASELINE_ANOMALY for c in cells):
        return VERDICT_FAIL
    if not applicable_r:
        return VERDICT_PARTIAL
    med = median_odd_even(list(applicable_r))
    if med <= 1.0:
        return VERDICT_PASS
    return VERDICT_PARTIAL


# ---------------------------------------------------------------------
# Collector / pipeline mismatch (Section I)
# ---------------------------------------------------------------------

COLLECTOR_PRECONDITION_FAIL = "PRECONDITION_FAIL"
COLLECTOR_INVALID_RUN = "INVALID_RUN"
COLLECTOR_OK = "OK"


def collector_mismatch_verdict(
    *,
    collector_sha_expected: str,
    collector_sha_actual: str,
    pipeline_sha_expected: str,
    pipeline_sha_actual: str,
    quantitative_access_started: bool,
) -> str:
    """Section I collector/pipeline mismatch outcome."""
    mismatch = (
        collector_sha_expected != collector_sha_actual
        or pipeline_sha_expected != pipeline_sha_actual
    )
    if not mismatch:
        return COLLECTOR_OK
    if quantitative_access_started:
        return COLLECTOR_INVALID_RUN
    return COLLECTOR_PRECONDITION_FAIL


# ---------------------------------------------------------------------
# Overall aggregation (Section K)
# ---------------------------------------------------------------------

def overall_verdict(axes: dict[str, str]) -> str:
    """Section K overall aggregation over {HZ, HG, HC, HB}."""
    values = list(axes.values())
    if any(v == VERDICT_FAIL for v in values):
        return VERDICT_FAIL
    if all(v == VERDICT_PASS for v in values):
        return VERDICT_PASS
    if any(v == VERDICT_PARTIAL for v in values):
        return VERDICT_PARTIAL
    if any(v == VERDICT_UNVERIFIABLE for v in values):
        return VERDICT_UNVERIFIABLE
    return VERDICT_PARTIAL


__all__ = [
    "THRESHOLD_EXACTNESS_TOLERANCE", "CONTROL_ENVELOPE_MULTIPLIER",
    "CLS_NOT_APPLICABLE", "CLS_CATASTROPHIC_STRUCTURAL", "CLS_WITHIN",
    "CLS_INVALID_RUN", "CLS_AXIS_UNVERIFIABLE", "CLS_FALSIFIED_BASELINE_ANOMALY",
    "CLS_CATASTROPHIC", "CLS_ELEVATED",
    "VERDICT_PASS", "VERDICT_FAIL", "VERDICT_PARTIAL", "VERDICT_UNVERIFIABLE",
    "CellInput", "CellResult", "HBCellInput", "HZCellInput",
    "envelope_ratio", "classify_cell", "median_odd_even",
    "axis_verdict_hg_hc", "threshold_component_verdict",
    "hb_verdict", "hz_verdict",
    "collector_mismatch_verdict",
    "COLLECTOR_OK", "COLLECTOR_PRECONDITION_FAIL", "COLLECTOR_INVALID_RUN",
    "overall_verdict",
]
