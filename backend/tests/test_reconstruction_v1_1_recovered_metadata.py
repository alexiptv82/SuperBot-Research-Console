"""Focused tests for the RECONSTRUCTION_V1.1 recovered-metadata patch
(closing AUDIT_BLOCKER_BLOCK_IDENTITY and AUDIT_BLOCKER_AGGREGATE_SCOPE).

ALL evidence used here is either:
  (a) the static RECONSTRUCTION_V1.1_RECOVERED_METADATA.json artifact
      (itself built ONE TIME from golden-vs-golden matching, no
      candidate reconstruction), or
  (b) purely synthetic fixtures.

This file NEVER calls recovery.harness.run_regression(), NEVER opens a
NEW36 session, and NEVER computes a RECONSTRUCTION_V1.1 candidate
value from real OLD36 raw data.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("SUPERBOT_ENV", "test")

from recovery import validation
from recovery.harness import (
    UnknownScopeError,
    _aggregate_scope_routing,
    _expected_block_identity,
    _iter_aggregate_row_views,
    _load_recovered_metadata,
    _resolve_scope_session_ids,
)

METADATA_PATH = (
    BACKEND_DIR / "recovery" / "specs" / "RECONSTRUCTION_V1.1_RECOVERED_METADATA.json"
)

# Pinned at generation time — see recovery/tools/generate_recovered_metadata.py
# output. A drift in this hash means the recovered metadata artifact
# changed; that must be re-audited, not silently accepted.
EXPECTED_METADATA_SHA256 = (
    "b12f1bb13ca7667f7fa55c59e741563b3e15267826de4511cf43fd79b2262ab3"
)


@pytest.fixture(scope="module")
def metadata():
    return _load_recovered_metadata()


# ===========================================================================
# 1-5: scope membership
# ===========================================================================
class TestScopeMembership:
    def test_all36_scope_membership(self, metadata):
        scope = metadata["scopes"]["ALL36"]
        assert scope["count"] == 11
        assert len(scope["session_ids"]) == 11
        assert scope["recovery_status"] == "EXPLICIT"

    def test_old24_scope_membership(self, metadata):
        scope = metadata["scopes"]["OLD24"]
        assert scope["count"] == 7
        assert len(scope["session_ids"]) == 7
        assert "ALL24" in scope["aliases"]
        assert scope["recovery_status"] == "EXPLICIT"

    def test_new12_scope_membership(self, metadata):
        scope = metadata["scopes"]["NEW12"]
        assert scope["count"] == 4
        assert len(scope["session_ids"]) == 4
        assert scope["recovery_status"] == "EXPLICIT"

    def test_weekend12_scope_membership(self, metadata):
        scope = metadata["scopes"]["WEEKEND12"]
        assert scope["count"] == 3
        assert len(scope["session_ids"]) == 3

    def test_weekday12_scope_membership(self, metadata):
        scope = metadata["scopes"]["WEEKDAY12"]
        assert scope["count"] == 4
        assert len(scope["session_ids"]) == 4


# ===========================================================================
# 6-7: set-algebra cross-checks
# ===========================================================================
class TestScopeSetAlgebra:
    def test_all36_equals_old24_union_new12(self, metadata):
        all36 = set(metadata["scopes"]["ALL36"]["session_ids"])
        old24 = set(metadata["scopes"]["OLD24"]["session_ids"])
        new12 = set(metadata["scopes"]["NEW12"]["session_ids"])
        assert all36 == old24 | new12
        assert metadata["cross_checks"]["all36_equals_old24_union_new12"] is True

    def test_old24_intersection_new12_empty(self, metadata):
        old24 = set(metadata["scopes"]["OLD24"]["session_ids"])
        new12 = set(metadata["scopes"]["NEW12"]["session_ids"])
        assert old24.isdisjoint(new12)
        assert metadata["cross_checks"]["old24_intersection_new12_empty"] is True

    def test_weekend_weekday_partition_old24(self, metadata):
        old24 = set(metadata["scopes"]["OLD24"]["session_ids"])
        we = set(metadata["scopes"]["WEEKEND12"]["session_ids"])
        wd = set(metadata["scopes"]["WEEKDAY12"]["session_ids"])
        assert we | wd == old24
        assert we.isdisjoint(wd)


# ===========================================================================
# 8: explicit WE/WD routing (not timestamp-derived)
# ===========================================================================
class TestExplicitWeekendWeekdayRouting:
    def test_weekend_and_weekday_resolve_via_metadata_not_timestamps(self, metadata):
        we_ids = _resolve_scope_session_ids("WEEKEND12")
        wd_ids = _resolve_scope_session_ids("WEEKDAY12")
        assert set(we_ids) == set(metadata["scopes"]["WEEKEND12"]["session_ids"])
        assert set(wd_ids) == set(metadata["scopes"]["WEEKDAY12"]["session_ids"])
        # Evidence sources must cite the explicit period column, never
        # a timestamp/timezone derivation.
        evidence = " ".join(metadata["scopes"]["WEEKEND12"]["evidence_source"])
        assert "period" in evidence.lower()
        assert "timezone" not in evidence.lower() and "rome" not in evidence.lower()


# ===========================================================================
# 9-12: identity recovery completeness
# ===========================================================================
class TestIdentityRecoveryCompleteness:
    def _all_entries(self, metadata):
        entries = []
        for file_entries in metadata["block_identities"].values():
            entries.extend(file_entries)
        return entries

    def test_total_rows_is_1604(self, metadata):
        assert metadata["identity_recovery_summary"]["total_rows"] == 1604
        assert len(self._all_entries(metadata)) == 1604

    def test_all_1604_min_identities_recover_uniquely(self, metadata):
        summary = metadata["identity_recovery_summary"]
        assert summary["min_unique"] == 1604
        entries = self._all_entries(metadata)
        recovered = sum(1 for e in entries if e.get("min_block") is not None)
        assert recovered == 1604

    def test_all_1604_max_identities_recover_uniquely(self, metadata):
        summary = metadata["identity_recovery_summary"]
        assert summary["max_unique"] == 1604
        entries = self._all_entries(metadata)
        recovered = sum(1 for e in entries if e.get("max_block") is not None)
        assert recovered == 1604

    def test_zero_ambiguous_identity_mappings(self, metadata):
        summary = metadata["identity_recovery_summary"]
        assert summary["min_ambiguous"] == 0
        assert summary["max_ambiguous"] == 0

    def test_zero_unrecoverable_identity_mappings(self, metadata):
        summary = metadata["identity_recovery_summary"]
        assert summary["min_unrecoverable"] == 0
        assert summary["max_unrecoverable"] == 0
        entries = self._all_entries(metadata)
        assert all(e.get("min_block") is not None for e in entries)
        assert all(e.get("max_block") is not None for e in entries)

    def test_matching_used_exact_tolerance_not_widened(self, metadata):
        """Part C discipline: exact (tolerance=0) matching alone
        recovered 100% — no silent 1e-9 widening was required."""
        summary = metadata["identity_recovery_summary"]
        assert summary["matching_tolerance"] == 0
        assert summary["matching_method"] == "exact_float_equality_after_csv_parse"


# ===========================================================================
# 13-17: aggregate scope routing
# ===========================================================================
class TestAggregateScopeRouting:
    def test_aggregate_cp24_routing(self):
        golden = {"feature": "bitget_ofi", "period": "WEEKEND12",
                  "horizon_ms": "100", "q": "0.8"}
        views = list(_iter_aggregate_row_views("CP24/simple_aggregates_24h.csv", golden))
        assert len(views) == 1
        scope, feat, horizon_ms, q, sub, is_disp = views[0]
        assert scope == "WEEKEND12"
        assert feat == "bitget_ofi"
        assert horizon_ms == 100
        assert q == 0.8
        assert is_disp is False

    def test_aggregate_new12_routing(self):
        golden = {"feature": "bitget_ofi", "q": "0.85"}
        views = list(_iter_aggregate_row_views("CP36/simple_sensitivity_new12_30s.csv", golden))
        assert len(views) == 1
        scope, feat, horizon_ms, q, sub, is_disp = views[0]
        assert scope == "NEW12"
        assert horizon_ms == 30000  # filename-implied "_30s"
        assert q == 0.85

    def test_aggregate_all36_routing(self):
        golden = {"feature": "bitget_ofi", "horizon_ms": "500"}
        views = list(_iter_aggregate_row_views("CP36/simple_horizon_profile_all36_q90.csv", golden))
        assert len(views) == 1
        scope, feat, horizon_ms, q, sub, is_disp = views[0]
        assert scope == "ALL36"
        assert horizon_ms == 500
        assert q == 0.90  # filename-implied "_q90"

    def test_weekend_weekday_wide_file_routing(self):
        golden = {
            "feature": "bitget_ofi", "type": "simple",
            "WEEKEND12_min_block": "0.1", "WEEKEND12_max_block": "0.2",
            "WEEKDAY12_min_block": "0.3", "WEEKDAY12_max_block": "0.4",
            "ALL24_min_block": "0.5", "ALL24_max_block": "0.6",
        }
        views = list(_iter_aggregate_row_views(
            "CP24/weekend_vs_weekday_selected_30s.csv", golden))
        scopes = {v[0] for v in views}
        assert scopes == {"WEEKEND12", "WEEKDAY12", "ALL24"}
        assert len(views) == 3
        for scope, feat, horizon_ms, q, sub, is_disp in views:
            assert horizon_ms == 30000
            assert q == 0.90
            assert sub["min_block"] == golden[f"{scope}_min_block"]
            assert sub["max_block"] == golden[f"{scope}_max_block"]

    def test_dispersion_aggregate_routing(self):
        golden = {"state": "low", "horizon_ms": "5000"}
        views = list(_iter_aggregate_row_views("CP36/fair_gap_dispersion_all36.csv", golden))
        assert len(views) == 1
        scope, feat, horizon_ms, q, sub, is_disp = views[0]
        assert scope == "ALL36"
        assert feat == "low"
        assert is_disp is True


# ===========================================================================
# 18-19: min/max identity comparison
# ===========================================================================
class TestMinMaxIdentityComparison:
    def test_min_identity_comparison_pass_and_fail(self):
        golden = {"min_block": ("SESSION_A", "BTC")}
        assert validation.compare_aggregate(golden, {"min_block": ("SESSION_A", "BTC")})["min_block"] is False
        assert validation.compare_aggregate(golden, {"min_block": ("SESSION_B", "ETH")})["min_block"] is True

    def test_max_identity_comparison_pass_and_fail(self):
        golden = {"max_block": ("SESSION_Z", "ETH")}
        assert validation.compare_aggregate(golden, {"max_block": ("SESSION_Z", "ETH")})["max_block"] is False
        assert validation.compare_aggregate(golden, {"max_block": ("SESSION_A", "BTC")})["max_block"] is True

    def test_expected_identity_lookup_feeds_comparison_correctly(self, metadata):
        """End-to-end (still golden-vs-golden + synthetic candidate,
        no OLD36 regression): pull a real recovered identity out of
        the metadata and confirm compare_aggregate() agrees it
        matches when candidate == expected, and fails when not."""
        rel = "CP36/fair_gap_dispersion_all36.csv"
        entries = metadata["block_identities"][rel]
        entry = entries[0]
        expected_min = (entry["min_block"]["session_id"], entry["min_block"]["asset"])
        golden = {"min_block": expected_min}
        assert validation.compare_aggregate(golden, {"min_block": expected_min})["min_block"] is False
        assert validation.compare_aggregate(golden, {"min_block": ("NOT_A_REAL_SESSION", "BTC")})["min_block"] is True


# ===========================================================================
# 20: unknown scope hard-fails
# ===========================================================================
class TestUnknownScopeHardFails:
    def test_unrecognized_scope_name_raises(self):
        with pytest.raises(UnknownScopeError):
            _resolve_scope_session_ids("BOGUS_SCOPE_XYZ")

    def test_unrouted_file_raises_in_iterator(self):
        with pytest.raises(UnknownScopeError):
            list(_iter_aggregate_row_views("CP99/not_a_real_file.csv", {"feature": "x"}))

    def test_period_column_with_unknown_value_raises_on_resolution(self):
        golden = {"feature": "bitget_ofi", "period": "SOME_UNKNOWN_PERIOD_LABEL"}
        views = list(_iter_aggregate_row_views("CP24/simple_aggregates_24h.csv", golden))
        scope = views[0][0]
        with pytest.raises(UnknownScopeError):
            _resolve_scope_session_ids(scope)


# ===========================================================================
# 21: metadata hash is stable
# ===========================================================================
class TestMetadataHashStable:
    def test_hash_is_deterministic_across_reads(self):
        h1 = hashlib.sha256(METADATA_PATH.read_bytes()).hexdigest()
        h2 = hashlib.sha256(METADATA_PATH.read_bytes()).hexdigest()
        assert h1 == h2

    def test_hash_matches_pinned_value(self):
        actual = hashlib.sha256(METADATA_PATH.read_bytes()).hexdigest()
        assert actual == EXPECTED_METADATA_SHA256


# ===========================================================================
# 22: candidate values are NOT used to build expected identity mapping
# ===========================================================================
class TestExpectedIdentityIsCandidateIndependent:
    def test_expected_block_identity_never_touches_block_reconstruction(self, monkeypatch):
        """_expected_block_identity() must be a pure static lookup —
        it must never call _get_block_summary / reconstruct_block /
        aggregate_blocks (i.e. it never needs or uses a candidate
        value to answer "what identity does the golden artifact
        expect")."""
        import recovery.harness as harness_mod

        def _boom(*args, **kwargs):
            raise AssertionError(
                "_expected_block_identity() must not trigger candidate "
                "block reconstruction"
            )

        monkeypatch.setattr(harness_mod, "_get_block_summary", _boom)

        rel = "CP36/fair_gap_dispersion_all36.csv"
        result = _expected_block_identity(rel, 0, "ALL36")
        assert result is not None
        assert result["min_block"] is not None
        assert result["max_block"] is not None

    def test_aggregate_scope_routing_is_static_json_only(self):
        routing = _aggregate_scope_routing()
        assert "CP24/simple_aggregates_24h.csv" in routing
        assert "CP36/fair_gap_dispersion_all36.csv" in routing
        assert routing["CP24/weekend_vs_weekday_selected_30s.csv"]["scope_source"] == "wide_columns"
