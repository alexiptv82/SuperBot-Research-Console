"""SECTION 16 STEP 4 — CANDIDATE NEW36 APPLICATION (A008 DRAFT4 §4).

backend/recovery/v1_2_new36_candidate_app.py — the "Candidate application"
named in A008 DRAFT4 §0.1. A COMPOSITION/ORCHESTRATION layer only (§4.1):
it defines no threshold, domain predicate, mask, filter, direction, metric,
fingerprint or count formula of its own. Every canonical field is obtained
by calling an AUTHORIZED existing implementation named in the §4.7 binding
table.

Invocation (explicit only; no import-time side effects; no FastAPI route):

    python -m recovery.v1_2_new36_candidate_app preflight
        --db <sqlite path> --environment-record <path>
        --session-zip-map <json path>
    python -m recovery.v1_2_new36_candidate_app generate
        --db <sqlite path> --environment-record <path>
        --session-zip-map <json path>
        --out-csv <path> --out-manifest <path> --out-evidence <path>
        --run-id <uuid> --ledger <path>

CLEAN-ROOM: authored EXCLUSIVELY from A008 DRAFT4 §5.2 permitted inputs.
No path under backend/recovery/reference/ is imported, opened, read,
executed or diffed anywhere in this module.

PROHIBITED IN THIS SESSION (Section 16 STEP 4 scope): this module MUST NOT
be invoked against real NEW36 data by the authoring agent. Its `generate`
gates (P-01..P-10) are fail-closed and will legitimately return
PRECONDITION_FAIL today because the Section 16 step 6 freeze record
(V1_2_CANDIDATE_IMPLEMENTATION_FREEZE_MANIFEST.txt) does not exist yet
(gate P-03) — this is the CORRECT state prior to Section 16 steps 5-9.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import v1_2_candidate as candidate
from . import v1_2_diagnostics as diagnostics
from . import v1_2_stage3 as stage3
from .allowlist import assert_recovery_allowed
from .engine import GRID_MS, _build_forward_return_array, _quantile_type7
from .v1_2_new36_validators import (
    CANDIDATE_ROW_FIELDS,
    EXPECTED_TOTAL_ROW_COUNT,
    FINGERPRINT_ENCODING_DEFINITION,
    HB_QUANTILE,
    HORIZON_ORDER,
    PROTOCOL_VERSION_LITERAL,
    QUANTILE_ORDER,
    NNC3Error,
    NNC4Error,
    NNC5ParseError,
    append_ledger_record_durable,
    build_shared_environment_record,
    canonical_line_bytes,
    canonical_pretty_bytes,
    canonical_sort_key,
    compute_runtime_environment_identity,
    environment_identities_equivalent,
    environment_identity_sha256,
    exclusive_create_write,
    load_and_validate_environment_record,
    nnc5_load_file,
    read_ledger_records,
    row_canonical_key_lexeme,
    sha256_bytes,
    sha256_file,
    sha256_positions,
    size_of,
    verify_session_id_uniqueness,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Frozen protocol / amendment identities (A008 DRAFT4 §0.2-0.4, §2.2)
# ---------------------------------------------------------------------------

_SPEC_DIR = REPO_ROOT / "backend" / "recovery" / "specs"

FROZEN_PROTOCOL_IDENTITIES: tuple[tuple[Path, int, str], ...] = (
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2.txt", 19392,
     "5ed8a8b12726d395d25262dc3e4073250be3de94f3d62e64e99167921380ef95"),
    (_SPEC_DIR / "V1_2_CANDIDATE_SPEC.txt", 6738,
     "aed67b5ec9ae1b710ec3d96e1fcc2e15bd9c30fdd60e80cc68553d8fc3b597ca"),
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2_AMENDMENT_001.txt", 13858,
     "e64f1198e3f97f975d1f0f8ec7183b1f0c83786429e98cbd66819d98f640f0bf"),
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2_AMENDMENT_002.txt", 7236,
     "9a335589a44df68de56804bce09d122308f01ba95403bcb10a764c0b3461df57"),
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2_AMENDMENT_003.txt", 5209,
     "cee61f2ea2b5d592deddb0fa6d2215c77039c365006ecb0bc7caf1d32075bea7"),
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2_AMENDMENT_004.txt", 21467,
     "62830b779ed77cabc6afdc0ac8cea4b9013871feee65b03aa73d7757c88197b4"),
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2_AMENDMENT_005.txt", 29853,
     "0c2dc05bc37fc392f67b45fd8f7e3576b49be9d764ffca4be0679db22b8d2a00"),
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2_AMENDMENT_006.txt", 61911,
     "a8610b5dc3328c164bceb91921ee9a658bf2721cb1b0384e66b6289255f910b7"),
    (_SPEC_DIR / "V1_2_NEW36_VALIDATION_PROTOCOL_V2_AMENDMENT_007.txt", 13971,
     "d33f9c66537e762f08d164bc791f31e3eb18fbc3a5e694507f9f436322ceaf10"),
)

FROZEN_COLLECTOR_SHA256 = "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3"
FROZEN_ENGINE_SHA256 = "abedee399c272eff858b109fb448cbda3ad3a49e40be4ba3f486650e3151a3e8"

FREEZE_MANIFEST_PATH: Path = _SPEC_DIR / "V1_2_CANDIDATE_IMPLEMENTATION_FREEZE_MANIFEST.txt"

SOURCE_PATHS: tuple[str, ...] = (
    "backend/recovery/v1_2_new36_candidate_app.py",
    "backend/recovery/v1_2_new36_validators.py",
    "backend/recovery/v1_2_new36_run.py",
    "backend/recovery/v1_2_candidate.py",
    "backend/recovery/v1_2_stage3.py",
    "backend/recovery/v1_2_diagnostics.py",
    "backend/recovery/harness.py",
    "backend/recovery/engine.py",
    "backend/checkpoint_registry.py",
    "backend/parquet_validator.py",
    "backend/frozen_engine.py",
)
ATTESTATION_PATHS: tuple[str, ...] = (
    "backend/recovery/CANDIDATE_INDEPENDENCE_ATTESTATION.txt",
)
CONTROL_PATHS: tuple[str, ...] = (
    "backend/recovery/v1_2_preflight.py",
    "backend/recovery/v1_2_acceptance.py",
    "backend/recovery/sandbox.py",
    "backend/recovery/allowlist.py",
)

# NOTE: Reference (0.6) byte-identity is verified GENERICALLY by the
# freeze-manifest row loop in gate_p03_implementation_identities below
# (any row, regardless of record_type, is checked against the real
# repository file at its recorded path/size/sha256). No Reference file
# hash is hardcoded in this module: computing it now, before Section 16
# step 6 exists, would require fabricating a value this agent cannot
# verify without violating the §5.1 clean-room prohibition on consulting
# backend/recovery/reference/. The freeze manifest (Section 16 step 6,
# NOT performed in this session) is the correct, authorized place to
# record that hash via a plain byte-level SHA256 (identity check, not
# content inspection).

# A008 DRAFT4 §14.2 — authoritative input identity triples (Appendix-B order).
NEW36_INPUT_IDENTITY: dict[str, tuple[int, str]] = {
    "20260910T123759Z_5ab0c1b2": (340751422, "3955aa2494086cd9e21ce7eb9ba446c50c7af43933999e805bc823ba6aeda47d"),
    "20260911T040213Z_43a798b7": (172003086, "d824a3447e4f21c3255b9cccbb5a2ae98e02c1fad1de3464b689fc011162011e"),
    "20260911T081446Z_28b5a901": (185950682, "17908158a060314d3126c24235a167ed2acfb3d6d3c727cfdf0cfb4c06db639c"),
    "20260911T113249Z_276d8c3b": (483830410, "193823eeb99fd0686ce680f381fa37c9b7ae5a795a9efd7412e6a8babe3c0868"),
    "20260911T145009Z_37923a7a": (361485063, "58cd05ae1dda9edf7081e8621ba7c1de1b13808dba80fed39fd3be28ff4401f8"),
    "20260911T220737Z_5d45563e": (171131284, "c554548dac283277d978dd789ba3f3ac4f5c3ff022c7c1c552f6f927da3a8c52"),
    "20260912T072730Z_adb96088": (141579081, "b6d02a8e864c00f18610225ce850a670db2c32568819691743a9f751efb052c2"),
    "20260912T163326Z_16b9ca74": (133546596, "13581fea0765956b8db35716b8a5d527781fb53a3055e008e1e0cc5757298365"),
    "20260912T213151Z_9f9a0088": (114954594, "14c48ca156a60daa374fbbf8d12c4d7a7e37d7015472c1e63de4f848681e1022"),
    "20260914T050815Z_3b68aabf": (201193333, "5500d6f96306006c78ab125b87e14c913a2882bc7c431dd0deac78e6d9c54d6a"),
    "20260914T133522Z_6141ec8b": (354205917, "24538b5a61c8d194a70327ac968868063f7a4278175186b306542f5d42bf5783"),
    "20260914T192945Z_1b899057": (271015335, "ceadd07e36aebc406ec208c19b7de7bfe2a36aa572cc381b0fc5411fcac01964"),
}

ASSETS: tuple[str, ...] = ("BTC", "ETH")

# Test/synthetic escape hatch ONLY: when set, gate P-09 checks the supplied
# identity table instead of the frozen NEW36_INPUT_IDENTITY table above, and
# gate P-03 may be pointed at a synthetic freeze manifest / control set.
# NEVER used against real NEW36 data; production invocation always uses the
# module-level frozen defaults.


class PreconditionFail(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass
class CandidateContext:
    """Injectable overrides for gates, used ONLY by synthetic tests
    (A008 §13). Production invocation uses every default unchanged."""
    input_identity_table: dict[str, tuple[int, str]] | None = None
    freeze_manifest_path: Path | None = None
    protocol_identities: tuple[tuple[Path, int, str], ...] | None = None
    session_ids_ordered: tuple[str, ...] | None = None
    skip_firewall_self_check: bool = False


def _session_ids_ordered(ctx: CandidateContext) -> tuple[str, ...]:
    if ctx.session_ids_ordered is not None:
        return ctx.session_ids_ordered
    from checkpoint_registry import NEW36_SESSION_IDS
    return tuple(NEW36_SESSION_IDS)


# ---------------------------------------------------------------------------
# Gate P-01 .. P-10 (A008 §4.12)
# ---------------------------------------------------------------------------

def gate_p01_generate_outputs_absent(out_csv: Path, out_manifest: Path, out_evidence: Path) -> None:
    for p in (out_csv, out_manifest, out_evidence):
        if p.exists():
            raise PreconditionFail("P-01", f"output path already exists: {p}")


def gate_p02_protocol_identities(ctx: CandidateContext) -> None:
    identities = ctx.protocol_identities or FROZEN_PROTOCOL_IDENTITIES
    for path, size_bytes, sha in identities:
        if not path.exists():
            raise PreconditionFail("P-02", f"missing frozen spec file: {path}")
        if path.stat().st_size != size_bytes or sha256_file(path) != sha:
            raise PreconditionFail("P-02", f"identity drift on {path}")


def gate_p03_implementation_identities(ctx: CandidateContext) -> None:
    freeze_path = ctx.freeze_manifest_path or FREEZE_MANIFEST_PATH
    if not freeze_path.exists():
        raise PreconditionFail(
            "P-03",
            f"freeze record absent: {freeze_path} (Section 16 step 6 has not "
            f"run yet; NEW36 is not eligible until then, §3.6)",
        )
    # Freeze record present: verify every per-file SHA256/aggregate + the
    # Reference (0.6) and protocol (0.2-0.4) identities it records.
    text = freeze_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if ln]
    kv: dict[str, str] = {}
    rows: list[tuple[str, str, str, str]] = []
    for ln in lines:
        if "=" in ln and "\t" not in ln:
            k, _, v = ln.partition("=")
            kv[k] = v
        elif "\t" in ln:
            parts = ln.split("\t")
            if len(parts) == 4:
                rows.append((parts[0], parts[1], parts[2], parts[3]))
    for record_type, relpath, size_str, sha in rows:
        p = REPO_ROOT / relpath
        if not p.exists() or p.stat().st_size != int(size_str) or sha256_file(p) != sha:
            raise PreconditionFail("P-03", f"freeze-manifest identity drift on {relpath} (record_type={record_type})")
    if kv.get("REFERENCE_IDENTITY_OK") != "YES" or kv.get("PROTOCOL_IDENTITY_OK") != "YES":
        raise PreconditionFail("P-03", "freeze manifest does not assert REFERENCE_IDENTITY_OK/PROTOCOL_IDENTITY_OK=YES")


def gate_p04_firewall_self_check(ctx: CandidateContext) -> None:
    if ctx.skip_firewall_self_check:
        return
    session_ids = _session_ids_ordered(ctx)
    try:
        assert_recovery_allowed(session_ids[0])
    except PermissionError:
        pass
    else:
        raise PreconditionFail("P-04", "recovery firewall did not reject a NEW36 session_id")
    from frozen_engine import current_status
    status = current_status()
    if status.status != "NOT_CONFIGURED" or status.accepts_input is not False:
        raise PreconditionFail("P-04", "FrozenAnalysisEngine is not NOT_CONFIGURED/accepts_input=False")


def _git_head(cwd: Path = REPO_ROOT) -> str:
    out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(cwd)).decode("ascii").strip()
    return out


def _git_show_blob_sha256(commit: str, relpath: str, cwd: Path = REPO_ROOT) -> str:
    import hashlib
    data = subprocess.check_output(["git", "show", f"{commit}:{relpath}"], cwd=str(cwd))
    return hashlib.sha256(data).hexdigest()


def gate_p05_source_identity() -> tuple[str, dict[str, str]]:
    """NNC-1 runtime_source_commit + source_sha256 with HEAD binding (A008 §12.2)."""
    import re
    try:
        head = _git_head()
    except Exception as exc:
        raise PreconditionFail("P-05", f"cannot resolve git HEAD: {exc}")
    if not re.match(r"^[0-9a-f]{40}$", head):
        raise PreconditionFail("P-05", f"HEAD does not match ^[0-9a-f]{{40}}$: {head!r}")
    source_sha256: dict[str, str] = {}
    for relpath in SOURCE_PATHS + ATTESTATION_PATHS:
        p = REPO_ROOT / relpath
        if not p.exists():
            raise PreconditionFail("P-05", f"source path missing: {relpath}")
        working_sha = sha256_file(p)
        try:
            committed_sha = _git_show_blob_sha256(head, relpath)
        except subprocess.CalledProcessError:
            raise PreconditionFail("P-05", f"{relpath} does not exist at HEAD ({head})")
        if committed_sha != working_sha:
            raise PreconditionFail("P-05", f"{relpath} working tree != committed blob at HEAD")
        source_sha256[relpath] = working_sha
    return head, source_sha256


def gate_p06_nnc3(environment_record_path: Path) -> dict:
    try:
        current_identity = compute_runtime_environment_identity()
    except NNC3Error as exc:
        raise PreconditionFail("P-06", str(exc))
    current_sha = environment_identity_sha256(current_identity)
    if not environment_record_path.exists():
        raise PreconditionFail("P-06", f"shared environment record does not exist: {environment_record_path}")
    try:
        recorded = load_and_validate_environment_record(environment_record_path)
    except (NNC3Error, NNC5ParseError) as exc:
        raise PreconditionFail("P-06", str(exc))
    if not environment_identities_equivalent(
        current_identity, current_sha,
        recorded["runtime_environment_identity"], recorded["runtime_environment_identity_sha256"],
    ):
        raise PreconditionFail("P-06", "current interpreter identity != recorded shared environment identity")
    return recorded


def gate_p07_nnc4(db_path: Path) -> None:
    try:
        result = verify_session_id_uniqueness(db_path)
    except NNC4Error as exc:
        raise PreconditionFail("P-07", str(exc))
    if not result.ok:
        raise PreconditionFail("P-07", result.detail)


def gate_p08_inventory(ctx: CandidateContext, session_zip_map: dict) -> None:
    frozen_ids = _session_ids_ordered(ctx)
    try:
        from checkpoint_registry import NEW36_SESSION_IDS
        if ctx.session_ids_ordered is None and tuple(NEW36_SESSION_IDS) != frozen_ids:
            raise PreconditionFail("P-08", "checkpoint_registry.NEW36_SESSION_IDS drifted from Appendix B")
    except ImportError:
        raise PreconditionFail("P-08", "cannot import checkpoint_registry.NEW36_SESSION_IDS")
    keys = list(session_zip_map.keys())
    if len(keys) != len(set(keys)) or set(keys) != set(frozen_ids):
        raise PreconditionFail("P-08", "session-zip-map key set != exactly the 12 frozen NEW36 identifiers")


def gate_p09_input_identity(ctx: CandidateContext, session_zip_map: dict[str, str]) -> None:
    table = ctx.input_identity_table or NEW36_INPUT_IDENTITY
    for session_id, path_str in session_zip_map.items():
        p = Path(path_str)
        if not p.is_file():
            raise PreconditionFail("P-09", f"mapped path is not a regular file: {path_str}")
        expected_size, expected_sha = table[session_id]
        if p.stat().st_size != expected_size:
            raise PreconditionFail("P-09", f"size mismatch for {session_id}")
        if sha256_file(p) != expected_sha:
            raise PreconditionFail("P-09", f"raw SHA256 mismatch for {session_id}")


def gate_p10_ledger_binding(ledger_path: Path, run_id: str) -> None:
    records = read_ledger_records(ledger_path)
    last_candidate = None
    for rec in records:
        if rec.get("role") == "CANDIDATE":
            last_candidate = rec
    if last_candidate is None or last_candidate.get("state") != "INVOKED" or last_candidate.get("run_id") != run_id:
        raise PreconditionFail(
            "P-10",
            "ledger does not contain, as the LAST record for role CANDIDATE, "
            "an INVOKED record with run_id equal to --run-id (A008 §4.2/9.7 step 5)",
        )


def run_preflight_gates(
    ctx: CandidateContext,
    db_path: Path,
    environment_record_path: Path,
    session_zip_map_path: Path,
) -> dict:
    try:
        session_zip_map = nnc5_load_file(session_zip_map_path)
    except NNC5ParseError as exc:
        raise PreconditionFail("P-01", f"session-zip-map NNC-5 parse failure: {exc}")
    gate_p02_protocol_identities(ctx)
    gate_p03_implementation_identities(ctx)
    gate_p04_firewall_self_check(ctx)
    head, source_sha256 = gate_p05_source_identity()
    env_record = gate_p06_nnc3(environment_record_path)
    gate_p07_nnc4(db_path)
    gate_p08_inventory(ctx, session_zip_map)
    gate_p09_input_identity(ctx, session_zip_map)
    return {
        "session_zip_map": session_zip_map,
        "runtime_source_commit": head,
        "source_sha256": source_sha256,
        "environment_record": env_record,
    }


# ---------------------------------------------------------------------------
# NEW36 loader (own implementation; NO sandbox/harness loader calls, 4.5(b))
# ---------------------------------------------------------------------------

class StructuralFailure(RuntimeError):
    """A004 §6 structural failure classification."""


def _authoritative_sync_grid_count(db_path: Path, session_id: str) -> int | None:
    """A005 R2.8 / A006 §4.10.2 authoritative expected part count, read
    directly (read-only) from the mapped --db path using the exact
    `sessions`/`qa_runs` schema quoted in the frozen A006 §8.2 text.
    Does NOT call harness._authoritative_sync_grid_count (own
    implementation per §4.5(b))."""
    import sqlite3
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_qa_run_id FROM sessions WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        cur.execute("SELECT sync_grid_file_count FROM qa_runs WHERE id = ?", (row[0],))
        run_row = cur.fetchone()
        if run_row is None:
            return None
        return run_row[0]
    finally:
        conn.close()


def _validate_and_load_new36_grid(zip_path: Path, session_id: str, asset: str, db_path: Path) -> pd.DataFrame:
    """Own loader (§4.6). Validates sync_grid_100ms part coverage via
    parquet_validator (permitted input), then reads parts directly.
    MUST NOT call recovery.sandbox.open_reference_zip or
    recovery.harness._load_grid_for_block (§4.5(b))."""
    from parquet_validator import validate_all_parquet

    validation = validate_all_parquet(str(zip_path))
    sync_dir = validation.per_dir.get("sync_grid_100ms")
    if sync_dir is None or sync_dir.files_seen == 0:
        raise StructuralFailure(f"{session_id}: no sync_grid_100ms parquet parts present")
    if not sync_dir.sequence_ok:
        raise StructuralFailure(f"{session_id}: sync_grid_100ms sequence invalid: {sync_dir.sequence_detail}")
    sync_failures = [f for f in validation.failures if "sync_grid_100ms" in f]
    if sync_failures:
        raise StructuralFailure(f"{session_id}: corrupt/truncated sync_grid_100ms part(s): {sync_failures[:5]}")
    expected = _authoritative_sync_grid_count(db_path, session_id)
    if expected is not None and sync_dir.files_seen != expected:
        raise StructuralFailure(
            f"{session_id}: authoritative manifest expects {expected} parts, found {sync_dir.files_seen}"
        )

    with zipfile.ZipFile(str(zip_path), "r") as zf:
        names = sorted(
            n for n in zf.namelist()
            if "sync_grid_100ms" in n and n.endswith(".parquet") and asset in n
        )
        if not names:
            names = sorted(
                n for n in zf.namelist()
                if "sync_grid_100ms" in n and n.endswith(".parquet")
            )
        if not names:
            raise StructuralFailure(f"{session_id}/{asset}: no sync_grid_100ms parquet entry found")
        frames = []
        for name in names:
            import io as _io
            with zf.open(name) as fh:
                buf = _io.BytesIO(fh.read())
            df = pd.read_parquet(buf)
            if "asset" in df.columns:
                df = df[df["asset"] == asset].copy()
            frames.append(df)
    if not frames:
        raise StructuralFailure(f"{session_id}/{asset}: empty parquet part concat list")
    combined = pd.concat(frames, ignore_index=True)
    if len(combined) == 0:
        raise StructuralFailure(f"{session_id}/{asset}: grid filters to zero rows for this asset")
    return combined


# ---------------------------------------------------------------------------
# §4.7 COMPOSITION — binding-table row generation (per block)
# ---------------------------------------------------------------------------

def _strip_diagnostic_version(row: dict) -> dict:
    out = {"protocol_version": PROTOCOL_VERSION_LITERAL}
    for f in CANDIDATE_ROW_FIELDS[1:]:
        out[f] = row.get(f)
    return out


def _apply_f3_fair_gap_fix(hz_rows: list[dict]) -> None:
    """F-3 (§4.7.6): the emitted calculated_threshold of BOTH the T0 row
    and the TZ row of fair_gap_reversion equals T0_threshold (zero
    included); the TZ zero-excluded diagnostic threshold is never
    emitted."""
    t0_by_q: dict[float, float | None] = {}
    for r in hz_rows:
        if r["feature_family"] == "fair_gap_reversion" and r["variant_id"] == "T0":
            t0_by_q[r["quantile"]] = r["calculated_threshold"]
    for r in hz_rows:
        if r["feature_family"] == "fair_gap_reversion" and r["variant_id"] == "TZ":
            r["calculated_threshold"] = t0_by_q.get(r["quantile"])


def _compose_hz_rows(ctx, session_id: str, asset: str):
    raw_rows, hz_cache = stage3._hz_block(ctx, session_id, asset)
    rows = [_strip_diagnostic_version(r) for r in raw_rows]
    _apply_f3_fair_gap_fix(rows)
    return rows, hz_cache


def _g0_arrays(ctx, horizon_ms: int, q: float):
    """Recompute the G0 (depthL1_extOFI) pre-overlap arrays using the
    SAME frozen formula _hg_block uses internally (no primitive change;
    extracted for evidence 11.4). k/spacing per A13."""
    k = horizon_ms // GRID_MS
    spacing = max(10, k)
    fwd = _build_forward_return_array(ctx.mid, k)
    derived_domain = ctx.quality & np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi)
    D = np.where(derived_domain, (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0, np.nan)
    threshold = _quantile_type7(np.abs(D[derived_domain]), q)
    domain_n = int(np.sum(derived_domain))
    if threshold is None:
        mask = np.zeros(len(ctx.gpos), dtype=bool)
    else:
        mask = derived_domain & (D != 0.0) & (np.abs(D) >= threshold) & np.isfinite(fwd)
    positions = ctx.gpos[mask]
    directions = np.sign(D[mask])
    returns = fwd[mask]
    return positions, directions, returns, threshold, spacing, domain_n


def _c0_arrays(ctx, horizon_ms: int, q: float):
    """Recompute the C0 (gap_depth_extOFI) pre-overlap arrays using the
    SAME frozen formula _hc_block uses internally."""
    k = horizon_ms // GRID_MS
    spacing = max(10, k)
    fwd = _build_forward_return_array(ctx.mid, k)
    gap = ctx.columns.get("bitget_gap_to_fair_bps", np.full(len(ctx.gpos), np.nan, dtype=float)).astype(float)
    gap_thresh_domain = ctx.quality & np.isfinite(gap)
    threshold = _quantile_type7(np.abs(gap[gap_thresh_domain]), q)
    D_ext = np.where(
        np.isfinite(ctx.z_di_l1) & np.isfinite(ctx.z_ext_ofi),
        (ctx.z_di_l1 + ctx.z_ext_ofi) / 2.0, np.nan,
    )
    conf_fin = np.isfinite(D_ext)
    alignment = (np.sign(D_ext) == -np.sign(gap)) & (D_ext != 0.0) & conf_fin
    gap_event_domain = gap_thresh_domain & conf_fin
    domain_n = int(np.sum(gap_event_domain))
    if threshold is None:
        mask = np.zeros(len(ctx.gpos), dtype=bool)
    else:
        mask = gap_event_domain & (gap != 0.0) & (np.abs(gap) >= threshold) & alignment & np.isfinite(fwd)
    positions = ctx.gpos[mask]
    directions = -np.sign(gap[mask])
    returns = fwd[mask]
    return positions, directions, returns, threshold, spacing, domain_n


def _compose_g1(ctx, session_id: str, asset: str, horizon_ms: int, q: float, hz_cache: dict):
    """F-2 (§4.7.5): G1 composed entirely on the normative G1 path."""
    k = horizon_ms // GRID_MS
    spacing = candidate.spacing_steps_for_horizon(horizon_ms)
    fwd = _build_forward_return_array(ctx.mid, k)
    di_l1 = ctx.columns["bitget_depth_imbalance_l1"]
    ext_ofi = ctx.columns["external_ofi_consensus_l1"]

    _t0_thr, tz_thr = hz_cache.get(("depth_imbalance_l1", q), (None, None))
    # Cross-check: TZ threshold must equal candidate.tz_threshold recomputation.
    recomputed_tz = candidate.tz_threshold(ctx.quality, di_l1, q)
    if (tz_thr is None) != (recomputed_tz is None) or (
        tz_thr is not None and recomputed_tz is not None and abs(tz_thr - recomputed_tz) > 1e-12
    ):
        raise AssertionError("F-2 DRIFT: hz_cache TZ threshold != candidate.tz_threshold recomputation")

    g1_mask = candidate.g1_pre_overlap_mask(quality=ctx.quality, di_l1=di_l1, ext_ofi=ext_ofi, fwd=fwd, tz_thr=tz_thr)
    parent_mask = candidate.g1_parent_gate_mask(quality=ctx.quality, di_l1=di_l1, fwd=fwd, tz_thr=tz_thr)
    candidate.assert_g1_parent_subset(g1_mask, parent_mask)

    gate_domain = ctx.quality & np.isfinite(di_l1)
    gate_zero_n = int(np.sum(gate_domain & (di_l1 == 0.0)))

    positions = ctx.gpos[g1_mask]
    directions = candidate.g1_direction(di_l1)[g1_mask]
    returns = fwd[g1_mask]
    accepted_mask = candidate.b0_greedy_filter(positions, spacing) if len(positions) else np.zeros(0, dtype=bool)
    accepted_n = int(np.sum(accepted_mask))
    pre_n = int(len(positions))
    accepted_positions = positions[accepted_mask]
    fp = sha256_positions(accepted_positions.tolist())
    metrics = stage3._compute_event_metrics(directions, returns, accepted_mask)
    parent_positions = ctx.gpos[parent_mask]

    row = {
        "protocol_version": PROTOCOL_VERSION_LITERAL,
        "session_id": session_id, "asset": asset, "axis": "HG",
        "feature_family": "depthL1_extOFI", "variant_id": "G1",
        "quantile": q, "horizon_ms": horizon_ms,
        "threshold_domain_finite_n": None, "threshold_domain_nonzero_n": None,
        "zero_n": None, "zero_fraction": None, "nonzero_unique_value_n": None,
        "hz_discrimination_class": None,
        "gate_zero_n": gate_zero_n,
        "calculated_threshold": tz_thr,
        "spacing_steps": spacing,
        "pre_overlap_n": pre_n,
        "exact_spacing_pair_n": None,
        "accepted_n": accepted_n,
        "overlap_dropped_n": pre_n - accepted_n,
        "accepted_positions_sha256": fp,
        "mean_signed_bps": metrics["mean_signed_bps"],
        "hit_rate": metrics["hit_rate"],
        "mean_abs_move": None,  # F-4 override applied later
    }
    evidence = {
        "session_id": session_id, "asset": asset, "axis": "HG",
        "feature_family": "depthL1_extOFI", "variant_id": "G1",
        "quantile": q, "horizon_ms": horizon_ms, "spacing_steps": spacing,
        "threshold_domain_n": int(np.sum(ctx.quality & np.isfinite(di_l1) & (di_l1 != 0.0))),
        "pre_overlap_positions": [int(p) for p in positions.tolist()],
        "parent_positions": [int(p) for p in parent_positions.tolist()],
        "accepted_positions": [int(p) for p in accepted_positions.tolist()],
        "accepted_signed_return_signs": _signed_return_signs(directions[accepted_mask], returns[accepted_mask]),
    }
    return row, evidence


def _signed_return_signs(directions: np.ndarray, returns: np.ndarray) -> list[int]:
    signed = directions * returns
    return [int(np.sign(v)) if v != 0.0 else 0 for v in signed.tolist()]


def _compose_hc_rows_and_evidence(ctx, session_id: str, asset: str):
    raw_rows = stage3._hc_block(ctx, session_id, asset)
    rows = [_strip_diagnostic_version(r) for r in raw_rows]
    evidence: list[dict] = []
    gap = ctx.columns["bitget_gap_to_fair_bps"]
    for row in rows:
        horizon_ms, q = row["horizon_ms"], row["quantile"]
        if row["variant_id"] == "C0":
            positions, directions, returns, threshold, spacing, domain_n = _c0_arrays(ctx, horizon_ms, q)
            if row["calculated_threshold"] is not None and threshold is not None and abs(row["calculated_threshold"] - threshold) > 1e-12:
                raise AssertionError("C0 DRIFT: recomputed threshold != _hc_block row threshold")
            accepted_mask = candidate.b0_greedy_filter(positions, row["spacing_steps"]) if len(positions) else np.zeros(0, dtype=bool)
            accepted_positions = positions[accepted_mask]
            parent_positions = None
        else:  # C1
            k = horizon_ms // GRID_MS
            spacing = candidate.spacing_steps_for_horizon(horizon_ms)
            fwd = _build_forward_return_array(ctx.mid, k)
            di_l1 = ctx.columns["bitget_depth_imbalance_l1"]
            ext_ofi = ctx.columns["external_ofi_consensus_l1"]
            gap_thresh_domain = ctx.quality & np.isfinite(gap)
            gap_thr = _quantile_type7(np.abs(gap[gap_thresh_domain]), q)
            c1_mask = candidate.c1_pre_overlap_mask(
                quality=ctx.quality, gap=gap, di_l1=di_l1, ext_ofi=ext_ofi, fwd=fwd, gap_thr=gap_thr,
            )
            gap_parent_mask = candidate.c1_gap_parent_mask(quality=ctx.quality, gap=gap, fwd=fwd, gap_thr=gap_thr)
            candidate.assert_c1_gap_subset(c1_mask, gap_parent_mask)
            positions = ctx.gpos[c1_mask]
            directions = candidate.c1_direction(gap)[c1_mask]
            returns = fwd[c1_mask]
            accepted_mask = candidate.b0_greedy_filter(positions, spacing) if len(positions) else np.zeros(0, dtype=bool)
            accepted_positions = positions[accepted_mask]
            parent_positions = ctx.gpos[gap_parent_mask]
        metrics_directions = directions[accepted_mask] if len(positions) else np.zeros(0, dtype=float)
        metrics_returns = returns[accepted_mask] if len(positions) else np.zeros(0, dtype=float)
        evidence.append({
            "session_id": session_id, "asset": asset, "axis": "HC",
            "feature_family": "gap_depth_extOFI", "variant_id": row["variant_id"],
            "quantile": q, "horizon_ms": horizon_ms, "spacing_steps": row["spacing_steps"],
            "threshold_domain_n": domain_n if row["variant_id"] == "C0" else int(np.sum(ctx.quality & np.isfinite(gap))),
            "pre_overlap_positions": [int(p) for p in positions.tolist()],
            "parent_positions": None if parent_positions is None else [int(p) for p in parent_positions.tolist()],
            "accepted_positions": [int(p) for p in accepted_positions.tolist()],
            "accepted_signed_return_signs": _signed_return_signs(metrics_directions, metrics_returns),
        })
        row["mean_abs_move"] = None  # F-4 override applied later
    return rows, evidence


def _compose_hg_rows_and_evidence(ctx, session_id: str, asset: str, hz_cache: dict):
    raw_rows = stage3._hg_block(ctx, session_id, asset, hz_cache)
    g0_rows = [_strip_diagnostic_version(r) for r in raw_rows if r["variant_id"] == "G0"]
    rows: list[dict] = []
    evidence: list[dict] = []
    for row in g0_rows:
        horizon_ms, q = row["horizon_ms"], row["quantile"]
        positions, directions, returns, threshold, spacing, domain_n = _g0_arrays(ctx, horizon_ms, q)
        if row["calculated_threshold"] is not None and threshold is not None and abs(row["calculated_threshold"] - threshold) > 1e-12:
            raise AssertionError("G0 DRIFT: recomputed threshold != _hg_block row threshold")
        accepted_mask = candidate.b0_greedy_filter(positions, row["spacing_steps"]) if len(positions) else np.zeros(0, dtype=bool)
        accepted_positions = positions[accepted_mask]
        row["mean_abs_move"] = None  # F-4 override applied later
        rows.append(row)
        m_dirs = directions[accepted_mask] if len(positions) else np.zeros(0, dtype=float)
        m_rets = returns[accepted_mask] if len(positions) else np.zeros(0, dtype=float)
        evidence.append({
            "session_id": session_id, "asset": asset, "axis": "HG",
            "feature_family": "depthL1_extOFI", "variant_id": "G0",
            "quantile": q, "horizon_ms": horizon_ms, "spacing_steps": row["spacing_steps"],
            "threshold_domain_n": domain_n,
            "pre_overlap_positions": [int(p) for p in positions.tolist()],
            "parent_positions": None,
            "accepted_positions": [int(p) for p in accepted_positions.tolist()],
            "accepted_signed_return_signs": _signed_return_signs(m_dirs, m_rets),
        })
    for horizon_ms in HORIZON_ORDER:
        for q in QUANTILE_ORDER:
            g1_row, g1_evidence = _compose_g1(ctx, session_id, asset, horizon_ms, q, hz_cache)
            rows.append(g1_row)
            evidence.append(g1_evidence)
    return rows, evidence


def _compose_hb_rows_and_evidence(ctx, session_id: str, asset: str):
    from .v1_2_new36_validators import HB_FEATURE_ORDER
    rows: list[dict] = []
    evidence: list[dict] = []
    q = HB_QUANTILE
    for feature in HB_FEATURE_ORDER:
        for horizon_ms in HORIZON_ORDER:
            k = horizon_ms // GRID_MS
            spacing = candidate.spacing_steps_for_horizon(horizon_ms)
            fwd = _build_forward_return_array(ctx.mid, k)
            positions, directions, returns, threshold = stage3._hb_extract_pre_overlap(feature, ctx, horizon_ms, q, fwd)
            if positions.size > 1 and bool(np.any(positions[1:] < positions[:-1])):
                raise AssertionError("F-1 DRIFT: HB pre-overlap positions are not ascending")
            exact_pair_n = candidate.exact_spacing_pair_n(positions, spacing)

            b0_accepted_mask = candidate.b0_greedy_filter(positions, spacing) if len(positions) else np.zeros(0, dtype=bool)
            b1_accepted_mask = candidate.b1_diagnostic_filter(positions, spacing) if len(positions) else np.zeros(0, dtype=bool)
            b0_accepted_n = int(np.sum(b0_accepted_mask))
            b1_accepted_n = int(np.sum(b1_accepted_mask))
            candidate.assert_hb_i11(b0_accepted_n, b1_accepted_n)

            pre_n = int(len(positions))
            for variant_id, accepted_mask, accepted_n in (
                ("B0", b0_accepted_mask, b0_accepted_n),
                ("B1", b1_accepted_mask, b1_accepted_n),
            ):
                accepted_positions = positions[accepted_mask]
                m_dirs = directions[accepted_mask] if len(positions) else np.zeros(0, dtype=float)
                m_rets = returns[accepted_mask] if len(positions) else np.zeros(0, dtype=float)
                metrics = stage3._compute_event_metrics(directions, returns, accepted_mask)
                fp = sha256_positions(accepted_positions.tolist())
                rows.append({
                    "protocol_version": PROTOCOL_VERSION_LITERAL,
                    "session_id": session_id, "asset": asset, "axis": "HB",
                    "feature_family": feature, "variant_id": variant_id,
                    "quantile": q, "horizon_ms": horizon_ms,
                    "threshold_domain_finite_n": None, "threshold_domain_nonzero_n": None,
                    "zero_n": None, "zero_fraction": None, "nonzero_unique_value_n": None,
                    "hz_discrimination_class": None, "gate_zero_n": None,
                    "calculated_threshold": threshold, "spacing_steps": spacing,
                    "pre_overlap_n": pre_n,
                    "exact_spacing_pair_n": exact_pair_n if variant_id == "B0" else exact_pair_n,
                    "accepted_n": accepted_n, "overlap_dropped_n": pre_n - accepted_n,
                    "accepted_positions_sha256": fp,
                    "mean_signed_bps": metrics["mean_signed_bps"], "hit_rate": metrics["hit_rate"],
                    "mean_abs_move": None,  # F-4 override applied later
                })
                evidence.append({
                    "session_id": session_id, "asset": asset, "axis": "HB",
                    "feature_family": feature, "variant_id": variant_id,
                    "quantile": q, "horizon_ms": horizon_ms, "spacing_steps": spacing,
                    "threshold_domain_n": int(len(positions)),
                    "pre_overlap_positions": ([int(p) for p in positions.tolist()] if variant_id == "B0" else None),
                    "parent_positions": None,
                    "accepted_positions": [int(p) for p in accepted_positions.tolist()],
                    "accepted_signed_return_signs": _signed_return_signs(m_dirs, m_rets),
                })
    return rows, evidence


def _apply_f4_mean_abs_move(rows: list[dict], ctx) -> None:
    """F-4 (§4.7.7): FEATURE-INDEPENDENT per (session_id, asset,
    horizon_ms); computed ONCE per horizon and written identically to
    every HG/HC/HB row of that triple."""
    cache: dict[int, float | None] = {}
    for horizon_ms in HORIZON_ORDER:
        k = horizon_ms // GRID_MS
        fwd = _build_forward_return_array(ctx.mid, k)
        cache[horizon_ms] = diagnostics._finite_mean_abs(fwd[ctx.quality & np.isfinite(fwd)])
    for r in rows:
        if r["axis"] in ("HG", "HC", "HB"):
            r["mean_abs_move"] = cache.get(r["horizon_ms"])


def compose_block(ctx, session_id: str, asset: str) -> tuple[list[dict], list[dict]]:
    hz_rows, hz_cache = _compose_hz_rows(ctx, session_id, asset)
    hg_rows, hg_evidence = _compose_hg_rows_and_evidence(ctx, session_id, asset, hz_cache)
    hc_rows, hc_evidence = _compose_hc_rows_and_evidence(ctx, session_id, asset)
    hb_rows, hb_evidence = _compose_hb_rows_and_evidence(ctx, session_id, asset)
    all_rows = hz_rows + hg_rows + hc_rows + hb_rows
    _apply_f4_mean_abs_move(all_rows, ctx)
    all_evidence = hg_evidence + hc_evidence + hb_evidence
    return all_rows, all_evidence


# ---------------------------------------------------------------------------
# Serialization (CSV / manifest / evidence) — A008 §4.8, §11.4, §12
# ---------------------------------------------------------------------------

def _format_field(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    return str(v)


def rows_to_csv_bytes(rows: list[dict]) -> bytes:
    import csv as _csv
    import io as _io
    buf = _io.StringIO(newline="")
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(list(CANDIDATE_ROW_FIELDS))
    for row in rows:
        writer.writerow([_format_field(row.get(f)) for f in CANDIDATE_ROW_FIELDS])
    data = buf.getvalue().encode("utf-8")
    if b'"' in data:
        raise AssertionError("A008 §4.8 violation: emitted CSV contains a '\"' character")
    return data


def evidence_to_jsonl_bytes(evidence_records: list[dict]) -> bytes:
    out = bytearray()
    for rec in evidence_records:
        out += canonical_line_bytes(rec)
        out += b"\n"
    return bytes(out)


def build_manifest(
    *,
    rows: list[dict],
    csv_bytes: bytes,
    runtime_source_commit: str,
    source_sha256: dict[str, str],
    environment_record_sha256: str,
    run_id: str,
    evidence_bytes: bytes,
    freeze_manifest_sha256: str,
    implementation_aggregates: dict[str, str],
    amendment_identities: dict[str, str],
    a008_sha256: str,
) -> dict:
    session_ids = list(dict.fromkeys(r["session_id"] for r in sorted(rows, key=canonical_sort_key)))
    per_axis_counts: dict[str, int] = {}
    for r in rows:
        per_axis_counts[r["axis"]] = per_axis_counts.get(r["axis"], 0) + 1

    manifest: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION_LITERAL,
        "implementation_role": "CANDIDATE",
        "runtime_source_commit": runtime_source_commit,
        "source_sha256": source_sha256,
        "csv_sha256": sha256_bytes(csv_bytes),
        "csv_size_bytes": len(csv_bytes),
        "manifest_precomparison_sha256": None,
        "session_ids": session_ids,
        "assets": list(ASSETS),
        "axes": ["HZ", "HG", "HC", "HB"],
        "feature_sets_per_axis": {
            "HZ": list(_hz_feature_order()),
            "HG": ["depthL1_extOFI"],
            "HC": ["gap_depth_extOFI"],
            "HB": list(_hb_feature_order()),
        },
        "variant_sets_per_axis": {"HZ": ["T0", "TZ"], "HG": ["G0", "G1"], "HC": ["C0", "C1"], "HB": ["B0", "B1"]},
        "quantiles_per_axis": {"HZ": list(QUANTILE_ORDER), "HG": list(QUANTILE_ORDER), "HC": list(QUANTILE_ORDER), "HB": [HB_QUANTILE]},
        "horizons_per_axis": {"HZ": [None], "HG": list(HORIZON_ORDER), "HC": list(HORIZON_ORDER), "HB": list(HORIZON_ORDER)},
        "expected_row_counts_per_axis": {"HZ": 1728, "HG": 432, "HC": 432, "HB": 864},
        "actual_row_counts_per_axis": per_axis_counts,
        "expected_total_row_count": EXPECTED_TOTAL_ROW_COUNT,
        "actual_total_row_count": len(rows),
        "collector_sha256": FROZEN_COLLECTOR_SHA256,
        "engine_sha256": FROZEN_ENGINE_SHA256,
        "candidate_spec_sha256": "aed67b5ec9ae1b710ec3d96e1fcc2e15bd9c30fdd60e80cc68553d8fc3b597ca",
        "protocol_sha256": "5ed8a8b12726d395d25262dc3e4073250be3de94f3d62e64e99167921380ef95",
        "new36_inventory_id": "NEW36_12SESSIONS_24BLOCKS_3456ROWS_V1",
        "golden_artifacts_read": False,
        "old36_quantitative_data_read": False,
        "frozen_analysis_engine_state": "NOT_CONFIGURED",
        "frozen_analysis_engine_accepts_input": False,
        "fingerprint_encoding_definition": FINGERPRINT_ENCODING_DEFINITION,
        "quantile_method": "TYPE7_LINEAR",
        "grid_ms": GRID_MS,
        "a008_provenance": {
            "runtime_environment_identity_sha256": environment_record_sha256,
            "implementation_source_aggregate_sha256": implementation_aggregates["source"],
            "implementation_test_aggregate_sha256": implementation_aggregates["test"],
            "implementation_control_aggregate_sha256": implementation_aggregates["control"],
            "freeze_manifest_sha256": freeze_manifest_sha256,
            "a008_sha256": a008_sha256,
            "amendment_identities": amendment_identities,
            "candidate_evidence_size_bytes": len(evidence_bytes),
            "candidate_evidence_sha256": sha256_bytes(evidence_bytes),
            "run_id": run_id,
        },
    }
    manifest["manifest_precomparison_sha256"] = _recompute_precomparison(manifest)
    return manifest


def _recompute_precomparison(manifest: dict) -> str:
    clone = dict(manifest)
    clone["manifest_precomparison_sha256"] = None
    return sha256_bytes(canonical_pretty_bytes(clone))


def _hz_feature_order() -> tuple[str, ...]:
    from .v1_2_new36_validators import HZ_FEATURE_ORDER
    return HZ_FEATURE_ORDER


def _hb_feature_order() -> tuple[str, ...]:
    from .v1_2_new36_validators import HB_FEATURE_ORDER
    return HB_FEATURE_ORDER


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_freeze_aggregates(freeze_path: Path) -> dict[str, str]:
    """Best-effort read of the 3.5 freeze-manifest aggregate lines. Returns
    empty strings when the freeze record does not exist yet (Section 16
    step 6 has not run — the correct state during Section 16 STEP 4)."""
    result = {"source": "", "test": "", "control": ""}
    if not freeze_path.exists():
        return result
    text = freeze_path.read_text(encoding="utf-8")
    for line in text.split("\n"):
        if line.startswith("IMPLEMENTATION_SOURCE_AGGREGATE_SHA256="):
            result["source"] = line.split("=", 1)[1]
        elif line.startswith("IMPLEMENTATION_TEST_AGGREGATE_SHA256="):
            result["test"] = line.split("=", 1)[1]
        elif line.startswith("IMPLEMENTATION_CONTROL_AGGREGATE_SHA256="):
            result["control"] = line.split("=", 1)[1]
    return result


def cmd_preflight(args: argparse.Namespace, ctx: CandidateContext | None = None) -> int:
    ctx = ctx or CandidateContext()
    try:
        run_preflight_gates(ctx, Path(args.db), Path(args.environment_record), Path(args.session_zip_map))
    except PreconditionFail as exc:
        print(f"PRECONDITION_FAIL {exc.code}: {exc}", file=sys.stderr)
        return 2
    print("PREFLIGHT_OK")
    return 0


def cmd_generate(args: argparse.Namespace, ctx: CandidateContext | None = None) -> int:
    ctx = ctx or CandidateContext()
    out_csv, out_manifest, out_evidence = Path(args.out_csv), Path(args.out_manifest), Path(args.out_evidence)
    try:
        gate_p01_generate_outputs_absent(out_csv, out_manifest, out_evidence)
        gate_result = run_preflight_gates(ctx, Path(args.db), Path(args.environment_record), Path(args.session_zip_map))
        gate_p10_ledger_binding(Path(args.ledger), args.run_id)
    except PreconditionFail as exc:
        print(f"PRECONDITION_FAIL {exc.code}: {exc}", file=sys.stderr)
        return 2

    session_zip_map: dict[str, str] = gate_result["session_zip_map"]
    all_rows: list[dict] = []
    all_evidence: list[dict] = []
    session_ids = _session_ids_ordered(ctx)
    for session_id in session_ids:
        zip_path = Path(session_zip_map[session_id])
        for asset in ASSETS:
            df = _validate_and_load_new36_grid(zip_path, session_id, asset, Path(args.db))
            block_ctx = diagnostics._prepare_block(session_id, asset, df)
            rows, evidence = compose_block(block_ctx, session_id, asset)
            all_rows.extend(rows)
            all_evidence.extend(evidence)
            del block_ctx, df

    all_rows.sort(key=canonical_sort_key)
    # canonical key uniqueness check (own key builder on typed rows)
    seen_keys = set()
    for r in all_rows:
        k = (r["session_id"], r["asset"], r["axis"], r["feature_family"], r["variant_id"], r["quantile"], r["horizon_ms"])
        if k in seen_keys:
            raise AssertionError(f"A02 VIOLATION: duplicate canonical key {k!r}")
        seen_keys.add(k)
    if len(all_rows) != EXPECTED_TOTAL_ROW_COUNT:
        raise AssertionError(f"A01 VIOLATION: emitted {len(all_rows)} rows, expected {EXPECTED_TOTAL_ROW_COUNT}")

    csv_bytes = rows_to_csv_bytes(all_rows)
    evidence_bytes = evidence_to_jsonl_bytes(all_evidence)

    freeze_path = ctx.freeze_manifest_path or FREEZE_MANIFEST_PATH
    freeze_manifest_sha256 = sha256_file(freeze_path) if freeze_path.exists() else ""
    amendment_identities = {
        f"A{i:03d}": sha
        for i, (_p, _sz, sha) in enumerate(
            (ctx.protocol_identities or FROZEN_PROTOCOL_IDENTITIES)[2:], start=1
        )
    }
    implementation_aggregates = _read_freeze_aggregates(freeze_path)
    manifest = build_manifest(
        rows=all_rows,
        csv_bytes=csv_bytes,
        runtime_source_commit=gate_result["runtime_source_commit"],
        source_sha256=gate_result["source_sha256"],
        environment_record_sha256=gate_result["environment_record"]["runtime_environment_identity_sha256"],
        run_id=args.run_id,
        evidence_bytes=evidence_bytes,
        freeze_manifest_sha256=freeze_manifest_sha256,
        implementation_aggregates=implementation_aggregates,
        amendment_identities=amendment_identities,
        a008_sha256="",
    )
    manifest_bytes = canonical_pretty_bytes(manifest)

    # A008 §4.11(a): serialize fully, hash, write "xb", re-read compare.
    exclusive_create_write(out_csv, csv_bytes)
    exclusive_create_write(out_manifest, manifest_bytes)
    exclusive_create_write(out_evidence, evidence_bytes)

    print(json.dumps({
        "row_count": len(all_rows),
        "csv_sha256": sha256_bytes(csv_bytes),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "evidence_sha256": sha256_bytes(evidence_bytes),
    }))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="v1_2_new36_candidate_app")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pf = sub.add_parser("preflight")
    p_pf.add_argument("--db", required=True)
    p_pf.add_argument("--environment-record", required=True)
    p_pf.add_argument("--session-zip-map", required=True)
    p_pf.set_defaults(func=cmd_preflight)

    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--db", required=True)
    p_gen.add_argument("--environment-record", required=True)
    p_gen.add_argument("--session-zip-map", required=True)
    p_gen.add_argument("--out-csv", required=True)
    p_gen.add_argument("--out-manifest", required=True)
    p_gen.add_argument("--out-evidence", required=True)
    p_gen.add_argument("--run-id", required=True)
    p_gen.add_argument("--ledger", required=True)
    p_gen.set_defaults(func=cmd_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
