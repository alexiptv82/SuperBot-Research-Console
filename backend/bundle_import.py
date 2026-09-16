"""OLD36_REFERENCE bundle importer.

One outer ZIP contains the eleven historical session ZIPs. This
module verifies the outer archive is safe, cross-checks every inner
filename against the frozen ``OLD36_REFERENCE_SESSIONS`` list, then
hands each inner ZIP to the existing single-session ingestion
pipeline via a caller-supplied ``process_inner`` callback.

Design goals
------------

- **Bounded memory**: the outer ZIP is opened via :mod:`zipfile`,
  each inner entry is streamed to a temporary file in fixed-size
  chunks. Nothing is loaded whole into RAM.
- **Not a session**: the outer archive itself never enters the
  session registry, never gets a QA run, never gets retained. It is
  only transport.
- **Frozen identity**: only files whose base name matches the
  pattern ``Bitget_MultiVenue_Microstructure_V2_SESSION_<sid>_<H>H.zip``
  and whose ``<sid>`` is in ``OLD36_REFERENCE_SESSIONS`` are
  accepted. Anything else is a hard error.
- **Traversal safety**: any inner entry with ``..``, absolute
  paths, drive letters, backslashes, NUL bytes, symlinks or a
  non-``.zip`` extension is refused before extraction.
- **Deterministic per-session results**: the caller receives a
  list of dicts, one per inner ZIP, describing verdict / duplicate
  status / retained state / errors. Partial failures leave the
  successful sessions committed.
- **Idempotent retry**: because retention is content-addressed
  (see ``raw_storage``) and the QA pipeline classifies duplicates
  by ``source_file_sha256``, re-importing the same bundle never
  double-counts hours or duplicates storage.

Nothing in this module mutates milestone totals, touches
FrozenAnalysisEngine, or reads NEW36 data. The recovery sandbox
firewall is orthogonal and remains in force.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from checkpoint_registry import (
    CHECKPOINT_OLD36_REFERENCE,
    OLD36_REFERENCE_SESSIONS,
)


class BundleImportError(Exception):
    """Raised for validation problems on the outer archive itself.

    Per-session failures (bad manifest, corrupt inner ZIP body,
    QA-engine FAIL) are surfaced through the per-session result
    list instead.
    """


# ---------------------------------------------------------------------------
# Filename policy
# ---------------------------------------------------------------------------

#: The inner-filename pattern the frozen bundle producer emits. We anchor
#: on the trailing ``_<H>H.zip`` and capture the ``session_id`` in the middle.
_INNER_NAME_RE = re.compile(
    r"^Bitget_MultiVenue_Microstructure_V2_SESSION_"
    r"(?P<sid>[0-9A-Za-z_]+)_"
    r"(?P<hours>\d+)H\.zip$"
)

_EXPECTED_IDS: frozenset[str] = frozenset(OLD36_REFERENCE_SESSIONS)


def _classify_entry_name(name: str) -> tuple[str, str | None]:
    """Return ``(kind, session_id_or_None)`` for a raw ZIP entry name.

    Kinds:
    - ``"traversal"``  - refuse: ``..``, absolute path, NUL, backslash
    - ``"nested"``     - refuse: ``.zip`` files below a subdirectory
    - ``"non_zip"``    - refuse: any file not ending in ``.zip``
    - ``"directory"``  - refuse: directory entries
    - ``"non_conforming"`` - ``.zip`` at top level but the filename
       doesn't match the frozen inner pattern
    - ``"ok"``         - inner ZIP name matches the pattern and belongs
       to the frozen OLD36_REFERENCE list. ``session_id_or_None`` = sid.
    """
    if name.endswith("/"):
        return "directory", None
    if "\x00" in name or "\\" in name:
        return "traversal", None
    # Reject absolute paths and any ".." segment.
    if name.startswith("/") or "../" in name or name == ".." or name.startswith("../"):
        return "traversal", None
    # Windows drive prefix (e.g. C:\foo). Backslash covered above; also refuse
    # explicit ``<letter>:``.
    if len(name) >= 2 and name[1] == ":":
        return "traversal", None
    # Only top-level entries allowed. ``foo/bar.zip`` -> nested.
    if "/" in name:
        return "nested", None
    if not name.lower().endswith(".zip"):
        return "non_zip", None
    m = _INNER_NAME_RE.match(name)
    if not m:
        return "non_conforming", None
    sid = m.group("sid")
    return "ok", sid


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class BundleValidation:
    accepted: list[tuple[str, str]] = field(default_factory=list)
    """List of ``(entry_name, session_id)`` for each accepted inner ZIP."""

    duplicates: list[str] = field(default_factory=list)
    """Entry names that appear more than once inside the outer ZIP."""

    traversal_entries: list[str] = field(default_factory=list)
    nested_entries: list[str] = field(default_factory=list)
    non_zip_entries: list[str] = field(default_factory=list)
    non_conforming_entries: list[str] = field(default_factory=list)
    directory_entries: list[str] = field(default_factory=list)
    unexpected_session_ids: list[str] = field(default_factory=list)
    """Inner ZIPs whose name is well-formed but whose session_id is NOT
    in ``OLD36_REFERENCE_SESSIONS``."""

    missing_session_ids: list[str] = field(default_factory=list)
    """OLD36_REFERENCE session_ids for which the bundle does not
    contain a matching inner ZIP."""

    def is_ok(self) -> bool:
        return not any(
            [
                self.duplicates,
                self.traversal_entries,
                self.nested_entries,
                self.non_zip_entries,
                self.non_conforming_entries,
                self.directory_entries,
                self.unexpected_session_ids,
                self.missing_session_ids,
            ]
        )

    def failure_summary(self) -> str:
        parts: list[str] = []
        if self.duplicates:
            parts.append(f"duplicate inner entries: {self.duplicates[:5]}")
        if self.traversal_entries:
            parts.append(f"path-traversal entries: {self.traversal_entries[:5]}")
        if self.nested_entries:
            parts.append(f"nested entries (must be top-level): {self.nested_entries[:5]}")
        if self.non_zip_entries:
            parts.append(f"non-.zip entries: {self.non_zip_entries[:5]}")
        if self.non_conforming_entries:
            parts.append(
                f"filenames not matching bundle pattern: {self.non_conforming_entries[:5]}"
            )
        if self.directory_entries:
            parts.append(f"directory entries not permitted: {self.directory_entries[:5]}")
        if self.unexpected_session_ids:
            parts.append(
                f"unexpected session_ids (not in OLD36_REFERENCE): {self.unexpected_session_ids[:5]}"
            )
        if self.missing_session_ids:
            parts.append(
                f"missing OLD36_REFERENCE session_ids: {self.missing_session_ids[:5]}"
            )
        return "; ".join(parts) or "ok"


def validate_bundle(zf: zipfile.ZipFile) -> BundleValidation:
    """Enumerate outer-archive entries and classify each one.

    The function is pure w.r.t. the ZIP object and never reads
    entry payloads.
    """
    v = BundleValidation()

    # ZIP allows duplicate names. Track them explicitly.
    seen_names: dict[str, int] = {}
    for info in zf.infolist():
        seen_names[info.filename] = seen_names.get(info.filename, 0) + 1

    accepted_sids: set[str] = set()

    for info in zf.infolist():
        name = info.filename
        # ``ZipInfo.file_size`` for directory entries is 0; we treat both
        # dir markers and 0-length entries with trailing slash uniformly.
        # An entry inside the archive listed as an OS symlink (external
        # attr 0xA1FF...) should also be refused; ``zipfile`` does not
        # follow symlinks so we just refuse the entry outright.
        if _is_symlink_entry(info):
            v.traversal_entries.append(name)
            continue
        kind, sid = _classify_entry_name(name)
        if kind == "directory":
            v.directory_entries.append(name)
            continue
        if kind == "traversal":
            v.traversal_entries.append(name)
            continue
        if kind == "nested":
            v.nested_entries.append(name)
            continue
        if kind == "non_zip":
            v.non_zip_entries.append(name)
            continue
        if kind == "non_conforming":
            v.non_conforming_entries.append(name)
            continue
        # kind == "ok" -> we have a session_id
        assert sid is not None
        if sid not in _EXPECTED_IDS:
            v.unexpected_session_ids.append(sid)
            continue
        if seen_names.get(name, 0) > 1:
            if name not in v.duplicates:
                v.duplicates.append(name)
            continue
        v.accepted.append((name, sid))
        accepted_sids.add(sid)

    v.missing_session_ids = sorted(_EXPECTED_IDS - accepted_sids)
    return v


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    # On UNIX, symlinks are stored with mode 0o120000 in the upper 16
    # bits of external_attr.
    mode = (info.external_attr >> 16) & 0xFFFF
    return (mode & 0o170000) == 0o120000


# ---------------------------------------------------------------------------
# Streaming extraction
# ---------------------------------------------------------------------------

_STREAM_CHUNK = 1024 * 1024  # 1 MiB


def _stream_extract(zf: zipfile.ZipFile, entry_name: str, dest: Path) -> None:
    """Copy one ZIP member to ``dest`` in bounded-memory chunks."""
    with zf.open(entry_name, "r") as src, open(dest, "wb") as dst:
        while True:
            buf = src.read(_STREAM_CHUNK)
            if not buf:
                break
            dst.write(buf)


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


@dataclass
class SessionImportResult:
    session_id: str
    inner_name: str
    ok: bool
    result: dict | None = None
    error: str | None = None


ProcessInner = Callable[[Path, str, str], dict]
"""Callback signature: ``process_inner(inner_path, inner_filename,
checkpoint_hint) -> per-session result dict``. The dict must be the
same shape used by ``/api/uploads/{id}/complete`` responses so the
frontend can render it verbatim.
"""


def import_bundle(
    outer_zip_path: Path,
    *,
    process_inner: ProcessInner,
    workdir: Path | None = None,
    on_progress: Callable[[str, dict], None] | None = None,
) -> list[SessionImportResult]:
    """Validate + extract + dispatch the outer OLD36 bundle.

    Parameters
    ----------
    outer_zip_path:
        Path to the assembled outer ZIP. Never modified. The caller
        is responsible for unlinking it afterwards.
    process_inner:
        Callback that runs the existing session ingestion pipeline on
        the extracted inner ZIP path. Must be side-effect complete
        (audit, retention, DB commit) before returning.
    workdir:
        Extraction root. Defaults to a fresh subdir under ``/tmp``.
        Automatically wiped at the end of the call (successful or
        not) so no orphan bytes are left behind.
    on_progress:
        Optional callback ``on_progress(stage, payload)`` invoked at
        each transition: ``"validated"``, ``"session_start"``,
        ``"session_end"``, ``"cleanup"``.

    Returns
    -------
    list[SessionImportResult]
        One entry per accepted inner ZIP.

    Raises
    ------
    BundleImportError
        For any outer-archive validation failure.
    """
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="old36-bundle-"))
    else:
        workdir.mkdir(parents=True, exist_ok=True)

    try:
        try:
            zf = zipfile.ZipFile(str(outer_zip_path), "r")
        except zipfile.BadZipFile as exc:
            raise BundleImportError(f"outer file is not a valid ZIP: {exc}") from exc

        try:
            validation = validate_bundle(zf)
            if not validation.is_ok():
                raise BundleImportError(validation.failure_summary())
            if on_progress is not None:
                on_progress(
                    "validated",
                    {
                        "expected": len(_EXPECTED_IDS),
                        "found": len(validation.accepted),
                    },
                )

            results: list[SessionImportResult] = []
            # Iterate in the frozen OLD36_REFERENCE order for stable
            # per-session progress and deterministic audit trails.
            by_sid = {sid: name for name, sid in validation.accepted}
            for i, sid in enumerate(OLD36_REFERENCE_SESSIONS, start=1):
                inner_name = by_sid[sid]
                inner_path = workdir / inner_name
                if on_progress is not None:
                    on_progress(
                        "session_start",
                        {
                            "index": i,
                            "total": len(OLD36_REFERENCE_SESSIONS),
                            "session_id": sid,
                            "inner_name": inner_name,
                        },
                    )
                try:
                    _stream_extract(zf, inner_name, inner_path)
                    per = process_inner(
                        inner_path,
                        inner_name,
                        CHECKPOINT_OLD36_REFERENCE,
                    )
                    results.append(
                        SessionImportResult(
                            session_id=sid,
                            inner_name=inner_name,
                            ok=True,
                            result=per,
                        )
                    )
                except Exception as exc:
                    results.append(
                        SessionImportResult(
                            session_id=sid,
                            inner_name=inner_name,
                            ok=False,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                finally:
                    # Free the extracted bytes immediately.
                    try:
                        if inner_path.exists():
                            inner_path.unlink()
                    except OSError:
                        pass
                if on_progress is not None:
                    on_progress(
                        "session_end",
                        {
                            "index": i,
                            "total": len(OLD36_REFERENCE_SESSIONS),
                            "session_id": sid,
                            "ok": results[-1].ok,
                        },
                    )
            return results
        finally:
            zf.close()
    finally:
        # Always wipe the workdir. The outer ZIP itself is the caller's
        # responsibility (endpoint unlinks it after the response is built).
        if on_progress is not None:
            on_progress("cleanup", {"workdir": str(workdir)})
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass


def summarize(results: Iterable[SessionImportResult]) -> dict:
    """Compact tally for API responses."""
    rs = list(results)
    ok = sum(1 for r in rs if r.ok)
    fail = sum(1 for r in rs if not r.ok)
    return {
        "total": len(rs),
        "ok": ok,
        "failed": fail,
        "sessions": [
            {
                "session_id": r.session_id,
                "inner_name": r.inner_name,
                "ok": r.ok,
                "result": r.result,
                "error": r.error,
            }
            for r in rs
        ],
    }


__all__ = [
    "BundleImportError",
    "BundleValidation",
    "SessionImportResult",
    "validate_bundle",
    "import_bundle",
    "summarize",
]
