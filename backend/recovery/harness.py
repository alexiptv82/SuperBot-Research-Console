"""Golden regression harness (skeleton).

Acts on the CP24/CP36 golden CSV rows and — when the OLD36 raw ZIPs
are fully imported — attempts to reproduce every ``mean_signed_bps``,
``N``, ``hit_rate`` and ``threshold`` per (session,asset,feature,
horizon,q). Until the raw ZIPs are present the harness records the
current state as ``PENDING_RAW_OLD36`` for every row.

Outputs written to ``recovery/reports/``:

- ``golden_regression_summary.csv`` : per-file counts (rows, matched, failed, pending)
- ``golden_regression_failures.csv``: any row where reproduced != golden beyond tolerance
- ``recovery_rules.json``           : rule catalogue + status snapshot
- ``recovery_provenance.json``      : detailed evidence trail per rule
- ``recovery_report.md``            : human-readable summary
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import GOLDENS_DIR, REPORTS_DIR
from .goldens import counts as golden_counts
from .goldens import (
    list_cp24_csvs,
    list_cp36_csvs,
    read_golden_rows,
)
from .rules import RULES, RuleStatus, status_summary
from .sandbox import availability_snapshot

PENDING = "PENDING_RAW_OLD36"
MATCHED = "MATCHED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"


@dataclass
class RegressionOutcome:
    source_file: str
    total_rows: int
    matched: int
    failed: int
    pending: int

    def as_row(self) -> dict[str, str | int]:
        return {
            "source_file": self.source_file,
            "total_rows": self.total_rows,
            "matched": self.matched,
            "failed": self.failed,
            "pending": self.pending,
        }


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_regression() -> dict:
    """Run the golden regression across every available CSV.

    Without raw OLD36 grids we cannot reproduce a single value, so
    every row is reported as PENDING_RAW_OLD36. The scaffolding is
    identical to the final harness so once the raw ZIPs arrive the
    only change is filling in the ``reproduce_row`` hook.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    availability = availability_snapshot()
    raw_ready = availability["present_sessions"] == availability["expected_sessions"]

    outcomes: list[RegressionOutcome] = []
    failures: list[dict] = []

    for path in list_cp24_csvs() + list_cp36_csvs():
        rel = path.relative_to(GOLDENS_DIR).as_posix()
        rows = list(read_golden_rows([path]))
        matched = failed = pending = 0
        if not raw_ready:
            pending = len(rows)
        else:
            # Placeholder: reproduce_row must be wired once the raw
            # OLD36 grids are imported. It is intentionally left as a
            # no-op here to avoid faking any golden reproduction.
            for _r in rows:
                pending += 1
        outcomes.append(
            RegressionOutcome(
                source_file=rel,
                total_rows=len(rows),
                matched=matched,
                failed=failed,
                pending=pending,
            )
        )

    # Summary CSV
    summary_path = REPORTS_DIR / "golden_regression_summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["source_file", "total_rows", "matched", "failed", "pending"],
        )
        w.writeheader()
        for o in outcomes:
            w.writerow(o.as_row())

    # Failures CSV (empty header row keeps the schema stable)
    failures_path = REPORTS_DIR / "golden_regression_failures.csv"
    with open(failures_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "source_file",
                "session",
                "asset",
                "feature",
                "horizon_ms",
                "q",
                "metric",
                "golden",
                "reproduced",
                "difference",
            ],
        )
        w.writeheader()
        for row in failures:
            w.writerow(row)

    # Rules JSON
    rules_json = {
        "generated_at_utc": _iso_utc(),
        "summary": status_summary(),
        "rules": [r.to_dict() for r in RULES],
    }
    (REPORTS_DIR / "recovery_rules.json").write_text(
        json.dumps(rules_json, indent=2, sort_keys=False), encoding="utf-8"
    )

    # Provenance JSON (per-rule detail with source artifact pointers)
    provenance = {
        "generated_at_utc": _iso_utc(),
        "rules": [
            {
                "key": r.key,
                "status": r.status.value,
                "confidence": r.confidence,
                "depends_on_raw_old36": r.depends_on_raw_old36,
                "source_artifacts": r.evidence,
                "inferred_hypothesis": r.inferred_hypothesis,
            }
            for r in RULES
        ],
    }
    (REPORTS_DIR / "recovery_provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )

    # Markdown report
    md = _render_markdown(outcomes, availability, raw_ready)
    (REPORTS_DIR / "recovery_report.md").write_text(md, encoding="utf-8")

    return {
        "summary_path": str(summary_path),
        "failures_path": str(failures_path),
        "rules_path": str(REPORTS_DIR / "recovery_rules.json"),
        "provenance_path": str(REPORTS_DIR / "recovery_provenance.json"),
        "report_path": str(REPORTS_DIR / "recovery_report.md"),
        "raw_ready": raw_ready,
        "availability": availability,
        "outcomes": [o.as_row() for o in outcomes],
        "rules_summary": status_summary(),
        "golden_counts": golden_counts(),
    }


def _render_markdown(outcomes, availability, raw_ready: bool) -> str:
    total_rows = sum(o.total_rows for o in outcomes)
    matched = sum(o.matched for o in outcomes)
    failed = sum(o.failed for o in outcomes)
    pending = sum(o.pending for o in outcomes)
    status_counts = status_summary()
    lines = [
        "# FrozenAnalysisEngine — Phase 2 Recovery Report",
        "",
        f"Generated: {_iso_utc()}",
        "",
        "## FrozenAnalysisEngine",
        "",
        "- Status: **NOT_CONFIGURED**  (unchanged; recovery is preparatory only)",
        "- Directional alpha: **REJECTED** (unchanged)",
        "- Live trading: **NOT AUTHORIZED** (unchanged)",
        "",
        "## NEW36 firewall",
        "",
        "- The recovery sandbox refuses to open any NEW36 raw ZIP.",
        "- Enforced by `recovery.allowlist.assert_recovery_allowed`.",
        "- Coverage: `tests/test_recovery_sandbox_firewall.py`.",
        "",
        "## OLD36 raw-reference availability",
        "",
        f"- Sessions present: **{availability['present_sessions']} / "
        f"{availability['expected_sessions']}**",
        f"- Nominal hours present: **{availability['present_nominal_hours']} / "
        f"{availability['expected_nominal_hours']} h**",
        f"- Raw-ready for reproduction: **{'YES' if raw_ready else 'NO'}**",
        "",
        "### Missing OLD36 raw sessions",
        "",
    ]
    if availability["missing_sessions"]:
        for sid in availability["missing_sessions"]:
            lines.append(f"- `{sid}`")
    else:
        lines.append("_none_")
    lines += [
        "",
        "## Golden regression summary",
        "",
        f"- Total golden rows across CP24/CP36 CSVs: **{total_rows}**",
        f"- Matched: **{matched}**",
        f"- Failed:  **{failed}**",
        f"- Pending raw OLD36: **{pending}**",
        "",
        "See `golden_regression_summary.csv` and `golden_regression_failures.csv`.",
        "",
        "## Rule status",
        "",
    ]
    for k, v in status_counts.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("See `recovery_rules.json` and `recovery_provenance.json`.")
    lines.append("")
    lines.append("## Next step")
    lines.append("")
    if raw_ready:
        lines.append(
            "All 11 OLD36 raw sessions are imported. Wire the "
            "`reproduce_row` hook and re-run the harness."
        )
    else:
        lines.append(
            "Upload the 11 OLD36_REFERENCE raw session ZIPs via the normal "
            "chunked uploader. Each session_id will be auto-tagged "
            "`OLD36_REFERENCE`; content-addressed retention deduplicates "
            "identical uploads; milestone totals are untouched."
        )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "run_regression",
    "RegressionOutcome",
    "PENDING",
    "MATCHED",
    "FAILED",
    "SKIPPED",
]
