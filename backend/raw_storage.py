"""Content-addressed retained raw ZIP storage.

Design goal
-----------

A retained raw session ZIP is uniquely identified by its
``source_file_sha256``. Every QA run against the same physical binary
MUST share ONE canonical file on disk.

Concretely:

- File path is ``<raw_dir>/<sha256>.zip``
- Multiple ``RawFile`` rows may point at the same ``stored_path`` if
  their parent QA runs share ``source_file_sha256``. That is expected:
  rows describe *retention state per QA run*, not per-blob identity.
- Reprocessing a session never duplicates the raw binary; it reuses
  the canonical file if it exists.
- EXACT_DUPLICATE uploads also reuse the canonical file.
- A different upload with the same session_id but a different SHA256
  gets its own canonical file - dedup is by content, not by session.

Local retention on a mounted persistent volume is a hard project
directive for SuperBot V1 (Docker portability, no object storage per
user spec).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def is_valid_sha256(s: str | None) -> bool:
    return bool(s) and bool(_SHA256_RE.match(s))


def canonical_path(raw_dir: Path, sha256: str) -> Path:
    """Return the deterministic on-disk path for a content-addressed ZIP."""
    if not is_valid_sha256(sha256):
        raise ValueError(f"invalid sha256 {sha256!r}")
    return Path(raw_dir) / f"{sha256}.zip"


def _write_bytes_local(path: Path, data: bytes) -> None:
    """Low-level positional write to the mounted persistent volume.

    We use ``os.open`` + ``os.write`` instead of the higher-level file
    object so that partial writes are handled explicitly on any FS.
    """
    fd = os.open(
        str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644
    )
    try:
        mv = memoryview(data)
        written = 0
        while written < len(mv):
            written += os.write(fd, mv[written:])
    finally:
        os.close(fd)


def persist_bytes(raw_dir: Path, data: bytes, sha256: str) -> Path:
    """Retain ``data`` at the canonical location for ``sha256``.

    Returns the canonical path. If the canonical file already exists
    (i.e. we have already retained this exact binary) the call is a
    no-op and the existing path is returned - no duplicate copy is
    written.
    """
    dest = canonical_path(raw_dir, sha256)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    _write_bytes_local(dest, data)
    return dest


def persist_from_path(raw_dir: Path, src: Path, sha256: str) -> Path:
    """Move ``src`` into the canonical location for ``sha256``.

    If the canonical file already exists, ``src`` is unlinked (we
    already have the retained copy) and the existing canonical path
    is returned. Otherwise ``os.replace`` performs an atomic rename
    on the same filesystem (both paths live on the same Docker /
    Railway persistent volume in this deployment).
    """
    dest = canonical_path(raw_dir, sha256)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    if dest.exists():
        try:
            if src.exists() and os.path.realpath(src) != os.path.realpath(dest):
                src.unlink()
        except OSError:
            pass
        return dest
    try:
        os.replace(src, dest)
    except OSError as exc:
        # EXDEV: src and dest live on different filesystems (e.g. /tmp
        # vs a mounted data volume). Fall back to a bounded-memory
        # streamed copy so the retention row is still written and the
        # source is unlinked afterwards. This keeps callers agnostic
        # to filesystem topology.
        import errno

        if exc.errno != errno.EXDEV:
            raise
        _stream_copy(src, dest)
        try:
            src.unlink()
        except OSError:
            pass
    return dest


def _stream_copy(src: Path, dest: Path) -> None:
    """Bounded-memory copy: 1 MiB chunks, no whole-file load."""
    with open(src, "rb", buffering=0) as fsrc, open(dest, "wb", buffering=0) as fdst:
        while True:
            buf = fsrc.read(1024 * 1024)
            if not buf:
                break
            fdst.write(buf)


def prune_orphan_blobs(raw_dir: Path, referenced_paths: Iterable[str]) -> list[str]:
    """Delete ``*.zip`` files under ``raw_dir`` that are not in
    ``referenced_paths``. Returns the list of paths removed.

    Callers pass every ``raw_files.stored_path`` currently in the
    database so this function operates on the source-of-truth set.
    """
    ref_set = set()
    for p in referenced_paths:
        if not p:
            continue
        try:
            ref_set.add(os.path.realpath(p))
        except OSError:
            continue

    raw_dir = Path(raw_dir)
    removed: list[str] = []
    if not raw_dir.exists():
        return removed
    for entry in raw_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".zip":
            continue
        real = os.path.realpath(str(entry))
        if real in ref_set:
            continue
        try:
            entry.unlink()
            removed.append(str(entry))
        except OSError:
            pass
    return removed
