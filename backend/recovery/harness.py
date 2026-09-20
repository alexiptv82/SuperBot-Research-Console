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
from functools import lru_cache
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

# FAILED reason codes (ISSUE 4, Message 222 — FINAL HARNESS AUDIT FIX).
# A row that cannot be reproduced because the underlying block is
# structurally invalid, or because the requested (feature,horizon,q)
# / dispersion sub-state simply does not exist in the reconstructed
# block, is a HARD FAIL of the regression. It must NEVER be reported
# as PENDING (PENDING is reserved EXCLUSIVELY for "the raw OLD36 grid
# for this session/asset has not been imported yet") nor as a vague
# SKIPPED bucket that silently drops out of the matched/failed
# accounting.
STRUCTURAL_INVALID = "STRUCTURAL_INVALID"
EXPECTED_METRIC_MISSING = "EXPECTED_METRIC_MISSING"
DISPERSION_STATE_MISSING = "DISPERSION_STATE_MISSING"
# FINAL2 AUDIT ISSUE 2: a quantitative aggregate golden file with NO
# aggregate_scope_file_routing entry at all is a HARD FAIL of every
# row it contains — never a silent SKIPPED_AGGREGATE_SCOPE bucket.
AGGREGATE_ROUTING_MISSING = "AGGREGATE_ROUTING_MISSING"

# Exact allowlist (ISSUE 1, Message 222) of golden CSVs that are
# collector/QA session metadata only (grid/book/trade file counts,
# sampler lag, watchdog, writer-queue diagnostics, etc.) — they carry
# NO feature/asset/mean_signed_bps analysis output whatsoever and
# must NEVER be routed into reproduce_row() or counted as a
# quantitative pending/matched/failed/skipped instance. This is an
# EXACT allowlist of the three known frozen paths — deliberately NOT
# a filename/glob heuristic (a future golden artifact literally named
# "session_audit_something.csv" that DOES carry analysis columns must
# not be silently swept into this bucket).
METADATA_ONLY_FILES: frozenset[str] = frozenset({
    "CP24/session_audit_24h.csv",
    "CP36/session_audit_36h.csv",
    "CP36/session_audit_new12.csv",
})


class GoldenSourceHashMismatchError(Exception):
    """Raised when a golden CP24/CP36 CSV's SHA256 does not match the
    hash pinned in RECONSTRUCTION_V1.1_RECOVERED_METADATA.json at
    generation time (ISSUE 5, Message 222). HARD STOP — the harness
    must never proceed with ANY comparison against a golden source
    file that may have been tampered with, silently edited, replaced,
    or is missing/extra relative to the pinned 24-file registry."""

# Comparison tolerances/field logic now live in ``recovery.validation``
# (transcribed verbatim from RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt).
# See ISSUE 1 fix: full field coverage (N, threshold, mean_signed_bps,
# median_signed_bps, mean_abs_move, hit_rate, disp_lo, disp_hi at row
# level; positive_blocks, positive_share, feature_mean, total_blocks,
# min_block, max_block at aggregate level).

# ---------------------------------------------------------------------------
# Recovered validation metadata (AUDIT_BLOCKER_BLOCK_IDENTITY /
# AUDIT_BLOCKER_AGGREGATE_SCOPE closure)
# ---------------------------------------------------------------------------
#
# ALL scope membership (ALL36/OLD24/ALL24/NEW12/WEEKEND12/WEEKDAY12) and
# ALL min_block/max_block (session_id,asset) identities are read from a
# STATIC, pre-generated JSON artifact:
#
#     recovery/specs/RECONSTRUCTION_V1.1_RECOVERED_METADATA.json
#
# generated ONE TIME by recovery/tools/generate_recovered_metadata.py
# from golden-vs-golden evidence only (session_audit_*.csv, block-level
# golden CSVs, checkpoint_registry.OLD36_REFERENCE_SESSIONS). The
# harness NEVER recomputes scope membership or identity from a golden
# numeric extreme at runtime, and NEVER derives a candidate identity
# from a golden value — it only looks up the pre-recovered EXPECTED
# identity to compare against whatever identity the engine's own
# aggregate_blocks()/aggregate_dispersion_states() independently
# computed from the candidate block population.
_RECOVERED_METADATA_PATH = (
    Path(__file__).resolve().parent / "specs" / "RECONSTRUCTION_V1.1_RECOVERED_METADATA.json"
)

# ISSUE 3 (Message 222): the ONLY golden CSV whose positive_blocks
# column is a packed "A/B" string. Never generalized to any other
# file by filename pattern — this is an exact, single-path scope.
_PACKED_POSITIVE_BLOCKS_FILE = "CP36/selected_q90_30s_comparison_24h_new12_all36.csv"


class UnknownScopeError(Exception):
    """Raised when an aggregate golden row references a scope label
    that is NOT present in the static recovered-metadata registry.

    This is a HARD FAIL by design (spec: never silently skip or guess
    membership for an unrecognized scope)."""


@lru_cache(maxsize=1)
def _load_recovered_metadata() -> dict:
    with open(_RECOVERED_METADATA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_scope_session_ids(scope_name: str) -> tuple[str, ...]:
    """Look up a scope's session_id membership from the static
    recovered-metadata artifact. Raises UnknownScopeError for any
    scope label not present there — never guesses."""
    meta = _load_recovered_metadata()
    scopes = meta.get("scopes", {})
    entry = scopes.get(scope_name)
    if entry is None:
        for candidate in scopes.values():
            if scope_name in candidate.get("aliases", []):
                entry = candidate
                break
    if entry is None:
        raise UnknownScopeError(
            f"Unknown aggregate scope {scope_name!r} is not present in "
            f"RECONSTRUCTION_V1.1_RECOVERED_METADATA.json — refusing to "
            f"guess session membership."
        )
    return tuple(entry["session_ids"])


def _expected_block_identity(source_file: str, row_index: int, scope: str) -> dict | None:
    """Look up the pre-recovered EXPECTED (session_id, asset) identity
    for a given aggregate golden row's min_block/max_block, purely
    from the static metadata artifact. Returns None if this exact
    (source_file, row_index, scope) was not recovered (e.g. the file
    carries no min_block/max_block columns at all)."""
    meta = _load_recovered_metadata()
    for entry in meta.get("block_identities", {}).get(source_file, []):
        if entry.get("row_index") == row_index and entry.get("scope") == scope:
            return entry
    return None


def _aggregate_scope_routing() -> dict:
    """Per-file scope routing rules — resolved from the SAME recovered
    metadata artifact (aggregate_scope_file_routing section)."""
    return _load_recovered_metadata().get("aggregate_scope_file_routing", {})

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

    Status is one of PENDING / MATCHED / FAILED (ISSUE 4, Message
    222): PENDING means the raw OLD36 grid has not been imported yet;
    FAILED (with a reason_code of STRUCTURAL_INVALID /
    EXPECTED_METRIC_MISSING / DISPERSION_STATE_MISSING) means the
    block/metric/dispersion-state could not be reproduced at all and
    is a hard regression failure; MATCHED means comparable candidate
    values were produced and the caller now performs the real
    field-by-field numeric comparison (which may still yield a FAILED
    row at the run_regression() level if a value differs).

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
        # The raw OLD36 grid for this (session_id, asset) has not been
        # imported yet — the ONLY condition this function reports as
        # PENDING (ISSUE 4, Message 222).
        return {"status": PENDING}

    if not summary.valid:
        return {
            "status": FAILED,
            "reason_code": STRUCTURAL_INVALID,
            "reason": summary.invalid_reason,
        }

    # Find matching metric
    match = next(
        (
            m for m in summary.metrics
            if m.feature == feature and m.horizon_ms == horizon_ms and m.q == q
        ),
        None,
    )
    if match is None:
        return {
            "status": FAILED,
            "reason_code": EXPECTED_METRIC_MISSING,
            "reason": "feature/horizon/q not found in block",
        }

    if dispersion_state:
        state_entry = next(
            (d for d in match.dispersion_states if d.get("state") == dispersion_state),
            None,
        )
        if state_entry is None:
            return {
                "status": FAILED,
                "reason_code": DISPERSION_STATE_MISSING,
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
    # FINAL2 AUDIT (Message: FINAL HARNESS CORRECTION): this counter is
    # now ALWAYS 0 for real runs — both of its former sources (an
    # unrouted aggregate file, and a raw_ready-but-missing aggregate
    # candidate) are hard FAILED (AGGREGATE_ROUTING_MISSING /
    # EXPECTED_METRIC_MISSING / DISPERSION_STATE_MISSING) instead of
    # silently skipped/pending. The field/column is kept for CSV
    # schema stability and as a permanent invariant-accounting slot.
    # ISSUE 2 (Message 222): file_kind/total_instances distinguish the
    # SOURCE CSV row count (total_rows) from the number of individual
    # quantitative comparison instances that count actually expands
    # into. For block-level files and narrow (single-scope-per-row)
    # aggregate files these are identical. For wide multi-scope
    # aggregate files (weekend_vs_weekday_selected_30s.csv: 3 scopes;
    # selected_q90_30s_comparison_24h_new12_all36.csv: 3 scopes) one
    # source row expands to N instances (e.g. 11 rows -> 33
    # instances) — matched+failed+pending+skipped_aggregate_scope
    # MUST sum to total_instances, never to total_rows, for those
    # files. METADATA_ONLY files report total_instances=0 (no
    # quantitative comparison is ever attempted against them).
    file_kind: str = "BLOCK_LEVEL"
    total_instances: int = 0

    def as_row(self) -> dict[str, str | int]:
        return {
            "source_file": self.source_file,
            "file_kind": self.file_kind,
            "total_rows": self.total_rows,
            "total_instances": self.total_instances,
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


_scope_block_cache: dict[str, list] = {}


def _scope_block_summaries(scope_name: str) -> list:
    """Materialize BlockSummary objects for every (session_id, asset) in
    the given scope's RECOVERED session_id membership x {BTC, ETH}.

    Scope membership comes exclusively from the static
    RECONSTRUCTION_V1.1_RECOVERED_METADATA.json artifact (see
    _resolve_scope_session_ids) — never guessed, never derived from a
    candidate value.
    """
    if scope_name in _scope_block_cache:
        return _scope_block_cache[scope_name]
    session_ids = _resolve_scope_session_ids(scope_name)
    out = []
    for sid in session_ids:
        for asset in ("BTC", "ETH"):
            summary = _get_block_summary(sid, asset)
            if summary is not None:
                out.append(summary)
    _scope_block_cache[scope_name] = out
    return out


_scope_aggregate_cache: dict[str, tuple] = {}


def _scope_aggregate_lookup(scope_name: str) -> tuple[list, list]:
    """Compute (main_aggregates, dispersion_aggregates) for one scope's
    block population and memoize per scope for this run."""
    if scope_name not in _scope_aggregate_cache:
        from .engine import aggregate_blocks, aggregate_dispersion_states

        summaries = _scope_block_summaries(scope_name)
        main = aggregate_blocks(summaries)
        disp = aggregate_dispersion_states(summaries)
        _scope_aggregate_cache[scope_name] = (main, disp)
    return _scope_aggregate_cache[scope_name]


def _aggregate_row_key(
    golden: dict, horizon_default: int | None, q_default: float | None,
) -> tuple[str, int | None, float | None, bool]:
    """Resolve (feature_or_state, horizon_ms, q, is_dispersion) for one
    narrow-schema aggregate golden row. horizon_default/q_default come
    from the recovered-metadata file-routing rules (filename-implied
    conventions already documented in
    aggregate_scope_file_routing) — never invented ad hoc here.
    """
    is_dispersion = "state" in golden and "feature" not in golden
    feature_or_state = golden.get("feature") or golden.get("state") or ""
    horizon_ms = (
        int(golden["horizon_ms"]) if golden.get("horizon_ms") not in (None, "") else horizon_default
    )
    q = float(golden["q"]) if golden.get("q") not in (None, "") else q_default
    return feature_or_state, horizon_ms, q, is_dispersion


def _iter_aggregate_row_views(rel: str, golden: dict):
    """Yield (scope, feature_or_state, horizon_ms, q, golden_subview,
    is_dispersion) for every scope embedded in one aggregate golden
    row, per the recovered aggregate_scope_file_routing rules.

    Narrow-schema files (one scope per row, via a ``period`` column or
    filename convention) yield exactly one view. Wide multi-scope
    files (weekend_vs_weekday_selected_30s.csv,
    selected_q90_30s_comparison_24h_new12_all36.csv) yield one view
    per embedded scope column-group.

    Raises UnknownScopeError if ``rel`` has no routing entry at all —
    callers must treat that as a structural (file-level) condition,
    distinct from a per-row unrecognized scope VALUE (also
    UnknownScopeError, raised later by _resolve_scope_session_ids).
    """
    routing = _aggregate_scope_routing().get(rel)
    if routing is None:
        raise UnknownScopeError(f"{rel!r} has no aggregate_scope_file_routing entry")

    source = routing["scope_source"]

    if source == "wide_columns":
        feature_or_state = golden.get("feature") or golden.get("state") or ""
        is_disp = "state" in golden and "feature" not in golden
        for scope in routing["scopes"]:
            sub = {
                "mean_signed_bps": golden.get(f"{scope}_mean_signed_bps"),
                "positive_blocks": golden.get(f"{scope}_positive_blocks"),
                "blocks":          golden.get(f"{scope}_blocks"),
                "min_block":       golden.get(f"{scope}_min_block"),
                "max_block":       golden.get(f"{scope}_max_block"),
            }
            yield scope, feature_or_state, 30000, 0.90, sub, is_disp
        return

    if source == "wide_columns_prefixed":
        feature_or_state = golden.get("feature") or ""
        # ISSUE 3 (Message 222): the packed "A/B" positive_blocks/
        # total_blocks string format is a SCOPED, deterministic parse
        # applied ONLY to the one known frozen schema that uses it
        # (CP36/selected_q90_30s_comparison_24h_new12_all36.csv).
        # Every other wide_columns_prefixed-routed file (there is
        # none today, but if one is ever added without this exact
        # packed convention) keeps the plain safe_int() behavior
        # untouched — this is never widened into a generic parsing
        # rule.
        use_packed_parser = rel == _PACKED_POSITIVE_BLOCKS_FILE
        for prefix, scope in routing["scope_prefix_map"].items():
            raw_positive_blocks = golden.get(f"{prefix}_positive_blocks")
            if use_packed_parser:
                positive_blocks, total_blocks = _val.parse_packed_positive_blocks(
                    raw_positive_blocks
                )
                sub = {
                    "mean_signed_bps": golden.get(f"{prefix}_mean_bps"),
                    "positive_blocks": positive_blocks,
                    "total_blocks":    total_blocks,
                }
            else:
                sub = {
                    "mean_signed_bps": golden.get(f"{prefix}_mean_bps"),
                    "positive_blocks": raw_positive_blocks,
                }
            yield scope, feature_or_state, 30000, 0.90, sub, False
        return

    if source == "period_column":
        scope = golden.get("period")
    elif source == "filename":
        scope = routing["scope"]
    else:
        raise UnknownScopeError(f"{rel!r}: unrecognized scope_source {source!r}")

    horizon_default = 30000 if rel.endswith("_30s.csv") else None
    q_default = 0.90 if "_q90" in rel else None
    feat_or_state, horizon_ms, q, is_disp = _aggregate_row_key(golden, horizon_default, q_default)
    yield scope, feat_or_state, horizon_ms, q, golden, is_disp


def _instances_per_source_row(rel: str, file_is_block_level: bool) -> int:
    """Number of quantitative comparison INSTANCES one golden CSV row
    expands into (ISSUE 2, Message 222).

    Block-level rows (keyed on session_id/session + asset) always
    reproduce exactly one instance. Aggregate-schema rows expand to
    one instance PER embedded scope for wide multi-scope files
    (wide_columns / wide_columns_prefixed routing —
    weekend_vs_weekday_selected_30s.csv and
    selected_q90_30s_comparison_24h_new12_all36.csv both embed 3
    scopes per row) and to exactly one instance for every narrow
    (single-scope-per-row) aggregate file. Never guessed — read from
    the SAME static aggregate_scope_file_routing registry used
    everywhere else in this module. An unrouted aggregate file (none
    exist today among the 24 pinned goldens) reports 1, matching the
    existing skipped_aggregate_scope per-row accounting.
    """
    if file_is_block_level:
        return 1
    routing = _aggregate_scope_routing().get(rel)
    if routing is None:
        return 1
    source = routing.get("scope_source")
    if source == "wide_columns":
        return len(routing["scopes"])
    if source == "wide_columns_prefixed":
        return len(routing["scope_prefix_map"])
    return 1


def _verify_golden_source_hashes() -> None:
    """GOLDEN_SOURCE_HASH_MISMATCH preflight (ISSUE 5, Message 222).

    MUST run before ANY candidate RAW is opened or ANY quantitative
    comparison is attempted — this is the very first statement of
    ``run_regression()``. Recomputes SHA256 for every golden CP24/CP36
    CSV currently on disk and compares it against the hashes pinned
    in RECONSTRUCTION_V1.1_RECOVERED_METADATA.json#golden_csv_sha256
    at metadata-generation time. Also verifies the pinned registry and
    the on-disk file set are IDENTICAL (a missing/renamed/extra golden
    CSV is itself a hash-integrity concern, never silently ignored).

    HARD STOP: raises GoldenSourceHashMismatchError on ANY mismatch.
    Never widens tolerance, never skips a file, never continues.
    """
    import hashlib

    meta = _load_recovered_metadata()
    pinned: dict = meta.get("golden_csv_sha256") or {}
    on_disk = list_cp24_csvs() + list_cp36_csvs()
    on_disk_rel = {p.relative_to(GOLDENS_DIR).as_posix() for p in on_disk}

    if not pinned:
        raise GoldenSourceHashMismatchError(
            "GOLDEN_SOURCE_HASH_MISMATCH: RECONSTRUCTION_V1.1_RECOVERED_"
            "METADATA.json carries no golden_csv_sha256 registry."
        )

    if set(pinned.keys()) != on_disk_rel:
        raise GoldenSourceHashMismatchError(
            "GOLDEN_SOURCE_HASH_MISMATCH: pinned golden_csv_sha256 registry "
            f"does not match the golden CSVs on disk. "
            f"pinned_only={sorted(set(pinned) - on_disk_rel)} "
            f"disk_only={sorted(on_disk_rel - set(pinned))}"
        )

    for path in on_disk:
        rel = path.relative_to(GOLDENS_DIR).as_posix()
        expected = pinned[rel]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise GoldenSourceHashMismatchError(
                f"GOLDEN_SOURCE_HASH_MISMATCH: {rel!r} sha256 {actual} != "
                f"pinned {expected}"
            )


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
    total_blocks, min_block, max_block) are attempted for EVERY known
    scope recovered in RECONSTRUCTION_V1.1_RECOVERED_METADATA.json
    (ALL36, OLD24/ALL24, NEW12, WEEKEND12, WEEKDAY12, and the wide
    multi-scope comparison files) — no known frozen golden scope is
    reported SKIPPED_AGGREGATE_SCOPE. A row referencing a scope value
    NOT present in that recovered registry is a HARD FAIL
    (UnknownScopeError), never silently skipped or guessed. Expected
    min_block/max_block identities are read from the SAME static
    metadata (never derived from a golden numeric extreme at runtime);
    only the CANDIDATE identity is computed live by
    engine.aggregate_blocks()/aggregate_dispersion_states().

    ISSUE 1 (Message 222): the 3 METADATA_ONLY_FILES (session-audit
    collector/QA diagnostics) are excluded entirely, before any other
    classification — they are never routed into reproduce_row() or
    counted as a pending/matched/failed/skipped instance.

    ISSUE 2 (Message 222): every RegressionOutcome reports BOTH
    total_rows (source CSV rows) and total_instances (quantitative
    comparison instances — total_rows * embedded-scope-count for wide
    multi-scope files, total_rows otherwise).
    matched+failed+pending+skipped_aggregate_scope always sums to
    total_instances for every non-metadata file.

    ISSUE 5 (Message 222): the golden CSV SHA256 preflight
    (_verify_golden_source_hashes) runs FIRST, before anything else
    in this function.
    """
    _verify_golden_source_hashes()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    availability = availability_snapshot()
    raw_ready = availability["present_sessions"] == availability["expected_sessions"]

    outcomes: list[RegressionOutcome] = []
    failures: list[dict] = []

    for path in list_cp24_csvs() + list_cp36_csvs():
        rel = path.relative_to(GOLDENS_DIR).as_posix()
        golden_rows = list(read_golden_rows([path]))

        if rel in METADATA_ONLY_FILES:
            # ISSUE 1: collector/QA session metadata carries no
            # feature/asset/mean_signed_bps analysis output — never
            # touched by reproduce_row(), never counted as a
            # quantitative instance.
            outcomes.append(
                RegressionOutcome(
                    source_file=rel,
                    file_kind="METADATA_ONLY",
                    total_rows=len(golden_rows),
                    total_instances=0,
                    matched=0,
                    failed=0,
                    pending=0,
                    skipped_aggregate_scope=0,
                )
            )
            continue

        file_is_block_level = bool(
            golden_rows
            and (golden_rows[0].row.get("session") or golden_rows[0].row.get("session_id"))
        )
        instances_per_row = _instances_per_source_row(rel, file_is_block_level)
        total_instances = len(golden_rows) * instances_per_row

        matched = failed = pending = skipped_agg = 0
        if not raw_ready:
            pending = total_instances
        else:
            # Raw grids are present: reproduce + compare each row.
            # NO tuning is performed here regardless of mismatch count.
            _block_cache.clear()
            _part_coverage_cache.clear()
            _scope_block_cache.clear()
            _scope_aggregate_cache.clear()

            for row_index, gr in enumerate(golden_rows):
                golden = gr.row
                is_block_level = bool(golden.get("session") or golden.get("session_id"))

                if is_block_level:
                    result = reproduce_row(golden)
                    st = result.get("status", PENDING)

                    if st == PENDING:
                        # ISSUE 4: PENDING is reserved EXCLUSIVELY for
                        # "raw OLD36 grid for this session/asset not
                        # imported yet".
                        pending += 1
                        continue

                    if st == FAILED:
                        # ISSUE 4: structural/metric/dispersion-state
                        # hard fail — never silently reported as
                        # PENDING or a vague SKIPPED bucket.
                        failed += 1
                        failures.append({
                            "source_file": rel,
                            "session":    golden.get("session_id", golden.get("session", "")),
                            "asset":      golden.get("asset", ""),
                            "feature":    golden.get("feature", golden.get("state", "")),
                            "horizon_ms": golden.get("horizon_ms", ""),
                            "q":          golden.get("q", ""),
                            "metric":     result.get("reason_code", "unknown"),
                            "golden":     "",
                            "reproduced": result.get("reason", ""),
                            "difference": "",
                        })
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
                if rel not in _aggregate_scope_routing():
                    # FINAL2 AUDIT ISSUE 2: an unrouted quantitative
                    # aggregate file is a HARD FAIL, never a silent
                    # skip. Count the full instances_per_row for this
                    # row so the per-file instance invariant still
                    # sums to total_instances exactly (this never
                    # happens for any of the 24 frozen golden files
                    # today — only reachable via a test-injected
                    # synthetic routing gap).
                    failed += instances_per_row
                    failures.append({
                        "source_file": rel, "session": "", "asset": "",
                        "feature": "", "horizon_ms": "", "q": "",
                        "metric": AGGREGATE_ROUTING_MISSING,
                        "golden": f"{rel!r} has no aggregate_scope_file_routing entry",
                        "reproduced": "", "difference": "",
                    })
                    continue

                try:
                    row_views = list(_iter_aggregate_row_views(rel, golden))
                except (UnknownScopeError, _val.PackedFieldParseError) as exc:
                    # ISSUE 3: a malformed packed "A/B" positive_blocks
                    # value is a HARD FAIL of that row's instances, not
                    # a silent skip/pending and never a coercion. Count
                    # ALL instances_per_row instances this row would
                    # otherwise have expanded into as failed, so the
                    # per-file instance accounting (ISSUE 2) still
                    # sums to total_instances exactly.
                    failed += instances_per_row
                    failures.append({
                        "source_file": rel, "session": "", "asset": "",
                        "feature": "", "horizon_ms": "", "q": "",
                        "metric": "scope", "golden": str(exc),
                        "reproduced": "", "difference": "",
                    })
                    continue

                for scope, feat_or_state, horizon_ms, q, sub_golden, is_disp in row_views:
                    try:
                        main_agg, disp_agg = _scope_aggregate_lookup(scope)
                    except UnknownScopeError as exc:
                        failed += 1
                        failures.append({
                            "source_file": rel, "session": "", "asset": "",
                            "feature": feat_or_state, "horizon_ms": horizon_ms, "q": q,
                            "metric": "scope", "golden": str(exc),
                            "reproduced": "", "difference": "",
                        })
                        continue

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
                        # FINAL2 AUDIT ISSUE 1: this branch is ONLY
                        # reached when raw_ready is True (it lives
                        # inside the `else:` of `if not raw_ready:` —
                        # see above). PENDING is reserved EXCLUSIVELY
                        # for "raw genuinely unavailable" (handled by
                        # the `if not raw_ready:` branch and by
                        # reproduce_row()'s summary-is-None case for
                        # block-level rows). An expected aggregate
                        # candidate that cannot be found while raw IS
                        # available is a HARD FAIL, never PENDING.
                        failed += 1
                        reason_code = (
                            DISPERSION_STATE_MISSING if is_disp else EXPECTED_METRIC_MISSING
                        )
                        failures.append({
                            "source_file": rel, "session": "", "asset": "",
                            "feature": feat_or_state, "horizon_ms": horizon_ms, "q": q,
                            "metric": reason_code,
                            "golden": _fmt_row_fields(sub_golden, list(sub_golden.keys())),
                            "reproduced": "", "difference": "",
                        })
                        continue

                    # Expected min_block/max_block identity comes ONLY
                    # from the static recovered metadata — never from
                    # the golden numeric extreme at runtime, never
                    # from the candidate.
                    expected_id = _expected_block_identity(rel, row_index, scope)
                    golden_for_compare = dict(sub_golden)
                    if expected_id and expected_id.get("min_block"):
                        golden_for_compare["min_block"] = (
                            expected_id["min_block"]["session_id"],
                            expected_id["min_block"]["asset"],
                        )
                    if expected_id and expected_id.get("max_block"):
                        golden_for_compare["max_block"] = (
                            expected_id["max_block"]["session_id"],
                            expected_id["max_block"]["asset"],
                        )

                    candidate = {
                        "total_blocks": cand.total_blocks,
                        "positive_blocks_count": cand.positive_blocks_count,
                        "positive_share": cand.positive_share,
                        "feature_mean": cand.feature_mean,
                        "min_block": cand.min_block,
                        "max_block": cand.max_block,
                    }
                    fails = _val.compare_aggregate(golden_for_compare, candidate)
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
                            "golden":     _fmt_row_fields(golden_for_compare, failed_fields),
                            "reproduced": _fmt_row_fields(
                                {**candidate,
                                 "min_block": _block_identity_str(candidate["min_block"]),
                                 "max_block": _block_identity_str(candidate["max_block"])},
                                failed_fields,
                            ),
                            "difference": _fmt_row_diff(golden_for_compare, candidate, failed_fields),
                        })
                    else:
                        matched += 1

        # ISSUE 2 invariant: every non-metadata file's
        # matched+failed+pending+skipped_aggregate_scope MUST sum to
        # exactly total_instances — never to total_rows for a wide
        # multi-scope file. This is asserted here (not just
        # documented) so any future regression in the counting logic
        # fails loudly instead of silently under-reporting.
        _computed_total = matched + failed + pending + skipped_agg
        assert _computed_total == total_instances, (
            f"{rel}: instance accounting mismatch "
            f"({_computed_total} != total_instances={total_instances})"
        )

        outcomes.append(
            RegressionOutcome(
                source_file=rel,
                file_kind="BLOCK_LEVEL" if file_is_block_level else "AGGREGATE",
                total_rows=len(golden_rows),
                total_instances=total_instances,
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
                "source_file", "file_kind", "total_rows", "total_instances",
                "matched", "failed", "pending", "skipped_aggregate_scope",
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
    total_instances = sum(o.total_instances for o in outcomes)
    matched = sum(o.matched for o in outcomes)
    failed = sum(o.failed for o in outcomes)
    pending = sum(o.pending for o in outcomes)
    metadata_only_files = sum(1 for o in outcomes if o.file_kind == "METADATA_ONLY")
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
        f"- Total quantitative comparison instances: **{total_instances}** "
        f"(wide multi-scope files expand 1 row into multiple instances)",
        f"- METADATA_ONLY files excluded from instances (session-audit "
        f"collector/QA diagnostics): **{metadata_only_files}**",
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
    "STRUCTURAL_INVALID",
    "EXPECTED_METRIC_MISSING",
    "DISPERSION_STATE_MISSING",
    "AGGREGATE_ROUTING_MISSING",
    "METADATA_ONLY_FILES",
    "GoldenSourceHashMismatchError",
    "reproduce_row",
]
