"""SECTION 16 STEP 4 — ORCHESTRATOR (A008 DRAFT4 §9).

backend/recovery/v1_2_new36_run.py — the "Orchestrator" named in A008
DRAFT4 §0.1. Sole authority controlling both role invocations (§9.3).
Implements:

* the durable append-only one-shot ledger (§9.5-9.6);
* the ordered procedure of §9.7 (steps 1-9);
* the recovery rules of §9.9 and the run_id semantics of §9.11;
* the Reference launch contract of §9.8 (subprocess only; the frozen
  Reference is NEVER imported, read or modified).

CLEAN-ROOM: authored exclusively from A008 DRAFT4 §5.2 permitted inputs.
This module MUST NOT read backend/recovery/reference/v1_2_reference.py to
construct the launch command; the command shape is fully specified here
(§9.8(a), taken from A008 only).

PROHIBITED IN THIS SESSION: this module MUST NOT be invoked against real
NEW36 data. `preflight`/`generate` subprocess launches only ever run
against the Candidate application and (via 9.8) the Reference
executable's own CLI contract — neither is executed here with real NEW36
ZIPs during Section 16 STEP 4.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .v1_2_new36_validators import (
    NNC5ParseError,
    append_ledger_record_durable,
    build_shared_environment_record,
    canonical_pretty_bytes,
    exclusive_create_write,
    latest_terminal_record,
    load_and_validate_environment_record,
    nnc5_load_file,
    read_ledger_records,
    sha256_bytes,
    sha256_file,
    size_of,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

LEDGER_SCHEMA_VERSION = "A008_LEDGER_V1"
INPUT_INVENTORY_ID = "NEW36_12SESSIONS_24BLOCKS_3456ROWS_V1"
MIN_FREE_BYTES_11_4_8 = 1_073_741_824


class LedgerCorruption(RuntimeError):
    """A008 §9.10: fail closed, nothing launched, manual audit required."""


class OrchestratorPreconditionFail(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass
class OrchestratorConfig:
    """All paths + injectable subprocess launchers. Production defaults
    point at the real Candidate application module and the frozen
    Reference CLI contract of A008 §9.8. Synthetic tests substitute
    stub executables/paths ONLY (A008 §13)."""
    reports_dir: Path
    db_path: Path
    session_zip_map_path: Path
    a008_sha256: str
    freeze_manifest_sha256: str
    candidate_module_args: tuple[str, ...] = ("-m", "recovery.v1_2_new36_candidate_app")
    reference_executable_args: tuple[str, ...] = (str(REPO_ROOT / "backend" / "recovery" / "reference" / "v1_2_reference.py"),)
    python_executable: str = sys.executable
    repo_root: Path = REPO_ROOT
    minimal_env: dict[str, str] = field(default_factory=lambda: {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})

    @property
    def ledger_path(self) -> Path:
        return self.reports_dir / "one_shot_ledger.jsonl"

    @property
    def environment_record_path(self) -> Path:
        return self.reports_dir / "runtime_environment_identity.json"

    @property
    def candidate_csv_path(self) -> Path:
        return self.reports_dir / "candidate_new36.csv"

    @property
    def candidate_manifest_path(self) -> Path:
        return self.reports_dir / "candidate_new36_manifest.json"

    @property
    def candidate_evidence_path(self) -> Path:
        return self.reports_dir / "candidate_new36_evidence.jsonl"

    @property
    def reference_csv_path(self) -> Path:
        return self.reports_dir / "reference_new36.csv"

    @property
    def reference_manifest_path(self) -> Path:
        return self.reports_dir / "reference_new36_manifest.json"

    @property
    def s1_report_path(self) -> Path:
        return self.reports_dir / "s1_report.json"

    @property
    def s2_report_path(self) -> Path:
        return self.reports_dir / "s2_report.json"

    @property
    def execution_evidence_index_path(self) -> Path:
        return self.reports_dir / "execution_evidence_index.json"


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _git_head(repo_root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root)).decode("ascii").strip()


# ---------------------------------------------------------------------------
# §9.9 recovery rules + §9.11 run_id semantics
# ---------------------------------------------------------------------------

@dataclass
class RecoveryDecision:
    run_id: str
    candidate_state: str  # "NOT_INVOKED" | "COMPLETED" | "FAILED" | "NEEDS_INCOMPLETE_MARK"
    reference_state: str
    fresh_attempt: bool


def determine_run_id_and_recover(records: list[dict]) -> RecoveryDecision:
    """A008 §9.7 step 1 / §9.9 / §9.11. Applies the recovery rules from
    the durable ledger BEFORE anything else. Raises LedgerCorruption on
    any ambiguous/incompatible state (§9.10)."""
    if not records:
        return RecoveryDecision(run_id=str(uuid.uuid4()), candidate_state="NOT_INVOKED",
                                 reference_state="NOT_INVOKED", fresh_attempt=True)

    run_ids = {r.get("run_id") for r in records if r.get("run_id")}
    if len(run_ids) > 1:
        # Only acceptable if terminal COMPLETED CANDIDATE/REFERENCE for
        # distinct run_ids never coexist; otherwise ambiguous.
        raise LedgerCorruption(f"multiple distinct run_ids present in ledger: {run_ids!r}")
    run_id = next(iter(run_ids)) if run_ids else str(uuid.uuid4())

    def _terminal_state(role: str) -> str:
        invoked = None
        terminal = None
        count_invoked = 0
        for rec in records:
            if rec.get("role") != role:
                continue
            if rec.get("state") == "INVOKED":
                count_invoked += 1
                invoked = rec
            if rec.get("state") in ("COMPLETED", "FAILED"):
                if terminal is not None:
                    raise LedgerCorruption(f"multiple terminal records for role {role}")
                terminal = rec
        if count_invoked > 1:
            raise LedgerCorruption(f"multiple INVOKED records for role {role}")
        if terminal is not None:
            return str(terminal["state"])
        if invoked is not None:
            return "NEEDS_INCOMPLETE_MARK"
        return "NOT_INVOKED"

    cand_state = _terminal_state("CANDIDATE")
    ref_state = _terminal_state("REFERENCE")

    cand_completed = next((r for r in records if r.get("role") == "CANDIDATE" and r.get("state") == "COMPLETED"), None)
    ref_completed = next((r for r in records if r.get("role") == "REFERENCE" and r.get("state") == "COMPLETED"), None)
    if cand_completed and ref_completed and cand_completed.get("run_id") != ref_completed.get("run_id"):
        raise LedgerCorruption("Candidate and Reference COMPLETED with different run_ids (§9.11 rule 6)")

    return RecoveryDecision(run_id=run_id, candidate_state=cand_state, reference_state=ref_state, fresh_attempt=False)


# ---------------------------------------------------------------------------
# Orchestrator procedure (A008 §9.7)
# ---------------------------------------------------------------------------

class Orchestrator:
    def __init__(self, config: OrchestratorConfig):
        self.config = config
        self.records: list[dict] = []

    def _append(self, record: dict) -> None:
        append_ledger_record_durable(self.config.ledger_path, record)
        self.records.append(record)

    def _base_record(self, role: str, run_id: str, state: str, **extra: Any) -> dict:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "run_id": run_id,
            "role": role,
            "state": state,
            "utc": _utc_iso_now(),
            "runtime_head": _git_head(self.config.repo_root),
            "runtime_environment_identity_sha256": extra.pop("runtime_environment_identity_sha256", None),
            "input_inventory_id": INPUT_INVENTORY_ID,
            "a008_sha256": self.config.a008_sha256,
            "freeze_manifest_sha256": self.config.freeze_manifest_sha256,
            "output_artifact_sha256": extra.pop("output_artifact_sha256", None),
            "detail": extra.pop("detail", None),
        }

    def step1_recover(self) -> RecoveryDecision:
        self.records = read_ledger_records(self.config.ledger_path)
        return determine_run_id_and_recover(self.records)

    def step2_shared_environment_record(self) -> dict:
        p = self.config.environment_record_path
        if p.exists():
            return load_and_validate_environment_record(p)
        record = build_shared_environment_record()
        exclusive_create_write(p, canonical_pretty_bytes(record))
        return record

    def step3_verify_free_space_and_identities(self) -> None:
        import shutil
        usage = shutil.disk_usage(str(self.config.reports_dir.parent if self.config.reports_dir.exists() else REPO_ROOT))
        if usage.free < MIN_FREE_BYTES_11_4_8:
            raise OrchestratorPreconditionFail("STEP3", "insufficient free space for §11.4.8 (>= 1073741824 bytes required)")

    def step4_candidate_preflight(self, env_sha: str) -> bool:
        cmd = [self.config.python_executable, *self.config.candidate_module_args, "preflight",
               "--db", str(self.config.db_path),
               "--environment-record", str(self.config.environment_record_path),
               "--session-zip-map", str(self.config.session_zip_map_path)]
        result = subprocess.run(cmd, cwd=str(self.config.repo_root / "backend"), capture_output=True, text=True)
        return result.returncode == 0

    def step5_candidate_generate(self, run_id: str, env_sha: str) -> dict:
        self._append(self._base_record("CANDIDATE", run_id, "GATES_PASS", runtime_environment_identity_sha256=env_sha))
        self._append(self._base_record("CANDIDATE", run_id, "INVOKED", runtime_environment_identity_sha256=env_sha))
        cmd = [self.config.python_executable, *self.config.candidate_module_args, "generate",
               "--db", str(self.config.db_path),
               "--environment-record", str(self.config.environment_record_path),
               "--session-zip-map", str(self.config.session_zip_map_path),
               "--out-csv", str(self.config.candidate_csv_path),
               "--out-manifest", str(self.config.candidate_manifest_path),
               "--out-evidence", str(self.config.candidate_evidence_path),
               "--run-id", run_id,
               "--ledger", str(self.config.ledger_path)]
        result = subprocess.run(cmd, cwd=str(self.config.repo_root / "backend"), capture_output=True, text=True)
        if result.returncode == 0 and self._seal_ok(self.config.candidate_csv_path, self.config.candidate_manifest_path):
            art = {
                "csv": sha256_file(self.config.candidate_csv_path),
                "manifest": sha256_file(self.config.candidate_manifest_path),
                "evidence": sha256_file(self.config.candidate_evidence_path) if self.config.candidate_evidence_path.exists() else None,
            }
            rec = self._base_record("CANDIDATE", run_id, "COMPLETED", runtime_environment_identity_sha256=env_sha, output_artifact_sha256=art)
            self._append(rec)
            return rec
        rec = self._base_record("CANDIDATE", run_id, "FAILED", runtime_environment_identity_sha256=env_sha, detail="STRUCTURAL")
        self._append(rec)
        return rec

    def _seal_ok(self, csv_path: Path, manifest_path: Path) -> bool:
        try:
            import json
            manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
        except Exception:
            return False
        return manifest.get("csv_sha256") == sha256_file(csv_path)

    def step6_gate_reference(self, run_id: str) -> None:
        """R2 (§9.7 step 6, narrows V2 §13, never widens)."""
        latest_candidate = latest_terminal_record(self.records, "CANDIDATE", run_id)
        if latest_candidate is None or latest_candidate.get("state") == "INVOKED":
            raise OrchestratorPreconditionFail("STEP6", "Candidate terminal state is not established yet (recovery required)")
        if latest_candidate.get("state") == "FAILED":
            raise OrchestratorPreconditionFail(
                "STEP6",
                "R2: Candidate FAILED — Reference preflight/generate MUST NOT run; "
                "no {role:REFERENCE, state:INVOKED} record MUST be appended",
            )
        if latest_candidate.get("state") != "COMPLETED":
            raise OrchestratorPreconditionFail("STEP6", f"unexpected Candidate terminal state {latest_candidate.get('state')!r}")

    def step6_reference_preflight(self) -> bool:
        cmd = [self.config.python_executable, *self.config.reference_executable_args, "preflight",
               "--db", str(self.config.db_path),
               "--environment-record", str(self.config.environment_record_path)]
        result = subprocess.run(
            cmd, cwd=str(self.config.repo_root), capture_output=True, text=True, env=dict(self.config.minimal_env),
        )
        return result.returncode == 0

    def step7_reference_generate(self, run_id: str, env_sha: str) -> dict:
        """§9.8 Reference launch contract. R1 input isolation: the
        Reference receives ONLY the read-only DB path, the shared
        environment record, the session-zip map and two fresh output
        paths — via argv only, minimal environment, no Candidate
        artifact path anywhere."""
        for p in (self.config.reference_csv_path, self.config.reference_manifest_path):
            if p.exists():
                raise OrchestratorPreconditionFail("STEP7", f"Reference output path already exists: {p}")
        self._append(self._base_record("REFERENCE", run_id, "GATES_PASS", runtime_environment_identity_sha256=env_sha))
        self._append(self._base_record("REFERENCE", run_id, "INVOKED", runtime_environment_identity_sha256=env_sha))
        cmd = [self.config.python_executable, *self.config.reference_executable_args, "generate",
               "--db", str(self.config.db_path),
               "--environment-record", str(self.config.environment_record_path),
               "--session-zip-map", str(self.config.session_zip_map_path),
               "--out-csv", str(self.config.reference_csv_path),
               "--out-manifest", str(self.config.reference_manifest_path)]
        result = subprocess.run(
            cmd, cwd=str(self.config.repo_root), capture_output=True, text=True, env=dict(self.config.minimal_env),
        )
        if result.returncode == 0 and self.config.reference_csv_path.exists() and self.config.reference_manifest_path.exists():
            art = {"csv": sha256_file(self.config.reference_csv_path), "manifest": sha256_file(self.config.reference_manifest_path), "evidence": None}
            rec = self._base_record("REFERENCE", run_id, "COMPLETED", runtime_environment_identity_sha256=env_sha, output_artifact_sha256=art)
            self._append(rec)
            return rec
        rec = self._base_record("REFERENCE", run_id, "FAILED", runtime_environment_identity_sha256=env_sha, detail="STRUCTURAL")
        self._append(rec)
        return rec

    def step8_run_s1_s2(self) -> tuple[dict, dict]:
        from . import v1_2_new36_validators as validators
        s1_inputs = validators.S1Inputs(
            candidate_csv=self.config.candidate_csv_path,
            candidate_manifest=self.config.candidate_manifest_path,
            reference_csv=self.config.reference_csv_path,
            reference_manifest=self.config.reference_manifest_path,
            ledger=self.config.ledger_path,
        )
        s1_report = validators.run_s1(s1_inputs)
        exclusive_create_write(self.config.s1_report_path, canonical_pretty_bytes(s1_report))

        freeze_manifest_path = REPO_ROOT / "backend" / "recovery" / "specs" / "V1_2_CANDIDATE_IMPLEMENTATION_FREEZE_MANIFEST.txt"
        s2_inputs = validators.S2Inputs(
            candidate_csv=self.config.candidate_csv_path,
            candidate_manifest=self.config.candidate_manifest_path,
            candidate_evidence=self.config.candidate_evidence_path,
            freeze_manifest=freeze_manifest_path,
            ledger=self.config.ledger_path,
        )
        s2_report = validators.run_s2(s2_inputs)
        exclusive_create_write(self.config.s2_report_path, canonical_pretty_bytes(s2_report))
        return s1_report, s2_report

    def step9_execution_evidence_index(self) -> dict:
        index: dict[str, Any] = {}
        for name, path in (
            ("candidate_csv", self.config.candidate_csv_path),
            ("candidate_manifest", self.config.candidate_manifest_path),
            ("candidate_evidence", self.config.candidate_evidence_path),
            ("reference_csv", self.config.reference_csv_path),
            ("reference_manifest", self.config.reference_manifest_path),
            ("s1_report", self.config.s1_report_path),
            ("s2_report", self.config.s2_report_path),
            ("ledger", self.config.ledger_path),
        ):
            if path.exists():
                index[name] = {"sha256": sha256_file(path), "size_bytes": size_of(path)}
        exclusive_create_write(self.config.execution_evidence_index_path, canonical_pretty_bytes(index))
        return index

    def run(self) -> dict:
        """Full A008 §9.7 procedure. NEVER call this against real NEW36
        session-zip-map paths outside an authorized, frozen (Section 16
        step 8 complete) execution interpreter."""
        decision = self.step1_recover()
        run_id = decision.run_id

        if decision.candidate_state == "NEEDS_INCOMPLETE_MARK":
            self._append(self._base_record("CANDIDATE", run_id, "FAILED", detail="INCOMPLETE_AFTER_RESTART"))
            decision.candidate_state = "FAILED"
        if decision.reference_state == "NEEDS_INCOMPLETE_MARK":
            self._append(self._base_record("REFERENCE", run_id, "FAILED", detail="INCOMPLETE_AFTER_RESTART"))
            decision.reference_state = "FAILED"

        env_record = self.step2_shared_environment_record()
        env_sha = env_record["runtime_environment_identity_sha256"]
        self.step3_verify_free_space_and_identities()

        if decision.candidate_state == "NOT_INVOKED":
            if not self.step4_candidate_preflight(env_sha):
                raise OrchestratorPreconditionFail("STEP4", "Candidate preflight (P-01..P-09) did not pass")
            self.step5_candidate_generate(run_id, env_sha)

        self.step6_gate_reference(run_id)

        if decision.reference_state == "NOT_INVOKED":
            if not self.step6_reference_preflight():
                raise OrchestratorPreconditionFail("STEP6", "Reference preflight did not pass")
            self.step7_reference_generate(run_id, env_sha)

        cand_completed = latest_terminal_record(self.records, "CANDIDATE", run_id)
        ref_completed = latest_terminal_record(self.records, "REFERENCE", run_id)
        s1_report = s2_report = None
        if cand_completed and cand_completed.get("state") == "COMPLETED" and ref_completed and ref_completed.get("state") == "COMPLETED":
            s1_report, s2_report = self.step8_run_s1_s2()

        index = self.step9_execution_evidence_index()
        return {"run_id": run_id, "s1_report": s1_report, "s2_report": s2_report, "execution_evidence_index": index}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="v1_2_new36_run")
    parser.add_argument("--db", required=True)
    parser.add_argument("--session-zip-map", required=True)
    parser.add_argument("--reports-dir", required=True)
    parser.add_argument("--a008-sha256", required=True)
    parser.add_argument("--freeze-manifest-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    config = OrchestratorConfig(
        reports_dir=Path(args.reports_dir),
        db_path=Path(args.db),
        session_zip_map_path=Path(args.session_zip_map),
        a008_sha256=args.a008_sha256,
        freeze_manifest_sha256=args.freeze_manifest_sha256,
    )
    orchestrator = Orchestrator(config)
    try:
        result = orchestrator.run()
    except (LedgerCorruption, OrchestratorPreconditionFail) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
