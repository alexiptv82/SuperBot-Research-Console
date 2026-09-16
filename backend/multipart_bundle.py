"""OLD36 multipart-bundle controller.

Reassembles the 5 raw binary parts of ``OLD36_REFERENCE_BUNDLE.zip``
(pre-split by the owner) into the exact byte-identical outer archive
and then hands it to the existing :mod:`bundle_import` pipeline. This
module is *transport only* - it never re-implements QA, never reads
inner-ZIP contents, and never touches accounting.

Design invariants
-----------------

- **Fixed manifest**: only the 5 expected filenames and sizes are
  accepted; anything else is rejected before a single byte is read.
- **Bounded memory**: each part is uploaded via the existing chunked
  uploader (:class:`uploads.UploadManager`) and concatenated 1 MiB at
  a time; neither the individual parts nor the ~2.15 GiB assembled
  bundle ever live in RAM.
- **Deterministic identity**: the reassembled bundle must match
  ``EXPECTED_BUNDLE_SHA256`` exactly. Any deviation aborts the run and
  wipes the reassembled temporary file.
- **Existing bundle path is authoritative**: on SHA match, the
  assembled ZIP is passed unchanged to ``bundle_import.import_bundle``
  which handles inner validation, per-session QA, content-addressed
  retention and audit trail. Nothing is duplicated here.
- **No permanent multipart storage**: on success or failure all five
  part files, the reassembled file, and any workdir are removed.
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

from uploads import UploadError, UploadManager, UploadSession

# ---------------------------------------------------------------------------
# Fixed OLD36 multipart manifest
# ---------------------------------------------------------------------------

EXPECTED_BUNDLE_NAME: str = "OLD36_REFERENCE_BUNDLE.zip"
EXPECTED_BUNDLE_TOTAL_SIZE: int = 2_302_694_463
EXPECTED_BUNDLE_SHA256: str = (
    "b45717e7d32bdd855343e3f89a44719ef1e286b2851fc740fcd6ab88c38acc67"
)

#: Exact filename + size per part, in the ONLY accepted order
#: (lexical == concatenation order == part_index).
EXPECTED_PARTS: tuple[tuple[str, int], ...] = (
    ("OLD36_REFERENCE_BUNDLE.zip.part-00", 471_859_200),
    ("OLD36_REFERENCE_BUNDLE.zip.part-01", 471_859_200),
    ("OLD36_REFERENCE_BUNDLE.zip.part-02", 471_859_200),
    ("OLD36_REFERENCE_BUNDLE.zip.part-03", 471_859_200),
    ("OLD36_REFERENCE_BUNDLE.zip.part-04", 415_257_663),
)

# Sanity gates (fail fast at import time if the constants are ever edited wrong).
assert len(EXPECTED_PARTS) == 5, "multipart manifest must contain exactly 5 parts"
assert (
    sum(sz for _, sz in EXPECTED_PARTS) == EXPECTED_BUNDLE_TOTAL_SIZE
), "sum of part sizes must equal EXPECTED_BUNDLE_TOTAL_SIZE"

STALE_TTL_SECONDS = 6 * 60 * 60  # 6h; aligned with UploadManager


class MultipartBundleError(Exception):
    """Raised for any client-visible multipart validation problem."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class MultipartSlot:
    part_index: int
    part_name: str
    expected_size: int
    upload_id: str
    chunk_size: int | None = None
    total_chunks: int | None = None

    def to_dict(self) -> dict:
        return {
            "part_index": self.part_index,
            "part_name": self.part_name,
            "expected_size": self.expected_size,
            "upload_id": self.upload_id,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
        }


@dataclass
class MultipartSession:
    id: str
    workdir: Path
    reassembled_path: Path
    slots: list[MultipartSlot]
    expected_total_size: int
    expected_sha256: str
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    assembled: bool = False

    def to_dict(self) -> dict:
        return {
            "multipart_id": self.id,
            "expected_parts": len(self.slots),
            "expected_total_size": self.expected_total_size,
            "expected_sha256": self.expected_sha256,
            "expected_bundle_name": EXPECTED_BUNDLE_NAME,
            "slots": [s.to_dict() for s in self.slots],
            "assembled": self.assembled,
            "created_at": self.created_at,
        }


class MultipartBundleController:
    """Tracks in-flight multipart bundle uploads.

    Only one manifest is currently supported (the frozen OLD36 5-part
    split), but the controller keeps the shape open for future
    additions.
    """

    def __init__(
        self,
        *,
        upload_manager: UploadManager,
        base_dir: Path,
    ) -> None:
        self.upload_manager = upload_manager
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, MultipartSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self, declared_parts: list[dict]) -> MultipartSession:
        """Create a fresh multipart session.

        ``declared_parts`` is the client-declared list of files the
        browser is about to upload. It MUST match ``EXPECTED_PARTS``
        exactly (filename + size + count + order). Duplicates,
        wrong sizes, wrong names or wrong ordering are rejected here,
        before any bytes are written.
        """
        if not isinstance(declared_parts, list):
            raise MultipartBundleError(400, "declared_parts must be a list")

        # Order-preserving name uniqueness check.
        seen_names: list[str] = []
        for entry in declared_parts:
            name = (entry or {}).get("name")
            if name in seen_names:
                raise MultipartBundleError(400, f"duplicate part name: {name}")
            seen_names.append(name)

        # Count check.
        if len(declared_parts) != len(EXPECTED_PARTS):
            raise MultipartBundleError(
                400,
                f"expected {len(EXPECTED_PARTS)} parts, got {len(declared_parts)}",
            )

        # Element-wise validation.
        for idx, ((exp_name, exp_size), entry) in enumerate(
            zip(EXPECTED_PARTS, declared_parts)
        ):
            got_name = (entry or {}).get("name")
            got_size = (entry or {}).get("size")
            if got_name != exp_name:
                raise MultipartBundleError(
                    400,
                    f"slot {idx}: expected part name {exp_name!r}, got {got_name!r} "
                    "(client MUST send parts in strict order part-00 .. part-04)",
                )
            if got_size != exp_size:
                raise MultipartBundleError(
                    400,
                    f"slot {idx} ({exp_name}): expected size {exp_size} bytes, "
                    f"got {got_size} bytes",
                )

        mp_id = uuid.uuid4().hex
        workdir = self.base_dir / mp_id
        workdir.mkdir(parents=True, exist_ok=True)
        reassembled_path = workdir / f"{EXPECTED_BUNDLE_NAME}.part-reassembled"

        # Open a chunked upload for every part on the shared bundle_manager.
        slots: list[MultipartSlot] = []
        try:
            for idx, (name, size) in enumerate(EXPECTED_PARTS):
                us = self.upload_manager.init(
                    filename=name,
                    total_size=size,
                    retain_raw=False,
                    checkpoint_hint=None,
                )
                slot = MultipartSlot(
                    part_index=idx,
                    part_name=name,
                    expected_size=size,
                    upload_id=us.id,
                )
                # Attach transport metadata for the client after the fact.
                slot.chunk_size = us.chunk_size  # type: ignore[attr-defined]
                slot.total_chunks = us.total_chunks  # type: ignore[attr-defined]
                slots.append(slot)
        except Exception:
            # Roll back any partial slots so we don't leak .part files.
            for s in slots:
                try:
                    self.upload_manager.abort(s.upload_id)
                except Exception:
                    pass
            raise

        session = MultipartSession(
            id=mp_id,
            workdir=workdir,
            reassembled_path=reassembled_path,
            slots=slots,
            expected_total_size=EXPECTED_BUNDLE_TOTAL_SIZE,
            expected_sha256=EXPECTED_BUNDLE_SHA256,
        )
        with self._lock:
            self._sessions[mp_id] = session
        return session

    def get(self, mp_id: str) -> MultipartSession:
        with self._lock:
            s = self._sessions.get(mp_id)
        if s is None:
            raise MultipartBundleError(404, "Unknown multipart_id")
        return s

    def abort(self, mp_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(mp_id, None)
        if s is None:
            return
        # Abort every child upload so its .part file is unlinked.
        for slot in s.slots:
            try:
                self.upload_manager.abort(slot.upload_id)
            except Exception:
                pass
        # Nuke the workdir.
        _rmtree_safe(s.workdir)

    def cleanup_stale(self) -> int:
        now = time.time()
        with self._lock:
            stale_ids = [
                sid
                for sid, s in self._sessions.items()
                if not s.assembled and (now - s.last_activity) > STALE_TTL_SECONDS
            ]
        for sid in stale_ids:
            self.abort(sid)
        return len(stale_ids)

    def list_sessions(self) -> Iterable[MultipartSession]:
        with self._lock:
            return list(self._sessions.values())

    # ------------------------------------------------------------------
    # Assemble
    # ------------------------------------------------------------------

    def status(self, mp_id: str) -> dict:
        s = self.get(mp_id)
        # Per-slot progress reflects the underlying chunked uploads.
        detail = []
        for slot in s.slots:
            try:
                us: UploadSession = self.upload_manager.get(slot.upload_id)
                detail.append(
                    {
                        "part_index": slot.part_index,
                        "part_name": slot.part_name,
                        "expected_size": slot.expected_size,
                        "upload_id": slot.upload_id,
                        "received_chunks": len(us.received),
                        "total_chunks": us.total_chunks,
                        "complete": len(us.received) == us.total_chunks,
                    }
                )
            except UploadError:
                # The child upload was aborted; surface this so the client
                # can re-init.
                detail.append(
                    {
                        "part_index": slot.part_index,
                        "part_name": slot.part_name,
                        "expected_size": slot.expected_size,
                        "upload_id": slot.upload_id,
                        "received_chunks": 0,
                        "total_chunks": 0,
                        "complete": False,
                        "missing": True,
                    }
                )
        return {
            "multipart_id": s.id,
            "expected_parts": len(s.slots),
            "expected_total_size": s.expected_total_size,
            "expected_sha256": s.expected_sha256,
            "assembled": s.assembled,
            "parts": detail,
        }

    def assemble(self, mp_id: str) -> tuple[MultipartSession, Path, str]:
        """Verify every part is fully received, concatenate them in
        order, verify total size + SHA256, and return the reassembled
        path. On any failure the reassembled file is deleted and
        :class:`MultipartBundleError` is raised.
        """
        s = self.get(mp_id)
        with s.lock:
            if s.assembled:
                raise MultipartBundleError(409, "Multipart bundle already assembled")

            # 1. Every child upload must be fully received.
            for slot in s.slots:
                try:
                    us = self.upload_manager.get(slot.upload_id)
                except UploadError as exc:
                    raise MultipartBundleError(
                        400, f"part {slot.part_name}: {exc.detail}"
                    ) from exc
                if len(us.received) != us.total_chunks:
                    missing_count = us.total_chunks - len(us.received)
                    raise MultipartBundleError(
                        400,
                        f"part {slot.part_name}: {missing_count} chunk(s) missing",
                    )
                on_disk = us.part_path.stat().st_size
                if on_disk != slot.expected_size:
                    raise MultipartBundleError(
                        400,
                        f"part {slot.part_name}: size on disk {on_disk} != "
                        f"expected {slot.expected_size}",
                    )

            # 2. Bounded-memory concatenation with incremental SHA256.
            hasher = hashlib.sha256()
            written = 0
            reassembled_tmp = s.reassembled_path
            # Fresh file per attempt.
            try:
                reassembled_tmp.unlink()
            except FileNotFoundError:
                pass
            fd = os.open(
                str(reassembled_tmp),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o644,
            )
            try:
                for slot in s.slots:
                    us = self.upload_manager.get(slot.upload_id)
                    part_size = 0
                    src_fd = os.open(str(us.part_path), os.O_RDONLY)
                    try:
                        while True:
                            chunk = os.read(src_fd, 1024 * 1024)
                            if not chunk:
                                break
                            hasher.update(chunk)
                            offset = 0
                            while offset < len(chunk):
                                offset += os.write(fd, chunk[offset:])
                            part_size += len(chunk)
                    finally:
                        os.close(src_fd)
                    if part_size != slot.expected_size:
                        raise MultipartBundleError(
                            400,
                            f"part {slot.part_name}: read {part_size} bytes, "
                            f"expected {slot.expected_size}",
                        )
                    written += part_size
            finally:
                os.close(fd)

            if written != s.expected_total_size:
                _unlink_safe(reassembled_tmp)
                raise MultipartBundleError(
                    400,
                    f"reassembled size {written} != expected {s.expected_total_size}",
                )
            digest = hasher.hexdigest()
            if digest != s.expected_sha256:
                _unlink_safe(reassembled_tmp)
                raise MultipartBundleError(
                    400,
                    f"reassembled SHA256 mismatch: got {digest}, "
                    f"expected {s.expected_sha256}",
                )

            # 3. Atomic rename to the canonical outer bundle name for
            #    the downstream importer.
            final_path = s.workdir / EXPECTED_BUNDLE_NAME
            os.replace(reassembled_tmp, final_path)
            s.assembled = True
            s.last_activity = time.time()
            return s, final_path, digest

    def consume(self, mp_id: str) -> None:
        """Called after the caller has taken ownership of the assembled
        bundle (or aborted). Unlinks part files, wipes the workdir.
        """
        with self._lock:
            s = self._sessions.pop(mp_id, None)
        if s is None:
            return
        for slot in s.slots:
            try:
                self.upload_manager.abort(slot.upload_id)
            except Exception:
                pass
        _rmtree_safe(s.workdir)


# ---------------------------------------------------------------------------
# small local helpers - kept private to avoid pulling shutil into the
# hot path of the assembler.
# ---------------------------------------------------------------------------


def _unlink_safe(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _rmtree_safe(p: Path) -> None:
    import shutil

    try:
        shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


__all__ = [
    "EXPECTED_PARTS",
    "EXPECTED_BUNDLE_NAME",
    "EXPECTED_BUNDLE_SHA256",
    "EXPECTED_BUNDLE_TOTAL_SIZE",
    "MultipartBundleController",
    "MultipartBundleError",
    "MultipartSession",
    "MultipartSlot",
]
