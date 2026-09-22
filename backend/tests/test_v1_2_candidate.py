"""Focused synthetic tests for the frozen V1.2 candidate + acceptance +
pre-NEW36 drift-guard preflight.

These tests do NOT touch OLD36, NEW36, or golden artifacts. They exercise
the frozen candidate semantics and the acceptance evaluator in isolation.
"""
from __future__ import annotations

import ast
import hashlib
import math
from pathlib import Path
from typing import Sequence

import numpy as np
import pytest

from backend.recovery import v1_2_candidate as cand
from backend.recovery import v1_2_acceptance as acc
from backend.recovery import v1_2_preflight as pf


# =====================================================================
# TZ threshold
# =====================================================================


class TestTZThreshold:
    def test_excludes_exact_zero_from_domain(self):
        quality = np.ones(6, dtype=bool)
        signal = np.array([0.0, 0.0, 0.0, 1.0, 2.0, 3.0])
        # Non-zero finite domain has 3 values -> type-7 quantile at q=0.5
        thr = cand.tz_threshold(quality, signal, 0.5)
        assert thr == pytest.approx(2.0)

    def test_retains_finite_non_zero_values(self):
        quality = np.ones(4, dtype=bool)
        signal = np.array([-3.0, -1.0, 2.0, 4.0])
        thr = cand.tz_threshold(quality, signal, 0.9)
        # abs -> [1,2,3,4]; q=0.9 type7 -> interpolate at index 2.7 -> 3.7
        assert thr == pytest.approx(3.7)

    def test_type7_quantile_none_when_fewer_than_two(self):
        quality = np.ones(3, dtype=bool)
        signal = np.array([0.0, 0.0, 5.0])
        # Only 1 non-zero finite -> None
        assert cand.tz_threshold(quality, signal, 0.9) is None

    def test_nan_is_not_zero_and_is_dropped(self):
        quality = np.ones(4, dtype=bool)
        signal = np.array([np.nan, 0.0, 1.0, 2.0])
        thr = cand.tz_threshold(quality, signal, 0.5)
        assert thr == pytest.approx(1.5)

    def test_quality_false_excludes_row(self):
        quality = np.array([False, True, True])
        signal = np.array([100.0, 1.0, 2.0])
        thr = cand.tz_threshold(quality, signal, 0.5)
        assert thr == pytest.approx(1.5)

    def test_rejects_shape_mismatch(self):
        with pytest.raises(ValueError):
            cand.tz_threshold(np.ones(3, dtype=bool), np.zeros(4), 0.5)

    def test_rejects_bad_q(self):
        with pytest.raises(ValueError):
            cand.tz_threshold(np.ones(2, dtype=bool), np.ones(2), 0.0)


# =====================================================================
# G1 pre-overlap (HG = G1, threshold source = TZ)
# =====================================================================


class TestG1PreOverlap:
    def test_uses_tz_threshold_source(self):
        # TZ threshold from di_l1 (raw), then applied inside G1 mask.
        quality = np.ones(5, dtype=bool)
        di_l1 = np.array([0.0, 0.5, -1.0, 2.0, 3.0])
        tz = cand.tz_threshold(quality, di_l1, 0.5)
        # |{0.5,1,2,3}| type-7 median -> 1.5
        assert tz == pytest.approx(1.5)
        ext_ofi = np.array([1.0, 1.0, -1.0, 1.0, 1.0])
        fwd = np.zeros(5)
        mask = cand.g1_pre_overlap_mask(
            quality=quality, di_l1=di_l1, ext_ofi=ext_ofi,
            fwd=fwd, tz_thr=tz,
        )
        # Only indices with |di|>=1.5 AND sign(ofi)==sign(di):
        # idx 2: |di|=1 -> excluded; idx 3: |di|=2, sign(2)==sign(1) -> ok
        # idx 4: |di|=3, sign(3)==sign(1) -> ok; idx 1: |di|=0.5 excluded
        assert list(mask.astype(int)) == [0, 0, 0, 1, 1]

    def test_raw_sign_confirmation_only(self):
        quality = np.ones(3, dtype=bool)
        di_l1 = np.array([2.0, -2.0, 2.0])
        ext_ofi = np.array([1.0, 1.0, -1.0])  # only idx 0 agrees in sign
        fwd = np.zeros(3)
        mask = cand.g1_pre_overlap_mask(
            quality=quality, di_l1=di_l1, ext_ofi=ext_ofi,
            fwd=fwd, tz_thr=1.0,
        )
        assert list(mask.astype(int)) == [1, 0, 0]

    def test_parent_subset_invariant_holds(self):
        quality = np.ones(4, dtype=bool)
        di_l1 = np.array([1.0, 2.0, 3.0, 4.0])
        ext_ofi = np.array([1.0, -1.0, 1.0, 1.0])
        fwd = np.zeros(4)
        tz = 2.0
        g1 = cand.g1_pre_overlap_mask(
            quality=quality, di_l1=di_l1, ext_ofi=ext_ofi,
            fwd=fwd, tz_thr=tz,
        )
        parent = cand.g1_parent_gate_mask(
            quality=quality, di_l1=di_l1, fwd=fwd, tz_thr=tz,
        )
        # Every G1 event must be inside parent gate
        cand.assert_g1_parent_subset(g1, parent)

    def test_parent_subset_violation_detected(self):
        g1 = np.array([True, True, False])
        parent = np.array([True, False, False])
        with pytest.raises(AssertionError, match="I_G1_PARENT_SUBSET FAIL"):
            cand.assert_g1_parent_subset(g1, parent)

    def test_tz_none_forces_empty_mask(self):
        mask = cand.g1_pre_overlap_mask(
            quality=np.ones(3, dtype=bool),
            di_l1=np.array([1.0, 2.0, 3.0]),
            ext_ofi=np.array([1.0, 1.0, 1.0]),
            fwd=np.zeros(3),
            tz_thr=None,
        )
        assert not mask.any()


# =====================================================================
# C1 pre-overlap (HC = C1, raw signs only, no z-scores)
# =====================================================================


class TestC1PreOverlap:
    def test_raw_sign_conjunction(self):
        quality = np.ones(4, dtype=bool)
        gap = np.array([-2.0, -2.0, 2.0, 2.0])
        di_l1 = np.array([1.0, -1.0, -1.0, 1.0])
        ext_ofi = np.array([1.0, -1.0, 1.0, -1.0])
        # For gap<0: sign(di) must be +1 and sign(ofi) must be +1
        # For gap>0: sign(di) must be -1 and sign(ofi) must be -1
        # Only idx 0 (gap<0, di=+1, ofi=+1) and idx 2 (gap>0, di=-1, ofi=+1)?
        # No: idx 2 ofi=+1 needs sign=-1 -> reject. Only idx 0 qualifies.
        fwd = np.zeros(4)
        mask = cand.c1_pre_overlap_mask(
            quality=quality, gap=gap, di_l1=di_l1, ext_ofi=ext_ofi,
            fwd=fwd, gap_thr=1.0,
        )
        assert list(mask.astype(int)) == [1, 0, 0, 0]

    def test_gap_parent_subset_invariant(self):
        quality = np.ones(3, dtype=bool)
        gap = np.array([-2.0, 3.0, 1.0])
        di_l1 = np.array([1.0, -1.0, -1.0])
        ext_ofi = np.array([1.0, -1.0, -1.0])
        fwd = np.zeros(3)
        thr = 1.5
        c1 = cand.c1_pre_overlap_mask(
            quality=quality, gap=gap, di_l1=di_l1, ext_ofi=ext_ofi,
            fwd=fwd, gap_thr=thr,
        )
        parent = cand.c1_gap_parent_mask(
            quality=quality, gap=gap, fwd=fwd, gap_thr=thr,
        )
        cand.assert_c1_gap_subset(c1, parent)

    def test_gap_parent_subset_violation(self):
        c1 = np.array([True, True])
        parent = np.array([True, False])
        with pytest.raises(AssertionError, match="I_C1_GAP_SUBSET FAIL"):
            cand.assert_c1_gap_subset(c1, parent)

    def test_candidate_source_uses_no_z_score(self):
        """The frozen v1_2_candidate.py module must not USE any of the
        prohibited z-score columns as identifiers or attribute accesses in
        code. The names may appear only inside the *declared* prohibition
        tuple ``CANDIDATE_FORBIDDEN_CONFIRMATION_COLUMNS`` (as documentary
        string constants).
        """
        src_path = (
            Path(__file__).resolve().parents[1]
            / "recovery" / "v1_2_candidate.py"
        )
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        prohibited = {"z_di_l1", "z_ext_ofi", "z_gap_d"}
        # Locate the forbidden-columns tuple assignment so we can whitelist
        # its string constants only.
        allowed_constant_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == "CANDIDATE_FORBIDDEN_CONFIRMATION_COLUMNS":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        allowed_constant_nodes.add(id(sub))
        # Now walk again and enforce prohibition everywhere else.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.attr, str):
                assert node.attr not in prohibited, (
                    f"prohibited attribute access: {node.attr!r}"
                )
            if isinstance(node, ast.Name):
                assert node.id not in prohibited, (
                    f"prohibited identifier: {node.id!r}"
                )
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in allowed_constant_nodes:
                    continue
                assert node.value not in prohibited, (
                    f"prohibited string constant outside declaration: "
                    f"{node.value!r}"
                )


# =====================================================================
# HB filters: B0 accepts equality; B1 strict >
# =====================================================================


class TestHBFilters:
    def test_b0_accepts_equality_at_exact_spacing(self):
        positions = np.array([0, 10, 20, 30], dtype=np.int64)
        mask = cand.b0_greedy_filter(positions, spacing_steps=10)
        assert list(mask.astype(int)) == [1, 1, 1, 1]

    def test_b1_rejects_equality_at_exact_spacing(self):
        positions = np.array([0, 10, 20, 30], dtype=np.int64)
        mask = cand.b1_diagnostic_filter(positions, spacing_steps=10)
        # B1 uses strict > 10: first accepted; each equality-gap of 10 rejected;
        # the last_accepted advances only on strict-> pattern is [1,0,1,0].
        # Key property: at NO index does B1 accept an equality-gap event that
        # B0 also accepts (index 1 shows the divergence).
        assert list(mask.astype(int)) == [1, 0, 1, 0]
        # And B1 must NEVER accept more events than B0
        b0 = cand.b0_greedy_filter(positions, spacing_steps=10)
        assert int(b1_sum := mask.sum()) <= int(b0.sum())
        # Divergence must exist at the equality-gap indices
        assert not bool(np.array_equal(mask, b0))

    def test_spacing_steps_frozen_formula(self):
        assert cand.spacing_steps_for_horizon(100) == 10
        assert cand.spacing_steps_for_horizon(500) == 10
        assert cand.spacing_steps_for_horizon(1000) == 10
        assert cand.spacing_steps_for_horizon(2000) == 20
        assert cand.spacing_steps_for_horizon(30000) == 300

    def test_exact_spacing_pair_n(self):
        positions = np.array([0, 10, 20, 35, 45], dtype=np.int64)
        # gaps: 10, 10, 15, 10 -> 3 pairs with gap==10
        assert cand.exact_spacing_pair_n(positions, 10) == 3

    def test_i11_pass_and_fail(self):
        cand.assert_hb_i11(b0_accepted_n=5, b1_accepted_n=3)
        cand.assert_hb_i11(b0_accepted_n=5, b1_accepted_n=5)
        with pytest.raises(AssertionError, match="I_HB_I11 FAIL"):
            cand.assert_hb_i11(b0_accepted_n=3, b1_accepted_n=5)

    def test_i12_holds_when_esp_zero(self):
        positions = np.array([0, 15, 30], dtype=np.int64)  # gaps 15,15
        s = 10
        assert cand.exact_spacing_pair_n(positions, s) == 0
        b0 = cand.b0_greedy_filter(positions, s)
        b1 = cand.b1_diagnostic_filter(positions, s)
        cand.assert_hb_i12(0, b0, b1)  # must not raise

    def test_i12_violation_detected(self):
        b0 = np.array([True, True])
        b1 = np.array([True, False])
        with pytest.raises(AssertionError, match="I_HB_I12 FAIL"):
            cand.assert_hb_i12(0, b0, b1)

    def test_hb_discriminating_cell_logic(self):
        # Non-discriminating: esp=0 (no equality pairs)
        cells = [
            acc.HBCellInput(
                exact_spacing_pair_n=0,
                b0_positions_sha256="a",
                b1_positions_sha256="a",
                b0_abs_relN=0.5, b1_abs_relN=0.5,
                i11_holds=True, i12_holds=True,
            ),
        ]
        assert acc.hb_verdict(cells) == acc.VERDICT_UNVERIFIABLE

        # Discriminating: esp>0 and digests differ, B0 strictly better
        cells = [
            acc.HBCellInput(
                exact_spacing_pair_n=3,
                b0_positions_sha256="x",
                b1_positions_sha256="y",
                b0_abs_relN=0.1, b1_abs_relN=0.5,
                i11_holds=True, i12_holds=True,
            ),
        ]
        assert acc.hb_verdict(cells) == acc.VERDICT_PASS

        # All discriminating cells: B1 strictly better -> FAIL
        cells = [
            acc.HBCellInput(
                exact_spacing_pair_n=3,
                b0_positions_sha256="x", b1_positions_sha256="y",
                b0_abs_relN=0.5, b1_abs_relN=0.1,
                i11_holds=True, i12_holds=True,
            ),
        ]
        assert acc.hb_verdict(cells) == acc.VERDICT_FAIL

        # Mixed -> PARTIAL
        cells = [
            acc.HBCellInput(3, "x", "y", 0.1, 0.5, True, True),
            acc.HBCellInput(3, "x", "y", 0.5, 0.1, True, True),
        ]
        assert acc.hb_verdict(cells) == acc.VERDICT_PARTIAL

        # I11 violation anywhere -> FAIL
        cells = [
            acc.HBCellInput(3, "x", "y", 0.1, 0.5, False, True),
        ]
        assert acc.hb_verdict(cells) == acc.VERDICT_FAIL


# =====================================================================
# Cell classification (Section C) & envelope ratio
# =====================================================================


class TestCellClassification:
    def _mk(self, **kw):
        defaults = dict(
            golden_N=10, candidate_N=10, env=1.0, relN=0.5,
            has_falsified=True, relN_falsified=2.0,
            control_envelope_available=True,
        )
        defaults.update(kw)
        return acc.CellInput(**defaults)

    def test_not_applicable_threshold_none(self):
        r = acc.classify_cell(self._mk(golden_N=None))
        assert r.classification == acc.CLS_NOT_APPLICABLE
        r = acc.classify_cell(self._mk(candidate_N=None))
        assert r.classification == acc.CLS_NOT_APPLICABLE

    def test_catastrophic_structural(self):
        r = acc.classify_cell(self._mk(golden_N=0, candidate_N=5))
        assert r.classification == acc.CLS_CATASTROPHIC_STRUCTURAL
        assert math.isinf(r.r)
        r = acc.classify_cell(self._mk(golden_N=5, candidate_N=0))
        assert r.classification == acc.CLS_CATASTROPHIC_STRUCTURAL

    def test_zero_golden_zero_candidate_is_within(self):
        r = acc.classify_cell(
            self._mk(golden_N=0, candidate_N=0, env=0.0, relN=0.0)
        )
        assert r.classification == acc.CLS_WITHIN

    def test_invalid_run_missing_falsified_baseline(self):
        r = acc.classify_cell(self._mk(has_falsified=False))
        assert r.classification == acc.CLS_INVALID_RUN

    def test_axis_unverifiable_missing_envelope(self):
        r = acc.classify_cell(self._mk(control_envelope_available=False))
        assert r.classification == acc.CLS_AXIS_UNVERIFIABLE

    def test_falsified_baseline_anomaly_F_le_env(self):
        r = acc.classify_cell(
            self._mk(env=1.0, relN=0.4, relN_falsified=1.0)
        )
        # F=1, env=1, F<=env -> anomaly
        assert r.classification == acc.CLS_FALSIFIED_BASELINE_ANOMALY

    def test_within_band(self):
        r = acc.classify_cell(
            self._mk(env=1.0, relN=0.4, relN_falsified=5.0)
        )
        # F=5, env=1, F>env -> band eval; |relN|=0.4 <= env=1 -> WITHIN
        assert r.classification == acc.CLS_WITHIN

    def test_catastrophic_band(self):
        # env=1, F=100 -> sqrt(1*100)=10; relN=15 -> CATASTROPHIC
        r = acc.classify_cell(
            self._mk(env=1.0, relN=15.0, relN_falsified=100.0)
        )
        assert r.classification == acc.CLS_CATASTROPHIC

    def test_elevated_band(self):
        # env=1, F=100 -> bound=10; relN=5 -> ELEVATED
        r = acc.classify_cell(
            self._mk(env=1.0, relN=5.0, relN_falsified=100.0)
        )
        assert r.classification == acc.CLS_ELEVATED

    def test_env_zero_and_relN_zero(self):
        assert acc.envelope_ratio(0.0, 0.0) == 0.0

    def test_env_zero_relN_nonzero_is_infinite(self):
        assert math.isinf(acc.envelope_ratio(0.1, 0.0))

    def test_F_zero_bound_collapses(self):
        # F=0, env=1 -> WITHIN band when |relN|<=1; else CATASTROPHIC (bound=0)
        r = acc.classify_cell(
            self._mk(env=1.0, relN=0.5, relN_falsified=0.0)
        )
        # F=0<=env=1 -> FALSIFIED_BASELINE_ANOMALY (before band)
        assert r.classification == acc.CLS_FALSIFIED_BASELINE_ANOMALY


class TestMedian:
    def test_odd_n(self):
        assert acc.median_odd_even([1, 2, 3]) == 2
        assert acc.median_odd_even([3, 1, 2]) == 2

    def test_even_n(self):
        assert acc.median_odd_even([1, 2, 3, 4]) == 2.5

    def test_infinity_at_center(self):
        # even: two central values [2, inf] -> inf
        assert math.isinf(acc.median_odd_even([1, 2, math.inf, math.inf]))

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            acc.median_odd_even([])


# =====================================================================
# Collector / pipeline mismatch (Section I)
# =====================================================================


class TestCollectorMismatch:
    def test_precondition_fail_before_access(self):
        v = acc.collector_mismatch_verdict(
            collector_sha_expected="a", collector_sha_actual="b",
            pipeline_sha_expected="p", pipeline_sha_actual="p",
            quantitative_access_started=False,
        )
        assert v == acc.COLLECTOR_PRECONDITION_FAIL

    def test_invalid_run_after_access(self):
        v = acc.collector_mismatch_verdict(
            collector_sha_expected="a", collector_sha_actual="b",
            pipeline_sha_expected="p", pipeline_sha_actual="p",
            quantitative_access_started=True,
        )
        assert v == acc.COLLECTOR_INVALID_RUN

    def test_ok_when_matching(self):
        v = acc.collector_mismatch_verdict(
            collector_sha_expected="a", collector_sha_actual="a",
            pipeline_sha_expected="p", pipeline_sha_actual="p",
            quantitative_access_started=False,
        )
        assert v == acc.COLLECTOR_OK


# =====================================================================
# Preflight: golden-path prohibition
# =====================================================================


class TestPreflightGoldenProhibition:
    def test_candidate_module_has_no_golden_imports(self):
        # Must not raise
        pf.verify_no_golden_imports_in_candidate()

    def test_preflight_report_all_ok(self, tmp_path):
        src_agg = pf.aggregate_sha256(pf.IMPL_SOURCE_PATHS)
        tst_agg = pf.aggregate_sha256(pf.IMPL_TEST_PATHS)
        head = pf.current_git_head()
        exp = pf.PreflightExpectations(
            candidate_spec_sha256=pf.sha256_of_file(pf.CANDIDATE_SPEC_PATH),
            acceptance_criteria_sha256=pf.sha256_of_file(pf.ACCEPTANCE_CRITERIA_PATH),
            implementation_source_aggregate_sha256=src_agg,
            implementation_test_aggregate_sha256=tst_agg,
            expected_runtime_commit=head,
            collector_sha="COLL_SHA_STUB",
            pipeline_sha="PIPE_SHA_STUB",
        )
        snap = pf.RuntimeSnapshot(
            actual_runtime_commit=head,
            actual_collector_sha="COLL_SHA_STUB",
            actual_pipeline_sha="PIPE_SHA_STUB",
            firewall_active=True,
            frozen_analysis_engine_configured=False,
            frozen_analysis_engine_accepts_input=False,
        )
        report = pf.run_preflight(exp, snap)
        assert report.ok, report.failures

    def test_preflight_detects_spec_drift(self):
        head = pf.current_git_head()
        src_agg = pf.aggregate_sha256(pf.IMPL_SOURCE_PATHS)
        tst_agg = pf.aggregate_sha256(pf.IMPL_TEST_PATHS)
        exp = pf.PreflightExpectations(
            candidate_spec_sha256="deadbeef" * 8,  # wrong
            acceptance_criteria_sha256=pf.sha256_of_file(pf.ACCEPTANCE_CRITERIA_PATH),
            implementation_source_aggregate_sha256=src_agg,
            implementation_test_aggregate_sha256=tst_agg,
            expected_runtime_commit=head,
            collector_sha="C", pipeline_sha="P",
        )
        snap = pf.RuntimeSnapshot(
            actual_runtime_commit=head,
            actual_collector_sha="C", actual_pipeline_sha="P",
            firewall_active=True,
            frozen_analysis_engine_configured=False,
            frozen_analysis_engine_accepts_input=False,
        )
        report = pf.run_preflight(exp, snap)
        assert not report.ok
        assert "CANDIDATE_SPEC_SHA_DRIFT" in report.failures

    def test_preflight_detects_firewall_off(self):
        head = pf.current_git_head()
        src_agg = pf.aggregate_sha256(pf.IMPL_SOURCE_PATHS)
        tst_agg = pf.aggregate_sha256(pf.IMPL_TEST_PATHS)
        exp = pf.PreflightExpectations(
            candidate_spec_sha256=pf.sha256_of_file(pf.CANDIDATE_SPEC_PATH),
            acceptance_criteria_sha256=pf.sha256_of_file(pf.ACCEPTANCE_CRITERIA_PATH),
            implementation_source_aggregate_sha256=src_agg,
            implementation_test_aggregate_sha256=tst_agg,
            expected_runtime_commit=head,
            collector_sha="C", pipeline_sha="P",
        )
        snap = pf.RuntimeSnapshot(
            actual_runtime_commit=head,
            actual_collector_sha="C", actual_pipeline_sha="P",
            firewall_active=False,           # off
            frozen_analysis_engine_configured=True,  # bad
            frozen_analysis_engine_accepts_input=True,  # bad
        )
        report = pf.run_preflight(exp, snap)
        assert not report.ok
        assert "FIREWALL_NOT_ACTIVE" in report.failures
        assert "FROZEN_ANALYSIS_ENGINE_CONFIGURED" in report.failures
        assert "FROZEN_ANALYSIS_ENGINE_ACCEPTS_INPUT" in report.failures


# =====================================================================
# HG / HC verdict integration
# =====================================================================


class TestAxisVerdictIntegration:
    def _cells_all_within(self, n=5) -> Sequence[acc.CellResult]:
        return [
            acc.CellResult(acc.CLS_WITHIN, r=0.1) for _ in range(n)
        ]

    def test_hg_pass_when_both_components_pass(self):
        cells = self._cells_all_within()
        v = acc.axis_verdict_hg_hc(cells, acc.VERDICT_PASS)
        assert v == acc.VERDICT_PASS

    def test_hg_fail_when_threshold_fails(self):
        cells = self._cells_all_within()
        v = acc.axis_verdict_hg_hc(cells, acc.VERDICT_FAIL)
        assert v == acc.VERDICT_FAIL

    def test_hg_fail_on_falsified_anomaly(self):
        cells = [
            acc.CellResult(acc.CLS_WITHIN, 0.1),
            acc.CellResult(acc.CLS_FALSIFIED_BASELINE_ANOMALY, 0.2),
        ]
        v = acc.axis_verdict_hg_hc(cells, acc.VERDICT_PASS)
        assert v == acc.VERDICT_FAIL

    def test_hg_partial_no_cat_median_le_1(self):
        cells = [
            acc.CellResult(acc.CLS_WITHIN, 0.5),
            acc.CellResult(acc.CLS_ELEVATED, 0.9),
            acc.CellResult(acc.CLS_WITHIN, 0.3),
        ]
        # comp1 -> PARTIAL (elevated with median<=1); threshold PASS -> PARTIAL
        v = acc.axis_verdict_hg_hc(cells, acc.VERDICT_PASS)
        assert v == acc.VERDICT_PARTIAL


class TestOverallVerdict:
    def test_all_pass(self):
        assert acc.overall_verdict(
            {"HZ": acc.VERDICT_PASS, "HG": acc.VERDICT_PASS,
             "HC": acc.VERDICT_PASS, "HB": acc.VERDICT_PASS}
        ) == acc.VERDICT_PASS

    def test_any_fail(self):
        assert acc.overall_verdict(
            {"HZ": acc.VERDICT_PASS, "HG": acc.VERDICT_FAIL,
             "HC": acc.VERDICT_PASS, "HB": acc.VERDICT_PASS}
        ) == acc.VERDICT_FAIL

    def test_any_partial(self):
        assert acc.overall_verdict(
            {"HZ": acc.VERDICT_PARTIAL, "HG": acc.VERDICT_PASS,
             "HC": acc.VERDICT_PASS, "HB": acc.VERDICT_UNVERIFIABLE}
        ) == acc.VERDICT_PARTIAL


# =====================================================================
# Threshold component (exactness tolerance 1e-12)
# =====================================================================


class TestThresholdComponent:
    def test_matches_within_tolerance(self):
        pairs = [(1.0, 1.0 + 1e-13), (2.0, 2.0)]
        assert acc.threshold_component_verdict(pairs) == acc.VERDICT_PASS

    def test_fails_beyond_tolerance(self):
        pairs = [(1.0, 1.0 + 1e-6), (2.0, 2.0)]
        assert acc.threshold_component_verdict(pairs) == acc.VERDICT_FAIL

    def test_unverifiable_when_all_none(self):
        pairs = [(None, 1.0), (1.0, None)]
        assert acc.threshold_component_verdict(pairs) == acc.VERDICT_UNVERIFIABLE
