"""RECONSTRUCTION_V1.2 PRE-NEW36 drift-guard preflight.

Fail-closed synthetic preflight that must be satisfied BEFORE any future
NEW36 execution. This module DOES NOT execute NEW36. It only:

* Recomputes SHA256 for the frozen candidate spec, acceptance criteria,
  and the frozen candidate/acceptance implementation modules.
* Compares against a caller-supplied expected snapshot.
* Verifies the runtime commit against an expected commit.
* Verifies collector/pipeline identity matches expectation.
* Verifies the candidate generator does not import any golden path.
* Verifies firewall/allowlist state and FrozenAnalysisEngine remains
  NOT_CONFIGURED.

All checks are read-only and side-effect free.
"""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

CANDIDATE_SPEC_PATH: Path = (
    REPO_ROOT / "backend" / "recovery" / "specs" / "V1_2_CANDIDATE_SPEC.txt"
)
ACCEPTANCE_CRITERIA_PATH: Path = (
    REPO_ROOT / "backend" / "recovery" / "specs" / "V1_2_ACCEPTANCE_CRITERIA.txt"
)
IMPL_SOURCE_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "recovery" / "v1_2_candidate.py",
    REPO_ROOT / "backend" / "recovery" / "v1_2_acceptance.py",
    REPO_ROOT / "backend" / "recovery" / "v1_2_preflight.py",
)
IMPL_TEST_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "tests" / "test_v1_2_candidate.py",
)

# Suspicious string-constant substrings that would only appear if the
# candidate module tried to *reference* a golden artifact by path or module
# name. Documentation / comment usage of the word "golden" is permitted
# because comments do not become string constants in the AST for imports
# or open() calls.
_GOLDEN_FORBIDDEN_STRING_TOKENS: tuple[str, ...] = (
    "goldens",  # matches the `backend/recovery/goldens.py` module + goldens/ dir
    "reports/v1_2/stage3_",
    "reports/v1_2/stage2_",
    "reports/v1_2/stage1_",
)


@dataclass(frozen=True)
class PreflightExpectations:
    candidate_spec_sha256: str
    acceptance_criteria_sha256: str
    implementation_source_aggregate_sha256: str
    implementation_test_aggregate_sha256: str
    expected_runtime_commit: str
    collector_sha: str
    pipeline_sha: str


@dataclass(frozen=True)
class RuntimeSnapshot:
    actual_runtime_commit: str
    actual_collector_sha: str
    actual_pipeline_sha: str
    firewall_active: bool
    frozen_analysis_engine_configured: bool
    frozen_analysis_engine_accepts_input: bool


def sha256_of_file(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def size_of_file(path: Path) -> int:
    return path.stat().st_size


def aggregate_sha256(paths: Sequence[Path], repo_root: Path = REPO_ROOT) -> str:
    """Deterministic aggregate: sha256 over sorted 'relpath<TAB>sha256\\n' lines.
    """
    records: list[str] = []
    for p in paths:
        rel = str(p.resolve().relative_to(repo_root))
        records.append(f"{rel}\t{sha256_of_file(p)}")
    records.sort()
    blob = ("\n".join(records) + "\n").encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def verify_no_golden_imports_in_candidate() -> None:
    """Fail-closed: the frozen candidate module must not import or reference
    any golden path/module.
    """
    path = REPO_ROOT / "backend" / "recovery" / "v1_2_candidate.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "golden" in alias.name.lower():
                    raise AssertionError(
                        f"I_NO_GOLDEN_IMPORTS FAIL: candidate imports {alias.name!r}"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "golden" in mod.lower():
                raise AssertionError(
                    f"I_NO_GOLDEN_IMPORTS FAIL: candidate imports from {mod!r}"
                )
    # Textual scan for forbidden *string constants* (imports or literal
    # paths that would attempt to reach golden artifacts). Docstring /
    # comment usage of the word "golden" is not present in string constants
    # for imports and is therefore not blocked here.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            for tok in _GOLDEN_FORBIDDEN_STRING_TOKENS:
                if tok.lower() in lowered:
                    raise AssertionError(
                        f"I_NO_GOLDEN_IMPORTS FAIL: candidate references "
                        f"forbidden string constant containing {tok!r}"
                    )


def current_git_head(repo_root: Path = REPO_ROOT) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root)
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"cannot resolve git HEAD: {exc!r}") from exc
    return out.decode("ascii").strip()


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    failures: tuple[str, ...]


def run_preflight(
    expectations: PreflightExpectations,
    snapshot: RuntimeSnapshot,
) -> PreflightReport:
    """Run the fail-closed preflight synthetically.

    Returns a :class:`PreflightReport`. If any check fails, ``ok`` is False
    and ``failures`` enumerates the specific blockers. NEW36 must not run.
    """
    failures: list[str] = []

    # Spec + acceptance hashes.
    if sha256_of_file(CANDIDATE_SPEC_PATH) != expectations.candidate_spec_sha256:
        failures.append("CANDIDATE_SPEC_SHA_DRIFT")
    if sha256_of_file(ACCEPTANCE_CRITERIA_PATH) != expectations.acceptance_criteria_sha256:
        failures.append("ACCEPTANCE_CRITERIA_SHA_DRIFT")

    # Implementation aggregates.
    src_agg = aggregate_sha256(IMPL_SOURCE_PATHS)
    if src_agg != expectations.implementation_source_aggregate_sha256:
        failures.append("IMPLEMENTATION_SOURCE_AGGREGATE_DRIFT")
    tst_agg = aggregate_sha256(IMPL_TEST_PATHS)
    if tst_agg != expectations.implementation_test_aggregate_sha256:
        failures.append("IMPLEMENTATION_TEST_AGGREGATE_DRIFT")

    # Runtime commit.
    if snapshot.actual_runtime_commit != expectations.expected_runtime_commit:
        failures.append("RUNTIME_COMMIT_DRIFT")

    # Collector / pipeline identity.
    if snapshot.actual_collector_sha != expectations.collector_sha:
        failures.append("COLLECTOR_SHA_DRIFT")
    if snapshot.actual_pipeline_sha != expectations.pipeline_sha:
        failures.append("PIPELINE_SHA_DRIFT")

    # Golden import prohibition.
    try:
        verify_no_golden_imports_in_candidate()
    except AssertionError as exc:
        failures.append(f"GOLDEN_IMPORT_PROHIBITION_FAIL:{exc}")

    # Firewall and FrozenAnalysisEngine invariants.
    if not snapshot.firewall_active:
        failures.append("FIREWALL_NOT_ACTIVE")
    if snapshot.frozen_analysis_engine_configured:
        failures.append("FROZEN_ANALYSIS_ENGINE_CONFIGURED")
    if snapshot.frozen_analysis_engine_accepts_input:
        failures.append("FROZEN_ANALYSIS_ENGINE_ACCEPTS_INPUT")

    return PreflightReport(ok=not failures, failures=tuple(failures))


__all__ = [
    "REPO_ROOT",
    "CANDIDATE_SPEC_PATH", "ACCEPTANCE_CRITERIA_PATH",
    "IMPL_SOURCE_PATHS", "IMPL_TEST_PATHS",
    "PreflightExpectations", "RuntimeSnapshot", "PreflightReport",
    "sha256_of_file", "size_of_file", "aggregate_sha256",
    "verify_no_golden_imports_in_candidate", "current_git_head",
    "run_preflight",
]
