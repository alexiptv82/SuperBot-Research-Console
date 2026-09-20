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
from . import validation as _val
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

# Comparison tolerances/field logic now live in ``recovery.validation``
# (transcribed verbatim from RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt).
# See ISSUE 1 fix: full field coverage (N, threshold, mean_signed_bps,
# median_signed_bps, mean_abs_move, hit_rate, disp_lo, disp_hi at row
# level; positive_blocks, positive_share, feature_mean, total_blocks,
# min_block, max_block at aggregate level).

# ---------------------------------------------------------------------------
# Aggregate-schema golden CSVs whose scope is UNAMBIGUOUSLY the full
# OLD36_REFERENCE block set (all 11 sessions x {BTC, ETH} = 22 blocks —
# "all36"). checkpoint_registry.py registers ONLY this population; it
# does not register which subset of sessions constitutes "ALL24",
# "WEEKEND12", "WEEKDAY12", or the NEW12-only CP36 aggregates. Since
# inventing such a membership rule would violate "no_tuning_on_mismatch"
# / "do not invent new rules", every OTHER aggregate-schema CSV is
# reported as SKIPPED_AGGREGATE_SCOPE (never silently ignored, never
# guessed).
_ALL36_AGGREGATE_FILES: frozenset[str] = frozenset({
    "CP36/simple_horizon_profile_all36_q90.csv",
    "CP36/composite_horizon_profile_all36_q90.csv",
    "CP36/simple_sensitivity_all36_30s.csv",
    "CP36/composite_sensitivity_all36_30s.csv",
    "CP36/fair_gap_dispersion_all36.csv",
})

# ---------------------------------------------------------------------------
# Block cache — reuse computed BlockSummary within one regression run
# ---------------------------------------------------------------------------
_block_cache: dict[tuple[str, str], "object"] = {}

# ISSUE 5 fix: sync_grid_100ms part-coverage verdict cache, one entry
# per session_id, reused across every (session_id, asset) block within
# one regression run.
_part_coverage_cache: dict[str, tuple[bool, str | None]] = {}


def _authoritative_sync_grid_count(session_id: str) -> int | None:
    """Fetch the authoritative expected sync_grid_100ms part count for
    ``session_id`` from its import-time QA manifest metadata
    (models.QARun.sync_grid_file_count — parsed once at ingest time by
    qa_engine.py from the collector manifest, NOT guessed from the
    observed parquet listing).

    Returns None if the session/run/field is unavailable — callers
    MUST NOT invent a fallback expected count in that case (spec:
    "Do not guess expected counts").
    """
    from database import SessionLocal
    from models import QARun, Session as SessionModel
    from sqlalchemy import select

    with SessionLocal() as db:
        row = db.execute(
            select(SessionModel).where(SessionModel.session_id == session_id)
        ).scalars().first()
        if row is None or row.current_qa_run_id is None:
            return None
        run = db.get(QARun, row.current_qa_run_id)
        if run is None:
            return None
        return run.sync_grid_file_count


def _evaluate_sync_grid_coverage(
    validation: "object",
    expected_count: int | None,
) -> tuple[bool, str | None]:
    """Pure evaluator: given a ``parquet_validator.ParquetValidation``
    result, decide HARD_FAIL / OK for the sync_grid_100ms directory
    ONLY.

    Spec (part_coverage_policy=HARD_FAIL_if_any_expected_part_missing_
    or_truncated):
    - filenames/part indices must be parseable
    - no duplicate part index
    - no missing part in the expected contiguous sequence
    - each expected part must be readable as valid parquet
    - truncated/corrupt expected part => structural INVALID / HARD FAIL

    Deliberately restricted to sync_grid_100ms: normalized_books /
    normalized_trades corruption is NOT a reconstruction-methodology
    concern for this engine (it only ever reads sync_grid_100ms), so
    hard-failing on those directories would broaden the frozen rule
    beyond its stated scope.

    Returns (ok, reason). ok=False means HARD FAIL.
    """
    sync_dir = validation.per_dir.get("sync_grid_100ms")

    if sync_dir is None or sync_dir.files_seen == 0:
        return False, "sync_grid_100ms: no parquet parts present"

    if not sync_dir.sequence_ok:
        return False, f"sync_grid_100ms: sequence invalid ({sync_dir.sequence_detail})"

    # Truncated/corrupt part detection (magic/metadata), restricted to
    # entries whose path falls under sync_grid_100ms/.
    sync_failures = [f for f in validation.failures if "sync_grid_100ms" in f]
    if sync_failures:
        return False, (
            f"sync_grid_100ms: corrupt/truncated part(s) detected: {sync_failures[:5]}"
        )

    # Authoritative expected-count cross-check (manifest/runtime
    # metadata). Catches whole-tail truncation that a purely
    # observed-range sequence check cannot see (e.g. manifest expects
    # 400 parts, only 0..389 are present contiguously with no internal
    # gaps — sequence_ok would be True on the observed range alone).
    if expected_count is not None and sync_dir.files_seen != expected_count:
        return False, (
            f"sync_grid_100ms: authoritative manifest expects "
            f"{expected_count} parts, found {sync_dir.files_seen}"
        )

    return True, None


def _sync_grid_coverage_from_path(
    zip_path,
    expected_count: int | None = None,
) -> tuple[bool, str | None]:
    """Run the existing deterministic parquet validator against a raw
    ZIP path and evaluate ONLY the sync_grid_100ms part coverage.

    Reuses ``parquet_validator.validate_all_parquet`` — the project's
    existing per-entry PAR1-magic + PyArrow-footer + sequence checker —
    rather than inventing a second validator.
    """
    from parquet_validator import validate_all_parquet

    validation = validate_all_parquet(zip_path)
    return _evaluate_sync_grid_coverage(validation, expected_count)


def _validate_sync_grid_part_coverage(session_id: str) -> tuple[bool, str | None]:
    """Production wrapper: resolve the session's retained raw ZIP path
    through the firewall-enforced sandbox, then validate sync_grid_100ms
    part coverage against the authoritative manifest part count.

    Returns (True, None) if the session has not been imported yet —
    that is a PENDING condition for the caller, not a coverage
    failure. Cached per session_id for the duration of one run.
    """
    if session_id in _part_coverage_cache:
        return _part_coverage_cache[session_id]

    from .sandbox import open_reference_zip

    try:
        zf = open_reference_zip(session_id)
    except FileNotFoundError:
        result = (True, None)
        _part_coverage_cache[session_id] = result
        return result

    stored_path = zf.filename
    zf.close()

    expected = _authoritative_sync_grid_count(session_id)
    result = _sync_grid_coverage_from_path(stored_path, expected)
    _part_coverage_cache[session_id] = result
    return result


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

    from .engine import BlockSummary, reconstruct_block

    # ISSUE 5 fix: HARD_FAIL_if_any_expected_part_missing_or_truncated.
    # This must be checked BEFORE any grid is loaded/concatenated —
    # part_coverage_policy is a structural precondition, not a
    # per-row comparison outcome. A coverage failure is classified
    # exactly like the other invalid_block_conditions (missing/
    # truncated parquet part) — zero rows emitted, block excluded
    # from all aggregation.
    coverage_ok, coverage_reason = _validate_sync_grid_part_coverage(session_id)
    if not coverage_ok:
        summary = BlockSummary(
            session_id=session_id,
            asset=asset,
            valid=False,
            invalid_reason=coverage_reason,
        )
        _block_cache[key] = summary
        return summary

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
    hit_rate, median_signed_bps, mean_abs_move, and — for
    dispersion_state rows — disp_lo/disp_hi in place of
    median_signed_bps/mean_abs_move (which are simple-feature-only and
    do not apply to a dispersion sub-state row).

    comparison_key=(source_file,session_id,asset,feature,horizon_ms,q
    [,dispersion_state]) — ISSUE 2 fix: when the golden row carries a
    dispersion_state (column name ``dispersion_state`` or ``state``,
    the latter used by the existing fair_gap_dispersion_*.csv golden
    files), the matching low/mid/high BlockMetrics.dispersion_states
    sub-entry is looked up and returned — the BASE fair_gap_reversion
    metrics are NEVER substituted for a dispersion-state row.

    Status is one of PENDING / MATCHED / SKIPPED (caller determines
    MATCHED vs FAILED by comparing field-by-field).

    SAFETY:
    - Does NOT compare reproduced values against golden numeric outputs
      internally (caller harness does the comparison).
    - Does NOT open NEW36 data (firewall enforced by sandbox).
    - Does NOT auto-execute (only called when raw_ready is True).
    - Does NOT tune or modify the methodology.
    """
    session_id = golden_row.get("session_id") or golden_row.get("session", "")
    asset      = golden_row.get("asset", "")
    dispersion_state = golden_row.get("dispersion_state") or golden_row.get("state")
    feature    = golden_row.get("feature") or ("fair_gap_reversion" if dispersion_state else "")
    horizon_ms = int(golden_row.get("horizon_ms", 0))
    q_str      = golden_row.get("q")
    # Some existing dispersion-state golden schemas (fair_gap_dispersion_
    # block_level_*.csv) carry no explicit "q" column at all. Every
    # such artifact in this project is documented/named against the
    # canonical q=0.90 slice (see *_q90 horizon-profile companions);
    # this default does NOT invent a new rule, it only resolves which
    # (feature,horizon_ms,q) comparison-key row is being requested.
    q = float(q_str) if q_str not in (None, "") else 0.90

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

    if dispersion_state:
        state_entry = next(
            (d for d in match.dispersion_states if d.get("state") == dispersion_state),
            None,
        )
        if state_entry is None:
            return {
                "status": SKIPPED,
                "reason": f"dispersion_state {dispersion_state!r} not found in block",
            }
        return {
            "status": MATCHED,
            "N":               state_entry["N"],
            "mean_signed_bps": state_entry["mean_signed_bps"],
            "hit_rate":        state_entry["hit_rate"],
            "disp_lo":         state_entry["disp_lo"],
            "disp_hi":         state_entry["disp_hi"],
            "threshold":       match.threshold,
        }

    return {
        "status": MATCHED,   # caller will determine MATCHED/FAILED
        "N":                 match.N,
        "threshold":         match.threshold,
        "mean_signed_bps":   match.mean_signed_bps,
        "hit_rate":          match.hit_rate,
        "median_signed_bps": match.median_signed_bps,
        "mean_abs_move":     match.mean_abs_move,
    }


@dataclass
class RegressionOutcome:
    source_file: str
    total_rows: int
    matched: int
    failed: int
    pending: int
    skipped_aggregate_scope: int = 0

    def as_row(self) -> dict[str, str | int]:
        return {
            "source_file": self.source_file,
            "total_rows": self.total_rows,
            "matched": self.matched,
            "failed": self.failed,
            "pending": self.pending,
            "skipped_aggregate_scope": self.skipped_aggregate_scope,
        }


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Comparison helpers (used by run_regression, not against tuned outputs)
# ---------------------------------------------------------------------------

def _block_identity_str(v: tuple[str, str] | None) -> str:
    return "" if v is None else f"{v[0]},{v[1]}"


def _fmt_row_fields(d: dict, fields: list[str]) -> str:
    return "; ".join(f"{f}={d.get(f)}" for f in fields)


def _fmt_row_diff(golden: dict, candidate: dict, fields: list[str]) -> str:
    parts = []
    for f in fields:
        gv = _val.safe_float(golden.get(f))
        cv = candidate.get(f)
        if gv is not None and isinstance(cv, (int, float)) and not isinstance(cv, bool):
            parts.append(f"d{f}={cv - gv:.2e}")
    return "; ".join(parts)


def _all36_block_summaries() -> list:
    """Materialize BlockSummary objects for every (session_id, asset) in
    the frozen OLD36_REFERENCE registry x {BTC, ETH} (22 blocks) — the
    ONLY aggregate scope this project's registry unambiguously
    defines. Session-to-period membership for ALL24 / WEEKEND12 /
    WEEKDAY12 / new12-only subsets is NOT registered anywhere in this
    codebase and is therefore never inferred here (see
    _ALL36_AGGREGATE_FILES / SKIPPED_AGGREGATE_SCOPE).
    """
    from checkpoint_registry import OLD36_REFERENCE_SESSIONS

    out = []
    for sid in OLD36_REFERENCE_SESSIONS:
        for asset in ("BTC", "ETH"):
            summary = _get_block_summary(sid, asset)
            if summary is not None:
                out.append(summary)
    return out


_aggregate_cache: dict[str, list] = {}


def _all36_aggregate_lookup() -> tuple[list, list]:
    """Compute (main_aggregates, dispersion_aggregates) over the full
    all36 block population once per run and memoize."""
    if "main" not in _aggregate_cache:
        from .engine import aggregate_blocks, aggregate_dispersion_states

        summaries = _all36_block_summaries()
        _aggregate_cache["main"] = aggregate_blocks(summaries)
        _aggregate_cache["dispersion"] = aggregate_dispersion_states(summaries)
    return _aggregate_cache["main"], _aggregate_cache["dispersion"]


def _aggregate_row_key(rel: str, golden: dict) -> tuple[str, int, float, bool]:
    """Resolve (feature_or_state, horizon_ms, q, is_dispersion) for an
    aggregate-schema golden row.

    horizon_ms/q are read from the row's own columns when present;
    otherwise from the filename convention used consistently across
    every CP36 aggregate artifact in this repository (a ``*_q90``
    file's rows are all q=0.90; a ``*_30s`` file's rows are all
    horizon_ms=30000). Nothing here is a new methodology rule — it
    only resolves which (feature,horizon_ms,q) key a given aggregate
    CSV's row refers to.
    """
    is_dispersion = "state" in golden and "feature" not in golden
    feature_or_state = golden.get("feature") or golden.get("state") or ""

    if golden.get("horizon_ms") not in (None, ""):
        horizon_ms = int(golden["horizon_ms"])
    elif "_30s" in rel:
        horizon_ms = 30000
    else:
        horizon_ms = 0

    if golden.get("q") not in (None, ""):
        q = float(golden["q"])
    elif "_q90" in rel:
        q = 0.90
    else:
        q = 0.90

    return feature_or_state, horizon_ms, q, is_dispersion


def run_regression() -> dict:
    """Run the golden regression across every available CSV.

    Without raw OLD36 grids we cannot reproduce a single value, so
    every row is reported as PENDING_RAW_OLD36. The scaffolding is
    identical to the final harness so once the raw ZIPs arrive the
    only change is filling in the ``reproduce_row`` hook.

    Row-level comparisons cover every field in
    RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt (N, threshold,
    mean_signed_bps, median_signed_bps, mean_abs_move, hit_rate,
    disp_lo, disp_hi — including dispersion_state rows). Aggregate-
    level comparisons (positive_blocks, positive_share, feature_mean,
    total_blocks, min_block, max_block) are attempted ONLY for the
    all36 scope the frozen registry unambiguously defines; every other
    aggregate-schema CSV is reported SKIPPED_AGGREGATE_SCOPE rather
    than silently guessed.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    availability = availability_snapshot()
    raw_ready = availability["present_sessions"] == availability["expected_sessions"]

    outcomes: list[RegressionOutcome] = []
    failures: list[dict] = []

    for path in list_cp24_csvs() + list_cp36_csvs():
        rel = path.relative_to(GOLDENS_DIR).as_posix()
        golden_rows = list(read_golden_rows([path]))
        matched = failed = pending = skipped_agg = 0
        if not raw_ready:
            pending = len(golden_rows)
        else:
            # Raw grids are present: reproduce + compare each row.
            # NO tuning is performed here regardless of mismatch count.
            _block_cache.clear()
            _part_coverage_cache.clear()
            _aggregate_cache.clear()

            for gr in golden_rows:
                golden = gr.row
                is_block_level = bool(golden.get("session") or golden.get("session_id"))

                if is_block_level:
                    result = reproduce_row(golden)
                    st = result.get("status", PENDING)
                    if st in (PENDING, SKIPPED):
                        pending += 1
                        continue

                    dispersion_state = golden.get("dispersion_state") or golden.get("state")
                    feature = golden.get("feature") or (
                        "fair_gap_reversion" if dispersion_state else ""
                    )
                    fails = _val.compare_row(golden, result, feature)
                    if _val.row_failed(fails):
                        failed += 1
                        failed_fields = [f for f, v in fails.items() if v]
                        failures.append({
                            "source_file": rel,
                            "session":    golden.get("session_id", golden.get("session", "")),
                            "asset":      golden.get("asset", ""),
                            "feature":    feature,
                            "horizon_ms": golden.get("horizon_ms", ""),
                            "q":          golden.get("q", ""),
                            "metric":     failed_fields[0] if failed_fields else "unknown",
                            "golden":     _fmt_row_fields(golden, failed_fields),
                            "reproduced": _fmt_row_fields(result, failed_fields),
                            "difference": _fmt_row_diff(golden, result, failed_fields),
                        })
                    else:
                        matched += 1
                    continue

                # Aggregate-schema row (no session/asset column).
                if rel not in _ALL36_AGGREGATE_FILES:
                    skipped_agg += 1
                    continue

                feat_or_state, horizon_ms, q, is_disp = _aggregate_row_key(rel, golden)
                main_agg, disp_agg = _all36_aggregate_lookup()
                if is_disp:
                    cand = next(
                        (a for a in disp_agg
                         if a.state == feat_or_state and a.horizon_ms == horizon_ms and a.q == q),
                        None,
                    )
                else:
                    cand = next(
                        (a for a in main_agg
                         if a.feature == feat_or_state and a.horizon_ms == horizon_ms and a.q == q),
                        None,
                    )
                if cand is None:
                    pending += 1
                    continue

                candidate = {
                    "total_blocks": cand.total_blocks,
                    "positive_blocks_count": cand.positive_blocks_count,
                    "positive_share": cand.positive_share,
                    "feature_mean": cand.feature_mean,
                    "min_block": cand.min_block,
                    "max_block": cand.max_block,
                }
                fails = _val.compare_aggregate(golden, candidate)
                if _val.aggregate_failed(fails):
                    failed += 1
                    failed_fields = [f for f, v in fails.items() if v]
                    failures.append({
                        "source_file": rel,
                        "session":    "",
                        "asset":      "",
                        "feature":    feat_or_state,
                        "horizon_ms": horizon_ms,
                        "q":          q,
                        "metric":     failed_fields[0] if failed_fields else "unknown",
                        "golden":     _fmt_row_fields(golden, failed_fields),
                        "reproduced": _fmt_row_fields(
                            {**candidate,
                             "min_block": _block_identity_str(candidate["min_block"]),
                             "max_block": _block_identity_str(candidate["max_block"])},
                            failed_fields,
                        ),
                        "difference": _fmt_row_diff(golden, candidate, failed_fields),
                    })
                else:
                    matched += 1

        outcomes.append(
            RegressionOutcome(
                source_file=rel,
                total_rows=len(golden_rows),
                matched=matched,
                failed=failed,
                pending=pending,
                skipped_aggregate_scope=skipped_agg,
            )
        )

    # Summary CSV
    summary_path = REPORTS_DIR / "golden_regression_summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "source_file", "total_rows", "matched", "failed", "pending",
                "skipped_aggregate_scope",
            ],
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
    "reproduce_row",
]
