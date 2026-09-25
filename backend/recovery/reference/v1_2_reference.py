"""SuperBot V1.2 — NEW36 Validation Protocol V2 — independent REFERENCE.

Clean-room implementation of the frozen chain
    V1_2_NEW36_VALIDATION_PROTOCOL_V2 + AMENDMENT 001..006
authored exclusively from the A006 Reference Input Pack.

Role literal: "REFERENCE" (V2 §5, A006 §5.2 / §6.2).

SAFETY PROPERTIES
- Importing this module performs no NEW36 access and no quantitative work.
- NEW36 quantitative input is touched only inside
  ``generate_reference_artifacts`` and only AFTER every pre-application
  gate (inventory, NNC-1, NNC-2, NNC-3, NNC-4, output-path freshness)
  has passed.
- No golden artifact, OLD36 quantitative artifact, Candidate source or
  Candidate output is read.
- No NEW36 session identifier is embedded here; the frozen inventory is
  read from backend/checkpoint_registry.py (V2 §11).

ENGINE ACCESS (A001 §A10)
backend/recovery/engine.py is loaded by path after its raw-byte SHA256 is
verified against the frozen ENGINE_SHA256. Only the symbols listed in
``ENGINE_SYMBOLS_USED`` are taken from it; all are members of the A001
allowlist. No high-level reconstruction routine is used.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import math
import re
import sqlite3
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

import numpy as np
import pandas as pd

# Guarded module-load import of pyarrow.parquet (A006 §4.5.2 _check_metadata:
# availability is decided at module load; any exception => unavailable).
try:  # pragma: no cover - environment dependent
    import pyarrow.parquet as _pq  # type: ignore

    _PYARROW_IMPORT_ERROR: BaseException | None = None
except Exception as _exc:  # pragma: no cover - environment dependent
    _pq = None
    _PYARROW_IMPORT_ERROR = _exc


# ===========================================================================
# 1. Errors
# ===========================================================================


class ReferenceGenerationError(RuntimeError):
    """Base class for every fail-closed Reference condition."""


class EngineIdentityError(ReferenceGenerationError):
    """engine.py missing or its raw-byte SHA256 differs from the frozen value."""


class PreApplicationGateError(ReferenceGenerationError):
    """A pre-application gate failed; NEW36 was not accessed."""


class InventoryError(PreApplicationGateError):
    """Frozen NEW36 inventory could not be established."""


class ProvenanceError(PreApplicationGateError):
    """NNC-1 / NNC-2 Reference provenance could not be established."""


class EnvironmentIdentityError(PreApplicationGateError):
    """NNC-3 runtime environment identity failure."""


class SessionUniquenessError(PreApplicationGateError):
    """NNC-4 session_id uniqueness gate failure (or unverifiable)."""


class OutputPathError(PreApplicationGateError):
    """An output artifact path already exists (no partial/overwritten output)."""


class ManifestValidationError(ReferenceGenerationError):
    """Manifest JSON invalid (including NNC-5 duplicate member names)."""


class ArtifactSerializationError(ReferenceGenerationError):
    """CSV/manifest canonical serialization contract violated."""


class BlockDataUnavailableError(ReferenceGenerationError):
    """Loader Case B (no entry selected) or Case D (zero combined rows)."""


class StructuralApplicationFailure(ReferenceGenerationError):
    """Structural application failure after NEW36 application start."""

    def __init__(self, message: str, *, session_id: str | None = None,
                 asset: str | None = None, classification: str | None = None):
        super().__init__(message)
        self.session_id = session_id
        self.asset = asset
        self.classification = classification


# ===========================================================================
# 2. Frozen identities and literals
# ===========================================================================

IMPLEMENTATION_ROLE = "REFERENCE"
PROTOCOL_VERSION = "V1_2_NEW36_VALIDATION_PROTOCOL_V2"

PROTOCOL_SHA256 = "5ed8a8b12726d395d25262dc3e4073250be3de94f3d62e64e99167921380ef95"
CANDIDATE_SPEC_SHA256 = "aed67b5ec9ae1b710ec3d96e1fcc2e15bd9c30fdd60e80cc68553d8fc3b597ca"
ENGINE_SHA256 = "abedee399c272eff858b109fb448cbda3ad3a49e40be4ba3f486650e3151a3e8"
COLLECTOR_SHA256 = "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3"

NEW36_INVENTORY_ID = "NEW36_12SESSIONS_24BLOCKS_3456ROWS_V1"
FINGERPRINT_ENCODING_DEFINITION = (
    "sorted accepted grid_pos values encoded as signed little-endian int64 raw "
    "bytes, concatenated with no delimiter, then SHA256 lowercase hexadecimal; "
    "empty accepted set hashes empty bytes"
)
QUANTILE_METHOD = "TYPE7_LINEAR"
FROZEN_ANALYSIS_ENGINE_STATE = "NOT_CONFIGURED"
FROZEN_ANALYSIS_ENGINE_ACCEPTS_INPUT = False
MANIFEST_GRID_MS = 100

EXPECTED_SESSION_COUNT = 12
ASSETS: tuple[str, ...] = ("BTC", "ETH")
AXES: tuple[str, ...] = ("HZ", "HG", "HC", "HB")

# V2 Appendix B HZ feature identifiers (normative verbatim per A002 §B2/§B3).
HZ_FEATURES: tuple[str, ...] = (
    "bitget_ofi",
    "bitget_trade_flow",
    "depth_imbalance_l1",
    "depth_imbalance_l5",
    "external_ofi",
    "external_trade_flow",
    "fair_accel_100ms",
    "fair_gap_reversion",
    "leader_gap_100ms",
    "leader_gap_200ms",
    "leader_gap_500ms",
    "leader_gap_1000ms",
)
HG_FAMILY = "depthL1_extOFI"
HC_FAMILY = "gap_depth_extOFI"
HB_FAMILIES: tuple[str, ...] = (
    "depth_imbalance_l1",
    "gap_depthL1",
    "gap_localOFI",
    "bitget_ofi",
    "depthL1_extOFI",
    "gap_depth_extOFI",
)
FEATURE_SETS_PER_AXIS: dict[str, tuple[str, ...]] = {
    "HZ": HZ_FEATURES,
    "HG": (HG_FAMILY,),
    "HC": (HC_FAMILY,),
    "HB": HB_FAMILIES,
}
VARIANT_SETS_PER_AXIS: dict[str, tuple[str, ...]] = {
    "HZ": ("T0", "TZ"),
    "HG": ("G0", "G1"),
    "HC": ("C0", "C1"),
    "HB": ("B0", "B1"),
}
QUANTILES_V2: tuple[float, ...] = (0.80, 0.90, 0.95)
HB_QUANTILE = 0.90
QUANTILES_PER_AXIS: dict[str, tuple[float, ...]] = {
    "HZ": QUANTILES_V2,
    "HG": QUANTILES_V2,
    "HC": QUANTILES_V2,
    "HB": (HB_QUANTILE,),
}
DIAGNOSTIC_HORIZONS_MS: tuple[int, ...] = (1000, 5000, 30000)
HORIZONS_PER_AXIS: dict[str, tuple[int, ...]] = {
    "HZ": (),
    "HG": DIAGNOSTIC_HORIZONS_MS,
    "HC": DIAGNOSTIC_HORIZONS_MS,
    "HB": DIAGNOSTIC_HORIZONS_MS,
}
# Frozen inventory counts (V2 §3, A002 §B2/§B6).
EXPECTED_ROW_COUNTS_PER_AXIS: dict[str, int] = {"HZ": 1728, "HG": 432, "HC": 432, "HB": 864}
EXPECTED_TOTAL_ROW_COUNT = 3456

HZ_CLASS_ZERO_FREE = "ZERO_FREE_CONTROL"
HZ_CLASS_DISCRIMINATING = "HZ_DISCRIMINATING"
HZ_CLASS_NONDISCRIMINATING = "HZ_NONDISCRIMINATING_EQUAL_THRESHOLD"

# Frozen 25-field row schema (V2 §4, A004 §8.6).
ROW_FIELDS: tuple[str, ...] = (
    "protocol_version",
    "session_id",
    "asset",
    "axis",
    "feature_family",
    "variant_id",
    "quantile",
    "horizon_ms",
    "threshold_domain_finite_n",
    "threshold_domain_nonzero_n",
    "zero_n",
    "zero_fraction",
    "nonzero_unique_value_n",
    "hz_discrimination_class",
    "gate_zero_n",
    "calculated_threshold",
    "spacing_steps",
    "pre_overlap_n",
    "exact_spacing_pair_n",
    "accepted_n",
    "overlap_dropped_n",
    "accepted_positions_sha256",
    "mean_signed_bps",
    "hit_rate",
    "mean_abs_move",
)
STRING_FIELDS = frozenset({
    "protocol_version", "session_id", "asset", "axis", "feature_family",
    "variant_id", "hz_discrimination_class", "accepted_positions_sha256",
})
INTEGER_FIELDS = frozenset({
    "horizon_ms", "threshold_domain_finite_n", "threshold_domain_nonzero_n",
    "zero_n", "nonzero_unique_value_n", "gate_zero_n", "spacing_steps",
    "pre_overlap_n", "exact_spacing_pair_n", "accepted_n", "overlap_dropped_n",
})
FLOAT_FIELDS = frozenset({
    "quantile", "zero_fraction", "calculated_threshold", "mean_signed_bps",
    "hit_rate", "mean_abs_move",
})
CANONICAL_KEY_FIELDS: tuple[str, ...] = (
    "session_id", "asset", "axis", "feature_family", "variant_id", "quantile", "horizon_ms",
)

_IDENTITY_FIELDS = ("protocol_version", "session_id", "asset", "axis",
                    "feature_family", "variant_id", "quantile")
_EVENT_COMMON = ("horizon_ms", "calculated_threshold", "spacing_steps", "pre_overlap_n",
                 "accepted_n", "overlap_dropped_n", "accepted_positions_sha256",
                 "mean_signed_bps", "hit_rate", "mean_abs_move")

# A001 §A7 per-axis/variant COMPUTED field sets.
COMPUTED_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("HZ", "T0"): frozenset(_IDENTITY_FIELDS + (
        "threshold_domain_finite_n", "threshold_domain_nonzero_n", "zero_n",
        "zero_fraction", "nonzero_unique_value_n", "hz_discrimination_class",
        "calculated_threshold")),
    ("HG", "G0"): frozenset(_IDENTITY_FIELDS + _EVENT_COMMON),
    ("HG", "G1"): frozenset(_IDENTITY_FIELDS + _EVENT_COMMON + ("gate_zero_n",)),
    ("HC", "C0"): frozenset(_IDENTITY_FIELDS + _EVENT_COMMON),
    ("HB", "B0"): frozenset(_IDENTITY_FIELDS + _EVENT_COMMON + ("exact_spacing_pair_n",)),
}
COMPUTED_FIELDS[("HZ", "TZ")] = COMPUTED_FIELDS[("HZ", "T0")]
COMPUTED_FIELDS[("HC", "C1")] = COMPUTED_FIELDS[("HC", "C0")]
COMPUTED_FIELDS[("HB", "B1")] = COMPUTED_FIELDS[("HB", "B0")]

# COMPUTED fields that may legitimately evaluate to NULL.
NULLABLE_COMPUTED_FIELDS = frozenset({
    "zero_fraction",          # finite_n == 0 (A001 §A2)
    "calculated_threshold",   # < 2 domain values (A001 §A8)
    "mean_signed_bps",        # accepted_n == 0 (A004 §7.5)
    "hit_rate",               # accepted_n == 0 (A004 §7.5)
    "mean_abs_move",          # empty mean_abs_move domain (A003 §C1)
})

# V2 §5 required manifest keys.
REQUIRED_MANIFEST_KEYS: tuple[str, ...] = (
    "protocol_version", "implementation_role", "runtime_source_commit",
    "source_sha256", "csv_sha256", "csv_size_bytes",
    "manifest_precomparison_sha256", "session_ids", "assets", "axes",
    "feature_sets_per_axis", "variant_sets_per_axis", "quantiles_per_axis",
    "horizons_per_axis", "expected_row_counts_per_axis",
    "actual_row_counts_per_axis", "expected_total_row_count",
    "actual_total_row_count", "collector_sha256", "engine_sha256",
    "candidate_spec_sha256", "protocol_sha256", "new36_inventory_id",
    "golden_artifacts_read", "old36_quantitative_data_read",
    "frozen_analysis_engine_state", "frozen_analysis_engine_accepts_input",
    "fingerprint_encoding_definition", "quantile_method", "grid_ms",
)

# NNC-2 exact Reference source paths (A006 §6.3).
REFERENCE_SOURCE_PATHS: tuple[str, ...] = (
    "backend/recovery/reference/v1_2_reference.py",
    "backend/recovery/reference/tests/test_v1_2_reference.py",
    "backend/recovery/reference/REFERENCE_INDEPENDENCE_ATTESTATION.txt",
)

# NNC-3 (A006 §7).
NORMATIVE_PYARROW_VERSION = "17.0.0"
REQUIREMENTS_RELATIVE_PATH = "backend/requirements.txt"

# Grid / composite column names. Simple-feature columns come from FEATURE_MAP.
COL_MID = "bitget_mid"
COL_GAP = "bitget_gap_to_fair_bps"
COL_DI_L1 = "bitget_depth_imbalance_l1"
COL_EXT_OFI = "external_ofi_consensus_l1"
COL_FAIR_OFI_ALIGNMENT = "bitget_fair_ofi_alignment"

_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


# ===========================================================================
# 3. Engine primitives (A001 §A10 allowlist, loaded after SHA verification)
# ===========================================================================

A001_ENGINE_ALLOWLIST: frozenset[str] = frozenset({
    "GRID_MS", "FEATURE_MAP", "SIMPLE_FEATURES", "QUANTILES",
    "HORIZONS_SIMPLE_MS", "HORIZONS_COMPOSITE_MS", "build_canonical_grid",
    "_quality_admissible_mask", "_rank_signed_uniform", "_quantile_type7",
    "_build_forward_return_array", "_greedy_overlap_filter",
})
ENGINE_SYMBOLS_USED: tuple[str, ...] = (
    "GRID_MS",
    "FEATURE_MAP",
    "SIMPLE_FEATURES",
    "QUANTILES",
    "build_canonical_grid",
    "_quality_admissible_mask",
    "_rank_signed_uniform",
    "_quantile_type7",
    "_build_forward_return_array",
    "_greedy_overlap_filter",
)
assert set(ENGINE_SYMBOLS_USED) <= A001_ENGINE_ALLOWLIST

_THIS_FILE = Path(__file__).resolve()
DEFAULT_REPO_ROOT = _THIS_FILE.parents[3]
ENGINE_PATH = _THIS_FILE.parents[1] / "engine.py"
CHECKPOINT_REGISTRY_PATH = _THIS_FILE.parents[2] / "checkpoint_registry.py"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_allowlisted_engine_symbols() -> dict[str, Any]:
    try:
        raw = ENGINE_PATH.read_bytes()
    except OSError as exc:
        raise EngineIdentityError(f"engine.py unreadable at {ENGINE_PATH}: {exc}") from exc
    digest = sha256_hex(raw)
    if digest != ENGINE_SHA256:
        raise EngineIdentityError(
            f"engine.py SHA256 drift: expected {ENGINE_SHA256}, found {digest}")
    module_name = "_superbot_v1_2_reference_frozen_engine"
    spec = importlib.util.spec_from_file_location(module_name, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise EngineIdentityError("engine.py import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        exec(compile(raw, str(ENGINE_PATH), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return {name: getattr(module, name) for name in ENGINE_SYMBOLS_USED}


_ENGINE = _load_allowlisted_engine_symbols()
GRID_MS: int = _ENGINE["GRID_MS"]
FEATURE_MAP: Mapping[str, str] = dict(_ENGINE["FEATURE_MAP"])
SIMPLE_FEATURES: tuple[str, ...] = tuple(_ENGINE["SIMPLE_FEATURES"])
ENGINE_QUANTILES: tuple[float, ...] = tuple(_ENGINE["QUANTILES"])
build_canonical_grid = _ENGINE["build_canonical_grid"]
_quality_admissible_mask = _ENGINE["_quality_admissible_mask"]
_rank_signed_uniform = _ENGINE["_rank_signed_uniform"]
_quantile_type7 = _ENGINE["_quantile_type7"]
_build_forward_return_array = _ENGINE["_build_forward_return_array"]
_greedy_overlap_filter = _ENGINE["_greedy_overlap_filter"]
del _ENGINE

# Consistency guards between the frozen protocol text and inherited primitives.
if GRID_MS != MANIFEST_GRID_MS:
    raise EngineIdentityError("GRID_MS disagrees with V2 grid_ms")
if SIMPLE_FEATURES != HZ_FEATURES:
    raise EngineIdentityError("engine SIMPLE_FEATURES disagree with V2 Appendix-B HZ features")
if ENGINE_QUANTILES != QUANTILES_V2:
    raise EngineIdentityError("engine QUANTILES disagree with V2 QUANTILES")
if (FEATURE_MAP["fair_gap_reversion"] != COL_GAP
        or FEATURE_MAP["depth_imbalance_l1"] != COL_DI_L1
        or FEATURE_MAP["external_ofi"] != COL_EXT_OFI):
    raise EngineIdentityError("FEATURE_MAP disagrees with frozen composite column names")


# ===========================================================================
# 4. Elementary computations
# ===========================================================================


def spacing_steps_for(horizon_ms: int) -> int:
    """A13: k = horizon_ms // 100; spacing_steps = max(10, k)."""
    return max(10, int(horizon_ms) // GRID_MS)


def accepted_positions_fingerprint(positions: Iterable[int]) -> str:
    """V2 §4 / A002 §B4: sorted positions as signed little-endian int64 bytes."""
    ordered = sorted(int(p) for p in positions)
    payload = b"".join(struct.pack("<q", p) for p in ordered)
    return sha256_hex(payload)


EMPTY_SHA256 = sha256_hex(b"")


def exact_spacing_pair_count(sorted_positions: Sequence[int], spacing: int) -> int:
    """A001 §A6: consecutive pairs only (not all unordered pairs)."""
    pos = [int(p) for p in sorted_positions]
    return sum(1 for i in range(len(pos) - 1) if pos[i + 1] - pos[i] == spacing)


def overlap_filter_b0(sorted_positions: np.ndarray, spacing: int) -> np.ndarray:
    """B0: greedy earliest-first, accept iff pos - last_accepted >= spacing."""
    arr = np.asarray(sorted_positions, dtype=np.int64)
    return np.asarray(_greedy_overlap_filter(arr, int(spacing)), dtype=bool)


def overlap_filter_b1(sorted_positions: np.ndarray, spacing: int) -> np.ndarray:
    """B1 (diagnostic only): first accepted; then accept iff pos - last > spacing."""
    arr = np.asarray(sorted_positions, dtype=np.int64)
    accepted = np.zeros(len(arr), dtype=bool)
    last: int | None = None
    for i, pos in enumerate(arr.tolist()):
        if last is None or pos - last > spacing:
            accepted[i] = True
            last = pos
    return accepted


def type7_threshold(values: np.ndarray, q: float) -> float | None:
    """Type-7 quantile via the permitted primitive; None if < 2 values."""
    out = _quantile_type7(np.asarray(values, dtype=float), float(q))
    return None if out is None else float(out)


def _thresholds_equal(a: float | None, b: float | None) -> bool:
    """A001 §A2 exact Python-value equality with NULL == NULL."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b


def hz_discrimination_class(zero_n: int, t0: float | None, tz: float | None) -> str:
    if zero_n == 0:
        return HZ_CLASS_ZERO_FREE
    if not _thresholds_equal(t0, tz):
        return HZ_CLASS_DISCRIMINATING
    return HZ_CLASS_NONDISCRIMINATING


def _mean(values: np.ndarray) -> float:
    return math.fsum(float(v) for v in values) / len(values)


# ===========================================================================
# 5. Block computation (one canonical grid = one (session, asset) block)
# ===========================================================================


def _empty_row() -> dict[str, Any]:
    return {name: None for name in ROW_FIELDS}


class _Block:
    """Pre-extracted arrays for one canonical grid."""

    def __init__(self, grid: pd.DataFrame):
        if "grid_pos" not in grid.columns:
            raise ArtifactSerializationError("grid lacks grid_pos; build_canonical_grid not applied")
        self.grid = grid
        self.n = len(grid)
        self.grid_pos = grid["grid_pos"].to_numpy(dtype=np.int64)
        if self.n and not np.all(np.diff(self.grid_pos) > 0):
            raise ArtifactSerializationError("grid_pos not strictly ascending")
        self.quality_series = _quality_admissible_mask(grid)
        self.quality = np.asarray(self.quality_series, dtype=bool)
        # A004 R-01: canonical_mid := grid["bitget_mid"].to_numpy(dtype=float)
        mid = grid[COL_MID].to_numpy(dtype=float, na_value=np.nan)
        self.fwd: dict[int, np.ndarray] = {
            h: np.asarray(_build_forward_return_array(mid, h // GRID_MS), dtype=float)
            for h in DIAGNOSTIC_HORIZONS_MS
        }
        self._cols: dict[str, np.ndarray] = {}
        self._z: dict[str, np.ndarray] = {}

    def col(self, name: str) -> np.ndarray:
        """Raw column as float; absent column => all-NaN (A004 §5.6)."""
        if name not in self._cols:
            if name in self.grid.columns:
                arr = self.grid[name].to_numpy(dtype=float, na_value=np.nan)
            else:
                arr = np.full(self.n, np.nan, dtype=float)
            self._cols[name] = np.asarray(arr, dtype=float)
        return self._cols[name]

    def z(self, name: str) -> np.ndarray:
        """Frozen V1.1 per-block rank-signed-uniform component (A001 §A9)."""
        if name not in self._z:
            series = pd.Series(self.col(name), index=self.grid.index, dtype=float)
            out = _rank_signed_uniform(series, self.quality_series)
            self._z[name] = np.asarray(out, dtype=float)
        return self._z[name]


def _finite(a: np.ndarray) -> np.ndarray:
    return np.isfinite(a)


def _events(block: _Block, mask: np.ndarray, direction: np.ndarray, horizon_ms: int,
            filt: str) -> dict[str, Any]:
    """Pre-overlap -> overlap filter -> accepted-event metrics."""
    spacing = spacing_steps_for(horizon_ms)
    idx = np.flatnonzero(mask)
    pre_pos = block.grid_pos[idx]
    if filt == "B0":
        acc = overlap_filter_b0(pre_pos, spacing)
    elif filt == "B1":
        acc = overlap_filter_b1(pre_pos, spacing)
    else:  # pragma: no cover
        raise ValueError(filt)
    acc_idx = idx[acc]
    acc_pos = block.grid_pos[acc_idx]
    pre_n = int(len(idx))
    acc_n = int(len(acc_idx))
    out: dict[str, Any] = {
        "spacing_steps": spacing,
        "pre_overlap_n": pre_n,
        "accepted_n": acc_n,
        "overlap_dropped_n": pre_n - acc_n,
        "accepted_positions_sha256": accepted_positions_fingerprint(acc_pos.tolist()),
        "_pre_positions": pre_pos.tolist(),
        "_accepted_positions": acc_pos.tolist(),
    }
    if acc_n == 0:
        out["mean_signed_bps"] = None
        out["hit_rate"] = None
    else:
        signed = direction[acc_idx] * block.fwd[horizon_ms][acc_idx]
        out["mean_signed_bps"] = _mean(signed)
        out["hit_rate"] = int(np.count_nonzero(signed > 0.0)) / acc_n
    return out


def _mean_abs_move(block: _Block, horizon_ms: int) -> float | None:
    """A003 §C1: mean |fwd| over quality AND finite(fwd); feature-independent."""
    fwd = block.fwd[horizon_ms]
    dom = block.quality & _finite(fwd)
    if not dom.any():
        return None
    return _mean(np.abs(fwd[dom]))


def _hz_rows(block: _Block, session_id: str, asset: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in HZ_FEATURES:
        s = block.col(FEATURE_MAP[feature])
        t0_dom = block.quality & _finite(s)
        tz_dom = t0_dom & (s != 0.0)
        finite_n = int(np.count_nonzero(t0_dom))
        nonzero_n = int(np.count_nonzero(tz_dom))
        zero_n = finite_n - nonzero_n
        zero_fraction = None if finite_n == 0 else zero_n / finite_n
        nonzero_unique = len({float(v) for v in np.abs(s[tz_dom]).tolist()})
        for q in QUANTILES_V2:
            t0_thr = type7_threshold(np.abs(s[t0_dom]), q)
            tz_true = type7_threshold(np.abs(s[tz_dom]), q)
            # A004 R-02 / A005 R1.1: for fair_gap_reversion the actual threshold
            # domain includes zeros, so the emitted TZ threshold is T0_threshold;
            # the zero-excluding quantile is the A005 R1.3 diagnostic operand only.
            tz_emitted = t0_thr if feature == "fair_gap_reversion" else tz_true
            cls = hz_discrimination_class(zero_n, t0_thr, tz_true)
            for variant, thr in (("T0", t0_thr), ("TZ", tz_emitted)):
                row = _empty_row()
                row.update({
                    "protocol_version": PROTOCOL_VERSION, "session_id": session_id,
                    "asset": asset, "axis": "HZ", "feature_family": feature,
                    "variant_id": variant, "quantile": q,
                    "threshold_domain_finite_n": finite_n,
                    "threshold_domain_nonzero_n": nonzero_n,
                    "zero_n": zero_n, "zero_fraction": zero_fraction,
                    "nonzero_unique_value_n": nonzero_unique,
                    "hz_discrimination_class": cls,
                    "calculated_threshold": thr,
                })
                rows.append(row)
    return rows


# ---- family pre-overlap definitions ------------------------------------------------


def _mask_simple_t0(block: _Block, column: str, q: float, fwd: np.ndarray):
    """HB bitget_ofi / depth_imbalance_l1: frozen baseline T0 domain (A001 §A5)."""
    s = block.col(column)
    dom = block.quality & _finite(s)
    thr = type7_threshold(np.abs(s[dom]), q)
    if thr is None:
        return thr, np.zeros(block.n, dtype=bool), np.sign(s)
    with np.errstate(invalid="ignore"):
        mask = dom & (s != 0.0) & (np.abs(s) >= thr) & _finite(fwd)
    return thr, mask, np.sign(s)


def _derived_d(block: _Block) -> tuple[np.ndarray, np.ndarray]:
    z_di = block.z(COL_DI_L1)
    z_ext = block.z(COL_EXT_OFI)
    both = _finite(z_di) & _finite(z_ext)
    d = np.full(block.n, np.nan, dtype=float)
    d[both] = (z_di[both] + z_ext[both]) / 2.0
    return d, both


def _mask_g0(block: _Block, q: float, fwd: np.ndarray):
    """G0 / HB depthL1_extOFI (A001 §A3, §A5)."""
    d, both = _derived_d(block)
    derived = block.quality & both
    thr = type7_threshold(np.abs(d[derived]), q)
    if thr is None:
        return thr, np.zeros(block.n, dtype=bool), np.sign(d)
    with np.errstate(invalid="ignore"):
        mask = derived & (d != 0.0) & (np.abs(d) >= thr) & _finite(fwd)
    return thr, mask, np.sign(d)


def _tz_threshold(block: _Block, column: str, q: float) -> float | None:
    s = block.col(column)
    dom = block.quality & _finite(s) & (s != 0.0)
    return type7_threshold(np.abs(s[dom]), q)


def _gate_zero_n(block: _Block) -> int:
    """A003 §C2."""
    di = block.col(COL_DI_L1)
    return int(np.count_nonzero(block.quality & _finite(di) & (di == 0.0)))


def _mask_g1(block: _Block, q: float, fwd: np.ndarray):
    """G1 candidate (spec §4): TZ depth_imbalance_l1 threshold, raw signs."""
    di = block.col(COL_DI_L1)
    ext = block.col(COL_EXT_OFI)
    thr = _tz_threshold(block, COL_DI_L1, q)
    if thr is None:
        return thr, np.zeros(block.n, dtype=bool), np.sign(di)
    with np.errstate(invalid="ignore"):
        mask = (block.quality & _finite(di) & (di != 0.0) & (np.abs(di) >= thr)
                & _finite(ext) & (ext != 0.0) & (np.sign(ext) == np.sign(di))
                & _finite(fwd))
    return thr, mask, np.sign(di)


def _gap_threshold(block: _Block, q: float) -> tuple[float | None, np.ndarray, np.ndarray]:
    """Frozen fair-gap threshold: domain quality AND finite(gap), zeros included."""
    gap = block.col(COL_GAP)
    dom = block.quality & _finite(gap)
    return type7_threshold(np.abs(gap[dom]), q), gap, dom


def _mask_c0(block: _Block, q: float, fwd: np.ndarray):
    """C0 / HB gap_depth_extOFI (A001 §A4, §A5)."""
    thr, gap, dom = _gap_threshold(block, q)
    direction = -np.sign(gap)
    if thr is None:
        return thr, np.zeros(block.n, dtype=bool), direction
    d_ext, _ = _derived_d(block)
    with np.errstate(invalid="ignore"):
        mask = (dom & _finite(d_ext) & (gap != 0.0) & (np.abs(gap) >= thr)
                & (d_ext != 0.0) & (np.sign(d_ext) == -np.sign(gap)) & _finite(fwd))
    return thr, mask, direction


def _mask_c1(block: _Block, q: float, fwd: np.ndarray):
    """C1 candidate (spec §5): frozen gap threshold, raw di_l1/ext_ofi signs."""
    thr, gap, dom = _gap_threshold(block, q)
    direction = -np.sign(gap)
    if thr is None:
        return thr, np.zeros(block.n, dtype=bool), direction
    di = block.col(COL_DI_L1)
    ext = block.col(COL_EXT_OFI)
    with np.errstate(invalid="ignore"):
        mask = (dom & (gap != 0.0) & (np.abs(gap) >= thr)
                & _finite(di) & (di != 0.0) & (np.sign(di) == -np.sign(gap))
                & _finite(ext) & (ext != 0.0) & (np.sign(ext) == -np.sign(gap))
                & _finite(fwd))
    return thr, mask, direction


def _mask_gap_depth_l1(block: _Block, q: float, fwd: np.ndarray):
    thr, gap, dom = _gap_threshold(block, q)
    direction = -np.sign(gap)
    if thr is None:
        return thr, np.zeros(block.n, dtype=bool), direction
    di = block.col(COL_DI_L1)
    with np.errstate(invalid="ignore"):
        mask = (dom & _finite(di) & (gap != 0.0) & (np.abs(gap) >= thr)
                & (di != 0.0) & (np.sign(di) == -np.sign(gap)) & _finite(fwd))
    return thr, mask, direction


def _mask_gap_local_ofi(block: _Block, q: float, fwd: np.ndarray):
    thr, gap, dom = _gap_threshold(block, q)
    direction = -np.sign(gap)
    if thr is None:
        return thr, np.zeros(block.n, dtype=bool), direction
    align = block.col(COL_FAIR_OFI_ALIGNMENT)
    with np.errstate(invalid="ignore"):
        mask = (dom & _finite(align) & (gap != 0.0) & (np.abs(gap) >= thr)
                & (align == 1.0) & _finite(fwd))
    return thr, mask, direction


def _hb_mask(block: _Block, family: str, fwd: np.ndarray):
    q = HB_QUANTILE
    if family == "depth_imbalance_l1":
        return _mask_simple_t0(block, FEATURE_MAP["depth_imbalance_l1"], q, fwd)
    if family == "bitget_ofi":
        return _mask_simple_t0(block, FEATURE_MAP["bitget_ofi"], q, fwd)
    if family == "gap_depthL1":
        return _mask_gap_depth_l1(block, q, fwd)
    if family == "gap_localOFI":
        return _mask_gap_local_ofi(block, q, fwd)
    if family == "depthL1_extOFI":
        return _mask_g0(block, q, fwd)
    if family == "gap_depth_extOFI":
        return _mask_c0(block, q, fwd)
    raise ValueError(family)  # pragma: no cover


def _event_row(session_id: str, asset: str, axis: str, feature: str, variant: str,
               q: float, horizon_ms: int, thr: float | None, ev: dict[str, Any],
               mean_abs_move: float | None) -> dict[str, Any]:
    row = _empty_row()
    row.update({
        "protocol_version": PROTOCOL_VERSION, "session_id": session_id,
        "asset": asset, "axis": axis, "feature_family": feature,
        "variant_id": variant, "quantile": q, "horizon_ms": horizon_ms,
        "calculated_threshold": thr,
        "spacing_steps": ev["spacing_steps"],
        "pre_overlap_n": ev["pre_overlap_n"],
        "accepted_n": ev["accepted_n"],
        "overlap_dropped_n": ev["overlap_dropped_n"],
        "accepted_positions_sha256": ev["accepted_positions_sha256"],
        "mean_signed_bps": ev["mean_signed_bps"],
        "hit_rate": ev["hit_rate"],
        "mean_abs_move": mean_abs_move,
    })
    return row


def compute_block_rows(session_id: str, asset: str, grid: pd.DataFrame) -> list[dict[str, Any]]:
    """All 144 rows (HZ 72, HG 18, HC 18, HB 36) of one canonical-grid block."""
    block = _Block(grid)
    rows = _hz_rows(block, session_id, asset)
    gate_zero = _gate_zero_n(block)
    for h in DIAGNOSTIC_HORIZONS_MS:
        fwd = block.fwd[h]
        mam = _mean_abs_move(block, h)
        for q in QUANTILES_V2:
            thr, mask, direction = _mask_g0(block, q, fwd)
            rows.append(_event_row(session_id, asset, "HG", HG_FAMILY, "G0", q, h, thr,
                                   _events(block, mask, direction, h, "B0"), mam))
            thr, mask, direction = _mask_g1(block, q, fwd)
            row = _event_row(session_id, asset, "HG", HG_FAMILY, "G1", q, h, thr,
                             _events(block, mask, direction, h, "B0"), mam)
            row["gate_zero_n"] = gate_zero
            rows.append(row)
            thr, mask, direction = _mask_c0(block, q, fwd)
            rows.append(_event_row(session_id, asset, "HC", HC_FAMILY, "C0", q, h, thr,
                                   _events(block, mask, direction, h, "B0"), mam))
            thr, mask, direction = _mask_c1(block, q, fwd)
            rows.append(_event_row(session_id, asset, "HC", HC_FAMILY, "C1", q, h, thr,
                                   _events(block, mask, direction, h, "B0"), mam))
        for family in HB_FAMILIES:
            thr, mask, direction = _hb_mask(block, family, fwd)
            for variant in ("B0", "B1"):
                ev = _events(block, mask, direction, h, variant)
                row = _event_row(session_id, asset, "HB", family, variant, HB_QUANTILE, h,
                                 thr, ev, mam)
                row["exact_spacing_pair_n"] = exact_spacing_pair_count(
                    ev["_pre_positions"], ev["spacing_steps"])
                rows.append(row)
    return rows


# ===========================================================================
# 6. Row validation, canonical ordering and CSV serialization
# ===========================================================================


def canonical_sort_key(row: Mapping[str, Any], session_order: Sequence[str]) -> tuple:
    axis = row["axis"]
    feats = FEATURE_SETS_PER_AXIS[axis]
    variants = VARIANT_SETS_PER_AXIS[axis]
    quants = QUANTILES_PER_AXIS[axis]
    hz = row["horizon_ms"]
    return (
        list(session_order).index(row["session_id"]),
        ASSETS.index(row["asset"]),
        AXES.index(axis),
        feats.index(row["feature_family"]),
        variants.index(row["variant_id"]),
        quants.index(row["quantile"]),
        -1 if hz is None else DIAGNOSTIC_HORIZONS_MS.index(hz),
    )


def canonical_row_key(row: Mapping[str, Any]) -> tuple:
    return tuple(row[k] for k in CANONICAL_KEY_FIELDS)


def validate_row(row: Mapping[str, Any]) -> None:
    """Types, finiteness and the A001 §A7 NULL/COMPUTED matrix (fail closed)."""
    if set(row.keys()) != set(ROW_FIELDS):
        raise ArtifactSerializationError("row field set differs from frozen 25-field schema")
    axis, variant = row.get("axis"), row.get("variant_id")
    computed = COMPUTED_FIELDS.get((axis, variant))
    if computed is None:
        raise ArtifactSerializationError(f"unknown axis/variant {axis}/{variant}")
    if row["protocol_version"] != PROTOCOL_VERSION:
        raise ArtifactSerializationError("protocol_version literal mismatch")
    for name in ROW_FIELDS:
        value = row[name]
        if name not in computed:
            if value is not None:
                raise ArtifactSerializationError(f"{axis}/{variant}: {name} must be NULL")
            continue
        if value is None:
            if name not in NULLABLE_COMPUTED_FIELDS:
                raise ArtifactSerializationError(f"{axis}/{variant}: {name} must be computed")
            continue
        if name in STRING_FIELDS:
            if not isinstance(value, str):
                raise ArtifactSerializationError(f"{name} must be str")
        elif name in INTEGER_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
                raise ArtifactSerializationError(f"{name} must be int")
            if int(value) < 0:
                raise ArtifactSerializationError(f"{name} must be non-negative")
        elif name in FLOAT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float, np.floating, np.integer)):
                raise ArtifactSerializationError(f"{name} must be float")
            if not math.isfinite(float(value)):
                raise ArtifactSerializationError(f"{name} non-finite value forbidden")
    if row["accepted_n"] is not None:
        if row["pre_overlap_n"] != row["accepted_n"] + row["overlap_dropped_n"]:
            raise ArtifactSerializationError("pre_overlap_n != accepted_n + overlap_dropped_n")
        if row["accepted_n"] == 0 and (row["mean_signed_bps"] is not None
                                       or row["hit_rate"] is not None):
            raise ArtifactSerializationError("accepted_n == 0 requires NULL event metrics")
    if row.get("calculated_threshold") is None and row.get("pre_overlap_n") not in (None, 0):
        raise ArtifactSerializationError("threshold NULL implies N == 0")


def format_field(name: str, value: Any) -> str:
    """A004 §8.1/§8.2: NULL -> '', int -> base-10, float -> repr(float(value))."""
    if value is None:
        return ""
    if name in STRING_FIELDS:
        if not isinstance(value, str):
            raise ArtifactSerializationError(f"{name} must be str")
        return value
    if name in INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
            raise ArtifactSerializationError(f"{name} must be int")
        return str(int(value))
    if name in FLOAT_FIELDS:
        if isinstance(value, bool):
            raise ArtifactSerializationError(f"{name} must be float")
        f = float(value)
        if not math.isfinite(f):
            raise ArtifactSerializationError(f"{name} non-finite value forbidden")
        return repr(f)
    raise ArtifactSerializationError(f"unknown field {name}")  # pragma: no cover


def serialize_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """UTF-8 (no BOM), one header, LF, csv.writer QUOTE_MINIMAL, frozen 25 fields."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL, quotechar='"',
                        delimiter=",")
    writer.writerow(ROW_FIELDS)
    for row in rows:
        writer.writerow([format_field(name, row[name]) for name in ROW_FIELDS])
    data = buf.getvalue().encode("utf-8")
    if b"\r" in data or data.startswith(b"\xef\xbb\xbf"):
        raise ArtifactSerializationError("CR or BOM in CSV output")
    return data


def finalize_rows(rows: list[dict[str, Any]], session_order: Sequence[str],
                  *, enforce_inventory: bool = True) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validate, sort canonically, check unique keys and (optionally) inventory."""
    for row in rows:
        validate_row(row)
    ordered = sorted(rows, key=lambda r: canonical_sort_key(r, session_order))
    keys = [canonical_row_key(r) for r in ordered]
    if len(set(keys)) != len(keys):
        raise ArtifactSerializationError("duplicate canonical row key")
    counts = {axis: 0 for axis in AXES}
    for r in ordered:
        counts[r["axis"]] += 1
    if enforce_inventory:
        expected_keys = set(_expected_inventory_keys(session_order))
        if set(keys) != expected_keys:
            raise StructuralApplicationFailure("row key set differs from closed inventory")
        if counts != EXPECTED_ROW_COUNTS_PER_AXIS or len(ordered) != EXPECTED_TOTAL_ROW_COUNT:
            raise StructuralApplicationFailure(f"row counts {counts} differ from frozen inventory")
    return ordered, counts


def _expected_inventory_keys(session_order: Sequence[str]) -> Iterable[tuple]:
    for s in session_order:
        for a in ASSETS:
            for axis in AXES:
                horizons: tuple[Any, ...] = HORIZONS_PER_AXIS[axis] or (None,)
                for f in FEATURE_SETS_PER_AXIS[axis]:
                    for v in VARIANT_SETS_PER_AXIS[axis]:
                        for q in QUANTILES_PER_AXIS[axis]:
                            for h in horizons:
                                yield (s, a, axis, f, v, q, h)


# ===========================================================================
# 7. Manifest: canonical serialization and NNC-5 strict parsing
# ===========================================================================


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """V2 §5: UTF-8 JSON, sort_keys=true, indent=2, final newline."""
    text = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False)
    return (text + "\n").encode("utf-8")


def manifest_precomparison_sha256(manifest: Mapping[str, Any]) -> str:
    probe = dict(manifest)
    probe["manifest_precomparison_sha256"] = None
    return sha256_hex(canonical_manifest_bytes(probe))


def finalize_manifest(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    out = dict(manifest)
    out["manifest_precomparison_sha256"] = manifest_precomparison_sha256(out)
    return out, canonical_manifest_bytes(out)


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for name, _ in pairs:
        if name in seen:
            raise ManifestValidationError(f"repeated JSON member name: {name!r}")
        seen.add(name)
    return dict(pairs)


def strict_json_loads(data: bytes) -> Any:
    """NNC-5 (A006 §8A): reject repeated decoded member names at every depth.

    Detection happens inside object_pairs_hook, i.e. on the full member-pair
    list of each object before any mapping is built. Any failure to parse
    fails closed.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ManifestValidationError("manifest must be supplied as serialized bytes")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestValidationError(f"manifest is not UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_members)
    except ManifestValidationError:
        raise
    except Exception as exc:  # fail closed on anything unparseable
        raise ManifestValidationError(f"manifest JSON unparseable: {exc}") from exc


def validate_reference_source_sha256_object(obj: Any) -> None:
    """A006 §6.3/§6.4(a) structural validity of Reference source_sha256."""
    if not isinstance(obj, dict):
        raise ProvenanceError("source_sha256 must be a JSON object")
    if set(obj.keys()) != set(REFERENCE_SOURCE_PATHS) or len(obj) != len(REFERENCE_SOURCE_PATHS):
        raise ProvenanceError("source_sha256 must contain exactly the three Reference paths")
    for path, value in obj.items():
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise ProvenanceError(f"source_sha256[{path}] is not 64 lowercase hex")


def validate_runtime_source_commit_value(value: Any) -> str:
    """A006 §5.3(a)(b)/§5.4(b)."""
    if not isinstance(value, str) or not _HEX40.fullmatch(value):
        raise ProvenanceError("runtime_source_commit must match ^[0-9a-f]{40}$")
    return value


def expected_manifest_semantics(session_ids: Sequence[str]) -> dict[str, Any]:
    """Non-role-specific manifest fields fixed by V2/A002/A004."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "session_ids": list(session_ids),
        "assets": list(ASSETS),
        "axes": list(AXES),
        "feature_sets_per_axis": {k: list(v) for k, v in FEATURE_SETS_PER_AXIS.items()},
        "variant_sets_per_axis": {k: list(v) for k, v in VARIANT_SETS_PER_AXIS.items()},
        "quantiles_per_axis": {k: list(v) for k, v in QUANTILES_PER_AXIS.items()},
        "horizons_per_axis": {k: list(v) for k, v in HORIZONS_PER_AXIS.items()},
        "expected_row_counts_per_axis": dict(EXPECTED_ROW_COUNTS_PER_AXIS),
        "expected_total_row_count": EXPECTED_TOTAL_ROW_COUNT,
        "collector_sha256": COLLECTOR_SHA256,
        "engine_sha256": ENGINE_SHA256,
        "candidate_spec_sha256": CANDIDATE_SPEC_SHA256,
        "protocol_sha256": PROTOCOL_SHA256,
        "new36_inventory_id": NEW36_INVENTORY_ID,
        "golden_artifacts_read": False,
        "old36_quantitative_data_read": False,
        "frozen_analysis_engine_state": FROZEN_ANALYSIS_ENGINE_STATE,
        "frozen_analysis_engine_accepts_input": FROZEN_ANALYSIS_ENGINE_ACCEPTS_INPUT,
        "fingerprint_encoding_definition": FINGERPRINT_ENCODING_DEFINITION,
        "quantile_method": QUANTILE_METHOD,
        "grid_ms": MANIFEST_GRID_MS,
    }


def build_reference_manifest(*, runtime_source_commit: str, source_sha256: Mapping[str, str],
                             csv_bytes: bytes, actual_row_counts: Mapping[str, int],
                             session_ids: Sequence[str]) -> tuple[dict[str, Any], bytes]:
    validate_runtime_source_commit_value(runtime_source_commit)
    validate_reference_source_sha256_object(dict(source_sha256))
    manifest = expected_manifest_semantics(session_ids)
    manifest.update({
        "implementation_role": IMPLEMENTATION_ROLE,
        "runtime_source_commit": runtime_source_commit,
        "source_sha256": {p: source_sha256[p] for p in REFERENCE_SOURCE_PATHS},
        "csv_sha256": sha256_hex(csv_bytes),
        "csv_size_bytes": len(csv_bytes),
        "manifest_precomparison_sha256": None,
        "actual_row_counts_per_axis": {a: int(actual_row_counts[a]) for a in AXES},
        "actual_total_row_count": int(sum(actual_row_counts[a] for a in AXES)),
    })
    if set(manifest) != set(REQUIRED_MANIFEST_KEYS):
        raise ArtifactSerializationError("manifest key set differs from V2 §5")
    return finalize_manifest(manifest)


def validate_reference_manifest_bytes(data: bytes, *, session_ids: Sequence[str] | None = None,
                                      csv_bytes: bytes | None = None) -> dict[str, Any]:
    """NNC-5 first, then schema, role-specific provenance and precomparison digest."""
    obj = strict_json_loads(data)
    if not isinstance(obj, dict):
        raise ManifestValidationError("manifest top level must be a JSON object")
    missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in obj]
    if missing:
        raise ManifestValidationError(f"manifest missing keys: {missing}")
    if set(obj) != set(REQUIRED_MANIFEST_KEYS):
        raise ManifestValidationError("Reference manifest carries keys outside V2 §5")
    if obj["implementation_role"] != IMPLEMENTATION_ROLE:
        raise ManifestValidationError("implementation_role must be exactly 'REFERENCE'")
    try:
        validate_runtime_source_commit_value(obj["runtime_source_commit"])
        validate_reference_source_sha256_object(obj["source_sha256"])
    except ProvenanceError as exc:
        raise ManifestValidationError(str(exc)) from exc
    if not isinstance(obj["csv_sha256"], str) or not _HEX64.fullmatch(obj["csv_sha256"]):
        raise ManifestValidationError("csv_sha256 invalid")
    if isinstance(obj["csv_size_bytes"], bool) or not isinstance(obj["csv_size_bytes"], int):
        raise ManifestValidationError("csv_size_bytes invalid")
    stored = obj["manifest_precomparison_sha256"]
    if not isinstance(stored, str) or stored != manifest_precomparison_sha256(obj):
        raise ManifestValidationError("manifest_precomparison_sha256 mismatch")
    if canonical_manifest_bytes(obj) != bytes(data):
        raise ManifestValidationError("manifest bytes are not canonical")
    if session_ids is not None:
        expected = expected_manifest_semantics(session_ids)
        for key, value in expected.items():
            if obj[key] != value or type(obj[key]) is not type(value):
                raise ManifestValidationError(f"manifest field {key} differs from frozen value")
    if csv_bytes is not None:
        if obj["csv_sha256"] != sha256_hex(csv_bytes) or obj["csv_size_bytes"] != len(csv_bytes):
            raise ManifestValidationError("csv identity mismatch")
    return obj


# ===========================================================================
# 8. NNC-1 / NNC-2 Reference provenance
# ===========================================================================


def _git(repo_root: Path, args: Sequence[str], git_executable: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([git_executable, "-C", str(repo_root), *args],
                              capture_output=True, check=False)
    except OSError as exc:
        raise ProvenanceError(f"git unavailable: {exc}") from exc


def resolve_runtime_source_commit(repo_root: Path | str | None = None, *,
                                  git_executable: str = "git") -> str:
    """NNC-1: stripped `git rev-parse HEAD`, must match ^[0-9a-f]{40}$."""
    root = Path(repo_root) if repo_root is not None else DEFAULT_REPO_ROOT
    proc = _git(root, ["rev-parse", "HEAD"], git_executable)
    if proc.returncode != 0:
        raise ProvenanceError("HEAD cannot be resolved: "
                              + proc.stderr.decode("utf-8", "replace").strip())
    try:
        value = proc.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProvenanceError("HEAD output not UTF-8") from exc
    return validate_runtime_source_commit_value(value)


def _verify_repo_toplevel(repo_root: Path, git_executable: str) -> None:
    proc = _git(repo_root, ["rev-parse", "--show-toplevel"], git_executable)
    if proc.returncode != 0:
        raise ProvenanceError("repository top level cannot be resolved")
    top = Path(proc.stdout.decode("utf-8", "replace").strip())
    if top.resolve() != repo_root.resolve():
        raise ProvenanceError(f"repo_root {repo_root} is not the repository top level {top}")


def compute_reference_source_sha256(repo_root: Path | str | None, commit: str, *,
                                    git_executable: str = "git") -> dict[str, str]:
    """NNC-2: raw working-tree SHA256 per exact path, bound to committed blob at C."""
    root = Path(repo_root) if repo_root is not None else DEFAULT_REPO_ROOT
    validate_runtime_source_commit_value(commit)
    _verify_repo_toplevel(root, git_executable)
    out: dict[str, str] = {}
    for rel in REFERENCE_SOURCE_PATHS:
        try:
            wt_bytes = root.joinpath(*rel.split("/")).read_bytes()
        except OSError as exc:
            raise ProvenanceError(f"working-tree file unreadable: {rel}: {exc}") from exc
        wt_sha = sha256_hex(wt_bytes)
        proc = _git(root, ["cat-file", "blob", f"{commit}:{rel}"], git_executable)
        if proc.returncode != 0:
            raise ProvenanceError(f"{rel} does not exist as a committed file at {commit}")
        if sha256_hex(proc.stdout) != wt_sha:
            raise ProvenanceError(f"{rel}: working-tree bytes differ from committed blob at {commit}")
        out[rel] = wt_sha
    validate_reference_source_sha256_object(out)
    return out


# ===========================================================================
# 9. NNC-3 runtime environment identity
# ===========================================================================


def normalize_distribution_name(name: str) -> str:
    """A006 §7.3.1(B): lower(), then each maximal [-_.]+ run -> '-'."""
    return re.sub(r"[-_.]+", "-", name.lower())


def _distribution_name_version(dist: Any) -> tuple[Any, Any]:
    try:
        md = dist.metadata
        name = md["Name"]
    except Exception:
        name = None
    try:
        version = dist.version
    except Exception:
        version = None
    return name, version


def _default_pyarrow_version_loader() -> str:
    if _pq is None:
        raise EnvironmentIdentityError(
            f"module-load import of pyarrow.parquet failed: {_PYARROW_IMPORT_ERROR}")
    importlib.import_module("pyarrow.parquet")
    return importlib.import_module("pyarrow").__version__


def build_runtime_environment_identity(
    *,
    repo_root: Path | str | None = None,
    requirements_path: Path | str | None = None,
    distributions: Iterable[Any] | None = None,
    pyarrow_version_loader: Callable[[], str] | None = None,
    implementation_name: str | None = None,
    python_version: str | None = None,
) -> dict[str, Any]:
    """A006 §7.3.1: exactly python / installed_distributions / requirements_sha256 /
    pyarrow_version. Parameters exist only for synthetic testing; defaults use
    the live interpreter."""
    impl = sys.implementation.name if implementation_name is None else implementation_name
    ver = sys.version if python_version is None else python_version
    if not isinstance(impl, str) or not isinstance(ver, str):
        raise EnvironmentIdentityError("python identity unavailable")

    dists = importlib.metadata.distributions() if distributions is None else distributions
    records: list[list[str]] = []
    try:
        for dist in dists:
            name, version = _distribution_name_version(dist)
            if not isinstance(name, str) or name == "":
                raise EnvironmentIdentityError("distribution Name absent or empty")
            if not isinstance(version, str) or version == "":
                raise EnvironmentIdentityError(f"distribution {name!r} version absent or empty")
            records.append([normalize_distribution_name(name), version])
    except EnvironmentIdentityError:
        raise
    except Exception as exc:
        raise EnvironmentIdentityError(f"distribution enumeration failed: {exc}") from exc
    records.sort()

    if requirements_path is None:
        root = Path(repo_root) if repo_root is not None else DEFAULT_REPO_ROOT
        requirements_path = root.joinpath(*REQUIREMENTS_RELATIVE_PATH.split("/"))
    try:
        req_sha = sha256_hex(Path(requirements_path).read_bytes())
    except OSError as exc:
        raise EnvironmentIdentityError(f"requirements unreadable: {exc}") from exc

    loader = _default_pyarrow_version_loader if pyarrow_version_loader is None else pyarrow_version_loader
    try:
        pa_version = loader()
    except EnvironmentIdentityError:
        raise
    except Exception as exc:
        raise EnvironmentIdentityError(f"import pyarrow.parquet failed: {exc}") from exc
    if pa_version != NORMATIVE_PYARROW_VERSION:
        raise EnvironmentIdentityError(
            f"pyarrow.__version__ must be exactly {NORMATIVE_PYARROW_VERSION!r}, got {pa_version!r}")

    return {
        "python": {"implementation": impl, "version": ver},
        "installed_distributions": records,
        "requirements_sha256": req_sha,
        "pyarrow_version": pa_version,
    }


def canonical_environment_identity_bytes(identity: Mapping[str, Any]) -> bytes:
    """A006 §7.3.2: sort_keys, compact separators, ensure_ascii, no final newline."""
    return json.dumps(identity, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def runtime_environment_identity_sha256(identity: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_environment_identity_bytes(identity))


def _validate_identity_shape(identity: Any) -> None:
    if not isinstance(identity, dict) or set(identity) != {
            "python", "installed_distributions", "requirements_sha256", "pyarrow_version"}:
        raise EnvironmentIdentityError("runtime_environment_identity member set invalid")
    py = identity["python"]
    if not isinstance(py, dict) or set(py) != {"implementation", "version"}:
        raise EnvironmentIdentityError("python member invalid")
    if not isinstance(identity["installed_distributions"], list):
        raise EnvironmentIdentityError("installed_distributions must be an array")
    for rec in identity["installed_distributions"]:
        if (not isinstance(rec, list) or len(rec) != 2
                or not all(isinstance(x, str) and x for x in rec)):
            raise EnvironmentIdentityError("installed_distributions record invalid")
    if identity["pyarrow_version"] != NORMATIVE_PYARROW_VERSION:
        raise EnvironmentIdentityError("recorded pyarrow_version is not 17.0.0")


def verify_environment_equivalence(current: Mapping[str, Any], current_sha256: str,
                                   recorded: Mapping[str, Any], recorded_sha256: str) -> None:
    """A006 §7.4: full-object equality AND digest equality (each digest must
    also be the canonical digest of its own object)."""
    _validate_identity_shape(current)
    _validate_identity_shape(recorded)
    if runtime_environment_identity_sha256(current) != current_sha256:
        raise EnvironmentIdentityError("current identity digest does not match its object")
    if runtime_environment_identity_sha256(recorded) != recorded_sha256:
        raise EnvironmentIdentityError("recorded identity digest does not match its object")
    if dict(current) != dict(recorded):
        raise EnvironmentIdentityError("runtime_environment_identity objects differ")
    if current_sha256 != recorded_sha256:
        raise EnvironmentIdentityError("runtime_environment_identity_sha256 values differ")


def environment_identity_record(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {"runtime_environment_identity": dict(identity),
            "runtime_environment_identity_sha256": runtime_environment_identity_sha256(identity)}


# ===========================================================================
# 10. NNC-4 session_id uniqueness gate and authoritative expected count
# ===========================================================================


def _open_readonly(db_path: Path | str) -> sqlite3.Connection:
    p = Path(db_path)
    if not p.is_file():
        raise SessionUniquenessError(f"runtime database not found: {p}")
    uri = f"file:{quote(str(p.resolve()))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise SessionUniquenessError(f"runtime database cannot be opened read-only: {exc}") from exc
    return conn


def verify_session_id_uniqueness(db_path: Path | str) -> None:
    """NNC-4 (A006 §8): A) no duplicate session_id rows; B) DB-enforced,
    non-partial, single-column uniqueness on exactly sessions.session_id.
    Read-only; fails closed if anything cannot be established."""
    conn = _open_readonly(db_path)
    try:
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'").fetchall()
            if not tables:
                raise SessionUniquenessError("sessions table absent")
            cols = [r[1] for r in conn.execute("PRAGMA table_info('sessions')").fetchall()]
            if "session_id" not in cols:
                raise SessionUniquenessError("sessions.session_id column absent")
            dup = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id IS NOT NULL "
                "GROUP BY session_id HAVING COUNT(*) > 1 LIMIT 1").fetchall()
            if dup:
                raise SessionUniquenessError("duplicate session_id rows present")
            qualifying = []
            for _seq, name, unique, _origin, partial in conn.execute(
                    "PRAGMA index_list('sessions')").fetchall():
                if int(unique) != 1 or int(partial) != 0:
                    continue
                key_cols = [(r[1], r[2]) for r in conn.execute(
                    f"PRAGMA index_xinfo({_sql_quote(name)})").fetchall() if int(r[5]) == 1]
                if len(key_cols) == 1 and key_cols[0][0] >= 0 and key_cols[0][1] == "session_id":
                    qualifying.append(name)
            if not qualifying:
                raise SessionUniquenessError(
                    "no DB-enforced non-partial single-column unique mechanism on sessions.session_id")
        except sqlite3.Error as exc:
            raise SessionUniquenessError(f"uniqueness cannot be established: {exc}") from exc
    finally:
        conn.close()


def _sql_quote(identifier: str) -> str:
    return "'" + identifier.replace("'", "''") + "'"


def authoritative_sync_grid_count(db_path: Path | str | None, session_id: str) -> int | None:
    """A005 R2.8 / A006 §4.10.2 expected sync_grid_100ms count (None if unavailable)."""
    if db_path is None:
        return None
    conn = _open_readonly(db_path)
    try:
        rows = conn.execute("SELECT current_qa_run_id FROM sessions WHERE session_id = ?",
                            (session_id,)).fetchall()
        if len(rows) > 1:  # impossible after NNC-4; never resolved heuristically
            raise SessionUniquenessError(f"duplicate sessions rows for {session_id}")
        if not rows or rows[0][0] is None:
            return None
        run = conn.execute("SELECT sync_grid_file_count FROM qa_runs WHERE id = ?",
                           (rows[0][0],)).fetchall()
        if not run:
            return None
        return run[0][0]
    finally:
        conn.close()


# ===========================================================================
# 11. AMB-R3 structural validation (A006 §4) and loader (A005 R2)
# ===========================================================================

DATASET_DIRS: tuple[str, ...] = ("sync_grid_100ms", "normalized_books", "normalized_trades")
PARQUET_MAGIC = b"PAR1"
_MIN_PARQUET_SIZE = 8
_PART_NAME_RE = re.compile(r"(\d+)\.parquet$")
UNASSIGNED_DIR = "_unassigned_"


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


def check_parquet_metadata(buf: bytes) -> tuple[bool, str]:
    """A006 §4.5.2 _check_metadata semantics."""
    if _pq is None:
        return False, f"pyarrow not available: {_PYARROW_IMPORT_ERROR}"
    try:
        md = _pq.ParquetFile(io.BytesIO(buf)).metadata
        if md is None:
            return False, "no metadata block"
    except Exception as exc:  # noqa: BLE001 - codified behavior
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def classify_parquet_entry(filename: str) -> str:
    """A006 §4.3: first match in DATASET_DIRS tuple order, else unassigned."""
    for d in DATASET_DIRS:
        needle = d + "/"
        if filename.startswith(needle) or ("/" + needle) in filename:
            return d
    return UNASSIGNED_DIR


def parse_part_index(filename: str) -> int | None:
    """A006 §4.4: search r'(\\d+)\\.parquet$' on the basename."""
    m = _PART_NAME_RE.search(PurePosixPath(filename).name)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:  # pragma: no cover - unreachable
        return None


def _apply_sequence_rules(res: PerDirResult, indices: list[int], unparseable: list[str]) -> None:
    """A006 §4.6.2, in source order."""
    res.unparseable_names = list(unparseable)
    if unparseable:
        res.sequence_ok = False
        res.sequence_detail = f"{len(unparseable)} entries not matching part-XXX.parquet"
    counts: dict[int, int] = {}
    for i in indices:
        counts[i] = counts.get(i, 0) + 1
    res.duplicate_parts = sorted(i for i, c in counts.items() if c > 1)
    if indices:
        lo, hi = min(indices), max(indices)
        res.parts_expected = hi - lo + 1
        res.missing_parts = sorted(set(range(lo, hi + 1)) - set(indices))
        if res.missing_parts or res.duplicate_parts:
            res.sequence_ok = False
            details: list[str] = []
            if res.missing_parts:
                details.append(f"missing {len(res.missing_parts)}")
            if res.duplicate_parts:
                details.append(f"duplicated {len(res.duplicate_parts)}")
            prefix = res.sequence_detail + "; " if res.sequence_detail else ""
            res.sequence_detail = prefix + ", ".join(details)
        elif not res.sequence_detail:
            res.sequence_detail = f"contiguous 0..{hi}"
    else:
        res.parts_expected = 0
        res.sequence_ok = False
        res.sequence_detail = "no parquet files present"


def validate_all_parquet(zip_path: Path | str, *,
                         metadata_checker: Callable[[bytes], tuple[bool, str]] | None = None
                         ) -> ParquetValidation:
    """A006 §4.2-§4.6 session-level parquet validation."""
    check_md = check_parquet_metadata if metadata_checker is None else metadata_checker
    v = ParquetValidation()
    with zipfile.ZipFile(zip_path) as zf:
        entries = [info for info in zf.infolist()
                   if info.filename.endswith(".parquet") and not info.filename.endswith("/")]
        v.parquet_files_total = len(entries)
        buckets: dict[str, list[zipfile.ZipInfo]] = {d: [] for d in DATASET_DIRS}
        unassigned: list[zipfile.ZipInfo] = []
        for info in entries:
            d = classify_parquet_entry(info.filename)
            (unassigned if d == UNASSIGNED_DIR else buckets[d]).append(info)

        for d in DATASET_DIRS:
            res = PerDirResult(dataset_dir=d)
            v.per_dir[d] = res
            indices: list[int] = []
            unparseable: list[str] = []
            for info in buckets[d]:
                res.files_seen += 1
                name = info.filename
                idx = parse_part_index(name)
                if info.file_size < _MIN_PARQUET_SIZE:
                    v.parquet_magic_checked += 1
                    v.parquet_magic_failed += 1
                    v.failures.append(f"{name}: size {info.file_size} < {_MIN_PARQUET_SIZE}")
                    v.parquet_metadata_checked += 1
                    v.parquet_metadata_failed += 1
                else:
                    buf = zf.read(info)
                    head_ok = buf[:4] == PARQUET_MAGIC
                    tail_ok = buf[-4:] == PARQUET_MAGIC
                    v.parquet_magic_checked += 1
                    if head_ok and tail_ok:
                        v.parquet_magic_passed += 1
                    else:
                        v.parquet_magic_failed += 1
                        reason: list[str] = []
                        if not head_ok:
                            reason.append("head")
                        if not tail_ok:
                            reason.append("tail")
                        v.failures.append(f"{name}: PAR1 magic missing ({'+'.join(reason)})")
                    v.parquet_metadata_checked += 1
                    md_ok, md_err = check_md(buf)
                    if md_ok:
                        v.parquet_metadata_passed += 1
                    else:
                        v.parquet_metadata_failed += 1
                        v.failures.append(f"{name}: metadata unreadable ({md_err})")
                if idx is None:
                    unparseable.append(name)
                else:
                    indices.append(idx)
            _apply_sequence_rules(res, indices, unparseable)
            if not res.sequence_ok:
                v.parquet_sequence_gaps += len(res.missing_parts)
                v.parquet_duplicate_parts += len(res.duplicate_parts)
                if res.missing_parts:
                    v.failures.append(f"{d}: missing part indices sample={res.missing_parts[:10]}")
                if res.duplicate_parts:
                    v.failures.append(f"{d}: duplicate part indices sample={res.duplicate_parts[:10]}")

        if unassigned:
            ures = PerDirResult(dataset_dir=UNASSIGNED_DIR)
            v.per_dir[UNASSIGNED_DIR] = ures
            for info in unassigned:
                ures.files_seen += 1
                name = info.filename
                if info.file_size < _MIN_PARQUET_SIZE:
                    v.parquet_magic_checked += 1
                    v.parquet_magic_failed += 1
                    v.parquet_metadata_checked += 1
                    v.parquet_metadata_failed += 1
                    v.failures.append(f"{name}: (unassigned dir) size < min")
                    continue
                buf = zf.read(info)
                v.parquet_magic_checked += 1
                if buf[:4] == PARQUET_MAGIC and buf[-4:] == PARQUET_MAGIC:
                    v.parquet_magic_passed += 1
                else:
                    v.parquet_magic_failed += 1
                    v.failures.append(f"{name}: (unassigned dir) PAR1 missing")
                v.parquet_metadata_checked += 1
                md_ok, md_err = check_md(buf)
                if md_ok:
                    v.parquet_metadata_passed += 1
                else:
                    v.parquet_metadata_failed += 1
                    v.failures.append(f"{name}: (unassigned dir) metadata: {md_err}")
            ures.sequence_ok = True
            ures.sequence_detail = "not enforced (unassigned)"
    return v


def evaluate_sync_grid_coverage(validation: ParquetValidation,
                                expected_count: int | None) -> tuple[bool, str | None]:
    """A006 §4.9: P1 > P2 > P3 > P4 > P5, first failing branch wins."""
    sync_dir = validation.per_dir.get("sync_grid_100ms")
    if sync_dir is None or sync_dir.files_seen == 0:
        return False, "sync_grid_100ms: no parquet parts present"
    if not sync_dir.sequence_ok:
        return False, f"sync_grid_100ms: sequence invalid ({sync_dir.sequence_detail})"
    sync_failures = [f for f in validation.failures if "sync_grid_100ms" in f]
    if sync_failures:
        # Reason prose is not cross-role normative (A006 §4.12, §10.3 X1).
        return False, f"sync_grid_100ms: {len(sync_failures)} parquet failure(s): {sync_failures[:5]}"
    if expected_count is not None and sync_dir.files_seen != expected_count:
        return False, (f"sync_grid_100ms: authoritative manifest expects {expected_count} "
                       f"parts, found {sync_dir.files_seen}")
    return True, None


def _default_parquet_reader(buf: bytes) -> pd.DataFrame:
    if _pq is None:
        raise RuntimeError(f"pyarrow not available: {_PYARROW_IMPORT_ERROR}")
    return _pq.read_table(io.BytesIO(buf)).to_pandas()


def select_sync_grid_entries(names: Sequence[str], asset: str) -> list[str]:
    """A005 R2.1-R2.3: primary (substring asset) else fallback; sorted() full paths."""
    primary = sorted(n for n in names
                     if "sync_grid_100ms" in n and n.endswith(".parquet") and asset in n)
    if primary:
        return primary
    return sorted(n for n in names if "sync_grid_100ms" in n and n.endswith(".parquet"))


def load_asset_frame(zip_path: Path | str, asset: str, *,
                     parquet_reader: Callable[[bytes], pd.DataFrame] | None = None) -> pd.DataFrame:
    """A005 R2.1-R2.7. FileNotFoundError propagates (PENDING path)."""
    reader = _default_parquet_reader if parquet_reader is None else parquet_reader
    with zipfile.ZipFile(zip_path) as zf:
        grid_names = select_sync_grid_entries(zf.namelist(), asset)
        if not grid_names:
            raise BlockDataUnavailableError(
                f"Case B: no sync_grid_100ms parquet entry for {asset}")
        parts: list[pd.DataFrame] = []
        for name in grid_names:
            df = reader(zf.read(name))
            if "asset" in df.columns:
                df = df[df["asset"] == asset].copy()
            parts.append(df)
    combined = pd.concat(parts, ignore_index=True)
    if len(combined) == 0:
        raise BlockDataUnavailableError(f"Case D: zero rows for {asset} after filtering")
    return combined


BLOCK_VALID = "VALID"
BLOCK_PENDING = "PENDING"
BLOCK_STRUCTURAL_INVALID = "STRUCTURAL_INVALID"


@dataclass
class BlockOutcome:
    status: str
    reason: str | None = None
    grid: pd.DataFrame | None = None


class SessionSource:
    """Coverage-cached access to canonical session ZIPs (A006 §4.10/§4.11).

    ``zip_resolver(session_id)`` returns the canonical session ZIP path or
    raises FileNotFoundError. Paths are deployment details (A004 §5.3)."""

    def __init__(self, zip_resolver: Callable[[str], Path | str], *,
                 db_path: Path | str | None,
                 parquet_reader: Callable[[bytes], pd.DataFrame] | None = None,
                 metadata_checker: Callable[[bytes], tuple[bool, str]] | None = None):
        self._resolver = zip_resolver
        self._db_path = db_path
        self._reader = parquet_reader
        self._md = metadata_checker
        self._part_coverage_cache: dict[str, tuple[bool, str | None]] = {}

    def _open_reference_zip_path(self, session_id: str) -> str:
        path = self._resolver(session_id)
        with zipfile.ZipFile(path) as zf:  # FileNotFoundError propagates
            return str(zf.filename)

    def validate_part_coverage(self, session_id: str) -> tuple[bool, str | None]:
        if session_id in self._part_coverage_cache:
            return self._part_coverage_cache[session_id]
        try:
            stored_path = self._open_reference_zip_path(session_id)
        except FileNotFoundError:
            result: tuple[bool, str | None] = (True, None)
            self._part_coverage_cache[session_id] = result
            return result
        expected = authoritative_sync_grid_count(self._db_path, session_id)
        result = evaluate_sync_grid_coverage(
            validate_all_parquet(stored_path, metadata_checker=self._md), expected)
        self._part_coverage_cache[session_id] = result
        return result

    def block(self, session_id: str, asset: str) -> BlockOutcome:
        ok, reason = self.validate_part_coverage(session_id)
        if not ok:
            return BlockOutcome(BLOCK_STRUCTURAL_INVALID, reason)
        try:
            path = self._resolver(session_id)
            df = load_asset_frame(path, asset, parquet_reader=self._reader)
        except FileNotFoundError:
            return BlockOutcome(BLOCK_PENDING, "session ZIP unavailable")
        except BlockDataUnavailableError as exc:
            return BlockOutcome(BLOCK_STRUCTURAL_INVALID, str(exc))
        try:
            grid = build_canonical_grid(df)
        except Exception as exc:  # CorruptGridError and any other grid failure
            return BlockOutcome(BLOCK_STRUCTURAL_INVALID, f"{type(exc).__name__}: {exc}")
        if len(grid) == 0:
            return BlockOutcome(BLOCK_STRUCTURAL_INVALID, "empty canonical grid")
        return BlockOutcome(BLOCK_VALID, None, grid)


# ===========================================================================
# 12. Inventory, pre-application gates and one-shot orchestration
# ===========================================================================


def load_frozen_new36_session_ids(registry_path: Path | str | None = None) -> tuple[str, ...]:
    """Frozen NEW36 identity from backend/checkpoint_registry.py (V2 §11)."""
    path = Path(registry_path) if registry_path is not None else CHECKPOINT_REGISTRY_PATH
    backend_dir = str(path.parent)
    added = backend_dir not in sys.path
    if added:
        sys.path.insert(0, backend_dir)
    try:
        spec = importlib.util.spec_from_file_location("_superbot_v1_2_reference_registry", path)
        if spec is None or spec.loader is None:
            raise InventoryError("checkpoint_registry unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ids = tuple(module.NEW36_SESSION_IDS)
    except InventoryError:
        raise
    except Exception as exc:
        raise InventoryError(f"checkpoint_registry could not be loaded: {exc}") from exc
    finally:
        if added:
            try:
                sys.path.remove(backend_dir)
            except ValueError:  # pragma: no cover
                pass
    return validate_session_inventory(ids)


def validate_session_inventory(session_ids: Sequence[str]) -> tuple[str, ...]:
    ids = tuple(session_ids)
    if (len(ids) != EXPECTED_SESSION_COUNT or len(set(ids)) != EXPECTED_SESSION_COUNT
            or not all(isinstance(s, str) and s for s in ids)):
        raise InventoryError("inventory must be exactly 12 unique session identifiers")
    return ids


@dataclass
class PreApplicationRecord:
    session_ids: tuple[str, ...]
    runtime_source_commit: str
    source_sha256: dict[str, str]
    runtime_environment_identity: dict[str, Any]
    runtime_environment_identity_sha256: str
    session_id_uniqueness_verified: bool


def run_pre_application_gates(
    *,
    db_path: Path | str | None,
    recorded_environment_identity: Mapping[str, Any] | None,
    recorded_environment_identity_sha256: str | None,
    session_ids: Sequence[str] | None = None,
    repo_root: Path | str | None = None,
    git_executable: str = "git",
    environment_identity_builder: Callable[[], dict[str, Any]] | None = None,
    output_paths: Sequence[Path | str] = (),
) -> PreApplicationRecord:
    """Every gate that must pass before any NEW36 quantitative input is read.
    This function never opens a session ZIP."""
    ids = (validate_session_inventory(session_ids) if session_ids is not None
           else load_frozen_new36_session_ids())
    for p in output_paths:
        if Path(p).exists():
            raise OutputPathError(f"output artifact already exists: {p}")
    commit = resolve_runtime_source_commit(repo_root, git_executable=git_executable)       # NNC-1
    sources = compute_reference_source_sha256(repo_root, commit, git_executable=git_executable)  # NNC-2
    if recorded_environment_identity is None or recorded_environment_identity_sha256 is None:
        raise EnvironmentIdentityError("no recorded runtime_environment_identity supplied")
    builder = environment_identity_builder or (lambda: build_runtime_environment_identity(
        repo_root=repo_root))
    current = builder()                                                                    # NNC-3
    verify_environment_equivalence(current, runtime_environment_identity_sha256(current),
                                   recorded_environment_identity,
                                   recorded_environment_identity_sha256)
    if db_path is None:
        raise SessionUniquenessError("runtime database not supplied; uniqueness unverifiable")
    verify_session_id_uniqueness(db_path)                                                  # NNC-4
    return PreApplicationRecord(ids, commit, sources, dict(current),
                                runtime_environment_identity_sha256(current), True)


@dataclass
class GenerationResult:
    csv_path: str
    manifest_path: str
    csv_sha256: str
    csv_size_bytes: int
    manifest_sha256: str
    manifest_precomparison_sha256: str
    runtime_source_commit: str
    row_counts: dict[str, int]
    pre_application: PreApplicationRecord


class ApplicationState:
    """One-shot accounting visible to callers (V2 §13)."""

    def __init__(self) -> None:
        self.application_invocation_started = False
        self.sessions_accessed: list[str] = []


def generate_reference_artifacts(
    *,
    csv_path: Path | str,
    manifest_path: Path | str,
    zip_resolver: Callable[[str], Path | str],
    db_path: Path | str | None,
    recorded_environment_identity: Mapping[str, Any] | None,
    recorded_environment_identity_sha256: str | None,
    session_ids: Sequence[str] | None = None,
    repo_root: Path | str | None = None,
    git_executable: str = "git",
    environment_identity_builder: Callable[[], dict[str, Any]] | None = None,
    parquet_reader: Callable[[bytes], pd.DataFrame] | None = None,
    metadata_checker: Callable[[bytes], tuple[bool, str]] | None = None,
    state: ApplicationState | None = None,
) -> GenerationResult:
    """Pre-application gates, then the single NEW36 Reference application."""
    state = state if state is not None else ApplicationState()
    record = run_pre_application_gates(
        db_path=db_path,
        recorded_environment_identity=recorded_environment_identity,
        recorded_environment_identity_sha256=recorded_environment_identity_sha256,
        session_ids=session_ids, repo_root=repo_root, git_executable=git_executable,
        environment_identity_builder=environment_identity_builder,
        output_paths=(csv_path, manifest_path),
    )

    # ---- application invocation begins (first NEW36 access) ----------------
    state.application_invocation_started = True
    source = SessionSource(zip_resolver, db_path=db_path, parquet_reader=parquet_reader,
                           metadata_checker=metadata_checker)
    rows: list[dict[str, Any]] = []
    for sid in record.session_ids:
        state.sessions_accessed.append(sid)
        for asset in ASSETS:
            outcome = source.block(sid, asset)
            if outcome.status != BLOCK_VALID or outcome.grid is None:
                raise StructuralApplicationFailure(
                    f"block {sid}/{asset} {outcome.status}: {outcome.reason}",
                    session_id=sid, asset=asset, classification=outcome.status)
            rows.extend(compute_block_rows(sid, asset, outcome.grid))
    ordered, counts = finalize_rows(rows, record.session_ids, enforce_inventory=True)
    csv_bytes = serialize_csv(ordered)

    # Generation-time provenance must be unchanged since the gate.
    commit = resolve_runtime_source_commit(repo_root, git_executable=git_executable)
    sources = compute_reference_source_sha256(repo_root, commit, git_executable=git_executable)
    if commit != record.runtime_source_commit or sources != record.source_sha256:
        raise ProvenanceError("Reference provenance changed during generation")

    manifest, manifest_bytes = build_reference_manifest(
        runtime_source_commit=commit, source_sha256=sources, csv_bytes=csv_bytes,
        actual_row_counts=counts, session_ids=record.session_ids)
    validate_reference_manifest_bytes(manifest_bytes, session_ids=record.session_ids,
                                      csv_bytes=csv_bytes)

    csv_p, man_p = Path(csv_path), Path(manifest_path)
    with open(csv_p, "xb") as fh:
        fh.write(csv_bytes)
    with open(man_p, "xb") as fh:
        fh.write(manifest_bytes)
    if csv_p.read_bytes() != csv_bytes or man_p.read_bytes() != manifest_bytes:
        raise ArtifactSerializationError("written artifact bytes differ from serialized bytes")
    return GenerationResult(
        csv_path=str(csv_p), manifest_path=str(man_p),
        csv_sha256=sha256_hex(csv_bytes), csv_size_bytes=len(csv_bytes),
        manifest_sha256=sha256_hex(manifest_bytes),
        manifest_precomparison_sha256=manifest["manifest_precomparison_sha256"],
        runtime_source_commit=commit, row_counts=counts, pre_application=record)


# ===========================================================================
# 13. Command-line interface (explicit invocation only)
# ===========================================================================


def _read_strict_json_file(path: str) -> Any:
    return strict_json_loads(Path(path).read_bytes())


def _zip_map_resolver(mapping: Mapping[str, str]) -> Callable[[str], str]:
    def resolve(session_id: str) -> str:
        if session_id not in mapping:
            raise FileNotFoundError(session_id)
        p = Path(mapping[session_id])
        if not p.is_file():
            raise FileNotFoundError(str(p))
        return str(p)
    return resolve


def _load_environment_record(path: str) -> tuple[dict[str, Any], str]:
    rec = _read_strict_json_file(path)
    if not isinstance(rec, dict) or set(rec) != {"runtime_environment_identity",
                                                 "runtime_environment_identity_sha256"}:
        raise EnvironmentIdentityError("environment record malformed")
    return rec["runtime_environment_identity"], rec["runtime_environment_identity_sha256"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="v1_2_reference")
    sub = parser.add_subparsers(dest="command", required=True)
    p_env = sub.add_parser("environment-identity", help="record NNC-3 identity (no NEW36 access)")
    p_env.add_argument("--out", required=True)
    for name in ("preflight", "generate"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--environment-record", required=True)
        if name == "generate":
            p.add_argument("--session-zip-map", required=True,
                           help="JSON object: session_id -> canonical session ZIP path")
            p.add_argument("--out-csv", required=True)
            p.add_argument("--out-manifest", required=True)
    args = parser.parse_args(argv)

    if args.command == "environment-identity":
        record = environment_identity_record(build_runtime_environment_identity())
        with open(args.out, "xb") as fh:
            fh.write(canonical_manifest_bytes(record))
        print(f"runtime_environment_identity_sha256={record['runtime_environment_identity_sha256']}")
        return 0

    identity, identity_sha = _load_environment_record(args.environment_record)
    if args.command == "preflight":
        rec = run_pre_application_gates(db_path=args.db, recorded_environment_identity=identity,
                                        recorded_environment_identity_sha256=identity_sha)
        print("REFERENCE_PRE_APPLICATION_GATES=PASS")
        print(f"runtime_source_commit={rec.runtime_source_commit}")
        print(f"runtime_environment_identity_sha256={rec.runtime_environment_identity_sha256}")
        return 0

    mapping = _read_strict_json_file(args.session_zip_map)
    if not isinstance(mapping, dict):
        raise SystemExit("session-zip-map must be a JSON object")
    result = generate_reference_artifacts(
        csv_path=args.out_csv, manifest_path=args.out_manifest,
        zip_resolver=_zip_map_resolver(mapping), db_path=args.db,
        recorded_environment_identity=identity,
        recorded_environment_identity_sha256=identity_sha)
    print(f"REFERENCE_CSV_SHA256={result.csv_sha256}")
    print(f"REFERENCE_CSV_SIZE_BYTES={result.csv_size_bytes}")
    print(f"REFERENCE_MANIFEST_SHA256={result.manifest_sha256}")
    return 0


if __name__ == "__main__":  # pragma: no cover - explicit invocation only
    sys.exit(main())
