"""Synthetic tests for V1.2 Stage 1 candidate-only diagnostics.

No real OLD36 RAW is opened and no historical target output is compared.
"""
from __future__ import annotations

import inspect
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from checkpoint_registry import NEW36_SESSION_IDS
from recovery.allowlist import NEW36QuantitativeFirewallError, assert_recovery_allowed
from recovery.engine import FEATURE_MAP, SIMPLE_FEATURES
from recovery.v1_2_diagnostics import (
    ASSETS,
    DECLARED_SESSIONS,
    DIAGNOSTIC_VERSION,
    EXPECTED_ROW_COUNT,
    DiagnosticCase,
    _prepare_block,
    diagnose_case,
    expand_fixed_matrix,
    fingerprint_positions,
    write_stage1_artifacts,
)


def _synthetic_grid(n: int = 360) -> pd.DataFrame:
    i = np.arange(n, dtype=float)
    # Smooth positive mid series with horizon-dependent forward moves.
    mid = 100.0 + 0.01 * i + 0.001 * np.sin(i / 7.0)
    d = {
        "local_ts_ms": (i * 100).astype(np.int64),
        "sample_monotonic_ns": (i * 100_000_000 + 1).astype(np.int64),
        "bitget_mid": mid,
        "bitget_book_age_recv_ms": np.full(n, 100.0),
        "adjusted_fair_venue_count": np.full(n, 3.0),
        "asset": np.full(n, "BTC", dtype=object),
    }
    # Deterministic finite signals with zeros and a few NaNs so the diagnostic
    # domains are genuinely different.
    for j, col in enumerate(FEATURE_MAP.values(), start=1):
        vals = np.sin(i / (4.0 + j)) + (j * 0.01)
        vals = vals.astype(float)
        vals[(np.arange(n) + j) % 41 == 0] = 0.0
        vals[(np.arange(n) + 2 * j) % 73 == 0] = np.nan
        d[col] = vals
    d["external_perp_dispersion_bps"] = (0.5 + np.cos(i / 13.0)).astype(float)
    d["bitget_fair_ofi_alignment"] = np.where((np.arange(n) % 3) == 0, 1.0, -1.0)
    d["bitget_fair_trade_alignment"] = np.where((np.arange(n) % 4) == 0, 1.0, -1.0)
    return pd.DataFrame(d)


def _ctx(session_id=DECLARED_SESSIONS[0], asset="BTC"):
    df = _synthetic_grid()
    if asset != "BTC":
        df = df.copy()
        df["asset"] = asset
    return _prepare_block(session_id, asset, df)


def test_fixed_matrix_is_exactly_252_with_expected_strata_counts():
    rows = expand_fixed_matrix()
    assert len(rows) == EXPECTED_ROW_COUNT == 252
    counts = {}
    for row in rows:
        counts[row.stratum] = counts.get(row.stratum, 0) + 1
    assert counts == {"S1": 32, "S2": 64, "S3": 24, "S4": 24, "S5": 36, "S6": 72}


def test_declared_session_set_is_exactly_the_reviewed_two_ids():
    assert DECLARED_SESSIONS == (
        "20260905T073818Z_e44d99bd",
        "20260906T221530Z_db18dc51",
    )
    assert set(ASSETS) == {"BTC", "ETH"}


def test_candidate_generator_rejects_session_outside_declared_matrix():
    bad = DiagnosticCase("S1", "not-declared", "BTC", "bitget_ofi", 1000, 0.90)
    with pytest.raises(PermissionError):
        diagnose_case(bad, None)  # rejection happens before context access


def test_simple_stage_counts_and_overlap_accounting_are_consistent():
    ctx = _ctx()
    row = diagnose_case(
        DiagnosticCase("S1", ctx.session_id, ctx.asset, "bitget_ofi", 1000, 0.90),
        ctx,
    )
    assert row["diagnostic_version"] == DIAGNOSTIC_VERSION
    assert row["quality_n"] >= row["signal_finite_quality_n"] >= row["threshold_crossing_n_before_forward"]
    assert row["threshold_crossing_n_before_forward"] >= row["threshold_crossing_n_after_forward"]
    assert row["threshold_crossing_n_after_forward"] >= row["accepted_n"] >= 0
    assert row["overlap_dropped_n"] >= 0
    assert row["accepted_n"] + row["overlap_dropped_n"] == row["threshold_crossing_n_after_forward"]
    assert row["candidate_N"] == row["accepted_n"]


def test_derived_and_gap_stage_counts_obey_overlap_identity():
    ctx = _ctx()
    derived = diagnose_case(
        DiagnosticCase("S3", ctx.session_id, ctx.asset, "depthL1_extOFI", 1000, 0.90),
        ctx,
    )
    assert derived["component1_finite_quality_n"] >= derived["derived_domain_n"]
    assert derived["component2_finite_quality_n"] >= derived["derived_domain_n"]
    assert derived["derived_domain_n"] >= derived["threshold_crossing_n_before_forward"]
    assert derived["threshold_crossing_n_before_forward"] >= derived["threshold_crossing_n_after_forward"]
    assert derived["accepted_n"] + derived["overlap_dropped_n"] == derived["threshold_crossing_n_after_forward"]

    gap = diagnose_case(
        DiagnosticCase("S4", ctx.session_id, ctx.asset, "gap_depthL1", 1000, 0.90),
        ctx,
    )
    assert gap["gap_threshold_domain_n"] >= gap["gap_event_domain_n"]
    assert gap["gap_event_domain_n"] >= gap["gap_threshold_crossing_n_pre_alignment"]
    assert gap["gap_threshold_crossing_n_pre_alignment"] >= gap["gap_event_n_post_alignment_pre_forward"]
    assert gap["gap_event_n_post_alignment_pre_forward"] >= gap["threshold_crossing_n_after_forward"]
    assert gap["accepted_n"] + gap["overlap_dropped_n"] == gap["threshold_crossing_n_after_forward"]


def test_accepted_position_fingerprint_is_order_independent_and_canonical():
    a = fingerprint_positions([9, 1, 5])
    b = fingerprint_positions(np.array([5, 9, 1], dtype=np.int64))
    assert a == b
    assert a["accepted_positions_count"] == 3
    assert a["accepted_first_grid_pos"] == 1
    assert a["accepted_last_grid_pos"] == 9
    assert len(a["accepted_positions_sha256"]) == 64


def test_mean_abs_move_probes_are_separate_and_do_not_replace_v11_metric():
    ctx = _ctx()
    row = diagnose_case(
        DiagnosticCase("S6", ctx.session_id, ctx.asset, "depth_imbalance_l1", 1000, 0.95),
        ctx,
    )
    metric = ctx.metrics[("depth_imbalance_l1", 1000, 0.95)]
    assert row["candidate_mean_abs_move_v11"] == metric.mean_abs_move
    assert row["probe_mam_all_forward_eligible"] == metric.mean_abs_move
    for key in (
        "probe_mam_all_forward_eligible",
        "probe_mam_feature_finite_forward",
        "probe_mam_threshold_crossing_forward",
        "probe_mam_accepted_events",
    ):
        assert key in row
    # The probe fields do not alter the frozen candidate metric field.
    assert row["candidate_mean_abs_move_v11"] == metric.mean_abs_move


def test_dispersion_event_tertile_probe_is_deterministic_and_null_safe():
    ctx = _ctx()
    case = DiagnosticCase("S5", ctx.session_id, ctx.asset, "fair_gap_reversion", 5000, 0.90, "high")
    r1 = diagnose_case(case, ctx)
    r2 = diagnose_case(case, ctx)
    assert r1["probe_disp_lo_accepted_events"] == r2["probe_disp_lo_accepted_events"]
    assert r1["probe_disp_hi_accepted_events"] == r2["probe_disp_hi_accepted_events"]
    assert r1["probe_state_n_using_event_tertiles"] == r2["probe_state_n_using_event_tertiles"]
    assert r1["accepted_state_n"] == r1["candidate_N"]



def test_dispersion_fixed_state_rows_survive_base_n_zero():
    df = _synthetic_grid()
    df["bitget_gap_to_fair_bps"] = 0.0
    ctx = _prepare_block(DECLARED_SESSIONS[0], "BTC", df)
    case = DiagnosticCase(
        "S5", ctx.session_id, ctx.asset, "fair_gap_reversion", 5000, 0.90, "high"
    )
    row = diagnose_case(case, ctx)
    assert row["fair_gap_accepted_n"] == 0
    assert row["accepted_state_n"] == 0
    assert row["candidate_N"] == 0
    assert row["candidate_mean_signed_bps"] is None
    assert row["candidate_hit_rate"] is None

def test_module_has_no_prohibited_historical_target_or_validation_access():
    import recovery.v1_2_diagnostics as mod

    src = inspect.getsource(mod)
    for forbidden in (
        "recovery.goldens",
        "from .goldens",
        "CP24/",
        "CP36/",
        "run_regression(",
        "compare_row(",
        "compare_aggregate(",
    ):
        assert forbidden not in src



def test_synthetic_full_matrix_generation_emits_exactly_252_rows():
    from recovery.v1_2_diagnostics import generate_stage1_rows

    contexts = {}
    for sid in DECLARED_SESSIONS:
        for asset in ASSETS:
            df = _synthetic_grid()
            df["asset"] = asset
            contexts[(sid, asset)] = _prepare_block(sid, asset, df)
    rows = generate_stage1_rows(contexts)
    assert len(rows) == EXPECTED_ROW_COUNT
    assert {r["stratum"] for r in rows} == {"S1", "S2", "S3", "S4", "S5", "S6"}
    assert all(r["accepted_positions_count"] is not None for r in rows)

def test_stage1_output_is_v12_only_and_refuses_silent_overwrite(tmp_path):
    rows = [{"diagnostic_version": DIAGNOSTIC_VERSION, "i": i} for i in range(EXPECTED_ROW_COUNT)]
    out = write_stage1_artifacts(rows, output_dir=tmp_path, runtime_source_commit="deadbeef")
    assert pathlib.Path(out["csv_path"]).name == "stage1_candidate_diagnostics.csv"
    assert pathlib.Path(out["manifest_path"]).name == "stage1_manifest.json"
    assert not any(name in out["csv_path"] for name in (
        "golden_regression_summary.csv",
        "golden_regression_failures.csv",
        "recovery_report.md",
        "recovery_provenance.json",
        "recovery_rules.json",
    ))
    with pytest.raises(FileExistsError):
        write_stage1_artifacts(rows, output_dir=tmp_path, runtime_source_commit="deadbeef")


def test_new36_session_is_rejected_by_existing_firewall():
    with pytest.raises(NEW36QuantitativeFirewallError):
        assert_recovery_allowed(NEW36_SESSION_IDS[0])


def test_simple_feature_inventory_remains_exactly_twelve():
    assert len(SIMPLE_FEATURES) == 12
