"""Recovery-rule catalogue with provenance.

Every rule the frozen validator needs is enumerated here with its
current evidence status. This file is a data catalogue — it never
chooses formulas by fitting against golden outputs (see the
anti-overfitting rule in the project directive).

Statuses (mutually exclusive):

- ``EXPLICIT_EVIDENCE``  : recovered directly from an artifact
  (collector source, published report or a CSV column value that
  admits only one interpretation).
- ``MATH_UNIQUE``        : uniquely determined by the golden
  numbers, but the raw grids are required to verify. Marked
  MATH_UNIQUE only once the constraint has been shown to admit
  exactly one implementation; until then use INFERRED_PENDING.
- ``INFERRED_PENDING``   : one plausible interpretation. Requires
  golden regression against the raw OLD36 grids to confirm.
- ``AMBIGUOUS``          : two or more materially different
  implementations satisfy every constraint we currently have.
- ``MISSING``            : no artifact carries this information.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RuleStatus(str, Enum):
    EXPLICIT_EVIDENCE = "EXPLICIT_EVIDENCE"
    MATH_UNIQUE = "MATH_UNIQUE"
    INFERRED_PENDING = "INFERRED_PENDING"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING = "MISSING"


@dataclass
class Rule:
    key: str
    description: str
    status: RuleStatus
    evidence: list[str] = field(default_factory=list)
    inferred_hypothesis: str | None = None
    confidence: str = "pending"  # low | medium | high | pending
    depends_on_raw_old36: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "status": self.status.value,
            "evidence": list(self.evidence),
            "inferred_hypothesis": self.inferred_hypothesis,
            "confidence": self.confidence,
            "depends_on_raw_old36": self.depends_on_raw_old36,
        }


def _r(*args, **kwargs) -> Rule:
    return Rule(*args, **kwargs)


# ---------------------------------------------------------------------------
# Recovery rule catalogue
#
# Ordering follows the audit questionnaire so downstream tools can
# diff two runs and see status transitions cleanly.
# ---------------------------------------------------------------------------

RULES: list[Rule] = [
    _r(
        key="horizons",
        description="Forward-return horizons in ms.",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=[
            "CP24/CP36 block-level CSV `horizon_ms` distinct values "
            "= {100,200,500,1000,2000,5000,10000,30000}",
            "Handoff §12 states the eight horizons explicitly",
        ],
        confidence="high",
    ),
    _r(
        key="quantiles",
        description="Quantile levels evaluated per feature.",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=[
            "CP24/CP36 block-level CSV `q` distinct values = {0.8,0.9,0.95}",
            "Reports state q90 is the primary decision quantile",
        ],
        confidence="high",
    ),
    _r(
        key="evaluation_block",
        description="Evaluation block = (session_id, asset).",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=[
            "CP24 has 14 (session,asset) blocks, CP36 all36 has 22 = 14+8",
            "Every block-level CSV keys on session+asset",
        ],
        confidence="high",
    ),
    _r(
        key="quality_gate",
        description=(
            "Row admissible for analysis iff "
            "adjusted_fair_venue_count>=2 AND bitget_book_age_recv_ms<=1000 "
            "AND bitget_mid finite and positive."
        ),
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=[
            "Handoff §12 states the quality gate",
            "Collector exposes adjusted_fair_venue_count, book_age_recv_ms, mid",
        ],
        confidence="high",
    ),
    _r(
        key="target_definition",
        description="Target = Bitget mid forward log-return * 10000 (bps).",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=[
            "Handoff §12 target",
            "Collector uses log(x2/x1)*10000 for every bps quantity",
        ],
        confidence="high",
    ),
    _r(
        key="simple_feature_list",
        description="12 simple analysis features (bitget_ofi, bitget_trade_flow, "
        "depth_imbalance_l1/l5, external_ofi, external_trade_flow, "
        "fair_accel_100ms, fair_gap_reversion, leader_gap_{100,200,500,1000}ms).",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=["CP24 simple block-level CSV distinct `feature` values"],
        confidence="high",
    ),
    _r(
        key="composite_horizons",
        description="Composite features are evaluated at 5 horizons only: "
        "{1000, 2000, 5000, 10000, 30000} ms. Sub-second horizons "
        "{100, 200, 500} are NOT evaluated on any composite.",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=[
            "CP24 composite block-level CSV `horizon_ms` distinct values "
            "= {1000, 2000, 5000, 10000, 30000}",
            "CP36 composite_block_level_new12 same subset",
            "Row totals: 14*10*5*3 = 2100 (CP24), 8*10*5*3 = 1200 (CP36).",
        ],
        confidence="high",
    ),
    _r(
        key="composite_feature_list",
        description="10 composite features (depthBoth, depthL1_extOFI, gap_depthBoth, "
        "gap_depthL1, gap_depth_extOFI, gap_extOFI, gap_leader1000, gap_localOFI, "
        "gap_localTrade, leader1000_extOFI).",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=["CP24 composite block-level CSV distinct `feature` values"],
        confidence="high",
    ),
    _r(
        key="analysis_feature_to_grid_column_map",
        description="Mapping from analysis feature names to collector grid "
        "columns (bitget_ofi -> bitget_ofi_norm_l1, depth_imbalance_l1 -> "
        "bitget_depth_imbalance_l1, fair_gap_reversion -> bitget_gap_to_fair_bps, "
        "leader_gap_{X}ms -> leader_gap_{X}ms_bps, etc.).",
        status=RuleStatus.INFERRED_PENDING,
        evidence=[
            "Feature names + collector output field names are mutually "
            "suggestive; final confirmation requires reproducing block N "
            "and mean_signed_bps against a raw OLD36 grid.",
        ],
        inferred_hypothesis="see phase1_audit.md §2 mapping table.",
        confidence="medium",
        depends_on_raw_old36=True,
    ),
    _r(
        key="sign_conventions_simple",
        description="Direction: +sign(signal) for every simple feature, "
        "except fair_gap_reversion which uses -sign(gap).",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=["Handoff §12 direction rules"],
        confidence="high",
    ),
    _r(
        key="forward_return_alignment",
        description="Forward return at horizon H is mid[t+H]/mid[t] on the 100ms grid.",
        status=RuleStatus.INFERRED_PENDING,
        evidence=[
            "Grid is a 100ms monotonic sampler; log/bps convention explicit",
            "Exact end-of-session truncation (censor last H rows vs. drop) "
            "not specified anywhere in the artifacts",
        ],
        inferred_hypothesis="Drop rows where t+H exceeds grid end; drop rows "
        "where either mid is not finite/positive.",
        confidence="medium",
        depends_on_raw_old36=True,
    ),
    _r(
        key="event_selection_tail",
        description="Event = row where |signal| >= q_q(|signal|) computed on "
        "the quality-filtered block (session,asset).",
        status=RuleStatus.INFERRED_PENDING,
        evidence=[
            "All 24H threshold values observed in CSV are positive, consistent "
            "with quantiles of |signal| on symmetric signed signals",
            "Report describes q80/q90/q95 tail selection",
        ],
        inferred_hypothesis="threshold = numpy.quantile(|signal|, q); event iff |signal| >= threshold.",
        confidence="medium",
        depends_on_raw_old36=True,
    ),
    _r(
        key="overlap_spacing",
        description="Minimum spacing between accepted events = max(1s, horizon).",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=["Handoff §12 states the spacing rule"],
        confidence="high",
    ),
    _r(
        key="overlap_ordering",
        description="Algorithm to pick which of two overlapping candidates to keep.",
        status=RuleStatus.MISSING,
        evidence=[],
        inferred_hypothesis="greedy earliest-first pass along the grid.",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="tie_handling",
        description="How exact-quantile-boundary values are counted (>= vs >).",
        status=RuleStatus.MISSING,
        evidence=[],
        inferred_hypothesis="|signal| >= threshold (inclusive).",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="nan_handling",
        description="How NaN forward returns / NaN signals are handled.",
        status=RuleStatus.MISSING,
        evidence=[],
        inferred_hypothesis="drop rows with NaN signal or NaN forward mid.",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="session_end_truncation",
        description="Policy for the tail max_horizon of each session/asset.",
        status=RuleStatus.MISSING,
        evidence=[],
        inferred_hypothesis="drop rows where t+H > session end for each horizon independently.",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="positive_block_rule",
        description="Block counts as positive iff mean_signed_bps > 0.",
        status=RuleStatus.MATH_UNIQUE,
        evidence=[
            "Aggregates CSVs report positive_blocks; block-level CSVs allow "
            "verifying that this is the count of blocks where mean_signed_bps > 0",
        ],
        confidence="high",
    ),
    _r(
        key="composite_formula_depthBoth",
        description="Formula and normalization of the depthBoth composite.",
        status=RuleStatus.AMBIGUOUS,
        evidence=[
            "CP24 threshold values for depthBoth differ from either component; "
            "consistent with a derived-signal quantile (e.g. mean of two normalized components) "
            "but multiple normalizations satisfy this.",
        ],
        inferred_hypothesis="depthBoth = (rank(depth_imbalance_l1) + rank(depth_imbalance_l5)) / 2, "
        "then threshold on |depthBoth|. Not selected without golden regression.",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="composite_formula_gap_star",
        description="Formula of every gap_* composite (localTrade, extOFI, depth_extOFI, "
        "depthL1, depthBoth, leader1000, localOFI).",
        status=RuleStatus.AMBIGUOUS,
        evidence=[
            "CP24 CSV thresholds for every gap_* composite match the "
            "fair_gap_reversion threshold exactly for the same block/horizon/q. "
            "This proves the gap_* composites gate on the fair-gap quantile. "
            "The auxiliary condition (sign agreement vs magnitude threshold on "
            "the second component) is not determined by the artifacts.",
        ],
        inferred_hypothesis="Event iff fair-gap quantile crossed AND "
        "sign of second component agrees with -sign(gap).",
        confidence="medium",
        depends_on_raw_old36=True,
    ),
    _r(
        key="composite_formula_leader1000_extOFI",
        description="Formula of the leader1000_extOFI composite.",
        status=RuleStatus.AMBIGUOUS,
        evidence=[
            "CP24 CSV thresholds match leader_gap_1000ms threshold exactly; "
            "composite gates on leader1000 quantile. Combination with extOFI "
            "remains underspecified by artifacts.",
        ],
        inferred_hypothesis="Event iff leader1000 quantile crossed AND "
        "sign(extOFI) agrees with sign(leader_gap).",
        confidence="medium",
        depends_on_raw_old36=True,
    ),
    _r(
        key="composite_weights",
        description="Component weights inside every composite.",
        status=RuleStatus.MISSING,
        evidence=["No weights file present in either checkpoint ZIP"],
        inferred_hypothesis="equal weights.",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="component_normalization",
        description="Per-component normalization used before composite formation.",
        status=RuleStatus.MISSING,
        evidence=[],
        inferred_hypothesis="per-block rank-uniform transform.",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="alignment_filter",
        description="Definition of the alignment filter used inside gap_* composites.",
        status=RuleStatus.MISSING,
        evidence=[
            "Collector exposes bitget_fair_ofi_alignment and bitget_fair_trade_alignment "
            "(both = sign(-gap)*sign(local_signal)). Whether composites use these "
            "binary flags or thresholded magnitudes is not stated.",
        ],
        inferred_hypothesis="Use the collector-provided alignment binary; require ==+1.",
        confidence="low",
        depends_on_raw_old36=True,
    ),
    _r(
        key="block_aggregation_weight",
        description="How per-block mean_signed_bps aggregate up to feature-level headline numbers.",
        status=RuleStatus.MATH_UNIQUE,
        evidence=[
            "Aggregate mean_signed_bps in simple_aggregates_24h.csv equals the "
            "pooled mean if reproduced from per-event returns, or the "
            "unweighted mean of per-block means. Which one applies is decidable "
            "once the raw grids are available.",
        ],
        inferred_hypothesis="unweighted mean of per-block mean_signed_bps.",
        confidence="medium",
        depends_on_raw_old36=True,
    ),
    _r(
        key="btc_eth_aggregation",
        description="BTC/ETH block combination inside feature-level aggregates.",
        status=RuleStatus.MATH_UNIQUE,
        evidence=[
            "Aggregate row count `blocks` includes both BTC and ETH; "
            "BTC and ETH appear as independent (session,asset) blocks.",
        ],
        inferred_hypothesis="BTC and ETH are treated as independent blocks; no cross-asset weighting.",
        confidence="high",
        depends_on_raw_old36=True,
    ),
    _r(
        key="dispersion_tertile_boundaries",
        description="Boundaries used to bucket rows into low/mid/high external "
        "dispersion tertiles.",
        status=RuleStatus.AMBIGUOUS,
        evidence=[
            "Per-block disp_lo and disp_hi columns are exposed in fair-gap "
            "dispersion CSVs; whether they are computed on the quality-filtered "
            "grid, event-only rows, or across the whole session is not stated.",
        ],
        inferred_hypothesis="per-block tertiles of external_perp_dispersion_bps "
        "on the quality-filtered grid rows.",
        confidence="medium",
        depends_on_raw_old36=True,
    ),
    _r(
        key="economic_hurdle_bps",
        description="Standalone directional promotion hurdle = 15 bps.",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=["Handoff §12; both CP24 and CP36 reports"],
        confidence="high",
    ),
    _r(
        key="horizon_domain_split",
        description="100ms–5s = execution-timing; 10s–30s = markout/adverse-selection.",
        status=RuleStatus.EXPLICIT_EVIDENCE,
        evidence=["CP24 and CP36 reports state this split verbatim"],
        confidence="high",
    ),
]


def status_summary() -> dict[str, int]:
    out: dict[str, int] = {s.value: 0 for s in RuleStatus}
    for r in RULES:
        out[r.status.value] += 1
    return out


__all__ = ["Rule", "RuleStatus", "RULES", "status_summary"]
