"""Synthetic tests for backend/recovery/v1_2_new36_run.py (Orchestrator).

A008 DRAFT4 §13.1-13.2: NO real NEW36 quantitative byte, no network, no
real DB. The Candidate and Reference roles are both replaced by tiny
stub executables that never touch backend/recovery/reference/ or any
NEW36 path; this is the standard synthetic-test substitution explicitly
permitted for the Orchestrator ("no sandbox-bound loader may be called",
§4.5(b); Orchestrator tests substitute stub executables/paths, §13).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recovery.v1_2_new36_run import (  # noqa: E402
    LedgerCorruption,
    Orchestrator,
    OrchestratorConfig,
    OrchestratorPreconditionFail,
    determine_run_id_and_recover,
)
from recovery.v1_2_new36_validators import (  # noqa: E402
    append_ledger_record_durable,
    read_ledger_records,
)

pytestmark = pytest.mark.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# §9.9 recovery rules / §9.11 run_id semantics (pure logic, no subprocess)
# ---------------------------------------------------------------------------

def test_fresh_ledger_yields_fresh_run_id():
    decision = determine_run_id_and_recover([])
    assert decision.fresh_attempt is True
    assert decision.candidate_state == "NOT_INVOKED"
    assert decision.reference_state == "NOT_INVOKED"


def test_invoked_without_terminal_needs_incomplete_mark():
    records = [{"role": "CANDIDATE", "state": "INVOKED", "run_id": "r1"}]
    decision = determine_run_id_and_recover(records)
    assert decision.candidate_state == "NEEDS_INCOMPLETE_MARK"
    assert decision.run_id == "r1"


def test_completed_candidate_recovers_as_completed():
    records = [
        {"role": "CANDIDATE", "state": "INVOKED", "run_id": "r1"},
        {"role": "CANDIDATE", "state": "COMPLETED", "run_id": "r1"},
    ]
    decision = determine_run_id_and_recover(records)
    assert decision.candidate_state == "COMPLETED"


def test_multiple_distinct_run_ids_is_ledger_corruption():
    records = [
        {"role": "CANDIDATE", "state": "COMPLETED", "run_id": "r1"},
        {"role": "REFERENCE", "state": "COMPLETED", "run_id": "r2"},
    ]
    with pytest.raises(LedgerCorruption):
        determine_run_id_and_recover(records)


def test_multiple_invoked_records_same_role_is_corruption():
    records = [
        {"role": "CANDIDATE", "state": "INVOKED", "run_id": "r1"},
        {"role": "CANDIDATE", "state": "INVOKED", "run_id": "r1"},
    ]
    with pytest.raises(LedgerCorruption):
        determine_run_id_and_recover(records)


def test_multiple_terminal_records_same_role_is_corruption():
    records = [
        {"role": "CANDIDATE", "state": "COMPLETED", "run_id": "r1"},
        {"role": "CANDIDATE", "state": "FAILED", "run_id": "r1"},
    ]
    with pytest.raises(LedgerCorruption):
        determine_run_id_and_recover(records)


# ---------------------------------------------------------------------------
# Stub executables (never touch reference/ or any NEW36 path)
# ---------------------------------------------------------------------------

_STUB_CANDIDATE = """\
import sys, json, os, shutil, pathlib
args = sys.argv[1:]
mode = args[0]
def val(flag):
    return args[args.index(flag) + 1]
if mode == "preflight":
    sys.exit(0)
elif mode == "generate":
    fixture_dir = pathlib.Path(os.environ["FIXTURE_DIR"])
    shutil.copyfile(fixture_dir / "candidate.csv", val("--out-csv"))
    shutil.copyfile(fixture_dir / "candidate_manifest.json", val("--out-manifest"))
    shutil.copyfile(fixture_dir / "candidate_evidence.jsonl", val("--out-evidence"))
    sys.exit(0)
sys.exit(1)
"""

_STUB_REFERENCE = """\
import sys, json, os, shutil, pathlib
args = sys.argv[1:]
mode = args[0]
def val(flag):
    return args[args.index(flag) + 1]
record_path = os.environ.get("REFERENCE_INVOCATION_RECORD")
if record_path:
    with open(record_path, "w") as fh:
        json.dump({"argv": args, "env": dict(os.environ), "cwd": os.getcwd()}, fh)
if mode == "preflight":
    sys.exit(0)
elif mode == "generate":
    fixture_dir = pathlib.Path(os.environ["FIXTURE_DIR"])
    shutil.copyfile(fixture_dir / "reference.csv", val("--out-csv"))
    shutil.copyfile(fixture_dir / "reference_manifest.json", val("--out-manifest"))
    sys.exit(0)
sys.exit(1)
"""


def _write_stub(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _make_matching_candidate_reference_fixtures(fixture_dir: Path) -> None:
    """Reuses the exact same minimal synthetic-row generator pattern as
    test_v1_2_new36_validators.py — byte-identical candidate/reference
    artifacts so S1 legitimately PASSes."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_v1_2_new36_validators import _rows_to_csv_bytes, _synthetic_row_set  # noqa

    from recovery.v1_2_new36_validators import canonical_pretty_bytes, sha256_bytes

    rows = _synthetic_row_set()
    csv_bytes = _rows_to_csv_bytes(rows)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    for role in ("candidate", "reference"):
        (fixture_dir / f"{role}.csv").write_bytes(csv_bytes)
        manifest = {
            "implementation_role": role.upper(),
            "csv_sha256": sha256_bytes(csv_bytes),
            "csv_size_bytes": len(csv_bytes),
            "manifest_precomparison_sha256": None,
        }
        manifest["manifest_precomparison_sha256"] = sha256_bytes(canonical_pretty_bytes(manifest))
        (fixture_dir / f"{role}_manifest.json").write_bytes(canonical_pretty_bytes(manifest))
    (fixture_dir / "candidate_evidence.jsonl").write_bytes(b"")


def _build_config(tmp_path: Path, fixture_dir: Path, invocation_record: Path) -> OrchestratorConfig:
    stub_candidate = _write_stub(tmp_path / "stub_candidate.py", _STUB_CANDIDATE)
    stub_reference = _write_stub(tmp_path / "stub_reference.py", _STUB_REFERENCE)
    db_path = tmp_path / "runtime.sqlite3"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE)")
    conn.commit()
    conn.close()
    session_zip_map_path = tmp_path / "session_zip_map.json"
    session_zip_map_path.write_text("{}", encoding="utf-8")

    return OrchestratorConfig(
        reports_dir=tmp_path / "reports",
        db_path=db_path,
        session_zip_map_path=session_zip_map_path,
        a008_sha256="0" * 64,
        freeze_manifest_sha256="0" * 64,
        candidate_module_args=(str(stub_candidate),),
        reference_executable_args=(str(stub_reference),),
        minimal_env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "FIXTURE_DIR": str(fixture_dir),
                     "REFERENCE_INVOCATION_RECORD": str(invocation_record)},
    )


# ---------------------------------------------------------------------------
# Full orchestrator run against stubs
# ---------------------------------------------------------------------------

def test_full_orchestrator_run_ledger_sequence_and_s1_pass(tmp_path, monkeypatch):
    fixture_dir = tmp_path / "fixtures"
    _make_matching_candidate_reference_fixtures(fixture_dir)
    invocation_record = tmp_path / "reference_invocation.json"
    config = _build_config(tmp_path, fixture_dir, invocation_record)
    monkeypatch.setenv("FIXTURE_DIR", str(fixture_dir))  # candidate subprocess inherits parent env

    orchestrator = Orchestrator(config)
    result = orchestrator.run()

    records = read_ledger_records(config.ledger_path)
    roles_states = [(r["role"], r["state"]) for r in records]
    assert roles_states == [
        ("CANDIDATE", "GATES_PASS"), ("CANDIDATE", "INVOKED"), ("CANDIDATE", "COMPLETED"),
        ("REFERENCE", "GATES_PASS"), ("REFERENCE", "INVOKED"), ("REFERENCE", "COMPLETED"),
    ]
    run_ids = {r["run_id"] for r in records}
    assert len(run_ids) == 1  # run_id consistent across both roles (§9.11)

    assert result["s1_report"]["s1_status"] == "PASS", result["s1_report"]
    assert config.s2_report_path.exists()
    assert config.execution_evidence_index_path.exists()


def test_reference_isolation_argv_env_and_no_candidate_path(tmp_path, monkeypatch):
    fixture_dir = tmp_path / "fixtures"
    _make_matching_candidate_reference_fixtures(fixture_dir)
    invocation_record = tmp_path / "reference_invocation.json"
    config = _build_config(tmp_path, fixture_dir, invocation_record)
    monkeypatch.setenv("FIXTURE_DIR", str(fixture_dir))

    orchestrator = Orchestrator(config)
    orchestrator.run()

    recorded = json.loads(invocation_record.read_text())
    argv = recorded["argv"]
    assert "--db" in argv and "--environment-record" in argv and "--session-zip-map" in argv
    assert "--out-csv" in argv and "--out-manifest" in argv
    assert str(config.candidate_csv_path) not in argv
    assert str(config.candidate_manifest_path) not in argv
    env = recorded["env"]
    # The Reference process must NOT inherit the orchestrator's ambient
    # PATH-independent secrets; only the configured minimal_env is passed.
    assert set(env.keys()) == set(config.minimal_env.keys())
    for k, v in config.minimal_env.items():
        assert env[k] == v


def test_step6_gate_reference_blocks_on_candidate_failed(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    invocation_record = tmp_path / "reference_invocation.json"
    config = _build_config(tmp_path, fixture_dir, invocation_record)
    orchestrator = Orchestrator(config)
    orchestrator.records = [{"role": "CANDIDATE", "state": "FAILED", "run_id": "r1"}]
    with pytest.raises(OrchestratorPreconditionFail) as exc:
        orchestrator.step6_gate_reference("r1")
    assert exc.value.code == "STEP6"


def test_reference_output_paths_must_be_absent_before_launch(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    invocation_record = tmp_path / "reference_invocation.json"
    config = _build_config(tmp_path, fixture_dir, invocation_record)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    config.reference_csv_path.write_bytes(b"pre-existing")
    orchestrator = Orchestrator(config)
    with pytest.raises(OrchestratorPreconditionFail):
        orchestrator.step7_reference_generate("r1", "envsha")
