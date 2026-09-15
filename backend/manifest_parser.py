"""Tolerant, adaptable manifest / runtime parser.

Per user directive: do NOT invent or guess critical values. Missing
critical fields yield UNRESOLVED. Values that come directly from the
uploaded ZIP are treated as facts; anything else is left as ``None``.

Real MultiVenue 3H session ZIPs (v20260911, verified against session
``20260911T040213Z_43a798b7``) ship THREE JSON artifacts at the root:

- ``manifest.json``           \u2014 collector name, session_id, run_minutes,
                                sample_ms, venues, connections, notes.
- ``runtime_status.json``     \u2014 counters (errors, reconnects, writer_errors,
                                sampler_missed_ticks_total, sampler_lag_events_gt50ms,
                                unknown_side), buffers, parts, connection_generation.
- ``fast_collection_summary.json`` \u2014 collector_sha256, exit_code,
                                    watchdog_triggered, target_hours,
                                    wrapper_elapsed_hours, files counts.

The parser reads all three, merges them into a single ``raw`` dict
(``summary`` > ``runtime`` > ``manifest`` on key collision, since the
summary is authoritative for the wrapper's post-run verdict), then
resolves every canonical field through ``FIELD_ALIASES``. A few fields
are DERIVED from arrays/dicts (e.g. total writer_errors, aggregated
reconnect summary, final buffer status) \u2014 all derivations are pure
functions of values that were present in the ZIP.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from zip_security import ZipInspection, ZipSource, find_entry, read_small_entry

# Canonical -> ordered aliases (dotted paths supported). Empty tuple entries
# will be resolved via DERIVATIONS below.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # Identity / timing
    "session_id": ("session_id", "sessionId", "session.id"),
    "start_time": ("start_time", "startTime", "session.start_time", "start_utc", "created_utc"),
    "end_time": ("end_time", "endTime", "session.end_time", "end_utc", "updated_utc"),
    "duration_hours": (
        "duration_hours",
        "durationHours",
        "session.duration_hours",
        "wrapper_elapsed_hours",
        "target_hours",
    ),
    # Critical wrapper fields
    "exit_code": ("exit_code", "exitCode", "process.exit_code", "runtime.exit_code"),
    "watchdog": ("watchdog", "watchdog_triggered", "runtime.watchdog"),
    "collector_sha256": (
        "collector_sha256",
        "collectorSha256",
        "collector.sha256",
        "collector_hash",
        "collector.hash",
    ),
    # Runtime signals
    "writer_errors": (
        "writer_errors",
        "writerErrors",
        "io.writer_errors",
        "counters.writer_errors",
    ),
    "missed_ticks": (
        "missed_ticks",
        "missedTicks",
        "sampler.missed_ticks",
        "counters.sampler_missed_ticks_total",
    ),
    "theoretical_ticks": (
        "theoretical_ticks",
        "theoreticalTicks",
        "sampler.theoretical_ticks",
    ),
    "lag_gt_50ms": (
        "lag_gt_50ms",
        "lagGt50ms",
        "sampler.lag_gt_50ms",
        "counters.sampler_lag_events_gt50ms",
    ),
    "max_lag_ms": ("max_lag_ms", "maxLagMs", "sampler.max_lag_ms"),
    # Fields that require aggregation \u2014 aliases empty on purpose;
    # DERIVATIONS below builds them from ``counters.*``.
    "reconnect_summary": ("reconnect_summary", "runtime.reconnect_summary"),
    "reconnect_count": ("reconnect_count", "reconnects.count", "runtime.reconnect_count"),
    "websocket_errors": ("websocket_errors", "ws_errors", "runtime.websocket_errors"),
    "unknown_sides": ("unknown_sides", "unknown_side_count", "trades.unknown_sides"),
    "final_buffer_status": (
        "final_buffer_status",
        "buffers.status",
        "runtime.final_buffer_status",
    ),
    "buffers": ("buffers", "final_buffers", "runtime.final_buffers"),
}

# Critical set (per handoff \u00a712.6). Missing any of these under any known
# alias => UNRESOLVED. Never fabricated.
CRITICAL_FIELDS: tuple[str, ...] = (
    "exit_code",
    "watchdog",
    "collector_sha256",
)

MANIFEST_ENTRY_CANDIDATES: tuple[str, ...] = (
    "manifest.json",
    "session_manifest.json",
    "run_manifest.json",
    "metadata.json",
)
RUNTIME_ENTRY_CANDIDATES: tuple[str, ...] = (
    "runtime_status.json",
    "runtime.json",
    "runtime_state.json",
)
SUMMARY_ENTRY_CANDIDATES: tuple[str, ...] = (
    "fast_collection_summary.json",
    "collection_summary.json",
    "session_summary.json",
    "summary.json",
)

# Session ID pattern per handoff \u00a79: 20260910T123759Z_5ab
_SESSION_ID_PATTERN = re.compile(r"([0-9]{8}T[0-9]{6}Z_[A-Za-z0-9]+)")


@dataclass
class ManifestResult:
    found: bool = False
    entries_found: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    resolved: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    parse_error: str | None = None
    derivations: dict[str, str] = field(default_factory=dict)


def _get_nested(d: Any, path: str) -> Any:
    parts = path.split(".")
    cur = d
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur


def _resolve_field(raw: dict[str, Any], aliases: Iterable[str]) -> tuple[bool, Any]:
    for a in aliases:
        val = _get_nested(raw, a)
        if val is not None:
            return True, val
    return False, None


def _find_and_read(zip_source: ZipSource, inspection: ZipInspection, candidates: tuple[str, ...]) -> tuple[str | None, bytes | None]:
    entry = find_entry(inspection, candidates)
    if entry is None:
        return None, None
    return entry.name, read_small_entry(zip_source, entry.name, max_bytes=4 * 1024 * 1024)


def _safe_json_load(data: bytes | None) -> tuple[dict | None, str | None]:
    if data is None:
        return None, None
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return None, f"JSON parse error: {exc}"
    if not isinstance(obj, dict):
        return None, "root is not an object"
    return obj, None


def _merge(dest: dict, src: dict) -> None:
    """Shallow merge with nested-dict awareness for known sub-objects."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dest.get(k), dict):
            dest_sub = dest[k]
            for kk, vv in v.items():
                dest_sub[kk] = vv
        else:
            dest[k] = v


# ---------------------------------------------------------------------------
# Derivations (values not directly present under any alias)
# ---------------------------------------------------------------------------


def _derive_reconnect_summary(raw: dict) -> list[dict] | None:
    """Build a normalized list from ``counters.reconnects`` dict.

    Real schema: ``counters.reconnects`` = {venue: int}. There is no
    explicit ``recovered`` flag in ``runtime_status.json`` \u2014 we
    therefore leave ``recovered = None`` (unknown). The verdict engine
    treats ``recovered is not False`` as non-fatal per \u00a712.6, and emits
    observational warnings for non-zero counts.
    """
    rec = _get_nested(raw, "counters.reconnects")
    if not isinstance(rec, dict):
        return None
    result: list[dict] = []
    for venue, count in rec.items():
        try:
            c = int(count)
        except (TypeError, ValueError):
            continue
        if c > 0:
            result.append({"venue": venue, "count": c, "recovered": None})
    return result


def _derive_reconnect_count(raw: dict) -> int | None:
    rec = _get_nested(raw, "counters.reconnects")
    if not isinstance(rec, dict):
        return None
    total = 0
    for v in rec.values():
        try:
            total += int(v)
        except (TypeError, ValueError):
            return None
    return total


def _sum_dict(raw: dict, path: str) -> int | None:
    d = _get_nested(raw, path)
    if not isinstance(d, dict):
        return None
    total = 0
    for v in d.values():
        try:
            total += int(v)
        except (TypeError, ValueError):
            return None
    return total


def _derive_websocket_errors(raw: dict) -> int | None:
    return _sum_dict(raw, "counters.errors")


def _derive_unknown_sides(raw: dict) -> int | None:
    return _sum_dict(raw, "counters.unknown_side")


def _derive_final_buffer_status(raw: dict) -> str | None:
    buffers = _get_nested(raw, "buffers")
    if not isinstance(buffers, dict):
        return None
    all_zero = True
    for v in buffers.values():
        try:
            if int(v) != 0:
                all_zero = False
                break
        except (TypeError, ValueError):
            return None
    return "CLEAN" if all_zero else "NON_EMPTY"


def _derive_duration_hours_from_run_minutes(raw: dict) -> float | None:
    run_min = raw.get("run_minutes")
    if run_min is None:
        return None
    try:
        return float(run_min) / 60.0
    except (TypeError, ValueError):
        return None


# Canonical -> (derivation function, source description). Applied only if
# alias resolution didn't already find a value.
DERIVATIONS: dict[str, tuple[Callable, str]] = {
    "reconnect_summary": (_derive_reconnect_summary, "derived from counters.reconnects"),
    "reconnect_count": (_derive_reconnect_count, "sum(counters.reconnects.values())"),
    "websocket_errors": (_derive_websocket_errors, "sum(counters.errors.values())"),
    "unknown_sides": (_derive_unknown_sides, "sum(counters.unknown_side.values())"),
    "final_buffer_status": (_derive_final_buffer_status, "buffers all-zero => CLEAN"),
    "duration_hours": (_derive_duration_hours_from_run_minutes, "manifest.run_minutes / 60"),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_bundle(
    manifest: dict | None,
    runtime: dict | None,
    summary: dict | None,
) -> ManifestResult:
    """Merge the three artifacts and resolve canonical fields."""
    result = ManifestResult()
    if manifest is None and runtime is None and summary is None:
        result.parse_error = "no supported JSON metadata files found"
        result.missing_fields = list(CRITICAL_FIELDS)
        return result

    merged: dict[str, Any] = {}
    # Merge order: manifest first, then runtime overrides, then summary
    # (summary is the wrapper's authoritative post-run verdict).
    for src in (manifest, runtime, summary):
        if isinstance(src, dict):
            _merge(merged, src)

    result.found = True
    result.raw = merged

    # 1) alias-based resolution
    for canonical, aliases in FIELD_ALIASES.items():
        ok, val = _resolve_field(merged, aliases)
        if ok:
            result.resolved[canonical] = val

    # 2) derived values (only if alias didn't already resolve)
    for canonical, (fn, note) in DERIVATIONS.items():
        if canonical in result.resolved:
            continue
        derived = fn(merged)
        if derived is not None:
            result.resolved[canonical] = derived
            result.derivations[canonical] = note

    # 3) critical fields
    result.missing_fields = [f for f in CRITICAL_FIELDS if f not in result.resolved]
    return result


def parse_manifest_from_source(source: ZipSource, inspection: ZipInspection) -> ManifestResult:
    """Locate and parse the manifest / runtime / summary bundle."""
    manifest_name, manifest_bytes = _find_and_read(source, inspection, MANIFEST_ENTRY_CANDIDATES)
    runtime_name, runtime_bytes = _find_and_read(source, inspection, RUNTIME_ENTRY_CANDIDATES)
    summary_name, summary_bytes = _find_and_read(source, inspection, SUMMARY_ENTRY_CANDIDATES)

    manifest_obj, manifest_err = _safe_json_load(manifest_bytes)
    runtime_obj, runtime_err = _safe_json_load(runtime_bytes)
    summary_obj, summary_err = _safe_json_load(summary_bytes)

    if manifest_obj is None and runtime_obj is None and summary_obj is None:
        errs = [e for e in (manifest_err, runtime_err, summary_err) if e]
        detail = "; ".join(errs) if errs else "no metadata JSON files found"
        return ManifestResult(
            found=False,
            parse_error=detail,
            missing_fields=list(CRITICAL_FIELDS),
        )

    result = ingest_bundle(manifest_obj, runtime_obj, summary_obj)
    result.entries_found = [
        n for n in (manifest_name, runtime_name, summary_name) if n
    ]
    if not result.entries_found:
        result.parse_error = "no metadata JSON files found"
    parse_errs = [e for e in (manifest_err, runtime_err, summary_err) if e]
    if parse_errs and not result.parse_error:
        result.parse_error = "; ".join(parse_errs)
    return result


# --- back-compat helpers used by older callers/tests ---------------------


def ingest_manifest_bytes(data: bytes | None, entry_name: str | None) -> ManifestResult:
    """Single-file legacy path (still used by unit tests)."""
    obj, err = _safe_json_load(data)
    if obj is None:
        return ManifestResult(
            found=False,
            parse_error=err or "manifest file not found",
            missing_fields=list(CRITICAL_FIELDS),
        )
    result = ingest_bundle(obj, None, None)
    if entry_name:
        result.entries_found = [entry_name]
    return result


parse_manifest_from_zip = parse_manifest_from_source


def guess_session_id_from_filename(filename: str) -> str | None:
    m = _SESSION_ID_PATTERN.search(filename)
    return m.group(1) if m else None
