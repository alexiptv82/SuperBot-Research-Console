"""Deterministic QA engine for MultiVenue 3H session ZIPs.

Implements the checks required by §12.6 of the handoff and produces a
verdict from the frozen vocabulary (PASS / PASS_WITH_WARNING / FAIL /
UNRESOLVED). Per user directive, unknown/missing critical fields yield
UNRESOLVED and never a fabricated PASS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from constants import (
    DATASET_DIRS,
    FROZEN_COLLECTOR_SHA256,
    PARQUET_MAGIC,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_PASS_WITH_WARNING,
    VERDICT_UNRESOLVED,
)
from manifest_parser import (
    ManifestResult,
    guess_session_id_from_filename,
    parse_manifest_from_source,
)
from zip_security import (
    ZipInspection,
    ZipSource,
    inspect_zip,
    list_dir_entries,
    read_parquet_magic,
    read_small_entry,
    sha256_of_source,
)


@dataclass
class QAReport:
    session_id: str | None
    file_sha256: str
    original_filename: str
    verdict: str = VERDICT_UNRESOLVED
    failure_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    manifest_raw: dict[str, Any] | None = None
    missing_fields: list[str] = field(default_factory=list)


def _record(
    report: QAReport,
    area: str,
    status: str,
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    report.checks[area] = {"status": status, "detail": detail, **(extra or {})}


def _combine(current: str, new: str) -> str:
    """Combine verdicts using the frozen hierarchy.

    FAIL > UNRESOLVED > PASS_WITH_WARNING > PASS.
    Note: FAIL beats UNRESOLVED because an outright integrity failure is a
    definitive result. Anything else UNKNOWN keeps us at UNRESOLVED.
    """
    order = {VERDICT_PASS: 0, VERDICT_PASS_WITH_WARNING: 1, VERDICT_UNRESOLVED: 2, VERDICT_FAIL: 3}
    return new if order[new] > order[current] else current


def run_qa(source: ZipSource, original_filename: str) -> QAReport:
    """Run all deterministic checks on ``source`` and return a QAReport.

    ``source`` may be an in-memory bytes blob or a filesystem path on a
    mounted persistent volume.
    """
    file_sha = sha256_of_source(source)
    report = QAReport(
        session_id=guess_session_id_from_filename(original_filename),
        file_sha256=file_sha,
        original_filename=original_filename,
    )
    verdict: str = VERDICT_PASS

    # 1. ZIP integrity ------------------------------------------------------
    inspection: ZipInspection = inspect_zip(source)
    if not inspection.crc_ok:
        _record(report, "zip", VERDICT_FAIL, inspection.error or "CRC error")
        report.failure_reasons.append(inspection.error or "ZIP CRC error")
        report.verdict = VERDICT_FAIL
        report.checks.setdefault("collector", {"status": VERDICT_UNRESOLVED, "detail": "skipped: ZIP unreadable"})
        return report
    if inspection.unsafe_paths:
        _record(
            report,
            "zip",
            VERDICT_FAIL,
            f"Unsafe paths detected: {inspection.unsafe_paths[:5]}",
            {"unsafe_paths": inspection.unsafe_paths},
        )
        report.failure_reasons.append("Unsafe ZIP paths (zip-slip attempt)")
        report.verdict = VERDICT_FAIL
        return report
    if inspection.error:
        _record(report, "zip", VERDICT_FAIL, inspection.error)
        report.failure_reasons.append(inspection.error)
        report.verdict = VERDICT_FAIL
        return report
    _record(
        report,
        "zip",
        VERDICT_PASS,
        f"{len(inspection.entries)} entries; {inspection.total_uncompressed} bytes",
        {"entries": len(inspection.entries), "uncompressed_bytes": inspection.total_uncompressed},
    )

    # 2. Manifest / runtime -------------------------------------------------
    manifest: ManifestResult = parse_manifest_from_source(source, inspection)
    report.manifest_raw = manifest.raw
    report.missing_fields = manifest.missing_fields

    if not manifest.found:
        _record(
            report,
            "manifest",
            VERDICT_UNRESOLVED,
            manifest.parse_error or "manifest not found",
            {"missing_fields": manifest.missing_fields},
        )
        verdict = _combine(verdict, VERDICT_UNRESOLVED)
    else:
        if manifest.missing_fields:
            _record(
                report,
                "manifest",
                VERDICT_UNRESOLVED,
                "missing critical fields",
                {"missing_fields": manifest.missing_fields, "resolved": list(manifest.resolved)},
            )
            verdict = _combine(verdict, VERDICT_UNRESOLVED)
        else:
            _record(
                report,
                "manifest",
                VERDICT_PASS,
                "all critical fields resolved",
                {"resolved": list(manifest.resolved)},
            )
        report.fields.update(manifest.resolved)

    # If manifest gives us a session_id, prefer it over the filename guess.
    if manifest.resolved.get("session_id"):
        report.session_id = str(manifest.resolved["session_id"])

    # 3. Collector hash -----------------------------------------------------
    collector_hash = manifest.resolved.get("collector_sha256")
    if collector_hash is None:
        _record(
            report,
            "collector",
            VERDICT_UNRESOLVED,
            "collector_sha256 not present in manifest under any known alias",
        )
        verdict = _combine(verdict, VERDICT_UNRESOLVED)
    else:
        collector_hash_str = str(collector_hash).lower().strip()
        if collector_hash_str == FROZEN_COLLECTOR_SHA256:
            _record(report, "collector", VERDICT_PASS, "matches frozen SHA256", {"collector_sha256": collector_hash_str})
        else:
            _record(
                report,
                "collector",
                VERDICT_FAIL,
                "COLLECTOR MISMATCH against frozen SHA256",
                {"expected": FROZEN_COLLECTOR_SHA256, "actual": collector_hash_str},
            )
            report.failure_reasons.append("Collector SHA256 mismatch")
            verdict = _combine(verdict, VERDICT_FAIL)

    # 4. Runtime signals from manifest (exit_code, watchdog, etc.) ---------
    runtime_status = VERDICT_PASS
    runtime_detail: list[str] = []
    exit_code = manifest.resolved.get("exit_code")
    if exit_code is None:
        runtime_status = _combine(runtime_status, VERDICT_UNRESOLVED)
        runtime_detail.append("exit_code missing")
    else:
        try:
            if int(exit_code) != 0:
                runtime_status = _combine(runtime_status, VERDICT_FAIL)
                report.failure_reasons.append(f"exit_code={exit_code}")
                runtime_detail.append(f"exit_code={exit_code}")
        except (TypeError, ValueError):
            runtime_status = _combine(runtime_status, VERDICT_UNRESOLVED)
            runtime_detail.append(f"exit_code non-integer: {exit_code!r}")

    watchdog = manifest.resolved.get("watchdog")
    if watchdog is None:
        runtime_status = _combine(runtime_status, VERDICT_UNRESOLVED)
        runtime_detail.append("watchdog missing")
    else:
        if bool(watchdog):
            runtime_status = _combine(runtime_status, VERDICT_FAIL)
            report.failure_reasons.append("watchdog triggered")
            runtime_detail.append("watchdog=true")

    writer_errors = manifest.resolved.get("writer_errors")
    if writer_errors is not None and int(writer_errors) > 0:
        runtime_status = _combine(runtime_status, VERDICT_FAIL)
        report.failure_reasons.append(f"writer_errors={writer_errors}")
        runtime_detail.append(f"writer_errors={writer_errors}")

    # Reconnects: recovered != FAIL; unresolved reconnect => FAIL
    recon_summary = manifest.resolved.get("reconnect_summary")
    if isinstance(recon_summary, list):
        for r in recon_summary:
            if isinstance(r, dict) and r.get("recovered") is False:
                runtime_status = _combine(runtime_status, VERDICT_FAIL)
                report.failure_reasons.append(
                    f"unrecovered reconnect on {r.get('venue', '?')}"
                )
                runtime_detail.append("unrecovered reconnect")
            elif isinstance(r, dict) and r.get("recovered") is True:
                report.warnings.append(
                    f"recovered reconnect on {r.get('venue', '?')} × {r.get('count', 1)}"
                )

    # Sampler / lag warnings (never invented thresholds; only report facts)
    if manifest.resolved.get("missed_ticks") is not None:
        try:
            mt = int(manifest.resolved["missed_ticks"])
            th = int(manifest.resolved.get("theoretical_ticks") or 0) or None
            if th:
                pct = round(mt / th * 100.0, 6)
                report.fields["missed_tick_pct"] = pct
        except (TypeError, ValueError):
            pass

    _record(report, "runtime", runtime_status, "; ".join(runtime_detail) or "ok")
    verdict = _combine(verdict, runtime_status)

    # 5. Dataset structure --------------------------------------------------
    dataset_status = VERDICT_PASS
    dataset_detail: list[str] = []
    counts: dict[str, int] = {}
    for d in DATASET_DIRS:
        files = list_dir_entries(inspection, d)
        counts[d] = len(files)
        if not files:
            dataset_status = _combine(dataset_status, VERDICT_FAIL)
            report.failure_reasons.append(f"dataset dir empty or missing: {d}")
            dataset_detail.append(f"{d}=0")
        else:
            dataset_detail.append(f"{d}={len(files)}")

    # Duplicate detection within the ZIP: identical entry names must not appear twice
    all_names = [e.name for e in inspection.entries if not e.is_dir]
    dupe_names = [n for n in set(all_names) if all_names.count(n) > 1]
    if dupe_names:
        dataset_status = _combine(dataset_status, VERDICT_FAIL)
        report.failure_reasons.append(
            f"duplicate parts inside ZIP: {dupe_names[:5]}"
        )

    _record(
        report,
        "dataset",
        dataset_status,
        ", ".join(dataset_detail),
        {"counts": counts, "duplicate_parts": dupe_names},
    )
    report.fields["sync_grid_file_count"] = counts.get("sync_grid_100ms")
    report.fields["books_file_count"] = counts.get("normalized_books")
    report.fields["trades_file_count"] = counts.get("normalized_trades")
    report.fields["parquet_total"] = sum(counts.values())
    verdict = _combine(verdict, dataset_status)

    # 6. Parquet magic checks (lightweight, first 3 files per dir) ---------
    parquet_status = VERDICT_PASS
    parquet_detail: list[str] = []
    for d in DATASET_DIRS:
        files = [e for e in list_dir_entries(inspection, d) if e.name.endswith(".parquet")]
        for e in files[:3]:  # cap to keep it cheap
            head, tail = read_parquet_magic(source, e.name)
            if not (head and tail):
                parquet_status = _combine(parquet_status, VERDICT_FAIL)
                report.failure_reasons.append(f"parquet magic missing: {e.name}")
                parquet_detail.append(f"BAD:{e.name}")
            else:
                parquet_detail.append(f"OK:{e.name}")
    _record(report, "parquet", parquet_status, "; ".join(parquet_detail) or "no parquet checked")
    report.fields["parquet_magic_status"] = parquet_status
    verdict = _combine(verdict, parquet_status)

    # 7. Reconnect summary snapshot ----------------------------------------
    if recon_summary is not None:
        report.fields["reconnect_summary"] = recon_summary
        report.fields["reconnect_count"] = (
            manifest.resolved.get("reconnect_count")
            or (len(recon_summary) if isinstance(recon_summary, list) else None)
        )

    # 8. Warnings elevate PASS -> PASS_WITH_WARNING -----------------------
    if verdict == VERDICT_PASS and report.warnings:
        verdict = VERDICT_PASS_WITH_WARNING

    # Also elevate to PASS_WITH_WARNING if unknown_sides present but manifest ok
    unknown_sides = manifest.resolved.get("unknown_sides")
    if unknown_sides is not None:
        try:
            if int(unknown_sides) > 0:
                report.warnings.append(f"unknown_side_count={unknown_sides}")
                if verdict == VERDICT_PASS:
                    verdict = VERDICT_PASS_WITH_WARNING
                report.fields["unknown_side_count"] = int(unknown_sides)
        except (TypeError, ValueError):
            report.warnings.append(f"unknown_sides non-numeric: {unknown_sides!r}")

    report.verdict = verdict
    return report
