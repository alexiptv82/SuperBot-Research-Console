"""Synthetic tests for backend/recovery/v1_2_new36_candidate_app.py.

A008 DRAFT4 §13.1-13.2: NO real NEW36 quantitative byte is used anywhere
in this file. No network, no real DB, no path under backend/data/, no
path under §14.4. All ZIP paths are throwaway placeholder files (never
opened for real — the ZIP-opening/parquet layer is deliberately isolated
via monkeypatch, see NOTE at the top of TestFullSyntheticGenerate).
The 12 real frozen NEW36 session identifiers are used ONLY to exercise
inventory-enforcement / the full composition pipeline against synthetic
(non-NEW36) numeric fixtures, per the explicit §13.2 exception
(identifiers are not quantitative data).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recovery.v1_2_new36_candidate_app as capp  # noqa: E402
from recovery import v1_2_candidate as candidate  # noqa: E402
from recovery.v1_2_new36_validators import (  # noqa: E402
    EXPECTED_TOTAL_ROW_COUNT,
    HORIZON_ORDER,
    QUANTILE_ORDER,
    append_ledger_record_durable,
    build_shared_environment_record,
    canonical_pretty_bytes,
    canonical_sort_key,
    nnc5_loads,
    row_canonical_key_lexeme,
    sha256_file,
    verify_session_id_uniqueness,
)
from checkpoint_registry import NEW36_SESSION_IDS  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_synthetic_sessions_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE, current_qa_run_id INTEGER)")
        cur.execute("CREATE TABLE qa_runs (id INTEGER PRIMARY KEY, sync_grid_file_count INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return db_path


def _make_environment_record(tmp_path: Path) -> Path:
    p = tmp_path / "env.json"
    record = build_shared_environment_record()
    p.write_bytes(canonical_pretty_bytes(record))
    return p


def _make_synthetic_freeze_manifest(tmp_path: Path) -> Path:
    """Own-file identities (SOURCE/CONTROL/ATTESTATION) computed as raw
    byte hashes of real repository files. Reference (0.6) identity rows
    are intentionally NOT included here: no test in this suite computes
    or hardcodes a hash for any path under backend/recovery/reference/
    (that value is recorded only by Section 16 step 6, which this
    session does not perform)."""
    lines = []
    for relpath in capp.SOURCE_PATHS + capp.ATTESTATION_PATHS + capp.CONTROL_PATHS:
        p = capp.REPO_ROOT / relpath
        lines.append(f"SOURCE\t{relpath}\t{p.stat().st_size}\t{sha256_file(p)}")
    lines.append("REFERENCE_IDENTITY_OK=YES")
    lines.append("PROTOCOL_IDENTITY_OK=YES")
    p = tmp_path / "freeze_manifest.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _make_synthetic_input_files_and_identity_table(tmp_path: Path, session_ids) -> tuple[dict, dict]:
    session_zip_map = {}
    identity_table = {}
    for i, sid in enumerate(session_ids):
        p = tmp_path / f"placeholder_{i}.zip"
        p.write_bytes(f"SYNTHETIC_PLACEHOLDER_NOT_A_REAL_ZIP_{i}".encode("utf-8"))
        session_zip_map[sid] = str(p)
        identity_table[sid] = (p.stat().st_size, sha256_file(p))
    return session_zip_map, identity_table


def _synthetic_block_df(session_id: str, asset: str, n: int = 300) -> pd.DataFrame:
    seed = int(hashlib.sha256(f"{session_id}:{asset}".encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    local_ts = np.arange(n, dtype=np.int64) * 100
    sample_ns = np.arange(n, dtype=np.int64) * 100_000_000
    mid = 50000.0 + rng.normal(0, 1, n).cumsum()
    zero_mask = np.zeros(n, dtype=bool)
    zero_mask[: max(2, n // 20)] = True
    di_l1 = rng.normal(0, 1, n)
    di_l1[zero_mask] = 0.0
    gap = rng.normal(0, 5, n)
    gap[zero_mask] = 0.0
    return pd.DataFrame({
        "local_ts_ms": local_ts,
        "sample_monotonic_ns": sample_ns,
        "bitget_mid": mid,
        "bitget_book_age_recv_ms": rng.integers(0, 50, n).astype(float),
        "adjusted_fair_venue_count": rng.integers(2, 6, n).astype(float),
        "bitget_gap_to_fair_bps": gap,
        "external_perp_dispersion_bps": rng.uniform(0, 10, n),
        "bitget_depth_imbalance_l1": di_l1,
        "bitget_depth_imbalance_l5": rng.normal(0, 1, n),
        "external_ofi_consensus_l1": rng.normal(0, 1, n),
        "bitget_fair_ofi_alignment": rng.normal(0, 1, n),
        "bitget_ofi_norm_l1": rng.normal(0, 1, n),
        "bitget_trade_imbalance_window": rng.normal(0, 1, n),
        "external_trade_imbalance_consensus": rng.normal(0, 1, n),
        "fair_accel_100ms_bps": rng.normal(0, 1, n),
        "leader_gap_100ms_bps": rng.normal(0, 1, n),
        "leader_gap_200ms_bps": rng.normal(0, 1, n),
        "leader_gap_500ms_bps": rng.normal(0, 1, n),
        "leader_gap_1000ms_bps": rng.normal(0, 1, n),
        "asset": asset,
    })


# ---------------------------------------------------------------------------
# Firewall / A20 static checks
# ---------------------------------------------------------------------------

def test_firewall_rejects_new36_session_id_after_import():
    from recovery.allowlist import assert_recovery_allowed
    with pytest.raises(PermissionError):
        assert_recovery_allowed(NEW36_SESSION_IDS[0])


def test_a20_withdrawn_invariant_absent_outside_literal_and_comments():
    for relpath in (
        "backend/recovery/v1_2_new36_validators.py",
        "backend/recovery/v1_2_new36_candidate_app.py",
        "backend/recovery/v1_2_new36_run.py",
    ):
        text = (capp.REPO_ROOT / relpath).read_text(encoding="utf-8")
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or "WITHDRAWN_INVARIANTS" in line:
                continue
            for token in ("A20", "I12", "I_HB_I12"):
                if token in line:
                    # Only acceptable inside a string literal referencing
                    # the withdrawn-invariant constant/comment context.
                    assert "WITHDRAWN" in line or "A20_I12" in line or "NOT IMPLEMENTED" in line, (
                        f"unexpected {token!r} reference in {relpath}: {line!r}"
                    )


# ---------------------------------------------------------------------------
# F-1 precedence (§13.3(h))
# ---------------------------------------------------------------------------

def test_f1_exact_spacing_pair_n_is_consecutive_pair_count_not_all_pairs():
    positions = np.array([0, 5, 10], dtype=np.int64)
    spacing = 10
    # consecutive pairs at exact spacing: (0,10) is NOT consecutive (5 is
    # between them) => 0; the all-pairs (non-consecutive) formula would
    # find (0,10) diff==10 => 1. The frozen v1_2_candidate implementation
    # MUST use the consecutive definition.
    assert candidate.exact_spacing_pair_n(positions, spacing) == 0


# ---------------------------------------------------------------------------
# Gates in isolation
# ---------------------------------------------------------------------------

def test_gate_p07_nnc4_read_only_uniqueness(tmp_path):
    db_path = _make_synthetic_sessions_db(tmp_path)
    capp.gate_p07_nnc4(db_path)  # must not raise


def test_gate_p07_fails_closed_on_missing_unique_index(tmp_path):
    db_path = tmp_path / "bad.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(capp.PreconditionFail) as exc:
        capp.gate_p07_nnc4(db_path)
    assert exc.value.code == "P-07"


def test_gate_p08_inventory_rejects_wrong_key_set():
    ctx = capp.CandidateContext()
    bad_map = {sid: "/x" for sid in list(NEW36_SESSION_IDS)[:11]}
    with pytest.raises(capp.PreconditionFail) as exc:
        capp.gate_p08_inventory(ctx, bad_map)
    assert exc.value.code == "P-08"


def test_gate_p08_inventory_rejects_duplicate_mapped_to_different_case():
    ctx = capp.CandidateContext()
    ids = list(NEW36_SESSION_IDS)
    bad_map = {sid: "/x" for sid in ids[:11]}
    bad_map[ids[0].upper()] = "/y"  # wrong id, still 12 keys but wrong set
    with pytest.raises(capp.PreconditionFail) as exc:
        capp.gate_p08_inventory(ctx, bad_map)
    assert exc.value.code == "P-08"


def test_gate_p09_input_identity_size_mismatch(tmp_path):
    ctx = capp.CandidateContext()
    session_zip_map, identity_table = _make_synthetic_input_files_and_identity_table(tmp_path, NEW36_SESSION_IDS)
    ctx.input_identity_table = identity_table
    first = NEW36_SESSION_IDS[0]
    Path(session_zip_map[first]).write_bytes(b"TAMPERED")
    with pytest.raises(capp.PreconditionFail) as exc:
        capp.gate_p09_input_identity(ctx, session_zip_map)
    assert exc.value.code == "P-09"


def test_gate_p03_fails_closed_when_freeze_manifest_absent(tmp_path):
    ctx = capp.CandidateContext(freeze_manifest_path=tmp_path / "absent.txt")
    with pytest.raises(capp.PreconditionFail) as exc:
        capp.gate_p03_implementation_identities(ctx)
    assert exc.value.code == "P-03"


def test_gate_p10_ledger_binding_requires_matching_invoked_record(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(capp.PreconditionFail):
        capp.gate_p10_ledger_binding(ledger, "run-x")
    append_ledger_record_durable(ledger, {"role": "CANDIDATE", "state": "INVOKED", "run_id": "run-x"})
    capp.gate_p10_ledger_binding(ledger, "run-x")  # now passes
    with pytest.raises(capp.PreconditionFail):
        capp.gate_p10_ledger_binding(ledger, "run-y")


def test_gate_p01_generate_outputs_absent_rejects_preexisting(tmp_path):
    existing = tmp_path / "out.csv"
    existing.write_bytes(b"x")
    with pytest.raises(capp.PreconditionFail):
        capp.gate_p01_generate_outputs_absent(existing, tmp_path / "m.json", tmp_path / "e.jsonl")


# ---------------------------------------------------------------------------
# Full synthetic end-to-end generate() — §13.3(g)
# ---------------------------------------------------------------------------

class TestFullSyntheticGenerate:
    """NOTE ON TEST BOUNDARY: this test monkeypatches
    `_validate_and_load_new36_grid` (the ZIP-open/parquet-read boundary
    ONLY) to return a synthetic, deterministic, non-NEW36 DataFrame per
    (session_id, asset). Every gate (P-01..P-10, including a REAL
    on-disk file-identity check for P-09 against throwaway placeholder
    files) and the ENTIRE §4.7 composition/serialization pipeline run
    for real, unmodified. gate_p05 (git HEAD binding) is monkeypatched
    because the newly authored Step-4 files are not yet committed at
    authoring time; this does not touch composition logic."""

    def _build_ctx_and_args(self, tmp_path, monkeypatch):
        db_path = _make_synthetic_sessions_db(tmp_path)
        env_path = _make_environment_record(tmp_path)
        freeze_path = _make_synthetic_freeze_manifest(tmp_path)
        session_ids = list(NEW36_SESSION_IDS)
        session_zip_map, identity_table = _make_synthetic_input_files_and_identity_table(tmp_path, session_ids)
        zip_map_path = tmp_path / "session_zip_map.json"
        zip_map_path.write_text(json.dumps(session_zip_map), encoding="utf-8")

        ctx = capp.CandidateContext(
            input_identity_table=identity_table,
            freeze_manifest_path=freeze_path,
        )

        monkeypatch.setattr(capp, "gate_p05_source_identity", lambda: ("a" * 40, {"synthetic": "0" * 64}))

        def fake_loader(zip_path, session_id, asset, db_path_arg):
            return _synthetic_block_df(session_id, asset)

        monkeypatch.setattr(capp, "_validate_and_load_new36_grid", fake_loader)

        ledger_path = tmp_path / "ledger.jsonl"
        run_id = "11111111-1111-1111-1111-111111111111"
        append_ledger_record_durable(ledger_path, {"role": "CANDIDATE", "state": "INVOKED", "run_id": run_id})

        args = argparse_namespace = _Namespace(
            db=str(db_path), environment_record=str(env_path), session_zip_map=str(zip_map_path),
            out_csv=str(tmp_path / "candidate.csv"), out_manifest=str(tmp_path / "candidate_manifest.json"),
            out_evidence=str(tmp_path / "candidate_evidence.jsonl"), run_id=run_id, ledger=str(ledger_path),
        )
        return ctx, args, tmp_path

    def test_preflight_passes(self, tmp_path, monkeypatch):
        ctx, args, _ = self._build_ctx_and_args(tmp_path, monkeypatch)
        rc = capp.cmd_preflight(args, ctx)
        assert rc == 0

    def test_generate_produces_3456_rows_with_correct_structure(self, tmp_path, monkeypatch):
        ctx, args, base = self._build_ctx_and_args(tmp_path, monkeypatch)
        rc = capp.cmd_generate(args, ctx)
        assert rc == 0

        csv_bytes = Path(args.out_csv).read_bytes()
        lex = __import__("recovery.v1_2_new36_validators", fromlist=["parse_and_validate_csv_bytes"]).parse_and_validate_csv_bytes(csv_bytes)
        assert lex.ok, lex.reason
        rows = lex.rows
        assert len(rows) == EXPECTED_TOTAL_ROW_COUNT

        per_axis = {}
        for r in rows:
            per_axis[r["axis"]] = per_axis.get(r["axis"], 0) + 1
        assert per_axis == {"HZ": 1728, "HG": 432, "HC": 432, "HB": 864}

        keys = [row_canonical_key_lexeme(r) for r in rows]
        assert len(set(keys)) == len(keys)  # A02 uniqueness
        assert keys == [row_canonical_key_lexeme(r) for r in sorted(rows, key=canonical_sort_key)]  # canonical sort

        # "xb" refusal on re-run (P-01: outputs already exist) — cmd_generate
        # catches PreconditionFail internally and returns rc=2.
        rc_retry = capp.cmd_generate(args, ctx)
        assert rc_retry == 2
        assert Path(args.out_csv).exists()
        with pytest.raises(FileExistsError):
            from recovery.v1_2_new36_validators import exclusive_create_write
            exclusive_create_write(Path(args.out_csv), b"anything")

        manifest = nnc5_loads(Path(args.out_manifest).read_bytes())
        assert manifest["csv_sha256"] == hashlib.sha256(csv_bytes).hexdigest()
        assert manifest["actual_total_row_count"] == EXPECTED_TOTAL_ROW_COUNT
        assert manifest["a008_provenance"]["candidate_evidence_sha256"] == hashlib.sha256(Path(args.out_evidence).read_bytes()).hexdigest()

        evidence_records = [nnc5_loads(line) for line in Path(args.out_evidence).read_bytes().split(b"\n") if line]
        assert len(evidence_records) == 432 + 432 + 864  # one per HG/HC/HB row (§11.4.3)

    def test_generate_then_s2_passes_on_own_output(self, tmp_path, monkeypatch):
        ctx, args, base = self._build_ctx_and_args(tmp_path, monkeypatch)
        assert capp.cmd_generate(args, ctx) == 0

        from recovery.v1_2_new36_validators import S2Inputs, run_s2
        s2_inputs = S2Inputs(
            candidate_csv=Path(args.out_csv),
            candidate_manifest=Path(args.out_manifest),
            candidate_evidence=Path(args.out_evidence),
            freeze_manifest=Path(args.out_csv).parent / "unused_freeze.txt",
            ledger=Path(args.ledger),
        )
        report = run_s2(s2_inputs)
        assert set(report["evaluated_invariants"]) | set(report["WITHDRAWN_INVARIANTS"]) == {
            f"A{i:02d}" for i in range(1, 31)
        }
        assert len(report["evaluated_invariants"]) == 29
        failing = {k: v["details"] for k, v in report["per_invariant"].items() if v["status"] == "FAIL"}
        assert report["s2_status"] == "PASS", failing


class _Namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
