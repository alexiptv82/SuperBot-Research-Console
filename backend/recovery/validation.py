"""Golden-regression comparison functions.

Implements EVERY field defined in the frozen validation spec:
    backend/recovery/specs/RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt
    SHA256: 30aea96229dbd96f1c98419ecd76105db171d79dc16bba29338b9650a9558d29

Row/block level fields (spec lines 7-54):
    N, threshold, mean_signed_bps, median_signed_bps, mean_abs_move,
    hit_rate, disp_lo, disp_hi

Aggregate level fields (spec lines 56-90):
    positive_blocks, positive_share, feature_mean, total_blocks,
    min_block, max_block

SAFETY INVARIANTS (never remove):
- Pure comparison functions only. No I/O, no ZIP access, no database
  access, no NEW36 access.
- Tolerances below are transcribed VERBATIM from the frozen spec and
  MUST NOT be changed/loosened to make any comparison pass
  (no_tuning_on_mismatch=true).
- This module never hard-codes or embeds any OLD36 golden numeric
  value as an oracle; it only compares whatever two dicts the caller
  supplies.
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Frozen tolerances (verbatim from RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt)
# ---------------------------------------------------------------------------
TOL_THRESHOLD = 1e-8
TOL_MEAN_SIGNED_BPS = 1e-6
TOL_MEDIAN_SIGNED_BPS = 1e-6
TOL_MEAN_ABS_MOVE = 1e-6
TOL_HIT_RATE = 1e-12
TOL_DISP_LO = 1e-8
TOL_DISP_HI = 1e-8
TOL_POSITIVE_SHARE = 1e-12
TOL_FEATURE_MEAN = 1e-6
# N / positive_blocks / total_blocks / min_block / max_block:
# exact_integer_equality / exact_identity_match, tolerance=0.

# Simple feature names (spec: FEATURE_MAP keys / SIMPLE_FEATURES in engine.py).
# Duplicated here (not imported from engine.py) so this module stays a pure,
# dependency-light comparator usable without importing the reconstruction
# engine itself.
SIMPLE_FEATURES: frozenset[str] = frozenset({
    "bitget_ofi", "bitget_trade_flow", "depth_imbalance_l1", "depth_imbalance_l5",
    "external_ofi", "external_trade_flow", "fair_accel_100ms", "fair_gap_reversion",
    "leader_gap_100ms", "leader_gap_200ms", "leader_gap_500ms", "leader_gap_1000ms",
})


def is_simple_feature(feature: str) -> bool:
    return feature in SIMPLE_FEATURES


def safe_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def safe_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _abs_diff_fail(golden: float | None, candidate: float | None, tol: float) -> bool:
    """Generic absolute-difference comparator.

    mismatch_if_definedness_differs=true is applied uniformly: every
    abs-difference field in the frozen spec is either explicitly
    marked with this rule (mean_signed_bps, median_signed_bps,
    hit_rate) or has no alternate definedness rule at all (threshold,
    mean_abs_move, disp_lo, disp_hi, positive_share, feature_mean) —
    so treating a one-sided None as a mismatch is the only rule that
    does not silently invent a pass for an undefined/defined split.
    """
    if golden is None and candidate is None:
        return False
    if golden is None or candidate is None:
        return True
    return abs(golden - candidate) > tol


# ---------------------------------------------------------------------------
# Row / block level comparison (spec lines 7-54)
# ---------------------------------------------------------------------------


def compare_row(golden: dict, candidate: dict, feature: str) -> dict[str, bool]:
    """Row/block-level comparison.

    ``golden``    : raw golden CSV row (string-valued dict). Only keys
                     actually PRESENT are evaluated — a CSV schema that
                     does not carry a given column simply never
                     contributes that field to the row's fail set.
    ``candidate``  : reproduced value dict with the SAME key names as
                     the golden row (N, threshold, mean_signed_bps,
                     median_signed_bps, mean_abs_move, hit_rate,
                     disp_lo, disp_hi).
    ``feature``    : the row's feature name — gates the
                     simple_features_only (median_signed_bps,
                     mean_abs_move) and fair_gap_reversion_feature_only
                     (disp_lo, disp_hi) scoping rules.

    Returns ``{field: True_if_FAIL}`` for every field evaluated.

    N_mismatch_overrides_all_other_fields_on_that_row=true: once N
    fails, no other field is evaluated for this row (spec line 93).
    """
    fails: dict[str, bool] = {}
    simple = is_simple_feature(feature)
    fair_gap = feature == "fair_gap_reversion"

    g_N = safe_int(golden.get("N"))
    c_N = candidate.get("N")
    n_fail = (g_N is None) != (c_N is None) or (
        g_N is not None and c_N is not None and g_N != c_N
    )
    fails["N"] = n_fail
    if n_fail:
        return fails

    n_gt_0_both = (g_N is not None and g_N > 0) and (c_N is not None and c_N > 0)

    if "threshold" in golden:
        fails["threshold"] = _abs_diff_fail(
            safe_float(golden.get("threshold")), candidate.get("threshold"),
            TOL_THRESHOLD,
        )

    if "mean_signed_bps" in golden and n_gt_0_both:
        fails["mean_signed_bps"] = _abs_diff_fail(
            safe_float(golden.get("mean_signed_bps")), candidate.get("mean_signed_bps"),
            TOL_MEAN_SIGNED_BPS,
        )

    if simple and "median_signed_bps" in golden and n_gt_0_both:
        fails["median_signed_bps"] = _abs_diff_fail(
            safe_float(golden.get("median_signed_bps")), candidate.get("median_signed_bps"),
            TOL_MEDIAN_SIGNED_BPS,
        )

    if simple and "mean_abs_move" in golden:
        fails["mean_abs_move"] = _abs_diff_fail(
            safe_float(golden.get("mean_abs_move")), candidate.get("mean_abs_move"),
            TOL_MEAN_ABS_MOVE,
        )

    if "hit_rate" in golden and n_gt_0_both:
        fails["hit_rate"] = _abs_diff_fail(
            safe_float(golden.get("hit_rate")), candidate.get("hit_rate"),
            TOL_HIT_RATE,
        )

    if fair_gap and "disp_lo" in golden:
        fails["disp_lo"] = _abs_diff_fail(
            safe_float(golden.get("disp_lo")), candidate.get("disp_lo"), TOL_DISP_LO
        )

    if fair_gap and "disp_hi" in golden:
        fails["disp_hi"] = _abs_diff_fail(
            safe_float(golden.get("disp_hi")), candidate.get("disp_hi"), TOL_DISP_HI
        )

    return fails


def row_failed(fails: dict[str, bool]) -> bool:
    """row_level_fail_rule=any_single_field_fail_marks_the_row_a_FAIL."""
    return any(fails.values())


def first_failed_field(fails: dict[str, bool]) -> str | None:
    for k, v in fails.items():
        if v:
            return k
    return None


# ---------------------------------------------------------------------------
# Aggregate level comparison (spec lines 56-90)
# ---------------------------------------------------------------------------


def parse_block_identity(raw) -> tuple[str, str] | None:
    """Parse a golden min_block/max_block value into (session_id, asset).

    Frozen spec: min_block/max_block comparison=exact_identity_match on
    (session_id, asset) — NOT a numeric mean value. Accepts the common
    delimiter conventions. Returns None (not comparable under this
    field) if the value cannot be parsed as a two-part identity — e.g.
    a bare float. Some existing golden CP24/CP36 artifacts store a
    numeric block-mean under a column literally named ``min_block`` /
    ``max_block`` instead of an identity string; such rows are NOT
    silently treated as a pass under this field.
    """
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return (str(raw[0]), str(raw[1]))
    s = str(raw).strip()
    if not s:
        return None
    for sep in (",", "/", ":", "|"):
        if sep in s:
            parts = s.split(sep)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return (parts[0].strip(), parts[1].strip())
    return None


def compare_aggregate(golden: dict, candidate: dict) -> dict[str, bool | None]:
    """Aggregate-level comparison.

    ``golden``    : raw golden aggregate-CSV row.
    ``candidate`` : dict with keys total_blocks, positive_blocks_count,
                    positive_share, feature_mean, min_block, max_block
                    (as produced by engine.AggregateMetrics /
                    engine.DispersionAggregateMetrics).

    A value of ``None`` in the returned dict means "not comparable
    under this golden schema" (e.g. min_block/max_block stored as a
    bare numeric mean rather than an identity) — this is distinct from
    ``False`` (compared and passed) and MUST NOT be counted as a pass.
    """
    fails: dict[str, bool | None] = {}

    if "total_blocks" in golden or "blocks" in golden:
        g_tb = safe_int(golden.get("total_blocks", golden.get("blocks")))
        c_tb = candidate.get("total_blocks")
        fails["total_blocks"] = (g_tb is None) != (c_tb is None) or (
            g_tb is not None and c_tb is not None and g_tb != c_tb
        )

    if "positive_blocks" in golden:
        g_pb = safe_int(golden.get("positive_blocks"))
        c_pb = candidate.get("positive_blocks_count")
        fails["positive_blocks"] = (g_pb is None) != (c_pb is None) or (
            g_pb is not None and c_pb is not None and g_pb != c_pb
        )

    if "positive_share" in golden:
        fails["positive_share"] = _abs_diff_fail(
            safe_float(golden.get("positive_share")), candidate.get("positive_share"),
            TOL_POSITIVE_SHARE,
        )

    if "feature_mean" in golden or "mean_signed_bps" in golden:
        g_fm = safe_float(golden.get("feature_mean", golden.get("mean_signed_bps")))
        fails["feature_mean"] = _abs_diff_fail(
            g_fm, candidate.get("feature_mean"), TOL_FEATURE_MEAN
        )

    if "min_block" in golden:
        g_min = parse_block_identity(golden.get("min_block"))
        c_min = candidate.get("min_block")
        fails["min_block"] = None if g_min is None else (g_min != c_min)

    if "max_block" in golden:
        g_max = parse_block_identity(golden.get("max_block"))
        c_max = candidate.get("max_block")
        fails["max_block"] = None if g_max is None else (g_max != c_max)

    return fails


def aggregate_failed(fails: dict[str, bool | None]) -> bool:
    return any(v is True for v in fails.values())


__all__ = [
    "TOL_THRESHOLD",
    "TOL_MEAN_SIGNED_BPS",
    "TOL_MEDIAN_SIGNED_BPS",
    "TOL_MEAN_ABS_MOVE",
    "TOL_HIT_RATE",
    "TOL_DISP_LO",
    "TOL_DISP_HI",
    "TOL_POSITIVE_SHARE",
    "TOL_FEATURE_MEAN",
    "SIMPLE_FEATURES",
    "is_simple_feature",
    "safe_float",
    "safe_int",
    "compare_row",
    "row_failed",
    "first_failed_field",
    "parse_block_identity",
    "compare_aggregate",
    "aggregate_failed",
]
