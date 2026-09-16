"""NEW36 firewall + recovery sandbox coverage.

Guarantees that:

- The recovery sandbox refuses to open any NEW36 raw ZIP
  quantitatively.
- The sandbox refuses unknown session_ids.
- Only OLD36_REFERENCE ids are accepted, and only if they have
  actually been imported.
- The FrozenAnalysisEngine status remains NOT_CONFIGURED.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from checkpoint_registry import (
    NEW36_SESSION_IDS,
    OLD36_REFERENCE_SESSIONS,
)
from database import engine
from frozen_engine import current_status
from recovery import GOLDENS_DIR, REPORTS_DIR
from recovery.allowlist import (
    NEW36QuantitativeFirewallError,
    allowed_session_ids,
    assert_recovery_allowed,
)
from recovery.goldens import counts as golden_counts
from recovery.harness import run_regression
from recovery.rules import RULES, RuleStatus, status_summary
from recovery.sandbox import (
    availability_snapshot,
    iter_reference_sessions,
    open_reference_zip,
    reference_session_status,
)


class TestFirewall:
    def test_allowlist_is_only_old36(self):
        assert set(allowed_session_ids()) == set(OLD36_REFERENCE_SESSIONS)

    @pytest.mark.parametrize("sid", NEW36_SESSION_IDS)
    def test_new36_ids_are_refused(self, sid):
        with pytest.raises(NEW36QuantitativeFirewallError):
            assert_recovery_allowed(sid)
        with pytest.raises(NEW36QuantitativeFirewallError):
            open_reference_zip(sid)
        with pytest.raises(NEW36QuantitativeFirewallError):
            reference_session_status(sid)

    def test_unknown_id_is_refused(self):
        with pytest.raises(NEW36QuantitativeFirewallError):
            assert_recovery_allowed("20301231T000000Z_unknown")

    def test_none_id_is_refused(self):
        with pytest.raises(NEW36QuantitativeFirewallError):
            assert_recovery_allowed(None)

    def test_permission_error_is_the_base_class(self):
        """So no `except Exception:` block can silently swallow it as
        an unrelated error."""
        assert issubclass(NEW36QuantitativeFirewallError, PermissionError)

    @pytest.mark.parametrize("sid", OLD36_REFERENCE_SESSIONS)
    def test_old36_id_allowed_but_missing_raises_file_not_found(self, sid):
        # Allowed identity, but no raw ZIP imported into the test DB.
        assert_recovery_allowed(sid)  # must NOT raise
        with pytest.raises(FileNotFoundError):
            open_reference_zip(sid)


class TestGoldensAvailable:
    def test_golden_directory_exists(self):
        assert (GOLDENS_DIR / "CP24").is_dir()
        assert (GOLDENS_DIR / "CP36").is_dir()

    def test_golden_counts_are_sane(self):
        c = golden_counts()
        # Simple 24H block = 14*12*8*3 = 4032; NEW12 = 8*12*8*3 = 2304
        assert c["simple_block_rows"] == 4032 + 2304
        # Composite 24H = 14*10*5*3 = 2100; NEW12 = 8*10*5*3 = 1200
        # (composites are only evaluated at horizons {1000,2000,5000,10000,30000})
        assert c["composite_block_rows"] == 2100 + 1200


class TestAvailabilitySnapshot:
    def test_snapshot_shape(self):
        # Ensure DB is empty of OLD36 rows for this test's isolation.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM raw_files"))
            conn.execute(text("UPDATE sessions SET current_qa_run_id = NULL"))
            conn.execute(text("DELETE FROM qa_runs"))
            conn.execute(text("DELETE FROM sessions"))

        s = availability_snapshot()
        assert s["expected_sessions"] == 11
        assert s["present_sessions"] == 0
        assert s["present_nominal_hours"] == 0.0
        assert s["expected_nominal_hours"] == 36.0
        assert set(s["missing_sessions"]) == set(OLD36_REFERENCE_SESSIONS)

    def test_iter_reference_sessions_yields_nothing_when_empty(self):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM raw_files"))
            conn.execute(text("UPDATE sessions SET current_qa_run_id = NULL"))
            conn.execute(text("DELETE FROM qa_runs"))
            conn.execute(text("DELETE FROM sessions"))
        assert list(iter_reference_sessions()) == []


class TestFrozenEngineUnchanged:
    def test_engine_still_not_configured(self):
        s = current_status()
        assert s.status == "NOT_CONFIGURED"
        assert s.accepts_input is False


class TestReportsGenerated:
    def test_run_regression_produces_all_five_files(self):
        # Ensure DB is empty of OLD36 rows for a stable pending count.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM raw_files"))
            conn.execute(text("UPDATE sessions SET current_qa_run_id = NULL"))
            conn.execute(text("DELETE FROM qa_runs"))
            conn.execute(text("DELETE FROM sessions"))
        out = run_regression()
        for k in (
            "summary_path",
            "failures_path",
            "rules_path",
            "provenance_path",
            "report_path",
        ):
            assert k in out
        # All files must exist on disk.
        for k in (
            "summary_path",
            "failures_path",
            "rules_path",
            "provenance_path",
            "report_path",
        ):
            from pathlib import Path

            assert Path(out[k]).exists(), f"missing {out[k]}"
        # Rules JSON parses.
        rules_data = json.loads((REPORTS_DIR / "recovery_rules.json").read_text())
        assert isinstance(rules_data["rules"], list)
        assert len(rules_data["rules"]) == len(RULES)

    def test_rule_status_enum_only(self):
        for r in RULES:
            assert isinstance(r.status, RuleStatus)
        assert set(status_summary().keys()) == {s.value for s in RuleStatus}
