"""Tolerant, adaptable manifest / runtime parser.

Per user directive: do NOT invent or guess critical values. If a critical
field is not present under any of its known aliases, mark the result as
UNRESOLVED and record the missing field. The parser exposes an
``adapt_alias`` hook so a future real ZIP can extend the alias registry
without changing verdict logic.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from zip_security import ZipInspection, ZipSource, find_entry, read_small_entry

# Field alias registry. Keys are the canonical field names used by V1;
# values are ordered tuples of plausible key names in the raw manifest.
# These aliases were derived from the handoff's field vocabulary (§12.3 /
# §12.6). No values are ever fabricated from prose — if none of the aliases
# resolve, the field is reported as missing.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "exit_code": ("exit_code", "exitCode", "process.exit_code", "runtime.exit_code"),
    "watchdog": ("watchdog", "watchdog_triggered", "runtime.watchdog"),
    "duration_hours": ("duration_hours", "durationHours", "session.duration_hours"),
    "duration_seconds": ("duration_seconds", "durationSeconds", "runtime.duration_seconds"),
    "start_time": ("start_time", "startTime", "session.start_time", "start_utc"),
    "end_time": ("end_time", "endTime", "session.end_time", "end_utc"),
    "errors": ("errors", "error_count", "runtime.errors"),
    "reconnects": ("reconnects", "reconnect_events", "runtime.reconnects"),
    "reconnect_count": ("reconnect_count", "reconnects.count", "runtime.reconnect_count"),
    "reconnect_summary": ("reconnect_summary", "reconnects", "runtime.reconnect_summary"),
    "writer_errors": ("writer_errors", "writerErrors", "io.writer_errors"),
    "missed_ticks": ("missed_ticks", "missedTicks", "sampler.missed_ticks"),
    "theoretical_ticks": ("theoretical_ticks", "theoreticalTicks", "sampler.theoretical_ticks"),
    "lag_gt_50ms": ("lag_gt_50ms", "lagGt50ms", "sampler.lag_gt_50ms"),
    "max_lag_ms": ("max_lag_ms", "maxLagMs", "sampler.max_lag_ms"),
    "buffers": ("buffers", "final_buffers", "runtime.final_buffers"),
    "final_buffer_status": ("final_buffer_status", "buffers.status", "runtime.final_buffer_status"),
    "unknown_sides": ("unknown_sides", "unknown_side_count", "trades.unknown_sides"),
    "websocket_errors": ("websocket_errors", "ws_errors", "runtime.websocket_errors"),
    "session_id": ("session_id", "sessionId", "session.id"),
    "collector_sha256": (
        "collector_sha256",
        "collectorSha256",
        "collector.sha256",
        "collector_hash",
        "collector.hash",
    ),
}

# The critical set: without these under any alias we CANNOT PASS. Missing
# critical fields => UNRESOLVED (parser policy, per user directive).
CRITICAL_FIELDS: tuple[str, ...] = (
    "exit_code",
    "watchdog",
    "collector_sha256",
)

# Candidate names for the manifest file inside a ZIP.
MANIFEST_ENTRY_CANDIDATES: tuple[str, ...] = (
    "manifest.json",
    "session_manifest.json",
    "run_manifest.json",
    "metadata.json",
)

# Regex for session ID pattern from handoff §9: 20260910T123759Z_5ab
_SESSION_ID_PATTERN = re.compile(r"([0-9]{8}T[0-9]{6}Z_[A-Za-z0-9]+)")


@dataclass
class ManifestResult:
    found: bool = False
    entry_name: str | None = None
    raw: dict[str, Any] | None = None
    resolved: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    parse_error: str | None = None


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


def parse_manifest(inspection: ZipInspection) -> ManifestResult:
    """Find and parse a manifest inside the ZIP. Tolerant to key names."""
    result = ManifestResult()
    entry = find_entry(inspection, MANIFEST_ENTRY_CANDIDATES)
    if entry is None:
        result.parse_error = "manifest file not found"
        result.missing_fields = list(CRITICAL_FIELDS)
        return result
    # We need the actual ZIP path; the caller passes an inspection but the
    # raw read requires zip_path. Callers use ``parse_manifest_from_zip``.
    # We accept a raw dict path via ``ingest_manifest_bytes`` too.
    return result


def ingest_manifest_bytes(data: bytes | None, entry_name: str | None) -> ManifestResult:
    result = ManifestResult(found=False, entry_name=entry_name)
    if data is None:
        result.parse_error = "manifest file not found"
        result.missing_fields = list(CRITICAL_FIELDS)
        return result
    try:
        raw = json.loads(data.decode("utf-8", errors="replace"))
    except Exception as exc:
        result.parse_error = f"manifest JSON parse error: {exc}"
        result.missing_fields = list(CRITICAL_FIELDS)
        return result
    if not isinstance(raw, dict):
        result.parse_error = "manifest root is not an object"
        result.missing_fields = list(CRITICAL_FIELDS)
        return result
    result.found = True
    result.raw = raw
    for canonical, aliases in FIELD_ALIASES.items():
        ok, val = _resolve_field(raw, aliases)
        if ok:
            result.resolved[canonical] = val
    result.missing_fields = [f for f in CRITICAL_FIELDS if f not in result.resolved]
    return result


def parse_manifest_from_source(source: ZipSource, inspection: ZipInspection) -> ManifestResult:
    entry = find_entry(inspection, MANIFEST_ENTRY_CANDIDATES)
    if entry is None:
        return ManifestResult(
            found=False,
            parse_error="manifest file not found",
            missing_fields=list(CRITICAL_FIELDS),
        )
    data = read_small_entry(source, entry.name, max_bytes=4 * 1024 * 1024)
    return ingest_manifest_bytes(data, entry.name)


# Back-compat alias
parse_manifest_from_zip = parse_manifest_from_source


def guess_session_id_from_filename(filename: str) -> str | None:
    """Extract a session id from the ZIP filename using the frozen pattern."""
    m = _SESSION_ID_PATTERN.search(filename)
    return m.group(1) if m else None
