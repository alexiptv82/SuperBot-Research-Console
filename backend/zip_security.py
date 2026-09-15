"""Safe ZIP handling.

Enforces zip-slip protection, entry count / size limits, and returns a
structured listing without executing anything or loading Parquet content
into memory. Uses only stdlib to keep runtime deps low.

Every helper accepts a ZipSource: either an in-memory ``bytes`` blob or a
path on a mounted persistent volume. In-memory processing is used for the
upload path so nothing is written to pod-local temp storage.
"""
from __future__ import annotations

import hashlib
import io
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Union

from constants import (
    MAX_ENTRIES_PER_ZIP,
    MAX_ENTRY_PATH_LEN,
    MAX_EXTRACTED_BYTES,
)

ZipSource = Union[bytes, str, os.PathLike]


@dataclass
class ZipEntry:
    name: str
    size: int
    crc: int
    is_dir: bool


@dataclass
class ZipInspection:
    ok: bool
    crc_ok: bool
    entries: list[ZipEntry] = field(default_factory=list)
    unsafe_paths: list[str] = field(default_factory=list)
    error: str | None = None
    total_uncompressed: int = 0


def _open_zip(source: ZipSource) -> zipfile.ZipFile:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return zipfile.ZipFile(io.BytesIO(bytes(source)), "r")
    return zipfile.ZipFile(source, "r")


def _is_within_directory(directory: Path, target: Path) -> bool:
    try:
        directory = directory.resolve(strict=False)
        target = target.resolve(strict=False)
    except OSError:
        return False
    try:
        target.relative_to(directory)
        return True
    except ValueError:
        return False


def sha256_of_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def sha256_of_source(source: ZipSource, chunk: int = 1024 * 1024) -> str:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return sha256_of_bytes(bytes(source))
    h = hashlib.sha256()
    with open(source, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def inspect_zip(source: ZipSource) -> ZipInspection:
    """Open a ZIP and validate safety properties without extracting."""
    result = ZipInspection(ok=False, crc_ok=False)
    try:
        with _open_zip(source) as zf:
            bad = zf.testzip()
            result.crc_ok = bad is None
            if bad is not None:
                result.error = f"CRC error in {bad}"
            names = zf.namelist()
            if len(names) > MAX_ENTRIES_PER_ZIP:
                result.error = f"Too many entries ({len(names)} > {MAX_ENTRIES_PER_ZIP})"
                return result
            total = 0
            root = Path("/__superbot_zip_root__")
            for info in zf.infolist():
                name = info.filename
                if len(name) > MAX_ENTRY_PATH_LEN:
                    result.unsafe_paths.append(name)
                    continue
                if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
                    result.unsafe_paths.append(name)
                    continue
                target = root / name
                if not _is_within_directory(root, target):
                    result.unsafe_paths.append(name)
                    continue
                total += info.file_size
                if total > MAX_EXTRACTED_BYTES:
                    result.error = "Extraction size limit exceeded"
                    return result
                result.entries.append(
                    ZipEntry(
                        name=name,
                        size=info.file_size,
                        crc=info.CRC,
                        is_dir=name.endswith("/"),
                    )
                )
            result.total_uncompressed = total
            result.ok = result.crc_ok and not result.unsafe_paths
            return result
    except zipfile.BadZipFile as exc:
        result.error = f"Bad ZIP: {exc}"
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"ZIP inspection failed: {exc}"
        return result


def read_small_entry(
    source: ZipSource, entry_name: str, max_bytes: int = 8 * 1024 * 1024
) -> bytes | None:
    """Read one small file from the ZIP into memory."""
    try:
        with _open_zip(source) as zf:
            if entry_name not in zf.namelist():
                return None
            info = zf.getinfo(entry_name)
            if info.file_size > max_bytes:
                return None
            if ".." in Path(info.filename).parts or info.filename.startswith("/"):
                return None
            with zf.open(info, "r") as fh:
                return fh.read(max_bytes + 1)[:max_bytes]
    except Exception:
        return None


def read_parquet_magic(source: ZipSource, entry_name: str) -> tuple[bool, bool]:
    """Check Parquet file has PAR1 magic at start and end (lightweight)."""
    try:
        with _open_zip(source) as zf:
            info = zf.getinfo(entry_name)
            with zf.open(info, "r") as fh:
                head = fh.read(4)
            # Read whole entry into memory only if small; otherwise stream to
            # discard, keeping only the last 4 bytes.
            with zf.open(info, "r") as fh:
                if info.file_size <= 16 * 1024 * 1024:
                    body = fh.read()
                    tail = body[-4:] if len(body) >= 4 else b""
                else:
                    remaining = info.file_size
                    tail = b""
                    while remaining > 0:
                        chunk = fh.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        tail = (tail + chunk)[-4:]
        return (head == b"PAR1", tail == b"PAR1")
    except Exception:
        return (False, False)


def list_dir_entries(inspection: ZipInspection, prefix: str) -> list[ZipEntry]:
    prefix = prefix.rstrip("/") + "/"
    return [
        e
        for e in inspection.entries
        if not e.is_dir and (e.name.startswith(prefix) or ("/" + prefix) in e.name)
    ]


def find_entry(inspection: ZipInspection, candidates: Iterable[str]) -> ZipEntry | None:
    names = {e.name: e for e in inspection.entries}
    for cand in candidates:
        if cand in names:
            return names[cand]
    for cand in candidates:
        for e in inspection.entries:
            if e.name.endswith("/" + cand) or e.name == cand:
                return e
    return None
