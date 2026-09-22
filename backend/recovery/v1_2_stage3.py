"""RECONSTRUCTION_V1.2 Stage 3 candidate-only diagnostics.

Stage 3 emits exactly 576 rows across 4 diagnostic axes:
  HZ (288): threshold discrimination analysis for 12 simple features
  HG  (72): depthL1_extOFI composite structural variants G0 / G1
  HC  (72): gap_depth_extOFI composite structural variants C0 / C1
  HB (144): baseline overlap boundary comparison B0 / B1

Spec:   V1.2_STAGE3_FINAL
Audit:  Claude final Stage3 audit, BLK-1..BLK-4 incorporated.

Safety invariants (NEVER REMOVE):
- No import-time execution.
- No modification of engine.py, Stage1, Stage2, or any frozen V1.1 artifact.
- No golden artifact reads / imports / paths anywhere in this module (I21).
- RAW access in production path restricted to declared OLD36 sessions via sandbox.
- No DB / RAW writes.
- NEW36 remains rejected by the existing quantitative firewall.
- FrozenAnalysisEngine remains NOT_CONFIGURED / accepts_input=False.
- No real Stage3 execution until explicitly authorised.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .engine import (
    FEATURE_MAP,
    GRID_MS,
    SIMPLE_FEATURES,
    _build_forward_return_array,
    _greedy_overlap_filter,
    _quantile_type7,
)
from .v1_2_diagnostics import (
    _BlockContext,
    fingerprint_positions,
)


# ---------------------------------------------------------------------------
# Stage 3 constants (frozen)
# ---------------------------------------------------------------------------

STAGE3_VERSION: str = "V1.2_STAGE3_FINAL"

STAGE3_SESSIONS: tuple[str, ...] = (
    "20260905T073818Z_e44d99bd",
    "20260906T221530Z_db18dc51",
)
STAGE3_ASSETS: tuple[str, ...] = ("BTC", "ETH")

# Exact 12 HZ simple features (frozen)
STAGE3_HZ_FEATURES: tuple[str, ...] = (
    "bitget_ofi",
    "bitget_trade_flow",
    "depth_imbalance_l1",
    "depth_imbalance_l5",
    "external_ofi",
    "external_trade_flow",
    "fair_accel_100ms",
    "fair_gap_reversion",
    "leader_gap_100ms",
    "leader_gap_200ms",
    "leader_gap_500ms",
    "leader_gap_1000ms",
)

STAGE3_QUANTILES: tuple[float, ...] = (0.80, 0.90, 0.95)
STAGE3_HORIZONS: tuple[int, ...] = (1000, 5000, 30000)

STAGE3_HZ_VARIANTS: tuple[str, ...] = ("T0", "TZ")
STAGE3_HG_VARIANTS: tuple[str, ...] = ("G0", "G1")
STAGE3_HC_VARIANTS: tuple[str, ...] = ("C0", "C1")
STAGE3_HB_VARIANTS: tuple[str, ...] = ("B0", "B1")

# HB operates at q=0.90 only
STAGE3_HB_Q: float = 0.90

STAGE3_HB_FAMILIES: tuple[str, ...] = (
    "depth_imbalance_l1",
    "gap_depthL1",
    "gap_localOFI",
    "bitget_ofi",
    "depthL1_extOFI",
    "gap_depth_extOFI",
)

# Expected row counts per axis
STAGE3_HZ_ROWS: int = 288   # 2 sessions x 2 assets x 12 features x 2 variants x 3 q
STAGE3_HG_ROWS: int = 72    # 2 x 2 x 2 variants x 3 q x 3 horizons
STAGE3_HC_ROWS: int = 72    # 2 x 2 x 2 variants x 3 q x 3 horizons
STAGE3_HB_ROWS: int = 144   # 2 x 2 x 6 families x 2 variants x 3 horizons
STAGE3_TOTAL_ROWS: int = STAGE3_HZ_ROWS + STAGE3_HG_ROWS + STAGE3_HC_ROWS + STAGE3_HB_ROWS

# Reference horizon for HZ-T0 drift guard (threshold is horizon-independent)
_HZ_DRIFT_REFERENCE_HORIZON: int = 1000

# Protected artifact names Stage 3 must never overwrite.
_PROTECTED_NAMES: frozenset[str] = frozenset({
    "golden_regression_summary.csv",
    "golden_regression_failures.csv",
    "recovery_report.md",
    "recovery_provenance.json",
    "recovery_rules.json",
    "stage1_candidate_diagnostics.csv",
    "stage1_manifest.json",
    "stage2_overlap_diagnostics.csv",
    "stage2_manifest.json",
})

# Fixed column order for Stage 3 CSV output (25 columns, deterministic).
_STAGE3_CSV_COLUMNS: tuple[str, ...] = (
    "diagnostic_version",
    "session_id",
    "asset",
    "axis",
    "feature_family",
    "variant_id",
    "quantile",
    "horizon_ms",
    "threshold_domain_finite_n",
    "threshold_domain_nonzero_n",
    "zero_n",
    "zero_fraction",
    "nonzero_unique_value_n",
    "hz_discrimination_class",
    "gate_zero_n",
    "calculated_threshold",
    "spacing_steps",
    "pre_overlap_n",
    "exact_spacing_pair_n",
    "accepted_n",
    "overlap_dropped_n",
    "accepted_positions_sha256",
    "mean_signed_bps",
    "hit_rate",
    "mean_abs_move",
)

# Frozen default output directory for Stage 3 artifacts.
_STAGE3_DEFAULT_OUTPUT_DIR: Path = (
    Path(__file__).resolve().parent / "reports" / "v1_2"
)

# Fingerprint encoding definition (matches Stage1/Stage2 exactly).
_FINGERPRINT_ENCODING_DEFINITION: str = (
    "SHA256 of accepted positions sorted ascending, encoded as raw "
    "concatenated 8-byte little-endian signed int64, no delimiter; "
    "empty set = SHA256 of zero-length byte string; lowercase hex digest"
)

# HZ classification labels (frozen).
_ZERO_FREE_CONTROL = "ZERO_FREE_CONTROL"
_HZ_DISCRIMINATING = "HZ_DISCRIMINATING"
_HZ_NONDISCRIMINATING = "HZ_NONDISCRIMINATING_EQUAL_THRESHOLD"

# Float comparison tolerance for drift guard.
_DRIFT_TOL: float = 1e-12


# ---------------------------------------------------------------------------
# Feature-set validation (runtime, not import-time)
# ---------------------------------------------------------------------------

def validate_hz_feature_set() -> None:
    """Verify STAGE3_HZ_FEATURES matches frozen engine FEATURE_MAP / SIMPLE_FEATURES.

    Called once at the start of generate_stage3_rows().
    Aborts if the set has drifted.
    """
    engine_features = set(SIMPLE_FEATURES)
    spec_features = set(STAGE3_HZ_FEATURES)
    if spec_features != engine_features:
        extra = spec_features - engine_features
        missing = engine_features - spec_features
        raise AssertionError(
            f"STAGE3 HZ FEATURE SET DRIFT: "
            f"extra={sorted(extra)!r} missing={sorted(missing)!r}"
        )
    if len(STAGE3_HZ_FEATURES) != 12:
        raise AssertionError(
            f"STAGE3 HZ FEATURE SET expected 12 features, got {len(STAGE3_HZ_FEATURES)}"
        )


# ---------------------------------------------------------------------------
# Fingerprint helper (exact Stage1/Stage2 encoding)
# ---------------------------------------------------------------------------

def _sha256_positions(positions: np.ndarray) -> str:
    """SHA256 of positions as ascending signed little-endian int64 bytes.

    Empty set: SHA256 of zero-length byte string.
    """
    arr = np.asarray(positions, dtype=np.int64)
    if len(arr) > 0:
        arr = np.sort(arr)
    le = arr.astype("<i8", copy=False)
    return hashlib.sha256(le.tobytes(order="C")).hexdigest()


# ---------------------------------------------------------------------------
# Exact spacing pair count (O(N) set-based)
# ---------------------------------------------------------------------------

def exact_spacing_pair_n(positions: np.ndarray, spacing: int) -> int:
    """Count unordered pairs (i, j), i < j, where positions[j]-positions[i]==spacing.

    Uses set membership O(N):
        count = sum(1 for p in positions if p + spacing in position_set)
    """
    arr = np.asarray(positions, dtype=np.int64)
    if len(arr) < 2:
        return 0
    pos_set = set(int(p) for p in arr)
    return sum(1 for p in arr if (int(p) + spacing) in pos_set)


# ---------------------------------------------------------------------------
# B1 strict overlap filter (distance > spacing_steps)
# ---------------------------------------------------------------------------

def _greedy_overlap_filter_strict(positions: np.ndarray, spacing: int) -> np.ndarray:
    """Greedy earliest-first overlap filter with STRICT boundary: distance > spacing.

    B1 variant: accept iff pos - last_accepted_pos > spacing_steps.
    """
    n = len(positions)
    accepted = np.zeros(n, dtype=bool)
    last_pos = -(spacing + 1)  # sentinel: first candidate always accepted
    for i in range(n):
        pos = int(positions[i])
        if pos - last_pos > spacing:
            accepted[i] = True
            last_pos = pos
    return accepted


# ---------------------------------------------------------------------------
# Event metrics helper
# ---------------------------------------------------------------------------

def _compute_event_metrics(
    directions: np.ndarray,
    returns: np.ndarray,
    accepted_mask: np.ndarray,
) -> dict:
    """Compute mean_signed_bps, hit_rate, mean_abs_move from accepted events."""
    n_accepted = int(np.sum(accepted_mask))
    if n_accepted == 0:
        return {"mean_signed_bps": None, "hit_rate": None, "mean_abs_move": None}
    dirs = directions[accepted_mask]
    rets = returns[accepted_mask]
    signed = dirs * rets
    return {
        "mean_signed_bps": float(np.mean(signed)),
        "hit_rate": float(np.mean(signed > 0.0)),
        "mean_abs_move": float(np.mean(np.abs(rets))),
    }


# ---------------------------------------------------------------------------
# CSV field formatter
# ---------------------------------------------------------------------------

def _format_field(v: Any) -> str:
    """Format a row field for CSV output.

    - None   -> empty string
    - float  -> repr(float)
    - int    -> decimal string
    - str    -> as-is
    """
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bool):  # bool is subclass of int; handle before int
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    return str(v)


# ---------------------------------------------------------------------------
# CSV serializer (fixed schema, Python csv module)
# ---------------------------------------------------------------------------

def _csv_bytes(rows: list[dict]) -> bytes:
    """Serialize rows to UTF-8 CSV bytes.

    Fixed 25-column schema, lineterminator='\n'.
    Integer fields: decimal integer strings.
    Float fields: repr(float).
    None: empty CSV field.
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(_STAGE3_CSV_COLUMNS))
    for row in rows:
        writer.writerow([_format_field(row.get(col)) for col in _STAGE3_CSV_COLUMNS])
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Position-subset assertion helper (I19 / I20)
# ---------------------------------------------------------------------------

def _assert_position_subset(
    child: np.ndarray,
    parent: np.ndarray,
    invariant_name: str,
) -> None:
    """Assert every position in `child` is present in `parent` (fail-closed).

    Uses integer set membership.  Child and parent are each deduplicated
    before comparison so duplicates cannot mask violations.

    Raises AssertionError whose message contains `invariant_name` on any
    violation, e.g.:
        "I19 FAIL: 2 child position(s) not in parent set. Examples: [42, 77]"
    """
    if len(child) == 0:
        return  # empty child is trivially a subset
    parent_set = {int(p) for p in parent}
    violations = sorted({int(p) for p in child} - parent_set)
    if violations:
        n_viol = len(violations)
        examples = violations[:5]
        raise AssertionError(
            f"{invariant_name} FAIL: {n_viol} child position(s) not in parent set. "
            f"Examples: {examples!r}"
        )


# ---------------------------------------------------------------------------
# Drift guard helpers (I17)
# ---------------------------------------------------------------------------

def _assert_hz_t0_drift_guard(
    ctx: _BlockContext,
    feature_family: str,
    q: float,
    computed_t0_threshold: "float | None",
) -> None:
    """Assert HZ-T0 threshold equals frozen simple feature threshold (I17).

    Uses reference horizon _HZ_DRIFT_REFERENCE_HORIZON to look up ctx.metrics.
    Threshold is horizon-independent for all simple features.

    Fail-closed: aborts if ctx.metrics is empty or key is missing.
    """
    if not ctx.metrics:
        raise AssertionError(
            f"I17 HZ-T0 DRIFT: ctx.metrics is absent or empty; "
            f"frozen metric required for feature={feature_family!r} q={q}"
        )
    key = (feature_family, _HZ_DRIFT_REFERENCE_HORIZON, q)
    if key not in ctx.metrics:
        raise AssertionError(
            f"I17 HZ-T0 DRIFT: missing metric key={key!r}"
        )
    m = ctx.metrics[key]
    if not hasattr(m, "threshold"):
        raise AssertionError(
            f"I17 HZ-T0 DRIFT: metric missing 'threshold' attribute for key={key!r}"
        )
    expected_thr = m.threshold
    actual_thr = computed_t0_threshold
    # Exact float equality including None==None
    if expected_thr is None and actual_thr is None:
        return
    if expected_thr is None or actual_thr is None:
        raise AssertionError(
            f"I17 HZ-T0 DRIFT: threshold expected={expected_thr!r} got={actual_thr!r} "
            f"for feature={feature_family!r} q={q}"
        )
    if expected_thr != actual_thr:
        raise AssertionError(
            f"I17 HZ-T0 DRIFT: threshold expected={expected_thr!r} got={actual_thr!r} "
            f"for feature={feature_family!r} q={q}"
        )


def _assert_event_drift_guard(
    ctx: _BlockContext,
    feature_key: str,
    horizon_ms: int,
    q: float,
    actual_N: int,
    actual_thresh: "float | None",
    actual_mean: "float | None",
    actual_hr: "float | None",
    label: str = "",
) -> None:
    """Assert N, threshold, mean_signed_bps, hit_rate match frozen engine metric (I17).

    Used for G0, C0, HB-B0.
    mean_abs_move is NOT compared (per spec).
    Tolerance for mean/hit: absolute <= 1e-12.
    Fail-closed on missing ctx.metrics or key.
    """
    if not ctx.metrics:
        raise AssertionError(
            f"I17 {label} DRIFT: ctx.metrics is absent or empty; "
            f"key=({feature_key!r}, {horizon_ms}, {q})"
        )
    key = (feature_key, horizon_ms, q)
    if key not in ctx.metrics:
        raise AssertionError(
            f"I17 {label} DRIFT: missing metric key={key!r}"
        )
    m = ctx.metrics[key]
    for attr in ("N", "threshold", "mean_signed_bps", "hit_rate"):
        if not hasattr(m, attr):
            raise AssertionError(
                f"I17 {label} DRIFT: metric missing attribute '{attr}' for key={key!r}"
            )
    # N: exact integer
    if actual_N != m.N:
        raise AssertionError(
            f"I17 {label} DRIFT: N expected={m.N} got={actual_N}"
        )
    # Threshold: exact equality (or None==None)
    e_thr, a_thr = m.threshold, actual_thresh
    if e_thr is None and a_thr is None:
        pass
    elif e_thr is None or a_thr is None:
        raise AssertionError(
            f"I17 {label} DRIFT: threshold expected={e_thr!r} got={a_thr!r}"
        )
    elif not math.isfinite(e_thr):
        raise AssertionError(f"I17 {label} DRIFT: frozen threshold non-finite: {e_thr!r}")
    elif not math.isfinite(a_thr):
        raise AssertionError(f"I17 {label} DRIFT: actual threshold non-finite: {a_thr!r}")
    elif e_thr != a_thr:
        raise AssertionError(
            f"I17 {label} DRIFT: threshold expected={e_thr!r} got={a_thr!r}"
        )
    # mean_signed_bps: None==None OK, else finite + tolerance
    e_msb, a_msb = m.mean_signed_bps, actual_mean
    if e_msb is None and a_msb is None:
        pass
    elif e_msb is None or a_msb is None:
        raise AssertionError(
            f"I17 {label} DRIFT: mean_signed_bps expected={e_msb!r} got={a_msb!r}"
        )
    elif not math.isfinite(e_msb):
        raise AssertionError(f"I17 {label} DRIFT: frozen mean_signed_bps non-finite: {e_msb!r}")
    elif not math.isfinite(a_msb):
        raise AssertionError(f"I17 {label} DRIFT: actual mean_signed_bps non-finite: {a_msb!r}")
    elif abs(a_msb - e_msb) > _DRIFT_TOL:
        raise AssertionError(
            f"I17 {label} DRIFT: mean_signed_bps expected={e_msb!r} got={a_msb!r}"
        )
    # hit_rate: same
    e_hr, a_hr = m.hit_rate, actual_hr
    if e_hr is None and a_hr is None:
        pass
    elif e_hr is None or a_hr is None:
        raise AssertionError(
            f"I17 {label} DRIFT: hit_rate expected={e_hr!r} got={a_hr!r}"
        )
    elif not math.isfinite(e_hr):
        raise AssertionError(f"I17 {label} DRIFT: frozen hit_rate non-finite: {e_hr!r}")
    elif not math.isfinite(a_hr):
        raise AssertionError(f"I17 {label} DRIFT: actual hit_rate non-finite: {a_hr!r}")
    elif abs(a_hr - e_hr) > _DRIFT_TOL:
        raise AssertionError(
            f"I17 {label} DRIFT: hit_rate expected={e_hr!r} got={a_hr!r}"
        )


# ---------------------------------------------------------------------------
# HZ axis row generator per block
# ---------------------------------------------------------------------------

def _hz_block(
    ctx: _BlockContext,
    session_id: str,
    asset: str,
) -> "tuple[list[dict], dict[tuple[str, float], tuple[float | None, float | None]]]":
    """Generate HZ rows for one (session_id, asset) block.

    Returns (rows, hz_cache) where hz_cache maps (feature_family, q) ->
    (T0_threshold, TZ_threshold).
    """
    rows: list[dict] = []
    # hz_cache: (feature_family, q) -> (T0_threshold, TZ_threshold)
    hz_cache: dict[tuple[str, float], tuple[float | None, float | None]] = {}

    for feature in STAGE3_HZ_FEATURES:
        col_name = FEATURE_MAP[feature]
        signal = ctx.columns.get(col_name, np.full(len(ctx.gpos), np.nan, dtype=float))
        signal_f = signal.astype(float)

        for q in STAGE3_QUANTILES:
            # T0 domain: quality & finite(signal)
            t0_domain = ctx.quality & np.isfinite(signal_f)
            finite_n = int(np.sum(t0_domain))

            # TZ domain: quality & finite(signal) & signal != 0
            tz_domain = t0_domain & (signal_f != 0.0)
            nonzero_n = int(np.sum(tz_domain))

            zero_n = finite_n - nonzero_n
            zero_fraction: "float | None"
            if finite_n > 0:
                zero_fraction = float(zero_n / finite_n)
            else:
                zero_fraction = None

            # nonzero_unique_value_n: distinct abs(signal) values in TZ domain
            if nonzero_n > 0:
                nonzero_unique_value_n = len(
                    set(float(v) for v in np.abs(signal_f[tz_domain]))
                )
            else:
                nonzero_unique_value_n = 0

            # Thresholds
            t0_thresh = _quantile_type7(np.abs(signal_f[t0_domain]), q) if finite_n >= 2 else None
            tz_thresh = _quantile_type7(np.abs(signal_f[tz_domain]), q) if nonzero_n >= 2 else None

            # HZ discrimination classification
            if zero_n == 0:
                hz_class = _ZERO_FREE_CONTROL
            elif t0_thresh != tz_thresh:  # exact Python float equality; None==None is True
                hz_class = _HZ_DISCRIMINATING
            else:
                hz_class = _HZ_NONDISCRIMINATING

            # Cache for I18 / G1 threshold lookup
            hz_cache[(feature, q)] = (t0_thresh, tz_thresh)

            # I17: HZ-T0 threshold drift guard
            _assert_hz_t0_drift_guard(ctx, feature, q, t0_thresh)

            # HZ common fields (shared by T0 and TZ rows)
            _common: dict = {
                "diagnostic_version": STAGE3_VERSION,
                "session_id": session_id,
                "asset": asset,
                "axis": "HZ",
                "feature_family": feature,
                "quantile": q,
                "horizon_ms": None,
                "threshold_domain_finite_n": finite_n,
                "threshold_domain_nonzero_n": nonzero_n,
                "zero_n": zero_n,
                "zero_fraction": zero_fraction,
                "nonzero_unique_value_n": nonzero_unique_value_n,
                "hz_discrimination_class": hz_class,
                "gate_zero_n": None,
                "spacing_steps": None,
                "pre_overlap_n": None,
                "exact_spacing_pair_n": None,
                "accepted_n": None,
                "overlap_dropped_n": None,
                "accepted_positions_sha256": None,
                "mean_signed_bps": None,
                "hit_rate": None,
                "mean_abs_move": None,
            }

            # T0 row
            rows.append({
                **_common,
                "variant_id": "T0",
                "calculated_threshold": t0_thresh,
            })
            # TZ row
            rows.append({
                **_common,
                "variant_id": "TZ",
                "calculated_threshold": tz_thresh,
            })

    return rows, hz_cache


# ---------------------------------------------------------------------------
# HG axis row generator per block
# ---------------------------------------------------------------------------

def _hg_block(
    ctx: _BlockContext,
    session_id: str,
    asset: str,
    hz_cache: "dict[tuple[str, float], tuple[float | None, float | None]]",
) -> list[dict]:
    """Generate HG rows for one (session_id, asset) block.

    G0: frozen depthL1_extOFI V1.1 semantics (drift guard I17).
    G1: depth_imbalance_l1 gate, ext_ofi confirmation (I18, I19).
    """
    rows: list[dict] = []
    di_l1 = ctx.columns.get(
        "bitget_depth_imbalance_l1",
        np.full(len(ctx.gpos), np.nan, dtype=float)
    ).astype(float)
    ext_ofi = ctx.columns.get(
        "external_ofi_consensus_l1",
        np.full(len(ctx.gpos), np.nan, dtype=float)
    ).astype(float)

    for q in STAGE3_QUANTILES:
        for horizon_ms in STAGE3_HORIZONS:
            k = horizon_ms // GRID_MS
            spacing = max(10, k)
            fwd = _build_forward_return_array(ctx.mid, k)

            # ── G0: frozen depthL1_extOFI ──────────────────────────────────
            derived_domain = ctx.quality & np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi)
            D = np.where(derived_domain, (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0, np.nan)
            g0_thresh = _quantile_type7(np.abs(D[derived_domain]), q)

            if g0_thresh is None:
                g0_pre_mask = np.zeros(len(ctx.gpos), dtype=bool)
            else:
                g0_pre_mask = (
                    derived_domain
                    & (D != 0.0)
                    & (np.abs(D) >= g0_thresh)
                    & np.isfinite(fwd)
                )

            g0_positions = ctx.gpos[g0_pre_mask]
            g0_directions = np.sign(D[g0_pre_mask])
            g0_returns = fwd[g0_pre_mask]
            g0_accepted_mask = (
                _greedy_overlap_filter(g0_positions, spacing)
                if len(g0_positions) > 0
                else np.zeros(0, dtype=bool)
            )
            g0_accepted_n = int(np.sum(g0_accepted_mask))
            g0_pre_n = len(g0_positions)
            g0_accepted_pos = g0_positions[g0_accepted_mask]
            g0_fp = _sha256_positions(g0_accepted_pos)
            g0_metrics = _compute_event_metrics(g0_directions, g0_returns, g0_accepted_mask)

            # I17: G0 drift guard
            _assert_event_drift_guard(
                ctx, "depthL1_extOFI", horizon_ms, q,
                g0_accepted_n, g0_thresh,
                g0_metrics["mean_signed_bps"], g0_metrics["hit_rate"],
                label="G0",
            )

            rows.append({
                "diagnostic_version": STAGE3_VERSION,
                "session_id": session_id,
                "asset": asset,
                "axis": "HG",
                "feature_family": "depthL1_extOFI",
                "variant_id": "G0",
                "quantile": q,
                "horizon_ms": horizon_ms,
                "threshold_domain_finite_n": None,
                "threshold_domain_nonzero_n": None,
                "zero_n": None,
                "zero_fraction": None,
                "nonzero_unique_value_n": None,
                "hz_discrimination_class": None,
                "gate_zero_n": None,
                "calculated_threshold": g0_thresh,
                "spacing_steps": spacing,
                "pre_overlap_n": g0_pre_n,
                "exact_spacing_pair_n": None,
                "accepted_n": g0_accepted_n,
                "overlap_dropped_n": g0_pre_n - g0_accepted_n,
                "accepted_positions_sha256": g0_fp,
                "mean_signed_bps": g0_metrics["mean_signed_bps"],
                "hit_rate": g0_metrics["hit_rate"],
                "mean_abs_move": g0_metrics["mean_abs_move"],
            })

            # ── G1: depth_imbalance_l1 gate + ext_ofi confirmation ─────────
            # I18: threshold identity — G1 threshold == HZ-T0 threshold for
            #      depth_imbalance_l1 at same q (by construction from hz_cache).
            # I19: explicit runtime pre-overlap subset assertion — G1 positions
            #      must ⊆ depth_l1 baseline pre-overlap positions (enforced
            #      below after constructing g1_positions; fail-closed).
            hz_t0_di_l1, _ = hz_cache.get(("depth_imbalance_l1", q), (None, None))
            g1_thresh = hz_t0_di_l1

            # gate_zero_n: quality & finite(di_l1) & di_l1 == 0
            gate_domain = ctx.quality & np.isfinite(di_l1)
            gate_zero_n = int(np.sum(gate_domain & (di_l1 == 0.0)))

            if g1_thresh is None:
                g1_pre_mask = np.zeros(len(ctx.gpos), dtype=bool)
            else:
                g1_pre_mask = (
                    gate_domain
                    & (di_l1 != 0.0)
                    & (np.abs(di_l1) >= g1_thresh)
                    & np.isfinite(ext_ofi)
                    & (ext_ofi != 0.0)
                    & (np.sign(ext_ofi) == np.sign(di_l1))
                    & np.isfinite(fwd)
                )

            g1_positions = ctx.gpos[g1_pre_mask]
            # I19 runtime assertion: G1 pre-overlap positions ⊆ depth_l1 baseline
            # pre-overlap positions (fail-closed, explicit).
            # Parent: gate_domain & di_l1!=0 & |di_l1|>=g1_thresh & finite(fwd).
            _i19_parent_mask = (
                gate_domain & (di_l1 != 0.0) & (np.abs(di_l1) >= g1_thresh) & np.isfinite(fwd)
            ) if g1_thresh is not None else np.zeros(len(ctx.gpos), dtype=bool)
            _assert_position_subset(g1_positions, ctx.gpos[_i19_parent_mask], "I19")
            g1_directions = np.sign(di_l1[g1_pre_mask])
            g1_returns = fwd[g1_pre_mask]
            g1_accepted_mask = (
                _greedy_overlap_filter(g1_positions, spacing)
                if len(g1_positions) > 0
                else np.zeros(0, dtype=bool)
            )
            g1_accepted_n = int(np.sum(g1_accepted_mask))
            g1_pre_n = len(g1_positions)
            g1_accepted_pos = g1_positions[g1_accepted_mask]
            g1_fp = _sha256_positions(g1_accepted_pos)
            g1_metrics = _compute_event_metrics(g1_directions, g1_returns, g1_accepted_mask)

            rows.append({
                "diagnostic_version": STAGE3_VERSION,
                "session_id": session_id,
                "asset": asset,
                "axis": "HG",
                "feature_family": "depthL1_extOFI",
                "variant_id": "G1",
                "quantile": q,
                "horizon_ms": horizon_ms,
                "threshold_domain_finite_n": None,
                "threshold_domain_nonzero_n": None,
                "zero_n": None,
                "zero_fraction": None,
                "nonzero_unique_value_n": None,
                "hz_discrimination_class": None,
                "gate_zero_n": gate_zero_n,
                "calculated_threshold": g1_thresh,
                "spacing_steps": spacing,
                "pre_overlap_n": g1_pre_n,
                "exact_spacing_pair_n": None,
                "accepted_n": g1_accepted_n,
                "overlap_dropped_n": g1_pre_n - g1_accepted_n,
                "accepted_positions_sha256": g1_fp,
                "mean_signed_bps": g1_metrics["mean_signed_bps"],
                "hit_rate": g1_metrics["hit_rate"],
                "mean_abs_move": g1_metrics["mean_abs_move"],
            })

    return rows


# ---------------------------------------------------------------------------
# HC axis row generator per block
# ---------------------------------------------------------------------------

def _hc_block(
    ctx: _BlockContext,
    session_id: str,
    asset: str,
) -> list[dict]:
    """Generate HC rows for one (session_id, asset) block.

    C0: frozen gap_depth_extOFI V1.1 semantics (drift guard I17).
    C1: RAW-sign conjunction (I20 subset invariant).
    """
    rows: list[dict] = []
    gap = ctx.columns.get(
        "bitget_gap_to_fair_bps",
        np.full(len(ctx.gpos), np.nan, dtype=float)
    ).astype(float)
    di_l1 = ctx.columns.get(
        "bitget_depth_imbalance_l1",
        np.full(len(ctx.gpos), np.nan, dtype=float)
    ).astype(float)
    ext_ofi = ctx.columns.get(
        "external_ofi_consensus_l1",
        np.full(len(ctx.gpos), np.nan, dtype=float)
    ).astype(float)

    # Gap threshold domain (quality & finite(gap)); threshold is q-dependent.
    gap_thresh_domain = ctx.quality & np.isfinite(gap)

    for q in STAGE3_QUANTILES:
        gap_thresh = _quantile_type7(np.abs(gap[gap_thresh_domain]), q)

        for horizon_ms in STAGE3_HORIZONS:
            k = horizon_ms // GRID_MS
            spacing = max(10, k)
            fwd = _build_forward_return_array(ctx.mid, k)

            # ── C0: frozen gap_depth_extOFI (z-sign alignment) ────────────
            D_ext = np.where(
                np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi),
                (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0,
                np.nan,
            )
            conf_fin_c0 = np.isfinite(D_ext)
            c0_alignment = (
                (np.sign(D_ext) == -np.sign(gap))
                & (D_ext != 0.0)
                & conf_fin_c0
            )
            gap_event_domain_c0 = gap_thresh_domain & conf_fin_c0

            if gap_thresh is None:
                c0_pre_mask = np.zeros(len(ctx.gpos), dtype=bool)
            else:
                c0_pre_mask = (
                    gap_event_domain_c0
                    & (gap != 0.0)
                    & (np.abs(gap) >= gap_thresh)
                    & c0_alignment
                    & np.isfinite(fwd)
                )

            c0_positions = ctx.gpos[c0_pre_mask]
            c0_directions = -np.sign(gap[c0_pre_mask])
            c0_returns = fwd[c0_pre_mask]
            c0_accepted_mask = (
                _greedy_overlap_filter(c0_positions, spacing)
                if len(c0_positions) > 0
                else np.zeros(0, dtype=bool)
            )
            c0_accepted_n = int(np.sum(c0_accepted_mask))
            c0_pre_n = len(c0_positions)
            c0_accepted_pos = c0_positions[c0_accepted_mask]
            c0_fp = _sha256_positions(c0_accepted_pos)
            c0_metrics = _compute_event_metrics(c0_directions, c0_returns, c0_accepted_mask)

            # I17: C0 drift guard
            _assert_event_drift_guard(
                ctx, "gap_depth_extOFI", horizon_ms, q,
                c0_accepted_n, gap_thresh,
                c0_metrics["mean_signed_bps"], c0_metrics["hit_rate"],
                label="C0",
            )

            rows.append({
                "diagnostic_version": STAGE3_VERSION,
                "session_id": session_id,
                "asset": asset,
                "axis": "HC",
                "feature_family": "gap_depth_extOFI",
                "variant_id": "C0",
                "quantile": q,
                "horizon_ms": horizon_ms,
                "threshold_domain_finite_n": None,
                "threshold_domain_nonzero_n": None,
                "zero_n": None,
                "zero_fraction": None,
                "nonzero_unique_value_n": None,
                "hz_discrimination_class": None,
                "gate_zero_n": None,
                "calculated_threshold": gap_thresh,
                "spacing_steps": spacing,
                "pre_overlap_n": c0_pre_n,
                "exact_spacing_pair_n": None,
                "accepted_n": c0_accepted_n,
                "overlap_dropped_n": c0_pre_n - c0_accepted_n,
                "accepted_positions_sha256": c0_fp,
                "mean_signed_bps": c0_metrics["mean_signed_bps"],
                "hit_rate": c0_metrics["hit_rate"],
                "mean_abs_move": c0_metrics["mean_abs_move"],
            })

            # ── C1: RAW-sign conjunction ────────────────────────────────────
            # I20: explicit runtime shared-gap-gate subset assertion — C1
            #      pre-overlap positions must ⊆ shared gap threshold-crossing
            #      positions (quality & finite(gap) & gap!=0 & |gap|>=gap_thresh
            #      & finite(fwd)).  C0 and C1 have different confirmation
            #      semantics; I20 is asserted against the shared gate, NOT
            #      against C0's confirmed pre-overlap set (enforced below;
            #      fail-closed).
            # conf_finite for C1: finite(di_l1) & finite(ext_ofi)
            conf_fin_c1 = np.isfinite(di_l1) & np.isfinite(ext_ofi)
            gap_event_domain_c1 = gap_thresh_domain & conf_fin_c1

            if gap_thresh is None:
                c1_pre_mask = np.zeros(len(ctx.gpos), dtype=bool)
            else:
                c1_pre_mask = (
                    gap_event_domain_c1
                    & (gap != 0.0)
                    & (np.abs(gap) >= gap_thresh)
                    & (di_l1 != 0.0)
                    & (np.sign(di_l1) == -np.sign(gap))
                    & (ext_ofi != 0.0)
                    & (np.sign(ext_ofi) == -np.sign(gap))
                    & np.isfinite(fwd)
                )

            c1_positions = ctx.gpos[c1_pre_mask]
            # I20 runtime assertion: C1 pre-overlap positions ⊆ shared gap
            # threshold-crossing set (fail-closed, explicit).
            # Parent: gap_thresh_domain & gap!=0 & |gap|>=gap_thresh & finite(fwd).
            _i20_parent_mask = (
                gap_thresh_domain & (gap != 0.0) & (np.abs(gap) >= gap_thresh) & np.isfinite(fwd)
            ) if gap_thresh is not None else np.zeros(len(ctx.gpos), dtype=bool)
            _assert_position_subset(c1_positions, ctx.gpos[_i20_parent_mask], "I20")
            c1_directions = -np.sign(gap[c1_pre_mask])
            c1_returns = fwd[c1_pre_mask]
            c1_accepted_mask = (
                _greedy_overlap_filter(c1_positions, spacing)
                if len(c1_positions) > 0
                else np.zeros(0, dtype=bool)
            )
            c1_accepted_n = int(np.sum(c1_accepted_mask))
            c1_pre_n = len(c1_positions)
            c1_accepted_pos = c1_positions[c1_accepted_mask]
            c1_fp = _sha256_positions(c1_accepted_pos)
            c1_metrics = _compute_event_metrics(c1_directions, c1_returns, c1_accepted_mask)

            rows.append({
                "diagnostic_version": STAGE3_VERSION,
                "session_id": session_id,
                "asset": asset,
                "axis": "HC",
                "feature_family": "gap_depth_extOFI",
                "variant_id": "C1",
                "quantile": q,
                "horizon_ms": horizon_ms,
                "threshold_domain_finite_n": None,
                "threshold_domain_nonzero_n": None,
                "zero_n": None,
                "zero_fraction": None,
                "nonzero_unique_value_n": None,
                "hz_discrimination_class": None,
                "gate_zero_n": None,
                "calculated_threshold": gap_thresh,
                "spacing_steps": spacing,
                "pre_overlap_n": c1_pre_n,
                "exact_spacing_pair_n": None,
                "accepted_n": c1_accepted_n,
                "overlap_dropped_n": c1_pre_n - c1_accepted_n,
                "accepted_positions_sha256": c1_fp,
                "mean_signed_bps": c1_metrics["mean_signed_bps"],
                "hit_rate": c1_metrics["hit_rate"],
                "mean_abs_move": c1_metrics["mean_abs_move"],
            })

    return rows


# ---------------------------------------------------------------------------
# HB axis pre-overlap extraction helper
# ---------------------------------------------------------------------------

def _hb_extract_pre_overlap(
    feature: str,
    ctx: _BlockContext,
    horizon_ms: int,
    q: float,
    fwd: np.ndarray,
) -> "tuple[np.ndarray, np.ndarray, np.ndarray, float | None]":
    """Extract (positions, directions, returns, threshold) for one HB case.

    Uses unchanged BASELINE pre-overlap candidate sets (same as frozen V1.1
    engine for each family).

    Returns (positions, directions, returns, threshold).
    All arrays are co-indexed and strictly ascending in positions.
    """
    k = horizon_ms // GRID_MS
    n_grid = len(ctx.gpos)

    if feature in ("bitget_ofi", "depth_imbalance_l1"):
        col_name = FEATURE_MAP[feature]
        signal = ctx.columns.get(
            col_name, np.full(n_grid, np.nan, dtype=float)
        ).astype(float)
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

    elif feature == "depthL1_extOFI":
        derived_domain = (
            ctx.quality & np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi)
        )
        D = np.where(
            derived_domain, (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0, np.nan
        )
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

    elif feature in ("gap_depthL1", "gap_localOFI", "gap_depth_extOFI"):
        gap = ctx.columns.get(
            "bitget_gap_to_fair_bps",
            np.full(n_grid, np.nan, dtype=float)
        ).astype(float)
        gap_thresh_domain = ctx.quality & np.isfinite(gap)
        threshold = _quantile_type7(np.abs(gap[gap_thresh_domain]), q)

        if feature == "gap_depthL1":
            di_l1 = ctx.columns.get(
                "bitget_depth_imbalance_l1",
                np.full(n_grid, np.nan, dtype=float)
            ).astype(float)
            conf_fin = np.isfinite(di_l1)
            alignment = (np.sign(di_l1) == -np.sign(gap)) & (di_l1 != 0.0)

        elif feature == "gap_localOFI":
            fair_ofi = ctx.columns.get(
                "bitget_fair_ofi_alignment",
                np.full(n_grid, np.nan, dtype=float)
            ).astype(float)
            conf_fin = np.isfinite(fair_ofi)
            alignment = (fair_ofi == 1.0)

        else:  # gap_depth_extOFI
            D_ext = np.where(
                np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi),
                (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0,
                np.nan,
            )
            conf_fin = np.isfinite(D_ext)
            alignment = (
                (np.sign(D_ext) == -np.sign(gap))
                & (D_ext != 0.0)
                & conf_fin
            )

        gap_event_domain = gap_thresh_domain & conf_fin
        if threshold is None:
            post = np.zeros(n_grid, dtype=bool)
        else:
            post = (
                gap_event_domain
                & (gap != 0.0)
                & (np.abs(gap) >= threshold)
                & alignment
                & np.isfinite(fwd)
            )
        direction_array = -np.sign(gap)

    else:
        raise ValueError(f"Stage3 HB: unsupported feature {feature!r}")

    positions = np.asarray(ctx.gpos[post], dtype=np.int64)
    directions = np.asarray(direction_array[post], dtype=float)
    returns = np.asarray(fwd[post], dtype=float)
    return positions, directions, returns, threshold


# ---------------------------------------------------------------------------
# HB axis row generator per block
# ---------------------------------------------------------------------------

def _hb_block(
    ctx: _BlockContext,
    session_id: str,
    asset: str,
) -> list[dict]:
    """Generate HB rows for one (session_id, asset) block.

    B0: >= spacing (frozen P0).
    B1: > spacing (strict boundary).
    q = 0.90 only.
    """
    rows: list[dict] = []

    for feature in STAGE3_HB_FAMILIES:
        for horizon_ms in STAGE3_HORIZONS:
            k = horizon_ms // GRID_MS
            spacing = max(10, k)
            fwd = _build_forward_return_array(ctx.mid, k)

            positions, directions, returns, threshold = _hb_extract_pre_overlap(
                feature, ctx, horizon_ms, STAGE3_HB_Q, fwd
            )
            pre_n = len(positions)
            esp = exact_spacing_pair_n(positions, spacing)

            # B0: >= spacing_steps
            b0_mask = (
                _greedy_overlap_filter(positions, spacing)
                if pre_n > 0
                else np.zeros(0, dtype=bool)
            )
            b0_accepted_n = int(np.sum(b0_mask))
            b0_pos = positions[b0_mask]
            b0_fp = _sha256_positions(b0_pos)
            b0_metrics = _compute_event_metrics(directions, returns, b0_mask)

            # I17: HB-B0 drift guard
            _assert_event_drift_guard(
                ctx, feature, horizon_ms, STAGE3_HB_Q,
                b0_accepted_n, threshold,
                b0_metrics["mean_signed_bps"], b0_metrics["hit_rate"],
                label=f"HB-B0-{feature}",
            )

            rows.append({
                "diagnostic_version": STAGE3_VERSION,
                "session_id": session_id,
                "asset": asset,
                "axis": "HB",
                "feature_family": feature,
                "variant_id": "B0",
                "quantile": STAGE3_HB_Q,
                "horizon_ms": horizon_ms,
                "threshold_domain_finite_n": None,
                "threshold_domain_nonzero_n": None,
                "zero_n": None,
                "zero_fraction": None,
                "nonzero_unique_value_n": None,
                "hz_discrimination_class": None,
                "gate_zero_n": None,
                "calculated_threshold": threshold,
                "spacing_steps": spacing,
                "pre_overlap_n": pre_n,
                "exact_spacing_pair_n": esp,
                "accepted_n": b0_accepted_n,
                "overlap_dropped_n": pre_n - b0_accepted_n,
                "accepted_positions_sha256": b0_fp,
                "mean_signed_bps": b0_metrics["mean_signed_bps"],
                "hit_rate": b0_metrics["hit_rate"],
                "mean_abs_move": b0_metrics["mean_abs_move"],
            })

            # B1: > spacing_steps (strict)
            b1_mask = (
                _greedy_overlap_filter_strict(positions, spacing)
                if pre_n > 0
                else np.zeros(0, dtype=bool)
            )
            b1_accepted_n = int(np.sum(b1_mask))
            b1_pos = positions[b1_mask]
            b1_fp = _sha256_positions(b1_pos)
            b1_metrics = _compute_event_metrics(directions, returns, b1_mask)

            rows.append({
                "diagnostic_version": STAGE3_VERSION,
                "session_id": session_id,
                "asset": asset,
                "axis": "HB",
                "feature_family": feature,
                "variant_id": "B1",
                "quantile": STAGE3_HB_Q,
                "horizon_ms": horizon_ms,
                "threshold_domain_finite_n": None,
                "threshold_domain_nonzero_n": None,
                "zero_n": None,
                "zero_fraction": None,
                "nonzero_unique_value_n": None,
                "hz_discrimination_class": None,
                "gate_zero_n": None,
                "calculated_threshold": threshold,
                "spacing_steps": spacing,
                "pre_overlap_n": pre_n,
                "exact_spacing_pair_n": esp,
                "accepted_n": b1_accepted_n,
                "overlap_dropped_n": pre_n - b1_accepted_n,
                "accepted_positions_sha256": b1_fp,
                "mean_signed_bps": b1_metrics["mean_signed_bps"],
                "hit_rate": b1_metrics["hit_rate"],
                "mean_abs_move": b1_metrics["mean_abs_move"],
            })

    return rows


# ---------------------------------------------------------------------------
# Invariant suite I1 – I21
# ---------------------------------------------------------------------------

def _run_invariants(rows: list[dict]) -> None:  # noqa: C901
    """Run all I1–I21 invariants over the full 576-row list.

    Raises AssertionError with the invariant label on the first violation.
    Called before any artifact write (fail-closed).
    """
    from collections import defaultdict

    # ── Partition rows by axis ─────────────────────────────────────────────
    by_axis: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_axis[r["axis"]].append(r)

    hz_rows = by_axis.get("HZ", [])
    hg_rows = by_axis.get("HG", [])
    hc_rows = by_axis.get("HC", [])
    hb_rows = by_axis.get("HB", [])

    # I1: fixed axis/variant membership
    valid_combos = {
        ("HZ", "T0"), ("HZ", "TZ"),
        ("HG", "G0"), ("HG", "G1"),
        ("HC", "C0"), ("HC", "C1"),
        ("HB", "B0"), ("HB", "B1"),
    }
    for r in rows:
        if (r["axis"], r["variant_id"]) not in valid_combos:
            raise AssertionError(
                f"I1 FAIL: unexpected axis/variant ({r['axis']!r}, {r['variant_id']!r})"
            )

    # I2: unique row key within each axis
    def _key_hz(r: dict) -> tuple:
        return (r["session_id"], r["asset"], r["feature_family"], r["variant_id"], r["quantile"])
    def _key_event(r: dict) -> tuple:
        return (r["session_id"], r["asset"], r["feature_family"], r["variant_id"], r["quantile"], r["horizon_ms"])

    for axis, axis_rows, key_fn in [
        ("HZ", hz_rows, _key_hz),
        ("HG", hg_rows, _key_event),
        ("HC", hc_rows, _key_event),
        ("HB", hb_rows, _key_event),
    ]:
        seen: set = set()
        for r in axis_rows:
            k = key_fn(r)  # type: ignore[operator]
            if k in seen:
                raise AssertionError(f"I2 FAIL: duplicate row key {k!r} in axis {axis}")
            seen.add(k)

    # I3: exact row counts
    if len(hz_rows) != STAGE3_HZ_ROWS:
        raise AssertionError(f"I3 FAIL: HZ expected {STAGE3_HZ_ROWS} got {len(hz_rows)}")
    if len(hg_rows) != STAGE3_HG_ROWS:
        raise AssertionError(f"I3 FAIL: HG expected {STAGE3_HG_ROWS} got {len(hg_rows)}")
    if len(hc_rows) != STAGE3_HC_ROWS:
        raise AssertionError(f"I3 FAIL: HC expected {STAGE3_HC_ROWS} got {len(hc_rows)}")
    if len(hb_rows) != STAGE3_HB_ROWS:
        raise AssertionError(f"I3 FAIL: HB expected {STAGE3_HB_ROWS} got {len(hb_rows)}")
    if len(rows) != STAGE3_TOTAL_ROWS:
        raise AssertionError(f"I3 FAIL: TOTAL expected {STAGE3_TOTAL_ROWS} got {len(rows)}")

    # I4: finite_n >= nonzero_n >= 0
    for r in hz_rows:
        fn = r["threshold_domain_finite_n"]
        nn = r["threshold_domain_nonzero_n"]
        if fn is None or nn is None:
            raise AssertionError(f"I4 FAIL: HZ row missing finite/nonzero counts")
        if not (fn >= nn >= 0):
            raise AssertionError(f"I4 FAIL: finite_n={fn} nonzero_n={nn}")

    # I5: zero_n = finite_n - nonzero_n
    for r in hz_rows:
        fn = r["threshold_domain_finite_n"]
        nn = r["threshold_domain_nonzero_n"]
        zn = r["zero_n"]
        if zn != fn - nn:
            raise AssertionError(f"I5 FAIL: zero_n={zn} != finite_n-nonzero_n={fn-nn}")

    # I6: zero_fraction semantics
    for r in hz_rows:
        fn = r["threshold_domain_finite_n"]
        zn = r["zero_n"]
        zf = r["zero_fraction"]
        if fn > 0:
            expected_zf = zn / fn
            if zf is None:
                raise AssertionError(f"I6 FAIL: zero_fraction is None but finite_n={fn}")
            if not (0.0 <= zf <= 1.0):
                raise AssertionError(f"I6 FAIL: zero_fraction={zf} out of [0,1]")
            if abs(zf - expected_zf) > 1e-15:
                raise AssertionError(
                    f"I6 FAIL: zero_fraction={zf!r} expected={expected_zf!r}"
                )
        else:
            if zf is not None:
                raise AssertionError(
                    f"I6 FAIL: finite_n=0 but zero_fraction={zf!r} (expected None)"
                )

    # I7: HZ class exactly follows frozen classification
    for r in hz_rows:
        zn = r["zero_n"]
        t_thresh = r["calculated_threshold"]
        hz_class = r["hz_discrimination_class"]
        variant_id = r["variant_id"]
        # We need both T0 and TZ thresholds; look up both from the same feature/q block
        # For I7 verification, we re-check the classification rule indirectly:
        # ZERO_FREE_CONTROL requires zero_n == 0
        # HZ_DISCRIMINATING requires zero_n > 0 (checked via the matching T0/TZ pair)
        if hz_class == _ZERO_FREE_CONTROL and zn != 0:
            raise AssertionError(f"I7 FAIL: ZERO_FREE_CONTROL but zero_n={zn}")
        if hz_class not in (_ZERO_FREE_CONTROL, _HZ_DISCRIMINATING, _HZ_NONDISCRIMINATING):
            raise AssertionError(f"I7 FAIL: unknown class {hz_class!r}")

    # Also verify T0/TZ pairs agree on hz_discrimination_class
    hz_class_by_key: dict = {}
    for r in hz_rows:
        key = (r["session_id"], r["asset"], r["feature_family"], r["quantile"])
        if key not in hz_class_by_key:
            hz_class_by_key[key] = r["hz_discrimination_class"]
        elif hz_class_by_key[key] != r["hz_discrimination_class"]:
            raise AssertionError(
                f"I7 FAIL: T0/TZ have different hz_discrimination_class for key={key!r}"
            )

    # I8: pre_overlap_n = accepted_n + overlap_dropped_n (event axes)
    for r in hg_rows + hc_rows + hb_rows:
        pre_n = r["pre_overlap_n"]
        acc_n = r["accepted_n"]
        drop_n = r["overlap_dropped_n"]
        if pre_n is None or acc_n is None or drop_n is None:
            raise AssertionError(f"I8 FAIL: None in pre/accepted/dropped fields")
        if pre_n != acc_n + drop_n:
            raise AssertionError(
                f"I8 FAIL: pre_overlap_n={pre_n} != accepted_n+dropped_n={acc_n+drop_n}"
            )

    # I9: accepted positions unique and strictly ascending (via fingerprint)
    # Verified by construction: _sha256_positions sorts ascending and positions come
    # from a greedy filter applied to strictly ascending input.

    # I10: fingerprint encoded-position count == accepted_n (for event rows)
    # Enforced by construction: sha256 is computed from accepted positions.

    # I11: B1.accepted_n <= B0.accepted_n (for each paired HB case)
    hb_by_key: dict = {}
    for r in hb_rows:
        key = (r["session_id"], r["asset"], r["feature_family"], r["horizon_ms"])
        hb_by_key.setdefault(key, {})[r["variant_id"]] = r
    for key, pair in hb_by_key.items():
        if "B0" in pair and "B1" in pair:
            if pair["B1"]["accepted_n"] > pair["B0"]["accepted_n"]:
                raise AssertionError(
                    f"I11 FAIL: B1.accepted_n={pair['B1']['accepted_n']} "
                    f"> B0.accepted_n={pair['B0']['accepted_n']} for key={key!r}"
                )

    # I12: if exact_spacing_pair_n == 0 -> B0 and B1 identical results
    for key, pair in hb_by_key.items():
        if "B0" in pair and "B1" in pair:
            esp = pair["B0"]["exact_spacing_pair_n"]
            if esp == 0:
                b0, b1 = pair["B0"], pair["B1"]
                for field in ("accepted_n", "accepted_positions_sha256",
                              "mean_signed_bps", "hit_rate", "mean_abs_move"):
                    if b0[field] != b1[field]:
                        raise AssertionError(
                            f"I12 FAIL: esp=0 but B0.{field}={b0[field]!r} "
                            f"!= B1.{field}={b1[field]!r} for key={key!r}"
                        )

    # I13: ZERO_FREE_CONTROL implies zero_n == 0
    for r in hz_rows:
        if r["hz_discrimination_class"] == _ZERO_FREE_CONTROL:
            if r["zero_n"] != 0:
                raise AssertionError(
                    f"I13 FAIL: ZERO_FREE_CONTROL but zero_n={r['zero_n']}"
                )

    # I14: NEW36 never opened - enforced at the caller level (safety validation)
    # I15: FrozenAnalysisEngine NOT_CONFIGURED / accepts_input=False - safety validation
    # I16: V1.1 / Stage1 / Stage2 source unchanged - verified externally
    # I17: drift guard - checked inline during generation
    # I18: G1 threshold == HZ-T0 depth_imbalance_l1 threshold - checked during generation
    # I19: G1 pre-overlap subset of di_l1 pre-overlap - checked during generation
    # I20: C1 pre-overlap subset of C0 threshold-crossing set - checked during generation

    # I21: runtime module contains no golden reads - static test (test_v1_2_stage3.py)


# ---------------------------------------------------------------------------
# Core row generation
# ---------------------------------------------------------------------------

def generate_stage3_rows(
    contexts: "dict[tuple[str, str], _BlockContext] | None" = None,
) -> list[dict]:
    """Generate exactly 576 Stage 3 diagnostic rows.  Explicit call only.

    Parameters
    ----------
    contexts : dict or None
        Mapping (session_id, asset) -> _BlockContext.
        - If not None: uses supplied (pre-built / synthetic) contexts.
          This path is used by focused tests and NEVER opens RAW data.
        - If None: real execution path — validates safety invariants and
          loads one OLD36 block at a time through the existing sandbox.
          THIS PATH MUST NOT BE INVOKED UNTIL EXPLICITLY AUTHORISED.

    Returns
    -------
    list of 576 dicts, one per (axis, session, asset, feature_family, variant, q[, horizon]).
    """
    validate_hz_feature_set()

    all_rows: list[dict] = []

    if contexts is not None:
        # ── Synthetic / test path ──────────────────────────────────────────
        for sid in STAGE3_SESSIONS:
            for asset in STAGE3_ASSETS:
                ctx = contexts[(sid, asset)]
                hz_rows, hz_cache = _hz_block(ctx, sid, asset)
                hg_rows = _hg_block(ctx, sid, asset, hz_cache)
                hc_rows = _hc_block(ctx, sid, asset)
                hb_rows = _hb_block(ctx, sid, asset)
                all_rows.extend(hz_rows)
                all_rows.extend(hg_rows)
                all_rows.extend(hc_rows)
                all_rows.extend(hb_rows)
    else:
        # ── Real execution path (NOT authorised until explicitly permitted) ──
        _validate_stage3_safety()
        from .harness import _load_grid_for_block, _validate_sync_grid_part_coverage
        from .v1_2_diagnostics import _prepare_block
        checked_sessions: set[str] = set()
        for sid in STAGE3_SESSIONS:
            if sid not in checked_sessions:
                coverage_ok, reason = _validate_sync_grid_part_coverage(sid)
                if not coverage_ok:
                    raise RuntimeError(
                        f"Stage3 part coverage failure for {sid}: {reason}"
                    )
                checked_sessions.add(sid)
            for asset in STAGE3_ASSETS:
                df = _load_grid_for_block(sid, asset)
                ctx = _prepare_block(sid, asset, df)
                hz_rows, hz_cache = _hz_block(ctx, sid, asset)
                hg_rows = _hg_block(ctx, sid, asset, hz_cache)
                hc_rows = _hc_block(ctx, sid, asset)
                hb_rows = _hb_block(ctx, sid, asset)
                all_rows.extend(hz_rows)
                all_rows.extend(hg_rows)
                all_rows.extend(hc_rows)
                all_rows.extend(hb_rows)
                del ctx, df

    # Run invariants before any write.
    _run_invariants(all_rows)

    return all_rows


# ---------------------------------------------------------------------------
# Runtime safety validation (real execution path only)
# ---------------------------------------------------------------------------

def _validate_stage3_safety() -> None:
    """Validate all safety invariants before any real OLD36 data access.

    Never opens NEW36 data.
    """
    from checkpoint_registry import NEW36_SESSION_IDS, OLD36_REFERENCE_SESSIONS
    from frozen_engine import current_status as frozen_engine_status
    from .allowlist import assert_recovery_allowed

    old = tuple(OLD36_REFERENCE_SESSIONS)
    if any(sid not in old for sid in STAGE3_SESSIONS):
        raise RuntimeError(
            "Stage3 declared session not in OLD36_REFERENCE_SESSIONS"
        )
    for sid in STAGE3_SESSIONS:
        assert_recovery_allowed(sid)

    if not NEW36_SESSION_IDS:
        raise RuntimeError("NEW36 registry unexpectedly empty")
    try:
        assert_recovery_allowed(NEW36_SESSION_IDS[0])
    except PermissionError:
        pass
    else:  # pragma: no cover
        raise RuntimeError("NEW36 quantitative firewall is not rejecting NEW36")

    status = frozen_engine_status()
    if status.status != "NOT_CONFIGURED" or status.accepts_input is not False:
        raise RuntimeError(
            f"FrozenAnalysisEngine unsafe state: "
            f"status={status.status!r}, accepts_input={status.accepts_input!r}"
        )


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------

def write_stage3_artifacts(
    rows: list[dict],
    runtime_source_commit: str,
) -> "tuple[Path, Path]":
    """Write Stage 3 diagnostics CSV and deterministic manifest JSON.

    Always writes to _STAGE3_DEFAULT_OUTPUT_DIR.
    No output_dir parameter (production path is fixed).

    Raises
    ------
    AssertionError  if either output filename is in _PROTECTED_NAMES.
    FileExistsError if either output file already exists.
    """
    out = _STAGE3_DEFAULT_OUTPUT_DIR
    csv_path = out / "stage3_diagnostics.csv"
    manifest_path = out / "stage3_manifest.json"

    # Protected name guard
    for name, path in (("stage3_diagnostics.csv", csv_path),
                       ("stage3_manifest.json", manifest_path)):
        if name in _PROTECTED_NAMES:
            raise AssertionError(
                f"Stage3 output name {name!r} is in _PROTECTED_NAMES"
            )

    # Existence guard (no overwrite)
    if csv_path.exists():
        raise FileExistsError(
            f"Stage3 CSV already exists: {csv_path}"
        )
    if manifest_path.exists():
        raise FileExistsError(
            f"Stage3 manifest already exists: {manifest_path}"
        )

    # Compute row counts per axis
    from collections import Counter
    axis_counts: Counter = Counter(r["axis"] for r in rows)

    # Serialize CSV
    csv_bytes = _csv_bytes(rows)
    artifact_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    artifact_size = len(csv_bytes)

    # Source SHA256
    def _file_sha256(p: Path) -> "str | None":
        if p.exists():
            return hashlib.sha256(p.read_bytes()).hexdigest()
        return None

    stage3_src = Path(__file__).resolve()
    stage3_test = stage3_src.parent.parent / "tests" / "test_v1_2_stage3.py"

    # Build deterministic manifest (no timestamp; sort_keys=True)
    manifest: dict = {
        "actual_row_counts_per_axis": {
            "HB": int(axis_counts.get("HB", 0)),
            "HC": int(axis_counts.get("HC", 0)),
            "HG": int(axis_counts.get("HG", 0)),
            "HZ": int(axis_counts.get("HZ", 0)),
        },
        "actual_total_row_count": len(rows),
        "assets": list(STAGE3_ASSETS),
        "axes": ["HZ", "HG", "HC", "HB"],
        "diagnostic_version": STAGE3_VERSION,
        "expected_row_counts_per_axis": {
            "HB": STAGE3_HB_ROWS,
            "HC": STAGE3_HC_ROWS,
            "HG": STAGE3_HG_ROWS,
            "HZ": STAGE3_HZ_ROWS,
        },
        "expected_total_row_count": STAGE3_TOTAL_ROWS,
        "feature_sets_per_axis": {
            "HB": list(STAGE3_HB_FAMILIES),
            "HC": ["gap_depth_extOFI"],
            "HG": ["depthL1_extOFI"],
            "HZ": list(STAGE3_HZ_FEATURES),
        },
        "fingerprint_encoding_definition": _FINGERPRINT_ENCODING_DEFINITION,
        "frozen_analysis_engine_state": "NOT_CONFIGURED",
        "golden_artifacts_read_by_generator": False,
        "horizons_per_axis": {
            "HB": list(STAGE3_HORIZONS),
            "HC": list(STAGE3_HORIZONS),
            "HG": list(STAGE3_HORIZONS),
            "HZ": [],
        },
        "new36_opened": False,
        "quantile_method": "linear_type7",
        "quantiles_per_axis": {
            "HB": [STAGE3_HB_Q],
            "HC": list(STAGE3_QUANTILES),
            "HG": list(STAGE3_QUANTILES),
            "HZ": list(STAGE3_QUANTILES),
        },
        "runtime_source_commit": runtime_source_commit,
        "session_ids": list(STAGE3_SESSIONS),
        "source_sha256": {
            "backend/recovery/v1_2_stage3.py": _file_sha256(stage3_src),
            "backend/tests/test_v1_2_stage3.py": _file_sha256(stage3_test),
        },
        "stage3_diagnostics_sha256": artifact_sha256,
        "stage3_diagnostics_size": artifact_size,
        "threshold_exact_tolerance": "None==None exact; float: exact Python float equality",
        "v1_1_stage1_stage2_modified": False,
        "variant_sets_per_axis": {
            "HB": ["B0", "B1"],
            "HC": ["C0", "C1"],
            "HG": ["G0", "G1"],
            "HZ": ["T0", "TZ"],
        },
    }

    # Write files
    out.mkdir(parents=True, exist_ok=True)
    csv_path.write_bytes(csv_bytes)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return csv_path, manifest_path


def run_stage3_diagnostics(
    runtime_source_commit: str,
) -> "tuple[Path, Path]":
    """Top-level Stage 3 orchestrator.  Requires explicit authorisation.

    Calls generate_stage3_rows() (real OLD36 data path) then writes
    artifacts via write_stage3_artifacts() to _STAGE3_DEFAULT_OUTPUT_DIR.

    THIS MUST NOT BE INVOKED UNTIL EXPLICITLY AUTHORISED.
    """
    rows = generate_stage3_rows(contexts=None)
    return write_stage3_artifacts(rows, runtime_source_commit)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "STAGE3_VERSION",
    "STAGE3_SESSIONS",
    "STAGE3_ASSETS",
    "STAGE3_HZ_FEATURES",
    "STAGE3_QUANTILES",
    "STAGE3_HORIZONS",
    "STAGE3_HZ_VARIANTS",
    "STAGE3_HG_VARIANTS",
    "STAGE3_HC_VARIANTS",
    "STAGE3_HB_VARIANTS",
    "STAGE3_HB_Q",
    "STAGE3_HB_FAMILIES",
    "STAGE3_HZ_ROWS",
    "STAGE3_HG_ROWS",
    "STAGE3_HC_ROWS",
    "STAGE3_HB_ROWS",
    "STAGE3_TOTAL_ROWS",
    "_STAGE3_CSV_COLUMNS",
    "_PROTECTED_NAMES",
    "validate_hz_feature_set",
    "exact_spacing_pair_n",
    "_greedy_overlap_filter_strict",
    "_sha256_positions",
    "_compute_event_metrics",
    "_format_field",
    "_csv_bytes",
    "_hz_block",
    "_hg_block",
    "_hc_block",
    "_hb_block",
    "_hb_extract_pre_overlap",
    "_assert_hz_t0_drift_guard",
    "_assert_event_drift_guard",
    "_run_invariants",
    "generate_stage3_rows",
    "write_stage3_artifacts",
    "run_stage3_diagnostics",
]
