"""Chunked upload manager.

Design (single Railway service, single persistent volume, no S3/object
store, no Redis \u2014 per project handoff \u00a713):

- Browser splits the ZIP into fixed-size chunks (default 8 MiB).
- ``POST /api/uploads/init`` opens an :class:`UploadSession`, creates an
  empty ``.part`` file on the persistent volume and reserves the full
  final size via ``truncate``.
- ``POST /api/uploads/{id}/chunk/{index}`` streams a chunk to disk at
  offset ``index * chunk_size`` and records the index in a set.
- ``POST /api/uploads/{id}/complete`` verifies (a) every chunk index is
  present, (b) the resulting file size matches, (c) the incrementally
  streamed SHA256 matches the client-supplied hash, then hands the
  assembled file to the QA engine.
- ``DELETE /api/uploads/{id}`` aborts and cleans up.

Memory stays bounded by the chunk size on both request handler and
QA engine (which now operates on the file path, not a bytes blob).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from constants import MAX_ENTRIES_PER_ZIP  # noqa: F401 (kept for future use)

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
# Absolute ceiling per single .part file. Aligned with SUPERBOT_MAX_UPLOAD_MB.
_max_mb = int(os.environ.get("SUPERBOT_MAX_UPLOAD_MB", "1024"))
MAX_UPLOAD_BYTES = _max_mb * 1024 * 1024
# Reject chunks larger than this to keep request buffers small.
MAX_CHUNK_BYTES = 32 * 1024 * 1024  # 32 MiB (client uses 8 MiB by default)
# Sessions with no progress for this long are cleaned up.
STALE_TTL_SECONDS = 6 * 60 * 60  # 6 hours


class UploadError(Exception):
    """Raised for any client-visible error during chunked upload."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class UploadSession:
    id: str
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    part_path: Path
    retain_raw: bool = False
    checkpoint_hint: str | None = None
    received: set[int] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    completed: bool = False

    def to_dict(self) -> dict:
        return {
            "upload_id": self.id,
            "filename": self.filename,
            "total_size": self.total_size,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "received_chunks": sorted(self.received),
            "received_count": len(self.received),
            "retain_raw": self.retain_raw,
            "checkpoint_hint": self.checkpoint_hint,
            "created_at": self.created_at,
            "completed": self.completed,
        }


class UploadManager:
    def __init__(self, base_dir: str | os.PathLike):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, UploadSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(
        self,
        *,
        filename: str,
        total_size: int,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        retain_raw: bool = False,
        checkpoint_hint: str | None = None,
    ) -> UploadSession:
        if total_size <= 0:
            raise UploadError(400, "total_size must be > 0")
        if total_size > MAX_UPLOAD_BYTES:
            raise UploadError(
                413, f"File exceeds max upload size ({MAX_UPLOAD_BYTES} bytes)"
            )
        if chunk_size <= 0 or chunk_size > MAX_CHUNK_BYTES:
            raise UploadError(
                400, f"chunk_size must be between 1 and {MAX_CHUNK_BYTES}"
            )
        safe_name = _sanitize_filename(filename)
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        upload_id = uuid.uuid4().hex
        part_path = self.base_dir / f"{upload_id}.part"
        # Reserve the target size; sparse allocation on most filesystems.
        with open(part_path, "wb") as fh:
            fh.truncate(total_size)
        session = UploadSession(
            id=upload_id,
            filename=safe_name,
            total_size=total_size,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            part_path=part_path,
            retain_raw=retain_raw,
            checkpoint_hint=checkpoint_hint,
        )
        with self._lock:
            self._sessions[upload_id] = session
        return session

    def get(self, upload_id: str) -> UploadSession:
        with self._lock:
            s = self._sessions.get(upload_id)
        if s is None:
            raise UploadError(404, "Unknown upload_id")
        return s

    def abort(self, upload_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(upload_id, None)
        if s is None:
            return
        try:
            s.part_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Chunk write
    # ------------------------------------------------------------------

    def write_chunk(self, upload_id: str, index: int, data: bytes) -> UploadSession:
        s = self.get(upload_id)
        if s.completed:
            raise UploadError(409, "Upload already finalized")
        if index < 0 or index >= s.total_chunks:
            raise UploadError(
                400,
                f"chunk index {index} out of range [0, {s.total_chunks - 1}]",
            )
        # Compute expected chunk length: last chunk may be shorter.
        offset = index * s.chunk_size
        expected_len = s.chunk_size
        if index == s.total_chunks - 1:
            expected_len = s.total_size - offset
        if len(data) != expected_len:
            raise UploadError(
                400,
                f"chunk {index} size mismatch: got {len(data)}, expected {expected_len}",
            )
        with s.lock:
            if index in s.received:
                # Idempotent re-upload of an already-received chunk (resume case).
                s.last_activity = time.time()
                return s
            with open(s.part_path, "r+b") as fh:
                fh.seek(offset)
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            s.received.add(index)
            s.last_activity = time.time()
        return s

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def finalize(self, upload_id: str, expected_sha256: str | None) -> tuple[UploadSession, str]:
        """Verify all chunks are present, size + optional SHA256 match, and
        return the path plus the computed SHA256. The session is marked
        completed; caller is responsible for consuming ``part_path`` (rename
        for retention, or unlink after QA).
        """
        s = self.get(upload_id)
        with s.lock:
            if s.completed:
                raise UploadError(409, "Upload already finalized")
            missing = [i for i in range(s.total_chunks) if i not in s.received]
            if missing:
                more = "\u2026" if len(missing) > 10 else ""
                raise UploadError(
                    400, f"Missing chunks: {missing[:10]}{more}"
                )
            size_on_disk = s.part_path.stat().st_size
            if size_on_disk != s.total_size:
                raise UploadError(
                    400, f"Assembled size mismatch: got {size_on_disk}, expected {s.total_size}"
                )
            # Incremental SHA256 over the assembled file. Bounded memory.
            h = hashlib.sha256()
            with open(s.part_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            if expected_sha256 and expected_sha256.lower() != digest:
                raise UploadError(
                    400,
                    f"SHA256 mismatch: client={expected_sha256.lower()}, server={digest}",
                )
            s.completed = True
            s.last_activity = time.time()
            return s, digest

    def consume(self, upload_id: str) -> None:
        """Drop the session from the registry after the caller has taken
        ownership of ``part_path``."""
        with self._lock:
            self._sessions.pop(upload_id, None)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def cleanup_stale(self) -> int:
        now = time.time()
        removed = 0
        with self._lock:
            stale_ids = [
                sid
                for sid, s in self._sessions.items()
                if not s.completed and (now - s.last_activity) > STALE_TTL_SECONDS
            ]
        for sid in stale_ids:
            self.abort(sid)
            removed += 1
        return removed

    def list_pending(self) -> Iterable[UploadSession]:
        with self._lock:
            return list(self._sessions.values())


def _sanitize_filename(name: str) -> str:
    """Return a filename safe to store next to the SQLite DB.

    Strips path components and any character other than [A-Za-z0-9._- ].
    """
    base = os.path.basename(name or "upload.zip")
    cleaned = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in base)
    return cleaned[:255] or "upload.zip"
