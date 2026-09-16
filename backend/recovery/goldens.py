"""Loaders for CP24 / CP36 golden checkpoint outputs.

Every golden CSV is treated as a read-only source of truth. Callers
never write, mutate, or hard-code any numeric value from these
files. They are used as regression targets for the recovered
validator.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from . import GOLDENS_DIR

CP24_DIR: Path = GOLDENS_DIR / "CP24"
CP36_DIR: Path = GOLDENS_DIR / "CP36"


@dataclass
class GoldenRow:
    source_file: str  # relative name within goldens/
    row: dict[str, str]


def _read_csv(path: Path) -> Iterator[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            yield r


def read_golden_rows(paths: Iterable[Path]) -> Iterator[GoldenRow]:
    for p in paths:
        rel = p.relative_to(GOLDENS_DIR).as_posix()
        for row in _read_csv(p):
            yield GoldenRow(source_file=rel, row=row)


def list_cp24_csvs() -> list[Path]:
    return sorted(p for p in CP24_DIR.iterdir() if p.suffix == ".csv")


def list_cp36_csvs() -> list[Path]:
    return sorted(p for p in CP36_DIR.iterdir() if p.suffix == ".csv")


def load_simple_block_level() -> list[GoldenRow]:
    return list(
        read_golden_rows(
            [
                CP24_DIR / "simple_q80_q90_q95_block_level_24h.csv",
                CP36_DIR / "simple_block_level_new12.csv",
            ]
        )
    )


def load_composite_block_level() -> list[GoldenRow]:
    return list(
        read_golden_rows(
            [
                CP24_DIR / "composite_q80_q90_q95_block_level_24h.csv",
                CP36_DIR / "composite_block_level_new12.csv",
            ]
        )
    )


def load_horizon_profile_all36_q90() -> list[GoldenRow]:
    return list(
        read_golden_rows(
            [
                CP36_DIR / "simple_horizon_profile_all36_q90.csv",
                CP36_DIR / "composite_horizon_profile_all36_q90.csv",
            ]
        )
    )


def load_sensitivity_all36_30s() -> list[GoldenRow]:
    return list(
        read_golden_rows(
            [
                CP36_DIR / "simple_sensitivity_all36_30s.csv",
                CP36_DIR / "composite_sensitivity_all36_30s.csv",
            ]
        )
    )


def counts() -> dict[str, int]:
    """Return a compact summary of golden row availability."""
    return {
        "cp24_files": len(list_cp24_csvs()),
        "cp36_files": len(list_cp36_csvs()),
        "simple_block_rows": len(load_simple_block_level()),
        "composite_block_rows": len(load_composite_block_level()),
        "horizon_profile_all36_q90_rows": len(load_horizon_profile_all36_q90()),
        "sensitivity_all36_30s_rows": len(load_sensitivity_all36_30s()),
    }


__all__ = [
    "CP24_DIR",
    "CP36_DIR",
    "GoldenRow",
    "list_cp24_csvs",
    "list_cp36_csvs",
    "read_golden_rows",
    "load_simple_block_level",
    "load_composite_block_level",
    "load_horizon_profile_all36_q90",
    "load_sensitivity_all36_30s",
    "counts",
]
