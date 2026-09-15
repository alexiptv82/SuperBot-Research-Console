"""Tests for the all-files Parquet validator (parquet_validator.py)."""
from __future__ import annotations

import io
import zipfile

import pytest

from constants import PARQUET_MAGIC
from parquet_validator import (
    ParquetValidation,
    parquet_overall_status,
    validate_all_parquet,
)


def _make_valid_parquet_bytes() -> bytes:
    """Return real pyarrow-generated Parquet bytes."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    tbl = pa.table({"x": pa.array([1, 2, 3], type=pa.int64())})
    sink = io.BytesIO()
    pq.write_table(tbl, sink)
    return sink.getvalue()


def _build_zip(*entries: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries:
            zf.writestr(name, body)
    return buf.getvalue()


class TestParquetValidatorAllFiles:
    def test_pyarrow_is_available_in_this_env(self):
        v = ParquetValidation()
        assert v.pyarrow_available, (
            "pyarrow must be installed for deterministic Parquet checks. "
            "See requirements.txt."
        )

    def test_all_files_pass(self):
        good = _make_valid_parquet_bytes()
        zbytes = _build_zip(
            *[(f"sync_grid_100ms/part-{i:03d}.parquet", good) for i in range(3)],
            *[(f"normalized_books/part-{i:03d}.parquet", good) for i in range(3)],
            *[(f"normalized_trades/part-{i:03d}.parquet", good) for i in range(3)],
        )
        v = validate_all_parquet(zbytes)
        assert v.parquet_files_total == 9
        assert v.parquet_magic_checked == 9
        assert v.parquet_magic_passed == 9
        assert v.parquet_magic_failed == 0
        assert v.parquet_metadata_checked == 9
        assert v.parquet_metadata_passed == 9
        assert v.parquet_metadata_failed == 0
        assert v.parquet_sequence_gaps == 0
        assert v.parquet_duplicate_parts == 0
        assert parquet_overall_status(v) == "PASS"

    def test_bad_magic_head_fails_file(self):
        good = _make_valid_parquet_bytes()
        # Corrupt head magic in one file.
        bad = b"XXXX" + good[4:]
        zbytes = _build_zip(
            ("sync_grid_100ms/part-000.parquet", bad),
            ("sync_grid_100ms/part-001.parquet", good),
            ("normalized_books/part-000.parquet", good),
            ("normalized_books/part-001.parquet", good),
            ("normalized_trades/part-000.parquet", good),
            ("normalized_trades/part-001.parquet", good),
        )
        v = validate_all_parquet(zbytes)
        assert v.parquet_magic_failed >= 1
        assert parquet_overall_status(v) == "FAIL"

    def test_bad_magic_tail_fails_file(self):
        good = _make_valid_parquet_bytes()
        bad = good[:-4] + b"XXXX"
        zbytes = _build_zip(
            ("sync_grid_100ms/part-000.parquet", bad),
            ("sync_grid_100ms/part-001.parquet", good),
            ("normalized_books/part-000.parquet", good),
            ("normalized_trades/part-000.parquet", good),
        )
        v = validate_all_parquet(zbytes)
        # Corrupt tail also breaks metadata parsing.
        assert v.parquet_magic_failed >= 1
        assert parquet_overall_status(v) == "FAIL"

    def test_missing_part_number_flags_sequence_gap(self):
        good = _make_valid_parquet_bytes()
        # Skip index 001 in sync_grid_100ms (0, 2 present -> gap at 1).
        zbytes = _build_zip(
            ("sync_grid_100ms/part-000.parquet", good),
            ("sync_grid_100ms/part-002.parquet", good),
            ("normalized_books/part-000.parquet", good),
            ("normalized_trades/part-000.parquet", good),
        )
        v = validate_all_parquet(zbytes)
        assert v.parquet_sequence_gaps >= 1
        assert v.per_dir["sync_grid_100ms"].missing_parts == [1]
        assert parquet_overall_status(v) == "FAIL"

    def test_duplicate_part_number_flagged(self):
        good = _make_valid_parquet_bytes()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # ZIP allows repeated entry names.
            zf.writestr("sync_grid_100ms/part-000.parquet", good)
            zf.writestr("sync_grid_100ms/part-000.parquet", good)
            zf.writestr("sync_grid_100ms/part-001.parquet", good)
            zf.writestr("normalized_books/part-000.parquet", good)
            zf.writestr("normalized_trades/part-000.parquet", good)
        v = validate_all_parquet(buf.getvalue())
        assert v.parquet_duplicate_parts >= 1
        assert 0 in v.per_dir["sync_grid_100ms"].duplicate_parts
        assert parquet_overall_status(v) == "FAIL"

    def test_below_min_size_fails(self):
        # Just the magic bytes twice - short enough to trip min-size.
        zbytes = _build_zip(
            ("sync_grid_100ms/part-000.parquet", b"PAR"),  # 3 bytes < 8
        )
        v = validate_all_parquet(zbytes)
        assert v.parquet_magic_failed >= 1
        assert v.parquet_metadata_failed >= 1
        assert parquet_overall_status(v) == "FAIL"

    def test_empty_zip_no_parquet_files_is_fail(self):
        zbytes = _build_zip(("manifest.json", b"{}"))
        v = validate_all_parquet(zbytes)
        assert v.parquet_files_total == 0
        assert parquet_overall_status(v) == "FAIL"

    def test_metadata_failure_isolated_from_magic_pass(self):
        """A file with correct PAR1 head+tail but no valid footer must
        still be flagged as a metadata failure (i.e. sampling based on
        magic alone would falsely PASS)."""
        bogus = PARQUET_MAGIC + b"\x00" * 40 + PARQUET_MAGIC
        zbytes = _build_zip(
            ("sync_grid_100ms/part-000.parquet", bogus),
            ("normalized_books/part-000.parquet", bogus),
            ("normalized_trades/part-000.parquet", bogus),
        )
        v = validate_all_parquet(zbytes)
        # Magic looks OK on all 3.
        assert v.parquet_magic_passed == 3
        # But metadata should fail everywhere.
        assert v.parquet_metadata_failed == 3
        assert parquet_overall_status(v) == "FAIL"

    def test_report_dict_shape_stable(self):
        good = _make_valid_parquet_bytes()
        zbytes = _build_zip(
            ("sync_grid_100ms/part-000.parquet", good),
            ("normalized_books/part-000.parquet", good),
            ("normalized_trades/part-000.parquet", good),
        )
        v = validate_all_parquet(zbytes)
        d = v.as_dict()
        for k in (
            "parquet_files_total",
            "parquet_magic_checked",
            "parquet_magic_passed",
            "parquet_magic_failed",
            "parquet_metadata_checked",
            "parquet_metadata_passed",
            "parquet_metadata_failed",
            "parquet_sequence_gaps",
            "parquet_duplicate_parts",
            "per_dir",
        ):
            assert k in d, f"missing key {k} in parquet result dict"
