"""Synthetic ZIP fixtures for pytest.

Builds tiny in-memory ZIP files that mimic the MultiVenue 3H session shape
with minimal parquet-magic-only files. No real Parquet payloads are
generated — we only assert magic bytes and structural counts.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass

from constants import FROZEN_COLLECTOR_SHA256

PAR1 = b"PAR1"
MINI_PARQUET = PAR1 + b"x" * 32 + PAR1  # 40 bytes; head + trailer magic


@dataclass
class BuildOptions:
    session_id: str = "20260910T123759Z_abc"
    include_manifest: bool = True
    manifest_override: dict | None = None
    collector_sha256: str | None = FROZEN_COLLECTOR_SHA256
    exit_code: int = 0
    watchdog: bool = False
    writer_errors: int = 0
    reconnect_summary: list | None = None
    unknown_sides: int = 0
    sync_grid_files: int = 2
    books_files: int = 2
    trades_files: int = 2
    corrupt_parquet: bool = False
    missing_dir: str | None = None
    duplicate_part: bool = False
    zip_slip: bool = False
    corrupt_zip_bytes: bool = False


def build_zip(opts: BuildOptions) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Manifest
        if opts.include_manifest:
            manifest = {
                "session_id": opts.session_id,
                "exit_code": opts.exit_code,
                "watchdog": opts.watchdog,
                "writer_errors": opts.writer_errors,
                "reconnect_summary": opts.reconnect_summary or [],
                "unknown_sides": opts.unknown_sides,
                "start_time": "2026-09-10T12:37:59Z",
                "end_time": "2026-09-10T15:37:59Z",
                "duration_hours": 3.0,
            }
            if opts.collector_sha256 is not None:
                manifest["collector_sha256"] = opts.collector_sha256
            if opts.manifest_override:
                manifest.update(opts.manifest_override)
            zf.writestr("manifest.json", json.dumps(manifest))

        # Dataset directories
        def add(dir_name: str, count: int, dup: bool = False, corrupt: bool = False):
            if opts.missing_dir == dir_name:
                return
            for i in range(count):
                body = MINI_PARQUET if not corrupt else b"NOT_PARQUET"
                zf.writestr(f"{dir_name}/part-{i:03d}.parquet", body)
            if dup and count > 0:
                # writestr does not deduplicate; adding same name twice yields
                # two entries with identical name (real ZIPs allow this).
                zf.writestr(f"{dir_name}/part-000.parquet", MINI_PARQUET)

        add("sync_grid_100ms", opts.sync_grid_files, opts.duplicate_part, opts.corrupt_parquet)
        add("normalized_books", opts.books_files, False, opts.corrupt_parquet)
        add("normalized_trades", opts.trades_files, False, opts.corrupt_parquet)

        if opts.zip_slip:
            # Add an entry with a traversal path
            zi = zipfile.ZipInfo(filename="../../../etc/passwd")
            zf.writestr(zi, b"root:x:0:0::/root:/bin/sh\n")

    raw = buf.getvalue()
    if opts.corrupt_zip_bytes:
        # Flip a byte in the middle of the archive to break CRC / structure.
        mid = len(raw) // 2
        raw = raw[:mid] + bytes([raw[mid] ^ 0xFF]) + raw[mid + 1:]
    return raw
