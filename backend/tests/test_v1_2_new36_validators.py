"""Synthetic tests for backend/recovery/v1_2_new36_validators.py.

A008 DRAFT4 §13.2: NO real NEW36 quantitative content is used anywhere in
this file. All fixtures are programmatically generated synthetic data.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recovery.v1_2_new36_validators import (  # noqa: E402
    CANDIDATE_ROW_FIELDS,
    EXPECTED_TOTAL_ROW_COUNT,
    NNC3Error,
    NNC4Error,
    NNC5DuplicateMemberError,
    NNC5ParseError,
    S1Inputs,
    S2Inputs,
    append_ledger_record_durable,
    canonical_line_bytes,
    canonical_pretty_bytes,
    canonical_sort_key,
    compute_runtime_environment_identity,
    environment_identities_equivalent,
    environment_identity_sha256,
    exclusive_create_write,
    latest_terminal_record,
    nnc5_load_jsonl,
    nnc5_loads,
    read_ledger_records,
    row_canonical_key_lexeme,
    run_s1,
    sha256_bytes,
    verify_session_id_uniqueness,
)


# ---------------------------------------------------------------------------
# NNC-5
# ---------------------------------------------------------------------------

def test_nnc5_accepts_well_formed_json():
    assert nnc5_loads(b'{"a": 1, "b": [1, 2, {"c": 3}]}') == {"a": 1, "b": [1, 2, {"c": 3}]}


def test_nnc5_rejects_duplicate_top_level_member():
    with pytest.raises(NNC5DuplicateMemberError):
        nnc5_loads(b'{"a": 1, "a": 2}')


def test_nnc5_rejects_duplicate_member_nested_in_array():
    with pytest.raises(NNC5DuplicateMemberError):
        nnc5_loads(b'{"x": [{"a": 1, "a": 2}]}')


def test_nnc5_rejects_duplicate_member_at_deep_nesting():
    with pytest.raises(NNC5DuplicateMemberError):
        nnc5_loads(b'{"a": {"b": {"c": 1, "d": 2, "c": 3}}}')


def test_nnc5_rejects_malformed_json():
    with pytest.raises(NNC5ParseError):
        nnc5_loads(b'{"a": }')


def test_nnc5_rejects_invalid_utf8():
    with pytest.raises(NNC5ParseError):
        nnc5_loads(b"\xff\xfe{}")


def test_nnc5_jsonl_parses_each_line_independently(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_bytes(b'{"a":1}\n{"b":2}\n')
    assert nnc5_load_jsonl(p) == [{"a": 1}, {"b": 2}]


def test_nnc5_jsonl_rejects_duplicate_member_on_any_line(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_bytes(b'{"a":1}\n{"a":1,"a":2}\n')
    with pytest.raises(NNC5DuplicateMemberError):
        nnc5_load_jsonl(p)


# ---------------------------------------------------------------------------
# NNC-3
# ---------------------------------------------------------------------------

def test_nnc3_identity_is_deterministic_and_equivalent_to_itself():
    identity_a = compute_runtime_environment_identity()
    identity_b = compute_runtime_environment_identity()
    sha_a = environment_identity_sha256(identity_a)
    sha_b = environment_identity_sha256(identity_b)
    assert environment_identities_equivalent(identity_a, sha_a, identity_b, sha_b)


def test_nnc3_sha256_recomputation_is_stable_under_key_reordering():
    identity = compute_runtime_environment_identity()
    sha_1 = environment_identity_sha256(identity)
    reordered = json.loads(json.dumps(identity))  # dict order irrelevant to canonical bytes
    sha_2 = environment_identity_sha256(reordered)
    assert sha_1 == sha_2


def test_nnc3_detects_pyarrow_version_drift(monkeypatch):
    import recovery.v1_2_new36_validators as validators
    monkeypatch.setattr(validators, "REQUIRED_PYARROW_VERSION", "999.0.0")
    with pytest.raises(NNC3Error):
        compute_runtime_environment_identity()


def test_nnc3_equivalence_fails_on_object_mismatch():
    identity_a = compute_runtime_environment_identity()
    identity_b = dict(identity_a)
    identity_b["pyarrow_version"] = "0.0.0"
    sha_a = environment_identity_sha256(identity_a)
    sha_b = environment_identity_sha256(identity_b)
    assert not environment_identities_equivalent(identity_a, sha_a, identity_b, sha_b)


# ---------------------------------------------------------------------------
# NNC-4 (corrected: read-only DB duplicate + unique-index verification)
# ---------------------------------------------------------------------------

def _make_synthetic_db(tmp_path: Path, *, with_unique_index: bool, inject_duplicate: bool) -> Path:
    db_path = tmp_path / "synthetic.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        unique_clause = "UNIQUE" if with_unique_index else ""
        cur.execute(f"CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id TEXT {unique_clause})")
        if not with_unique_index:
            pass
        cur.execute("INSERT INTO sessions (session_id) VALUES ('synthetic_session_a')")
        cur.execute("INSERT INTO sessions (session_id) VALUES ('synthetic_session_b')")
        if inject_duplicate:
            cur.execute("INSERT INTO sessions (session_id) VALUES ('synthetic_session_a')")
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_nnc4_passes_on_unique_no_duplicate_schema(tmp_path):
    db_path = _make_synthetic_db(tmp_path, with_unique_index=True, inject_duplicate=False)
    result = verify_session_id_uniqueness(db_path)
    assert result.ok
    assert result.no_duplicate_session_ids
    assert result.unique_index_present


def test_nnc4_fails_when_no_unique_index_present(tmp_path):
    db_path = _make_synthetic_db(tmp_path, with_unique_index=False, inject_duplicate=False)
    result = verify_session_id_uniqueness(db_path)
    assert not result.ok
    assert not result.unique_index_present


def test_nnc4_fails_when_duplicate_session_id_rows_exist(tmp_path):
    db_path = _make_synthetic_db(tmp_path, with_unique_index=False, inject_duplicate=True)
    result = verify_session_id_uniqueness(db_path)
    assert not result.ok
    assert not result.no_duplicate_session_ids
    assert "synthetic_session_a" in result.duplicate_session_ids


def test_nnc4_never_writes_to_database(tmp_path):
    db_path = _make_synthetic_db(tmp_path, with_unique_index=True, inject_duplicate=False)
    mtime_before = db_path.stat().st_mtime_ns
    verify_session_id_uniqueness(db_path)
    assert db_path.stat().st_mtime_ns == mtime_before


def test_nnc4_raises_on_missing_database(tmp_path):
    with pytest.raises(NNC4Error):
        verify_session_id_uniqueness(tmp_path / "does_not_exist.sqlite3")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_ledger_append_and_read_roundtrip(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    rec1 = {"role": "CANDIDATE", "state": "INVOKED", "run_id": "r1"}
    rec2 = {"role": "CANDIDATE", "state": "COMPLETED", "run_id": "r1"}
    append_ledger_record_durable(ledger, rec1)
    append_ledger_record_durable(ledger, rec2)
    records = read_ledger_records(ledger)
    assert records == [rec1, rec2]


def test_ledger_lines_are_canonical_and_lf_terminated(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    append_ledger_record_durable(ledger, {"b": 2, "a": 1})
    raw = ledger.read_bytes()
    assert raw == b'{"a":1,"b":2}\n'


def test_latest_terminal_record_scoped_by_run_id():
    records = [
        {"role": "CANDIDATE", "state": "INVOKED", "run_id": "r1"},
        {"role": "CANDIDATE", "state": "COMPLETED", "run_id": "r1"},
        {"role": "CANDIDATE", "state": "INVOKED", "run_id": "r2"},
    ]
    assert latest_terminal_record(records, "CANDIDATE", "r1")["state"] == "COMPLETED"
    assert latest_terminal_record(records, "CANDIDATE", "r2")["state"] == "INVOKED"


# ---------------------------------------------------------------------------
# exclusive_create_write (§4.11(a))
# ---------------------------------------------------------------------------

def test_exclusive_create_write_refuses_preexisting_path(tmp_path):
    p = tmp_path / "out.bin"
    p.write_bytes(b"seed")
    with pytest.raises(FileExistsError):
        exclusive_create_write(p, b"data")


def test_exclusive_create_write_rereads_and_matches(tmp_path):
    p = tmp_path / "out.bin"
    exclusive_create_write(p, b"hello-world")
    assert p.read_bytes() == b"hello-world"


# ---------------------------------------------------------------------------
# S1 comparator (synthetic PASS / FAIL / INVALID_INPUT)
# ---------------------------------------------------------------------------

def _synthetic_row_set() -> list[dict]:
    """Deterministic 3456-row synthetic set with generic (non-NEW36)
    session identifiers, purely to exercise the S1 comparator's own
    logic (row-count / key-set / sort / field comparison)."""
    from recovery.v1_2_new36_validators import (
        ASSET_ORDER,
        HB_FEATURE_ORDER,
        HB_QUANTILE,
        HG_FEATURE,
        HC_FEATURE,
        HORIZON_ORDER,
        HZ_FEATURE_ORDER,
        QUANTILE_ORDER,
    )
    sessions = [f"synthetic_session_{i:02d}" for i in range(12)]
    rows: list[dict] = []
    for session_id in sessions:
        for asset in ASSET_ORDER:
            for feature in HZ_FEATURE_ORDER:
                for q in QUANTILE_ORDER:
                    for variant in ("T0", "TZ"):
                        rows.append({
                            "protocol_version": "V1_2_NEW36_VALIDATION_PROTOCOL_V2",
                            "session_id": session_id, "asset": asset, "axis": "HZ",
                            "feature_family": feature, "variant_id": variant,
                            "quantile": q, "horizon_ms": None,
                            "threshold_domain_finite_n": 100, "threshold_domain_nonzero_n": 80,
                            "zero_n": 20, "zero_fraction": 0.2, "nonzero_unique_value_n": 50,
                            "hz_discrimination_class": "HZ_DISCRIMINATING", "gate_zero_n": None,
                            "calculated_threshold": 1.5, "spacing_steps": None, "pre_overlap_n": None,
                            "exact_spacing_pair_n": None, "accepted_n": None, "overlap_dropped_n": None,
                            "accepted_positions_sha256": "", "mean_signed_bps": None, "hit_rate": None,
                            "mean_abs_move": None,
                        })
            for feature, variants in ((HG_FEATURE, ("G0", "G1")), (HC_FEATURE, ("C0", "C1"))):
                axis = "HG" if feature == HG_FEATURE else "HC"
                for q in QUANTILE_ORDER:
                    for horizon in HORIZON_ORDER:
                        for variant in variants:
                            rows.append({
                                "protocol_version": "V1_2_NEW36_VALIDATION_PROTOCOL_V2",
                                "session_id": session_id, "asset": asset, "axis": axis,
                                "feature_family": feature, "variant_id": variant,
                                "quantile": q, "horizon_ms": horizon,
                                "threshold_domain_finite_n": None, "threshold_domain_nonzero_n": None,
                                "zero_n": None, "zero_fraction": None, "nonzero_unique_value_n": None,
                                "hz_discrimination_class": None, "gate_zero_n": None,
                                "calculated_threshold": 2.0, "spacing_steps": 10, "pre_overlap_n": 40,
                                "exact_spacing_pair_n": None, "accepted_n": 30, "overlap_dropped_n": 10,
                                "accepted_positions_sha256": "a" * 64, "mean_signed_bps": 0.1, "hit_rate": 0.55,
                                "mean_abs_move": 0.3,
                            })
            for feature in HB_FEATURE_ORDER:
                for horizon in HORIZON_ORDER:
                    for variant in ("B0", "B1"):
                        rows.append({
                            "protocol_version": "V1_2_NEW36_VALIDATION_PROTOCOL_V2",
                            "session_id": session_id, "asset": asset, "axis": "HB",
                            "feature_family": feature, "variant_id": variant,
                            "quantile": HB_QUANTILE, "horizon_ms": horizon,
                            "threshold_domain_finite_n": None, "threshold_domain_nonzero_n": None,
                            "zero_n": None, "zero_fraction": None, "nonzero_unique_value_n": None,
                            "hz_discrimination_class": None, "gate_zero_n": None,
                            "calculated_threshold": 3.0, "spacing_steps": 10, "pre_overlap_n": 20,
                            "exact_spacing_pair_n": 5, "accepted_n": 15, "overlap_dropped_n": 5,
                            "accepted_positions_sha256": "b" * 64, "mean_signed_bps": -0.2, "hit_rate": 0.45,
                            "mean_abs_move": 0.4,
                        })
    return rows


def _rows_to_csv_bytes(rows: list[dict]) -> bytes:
    def fmt(v):
        if v is None:
            return ""
        if isinstance(v, float):
            return repr(v)
        return str(v)

    lines = [",".join(CANDIDATE_ROW_FIELDS)]
    for r in sorted(rows, key=canonical_sort_key):
        lines.append(",".join(fmt(r[f]) for f in CANDIDATE_ROW_FIELDS))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_s1_fixture_pair(tmp_path: Path, mutate_reference: bool = False):
    rows = _synthetic_row_set()
    assert len(rows) == EXPECTED_TOTAL_ROW_COUNT
    cand_csv_bytes = _rows_to_csv_bytes(rows)
    ref_rows = json.loads(json.dumps(rows))
    if mutate_reference:
        ref_rows[0]["calculated_threshold"] = 999.0
    ref_csv_bytes = _rows_to_csv_bytes(ref_rows)

    paths = {}
    for name, csv_bytes, role in (("candidate", cand_csv_bytes, "CANDIDATE"), ("reference", ref_csv_bytes, "REFERENCE")):
        csv_path = tmp_path / f"{name}.csv"
        csv_path.write_bytes(csv_bytes)
        manifest = {
            "implementation_role": role,
            "csv_sha256": sha256_bytes(csv_bytes),
            "csv_size_bytes": len(csv_bytes),
            "manifest_precomparison_sha256": None,
            "note": "synthetic-fixture",
        }
        manifest["manifest_precomparison_sha256"] = sha256_bytes(canonical_pretty_bytes(manifest))
        manifest_path = tmp_path / f"{name}_manifest.json"
        manifest_path.write_bytes(canonical_pretty_bytes(manifest))
        paths[name] = (csv_path, manifest_path)

    run_id = str(uuid.uuid4())
    ledger_path = tmp_path / "ledger.jsonl"
    for name, role in (("candidate", "CANDIDATE"), ("reference", "REFERENCE")):
        csv_path, manifest_path = paths[name]
        append_ledger_record_durable(ledger_path, {
            "role": role, "state": "INVOKED", "run_id": run_id,
        })
        append_ledger_record_durable(ledger_path, {
            "role": role, "state": "COMPLETED", "run_id": run_id,
            "output_artifact_sha256": {
                "csv": sha256_bytes(csv_path.read_bytes()),
                "manifest": sha256_bytes(manifest_path.read_bytes()),
            },
        })
    return S1Inputs(
        candidate_csv=paths["candidate"][0], candidate_manifest=paths["candidate"][1],
        reference_csv=paths["reference"][0], reference_manifest=paths["reference"][1],
        ledger=ledger_path,
    )


def test_s1_passes_on_byte_identical_synthetic_artifacts(tmp_path):
    inputs = _write_s1_fixture_pair(tmp_path, mutate_reference=False)
    report = run_s1(inputs)
    assert report["s1_status"] == "PASS", report
    assert report["row_counts"] == {"candidate": EXPECTED_TOTAL_ROW_COUNT, "reference": EXPECTED_TOTAL_ROW_COUNT}
    assert report["field_mismatch_total"] == 0


def test_s1_fails_on_single_field_mismatch(tmp_path):
    inputs = _write_s1_fixture_pair(tmp_path, mutate_reference=True)
    report = run_s1(inputs)
    assert report["s1_status"] == "FAIL", report
    assert report["field_mismatch_total"] >= 1


def test_s1_invalid_input_on_manifest_hash_tamper(tmp_path):
    inputs = _write_s1_fixture_pair(tmp_path, mutate_reference=False)
    manifest = json.loads(inputs.candidate_manifest.read_bytes())
    manifest["csv_sha256"] = "0" * 64
    inputs.candidate_manifest.write_bytes(canonical_pretty_bytes(manifest))
    report = run_s1(inputs)
    assert report["s1_status"] == "INVALID_INPUT", report


def test_row_canonical_key_lexeme_uses_raw_string_fields():
    row = {f: "" for f in CANDIDATE_ROW_FIELDS}
    row.update({"session_id": "s", "asset": "BTC", "axis": "HZ", "feature_family": "bitget_ofi",
                "variant_id": "T0", "quantile": "0.8", "horizon_ms": ""})
    assert row_canonical_key_lexeme(row) == ("s", "BTC", "HZ", "bitget_ofi", "T0", "0.8", "")
