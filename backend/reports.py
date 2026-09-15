"""Report exporters: JSON, CSV, Markdown."""
from __future__ import annotations

import csv
import io
import json
from typing import Iterable

from models import QARun


def _qa_run_dict(run: QARun) -> dict:
    return {
        "session_id": run.session_id,
        "original_filename": run.original_filename,
        "uploaded_at": run.uploaded_at,
        "source_file_sha256": run.source_file_sha256,
        "collector_sha256": run.collector_sha256,
        "start_time": run.start_time,
        "end_time": run.end_time,
        "duration_hours": run.duration_hours,
        "operational_status": run.operational_status,
        "duplicate_status": run.duplicate_status,
        "zip_crc_status": run.zip_crc_status,
        "manifest_status": run.manifest_status,
        "runtime_status": run.runtime_status,
        "watchdog_status": run.watchdog_status,
        "exit_code": run.exit_code,
        "sync_grid_file_count": run.sync_grid_file_count,
        "books_file_count": run.books_file_count,
        "trades_file_count": run.trades_file_count,
        "parquet_total": run.parquet_total,
        "parquet_magic_status": run.parquet_magic_status,
        "sync_sequence_status": run.sync_sequence_status,
        "books_sequence_status": run.books_sequence_status,
        "trades_sequence_status": run.trades_sequence_status,
        "missed_ticks": run.missed_ticks,
        "theoretical_ticks": run.theoretical_ticks,
        "missed_tick_pct": run.missed_tick_pct,
        "lag_gt_50ms": run.lag_gt_50ms,
        "max_lag_ms": run.max_lag_ms,
        "writer_errors": run.writer_errors,
        "websocket_errors": run.websocket_errors,
        "reconnect_count": run.reconnect_count,
        "reconnect_summary": run.reconnect_summary,
        "unknown_side_count": run.unknown_side_count,
        "final_buffer_status": run.final_buffer_status,
        "validated_hours": run.validated_hours,
        "failure_reasons": run.failure_reasons,
        "warnings": run.warnings,
        "missing_fields": run.missing_fields,
        "qa_timestamp": run.qa_timestamp,
        "checks": run.checks,
    }


def to_json(runs: Iterable[QARun]) -> str:
    return json.dumps([_qa_run_dict(r) for r in runs], indent=2, default=str)


def to_csv(runs: Iterable[QARun]) -> str:
    rows = [_qa_run_dict(r) for r in runs]
    if not rows:
        return ""
    # Flatten complex fields to JSON strings for CSV portability.
    flat_rows = []
    for r in rows:
        fr = {}
        for k, v in r.items():
            if isinstance(v, (list, dict)):
                fr[k] = json.dumps(v, default=str)
            else:
                fr[k] = v
        flat_rows.append(fr)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(flat_rows[0].keys()))
    writer.writeheader()
    for row in flat_rows:
        writer.writerow(row)
    return buf.getvalue()


def to_markdown(runs: Iterable[QARun]) -> str:
    runs = list(runs)
    lines = [
        "# SuperBot Research Console — QA Report",
        "",
        f"Sessions in report: {len(runs)}",
        "",
    ]
    for r in runs:
        d = _qa_run_dict(r)
        lines += [
            f"## Session `{d['session_id']}` — **{d['operational_status']}**",
            "",
            f"- Original filename: `{d['original_filename']}`",
            f"- Uploaded at: `{d['uploaded_at']}`",
            f"- File SHA256: `{d['source_file_sha256']}`",
            f"- Collector SHA256: `{d['collector_sha256']}`",
            f"- Duplicate status: `{d['duplicate_status']}`",
            f"- Validated hours: **{d['validated_hours']}**",
            f"- Exit code: `{d['exit_code']}` · Watchdog: `{d['watchdog_status']}`",
            f"- Dataset counts: sync_grid={d['sync_grid_file_count']}, books={d['books_file_count']}, trades={d['trades_file_count']}",
            "",
            "### Failure reasons",
            "",
        ]
        if d["failure_reasons"]:
            for reason in d["failure_reasons"]:
                lines.append(f"- {reason}")
        else:
            lines.append("_(none)_")
        lines.append("")
        lines.append("### Warnings")
        lines.append("")
        if d["warnings"]:
            for w in d["warnings"]:
                lines.append(f"- {w}")
        else:
            lines.append("_(none)_")
        lines.append("")
        lines.append("### Missing critical fields")
        lines.append("")
        if d["missing_fields"]:
            for m in d["missing_fields"]:
                lines.append(f"- {m}")
        else:
            lines.append("_(none)_")
        lines.append("")
    return "\n".join(lines)
