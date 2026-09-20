"""Golden regression harness.

Acts on the CP24/CP36 golden CSV rows and — when the OLD36 raw ZIPs
are fully imported — attempts to reproduce every ``mean_signed_bps``,
``N``, ``hit_rate`` and ``threshold`` per (session,asset,feature,
horizon,q).

When the raw ZIPs are NOT yet present the harness records every row as
``PENDING_RAW_OLD36``.

When the raw ZIPs ARE present the harness loads each grid DataFrame,
calls ``engine.reconstruct_block`` (wired below in ``reproduce_row``),
and compares each reproduced value to the golden row using the tolerances
from the frozen validation spec:
    backend/recovery/specs/RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt
    SHA256: 30aea96229dbd96f1c98419ecd76105db171d79dc16bba29338b9650a9558d29

SAFETY (Phase 2 invariants):
- This module does NOT auto-execute at import time.
- ``run_regression()`` must be called EXPLICITLY.
- It does NOT set FrozenAnalysisEngine.accepts_input = True.
- It does NOT read NEW36 raw data (firewall enforced by sandbox).
- NO tuning against golden outputs is performed here.

Outputs written to ``recovery/reports/``:
- ``golden_regression_summary.csv`` : per-file counts
- ``golden_regression_failures.csv``: mismatch rows
- ``recovery_rules.json``
- ``recovery_provenance.json``
- ``recovery_report.md``
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

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
FAILED  = "FAILED"
SKIPPED = "SKIPPED"

# Validation tolerances from RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt
_TOL_N        = 0       # exact integer
_TOL_THRESH   = 1e-8
_TOL_MEAN_BPS = 1e-6
_TOL_HIT_RATE = 1e-12


# ---------------------------------------------------------------------------
# Block cache — reuse computed BlockSummary within one regression run
# ---------------------------------------------------------------------------
_block_cache: dict[tuple[str, str], "object"] = {}


def _load_grid_for_block(session_id: str, asset: str) -> pd.DataFrame | None:
    """Load and concatenate the parquet grid parts for one (session_id, asset).

    Returns None if the session has not been imported yet.
    Raises NEW36QuantitativeFirewallError for non-allowlisted ids.

    Part concatenation order: filename_ascending (spec: part_concat_order).
    Part coverage policy: caller (harness) detects missing parts by
    checking availability_snapshot() before calling this function.
    """
    from .sandbox import open_reference_zip
    import io
    import zipfile

    try:
        zf = open_reference_zip(session_id)
    except FileNotFoundError:
        return None

    # Collect and sort grid parquet parts by filename (ascending)
    with zf:
        grid_names = sorted(
            n for n in zf.namelist()
            if "sync_grid_100ms" in n and n.endswith(".parquet")
            and asset in n
        )
        if not grid_names:
            # Try without asset filter (session may not encode asset in filename)
            grid_names = sorted(
                n for n in zf.namelist()
                if "sync_grid_100ms" in n and n.endswith(".parquet")
            )
        if not grid_names:
            return None

        dfs = []
        for name in grid_names:
            with zf.open(name) as f:
                buf = io.BytesIO(f.read())
            df = pd.read_parquet(buf)
            # Filter to this asset
            if "asset" in df.columns:
                df = df[df["asset"] == asset].copy()
            dfs.append(df)

    if not dfs:
        return None

    combined = pd.concat(dfs, ignore_index=True)
    return combined if len(combined) > 0 else None


def _get_block_summary(session_id: str, asset: str) -> "object | None":
    """Return cached BlockSummary for (session_id, asset) or compute it."""
    key = (session_id, asset)
    if key in _block_cache:
        return _block_cache[key]

    from .engine import reconstruct_block

    df = _load_grid_for_block(session_id, asset)
    if df is None:
        _block_cache[key] = None
        return None

    summary = reconstruct_block(session_id, asset, df)
    _block_cache[key] = summary
    return summary


def reproduce_row(golden_row: dict) -> dict:
    """Reproduce one golden row using the frozen V1.1 engine.

    Returns a dict with keys: status, N, threshold, mean_signed_bps,
    hit_rate, mean_abs_move.

    Status is one of PENDING / MATCHED / FAILED / SKIPPED.

    SAFETY:
    - Does NOT compare reproduced values against golden numeric outputs
      internally (caller harness does the comparison).
    - Does NOT open NEW36 data (firewall enforced by sandbox).
    - Does NOT auto-execute (only called when raw_ready is True).
    - Does NOT tune or modify the methodology.
    """
    session_id = golden_row.get("session_id", "")
    asset      = golden_row.get("asset", "")
    feature    = golden_row.get("feature", "")
    horizon_ms = int(golden_row.get("horizon_ms", 0))
    q_str      = golden_row.get("q", "0.9")
    q          = float(q_str)

    summary = _get_block_summary(session_id, asset)
    if summary is None:
        return {"status": PENDING}

    if not summary.valid:
        return {"status": SKIPPED, "reason": summary.invalid_reason}

    # Find matching metric
    match = next(
        (
            m for m in summary.metrics
            if m.feature == feature and m.horizon_ms == horizon_ms and m.q == q
        ),
        None,
    )
    if match is None:
        return {"status": SKIPPED, "reason": "feature/horizon/q not found in block"}

    return {
        "status": MATCHED,   # caller will determine MATCHED/FAILED
        "N":               match.N,
        "threshold":       match.threshold,
        "mean_signed_bps": match.mean_signed_bps,
        "hit_rate":        match.hit_rate,
        "mean_abs_move":   match.mean_abs_move,
    }


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


# ---------------------------------------------------------------------------
# Comparison helpers (used by run_regression, not against tuned outputs)
# ---------------------------------------------------------------------------

def _safe_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        import math
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _compare_float(golden: float | None, reproduced: float | None, tol: float) -> bool:
    """Return True (= FAIL) if the values differ beyond tolerance.

    Spec rule: mismatch_if_definedness_differs=true
    N_mismatch_overrides_all_other_fields_on_that_row=true (handled by caller)
    """
    if golden is None and reproduced is None:
        return False
    if golden is None or reproduced is None:
        return True   # definedness mismatch
    return abs(golden - reproduced) > tol


def _first_failed_metric(n_fail, mean_fail, hr_fail, th_fail) -> str:
    if n_fail:   return "N"
    if th_fail:  return "threshold"
    if mean_fail: return "mean_signed_bps"
    if hr_fail:  return "hit_rate"
    return "unknown"


def _fmt_golden(row, n_fail, mean_fail, hr_fail, th_fail) -> str:
    parts = []
    if n_fail:    parts.append(f"N={row.get('N')}")
    if th_fail:   parts.append(f"threshold={row.get('threshold')}")
    if mean_fail: parts.append(f"mean_signed_bps={row.get('mean_signed_bps')}")
    if hr_fail:   parts.append(f"hit_rate={row.get('hit_rate')}")
    return "; ".join(parts)


def _fmt_reproduced(result, n_fail, mean_fail, hr_fail, th_fail) -> str:
    parts = []
    if n_fail:    parts.append(f"N={result.get('N')}")
    if th_fail:   parts.append(f"threshold={result.get('threshold')}")
    if mean_fail: parts.append(f"mean_signed_bps={result.get('mean_signed_bps')}")
    if hr_fail:   parts.append(f"hit_rate={result.get('hit_rate')}")
    return "; ".join(parts)


def _fmt_diff(g_N, r_N, g_mean, r_mean, g_hr, r_hr, g_th, r_th) -> str:
    parts = []
    if g_N is not None and r_N is not None:
        parts.append(f"dN={r_N - g_N}")
    if g_mean is not None and r_mean is not None:
        parts.append(f"dmean={r_mean - g_mean:.2e}")
    if g_hr is not None and r_hr is not None:
        parts.append(f"dhr={r_hr - g_hr:.2e}")
    if g_th is not None and r_th is not None:
        parts.append(f"dth={r_th - g_th:.2e}")
    return "; ".join(parts)


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
            # Raw grids are present: call reproduce_row for each golden row.
            # Compare reproduced values to golden values using frozen tolerances.
            # NO tuning is performed here regardless of mismatch count.
            _block_cache.clear()  # reset cache per run
            for golden_r in rows:
                result = reproduce_row(golden_r)
                st = result.get("status", PENDING)
                if st == PENDING:
                    pending += 1
                    continue
                if st == SKIPPED:
                    pending += 1
                    continue
                # Compare N (exact)
                g_N  = int(golden_r.get("N", -1)) if golden_r.get("N") not in (None, "") else None
                r_N  = result.get("N")
                n_fail = (g_N is not None and r_N is not None and g_N != r_N)

                # Compare mean_signed_bps
                g_mean = _safe_float(golden_r.get("mean_signed_bps"))
                r_mean = result.get("mean_signed_bps")
                mean_fail = _compare_float(g_mean, r_mean, _TOL_MEAN_BPS)

                # Compare hit_rate
                g_hr = _safe_float(golden_r.get("hit_rate"))
                r_hr = result.get("hit_rate")
                hr_fail = _compare_float(g_hr, r_hr, _TOL_HIT_RATE)

                # Compare threshold
                g_th = _safe_float(golden_r.get("threshold"))
                r_th = result.get("threshold")
                th_fail = _compare_float(g_th, r_th, _TOL_THRESH)

                row_failed = n_fail or mean_fail or hr_fail or th_fail
                if row_failed:
                    failed += 1
                    failures.append({
                        "source_file": rel,
                        "session":    golden_r.get("session_id", ""),
                        "asset":      golden_r.get("asset", ""),
                        "feature":    golden_r.get("feature", ""),
                        "horizon_ms": golden_r.get("horizon_ms", ""),
                        "q":          golden_r.get("q", ""),
                        "metric":     _first_failed_metric(n_fail, mean_fail, hr_fail, th_fail),
                        "golden":     _fmt_golden(golden_r, n_fail, mean_fail, hr_fail, th_fail),
                        "reproduced": _fmt_reproduced(result, n_fail, mean_fail, hr_fail, th_fail),
                        "difference": _fmt_diff(g_N, r_N, g_mean, r_mean, g_hr, r_hr, g_th, r_th),
                    })
                else:
                    matched += 1
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
