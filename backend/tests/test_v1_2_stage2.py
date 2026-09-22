"""Focused tests for RECONSTRUCTION_V1.2 Stage 2 implementation.

Spec:   V1.2_STAGE2_FINAL
Tests:  T1-T6 plus additional invariant/edge-case coverage.

IMPORTANT: These tests NEVER execute the real Stage 2 diagnostic against
OLD36 sessions.  All data-path tests use synthetic _BlockContext objects.
No RAW data is opened.  No Stage 2 artifact files are written.
"""
from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ── path setup ──────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import csv as csv_module
import json as json_module

from recovery.v1_2_stage2 import (
    STAGE2_ASSETS,
    STAGE2_FAMILIES,
    STAGE2_HORIZONS,
    STAGE2_POLICY_ROWS,
    STAGE2_PRIMARY_FAMILIES,
    STAGE2_CONTROL_FAMILIES,
    STAGE2_Q,
    STAGE2_SESSIONS,
    STAGE2_UNIQUE_CASES,
    STAGE2_VERSION,
    Stage2Case,
    _STAGE2_CSV_COLUMNS,
    _I12_GAP_EXTRA_FIELDS,
    _I12_SHARED_FIELDS,
    _assert_i12_actual_field_comparison,
    _assert_r3_p0_drift_guard,
    _compute_policy_metrics,
    _extract_pre_overlap,
    _greedy_latest_first_filter,
    _PreOverlapResult,
    _sha256_positions,
    compute_pre_overlap_diagnostics,
    expand_stage2_matrix,
    generate_stage2_rows,
    write_stage2_artifacts,
)
from recovery.engine import GRID_MS, BlockMetrics, _greedy_overlap_filter
from recovery.v1_2_diagnostics import _BlockContext


# ===========================================================================
# Synthetic context helpers
# ===========================================================================

def _make_ctx(
    session_id: str,
    asset: str,
    n: int = 400,
    seed: int = 42,
) -> _BlockContext:
    """Build a minimal synthetic _BlockContext for testing.

    Uses deterministic random data with enough variation to produce
    non-trivial pre-overlap event sets for all Stage 2 feature families.
    mid has a gentle oscillating pattern so forward returns are non-zero
    and alternate sign, giving non-trivial hit_rate values.
    """
    rng = np.random.default_rng(seed)

    gpos = np.arange(n, dtype=np.int64)
    quality = np.ones(n, dtype=bool)

    # Oscillating mid: ensures forward returns alternate +/- for rich coverage.
    t = np.linspace(0, 4 * np.pi, n)
    mid = 100.0 + 2.0 * np.sin(t) + rng.normal(0.0, 0.05, n)
    mid = np.maximum(mid, 0.01)  # must be positive

    # Feature columns
    signal_ofi = rng.normal(0.0, 1.0, n)
    signal_di_l1 = rng.normal(0.0, 1.0, n)
    signal_ext_ofi = rng.normal(0.0, 1.0, n)
    signal_gap = rng.normal(0.0, 1.0, n)
    # bitget_fair_ofi_alignment: sparse ternary {-1, 0, 1}
    signal_align = rng.choice([-1.0, 0.0, 1.0], size=n)

    # z-scores for derived features (approximated by rank-normalised values)
    # Use simple standardisation rather than full _rank_signed_uniform to avoid
    # engine import for tests.
    def _zscore(x: np.ndarray) -> np.ndarray:
        std = np.std(x)
        if std == 0:
            return np.zeros_like(x)
        return (x - np.mean(x)) / std

    z_di_l1 = _zscore(signal_di_l1)
    z_ext_ofi = _zscore(signal_ext_ofi)

    columns: dict[str, np.ndarray] = {
        "bitget_ofi_norm_l1": signal_ofi,
        "bitget_depth_imbalance_l1": signal_di_l1,
        "bitget_depth_imbalance_l5": signal_di_l1,   # reuse l1 for simplicity
        "bitget_gap_to_fair_bps": signal_gap,
        "bitget_fair_ofi_alignment": signal_align,
        "external_ofi_consensus_l1": signal_ext_ofi,
        # Extra columns referenced by _col helper
        "external_perp_dispersion_bps": rng.uniform(0.0, 5.0, n),
        "external_trade_imbalance_consensus": rng.normal(0.0, 1.0, n),
        "bitget_trade_imbalance_window": rng.normal(0.0, 1.0, n),
        "fair_accel_100ms_bps": rng.normal(0.0, 0.5, n),
        "leader_gap_100ms_bps": rng.normal(0.0, 1.0, n),
        "leader_gap_200ms_bps": rng.normal(0.0, 1.0, n),
        "leader_gap_500ms_bps": rng.normal(0.0, 1.0, n),
        "leader_gap_1000ms_bps": rng.normal(0.0, 1.0, n),
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
        z_di_l5=z_di_l1,   # reuse
        z_ext_ofi=z_ext_ofi,
        disp_lo=None,
        disp_hi=None,
        metrics={},   # Stage 2 does not use V1.1 metrics
    )


def _make_all_contexts(seed_base: int = 0) -> dict[tuple[str, str], _BlockContext]:
    """Build synthetic contexts for all declared (session, asset) pairs."""
    ctxs = {}
    for i, sid in enumerate(STAGE2_SESSIONS):
        for j, asset in enumerate(STAGE2_ASSETS):
            ctxs[(sid, asset)] = _make_ctx(sid, asset, seed=seed_base + i * 10 + j)
    return ctxs


# ===========================================================================
# T1: Fixed matrix exactly 144 policy rows / 72 P0 + 72 P1
# ===========================================================================

def test_t1_matrix_is_exactly_144_rows_72_p0_72_p1():
    """T1: expand_stage2_matrix() produces exactly 144 rows with 72 P0 and 72 P1."""
    matrix = expand_stage2_matrix()
    assert len(matrix) == STAGE2_POLICY_ROWS == 144

    p0 = [c for c in matrix if c.policy == "P0"]
    p1 = [c for c in matrix if c.policy == "P1"]
    assert len(p0) == STAGE2_UNIQUE_CASES == 72
    assert len(p1) == STAGE2_UNIQUE_CASES == 72


def test_t1_matrix_covers_declared_dimensions():
    """T1 (dimension check): matrix spans all declared sessions/assets/families/horizons."""
    matrix = expand_stage2_matrix()
    sessions = {c.session_id for c in matrix}
    assets = {c.asset for c in matrix}
    features = {c.feature for c in matrix}
    horizons = {c.horizon_ms for c in matrix}
    policies = {c.policy for c in matrix}

    assert sessions == set(STAGE2_SESSIONS)
    assert assets == set(STAGE2_ASSETS)
    assert features == set(STAGE2_FAMILIES)
    assert horizons == set(STAGE2_HORIZONS)
    assert policies == {"P0", "P1"}


def test_t1_generate_stage2_rows_with_synthetic_contexts():
    """T1 (integration): generate_stage2_rows returns exactly 144 rows with synthetic data."""
    contexts = _make_all_contexts(seed_base=100)
    rows = generate_stage2_rows(contexts=contexts)
    assert len(rows) == STAGE2_POLICY_ROWS


def test_t1_exactly_one_p0_one_p1_per_unique_case_in_output():
    """T1 / I10: each unique case key appears exactly once for P0 and once for P1."""
    contexts = _make_all_contexts(seed_base=200)
    rows = generate_stage2_rows(contexts=contexts)
    from collections import Counter
    counter: Counter = Counter()
    for row in rows:
        key = (row["session_id"], row["asset"], row["feature"],
               row["horizon_ms"], row["q"])
        counter[(key, row["policy"])] += 1
    for (key, policy), cnt in counter.items():
        assert cnt == 1, f"case {key} policy {policy} appears {cnt} times"
    unique_keys = {k for (k, _) in counter}
    assert len(unique_keys) == STAGE2_UNIQUE_CASES


# ===========================================================================
# T2: I12 paired pre-overlap identity
# ===========================================================================

def test_t2_i12_pre_overlap_fields_identical_between_p0_and_p1():
    """T2 / I12: for every (session,asset,feature,horizon,q) the 10 policy-independent
    pre-overlap diagnostic fields must be byte-identical between P0 and P1 rows."""
    I12_FIELDS = (
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
    contexts = _make_all_contexts(seed_base=300)
    rows = generate_stage2_rows(contexts=contexts)

    # Index by case key → {policy → row}
    by_case: dict = {}
    for row in rows:
        key = (row["session_id"], row["asset"], row["feature"],
               row["horizon_ms"], row["q"])
        by_case.setdefault(key, {})[row["policy"]] = row

    for key, policy_rows in by_case.items():
        assert "P0" in policy_rows and "P1" in policy_rows, f"missing policy for {key}"
        r0 = policy_rows["P0"]
        r1 = policy_rows["P1"]
        for field in I12_FIELDS:
            assert r0[field] == r1[field], (
                f"I12 FAIL for case {key}: field '{field}' differs "
                f"P0={r0[field]!r} P1={r1[field]!r}"
            )


def test_t2_i12_gap_depth_extofi_extra_fields_identical():
    """T2 / I12 extension: gap_depth_extOFI additional pipeline diagnostics must be
    identical between P0 and P1."""
    GAP_EXTRA_FIELDS = (
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
    contexts = _make_all_contexts(seed_base=400)
    rows = generate_stage2_rows(contexts=contexts)

    gap_rows = [r for r in rows if r["feature"] == "gap_depth_extOFI"]
    assert len(gap_rows) > 0

    by_case: dict = {}
    for row in gap_rows:
        key = (row["session_id"], row["asset"], row["feature"],
               row["horizon_ms"], row["q"])
        by_case.setdefault(key, {})[row["policy"]] = row

    for key, policy_rows in by_case.items():
        r0 = policy_rows["P0"]
        r1 = policy_rows["P1"]
        for field in GAP_EXTRA_FIELDS:
            assert r0[field] == r1[field], (
                f"I12 GAP EXTRA FAIL for case {key}: '{field}' differs "
                f"P0={r0[field]!r} P1={r1[field]!r}"
            )


# ===========================================================================
# T3: Synthetic P0/P1 directional behaviour + exact boundary semantics
# ===========================================================================

def test_t3_p0_earliest_first_accepts_smallest_position():
    """T3: P0 (earliest-first) accepts the first valid event, not the latest."""
    # Five positions with spacing=25: only positions 0 and 25 (exactly) can coexist.
    # Positions further apart: 0, 25 → gap=25>=25 ✓; 25, 50 → gap=25>=25 ✓;
    # 0, 25, 50 all coexist.
    positions = np.array([0, 10, 25, 35, 50], dtype=np.int64)
    spacing = 25

    p0_mask = _greedy_overlap_filter(positions, spacing)
    p0 = positions[p0_mask]
    # P0: accept 0 (first), skip 10 (gap 10<25), accept 25 (gap 25>=25),
    #     skip 35 (gap 10<25), accept 50 (gap 25>=25)
    assert list(p0) == [0, 25, 50], f"P0 accepted {list(p0)}"


def test_t3_p1_latest_first_accepts_largest_position():
    """T3: P1 (latest-first) accepts the last valid event, not the earliest."""
    positions = np.array([0, 10, 25, 35, 50], dtype=np.int64)
    spacing = 25

    p1_mask = _greedy_latest_first_filter(positions, spacing)
    p1 = positions[p1_mask]
    # P1: accept 50 (first/largest), skip 35 (50-35=15<25), accept 25 (50-25=25>=25),
    #     skip 10 (25-10=15<25), accept 0 (25-0=25>=25)
    assert list(p1) == [0, 25, 50], f"P1 accepted {list(p1)}"


def test_t3_p0_p1_differ_on_asymmetric_dense_cluster():
    """T3: P0 and P1 produce different accepted sets when candidate density is asymmetric."""
    # Dense early cluster [0,5,10,15] + sparse late [100]
    positions = np.array([0, 5, 10, 15, 100], dtype=np.int64)
    spacing = 20   # 0→5→10→15 are all within spacing

    p0_mask = _greedy_overlap_filter(positions, spacing)
    p1_mask = _greedy_latest_first_filter(positions, spacing)
    p0 = list(map(int, positions[p0_mask]))
    p1 = list(map(int, positions[p1_mask]))

    # P0: accept 0 (first), skip 5,10,15 (gaps<20), accept 100 (gap>=20)
    assert p0 == [0, 100], f"P0={p0}"
    # P1: accept 100 (last), skip 15 (100-15=85>=20 → actually accept!),
    # wait: 100-15=85>=20 → ACCEPT 15, then 15-10=5<20 → skip 10,
    # 15-5=10<20 → skip 5, 15-0=15<20 → skip 0
    # Re-trace: P1 processing descending: [100,15,10,5,0]
    #   100: accept (last_pos = 100)
    #   15: 100-15=85>=20 → accept (last_pos = 15)
    #   10: 15-10=5<20 → skip
    #   5:  15-5=10<20 → skip
    #   0:  15-0=15<20 → skip
    assert p1 == [15, 100], f"P1={p1}"
    assert p0 != p1


def test_t3_boundary_distance_equal_spacing_both_accepted():
    """T3 (exact boundary): distance == spacing_steps → ACCEPTED (non-conflicting)."""
    spacing = 10
    # Two positions exactly spacing apart
    positions = np.array([0, 10], dtype=np.int64)

    p0_mask = _greedy_overlap_filter(positions, spacing)
    p1_mask = _greedy_latest_first_filter(positions, spacing)

    # Both policies must accept both events (gap = spacing → non-conflicting)
    assert list(positions[p0_mask]) == [0, 10], "P0 should accept both at equality"
    assert list(positions[p1_mask]) == [0, 10], "P1 should accept both at equality"


def test_t3_boundary_distance_less_than_spacing_conflict():
    """T3 (exact boundary): distance < spacing_steps → CONFLICT, only one accepted."""
    spacing = 10
    # Two positions separated by spacing-1
    positions = np.array([0, 9], dtype=np.int64)

    p0_mask = _greedy_overlap_filter(positions, spacing)
    p1_mask = _greedy_latest_first_filter(positions, spacing)

    p0 = list(map(int, positions[p0_mask]))
    p1 = list(map(int, positions[p1_mask]))

    # Each policy accepts exactly one event, but different ones
    assert len(p0) == 1, f"P0 should accept exactly 1, got {p0}"
    assert len(p1) == 1, f"P1 should accept exactly 1, got {p1}"
    assert p0 == [0],  f"P0 should accept the earliest (0), got {p0}"
    assert p1 == [9],  f"P1 should accept the latest (9), got {p1}"


def test_t3_p1_accepted_positions_returned_ascending():
    """T3: P1 filter returns positions in ascending order regardless of selection order."""
    positions = np.array([10, 20, 100, 200, 300], dtype=np.int64)
    spacing = 50   # 10,20 conflict; 20,100 → gap=80>=50 OK; 100,200,300 OK

    p1_mask = _greedy_latest_first_filter(positions, spacing)
    p1_positions = positions[p1_mask]

    # Verify strictly ascending
    assert list(p1_positions) == sorted(p1_positions.tolist()), (
        f"P1 positions not ascending: {list(p1_positions)}"
    )


def test_t3_p0_p1_empty_input():
    """T3 (edge): empty position array returns empty mask for both policies."""
    empty = np.array([], dtype=np.int64)
    p0 = _greedy_overlap_filter(empty, 10)
    p1 = _greedy_latest_first_filter(empty, 10)
    assert len(p0) == 0
    assert len(p1) == 0


def test_t3_p0_p1_single_event_always_accepted():
    """T3 (edge): single event is always accepted by both policies."""
    positions = np.array([42], dtype=np.int64)
    p0 = _greedy_overlap_filter(positions, 10)
    p1 = _greedy_latest_first_filter(positions, 10)
    assert list(positions[p0]) == [42]
    assert list(positions[p1]) == [42]


# ===========================================================================
# T4: Metric domain restricted to accepted events
# ===========================================================================

def _make_pre_overlap_result(
    positions: list[int],
    directions: list[float],
    returns: list[float],
) -> _PreOverlapResult:
    """Helper: build a _PreOverlapResult from plain lists."""
    return _PreOverlapResult(
        positions=np.array(positions, dtype=np.int64),
        directions=np.array(directions, dtype=float),
        returns=np.array(returns, dtype=float),
        grid_rows=max(positions) + 1 if positions else 1,
        quality_n=len(positions),
        forward_eligible_n=len(positions),
        overlap_spacing_steps=10,
        gap_extra=None,
        threshold=None,  # not needed for policy-metrics tests
    )


def test_t4_metrics_use_only_accepted_events():
    """T4: mean_signed_bps, hit_rate, mean_abs_move are computed ONLY from accepted events.

    Positions [0, 5, 12] with spacing=10:
      P0 (earliest-first): accept 0, reject 5 (gap=5<10), accept 12 (gap=12>=10) → {0, 12}
      P1 (latest-first):   accept 12, reject 5 (12-5=7<10), accept 0 (12-0=12>=10) → {0, 12}

    Both policies reject position 5.  Event at pos 5 has a large negative return
    (-200) which would contaminate metrics if erroneously included.
    """
    # pos 0: dir=+1, ret=+100 (ACCEPTED by both)
    # pos 5: dir=+1, ret=-200 (REJECTED by both — must NOT affect metrics)
    # pos 12: dir=+1, ret=+100 (ACCEPTED by both)
    pre = _make_pre_overlap_result([0, 5, 12], [1.0, 1.0, 1.0], [100.0, -200.0, 100.0])
    spacing = 10

    p0_mask = _greedy_overlap_filter(pre.positions, spacing)
    p1_mask = _greedy_latest_first_filter(pre.positions, spacing)

    p0_acc = list(map(int, pre.positions[p0_mask]))
    p1_acc = list(map(int, pre.positions[p1_mask]))
    assert p0_acc == [0, 12], f"P0 accepted {p0_acc}"
    assert p1_acc == [0, 12], f"P1 accepted {p1_acc}"

    m0 = _compute_policy_metrics(pre, p0_mask)
    m1 = _compute_policy_metrics(pre, p1_mask)

    # Accepted events {0, 12}: signed returns = [100, 100]
    # mean_signed_bps = 100.0; hit_rate = 1.0; mean_abs_move = 100.0
    # If position 5 were included: mean = (100 - 200 + 100)/3 = 0 ← wrong
    assert m0["mean_signed_bps"] == pytest.approx(100.0)
    assert m0["hit_rate"] == pytest.approx(1.0)
    assert m0["mean_abs_move"] == pytest.approx(100.0)
    # P1 same accepted set → same metrics
    assert m1["mean_signed_bps"] == pytest.approx(100.0)
    assert m1["hit_rate"] == pytest.approx(1.0)
    assert m1["mean_abs_move"] == pytest.approx(100.0)


def test_t4_metrics_differ_when_p0_p1_accept_different_sets():
    """T4: when P0 and P1 accept different events, metrics must differ accordingly."""
    # positions [0, 8, 20], spacing=10
    # P0: accept 0, skip 8 (gap=8<10), accept 20 (gap=12>=10) → {0, 20}
    # P1: descending [20,8,0]: accept 20, accept 8 (20-8=12>=10), skip 0 (8-0=8<10) → {8,20}
    # Returns: pos 0 → ret=+50 (positive), pos 8 → ret=-50 (negative), pos 20 → ret=+50
    # Directions: all +1
    pre = _make_pre_overlap_result([0, 8, 20], [1.0, 1.0, 1.0], [50.0, -50.0, 50.0])
    spacing = 10

    p0_mask = _greedy_overlap_filter(pre.positions, spacing)
    p1_mask = _greedy_latest_first_filter(pre.positions, spacing)

    p0_acc = list(map(int, pre.positions[p0_mask]))
    p1_acc = list(map(int, pre.positions[p1_mask]))
    assert p0_acc == [0, 20], f"P0 accepted {p0_acc}"
    assert p1_acc == [8, 20], f"P1 accepted {p1_acc}"

    m0 = _compute_policy_metrics(pre, p0_mask)
    m1 = _compute_policy_metrics(pre, p1_mask)

    # P0: events {0,20}: signed=[50,50], mean=50, hit=1.0, mam=50
    assert m0["mean_signed_bps"] == pytest.approx(50.0)
    assert m0["hit_rate"] == pytest.approx(1.0)

    # P1: events {8,20}: signed=[-50,50], mean=0, hit=0.5, mam=50
    assert m1["mean_signed_bps"] == pytest.approx(0.0)
    assert m1["hit_rate"] == pytest.approx(0.5)
    assert m1["mean_abs_move"] == pytest.approx(50.0)


def test_t4_metrics_none_when_no_accepted_events():
    """T4: all three metrics are None when accepted_n == 0."""
    pre = _make_pre_overlap_result([], [], [])
    mask = np.zeros(0, dtype=bool)
    m = _compute_policy_metrics(pre, mask)
    assert m["mean_signed_bps"] is None
    assert m["hit_rate"] is None
    assert m["mean_abs_move"] is None


# ===========================================================================
# T5: NEW36 firewall remains enforced on Stage 2 path
# ===========================================================================

def test_t5_stage2_sessions_are_old36_not_new36():
    """T5: STAGE2_SESSIONS must all be in OLD36_REFERENCE_SESSIONS."""
    from checkpoint_registry import OLD36_REFERENCE_SESSIONS, NEW36_SESSION_IDS
    for sid in STAGE2_SESSIONS:
        assert sid in OLD36_REFERENCE_SESSIONS, (
            f"STAGE2 session {sid!r} not in OLD36_REFERENCE_SESSIONS"
        )
        assert sid not in NEW36_SESSION_IDS, (
            f"STAGE2 session {sid!r} found in NEW36_SESSION_IDS — firewall violation"
        )


def test_t5_new36_firewall_still_raises():
    """T5: assert_recovery_allowed still raises for any NEW36 session_id."""
    from checkpoint_registry import NEW36_SESSION_IDS
    from recovery.allowlist import NEW36QuantitativeFirewallError, assert_recovery_allowed
    assert len(NEW36_SESSION_IDS) > 0, "NEW36 registry is empty"
    for sid in NEW36_SESSION_IDS[:2]:   # spot-check first two
        with pytest.raises(NEW36QuantitativeFirewallError):
            assert_recovery_allowed(sid)


def test_t5_stage2_matrix_never_references_new36():
    """T5: expand_stage2_matrix() contains no NEW36 session_ids."""
    from checkpoint_registry import NEW36_SESSION_IDS
    new36_set = set(NEW36_SESSION_IDS)
    for case in expand_stage2_matrix():
        assert case.session_id not in new36_set, (
            f"Stage2 matrix contains NEW36 session {case.session_id!r}"
        )


# ===========================================================================
# T6: Stage 2 module does not mutate protected / frozen source
# ===========================================================================

def test_t6_stage2_module_does_not_reference_forbidden_symbols():
    """T6: v1_2_stage2.py source must not call run_regression() or import from goldens.

    Note: string literals that mention artifact names (e.g. in _PROTECTED_NAMES) are
    intentional — they prevent Stage 2 from overwriting those files.  The test checks
    for *code-level* forbidden references: actual imports or function calls.
    """
    import recovery.v1_2_stage2 as mod
    src = inspect.getsource(mod)
    # Forbidden: actual function calls or import statements (not string literals)
    forbidden_code = [
        "run_regression(",       # function call
        "from .goldens import",  # direct golden import
        "import goldens",        # module import
    ]
    for token in forbidden_code:
        assert token not in src, (
            f"Stage2 module contains forbidden code reference: {token!r}"
        )


def test_t6_importing_stage2_does_not_alter_engine_module():
    """T6: importing v1_2_stage2 must not mutate any attribute of engine.py."""
    import recovery.engine as engine_before
    attrs_before = {k: id(v) for k, v in vars(engine_before).items()
                    if not k.startswith("__")}

    import recovery.v1_2_stage2  # noqa: F401 — import for side-effect check

    attrs_after = {k: id(v) for k, v in vars(engine_before).items()
                   if not k.startswith("__")}
    assert attrs_before == attrs_after, (
        "Importing v1_2_stage2 mutated engine module attributes"
    )


def test_t6_stage2_does_not_call_run_regression():
    """T6: v1_2_stage2 source contains no call to run_regression() at all."""
    import recovery.v1_2_stage2 as mod
    src = inspect.getsource(mod)
    assert "run_regression" not in src


# ===========================================================================
# Additional: empty fingerprint (N=0)
# ===========================================================================

def test_empty_fingerprint_is_sha256_of_zero_length_bytes():
    """Empty position set → SHA256 over zero-length byte string (exact Stage 1 convention)."""
    expected = hashlib.sha256(b"").hexdigest()
    result = _sha256_positions(np.array([], dtype=np.int64))
    assert result == expected, f"empty fingerprint mismatch: {result!r} != {expected!r}"


def test_fingerprint_ascending_little_endian_int64():
    """Fingerprint sorts ascending, encodes as little-endian int64, returns lowercase hex."""
    # Two positions in reverse order — fingerprint must sort them first.
    positions_unordered = np.array([20, 10], dtype=np.int64)
    positions_ordered = np.array([10, 20], dtype=np.int64)

    fp_unordered = _sha256_positions(positions_unordered)
    fp_ordered = _sha256_positions(positions_ordered)
    assert fp_unordered == fp_ordered, "fingerprint must be order-independent (sorts internally)"

    # Verify encoding: [10, 20] as little-endian int64
    raw = np.array([10, 20], dtype="<i8").tobytes()
    expected = hashlib.sha256(raw).hexdigest()
    assert fp_ordered == expected


# ===========================================================================
# Additional: N=0 / N=1 conflict diagnostic edge cases
# ===========================================================================

def test_pre_overlap_diagnostics_n_zero():
    """N=0: all gap/conflict fields are null, maximum_cluster=0."""
    d = compute_pre_overlap_diagnostics(np.array([], dtype=np.int64), spacing=10)
    assert d["pre_overlap_event_n"] == 0
    assert d["first_event_grid_pos"] is None
    assert d["last_event_grid_pos"] is None
    assert d["gap_steps_min"] is None
    assert d["gap_steps_median"] is None
    assert d["gap_steps_max"] is None
    assert d["conflicting_pair_n"] == 0
    assert d["fraction_events_with_neighbor_inside_spacing"] == 0.0
    assert d["maximum_local_cluster_size"] == 0


def test_pre_overlap_diagnostics_n_one():
    """N=1: no gap/conflict statistics possible; cluster size is 1."""
    d = compute_pre_overlap_diagnostics(np.array([42], dtype=np.int64), spacing=10)
    assert d["pre_overlap_event_n"] == 1
    assert d["first_event_grid_pos"] == 42
    assert d["last_event_grid_pos"] == 42
    assert d["gap_steps_min"] is None
    assert d["gap_steps_median"] is None
    assert d["gap_steps_max"] is None
    assert d["conflicting_pair_n"] == 0
    assert d["fraction_events_with_neighbor_inside_spacing"] == 0.0
    assert d["maximum_local_cluster_size"] == 1


def test_pre_overlap_diagnostics_no_conflicts():
    """All positions well-separated: zero conflicts, fraction=0, cluster=1."""
    # All gaps = 20 >= spacing=10
    positions = np.arange(0, 100, 20, dtype=np.int64)   # [0,20,40,60,80]
    d = compute_pre_overlap_diagnostics(positions, spacing=10)
    assert d["conflicting_pair_n"] == 0
    assert d["fraction_events_with_neighbor_inside_spacing"] == 0.0
    assert d["maximum_local_cluster_size"] == 1


def test_pre_overlap_diagnostics_all_conflict():
    """All positions within spacing of each other: all-conflict case."""
    # [0,1,2,3,4], spacing=10: every pair conflicts
    positions = np.array([0, 1, 2, 3, 4], dtype=np.int64)
    d = compute_pre_overlap_diagnostics(positions, spacing=10)
    n = 5
    expected_pairs = n * (n - 1) // 2   # C(5,2) = 10
    assert d["conflicting_pair_n"] == expected_pairs
    assert d["fraction_events_with_neighbor_inside_spacing"] == pytest.approx(1.0)
    assert d["maximum_local_cluster_size"] == n


def test_pre_overlap_diagnostics_exact_boundary_not_conflicting():
    """Positions exactly spacing apart are NOT conflicting (equality → accepted)."""
    positions = np.array([0, 10, 20], dtype=np.int64)   # gaps all == spacing=10
    d = compute_pre_overlap_diagnostics(positions, spacing=10)
    assert d["conflicting_pair_n"] == 0
    assert d["fraction_events_with_neighbor_inside_spacing"] == 0.0
    assert d["maximum_local_cluster_size"] == 1


# ===========================================================================
# Additional: transitive connected-component (cluster) test
# ===========================================================================

def test_pre_overlap_diagnostics_transitive_cluster():
    """Transitivity: [0,5,11] with spacing=10 — 0 and 11 are NOT in same cluster
    even though 0→5 and 5→11 individually.  But 5→11 gap=6<10 so they share cluster.
    0→5 gap=5<10 so 0,5,11 all in same cluster (transitive via 5)."""
    # 0-5: gap=5<10 → edge
    # 5-11: gap=6<10 → edge
    # 0-11: gap=11>=10 → no direct edge, but connected via 5
    # Connected component: {0, 5, 11} → size 3
    positions = np.array([0, 5, 11], dtype=np.int64)
    d = compute_pre_overlap_diagnostics(positions, spacing=10)
    assert d["maximum_local_cluster_size"] == 3
    # Pairs: (0,5)→5<10 conflict, (5,11)→6<10 conflict, (0,11)→11>=10 NOT conflict
    assert d["conflicting_pair_n"] == 2


def test_pre_overlap_diagnostics_two_separate_clusters():
    """Two distinct clusters: [0,5,30,35] with spacing=10."""
    # Cluster 1: [0,5] (gap=5<10)
    # Cluster 2: [30,35] (gap=5<10)
    # Gap between clusters: 30-5=25>=10
    positions = np.array([0, 5, 30, 35], dtype=np.int64)
    d = compute_pre_overlap_diagnostics(positions, spacing=10)
    assert d["maximum_local_cluster_size"] == 2  # both clusters same size
    assert d["conflicting_pair_n"] == 2           # one pair per cluster


def test_pre_overlap_diagnostics_gap_statistics_even_n():
    """Gap median for even number of gaps = arithmetic mean of two central values."""
    # positions [0,10,30,50] → gaps [10,20,20] (3 gaps, odd → middle)
    # Actually 4 positions → 3 gaps → odd → middle value
    # Let's use 5 positions → 4 gaps (even)
    positions = np.array([0, 10, 30, 60, 100], dtype=np.int64)
    # gaps: [10, 20, 30, 40] → sorted [10,20,30,40] → median = (20+30)/2 = 25.0
    d = compute_pre_overlap_diagnostics(positions, spacing=5)
    assert d["gap_steps_min"] == 10
    assert d["gap_steps_max"] == 40
    assert d["gap_steps_median"] == pytest.approx(25.0)


def test_pre_overlap_diagnostics_gap_statistics_odd_n():
    """Gap median for odd number of gaps = middle sorted value."""
    # 4 positions → 3 gaps
    positions = np.array([0, 10, 40, 100], dtype=np.int64)
    # gaps: [10, 30, 60] → sorted [10,30,60] → median = 30
    d = compute_pre_overlap_diagnostics(positions, spacing=5)
    assert d["gap_steps_min"] == 10
    assert d["gap_steps_max"] == 60
    assert d["gap_steps_median"] == pytest.approx(30.0)


# ===========================================================================
# Additional: I6 accounting invariant
# ===========================================================================

def test_accounting_invariant_i6_holds_for_synthetic_rows():
    """I6: pre_overlap_event_n == accepted_n + overlap_dropped_n for every row."""
    contexts = _make_all_contexts(seed_base=500)
    rows = generate_stage2_rows(contexts=contexts)
    for row in rows:
        n_pre = row["pre_overlap_event_n"]
        n_acc = row["accepted_n"]
        n_drop = row["overlap_dropped_n"]
        assert n_acc + n_drop == n_pre, (
            f"I6 fail for {row['session_id']}/{row['asset']}/"
            f"{row['feature']}/{row['horizon_ms']}/{row['policy']}: "
            f"acc={n_acc} drop={n_drop} pre={n_pre}"
        )


# ===========================================================================
# Additional: I7 subset invariant
# ===========================================================================

def test_subset_invariant_i7_accepted_subset_of_pre_overlap():
    """I7: accepted_positions_sha256 must correspond to positions that are a subset of
    the pre-overlap positions (verified via count <= pre_overlap_event_n)."""
    contexts = _make_all_contexts(seed_base=600)
    rows = generate_stage2_rows(contexts=contexts)
    for row in rows:
        assert row["accepted_n"] <= row["pre_overlap_event_n"], (
            f"I7 fail: accepted_n {row['accepted_n']} > pre_overlap_event_n "
            f"{row['pre_overlap_event_n']} for "
            f"{row['feature']}/{row['horizon_ms']}/{row['policy']}"
        )


# ===========================================================================
# Additional: I8 spacing invariant (via _greedy_latest_first_filter)
# ===========================================================================

def test_spacing_invariant_i8_p1_all_accepted_pairs_satisfy_spacing():
    """I8: all unordered P1 accepted pairs must satisfy abs(pos_i - pos_j) >= spacing."""
    rng = np.random.default_rng(77)
    for spacing in (10, 50, 300):
        # Randomly sampled positions
        raw = np.sort(rng.integers(0, 1000, size=50)).astype(np.int64)
        mask = _greedy_latest_first_filter(raw, spacing)
        accepted = raw[mask]
        if len(accepted) > 1:
            min_gap = int(np.min(np.diff(accepted)))
            assert min_gap >= spacing, (
                f"I8 FAIL for spacing={spacing}: min_gap={min_gap}"
            )


def test_spacing_invariant_i8_p0_all_accepted_pairs_satisfy_spacing():
    """I8: same check for P0."""
    rng = np.random.default_rng(88)
    for spacing in (10, 50, 300):
        raw = np.sort(rng.integers(0, 1000, size=50)).astype(np.int64)
        mask = _greedy_overlap_filter(raw, spacing)
        accepted = raw[mask]
        if len(accepted) > 1:
            min_gap = int(np.min(np.diff(accepted)))
            assert min_gap >= spacing, (
                f"I8 FAIL for spacing={spacing}: min_gap={min_gap}"
            )


# ===========================================================================
# Additional: I10 / I11 in generation
# ===========================================================================

def test_i10_i11_via_generate_stage2_rows():
    """I10+I11: generate_stage2_rows enforces both invariants (no duplicates, 144 rows)."""
    contexts = _make_all_contexts(seed_base=700)
    rows = generate_stage2_rows(contexts=contexts)
    # I11
    assert len(rows) == STAGE2_POLICY_ROWS
    # I10: each case key appears exactly twice (once P0, once P1)
    from collections import Counter
    key_counts: Counter = Counter(
        (r["session_id"], r["asset"], r["feature"], r["horizon_ms"], r["q"])
        for r in rows
    )
    for key, cnt in key_counts.items():
        assert cnt == 2, f"case {key} appears {cnt} times (expected 2: P0+P1)"


# ===========================================================================
# Additional: spacing constants match declared horizons
# ===========================================================================

def test_spacing_constants_match_spec():
    """Frozen spacing semantics: spacing_steps = max(10, k) for declared horizons."""
    assert max(10, 1000 // GRID_MS) == 10
    assert max(10, 5000 // GRID_MS) == 50
    assert max(10, 30000 // GRID_MS) == 300


# ===========================================================================
# Additional: _extract_pre_overlap produces consistent pre_overlap_event_n
# ===========================================================================

def test_extract_pre_overlap_consistency_for_all_families():
    """_extract_pre_overlap returns a _PreOverlapResult where positions/directions/returns
    arrays have the same length for all Stage 2 feature families."""
    ctx = _make_ctx("20260905T073818Z_e44d99bd", "BTC", n=400, seed=42)
    for feature in STAGE2_FAMILIES:
        for h in STAGE2_HORIZONS:
            result = _extract_pre_overlap(feature, ctx, h, STAGE2_Q)
            n = len(result.positions)
            assert len(result.directions) == n, (
                f"directions length mismatch for {feature}/{h}"
            )
            assert len(result.returns) == n, (
                f"returns length mismatch for {feature}/{h}"
            )
            # All returns must be finite
            if n > 0:
                assert np.all(np.isfinite(result.returns)), (
                    f"non-finite returns for {feature}/{h}"
                )


def test_extract_pre_overlap_gap_extra_only_for_gap_depth_extofi():
    """gap_extra is non-None only for gap_depth_extOFI."""
    ctx = _make_ctx("20260905T073818Z_e44d99bd", "BTC", n=300, seed=13)
    for feature in STAGE2_FAMILIES:
        result = _extract_pre_overlap(feature, ctx, 1000, STAGE2_Q)
        if feature == "gap_depth_extOFI":
            assert result.gap_extra is not None, "gap_extra must be set for gap_depth_extOFI"
            # Verify all 10 extra fields are present
            for field in _I12_GAP_EXTRA_FIELDS:
                assert field in result.gap_extra, f"missing gap_extra field: {field}"
        else:
            assert result.gap_extra is None, (
                f"gap_extra must be None for {feature}"
            )


# ===========================================================================
# Additional: schema completeness
# ===========================================================================

def test_all_rows_have_consistent_schema():
    """All 144 rows share the same key set."""
    contexts = _make_all_contexts(seed_base=800)
    rows = generate_stage2_rows(contexts=contexts)
    assert len(rows) > 0
    reference_keys = set(rows[0].keys())
    for row in rows[1:]:
        assert set(row.keys()) == reference_keys, (
            f"Row schema mismatch: {set(row.keys()) ^ reference_keys}"
        )


def test_row_schema_contains_required_fields():
    """Each row contains all required identification, pre-overlap, policy, and metric fields."""
    contexts = _make_all_contexts(seed_base=900)
    rows = generate_stage2_rows(contexts=contexts)
    required = {
        # identification
        "diagnostic_version", "session_id", "asset", "feature",
        "horizon_ms", "q", "policy",
        # grid info
        "grid_rows", "quality_n", "forward_eligible_n", "overlap_spacing_steps",
        # pre-overlap
        "pre_overlap_event_n", "first_event_grid_pos", "last_event_grid_pos",
        "pre_overlap_positions_sha256",
        "gap_steps_min", "gap_steps_median", "gap_steps_max",
        "conflicting_pair_n", "fraction_events_with_neighbor_inside_spacing",
        "maximum_local_cluster_size",
        # policy results
        "accepted_n", "overlap_dropped_n",
        "accepted_positions_count", "accepted_positions_sha256",
        "accepted_first_grid_pos", "accepted_last_grid_pos",
        "mean_signed_bps", "hit_rate", "mean_abs_move",
        # gap extras (None for non-gap_depth_extOFI)
        "gap_threshold_crossing_n", "confirmation_finite_n",
        "alignment_true_n", "aligned_forward_valid_n",
        "confirmation_component_positive_n", "confirmation_component_negative_n",
        "direction_positive_n", "direction_negative_n",
        "aligned_positive_pair_n", "aligned_negative_pair_n",
    }
    for row in rows:
        missing = required - set(row.keys())
        assert not missing, f"Row missing required fields: {missing}"


def test_gap_depth_extofi_rows_have_non_none_gap_extras():
    """gap_depth_extOFI rows have non-None gap extra fields;
    all other feature rows have None for those fields."""
    contexts = _make_all_contexts(seed_base=1000)
    rows = generate_stage2_rows(contexts=contexts)
    for row in rows:
        if row["feature"] == "gap_depth_extOFI":
            for field in _I12_GAP_EXTRA_FIELDS:
                assert row[field] is not None, (
                    f"gap_depth_extOFI row missing {field}"
                )
        else:
            for field in _I12_GAP_EXTRA_FIELDS:
                assert row[field] is None, (
                    f"Non-gap row has non-None {field}: {row['feature']}"
                )



# ===========================================================================
# Patch 1 helpers
# ===========================================================================

def _make_ctx_with_metrics(
    feature: str,
    horizon_ms: int,
    N: int,
    threshold: "float | None",
    mean_signed_bps: "float | None",
    hit_rate: "float | None",
    mean_abs_move: "float | None" = 0.0,
    seed: int = 42,
) -> _BlockContext:
    """Build a synthetic _BlockContext pre-populated with a matching BlockMetrics entry."""
    ctx = _make_ctx(STAGE2_SESSIONS[0], "BTC", seed=seed)
    bm = BlockMetrics(
        session_id=STAGE2_SESSIONS[0],
        asset="BTC",
        feature=feature,
        horizon_ms=horizon_ms,
        q=STAGE2_Q,
        N=N,
        threshold=threshold,
        mean_signed_bps=mean_signed_bps,
        hit_rate=hit_rate,
        median_signed_bps=None,
        mean_abs_move=mean_abs_move,
    )
    ctx.metrics[(feature, horizon_ms, STAGE2_Q)] = bm
    return ctx


# ===========================================================================
# R1: Final artifact writer tests
# ===========================================================================

def test_r1a_write_creates_csv_and_manifest(tmp_path):
    """R1-A: write_stage2_artifacts creates both CSV and manifest on first call."""
    contexts = _make_all_contexts(seed_base=1100)
    rows = generate_stage2_rows(contexts=contexts)

    csv_path, manifest_path = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)

    assert csv_path.exists(), "CSV file not created"
    assert manifest_path.exists(), "Manifest file not created"
    assert csv_path.name == "stage2_policy_diagnostics.csv"
    assert manifest_path.name == "stage2_manifest.json"


def test_r1b_second_call_raises_file_exists_error(tmp_path):
    """R1-B: second call to write_stage2_artifacts raises FileExistsError."""
    contexts = _make_all_contexts(seed_base=1200)
    rows = generate_stage2_rows(contexts=contexts)

    write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)

    with pytest.raises(FileExistsError):
        write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)


def test_r1c_csv_columns_in_fixed_deterministic_order(tmp_path):
    """R1-C: CSV columns match _STAGE2_CSV_COLUMNS exactly (fixed deterministic order)."""
    import pandas as _pd
    contexts = _make_all_contexts(seed_base=1300)
    rows = generate_stage2_rows(contexts=contexts)

    csv_path, _ = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)

    df = _pd.read_csv(csv_path)
    assert list(df.columns) == list(_STAGE2_CSV_COLUMNS), (
        f"Column order mismatch.\nExpected: {list(_STAGE2_CSV_COLUMNS)}\nGot: {list(df.columns)}"
    )


def test_r1d_manifest_top_level_keys_are_sorted(tmp_path):
    """R1-D: manifest JSON top-level keys are sorted (sort_keys=True serialisation)."""
    contexts = _make_all_contexts(seed_base=1400)
    rows = generate_stage2_rows(contexts=contexts)

    _, manifest_path = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)

    manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))
    keys = list(manifest.keys())
    assert keys == sorted(keys), f"Manifest top-level keys not sorted: {keys}"


def test_r1e_existing_csv_alone_raises_file_exists_error(tmp_path):
    """R1-E: if CSV already exists (manifest absent), FileExistsError is raised."""
    contexts = _make_all_contexts(seed_base=1500)
    rows = generate_stage2_rows(contexts=contexts)

    # Pre-create only the CSV
    (tmp_path / "stage2_policy_diagnostics.csv").write_text("dummy")

    with pytest.raises(FileExistsError):
        write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)


def test_r1f_existing_manifest_alone_raises_file_exists_error(tmp_path):
    """R1-F: if manifest already exists (CSV absent), FileExistsError is raised."""
    contexts = _make_all_contexts(seed_base=1600)
    rows = generate_stage2_rows(contexts=contexts)

    # Pre-create only the manifest
    (tmp_path / "stage2_manifest.json").write_text("{}")

    with pytest.raises(FileExistsError):
        write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)


def test_r1g_none_values_produce_empty_csv_fields(tmp_path):
    """R1-G: None row values appear as empty fields (not 'None' or 'nan') in the CSV."""
    contexts = _make_all_contexts(seed_base=1700)
    rows = generate_stage2_rows(contexts=contexts)

    csv_path, _ = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)

    # For non-gap_depth_extOFI rows the gap-extra fields must be None → empty string.
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv_module.DictReader(f)
        found_non_gap = False
        for row in reader:
            if row["feature"] != "gap_depth_extOFI":
                assert row["gap_threshold_crossing_n"] == "", (
                    f"Expected empty string for None field, "
                    f"got: {row['gap_threshold_crossing_n']!r}"
                )
                found_non_gap = True
                break
        assert found_non_gap, "No non-gap_depth_extOFI row found in CSV"


def test_r1h_manifest_csv_sha256_matches_written_file(tmp_path):
    """R1-H: manifest csv_sha256 == SHA256 of the exact bytes written to disk."""
    import hashlib as _hl
    contexts = _make_all_contexts(seed_base=1800)
    rows = generate_stage2_rows(contexts=contexts)

    csv_path, manifest_path = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)

    csv_bytes = csv_path.read_bytes()
    actual_sha256 = _hl.sha256(csv_bytes).hexdigest()

    manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["csv_sha256"] == actual_sha256, (
        f"SHA256 mismatch: manifest={manifest['csv_sha256']!r} actual={actual_sha256!r}"
    )


def test_r1i_manifest_csv_byte_size_matches_actual(tmp_path):
    """R1-I: manifest csv_byte_size == actual byte size of the written CSV file."""
    contexts = _make_all_contexts(seed_base=1900)
    rows = generate_stage2_rows(contexts=contexts)

    csv_path, manifest_path = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)

    actual_byte_size = csv_path.stat().st_size
    manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["csv_byte_size"] == actual_byte_size, (
        f"Byte size mismatch: manifest={manifest['csv_byte_size']} actual={actual_byte_size}"
    )


def test_r1j_manifest_complete_required_field_set(tmp_path):
    """R1-J: manifest contains the complete required FINAL/DRAFT3 field set."""
    _REQUIRED_MANIFEST_KEYS = {
        "GRID_MS",
        "P0_definition",
        "P1_definition",
        "actual_row_count",
        "actual_unique_case_count",
        "assets",
        "candidate_generation_frozen_before_golden_comparison",
        "csv_byte_size",
        "csv_sha256",
        "diagnostic_version",
        "engine_py_modified",
        "expected_row_count",
        "expected_unique_case_count",
        "feature_families",
        "fingerprint_encoding_definition",
        "gap_depth_extOFI_diagnostic_counters",
        "historical_target_numerics_read_by_generator",
        "horizons_ms",
        "k_values",
        "mean_abs_move_domain",
        "new36_opened",
        "policy_ids",
        "q",
        "runtime_source_commit",
        "session_ids",
        "spacing_boundary_semantics",
        "spacing_steps",
        "stage1_artifacts_modified",
        "v1_1_reports_modified",
    }

    contexts = _make_all_contexts(seed_base=2000)
    rows = generate_stage2_rows(contexts=contexts)

    _, manifest_path = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)
    manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))

    missing = _REQUIRED_MANIFEST_KEYS - set(manifest.keys())
    assert not missing, f"Manifest missing required fields: {sorted(missing)}"


def test_r1k_manifest_gap_depth_extofi_scope_dict_present_and_correct(tmp_path):
    """R1-K: gap_depth_extOFI_diagnostic_counters has all 10 fields with correct SCOPE labels."""
    contexts = _make_all_contexts(seed_base=2100)
    rows = generate_stage2_rows(contexts=contexts)

    _, manifest_path = write_stage2_artifacts(rows, "abc1234", output_dir=tmp_path)
    manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))

    scope_dict = manifest.get("gap_depth_extOFI_diagnostic_counters")
    assert scope_dict is not None, "gap_depth_extOFI_diagnostic_counters missing from manifest"
    assert isinstance(scope_dict, dict)

    # All 10 _I12_GAP_EXTRA_FIELDS must be present.
    for field in _I12_GAP_EXTRA_FIELDS:
        assert field in scope_dict, f"Missing counter in scope dict: {field}"
        entry = scope_dict[field]
        assert "scope" in entry, f"Missing 'scope' key for counter: {field}"
        assert "domain" in entry, f"Missing 'domain' key for counter: {field}"
        assert entry["scope"] in ("GRID-SCOPE", "EVENT-SCOPE"), (
            f"Invalid scope value for {field}: {entry['scope']!r}"
        )

    # Exactly one field is GRID-SCOPE: confirmation_finite_n.
    grid_scope = [f for f in _I12_GAP_EXTRA_FIELDS if scope_dict[f]["scope"] == "GRID-SCOPE"]
    assert grid_scope == ["confirmation_finite_n"], (
        f"Expected only 'confirmation_finite_n' to be GRID-SCOPE, got: {grid_scope}"
    )

    # All remaining nine are EVENT-SCOPE.
    event_scope = [f for f in _I12_GAP_EXTRA_FIELDS if scope_dict[f]["scope"] == "EVENT-SCOPE"]
    assert len(event_scope) == 9


# ===========================================================================
# R2: Real I12 Enforcement (actual emitted field comparison) tests
# ===========================================================================

def test_r2a_i12_actual_comparison_passes_on_equal_shared_fields():
    """R2-A: _assert_i12_actual_field_comparison passes when P0 == P1 on all 10 shared fields."""
    shared: dict = {
        "feature": "bitget_ofi",
        "pre_overlap_event_n": 5,
        "first_event_grid_pos": 10,
        "last_event_grid_pos": 100,
        "pre_overlap_positions_sha256": "abc123def456",
        "gap_steps_min": 5,
        "gap_steps_median": 10.0,
        "gap_steps_max": 20,
        "conflicting_pair_n": 2,
        "fraction_events_with_neighbor_inside_spacing": 0.4,
        "maximum_local_cluster_size": 2,
    }
    row_p0 = dict(shared)
    row_p1 = dict(shared)

    # Must not raise.
    _assert_i12_actual_field_comparison(row_p0, row_p1)


def test_r2b_i12_raises_assertionerror_on_shared_field_mismatch():
    """R2-B: I12 raises AssertionError('I12 FAIL ...') when a shared pre-overlap field differs."""
    base: dict = {
        "feature": "bitget_ofi",
        "pre_overlap_event_n": 5,
        "first_event_grid_pos": 10,
        "last_event_grid_pos": 100,
        "pre_overlap_positions_sha256": "abc123def456",
        "gap_steps_min": 5,
        "gap_steps_median": 10.0,
        "gap_steps_max": 20,
        "conflicting_pair_n": 2,
        "fraction_events_with_neighbor_inside_spacing": 0.4,
        "maximum_local_cluster_size": 2,
    }
    row_p0 = dict(base)
    row_p1 = dict(base)
    row_p1["pre_overlap_event_n"] = 99  # Deliberate mismatch.

    with pytest.raises(AssertionError, match="I12 FAIL"):
        _assert_i12_actual_field_comparison(row_p0, row_p1)


def test_r2c_i12_raises_assertionerror_on_gap_extra_field_mismatch():
    """R2-C: I12 raises AssertionError('I12 FAIL ...') on gap_depth_extOFI extra field mismatch."""
    shared: dict = {
        "feature": "gap_depth_extOFI",
        "pre_overlap_event_n": 10,
        "first_event_grid_pos": 5,
        "last_event_grid_pos": 200,
        "pre_overlap_positions_sha256": "xyz789",
        "gap_steps_min": 8,
        "gap_steps_median": 15.0,
        "gap_steps_max": 30,
        "conflicting_pair_n": 3,
        "fraction_events_with_neighbor_inside_spacing": 0.3,
        "maximum_local_cluster_size": 2,
        **{f: 10 for f in _I12_GAP_EXTRA_FIELDS},
    }
    row_p0 = dict(shared)
    row_p1 = dict(shared)
    row_p1["gap_threshold_crossing_n"] = 99  # Deliberate mismatch on gap extra.

    with pytest.raises(AssertionError, match="I12 FAIL"):
        _assert_i12_actual_field_comparison(row_p0, row_p1)


def test_r2_i12_passes_on_equal_gap_extra_fields():
    """R2 (gap extra): I12 passes when all 10 gap_depth_extOFI extra fields are equal."""
    shared: dict = {
        "feature": "gap_depth_extOFI",
        "pre_overlap_event_n": 10,
        "first_event_grid_pos": 5,
        "last_event_grid_pos": 200,
        "pre_overlap_positions_sha256": "xyz789",
        "gap_steps_min": 8,
        "gap_steps_median": 15.0,
        "gap_steps_max": 30,
        "conflicting_pair_n": 3,
        "fraction_events_with_neighbor_inside_spacing": 0.3,
        "maximum_local_cluster_size": 2,
        **{f: 10 for f in _I12_GAP_EXTRA_FIELDS},
    }
    row_p0 = dict(shared)
    row_p1 = dict(shared)

    _assert_i12_actual_field_comparison(row_p0, row_p1)  # Must not raise.


def test_r2_i12_integration_via_generate_stage2_rows():
    """R2 (integration): generate_stage2_rows with synthetic contexts passes R2 check end-to-end."""
    contexts = _make_all_contexts(seed_base=2200)
    # If R2 fails, generate_stage2_rows will raise AssertionError — must not raise here.
    rows = generate_stage2_rows(contexts=contexts)
    assert len(rows) == STAGE2_POLICY_ROWS


# ===========================================================================
# R3: Frozen Engine P0 Drift Guard tests
# ===========================================================================

def test_r3a_drift_guard_passes_on_exact_match():
    """R3-A: drift guard passes when P0 output exactly matches ctx.metrics."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx_with_metrics(
        feature, horizon_ms,
        N=7, threshold=0.25, mean_signed_bps=3.5, hit_rate=0.7,
    )
    p0_row = {"accepted_n": 7, "mean_signed_bps": 3.5, "hit_rate": 0.7}

    _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)


def test_r3b_drift_guard_raises_on_threshold_mismatch():
    """R3-B: drift guard raises AssertionError('R3 DRIFT ...') on threshold mismatch."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx_with_metrics(
        feature, horizon_ms,
        N=7, threshold=0.25, mean_signed_bps=3.5, hit_rate=0.7,
    )
    p0_row = {"accepted_n": 7, "mean_signed_bps": 3.5, "hit_rate": 0.7}

    # P0 computed threshold differs from ctx.metrics.threshold.
    with pytest.raises(AssertionError, match="R3 DRIFT"):
        _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.30)


def test_r3c_drift_guard_raises_on_mean_signed_bps_mismatch():
    """R3-C: drift guard raises AssertionError('R3 DRIFT ...') on mean_signed_bps mismatch."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx_with_metrics(
        feature, horizon_ms,
        N=7, threshold=0.25, mean_signed_bps=3.5, hit_rate=0.7,
    )
    # P0 row reports a different mean_signed_bps.
    p0_row = {"accepted_n": 7, "mean_signed_bps": 5.0, "hit_rate": 0.7}

    with pytest.raises(AssertionError, match="R3 DRIFT"):
        _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)


def test_r3d_drift_guard_raises_on_hit_rate_mismatch():
    """R3-D: drift guard raises AssertionError('R3 DRIFT ...') on hit_rate mismatch."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx_with_metrics(
        feature, horizon_ms,
        N=7, threshold=0.25, mean_signed_bps=3.5, hit_rate=0.7,
    )
    p0_row = {"accepted_n": 7, "mean_signed_bps": 3.5, "hit_rate": 0.9}  # wrong hit_rate

    with pytest.raises(AssertionError, match="R3 DRIFT"):
        _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)


def test_r3e_mean_abs_move_excluded_no_assertion_raised():
    """R3-E: mean_abs_move is NOT checked by R3 — no assertion even when it differs."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx(STAGE2_SESSIONS[0], "BTC")
    # Create BlockMetrics with a deliberately wrong mean_abs_move.
    bm = BlockMetrics(
        session_id=STAGE2_SESSIONS[0],
        asset="BTC",
        feature=feature,
        horizon_ms=horizon_ms,
        q=STAGE2_Q,
        N=7,
        threshold=0.25,
        mean_signed_bps=3.5,
        hit_rate=0.7,
        median_signed_bps=None,
        mean_abs_move=999.0,  # Deliberately set to a wrong value.
    )
    ctx.metrics[(feature, horizon_ms, STAGE2_Q)] = bm

    # P0 row has a different mean_abs_move — R3 must NOT raise.
    p0_row = {
        "accepted_n": 7,
        "mean_signed_bps": 3.5,
        "hit_rate": 0.7,
        "mean_abs_move": 1.0,  # Differs from bm.mean_abs_move=999.0
    }

    # Should NOT raise (mean_abs_move is excluded from R3).
    _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)


def test_r3f_drift_guard_raises_on_N_mismatch():
    """R3-F: drift guard raises AssertionError('R3 DRIFT ...') on N (accepted_n) mismatch."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx_with_metrics(
        feature, horizon_ms,
        N=10, threshold=0.25, mean_signed_bps=3.5, hit_rate=0.7,
    )
    p0_row = {"accepted_n": 7, "mean_signed_bps": 3.5, "hit_rate": 0.7}  # N=7 != 10

    with pytest.raises(AssertionError, match="R3 DRIFT"):
        _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)


def test_r3_skips_silently_when_metrics_empty():
    """R3 (skip): drift guard does nothing when ctx.metrics is empty (synthetic contexts)."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx(STAGE2_SESSIONS[0], "BTC")
    assert not ctx.metrics, "Expected empty metrics for synthetic context"

    p0_row = {"accepted_n": 7, "mean_signed_bps": 3.5, "hit_rate": 0.7}
    # Must not raise (ctx.metrics has no matching key).
    _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)


def test_r3_schema_guard_raises_on_missing_attribute():
    """R3 (schema): STAGE2_PATCH1_FAIL_R3_SCHEMA raised if metrics object lacks required attr."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    ctx = _make_ctx(STAGE2_SESSIONS[0], "BTC")

    # Inject a mock object that is missing the 'N' attribute.
    class _BadMetrics:
        threshold = 0.5
        mean_signed_bps = 1.0
        hit_rate = 0.6
        # N is intentionally absent.

    ctx.metrics[(feature, horizon_ms, STAGE2_Q)] = _BadMetrics()

    p0_row = {"accepted_n": 5, "mean_signed_bps": 1.0, "hit_rate": 0.6}

    with pytest.raises(AssertionError, match="STAGE2_PATCH1_FAIL_R3_SCHEMA"):
        _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.5)


def test_r3_tolerance_within_1e12_passes():
    """R3 (numeric): difference well within 1e-12 tolerance passes for mean_signed_bps."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    val = 3.5
    ctx = _make_ctx_with_metrics(
        feature, horizon_ms,
        N=7, threshold=0.25, mean_signed_bps=val, hit_rate=0.7,
    )
    # Difference of 1e-13 (well within the 1e-12 tolerance) must pass.
    p0_row = {"accepted_n": 7, "mean_signed_bps": val + 1e-13, "hit_rate": 0.7}

    _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)


def test_r3_tolerance_exceeds_1e12_fails():
    """R3 (numeric): difference > 1e-12 raises AssertionError for mean_signed_bps."""
    feature = "bitget_ofi"
    horizon_ms = 1000

    val = 3.5
    ctx = _make_ctx_with_metrics(
        feature, horizon_ms,
        N=7, threshold=0.25, mean_signed_bps=val, hit_rate=0.7,
    )
    p0_row = {"accepted_n": 7, "mean_signed_bps": val + 2e-12, "hit_rate": 0.7}

    with pytest.raises(AssertionError, match="R3 DRIFT"):
        _assert_r3_p0_drift_guard(ctx, feature, horizon_ms, p0_row, threshold=0.25)
