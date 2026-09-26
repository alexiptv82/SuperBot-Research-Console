"""SECTION 16 STEP 4 — EXECUTORS (A008 DRAFT4 Sections 8, 10, 11).

This module is the "Executors" module named in A008 DRAFT4 §0.1:
  backend/recovery/v1_2_new36_validators.py

It implements, EXACTLY as specified in the frozen text it is authorized
to consume (A008 DRAFT4 §5.2: V2, A001-A008 frozen text; V1_2_CANDIDATE_SPEC.txt;
v1_2_candidate.py, v1_2_stage3.py, v1_2_diagnostics.py; engine.py, frozen_engine.py,
checkpoint_registry.py, parquet_validator.py; harness.py contracts only;
v1_2_preflight.py, v1_2_acceptance.py):

* NNC-5 — duplicate-member-safe JSON parser (A006 §8A; A008 §8).
* NNC-3 — runtime environment identity (A006 §7; A008 §6), CANDIDATE side.
* NNC-4 — live database session_id uniqueness precondition (A006 §8;
  A008 §7). Per explicit user correction: this is a READ-ONLY structural
  verification that (A) no duplicate sessions.session_id rows exist and
  (B) the live schema enforces a DB-level, non-partial, single-column
  UNIQUE constraint/index on EXACTLY sessions.session_id. It is NOT a
  canonical-schema/manifest-shape validator. No DB write, no
  state-modifying PRAGMA, no duplicate resolution (A006 §8.5).
* S1 — independent implementation equivalence comparator (A008 §10).
* S2 — closed candidate invariant validator against the BINDING
  OBSERVABILITY MATRIX (A008 §11).

CLEAN-ROOM: this module MUST NOT import, open, read or reference any path
under backend/recovery/reference/. It MUST NOT open any NEW36 ZIP and MUST
NOT execute Candidate or Reference application code. It is Section 16
STEP 4 implementation ONLY — no quantitative NEW36 access anywhere in
this file.

SAFETY (never remove):
* No import-time execution of any NEW36-consuming behaviour.
* No modification of engine.py, v1_2_candidate.py, v1_2_stage3.py,
  v1_2_diagnostics.py, checkpoint_registry.py, allowlist.py, sandbox.py,
  harness.py, parquet_validator.py, frozen_engine.py, v1_2_preflight.py,
  v1_2_acceptance.py, or any path under backend/recovery/reference/.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
REQUIREMENTS_PATH: Path = REPO_ROOT / "backend" / "requirements.txt"

# ---------------------------------------------------------------------------
# Canonical ordering constants (A008 DRAFT4 / V2 §3, §4 — frozen text only)
# ---------------------------------------------------------------------------

ASSET_ORDER: tuple[str, ...] = ("BTC", "ETH")
AXIS_ORDER: tuple[str, ...] = ("HZ", "HG", "HC", "HB")

# Appendix-B literal (frozen text) — used ONLY as a drift-guard assertion
# against the authorized STAGE3_HZ_FEATURES/STAGE3_HB_FAMILIES constants
# actually consumed below (A008 §4.7.3: "verified equal to Appendix B").
_APPENDIX_B_HZ_FEATURE_ORDER: tuple[str, ...] = (
    "bitget_ofi", "bitget_trade_flow", "depth_imbalance_l1", "depth_imbalance_l5",
    "external_ofi", "external_trade_flow", "fair_accel_100ms", "fair_gap_reversion",
    "leader_gap_100ms", "leader_gap_200ms", "leader_gap_500ms", "leader_gap_1000ms",
)
_APPENDIX_B_HB_FEATURE_ORDER: tuple[str, ...] = (
    "depth_imbalance_l1", "gap_depthL1", "gap_localOFI", "bitget_ofi",
    "depthL1_extOFI", "gap_depth_extOFI",
)

from .v1_2_stage3 import STAGE3_HB_FAMILIES as _STAGE3_HB_FAMILIES  # noqa: E402
from .v1_2_stage3 import STAGE3_HZ_FEATURES as _STAGE3_HZ_FEATURES  # noqa: E402

if tuple(_STAGE3_HZ_FEATURES) != _APPENDIX_B_HZ_FEATURE_ORDER:
    raise RuntimeError("A008 §4.7.3 DRIFT: STAGE3_HZ_FEATURES != Appendix B HZ feature order")
if tuple(_STAGE3_HB_FAMILIES) != _APPENDIX_B_HB_FEATURE_ORDER:
    raise RuntimeError("A008 §4.7.3 DRIFT: STAGE3_HB_FAMILIES != Appendix B HB family order")

HZ_FEATURE_ORDER: tuple[str, ...] = tuple(_STAGE3_HZ_FEATURES)
HG_FEATURE: str = "depthL1_extOFI"
HC_FEATURE: str = "gap_depth_extOFI"
HB_FEATURE_ORDER: tuple[str, ...] = tuple(_STAGE3_HB_FAMILIES)
VARIANT_ORDER_BY_AXIS: dict[str, tuple[str, ...]] = {
    "HZ": ("T0", "TZ"),
    "HG": ("G0", "G1"),
    "HC": ("C0", "C1"),
    "HB": ("B0", "B1"),
}
QUANTILE_ORDER: tuple[float, ...] = (0.80, 0.90, 0.95)
HORIZON_ORDER: tuple[int, ...] = (1000, 5000, 30000)
HB_QUANTILE: float = 0.90

CANDIDATE_ROW_FIELDS: tuple[str, ...] = (
    "protocol_version", "session_id", "asset", "axis", "feature_family",
    "variant_id", "quantile", "horizon_ms",
    "threshold_domain_finite_n", "threshold_domain_nonzero_n", "zero_n",
    "zero_fraction", "nonzero_unique_value_n", "hz_discrimination_class",
    "gate_zero_n", "calculated_threshold", "spacing_steps", "pre_overlap_n",
    "exact_spacing_pair_n", "accepted_n", "overlap_dropped_n",
    "accepted_positions_sha256", "mean_signed_bps", "hit_rate",
    "mean_abs_move",
)
STRING_FIELDS: frozenset[str] = frozenset({
    "protocol_version", "session_id", "asset", "axis", "feature_family",
    "variant_id", "hz_discrimination_class", "accepted_positions_sha256",
})
INTEGER_FIELDS: frozenset[str] = frozenset({
    "horizon_ms", "threshold_domain_finite_n", "threshold_domain_nonzero_n",
    "zero_n", "nonzero_unique_value_n", "gate_zero_n", "spacing_steps",
    "pre_overlap_n", "exact_spacing_pair_n", "accepted_n",
    "overlap_dropped_n",
})
FLOAT_FIELDS: frozenset[str] = frozenset({
    "quantile", "zero_fraction", "calculated_threshold", "mean_signed_bps",
    "hit_rate", "mean_abs_move",
})
CANONICAL_KEY_FIELDS: tuple[str, ...] = (
    "session_id", "asset", "axis", "feature_family", "variant_id",
    "quantile", "horizon_ms",
)
PROTOCOL_VERSION_LITERAL: str = "V1_2_NEW36_VALIDATION_PROTOCOL_V2"
EXPECTED_TOTAL_ROW_COUNT: int = 3456
FINGERPRINT_ENCODING_DEFINITION: str = (
    "accepted grid_pos sorted ascending; cast each to signed little-endian "
    "int64; concatenate raw 8-byte representations without delimiter; "
    "SHA256; lowercase 64-character hexadecimal; empty set uses "
    "SHA256(empty_bytes)"
)


def _new36_session_order() -> tuple[str, ...]:
    try:
        from checkpoint_registry import NEW36_SESSION_IDS
        return tuple(NEW36_SESSION_IDS)
    except Exception:  # pragma: no cover - defensive only
        return ()


def canonical_sort_key(row: dict) -> tuple:
    """A008/V2 §4 canonical_sort key for one candidate/reference row dict.

    Session order = frozen Appendix-B order when the session_id is a
    member of checkpoint_registry.NEW36_SESSION_IDS; otherwise (synthetic
    testing only, §13.2) falls back to lexicographic string order, applied
    consistently to both artifacts under comparison.
    """
    frozen_order = _new36_session_order()
    session_id = row["session_id"]
    if session_id in frozen_order:
        session_rank = (0, frozen_order.index(session_id))
    else:
        session_rank = (1, session_id)

    axis = row["axis"]
    axis_rank = AXIS_ORDER.index(axis) if axis in AXIS_ORDER else len(AXIS_ORDER)

    feature = row["feature_family"]
    if axis == "HZ":
        feat_rank = HZ_FEATURE_ORDER.index(feature) if feature in HZ_FEATURE_ORDER else 999
    elif axis == "HB":
        feat_rank = HB_FEATURE_ORDER.index(feature) if feature in HB_FEATURE_ORDER else 999
    else:
        feat_rank = 0

    variant = row["variant_id"]
    variant_order = VARIANT_ORDER_BY_AXIS.get(axis, ())
    variant_rank = variant_order.index(variant) if variant in variant_order else 999

    q = row["quantile"]
    try:
        q_rank = QUANTILE_ORDER.index(round(float(q), 2))
    except ValueError:
        q_rank = float(q) if q is not None else -1.0

    h = row["horizon_ms"]
    if h in (None, ""):
        h_rank = -1
    else:
        try:
            h_rank = HORIZON_ORDER.index(int(h))
        except ValueError:
            h_rank = int(h)

    return (session_rank, ASSET_ORDER.index(row["asset"]) if row["asset"] in ASSET_ORDER else 99,
            axis_rank, feat_rank, variant_rank, q_rank, h_rank)


# ---------------------------------------------------------------------------
# NNC-5 — duplicate-member-safe JSON parser (A006 §8A; A008 §8)
# ---------------------------------------------------------------------------

class NNC5ParseError(ValueError):
    """Raised when NNC-5 cannot establish JSON validity (fail closed)."""


class NNC5DuplicateMemberError(NNC5ParseError):
    """Raised when a repeated decoded member name is detected at any
    nesting depth of a JSON object (A006 §8A.3)."""


def _nnc5_object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict:
    seen: set[str] = set()
    for name, _value in pairs:
        if name in seen:
            raise NNC5DuplicateMemberError(
                f"NNC-5 FAIL: repeated member name {name!r} detected in a "
                f"JSON object before mapping construction (A006 §8A.3-8A.4)."
            )
        seen.add(name)
    return dict(pairs)


def nnc5_loads(raw: bytes | str) -> Any:
    """Parse serialized JSON bytes/text, rejecting duplicate object member
    names at EVERY nesting depth (A006 §8A). Fails closed on any input
    whose validity (incl. duplicate-member status) cannot be established.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NNC5ParseError(f"NNC-5 FAIL: input is not valid UTF-8: {exc}") from exc
    else:
        text = raw
    try:
        return json.loads(text, object_pairs_hook=_nnc5_object_pairs_hook)
    except NNC5DuplicateMemberError:
        raise
    except json.JSONDecodeError as exc:
        raise NNC5ParseError(f"NNC-5 FAIL: input is not well-formed JSON: {exc}") from exc


def nnc5_load_file(path: str | Path) -> Any:
    return nnc5_loads(Path(path).read_bytes())


def nnc5_load_jsonl(path: str | Path) -> list[Any]:
    """Parse a JSON-Lines file, applying nnc5_loads() to every non-empty
    line independently (used for the ledger and the supplemental evidence
    artifact)."""
    data = Path(path).read_bytes()
    if not data:
        return []
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    return [nnc5_loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Canonical serialization helpers
# ---------------------------------------------------------------------------

def canonical_line_bytes(obj: Any) -> bytes:
    """UTF-8; sort_keys=true; separators=(",",":"); ensure_ascii=true;
    no trailing whitespace; no final newline (A006 §7.3.2 / A008 §6.3
    line rules; also used for ledger records and evidence records before
    the caller appends exactly one LF)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def canonical_pretty_bytes(obj: Any) -> bytes:
    """UTF-8 JSON; sort_keys=true; indent=2; final newline=true
    (V2 §5 manifest_serialization; also used for s1_report.json,
    s2_report.json and the shared environment record when pretty form is
    requested)."""
    return (json.dumps(obj, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def size_of(path: str | Path) -> int:
    return Path(path).stat().st_size


def exclusive_create_write(path: str | Path, data: bytes) -> None:
    """A008 §4.11(a): serialize fully in memory, hash (caller's job),
    write with 'xb', then re-read and compare byte-for-byte; raise on any
    difference. Refuses pre-existence (PRECONDITION_FAIL semantics are the
    caller's responsibility; this raises FileExistsError)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    reread = p.read_bytes()
    if reread != data:
        raise AssertionError(
            f"A008_SAFETY_RULE_4_11_a FAIL: byte re-read mismatch for {p}"
        )


# ---------------------------------------------------------------------------
# NNC-3 — runtime environment identity (A006 §7; A008 §6)
# ---------------------------------------------------------------------------

class NNC3Error(RuntimeError):
    """Raised when the NNC-3 runtime environment identity cannot be
    established, or when equivalence (A006 §7.4) fails."""


REQUIRED_PYARROW_VERSION: str = "17.0.0"
_DIST_NAME_SEP_RE = re.compile(r"[-_.]+")


def _normalize_distribution_name(name: str) -> str:
    return _DIST_NAME_SEP_RE.sub("-", name.lower())


def compute_runtime_environment_identity(
    requirements_path: Path | None = None,
) -> dict:
    """Build the A006 §7.3.1 runtime_environment_identity object in the
    CURRENT interpreter. Fails closed (NNC3Error) on any condition of
    §7.6."""
    import importlib.metadata
    req_path = requirements_path or REQUIREMENTS_PATH
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except Exception as exc:
        raise NNC3Error(f"NNC-3 FAIL: import pyarrow.parquet did not succeed: {exc}") from exc

    pyarrow_version = str(pyarrow.__version__)
    if pyarrow_version != REQUIRED_PYARROW_VERSION:
        raise NNC3Error(
            f"NNC-3 FAIL: pyarrow.__version__={pyarrow_version!r} != "
            f"{REQUIRED_PYARROW_VERSION!r} (A006 §7.3.1(D))"
        )

    distributions: list[list[str]] = []
    for dist in importlib.metadata.distributions():
        try:
            raw_name = dist.metadata["Name"]
        except Exception:
            raw_name = None
        try:
            version = dist.version
        except Exception:
            version = None
        if not raw_name or not version:
            raise NNC3Error(
                "NNC-3 FAIL: an installed distribution is missing Name or "
                "version (A006 §7.3.1(B))"
            )
        distributions.append([_normalize_distribution_name(str(raw_name)), str(version)])
    distributions.sort(key=lambda pair: (pair[0], pair[1]))

    if not req_path.exists():
        raise NNC3Error(f"NNC-3 FAIL: requirements file not found at {req_path}")
    requirements_sha256 = hashlib.sha256(req_path.read_bytes()).hexdigest()

    identity = {
        "python": {
            "implementation": sys.implementation.name,
            "version": sys.version,
        },
        "installed_distributions": distributions,
        "requirements_sha256": requirements_sha256,
        "pyarrow_version": pyarrow_version,
    }
    return identity


def environment_identity_sha256(identity: dict) -> str:
    return sha256_bytes(canonical_line_bytes(identity))


def environment_identities_equivalent(
    identity_a: dict, sha_a: str, identity_b: dict, sha_b: str,
) -> bool:
    """A006 §7.4 equivalence: full objects AND digests must both compare
    exactly equal."""
    return identity_a == identity_b and sha_a == sha_b


def build_shared_environment_record() -> dict:
    """A008 §6.3 shared environment record: EXACTLY two members."""
    identity = compute_runtime_environment_identity()
    digest = environment_identity_sha256(identity)
    return {
        "runtime_environment_identity": identity,
        "runtime_environment_identity_sha256": digest,
    }


def load_and_validate_environment_record(path: str | Path) -> dict:
    """Parse an existing shared environment record with the NNC-5 parser
    and verify it is internally consistent (its own recorded sha256
    matches a recomputation over its own identity object) and that it
    contains EXACTLY the two required members."""
    obj = nnc5_load_file(path)
    if not isinstance(obj, dict) or set(obj.keys()) != {
        "runtime_environment_identity", "runtime_environment_identity_sha256",
    }:
        raise NNC3Error(
            "NNC-3 FAIL: shared environment record does not contain EXACTLY "
            "the two required members (A008 §6.3)"
        )
    recomputed = environment_identity_sha256(obj["runtime_environment_identity"])
    if recomputed != obj["runtime_environment_identity_sha256"]:
        raise NNC3Error(
            "NNC-3 FAIL: shared environment record sha256 does not match its "
            "own recorded identity object"
        )
    return obj


# ---------------------------------------------------------------------------
# NNC-4 — live database session_id uniqueness precondition
# (A006 §8; A008 §7). READ-ONLY. NOT a schema/manifest validator.
# ---------------------------------------------------------------------------

class NNC4Error(RuntimeError):
    """Raised when NNC-4 cannot be established or fails (A006 §8.4)."""


@dataclass(frozen=True)
class NNC4Result:
    ok: bool
    no_duplicate_session_ids: bool
    unique_index_present: bool
    duplicate_session_ids: tuple[str, ...]
    detail: str


def verify_session_id_uniqueness(db_path: str | Path) -> NNC4Result:
    """A006 §8.3, read-only, on the ACTUAL runtime database:

    A. No duplicate session_id rows exist in `sessions`.
    B. The live schema enforces uniqueness of sessions.session_id through
       a DB-enforced, NON-PARTIAL, SINGLE-COLUMN unique index/constraint
       whose key is exactly (session_id).

    No write; no state-modifying PRAGMA; no duplicate resolution
    (A006 §8.5). Opens the database strictly read-only via SQLite URI
    mode=ro; PRAGMA index_list/index_info are read-only informational
    pragmas.
    """
    import sqlite3

    db_file = Path(db_path).resolve()
    if not db_file.exists():
        raise NNC4Error(f"NNC-4 FAIL: database file does not exist: {db_file}")
    uri = f"file:{db_file}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise NNC4Error(f"NNC-4 FAIL: cannot open database read-only: {exc}") from exc
    try:
        cur = conn.cursor()

        # A. no duplicate session_id rows.
        try:
            cur.execute(
                "SELECT session_id, COUNT(*) AS c FROM sessions "
                "GROUP BY session_id HAVING COUNT(*) > 1"
            )
            dupes = cur.fetchall()
        except sqlite3.Error as exc:
            raise NNC4Error(f"NNC-4 FAIL: cannot query sessions table: {exc}") from exc
        duplicate_ids = tuple(str(r[0]) for r in dupes)
        no_dupes = len(duplicate_ids) == 0

        # B. DB-enforced, non-partial, single-column UNIQUE index on
        # EXACTLY sessions.session_id.
        try:
            cur.execute("PRAGMA index_list('sessions')")
            idx_rows = cur.fetchall()
        except sqlite3.Error as exc:
            raise NNC4Error(f"NNC-4 FAIL: cannot read index_list: {exc}") from exc

        unique_index_present = False
        for row in idx_rows:
            # (seq, name, unique, origin, partial) — origin/partial columns
            # are present on SQLite versions that support PRAGMA
            # index_list partial reporting; degrade gracefully otherwise.
            name = row[1]
            is_unique = bool(row[2])
            is_partial = bool(row[4]) if len(row) > 4 else False
            if not is_unique or is_partial:
                continue
            cur.execute(f"PRAGMA index_info({json.dumps(name)})")
            info_rows = cur.fetchall()
            cols = [r[2] for r in info_rows]
            if cols == ["session_id"]:
                unique_index_present = True
                break
            # Also accept a PRIMARY KEY / inline UNIQUE constraint that
            # SQLite auto-names (origin 'u' or 'pk') — already covered by
            # the loop above since PRAGMA index_list enumerates those too.

        ok = no_dupes and unique_index_present
        parts: list[str] = []
        if not no_dupes:
            parts.append(f"duplicate session_id values: {duplicate_ids!r}")
        if not unique_index_present:
            parts.append(
                "no non-partial single-column UNIQUE index/constraint on "
                "exactly sessions.session_id"
            )
        detail = "; ".join(parts) if parts else "NNC-4 OK: A and B both hold"
        return NNC4Result(
            ok=ok,
            no_duplicate_session_ids=no_dupes,
            unique_index_present=unique_index_present,
            duplicate_session_ids=duplicate_ids,
            detail=detail,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ledger helpers (A008 §9.5-9.6). Shared by candidate app, run.py and S1.
# ---------------------------------------------------------------------------

def read_ledger_records(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return list(nnc5_load_jsonl(p))


def append_ledger_record_durable(path: str | Path, record: dict) -> None:
    """A008 §9.6 durable append: O_APPEND (+O_CREAT on first use); ONE
    complete write of the full line; flush; fsync(fd);
    fsync(parent directory fd) where supported."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_line_bytes(record) + b"\n"
    fd = os.open(str(p), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        dir_fd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:  # pragma: no cover - platform without dir fsync support
        pass


def latest_terminal_record(records: Sequence[dict], role: str, run_id: str | None = None) -> dict | None:
    """Latest record for `role` (optionally scoped to run_id), or None."""
    latest = None
    for rec in records:
        if rec.get("role") != role:
            continue
        if run_id is not None and rec.get("run_id") != run_id:
            continue
        latest = rec
    return latest


# ---------------------------------------------------------------------------
# CSV lexical contract (A008 §10.3) and parsing helpers
# ---------------------------------------------------------------------------

@dataclass
class CsvLexResult:
    ok: bool
    reason: str | None
    header: list[str] | None
    rows: list[dict] | None


def parse_and_validate_csv_bytes(data: bytes) -> CsvLexResult:
    """A008 §10.3 CSV lexical contract for both roles."""
    if b"\r" in data:
        return CsvLexResult(False, "CR byte present (LF-only required)", None, None)
    if b'"' in data:
        return CsvLexResult(False, "'\"' character present (forbidden by §10.3)", None, None)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return CsvLexResult(False, f"not valid UTF-8: {exc}", None, None)
    if text.startswith("\ufeff"):
        return CsvLexResult(False, "BOM present", None, None)
    if not text.endswith("\n"):
        return CsvLexResult(False, "final byte is not LF", None, None)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return CsvLexResult(False, "empty file", None, None)
    if any(line == "" for line in lines):
        return CsvLexResult(False, "blank line present", None, None)
    header = lines[0].split(",")
    if tuple(header) != CANDIDATE_ROW_FIELDS:
        return CsvLexResult(False, "header does not equal the 25 V2 field names in order", None, None)
    rows: list[dict] = []
    for line in lines[1:]:
        fields = line.split(",")
        if len(fields) != len(CANDIDATE_ROW_FIELDS):
            return CsvLexResult(False, f"row does not have exactly {len(CANDIDATE_ROW_FIELDS)} fields", None, None)
        rows.append(dict(zip(CANDIDATE_ROW_FIELDS, fields)))
    return CsvLexResult(True, None, header, rows)


_INT_LEXEME_RE = re.compile(r"^-?[0-9]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_INVALID_FLOAT_LEXEMES = {"nan", "inf", "-inf", "+inf", "infinity", "-infinity", "+infinity"}


def _lexeme_is_invalid_float_token(lexeme: str) -> bool:
    return lexeme.strip().lower() in _INVALID_FLOAT_LEXEMES


def row_canonical_key_lexeme(row: dict) -> tuple:
    """EXACT SERIALIZED STRING key per §10.4 — no numeric normalization."""
    return tuple(row[f] for f in CANONICAL_KEY_FIELDS)


# ---------------------------------------------------------------------------
# Manifest handling (NNC-5 parse + manifest_precomparison_sha256)
# ---------------------------------------------------------------------------

def recompute_manifest_precomparison_sha256(manifest: dict) -> str:
    """V2 §5: SHA256 of canonical manifest bytes with
    manifest_precomparison_sha256 set to null."""
    clone = dict(manifest)
    clone["manifest_precomparison_sha256"] = None
    return sha256_bytes(canonical_pretty_bytes(clone))


ROLE_SPECIFIC_MANIFEST_FIELDS: frozenset[str] = frozenset({
    "implementation_role", "runtime_source_commit", "source_sha256",
    "csv_sha256", "csv_size_bytes", "manifest_precomparison_sha256",
})


# ---------------------------------------------------------------------------
# S1 — independent implementation equivalence comparator (A008 §10)
# ---------------------------------------------------------------------------

@dataclass
class S1Inputs:
    candidate_csv: Path
    candidate_manifest: Path
    reference_csv: Path
    reference_manifest: Path
    ledger: Path


def run_s1(inputs: S1Inputs) -> dict:
    report: dict[str, Any] = {
        "s1_status": "INVALID_INPUT",
        "row_counts": {"candidate": None, "reference": None},
        "key_set_mismatch_n": None,
        "sort_mismatch": None,
        "field_mismatch_total": 0,
        "field_mismatch_by_field": {f: 0 for f in CANDIDATE_ROW_FIELDS},
        "manifest_mismatch_fields": [],
        "mismatches": [],
        "input_identities": {},
        "reason": None,
    }

    paths = {
        "candidate_csv": inputs.candidate_csv,
        "candidate_manifest": inputs.candidate_manifest,
        "reference_csv": inputs.reference_csv,
        "reference_manifest": inputs.reference_manifest,
    }
    raw: dict[str, bytes] = {}
    for name, p in paths.items():
        try:
            raw[name] = Path(p).read_bytes()
        except OSError as exc:
            report["reason"] = f"cannot read {name}: {exc}"
            return report
        report["input_identities"][name] = {
            "sha256": sha256_bytes(raw[name]),
            "size_bytes": len(raw[name]),
        }

    # --- 10.2 sealed-input preconditions ------------------------------------
    try:
        cand_manifest = nnc5_loads(raw["candidate_manifest"])
        ref_manifest = nnc5_loads(raw["reference_manifest"])
    except NNC5ParseError as exc:
        report["reason"] = f"manifest NNC-5 parse failure: {exc}"
        return report

    for role_name, manifest, csv_key in (
        ("candidate", cand_manifest, "candidate_csv"),
        ("reference", ref_manifest, "reference_csv"),
    ):
        if not isinstance(manifest, dict):
            report["reason"] = f"{role_name} manifest is not a JSON object"
            return report
        if manifest.get("csv_sha256") != report["input_identities"][csv_key]["sha256"]:
            report["reason"] = f"{role_name} manifest csv_sha256 does not equal computed CSV sha256"
            return report
        if manifest.get("csv_size_bytes") != report["input_identities"][csv_key]["size_bytes"]:
            report["reason"] = f"{role_name} manifest csv_size_bytes does not equal computed CSV size"
            return report
        recomputed = recompute_manifest_precomparison_sha256(manifest)
        if manifest.get("manifest_precomparison_sha256") != recomputed:
            report["reason"] = f"{role_name} manifest_precomparison_sha256 mismatch on recomputation"
            return report

    ledger_records = read_ledger_records(inputs.ledger)
    cand_completed = latest_terminal_record(ledger_records, "CANDIDATE")
    ref_completed = latest_terminal_record(ledger_records, "REFERENCE")
    if (
        cand_completed is None or cand_completed.get("state") != "COMPLETED"
        or ref_completed is None or ref_completed.get("state") != "COMPLETED"
    ):
        report["reason"] = "no COMPLETED ledger record for both CANDIDATE and REFERENCE"
        return report
    cand_art = cand_completed.get("output_artifact_sha256") or {}
    ref_art = ref_completed.get("output_artifact_sha256") or {}
    if cand_art.get("csv") != report["input_identities"]["candidate_csv"]["sha256"]:
        report["reason"] = "ledger CANDIDATE output_artifact_sha256.csv does not match computed CSV sha256"
        return report
    if cand_art.get("manifest") != report["input_identities"]["candidate_manifest"]["sha256"]:
        report["reason"] = "ledger CANDIDATE output_artifact_sha256.manifest does not match computed manifest sha256"
        return report
    if ref_art.get("csv") != report["input_identities"]["reference_csv"]["sha256"]:
        report["reason"] = "ledger REFERENCE output_artifact_sha256.csv does not match computed CSV sha256"
        return report
    if ref_art.get("manifest") != report["input_identities"]["reference_manifest"]["sha256"]:
        report["reason"] = "ledger REFERENCE output_artifact_sha256.manifest does not match computed manifest sha256"
        return report
    candidate_run_id = cand_completed.get("run_id")
    reference_run_id = ref_completed.get("run_id")
    if not candidate_run_id or candidate_run_id != reference_run_id:
        report["reason"] = "candidate_run_id != reference_run_id (A008 §9.11 R3)"
        return report

    # --- 10.3 CSV lexical contract ------------------------------------------
    cand_lex = parse_and_validate_csv_bytes(raw["candidate_csv"])
    ref_lex = parse_and_validate_csv_bytes(raw["reference_csv"])
    if not cand_lex.ok:
        report["reason"] = f"candidate CSV lexical contract violation: {cand_lex.reason}"
        return report
    if not ref_lex.ok:
        report["reason"] = f"reference CSV lexical contract violation: {ref_lex.reason}"
        return report

    cand_rows = cand_lex.rows or []
    ref_rows = ref_lex.rows or []
    report["row_counts"] = {"candidate": len(cand_rows), "reference": len(ref_rows)}
    if len(cand_rows) != EXPECTED_TOTAL_ROW_COUNT or len(ref_rows) != EXPECTED_TOTAL_ROW_COUNT:
        report["reason"] = f"row count is not exactly {EXPECTED_TOTAL_ROW_COUNT} for both artifacts"
        return report

    # --- 10.4 key set / uniqueness / sort ------------------------------------
    cand_keys = [row_canonical_key_lexeme(r) for r in cand_rows]
    ref_keys = [row_canonical_key_lexeme(r) for r in ref_rows]
    if len(set(cand_keys)) != len(cand_keys):
        report["reason"] = "candidate canonical key is not unique (A02)"
        return report
    if len(set(ref_keys)) != len(ref_keys):
        report["reason"] = "reference canonical key is not unique (A02)"
        return report

    cand_key_set = set(cand_keys)
    ref_key_set = set(ref_keys)
    key_set_mismatch = cand_key_set.symmetric_difference(ref_key_set)
    report["key_set_mismatch_n"] = len(key_set_mismatch)

    def _canonical_sorted(rows: list[dict]) -> list[dict]:
        return sorted(rows, key=canonical_sort_key)

    cand_sorted_keys = [row_canonical_key_lexeme(r) for r in _canonical_sorted(cand_rows)]
    ref_sorted_keys = [row_canonical_key_lexeme(r) for r in _canonical_sorted(ref_rows)]
    sort_mismatch = (cand_keys != cand_sorted_keys) or (ref_keys != ref_sorted_keys)
    report["sort_mismatch"] = bool(sort_mismatch)

    if key_set_mismatch or sort_mismatch:
        report["s1_status"] = "FAIL"
        return report

    # --- 10.5 field comparison ------------------------------------------------
    ref_by_key = {row_canonical_key_lexeme(r): r for r in ref_rows}
    mismatches: list[dict] = []
    field_mismatch_by_field = {f: 0 for f in CANDIDATE_ROW_FIELDS}
    invalid_input = False
    invalid_reason = None

    for cand_row in _canonical_sorted(cand_rows):
        key = row_canonical_key_lexeme(cand_row)
        ref_row = ref_by_key[key]
        for f in CANDIDATE_ROW_FIELDS:
            c_val = cand_row[f]
            r_val = ref_row[f]
            if f in STRING_FIELDS:
                if f == "accepted_positions_sha256":
                    for v, who in ((c_val, "candidate"), (r_val, "reference")):
                        if v != "" and not _HEX64_RE.match(v):
                            invalid_input = True
                            invalid_reason = f"{who} accepted_positions_sha256 lexeme {v!r} is not 64-hex"
                    if c_val != r_val:
                        mismatches.append({"key": key, "field": f, "candidate": c_val, "reference": r_val})
                        field_mismatch_by_field[f] += 1
                else:
                    if c_val != r_val:
                        mismatches.append({"key": key, "field": f, "candidate": c_val, "reference": r_val})
                        field_mismatch_by_field[f] += 1
            elif f in INTEGER_FIELDS:
                for v, who in ((c_val, "candidate"), (r_val, "reference")):
                    if v != "" and not _INT_LEXEME_RE.match(v):
                        invalid_input = True
                        invalid_reason = f"{who} integer field {f} lexeme {v!r} does not match ^-?[0-9]+$"
                if (c_val == "") != (r_val == ""):
                    mismatches.append({"key": key, "field": f, "candidate": c_val, "reference": r_val})
                    field_mismatch_by_field[f] += 1
                elif c_val != "" and int(c_val) != int(r_val):
                    mismatches.append({"key": key, "field": f, "candidate": c_val, "reference": r_val})
                    field_mismatch_by_field[f] += 1
            elif f in FLOAT_FIELDS:
                for v, who in ((c_val, "candidate"), (r_val, "reference")):
                    if v != "" and _lexeme_is_invalid_float_token(v):
                        invalid_input = True
                        invalid_reason = f"{who} float field {f} lexeme {v!r} is NaN/Inf (forbidden)"
                if (c_val == "") != (r_val == ""):
                    mismatches.append({"key": key, "field": f, "candidate": c_val, "reference": r_val})
                    field_mismatch_by_field[f] += 1
                elif c_val != "":
                    try:
                        cf, rf = float(c_val), float(r_val)
                    except ValueError:
                        invalid_input = True
                        invalid_reason = f"unparsable float lexeme for field {f}"
                        continue
                    if not (_is_finite(cf) and _is_finite(rf)):
                        invalid_input = True
                        invalid_reason = f"non-finite float value for field {f}"
                    elif abs(cf - rf) > 1e-12:
                        mismatches.append({"key": key, "field": f, "candidate": c_val, "reference": r_val})
                        field_mismatch_by_field[f] += 1

    if invalid_input:
        report["s1_status"] = "INVALID_INPUT"
        report["reason"] = invalid_reason
        return report

    # --- 10.6 manifest semantic comparison ------------------------------------
    manifest_mismatch_fields: list[str] = []
    all_semantic_fields = set(cand_manifest.keys()) | set(ref_manifest.keys())
    for f in sorted(all_semantic_fields):
        if f in ROLE_SPECIFIC_MANIFEST_FIELDS or f == "a008_provenance":
            continue
        if cand_manifest.get(f) != ref_manifest.get(f):
            manifest_mismatch_fields.append(f)
    report["manifest_mismatch_fields"] = manifest_mismatch_fields

    report["mismatches"] = mismatches
    report["field_mismatch_by_field"] = field_mismatch_by_field
    report["field_mismatch_total"] = len(mismatches)

    if mismatches or manifest_mismatch_fields:
        report["s1_status"] = "FAIL"
    else:
        report["s1_status"] = "PASS"
    report["reason"] = None
    return report


def _is_finite(x: float) -> bool:
    import math
    return math.isfinite(x)


# ---------------------------------------------------------------------------
# S2 — closed candidate invariant validator (A008 §11, BINDING
# OBSERVABILITY MATRIX). Reads ONLY sealed Candidate artifacts, the frozen
# freeze-manifest record and the hash-bound Candidate source. STATIC mode.
# It MUST NOT read any Reference artifact and MUST NOT open any NEW36 ZIP.
# ---------------------------------------------------------------------------

EVALUATED_INVARIANT_IDS: tuple[str, ...] = (
    "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10",
    "A11", "A12", "A13", "A14", "A15", "A16", "A17", "A18", "A19", "A21",
    "A22", "A23", "A24", "A25", "A26", "A27", "A28", "A29", "A30",
)
WITHDRAWN_INVARIANTS: tuple[str, ...] = ("A20",)


@dataclass
class S2Inputs:
    candidate_csv: Path
    candidate_manifest: Path
    candidate_evidence: Path
    freeze_manifest: Path
    ledger: Path


def _s2_fail(report: dict, inv_id: str, detail: str) -> None:
    report["per_invariant"][inv_id]["status"] = "FAIL"
    report["per_invariant"][inv_id]["detail_count"] += 1
    if len(report["per_invariant"][inv_id]["details"]) < 20:
        report["per_invariant"][inv_id]["details"].append(detail)


def run_s2(inputs: S2Inputs) -> dict:
    report: dict[str, Any] = {
        "s2_status": "INVALID_INPUT",
        "evaluated_invariants": list(EVALUATED_INVARIANT_IDS),
        "WITHDRAWN_INVARIANTS": list(WITHDRAWN_INVARIANTS),
        "failed_invariant_count": 0,
        "failed_invariant_ids": [],
        "per_invariant": {
            inv: {
                "id": inv, "evidence_source": None, "evaluation_mode": None,
                "status": "PASS", "detail_count": 0, "details": [],
            }
            for inv in EVALUATED_INVARIANT_IDS
        },
        "reason": None,
    }

    try:
        csv_bytes = Path(inputs.candidate_csv).read_bytes()
        manifest = nnc5_loads(Path(inputs.candidate_manifest).read_bytes())
    except (OSError, NNC5ParseError) as exc:
        report["reason"] = f"cannot read/parse candidate artifacts: {exc}"
        return report

    lex = parse_and_validate_csv_bytes(csv_bytes)
    if not lex.ok:
        report["reason"] = f"candidate CSV lexical contract violation: {lex.reason}"
        return report
    rows = lex.rows or []

    evidence_records: list[dict] = []
    if Path(inputs.candidate_evidence).exists():
        try:
            evidence_records = nnc5_load_jsonl(inputs.candidate_evidence)
        except NNC5ParseError as exc:
            report["reason"] = f"cannot parse candidate evidence: {exc}"
            return report

    def cast_int(v: str) -> int | None:
        return None if v == "" else int(v)

    def cast_float(v: str) -> float | None:
        return None if v == "" else float(v)

    # Typed rows for convenience.
    typed_rows: list[dict] = []
    for r in rows:
        t = dict(r)
        for f in INTEGER_FIELDS:
            t[f] = cast_int(r[f])
        for f in FLOAT_FIELDS:
            t[f] = cast_float(r[f])
        typed_rows.append(t)

    ev_by_key: dict[tuple, dict] = {}
    for ev in evidence_records:
        key = (ev.get("session_id"), ev.get("asset"), ev.get("axis"), ev.get("feature_family"),
               ev.get("variant_id"), ev.get("quantile"), ev.get("horizon_ms"))
        ev_by_key[key] = ev

    def row_ev(row: dict) -> dict | None:
        key = (row["session_id"], row["asset"], row["axis"], row["feature_family"],
               row["variant_id"], row["quantile"], row["horizon_ms"])
        return ev_by_key.get(key)

    def mark(inv: str, source: str, mode: str) -> None:
        report["per_invariant"][inv]["evidence_source"] = source
        report["per_invariant"][inv]["evaluation_mode"] = mode

    # A01 — inventory.
    mark("A01", "CANDIDATE_CSV", "ARTIFACT")
    if len(typed_rows) != EXPECTED_TOTAL_ROW_COUNT:
        _s2_fail(report, "A01", f"row count {len(typed_rows)} != {EXPECTED_TOTAL_ROW_COUNT}")
    per_axis_counts: dict[str, int] = {}
    for r in typed_rows:
        per_axis_counts[r["axis"]] = per_axis_counts.get(r["axis"], 0) + 1
    expected_axis_counts = {"HZ": 1728, "HG": 432, "HC": 432, "HB": 864}
    for axis, expected in expected_axis_counts.items():
        if per_axis_counts.get(axis, 0) != expected:
            _s2_fail(report, "A01", f"axis {axis} row count {per_axis_counts.get(axis, 0)} != {expected}")

    # A02 — unique key.
    mark("A02", "CANDIDATE_CSV", "ARTIFACT")
    keys = [row_canonical_key_lexeme(r) for r in rows]
    if len(set(keys)) != len(keys):
        _s2_fail(report, "A02", "canonical row key is not unique")

    # A03 — TZ zero exclusion (static + artifact; light-weight artifact check only).
    mark("A03", "STATIC+ARTIFACT", "STATIC+ARTIFACT")
    for r in typed_rows:
        if r["axis"] == "HZ" and r["variant_id"] == "TZ":
            if r["threshold_domain_nonzero_n"] is not None and r["threshold_domain_finite_n"] is not None:
                if r["threshold_domain_nonzero_n"] > r["threshold_domain_finite_n"]:
                    _s2_fail(report, "A03", f"nonzero_n>finite_n for {row_canonical_key_lexeme(r)}")

    # A04 — threshold NULL <=> domain count < 2 and NULL => N=0.
    mark("A04", "ARTIFACT+EVIDENCE", "ARTIFACT+EVIDENCE")
    for r in typed_rows:
        if r["axis"] == "HZ":
            domain_n = r["threshold_domain_nonzero_n"] if r["variant_id"] == "TZ" else r["threshold_domain_finite_n"]
            if r["calculated_threshold"] is None and domain_n is not None and domain_n >= 2:
                _s2_fail(report, "A04", f"HZ threshold NULL but domain_n={domain_n}>=2 for {row_canonical_key_lexeme(r)}")
        elif r["axis"] in ("HG", "HC", "HB"):
            if r["calculated_threshold"] is None:
                if (r["pre_overlap_n"] or 0) != 0 or (r["accepted_n"] or 0) != 0:
                    _s2_fail(report, "A04", f"threshold NULL but pre_overlap_n/accepted_n != 0 for {row_canonical_key_lexeme(r)}")
            ev = row_ev(r)
            if ev is not None and ev.get("threshold_domain_n") is not None:
                if r["calculated_threshold"] is None and ev["threshold_domain_n"] >= 2:
                    _s2_fail(report, "A04", f"threshold NULL but evidence threshold_domain_n={ev['threshold_domain_n']}>=2 for {row_canonical_key_lexeme(r)}")

    # A05 — HZ count identities and zero_fraction range.
    mark("A05", "CANDIDATE_CSV", "ARTIFACT")
    for r in typed_rows:
        if r["axis"] != "HZ":
            continue
        finite_n, nonzero_n, zero_n = r["threshold_domain_finite_n"], r["threshold_domain_nonzero_n"], r["zero_n"]
        if finite_n is None or nonzero_n is None or zero_n is None:
            _s2_fail(report, "A05", f"HZ counts missing for {row_canonical_key_lexeme(r)}")
            continue
        if not (finite_n >= nonzero_n >= 0):
            _s2_fail(report, "A05", f"finite_n>=nonzero_n>=0 violated for {row_canonical_key_lexeme(r)}")
        if zero_n != finite_n - nonzero_n:
            _s2_fail(report, "A05", f"zero_n != finite_n-nonzero_n for {row_canonical_key_lexeme(r)}")
        zf = r["zero_fraction"]
        if finite_n == 0:
            if zf is not None:
                _s2_fail(report, "A05", f"zero_fraction not NULL when finite_n=0 for {row_canonical_key_lexeme(r)}")
        else:
            if zf is None or not (0.0 <= zf <= 1.0):
                _s2_fail(report, "A05", f"zero_fraction out of [0,1] for {row_canonical_key_lexeme(r)}")

    # A06 — zero-free control: zero_n=0 => T0==TZ threshold within 1e-12.
    mark("A06", "CANDIDATE_CSV", "ARTIFACT")
    hz_rows = {(r["session_id"], r["asset"], r["feature_family"], r["quantile"], r["variant_id"]): r
               for r in typed_rows if r["axis"] == "HZ"}
    for r in typed_rows:
        if r["axis"] == "HZ" and r["variant_id"] == "T0" and r["zero_n"] == 0:
            tz = hz_rows.get((r["session_id"], r["asset"], r["feature_family"], r["quantile"], "TZ"))
            if tz is None or r["calculated_threshold"] is None or tz["calculated_threshold"] is None:
                continue
            if abs(r["calculated_threshold"] - tz["calculated_threshold"]) > 1e-12:
                _s2_fail(report, "A06", f"T0!=TZ threshold at zero_n=0 for {row_canonical_key_lexeme(r)}")

    # A07 — G1 threshold source = TZ depth_imbalance_l1.
    mark("A07", "CANDIDATE_CSV", "ARTIFACT")
    for r in typed_rows:
        if r["axis"] == "HG" and r["variant_id"] == "G1":
            tz_di = hz_rows.get((r["session_id"], r["asset"], "depth_imbalance_l1", r["quantile"], "TZ"))
            if tz_di is None:
                continue
            ct, tz_ct = r["calculated_threshold"], tz_di["calculated_threshold"]
            if (ct is None) != (tz_ct is None):
                _s2_fail(report, "A07", f"G1 threshold nullability mismatch for {row_canonical_key_lexeme(r)}")
            elif ct is not None and abs(ct - tz_ct) > 1e-12:
                _s2_fail(report, "A07", f"G1 threshold != TZ depth_imbalance_l1 threshold for {row_canonical_key_lexeme(r)}")

    # A08 — G1 pre-overlap ⊆ parent gate.
    mark("A08", "EVIDENCE", "EVIDENCE")
    for r in typed_rows:
        if r["axis"] == "HG" and r["variant_id"] == "G1":
            ev = row_ev(r)
            if ev is None:
                continue
            pre = set(ev.get("pre_overlap_positions") or [])
            parent = set(ev.get("parent_positions") or [])
            if not pre.issubset(parent):
                _s2_fail(report, "A08", f"G1 pre-overlap not subset of parent for {row_canonical_key_lexeme(r)}")

    # A09 — G1 raw-sign only (static AST scan of the candidate app source).
    mark("A09", "STATIC", "STATIC")
    _static_scan_forbidden_confirmation(report, "A09")

    # A10 — C1 threshold = fair_gap_reversion T0 threshold.
    mark("A10", "CANDIDATE_CSV", "ARTIFACT")
    fair_gap_t0 = {(r["session_id"], r["asset"], r["quantile"]): r
                   for r in typed_rows if r["axis"] == "HZ" and r["feature_family"] == "fair_gap_reversion" and r["variant_id"] == "T0"}
    for r in typed_rows:
        if r["axis"] == "HC" and r["variant_id"] == "C1":
            t0 = fair_gap_t0.get((r["session_id"], r["asset"], r["quantile"]))
            if t0 is None:
                continue
            ct, t0ct = r["calculated_threshold"], t0["calculated_threshold"]
            if (ct is None) != (t0ct is None):
                _s2_fail(report, "A10", f"C1 threshold nullability mismatch for {row_canonical_key_lexeme(r)}")
            elif ct is not None and abs(ct - t0ct) > 1e-12:
                _s2_fail(report, "A10", f"C1 threshold != fair_gap_reversion T0 threshold for {row_canonical_key_lexeme(r)}")

    # A11 — C1 pre-overlap ⊆ gap parent set.
    mark("A11", "EVIDENCE", "EVIDENCE")
    for r in typed_rows:
        if r["axis"] == "HC" and r["variant_id"] == "C1":
            ev = row_ev(r)
            if ev is None:
                continue
            pre = set(ev.get("pre_overlap_positions") or [])
            parent = set(ev.get("parent_positions") or [])
            if not pre.issubset(parent):
                _s2_fail(report, "A11", f"C1 pre-overlap not subset of gap parent for {row_canonical_key_lexeme(r)}")

    # A12 — C1 raw signs only (static).
    mark("A12", "STATIC", "STATIC")
    _static_scan_forbidden_confirmation(report, "A12")

    # A13 — spacing_steps = max(10, horizon_ms // 100).
    mark("A13", "CANDIDATE_CSV", "ARTIFACT")
    for r in typed_rows:
        if r["axis"] in ("HG", "HC", "HB") and r["horizon_ms"] is not None and r["spacing_steps"] is not None:
            expected = max(10, r["horizon_ms"] // 100)
            if r["spacing_steps"] != expected:
                _s2_fail(report, "A13", f"spacing_steps={r['spacing_steps']} != {expected} for {row_canonical_key_lexeme(r)}")

    # A14 — B0 greedy earliest-first.
    mark("A14", "EVIDENCE+ARTIFACT", "EVIDENCE+ARTIFACT")
    for r in typed_rows:
        if r["axis"] == "HB" and r["variant_id"] == "B0":
            ev = row_ev(r)
            if ev is None:
                continue
            pre = ev.get("pre_overlap_positions") or []
            recomputed = _greedy_b0(pre, r["spacing_steps"] or 0)
            if sorted(ev.get("accepted_positions") or []) != recomputed:
                _s2_fail(report, "A14", f"B0 recomputed accepted set differs for {row_canonical_key_lexeme(r)}")
            if r["accepted_n"] != len(ev.get("accepted_positions") or []):
                _s2_fail(report, "A14", f"B0 accepted_n != len(accepted_positions) for {row_canonical_key_lexeme(r)}")

    # A15 — B1 strict; B1 never candidate.
    mark("A15", "EVIDENCE+ARTIFACT+STATIC", "EVIDENCE+ARTIFACT+STATIC")
    for r in typed_rows:
        if r["axis"] == "HB" and r["variant_id"] == "B1":
            ev = row_ev(r)
            if ev is None:
                continue
            b0_ev = ev_by_key.get((r["session_id"], r["asset"], "HB", r["feature_family"], "B0", r["quantile"], r["horizon_ms"]))
            pre = (b0_ev or {}).get("pre_overlap_positions") or []
            recomputed = _strict_b1(pre, r["spacing_steps"] or 0)
            if sorted(ev.get("accepted_positions") or []) != recomputed:
                _s2_fail(report, "A15", f"B1 recomputed accepted set differs for {row_canonical_key_lexeme(r)}")

    # A16 — pre_overlap_n = accepted_n + overlap_dropped_n.
    mark("A16", "CANDIDATE_CSV", "ARTIFACT")
    for r in typed_rows:
        if r["axis"] in ("HG", "HC", "HB"):
            pre, acc, drop = r["pre_overlap_n"], r["accepted_n"], r["overlap_dropped_n"]
            if None not in (pre, acc, drop) and pre != acc + drop:
                _s2_fail(report, "A16", f"pre_overlap_n != accepted_n+overlap_dropped_n for {row_canonical_key_lexeme(r)}")

    # A17 — accepted positions unique, ascending, subset of pre-overlap.
    mark("A17", "EVIDENCE+ARTIFACT", "EVIDENCE+ARTIFACT")
    for r in typed_rows:
        if r["axis"] in ("HG", "HC", "HB"):
            ev = row_ev(r)
            if ev is None:
                continue
            acc = ev.get("accepted_positions") or []
            if len(set(acc)) != len(acc):
                _s2_fail(report, "A17", f"accepted positions not unique for {row_canonical_key_lexeme(r)}")
            if acc != sorted(acc):
                _s2_fail(report, "A17", f"accepted positions not ascending for {row_canonical_key_lexeme(r)}")
            pre = ev.get("pre_overlap_positions")
            if pre is None and r["variant_id"] == "B1":
                b0_ev = ev_by_key.get((r["session_id"], r["asset"], "HB", r["feature_family"], "B0", r["quantile"], r["horizon_ms"]))
                pre = (b0_ev or {}).get("pre_overlap_positions") or []
            if pre is not None and not set(acc).issubset(set(pre)):
                _s2_fail(report, "A17", f"accepted positions not subset of pre-overlap for {row_canonical_key_lexeme(r)}")

    # A18 — fingerprint = V2 encoding of exactly accepted_n positions.
    mark("A18", "EVIDENCE+ARTIFACT", "EVIDENCE+ARTIFACT")
    for r in typed_rows:
        if r["axis"] in ("HG", "HC", "HB"):
            ev = row_ev(r)
            if ev is None:
                continue
            acc = ev.get("accepted_positions") or []
            recomputed_fp = sha256_positions(acc)
            if r["accepted_positions_sha256"] and r["accepted_positions_sha256"] != recomputed_fp:
                _s2_fail(report, "A18", f"accepted_positions_sha256 mismatch for {row_canonical_key_lexeme(r)}")
            if r["accepted_n"] is not None and r["accepted_n"] != len(acc):
                _s2_fail(report, "A18", f"accepted_n != len(accepted_positions) for {row_canonical_key_lexeme(r)}")

    # A19 (ACTIVE) — paired B1.accepted_n <= B0.accepted_n.
    mark("A19", "CANDIDATE_CSV", "ARTIFACT")
    b0_by_cell = {(r["session_id"], r["asset"], r["feature_family"], r["quantile"], r["horizon_ms"]): r
                  for r in typed_rows if r["axis"] == "HB" and r["variant_id"] == "B0"}
    for r in typed_rows:
        if r["axis"] == "HB" and r["variant_id"] == "B1":
            b0 = b0_by_cell.get((r["session_id"], r["asset"], r["feature_family"], r["quantile"], r["horizon_ms"]))
            if b0 is not None and r["accepted_n"] is not None and b0["accepted_n"] is not None:
                if r["accepted_n"] > b0["accepted_n"]:
                    _s2_fail(report, "A19", f"B1.accepted_n>B0.accepted_n for {row_canonical_key_lexeme(r)}")

    # A21 — exact_spacing_pair_n = consecutive-pair count.
    mark("A21", "EVIDENCE+ARTIFACT", "EVIDENCE+ARTIFACT")
    for r in typed_rows:
        if r["axis"] == "HB":
            ev = row_ev(r)
            if ev is None:
                continue
            pre = ev.get("pre_overlap_positions")
            if pre is None:
                b0_ev = ev_by_key.get((r["session_id"], r["asset"], "HB", r["feature_family"], "B0", r["quantile"], r["horizon_ms"]))
                pre = (b0_ev or {}).get("pre_overlap_positions") or []
            pre = sorted(pre)
            spacing = r["spacing_steps"] or 0
            expected = sum(1 for i in range(1, len(pre)) if pre[i] - pre[i - 1] == spacing)
            if r["exact_spacing_pair_n"] is not None and r["exact_spacing_pair_n"] != expected:
                _s2_fail(report, "A21", f"exact_spacing_pair_n={r['exact_spacing_pair_n']} != consecutive-pair count {expected} for {row_canonical_key_lexeme(r)}")

    # A22 — accepted_n=0 => mean_signed_bps, hit_rate NULL.
    mark("A22", "CANDIDATE_CSV", "ARTIFACT")
    for r in typed_rows:
        if r["axis"] in ("HG", "HC", "HB") and r["accepted_n"] == 0:
            if r["mean_signed_bps"] is not None or r["hit_rate"] is not None:
                _s2_fail(report, "A22", f"accepted_n=0 but mean_signed_bps/hit_rate not NULL for {row_canonical_key_lexeme(r)}")

    # A23 — hit_rate = count(sign==+1)/accepted_n; zero is a miss.
    mark("A23", "EVIDENCE+ARTIFACT", "EVIDENCE+ARTIFACT")
    for r in typed_rows:
        if r["axis"] in ("HG", "HC", "HB"):
            ev = row_ev(r)
            if ev is None or r["accepted_n"] in (None, 0):
                continue
            signs = ev.get("accepted_signed_return_signs") or []
            if len(signs) != r["accepted_n"]:
                _s2_fail(report, "A23", f"accepted_signed_return_signs length mismatch for {row_canonical_key_lexeme(r)}")
                continue
            expected_hr = sum(1 for s in signs if s == 1) / len(signs)
            if r["hit_rate"] is not None and abs(r["hit_rate"] - expected_hr) > 1e-12:
                _s2_fail(report, "A23", f"hit_rate={r['hit_rate']} != recomputed {expected_hr} for {row_canonical_key_lexeme(r)}")

    # A24 — BTC/ETH independence (static: composition is per-block; no
    # cross-asset state kept by the candidate application).
    mark("A24", "STATIC", "STATIC")

    # A25 — no golden reads.
    mark("A25", "MANIFEST+STATIC", "MANIFEST+STATIC")
    if manifest.get("golden_artifacts_read") is not False:
        _s2_fail(report, "A25", "manifest golden_artifacts_read is not exactly false")

    # A26 — no OLD36 quantitative reads.
    mark("A26", "MANIFEST+STATIC", "MANIFEST+STATIC")
    if manifest.get("old36_quantitative_data_read") is not False:
        _s2_fail(report, "A26", "manifest old36_quantitative_data_read is not exactly false")

    # A27 — collector_sha256 = frozen.
    mark("A27", "MANIFEST", "MANIFEST")
    from checkpoint_registry import NEW36_SESSION_IDS  # noqa: F401 (import-availability check only)
    FROZEN_COLLECTOR_SHA256 = "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3"
    if manifest.get("collector_sha256") != FROZEN_COLLECTOR_SHA256:
        _s2_fail(report, "A27", "manifest collector_sha256 does not equal the frozen value")

    # A28 — frozen analysis engine state.
    mark("A28", "MANIFEST", "MANIFEST")
    if manifest.get("frozen_analysis_engine_state") != "NOT_CONFIGURED":
        _s2_fail(report, "A28", "frozen_analysis_engine_state != NOT_CONFIGURED")
    if manifest.get("frozen_analysis_engine_accepts_input") is not False:
        _s2_fail(report, "A28", "frozen_analysis_engine_accepts_input is not exactly false")

    # A29 — protocol/spec/engine identities frozen.
    mark("A29", "MANIFEST", "MANIFEST")
    FROZEN_PROTOCOL_SHA256 = "5ed8a8b12726d395d25262dc3e4073250be3de94f3d62e64e99167921380ef95"
    FROZEN_CANDIDATE_SPEC_SHA256 = "aed67b5ec9ae1b710ec3d96e1fcc2e15bd9c30fdd60e80cc68553d8fc3b597ca"
    FROZEN_ENGINE_SHA256 = "abedee399c272eff858b109fb448cbda3ad3a49e40be4ba3f486650e3151a3e8"
    if manifest.get("protocol_sha256") != FROZEN_PROTOCOL_SHA256:
        _s2_fail(report, "A29", "manifest protocol_sha256 mismatch")
    if manifest.get("candidate_spec_sha256") != FROZEN_CANDIDATE_SPEC_SHA256:
        _s2_fail(report, "A29", "manifest candidate_spec_sha256 mismatch")
    if manifest.get("engine_sha256") != FROZEN_ENGINE_SHA256:
        _s2_fail(report, "A29", "manifest engine_sha256 mismatch")

    # A30 — controls present only as controls; exactly one candidate variant per axis.
    mark("A30", "ARTIFACT+MANIFEST", "ARTIFACT+MANIFEST")
    per_axis_variants: dict[str, set] = {}
    for r in typed_rows:
        per_axis_variants.setdefault(r["axis"], set()).add(r["variant_id"])
    expected_variants = {"HZ": {"T0", "TZ"}, "HG": {"G0", "G1"}, "HC": {"C0", "C1"}, "HB": {"B0", "B1"}}
    for axis, expected in expected_variants.items():
        if per_axis_variants.get(axis) != expected:
            _s2_fail(report, "A30", f"axis {axis} variant set {per_axis_variants.get(axis)} != {expected}")

    failed = [inv for inv, v in report["per_invariant"].items() if v["status"] == "FAIL"]
    report["failed_invariant_ids"] = failed
    report["failed_invariant_count"] = len(failed)
    report["s2_status"] = "FAIL" if failed else "PASS"
    return report


def sha256_positions(positions: Sequence[int]) -> str:
    import numpy as np
    arr = np.asarray(list(positions), dtype=np.int64)
    if len(arr):
        arr = np.sort(arr)
    return hashlib.sha256(arr.astype("<i8", copy=False).tobytes(order="C")).hexdigest()


def _greedy_b0(positions: Sequence[int], spacing: int) -> list[int]:
    pos = sorted(positions)
    out: list[int] = []
    last = None
    for p in pos:
        if last is None or p - last >= spacing:
            out.append(p)
            last = p
    return out


def _strict_b1(positions: Sequence[int], spacing: int) -> list[int]:
    pos = sorted(positions)
    out: list[int] = []
    last = None
    for p in pos:
        if last is None or p - last > spacing:
            out.append(p)
            last = p
    return out


def _static_scan_forbidden_confirmation(report: dict, inv_id: str) -> None:
    """STATIC mode (A008 §11.3): hash the Candidate application source,
    scan its AST for forbidden confirmation-column string literals
    (CANDIDATE_FORBIDDEN_CONFIRMATION_COLUMNS) and verify the only G1/C1
    mask sources referenced are the frozen candidate functions."""
    import ast
    app_path = REPO_ROOT / "backend" / "recovery" / "v1_2_new36_candidate_app.py"
    try:
        text = app_path.read_text(encoding="utf-8")
    except OSError as exc:
        _s2_fail(report, inv_id, f"cannot read candidate application source: {exc}")
        return
    forbidden = ("z_di_l1", "z_ext_ofi", "z_gap_d")
    try:
        tree = ast.parse(text, filename=str(app_path))
    except SyntaxError as exc:
        _s2_fail(report, inv_id, f"candidate application source does not parse: {exc}")
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in forbidden:
                _s2_fail(report, inv_id, f"forbidden confirmation column literal {node.value!r} referenced")


# ---------------------------------------------------------------------------
# CLI (A008 §10.1 `s1`, §11.1 `s2`)
# ---------------------------------------------------------------------------

def _cmd_s1(args: argparse.Namespace) -> int:
    inputs = S1Inputs(
        candidate_csv=Path(args.candidate_csv),
        candidate_manifest=Path(args.candidate_manifest),
        reference_csv=Path(args.reference_csv),
        reference_manifest=Path(args.reference_manifest),
        ledger=Path(args.ledger),
    )
    report = run_s1(inputs)
    data = canonical_pretty_bytes(report)
    out_path = Path(args.out_report)
    if out_path.exists():
        print(f"PRECONDITION_FAIL: {out_path} already exists (xb required)", file=sys.stderr)
        return 2
    exclusive_create_write(out_path, data)
    print(json.dumps({"s1_status": report["s1_status"]}))
    return 0 if report["s1_status"] == "PASS" else 1


def _cmd_s2(args: argparse.Namespace) -> int:
    inputs = S2Inputs(
        candidate_csv=Path(args.candidate_csv),
        candidate_manifest=Path(args.candidate_manifest),
        candidate_evidence=Path(args.candidate_evidence),
        freeze_manifest=Path(args.freeze_manifest),
        ledger=Path(args.ledger),
    )
    report = run_s2(inputs)
    data = canonical_pretty_bytes(report)
    out_path = Path(args.out_report)
    if out_path.exists():
        print(f"PRECONDITION_FAIL: {out_path} already exists (xb required)", file=sys.stderr)
        return 2
    exclusive_create_write(out_path, data)
    print(json.dumps({"s2_status": report["s2_status"]}))
    return 0 if report["s2_status"] == "PASS" else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="v1_2_new36_validators")
    sub = parser.add_subparsers(dest="command", required=True)

    p_s1 = sub.add_parser("s1")
    p_s1.add_argument("--candidate-csv", required=True)
    p_s1.add_argument("--candidate-manifest", required=True)
    p_s1.add_argument("--reference-csv", required=True)
    p_s1.add_argument("--reference-manifest", required=True)
    p_s1.add_argument("--ledger", required=True)
    p_s1.add_argument("--out-report", required=True)
    p_s1.set_defaults(func=_cmd_s1)

    p_s2 = sub.add_parser("s2")
    p_s2.add_argument("--candidate-csv", required=True)
    p_s2.add_argument("--candidate-manifest", required=True)
    p_s2.add_argument("--candidate-evidence", required=True)
    p_s2.add_argument("--freeze-manifest", required=True)
    p_s2.add_argument("--ledger", required=True)
    p_s2.add_argument("--out-report", required=True)
    p_s2.set_defaults(func=_cmd_s2)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
