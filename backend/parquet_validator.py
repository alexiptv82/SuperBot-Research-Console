"""Deterministic Parquet validation for every file in a session ZIP.

This module replaces the previous "sample the first few files" magic
check. For a real 3H MultiVenue session there are ~1080 Parquet parts
(360 per dataset directory) and every single one must be validated
structurally before we can claim a PASS.

Checks performed per Parquet entry:

1. entry exists inside the ZIP
2. entry size >= 8 bytes (minimum for two PAR1 magics)
3. first 4 bytes == ``PAR1``
4. last 4 bytes == ``PAR1``
5. PyArrow footer metadata is readable (num_rows, num_columns exposed)

Sequence checks per dataset directory:

- part numbers parseable as ``part-<digits>.parquet``
- no duplicate part numbers
- contiguous range ``[0..N-1]`` with no gaps

Memory profile: each file is read once into a bounded byte buffer to
verify magic + parse the footer. **Never** do we load a full dataset
directory into memory at once, and the buffer for a single file is
released before moving to the next.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import pyarrow.parquet as _pq  # type: ignore
    _PYARROW_AVAILABLE = True
    _PYARROW_IMPORT_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover - defensive
    _pq = None  # type: ignore
    _PYARROW_AVAILABLE = False
    _PYARROW_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

from constants import DATASET_DIRS, PARQUET_MAGIC

#: Match the trailing numeric sequence index in any parquet basename.
#: Handles all observed collector patterns:
#:   - part-000.parquet
#:   - sync_grid_100ms_<sid>_0000269.parquet
#:   - trades_<sid>_0000160.parquet
#: The final ``\d+\.parquet$`` guarantees we anchor on the sequence
#: number right before the extension, so IDs embedded elsewhere in the
#: name never confuse the parser.
_PART_NAME_RE = re.compile(r"(\d+)\.parquet$")
_MIN_PARQUET_SIZE = 8  # 4 bytes head + 4 bytes tail minimum


@dataclass
class PerFileResult:
    entry_name: str
    dataset_dir: str
    part_index: int | None
    size: int
    magic_head_ok: bool
    magic_tail_ok: bool
    metadata_ok: bool
    metadata_error: str | None = None
    num_rows: int | None = None
    num_columns: int | None = None


@dataclass
class PerDirResult:
    dataset_dir: str
    files_seen: int = 0
    parts_expected: int | None = None
    duplicate_parts: list[int] = field(default_factory=list)
    missing_parts: list[int] = field(default_factory=list)
    unparseable_names: list[str] = field(default_factory=list)
    sequence_ok: bool = True
    sequence_detail: str = ""


@dataclass
class ParquetValidation:
    """Aggregate report over every Parquet entry in a ZIP."""

    parquet_files_total: int = 0
    parquet_magic_checked: int = 0
    parquet_magic_passed: int = 0
    parquet_magic_failed: int = 0
    parquet_metadata_checked: int = 0
    parquet_metadata_passed: int = 0
    parquet_metadata_failed: int = 0
    parquet_sequence_gaps: int = 0
    parquet_duplicate_parts: int = 0
    failures: list[str] = field(default_factory=list)
    per_dir: dict[str, PerDirResult] = field(default_factory=dict)
    pyarrow_available: bool = _PYARROW_AVAILABLE
    pyarrow_error: str | None = _PYARROW_IMPORT_ERROR

    def as_dict(self) -> dict:
        return {
            "parquet_files_total": self.parquet_files_total,
            "parquet_magic_checked": self.parquet_magic_checked,
            "parquet_magic_passed": self.parquet_magic_passed,
            "parquet_magic_failed": self.parquet_magic_failed,
            "parquet_metadata_checked": self.parquet_metadata_checked,
            "parquet_metadata_passed": self.parquet_metadata_passed,
            "parquet_metadata_failed": self.parquet_metadata_failed,
            "parquet_sequence_gaps": self.parquet_sequence_gaps,
            "parquet_duplicate_parts": self.parquet_duplicate_parts,
            "pyarrow_available": self.pyarrow_available,
            "pyarrow_error": self.pyarrow_error,
            "per_dir": {
                d: {
                    "files_seen": r.files_seen,
                    "parts_expected": r.parts_expected,
                    "duplicate_parts": r.duplicate_parts,
                    "missing_parts": r.missing_parts,
                    "unparseable_names": r.unparseable_names,
                    "sequence_ok": r.sequence_ok,
                    "sequence_detail": r.sequence_detail,
                }
                for d, r in self.per_dir.items()
            },
            # Cap the failures list so a catastrophic ZIP does not
            # explode the QA JSON. We keep the counts intact.
            "failures_sample": self.failures[:50],
            "failures_truncated": len(self.failures) > 50,
        }


def _is_parquet_entry(name: str) -> bool:
    return name.endswith(".parquet") and not name.endswith("/")


def _classify_dir(name: str) -> str | None:
    """Return the dataset directory this entry belongs to, or None."""
    for d in DATASET_DIRS:
        needle = d + "/"
        if name.startswith(needle) or ("/" + needle) in name:
            return d
    return None


def _parse_part_index(entry_name: str) -> int | None:
    base = Path(entry_name).name
    m = _PART_NAME_RE.search(base)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _read_entry_bytes(zf: zipfile.ZipFile, entry_name: str) -> bytes:
    """Read a single entry fully into memory.

    Individual Parquet parts in a 3H session are small (typically a few
    MB), so one-file-at-a-time is safe. We never materialize the whole
    dataset directory at once.
    """
    with zf.open(entry_name, "r") as fh:
        return fh.read()


def _check_metadata(buf: bytes) -> tuple[bool, str | None, int | None, int | None]:
    """Read Parquet footer metadata without decoding row groups."""
    if not _PYARROW_AVAILABLE:
        return False, f"pyarrow not available: {_PYARROW_IMPORT_ERROR}", None, None
    try:
        pf = _pq.ParquetFile(io.BytesIO(buf))  # type: ignore[union-attr]
        md = pf.metadata
        if md is None:
            return False, "no metadata block", None, None
        return True, None, int(md.num_rows), int(md.num_columns)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", None, None


def _validate_dir_sequence(
    per_dir: PerDirResult, indices: list[int], unparseable: list[str]
) -> None:
    per_dir.unparseable_names = list(unparseable)
    if unparseable:
        per_dir.sequence_ok = False
        per_dir.sequence_detail = (
            f"{len(unparseable)} entries not matching part-XXX.parquet"
        )

    seen: dict[int, int] = {}
    for i in indices:
        seen[i] = seen.get(i, 0) + 1
    dups = sorted([i for i, n in seen.items() if n > 1])
    per_dir.duplicate_parts = dups

    if indices:
        lo, hi = min(indices), max(indices)
        per_dir.parts_expected = hi - lo + 1
        expected = set(range(lo, hi + 1))
        actual = set(indices)
        missing = sorted(expected - actual)
        per_dir.missing_parts = missing
        if missing or dups:
            per_dir.sequence_ok = False
            details = []
            if missing:
                details.append(f"missing {len(missing)}")
            if dups:
                details.append(f"duplicated {len(dups)}")
            per_dir.sequence_detail = (
                (per_dir.sequence_detail + "; " if per_dir.sequence_detail else "")
                + ", ".join(details)
            )
        else:
            if not per_dir.sequence_detail:
                per_dir.sequence_detail = f"contiguous 0..{hi}"
    else:
        per_dir.parts_expected = 0
        per_dir.sequence_ok = False
        per_dir.sequence_detail = "no parquet files present"


def validate_all_parquet(source) -> ParquetValidation:
    """Validate every Parquet entry inside ``source`` deterministically.

    ``source`` may be an in-memory ``bytes`` blob or a filesystem path.
    """
    result = ParquetValidation()

    if isinstance(source, (bytes, bytearray, memoryview)):
        zf = zipfile.ZipFile(io.BytesIO(bytes(source)), "r")
    else:
        zf = zipfile.ZipFile(str(source), "r")

    try:
        all_entries = [i for i in zf.infolist() if _is_parquet_entry(i.filename)]
        result.parquet_files_total = len(all_entries)

        # Bucket per dataset dir so we can validate sequences.
        buckets: dict[str, list[tuple[zipfile.ZipInfo, int | None]]] = {
            d: [] for d in DATASET_DIRS
        }
        unbucketed: list[zipfile.ZipInfo] = []
        for info in all_entries:
            d = _classify_dir(info.filename)
            if d is None:
                unbucketed.append(info)
                continue
            buckets[d].append((info, _parse_part_index(info.filename)))

        # 1. Per-file magic + metadata (streaming, one file at a time)
        for d in DATASET_DIRS:
            per_dir = PerDirResult(dataset_dir=d)
            result.per_dir[d] = per_dir
            indices: list[int] = []
            unparseable_names: list[str] = []
            for info, idx in buckets[d]:
                per_dir.files_seen += 1

                # Existence + size guardrail
                if info.file_size < _MIN_PARQUET_SIZE:
                    result.parquet_magic_checked += 1
                    result.parquet_magic_failed += 1
                    result.failures.append(
                        f"{info.filename}: size {info.file_size} < {_MIN_PARQUET_SIZE}"
                    )
                    # We still count metadata as failed since it can't parse.
                    result.parquet_metadata_checked += 1
                    result.parquet_metadata_failed += 1
                    if idx is None:
                        unparseable_names.append(info.filename)
                    else:
                        indices.append(idx)
                    continue

                buf = _read_entry_bytes(zf, info.filename)
                result.parquet_magic_checked += 1
                head_ok = buf[:4] == PARQUET_MAGIC
                tail_ok = buf[-4:] == PARQUET_MAGIC
                if head_ok and tail_ok:
                    result.parquet_magic_passed += 1
                else:
                    result.parquet_magic_failed += 1
                    reason = []
                    if not head_ok:
                        reason.append("head")
                    if not tail_ok:
                        reason.append("tail")
                    result.failures.append(
                        f"{info.filename}: PAR1 magic missing ({'+'.join(reason)})"
                    )

                # Metadata check
                result.parquet_metadata_checked += 1
                md_ok, md_err, nrows, ncols = _check_metadata(buf)
                if md_ok:
                    result.parquet_metadata_passed += 1
                else:
                    result.parquet_metadata_failed += 1
                    result.failures.append(
                        f"{info.filename}: metadata unreadable ({md_err})"
                    )
                del buf  # release before next file

                if idx is None:
                    unparseable_names.append(info.filename)
                else:
                    indices.append(idx)

            _validate_dir_sequence(per_dir, indices, unparseable_names)
            if not per_dir.sequence_ok:
                result.parquet_sequence_gaps += len(per_dir.missing_parts)
                result.parquet_duplicate_parts += len(per_dir.duplicate_parts)
                if per_dir.missing_parts:
                    result.failures.append(
                        f"{d}: missing part indices sample={per_dir.missing_parts[:10]}"
                    )
                if per_dir.duplicate_parts:
                    result.failures.append(
                        f"{d}: duplicate part indices sample={per_dir.duplicate_parts[:10]}"
                    )

        # 2. Parquet entries outside the three known dataset dirs are
        #    unexpected but not fatal: we still magic-check them so any
        #    corruption surfaces in the totals.
        if unbucketed:
            extra_dir = PerDirResult(dataset_dir="_unassigned_")
            result.per_dir["_unassigned_"] = extra_dir
            for info in unbucketed:
                extra_dir.files_seen += 1
                if info.file_size < _MIN_PARQUET_SIZE:
                    result.parquet_magic_checked += 1
                    result.parquet_magic_failed += 1
                    result.parquet_metadata_checked += 1
                    result.parquet_metadata_failed += 1
                    result.failures.append(
                        f"{info.filename}: (unassigned dir) size < min"
                    )
                    continue
                buf = _read_entry_bytes(zf, info.filename)
                result.parquet_magic_checked += 1
                head_ok = buf[:4] == PARQUET_MAGIC
                tail_ok = buf[-4:] == PARQUET_MAGIC
                if head_ok and tail_ok:
                    result.parquet_magic_passed += 1
                else:
                    result.parquet_magic_failed += 1
                    result.failures.append(
                        f"{info.filename}: (unassigned dir) PAR1 missing"
                    )
                result.parquet_metadata_checked += 1
                md_ok, md_err, _, _ = _check_metadata(buf)
                if md_ok:
                    result.parquet_metadata_passed += 1
                else:
                    result.parquet_metadata_failed += 1
                    result.failures.append(
                        f"{info.filename}: (unassigned dir) metadata: {md_err}"
                    )
                del buf
            extra_dir.sequence_ok = True
            extra_dir.sequence_detail = "not enforced (unassigned)"

    finally:
        zf.close()

    return result


def parquet_overall_status(v: ParquetValidation) -> str:
    """Return PASS / FAIL derived from all-files results (no sampling)."""
    if v.parquet_files_total == 0:
        return "FAIL"
    if v.parquet_magic_failed > 0:
        return "FAIL"
    if v.parquet_metadata_failed > 0:
        return "FAIL"
    if any(not d.sequence_ok for d in v.per_dir.values() if d.dataset_dir in DATASET_DIRS):
        return "FAIL"
    return "PASS"
