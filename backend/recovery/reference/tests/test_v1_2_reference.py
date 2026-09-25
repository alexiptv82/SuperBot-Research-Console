"""Synthetic/static tests for the SuperBot V1.2 independent REFERENCE.

Every input here is synthetic and generated in-process. No NEW36, OLD36
quantitative content, golden artifact, Candidate output or real Reference
output is read or compared. Session identifiers used below are synthetic
("SYN..."). Runnable with ``python -m unittest`` or ``pytest``.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import math
import os
import random
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

_REF_PATH = Path(__file__).resolve().parents[1] / "v1_2_reference.py"


def _load_reference():
    name = "v1_2_reference_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _REF_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ref = _load_reference()

SYN_SESSIONS = tuple(f"SYN{i:02d}_session" for i in range(1, 13))


# ---------------------------------------------------------------------------
# Synthetic helpers (independent of the implementation under test)
# ---------------------------------------------------------------------------


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def manual_type7(values, q):
    v = sorted(float(x) for x in values)
    n = len(v)
    if n < 2:
        return None
    h = (n - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, n - 1)
    return v[lo] + (h - lo) * (v[hi] - v[lo])


def manual_avg_rank_z(values: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    idx = np.flatnonzero(eligible)
    x = [float(values[i]) for i in idx]
    n = len(x)
    z = np.full(len(values), np.nan)
    if n == 0:
        return z
    order = sorted(range(n), key=lambda i: x[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and x[order[j + 1]] == x[order[i]]:
            j += 1
        r = ((i + 1) + (j + 1)) / 2.0
        for t in range(i, j + 1):
            ranks[order[t]] = r
        i = j + 1
    for k, row in enumerate(idx):
        z[row] = 2.0 * (ranks[k] / (n + 1.0)) - 1.0
    return z


def manual_greedy(positions, spacing, strict=False):
    out, last = [], None
    for p in positions:
        if last is None or (p - last > spacing if strict else p - last >= spacing):
            out.append(p)
            last = p
    return out


def raw_grid(n=400, seed=0, **overrides) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = {
        "local_ts_ms": 1_000_000 + np.arange(n, dtype=np.int64) * 100,
        "sample_monotonic_ns": np.arange(n, dtype=np.int64) * 100_000_000 + 7,
        "bitget_mid": 100.0 + np.cumsum(rng.normal(0.0, 0.05, n)),
        "bitget_book_age_recv_ms": rng.choice([10.0, 2000.0], n, p=[0.9, 0.1]),
        "adjusted_fair_venue_count": rng.choice([1.0, 3.0], n, p=[0.1, 0.9]),
        "bitget_fair_ofi_alignment": rng.choice([-1.0, 0.0, 1.0], n),
    }
    for col in ref.FEATURE_MAP.values():
        x = np.round(rng.normal(0.0, 1.0, n), 1)
        x[rng.random(n) < 0.05] = np.nan
        data[col] = x
    data.update(overrides)
    return pd.DataFrame(data)


def grid_of(df: pd.DataFrame) -> pd.DataFrame:
    return ref.build_canonical_grid(df)


def rows_by(rows, **kw):
    return [r for r in rows if all(r[k] == v for k, v in kw.items())]


def one(rows, **kw):
    found = rows_by(rows, **kw)
    assert len(found) == 1, (kw, len(found))
    return found[0]


def quality(grid):
    return np.asarray(ref._quality_admissible_mask(grid), dtype=bool)


def fwd_of(grid, h):
    mid = grid["bitget_mid"].to_numpy(dtype=float)
    k = h // 100
    out = np.full(len(mid), np.nan)
    for i in range(len(mid)):
        if i + k <= len(mid) - 1 and mid[i] > 0 and mid[i + k] > 0:
            out[i] = math.log(mid[i + k] / mid[i]) * 10000.0
    return out


# ---------------------------------------------------------------------------
# A. CSV / manifest
# ---------------------------------------------------------------------------

FROZEN_FIELDS = [
    "protocol_version", "session_id", "asset", "axis", "feature_family", "variant_id",
    "quantile", "horizon_ms", "threshold_domain_finite_n", "threshold_domain_nonzero_n",
    "zero_n", "zero_fraction", "nonzero_unique_value_n", "hz_discrimination_class",
    "gate_zero_n", "calculated_threshold", "spacing_steps", "pre_overlap_n",
    "exact_spacing_pair_n", "accepted_n", "overlap_dropped_n", "accepted_positions_sha256",
    "mean_signed_bps", "hit_rate", "mean_abs_move",
]


class TestA_CsvManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid = grid_of(raw_grid(seed=1))
        cls.rows = ref.compute_block_rows("SYN01_session", "BTC", cls.grid)

    def test_exact_25_field_order(self):
        self.assertEqual(list(ref.ROW_FIELDS), FROZEN_FIELDS)
        self.assertEqual(len(ref.ROW_FIELDS), 25)
        header = ref.serialize_csv([]).decode("utf-8")
        self.assertEqual(header, ",".join(FROZEN_FIELDS) + "\n")

    def test_type_partition(self):
        self.assertEqual(len(ref.STRING_FIELDS), 8)
        self.assertEqual(len(ref.INTEGER_FIELDS), 11)
        self.assertEqual(len(ref.FLOAT_FIELDS), 6)
        self.assertEqual(ref.STRING_FIELDS | ref.INTEGER_FIELDS | ref.FLOAT_FIELDS,
                         set(FROZEN_FIELDS))

    def test_null_encoding_and_float_serialization(self):
        self.assertEqual(ref.format_field("calculated_threshold", None), "")
        self.assertEqual(ref.format_field("gate_zero_n", None), "")
        self.assertEqual(ref.format_field("calculated_threshold", np.float64(0.1)), "0.1")
        self.assertEqual(ref.format_field("quantile", 0.8), "0.8")
        self.assertEqual(ref.format_field("mean_signed_bps", 1e-20), "1e-20")
        self.assertEqual(ref.format_field("mean_signed_bps", -0.0), "-0.0")
        self.assertEqual(ref.format_field("hit_rate", np.float64(1) / 3), repr(1 / 3))
        self.assertEqual(ref.format_field("accepted_n", np.int64(5)), "5")
        self.assertNotIn("np.", ref.format_field("mean_abs_move", np.float64(2.5)))
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ref.ArtifactSerializationError):
                ref.format_field("mean_signed_bps", bad)

    def test_csv_lf_no_bom_single_header(self):
        data = ref.serialize_csv(self.rows)
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))
        lines = data.decode("utf-8").split("\n")
        self.assertEqual(lines[-1], "")
        self.assertEqual(len(lines) - 1, len(self.rows) + 1)
        self.assertEqual(sum(1 for ln in lines if ln.startswith("protocol_version,")), 1)
        parsed = list(__import__("csv").reader(io.StringIO(data.decode("utf-8"))))
        self.assertTrue(all(len(r) == 25 for r in parsed))

    def test_hz_null_fields_serialize_empty(self):
        r = one(self.rows, axis="HZ", feature_family="bitget_ofi", variant_id="T0", quantile=0.8)
        line = ref.serialize_csv([r]).decode().split("\n")[1].split(",")
        rec = dict(zip(FROZEN_FIELDS, line))
        for f in ("horizon_ms", "gate_zero_n", "spacing_steps", "pre_overlap_n",
                  "exact_spacing_pair_n", "accepted_n", "overlap_dropped_n",
                  "accepted_positions_sha256", "mean_signed_bps", "hit_rate", "mean_abs_move"):
            self.assertEqual(rec[f], "", f)

    def test_canonical_key_and_sort(self):
        g2 = grid_of(raw_grid(seed=2))
        rows = (ref.compute_block_rows("SYN02_session", "ETH", g2)
                + ref.compute_block_rows("SYN01_session", "ETH", g2)
                + list(self.rows))
        random.Random(5).shuffle(rows)
        order = ("SYN01_session", "SYN02_session")
        ordered, counts = ref.finalize_rows(rows, order, enforce_inventory=False)
        self.assertEqual(counts, {"HZ": 216, "HG": 54, "HC": 54, "HB": 108})
        expected = []
        for s in order:
            for a in ref.ASSETS:
                for axis in ref.AXES:
                    hs = ref.HORIZONS_PER_AXIS[axis] or (None,)
                    for f in ref.FEATURE_SETS_PER_AXIS[axis]:
                        for v in ref.VARIANT_SETS_PER_AXIS[axis]:
                            for q in ref.QUANTILES_PER_AXIS[axis]:
                                for h in hs:
                                    expected.append((s, a, axis, f, v, q, h))
        expected = [k for k in expected if not (k[0] == "SYN02_session" and k[1] == "BTC")]
        self.assertEqual([ref.canonical_row_key(r) for r in ordered], expected)
        self.assertEqual(ref.CANONICAL_KEY_FIELDS,
                         ("session_id", "asset", "axis", "feature_family", "variant_id",
                          "quantile", "horizon_ms"))
        with self.assertRaises(ref.ArtifactSerializationError):
            ref.finalize_rows(list(self.rows) + [dict(self.rows[0])], ("SYN01_session",),
                              enforce_inventory=False)

    def test_inventory_enforced(self):
        with self.assertRaises(ref.StructuralApplicationFailure):
            ref.finalize_rows(list(self.rows), ("SYN01_session",), enforce_inventory=True)

    def test_null_matrix_enforced(self):
        bad = dict(one(self.rows, axis="HG", variant_id="G0", quantile=0.8, horizon_ms=1000))
        bad["gate_zero_n"] = 0
        with self.assertRaises(ref.ArtifactSerializationError):
            ref.validate_row(bad)
        bad = dict(one(self.rows, axis="HB", feature_family="bitget_ofi", variant_id="B0",
                       horizon_ms=1000))
        bad["exact_spacing_pair_n"] = None
        with self.assertRaises(ref.ArtifactSerializationError):
            ref.validate_row(bad)

    def _manifest(self):
        csv_bytes = ref.serialize_csv(self.rows)
        sources = {p: sha(p.encode()) for p in ref.REFERENCE_SOURCE_PATHS}
        return ref.build_reference_manifest(
            runtime_source_commit="a" * 40, source_sha256=sources, csv_bytes=csv_bytes,
            actual_row_counts={"HZ": 1, "HG": 2, "HC": 3, "HB": 4},
            session_ids=SYN_SESSIONS), csv_bytes

    def test_manifest_canonicalization_and_precomparison(self):
        (manifest, data), csv_bytes = self._manifest()
        self.assertEqual(set(manifest), set(ref.REQUIRED_MANIFEST_KEYS))
        self.assertEqual(len(ref.REQUIRED_MANIFEST_KEYS), 30)
        text = data.decode("utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(text, json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        probe = dict(manifest)
        probe["manifest_precomparison_sha256"] = None
        digest = sha((json.dumps(probe, sort_keys=True, indent=2) + "\n").encode())
        self.assertEqual(manifest["manifest_precomparison_sha256"], digest)
        self.assertEqual(manifest["csv_sha256"], sha(csv_bytes))
        self.assertEqual(manifest["csv_size_bytes"], len(csv_bytes))
        self.assertEqual(manifest["actual_total_row_count"], 10)
        ref.validate_reference_manifest_bytes(data, session_ids=SYN_SESSIONS, csv_bytes=csv_bytes)
        tampered = json.loads(text)
        tampered["csv_size_bytes"] += 1
        with self.assertRaises(ref.ManifestValidationError):
            ref.validate_reference_manifest_bytes(
                (json.dumps(tampered, sort_keys=True, indent=2) + "\n").encode())

    def test_manifest_frozen_literals(self):
        (m, _), _ = self._manifest()
        self.assertEqual(m["implementation_role"], "REFERENCE")
        self.assertEqual(m["protocol_version"], "V1_2_NEW36_VALIDATION_PROTOCOL_V2")
        self.assertEqual(m["new36_inventory_id"], "NEW36_12SESSIONS_24BLOCKS_3456ROWS_V1")
        self.assertEqual(m["protocol_sha256"],
                         "5ed8a8b12726d395d25262dc3e4073250be3de94f3d62e64e99167921380ef95")
        self.assertEqual(m["collector_sha256"],
                         "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3")
        self.assertEqual(m["quantiles_per_axis"],
                         {"HZ": [0.8, 0.9, 0.95], "HG": [0.8, 0.9, 0.95],
                          "HC": [0.8, 0.9, 0.95], "HB": [0.9]})
        self.assertEqual(m["horizons_per_axis"]["HZ"], [])
        self.assertEqual(m["expected_row_counts_per_axis"],
                         {"HZ": 1728, "HG": 432, "HC": 432, "HB": 864})
        self.assertEqual(m["feature_sets_per_axis"]["HZ"], list(ref.HZ_FEATURES))
        self.assertIs(m["golden_artifacts_read"], False)
        self.assertIs(m["frozen_analysis_engine_accepts_input"], False)
        self.assertEqual(m["grid_ms"], 100)
        self.assertEqual(m["quantile_method"], "TYPE7_LINEAR")

    def test_duplicate_member_rejection(self):
        cases = [
            b'{"runtime_source_commit":"A","runtime_source_commit":"B"}',
            b'{"a":{"x":1,"x":2}}',
            b'{"a":[{"k":1},{"k":1,"k":2}]}',
            b'{"a":1,"\\u0061":2}',
            b'{"source_sha256":{"p":"1","\\u0070":"2"}}',
            b'{"z":{"y":{"w":{"v":0,"v":0}}}}',
        ]
        for c in cases:
            with self.assertRaises(ref.ManifestValidationError, msg=c):
                ref.strict_json_loads(c)
        self.assertEqual(ref.strict_json_loads(b'{"a":{"b":1},"b":{"a":2}}'),
                         {"a": {"b": 1}, "b": {"a": 2}})

    def test_duplicate_rejected_before_other_validation(self):
        (m, data), _ = self._manifest()
        text = data.decode()
        dup = text.replace('"runtime_source_commit": "' + "a" * 40 + '"',
                           '"runtime_source_commit": "' + "a" * 40 + '",\n  "runtime_source_commit": "'
                           + "b" * 40 + '"', 1)
        self.assertNotEqual(dup, text)
        with self.assertRaisesRegex(ref.ManifestValidationError, "repeated JSON member"):
            ref.validate_reference_manifest_bytes(dup.encode())

    def test_manifest_fail_closed_on_unparseable(self):
        for bad in (b"\xff\xfe", b'{"a":1', b"\xef\xbb\xbf{}", "{}"):
            with self.assertRaises(ref.ManifestValidationError):
                ref.strict_json_loads(bad)

    def test_exact_three_reference_source_paths(self):
        self.assertEqual(ref.REFERENCE_SOURCE_PATHS, (
            "backend/recovery/reference/v1_2_reference.py",
            "backend/recovery/reference/tests/test_v1_2_reference.py",
            "backend/recovery/reference/REFERENCE_INDEPENDENCE_ATTESTATION.txt"))
        good = {p: "0" * 64 for p in ref.REFERENCE_SOURCE_PATHS}
        ref.validate_reference_source_sha256_object(good)
        for mutate in (
            lambda d: d.pop(ref.REFERENCE_SOURCE_PATHS[0]),
            lambda d: d.__setitem__("backend/extra.py", "0" * 64),
            lambda d: d.__setitem__(ref.REFERENCE_SOURCE_PATHS[1], "A" * 64),
            lambda d: d.__setitem__(ref.REFERENCE_SOURCE_PATHS[2], "0" * 63),
        ):
            d = dict(good)
            mutate(d)
            with self.assertRaises(ref.ProvenanceError):
                ref.validate_reference_source_sha256_object(d)

    def test_reference_role_literal(self):
        self.assertEqual(ref.IMPLEMENTATION_ROLE, "REFERENCE")
        (m, data), _ = self._manifest()
        obj = json.loads(data)
        obj["implementation_role"] = "CANDIDATE"
        obj["manifest_precomparison_sha256"] = ref.manifest_precomparison_sha256(obj)
        with self.assertRaises(ref.ManifestValidationError):
            ref.validate_reference_manifest_bytes(ref.canonical_manifest_bytes(obj))


# ---------------------------------------------------------------------------
# B. Fingerprints / counts
# ---------------------------------------------------------------------------


class TestB_FingerprintsCounts(unittest.TestCase):
    def test_signed_little_endian_int64_fingerprint(self):
        expected = sha(b"\x01" + b"\x00" * 7 + b"\x00\x01" + b"\x00" * 6)
        self.assertEqual(ref.accepted_positions_fingerprint([256, 1]), expected)
        self.assertEqual(ref.accepted_positions_fingerprint([-1]), sha(b"\xff" * 8))
        self.assertEqual(ref.accepted_positions_fingerprint(np.array([3], dtype=np.int64)),
                         sha(struct.pack("<q", 3)))
        fp = ref.accepted_positions_fingerprint([5, 2])
        self.assertEqual(fp, fp.lower())
        self.assertEqual(len(fp), 64)

    def test_empty_fingerprint(self):
        self.assertEqual(ref.accepted_positions_fingerprint([]),
                         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(ref.EMPTY_SHA256, sha(b""))

    def test_exact_spacing_consecutive_only(self):
        self.assertEqual(ref.exact_spacing_pair_count([0, 5, 10], 10), 0)
        self.assertEqual(ref.exact_spacing_pair_count([0, 10, 20], 10), 2)
        self.assertEqual(ref.exact_spacing_pair_count([0, 10, 15, 25, 60], 10), 2)
        self.assertEqual(ref.exact_spacing_pair_count([], 10), 0)
        self.assertEqual(ref.exact_spacing_pair_count([7], 10), 0)

    def test_b0_b1_accounting(self):
        pos = np.array([0, 10, 20, 25, 40, 49], dtype=np.int64)
        b0 = pos[ref.overlap_filter_b0(pos, 10)].tolist()
        b1 = pos[ref.overlap_filter_b1(pos, 10)].tolist()
        self.assertEqual(b0, [0, 10, 20, 40])
        self.assertEqual(b1, [0, 20, 40])
        self.assertEqual(b0, manual_greedy(pos.tolist(), 10))
        self.assertEqual(b1, manual_greedy(pos.tolist(), 10, strict=True))
        self.assertLessEqual(len(b1), len(b0))
        no_exact = np.array([0, 11, 30, 55], dtype=np.int64)
        self.assertEqual(ref.exact_spacing_pair_count(no_exact, 10), 0)
        self.assertEqual(no_exact[ref.overlap_filter_b0(no_exact, 10)].tolist(),
                         no_exact[ref.overlap_filter_b1(no_exact, 10)].tolist())
        self.assertEqual(ref.spacing_steps_for(1000), 10)
        self.assertEqual(ref.spacing_steps_for(5000), 50)
        self.assertEqual(ref.spacing_steps_for(30000), 300)
        self.assertEqual(ref.spacing_steps_for(500), 10)

    def test_b0_b1_follow_a14_a15_literally(self):
        # A21 counts CONSECUTIVE pre-overlap pairs only; a non-consecutive
        # exact-spacing gap to the last accepted position still separates
        # B0 (>=) from B1 (>). The Reference applies A14/A15 literally and
        # never coerces B1 to B0.
        pos = np.array([0, 5, 10], dtype=np.int64)
        self.assertEqual(ref.exact_spacing_pair_count(pos, 10), 0)
        self.assertEqual(pos[ref.overlap_filter_b0(pos, 10)].tolist(), [0, 10])
        self.assertEqual(pos[ref.overlap_filter_b1(pos, 10)].tolist(), [0])

    def test_b0_b1_identical_without_any_exact_spacing_difference(self):
        rng = np.random.default_rng(12)
        for _ in range(300):
            spacing = int(rng.choice([10, 50, 300]))
            pos = np.unique(rng.integers(0, 4 * spacing, size=int(rng.integers(0, 25))))
            b0 = pos[ref.overlap_filter_b0(pos, spacing)].tolist()
            b1 = pos[ref.overlap_filter_b1(pos, spacing)].tolist()
            self.assertEqual(b0, manual_greedy(pos.tolist(), spacing))
            self.assertEqual(b1, manual_greedy(pos.tolist(), spacing, strict=True))
            diffs = {int(b - a) for i, a in enumerate(pos) for b in pos[i + 1:]}
            if spacing not in diffs:
                self.assertEqual(b0, b1)

    def test_block_count_accounting_and_pairing(self):
        grid = grid_of(raw_grid(seed=3, n=600))
        rows = ref.compute_block_rows("SYN03_session", "ETH", grid)
        for r in rows:
            if r["accepted_n"] is not None:
                self.assertEqual(r["pre_overlap_n"], r["accepted_n"] + r["overlap_dropped_n"])
        for fam in ref.HB_FAMILIES:
            for h in ref.DIAGNOSTIC_HORIZONS_MS:
                b0 = one(rows, axis="HB", feature_family=fam, variant_id="B0", horizon_ms=h)
                b1 = one(rows, axis="HB", feature_family=fam, variant_id="B1", horizon_ms=h)
                self.assertEqual(b0["pre_overlap_n"], b1["pre_overlap_n"])
                self.assertEqual(b0["exact_spacing_pair_n"], b1["exact_spacing_pair_n"])
                self.assertLessEqual(b1["accepted_n"], b0["accepted_n"])
                self.assertEqual(b0["spacing_steps"], b1["spacing_steps"])

    def test_gate_zero_n(self):
        n = 200
        di = np.full(n, 0.5)
        di[[0, 1, 2]] = 0.0
        di[3] = -0.0
        di[4] = np.nan
        di[5] = np.inf
        age = np.full(n, 10.0)
        age[6] = 5000.0          # non-quality zero
        di[6] = 0.0
        df = raw_grid(seed=4, n=n, bitget_depth_imbalance_l1=di, bitget_book_age_recv_ms=age,
                      adjusted_fair_venue_count=np.full(n, 3.0))
        rows = ref.compute_block_rows("SYN04_session", "BTC", grid_of(df))
        g1 = rows_by(rows, axis="HG", variant_id="G1")
        self.assertEqual(len(g1), 9)
        self.assertEqual({r["gate_zero_n"] for r in g1}, {4})
        for r in rows:
            if (r["axis"], r["variant_id"]) != ("HG", "G1"):
                self.assertIsNone(r["gate_zero_n"])

    def test_mean_abs_move_with_zero_accepted(self):
        n = 300
        df = raw_grid(seed=5, n=n, external_ofi_consensus_l1=np.full(n, np.nan),
                      bitget_book_age_recv_ms=np.full(n, 10.0),
                      adjusted_fair_venue_count=np.full(n, 3.0))
        grid = grid_of(df)
        rows = ref.compute_block_rows("SYN05_session", "BTC", grid)
        q = quality(grid)
        for h in ref.DIAGNOSTIC_HORIZONS_MS:
            f = fwd_of(grid, h)
            dom = q & np.isfinite(f)
            expected = None if not dom.any() else float(np.mean(np.abs(f[dom])))
            g1 = one(rows, axis="HG", variant_id="G1", quantile=0.8, horizon_ms=h)
            self.assertEqual(g1["accepted_n"], 0)
            self.assertIsNone(g1["mean_signed_bps"])
            self.assertIsNone(g1["hit_rate"])
            vals = {r["mean_abs_move"] for r in rows if r["horizon_ms"] == h}
            self.assertEqual(len(vals), 1)
            got = vals.pop()
            if expected is None:
                self.assertIsNone(got)
            else:
                self.assertAlmostEqual(got, expected, delta=1e-12)
        self.assertIsNone(one(rows, axis="HG", variant_id="G1", quantile=0.8,
                              horizon_ms=30000)["mean_abs_move"])  # n-1 < k

    def test_mean_abs_move_null_when_domain_empty(self):
        n = 150
        df = raw_grid(seed=6, n=n, adjusted_fair_venue_count=np.full(n, 1.0))
        rows = ref.compute_block_rows("SYN06_session", "ETH", grid_of(df))
        for r in rows:
            self.assertIsNone(r["mean_abs_move"])
            if r["accepted_n"] is not None:
                self.assertEqual(r["accepted_n"], 0)
                self.assertEqual(r["accepted_positions_sha256"], ref.EMPTY_SHA256)
                self.assertIsNone(r["calculated_threshold"])

    def test_hit_rate_zero_is_miss(self):
        n = 120
        di = np.array([1.0 if i % 20 == 0 else 0.0 for i in range(n)])
        ext = di.copy()
        mid = np.full(n, 100.0)  # all forward returns are exactly zero
        df = raw_grid(seed=7, n=n, bitget_depth_imbalance_l1=di, external_ofi_consensus_l1=ext,
                      bitget_mid=mid, bitget_book_age_recv_ms=np.full(n, 10.0),
                      adjusted_fair_venue_count=np.full(n, 3.0))
        rows = ref.compute_block_rows("SYN07_session", "BTC", grid_of(df))
        g1 = one(rows, axis="HG", variant_id="G1", quantile=0.8, horizon_ms=1000)
        self.assertGreater(g1["accepted_n"], 0)
        self.assertEqual(g1["hit_rate"], 0.0)
        self.assertEqual(g1["mean_signed_bps"], 0.0)


# ---------------------------------------------------------------------------
# C. Threshold / feature semantics
# ---------------------------------------------------------------------------


class TestC_ThresholdFeature(unittest.TestCase):
    def _clean(self, n, **cols):
        base = dict(bitget_book_age_recv_ms=np.full(n, 10.0),
                    adjusted_fair_venue_count=np.full(n, 3.0))
        base.update(cols)
        return grid_of(raw_grid(seed=11, n=n, **base))

    def test_type7_through_primitive(self):
        rng = np.random.default_rng(12)
        for n in (2, 3, 7, 50, 101):
            vals = np.abs(rng.normal(size=n))
            for q in (0.8, 0.9, 0.95):
                self.assertAlmostEqual(ref.type7_threshold(vals, q), manual_type7(vals, q),
                                       delta=1e-12)
        self.assertIsNone(ref.type7_threshold(np.array([1.0]), 0.9))
        self.assertIsNone(ref.type7_threshold(np.array([]), 0.9))

    def test_t0_tz_counts_and_thresholds(self):
        n = 100
        s = np.round(np.linspace(-3, 3, n), 1)
        s[[0, 1]] = np.nan
        grid = self._clean(n, bitget_ofi_norm_l1=s)
        rows = ref.compute_block_rows("SYN11_session", "BTC", grid)
        q = quality(grid)
        t0 = q & np.isfinite(s)
        tz = t0 & (s != 0.0)
        zero_n = int(t0.sum() - tz.sum())
        self.assertGreater(zero_n, 0)
        for qq in ref.QUANTILES_V2:
            r0 = one(rows, axis="HZ", feature_family="bitget_ofi", variant_id="T0", quantile=qq)
            rz = one(rows, axis="HZ", feature_family="bitget_ofi", variant_id="TZ", quantile=qq)
            for r in (r0, rz):
                self.assertEqual(r["threshold_domain_finite_n"], int(t0.sum()))
                self.assertEqual(r["threshold_domain_nonzero_n"], int(tz.sum()))
                self.assertEqual(r["zero_n"], zero_n)
                self.assertEqual(r["zero_fraction"], zero_n / int(t0.sum()))
                self.assertEqual(r["nonzero_unique_value_n"],
                                 len({float(v) for v in np.abs(s[tz])}))
                self.assertIsNone(r["horizon_ms"])
                self.assertIsNone(r["accepted_n"])
            self.assertAlmostEqual(r0["calculated_threshold"], manual_type7(np.abs(s[t0]), qq),
                                   delta=1e-12)
            self.assertAlmostEqual(rz["calculated_threshold"], manual_type7(np.abs(s[tz]), qq),
                                   delta=1e-12)
            expected_cls = ("HZ_DISCRIMINATING"
                            if r0["calculated_threshold"] != rz["calculated_threshold"]
                            else "HZ_NONDISCRIMINATING_EQUAL_THRESHOLD")
            self.assertEqual(r0["hz_discrimination_class"], expected_cls)
            self.assertEqual(rz["hz_discrimination_class"], expected_cls)

    def test_hz_class_literals(self):
        self.assertEqual(ref.hz_discrimination_class(0, 1.0, 2.0), "ZERO_FREE_CONTROL")
        self.assertEqual(ref.hz_discrimination_class(3, 1.0, 2.0), "HZ_DISCRIMINATING")
        self.assertEqual(ref.hz_discrimination_class(3, 0.0, None), "HZ_DISCRIMINATING")
        self.assertEqual(ref.hz_discrimination_class(3, None, None),
                         "HZ_NONDISCRIMINATING_EQUAL_THRESHOLD")
        self.assertEqual(ref.hz_discrimination_class(1, 5.0, 5.0),
                         "HZ_NONDISCRIMINATING_EQUAL_THRESHOLD")

    def test_zero_free_control_equal_thresholds(self):
        n = 60
        s = np.linspace(0.1, 6.0, n)
        grid = self._clean(n, bitget_trade_imbalance_window=s)
        rows = ref.compute_block_rows("SYN12_session", "ETH", grid)
        for qq in ref.QUANTILES_V2:
            r0 = one(rows, axis="HZ", feature_family="bitget_trade_flow", variant_id="T0", quantile=qq)
            rz = one(rows, axis="HZ", feature_family="bitget_trade_flow", variant_id="TZ", quantile=qq)
            self.assertEqual(r0["zero_n"], 0)
            self.assertEqual(r0["hz_discrimination_class"], "ZERO_FREE_CONTROL")
            self.assertLessEqual(abs(r0["calculated_threshold"] - rz["calculated_threshold"]), 1e-12)

    def test_fewer_than_two_values_threshold_null(self):
        n = 50
        s = np.full(n, np.nan)
        s[10] = 0.0
        s[20] = 2.0
        grid = self._clean(n, leader_gap_100ms_bps=s)
        rows = ref.compute_block_rows("SYN13_session", "BTC", grid)
        r0 = one(rows, axis="HZ", feature_family="leader_gap_100ms", variant_id="T0", quantile=0.9)
        rz = one(rows, axis="HZ", feature_family="leader_gap_100ms", variant_id="TZ", quantile=0.9)
        self.assertIsNotNone(r0["calculated_threshold"])
        self.assertIsNone(rz["calculated_threshold"])
        self.assertEqual(r0["hz_discrimination_class"], "HZ_DISCRIMINATING")
        s2 = np.full(n, np.nan)
        s2[3] = 0.0
        rows = ref.compute_block_rows("SYN13_session", "BTC",
                                      self._clean(n, leader_gap_200ms_bps=s2))
        r0 = one(rows, axis="HZ", feature_family="leader_gap_200ms", variant_id="T0", quantile=0.9)
        self.assertIsNone(r0["calculated_threshold"])
        self.assertEqual(r0["hz_discrimination_class"], "HZ_NONDISCRIMINATING_EQUAL_THRESHOLD")
        self.assertIsNone(r0["zero_fraction"] if r0["threshold_domain_finite_n"] == 0 else None)
        empty = np.full(n, np.nan)
        rows = ref.compute_block_rows("SYN13_session", "BTC",
                                      self._clean(n, leader_gap_500ms_bps=empty))
        r = one(rows, axis="HZ", feature_family="leader_gap_500ms", variant_id="T0", quantile=0.8)
        self.assertEqual(r["threshold_domain_finite_n"], 0)
        self.assertIsNone(r["zero_fraction"])
        self.assertEqual(r["nonzero_unique_value_n"], 0)
        self.assertEqual(r["hz_discrimination_class"], "ZERO_FREE_CONTROL")

    def test_absent_feature_column_all_nan(self):
        grid = grid_of(raw_grid(seed=14, n=80).drop(columns=["fair_accel_100ms_bps"]))
        rows = ref.compute_block_rows("SYN14_session", "BTC", grid)
        r = one(rows, axis="HZ", feature_family="fair_accel_100ms", variant_id="TZ", quantile=0.8)
        self.assertEqual(r["threshold_domain_finite_n"], 0)
        self.assertIsNone(r["calculated_threshold"])

    def test_fair_gap_domain_includes_zeros(self):
        n = 120
        gap = np.round(np.linspace(-4, 4, n), 0)
        grid = self._clean(n, bitget_gap_to_fair_bps=gap)
        rows = ref.compute_block_rows("SYN15_session", "BTC", grid)
        q = quality(grid)
        t0 = q & np.isfinite(gap)
        tz = t0 & (gap != 0.0)
        self.assertGreater(int(t0.sum() - tz.sum()), 0)
        for qq in ref.QUANTILES_V2:
            actual = manual_type7(np.abs(gap[t0]), qq)
            diag = manual_type7(np.abs(gap[tz]), qq)
            r0 = one(rows, axis="HZ", feature_family="fair_gap_reversion", variant_id="T0", quantile=qq)
            rz = one(rows, axis="HZ", feature_family="fair_gap_reversion", variant_id="TZ", quantile=qq)
            self.assertAlmostEqual(r0["calculated_threshold"], actual, delta=1e-12)
            self.assertEqual(rz["calculated_threshold"], r0["calculated_threshold"])
            cls = "HZ_DISCRIMINATING" if abs(actual - diag) > 0 else "HZ_NONDISCRIMINATING_EQUAL_THRESHOLD"
            self.assertEqual(rz["hz_discrimination_class"], cls)
            for h in ref.DIAGNOSTIC_HORIZONS_MS:
                for v in ("C0", "C1"):
                    self.assertEqual(one(rows, axis="HC", variant_id=v, quantile=qq,
                                         horizon_ms=h)["calculated_threshold"],
                                     r0["calculated_threshold"])
        for fam in ("gap_depthL1", "gap_localOFI", "gap_depth_extOFI"):
            for h in ref.DIAGNOSTIC_HORIZONS_MS:
                self.assertEqual(one(rows, axis="HB", feature_family=fam, variant_id="B0",
                                     horizon_ms=h)["calculated_threshold"],
                                 one(rows, axis="HZ", feature_family="fair_gap_reversion",
                                     variant_id="T0", quantile=0.9)["calculated_threshold"])

    def test_fair_gap_discriminating_class_uses_diagnostic_threshold(self):
        n = 40
        gap = np.zeros(n)
        gap[:10] = np.arange(1, 11)
        grid = self._clean(n, bitget_gap_to_fair_bps=gap)
        rows = ref.compute_block_rows("SYN16_session", "ETH", grid)
        rz = one(rows, axis="HZ", feature_family="fair_gap_reversion", variant_id="TZ", quantile=0.8)
        r0 = one(rows, axis="HZ", feature_family="fair_gap_reversion", variant_id="T0", quantile=0.8)
        self.assertEqual(rz["calculated_threshold"], r0["calculated_threshold"])
        self.assertAlmostEqual(r0["calculated_threshold"], manual_type7(np.abs(gap), 0.8), delta=1e-12)
        self.assertEqual(rz["hz_discrimination_class"], "HZ_DISCRIMINATING")

    def test_forward_return_uses_bitget_mid_log_return(self):
        n = 100
        mid = 100.0 * np.exp(np.linspace(0, 0.01, n))
        grid = self._clean(n, bitget_mid=mid, canonical_mid=np.full(n, 50.0),
                           microprice=np.full(n, 70.0))
        block = ref._Block(grid)
        for h in ref.DIAGNOSTIC_HORIZONS_MS:
            k = h // 100
            f = block.fwd[h]
            for i in range(n):
                if i + k > n - 1:
                    self.assertTrue(math.isnan(f[i]))
                else:
                    self.assertAlmostEqual(f[i], math.log(mid[i + k] / mid[i]) * 10000.0, delta=1e-9)

    def _g0_expected(self, grid, q_level, h):
        q = quality(grid)
        z_di = manual_avg_rank_z(grid["bitget_depth_imbalance_l1"].to_numpy(float),
                                 q & np.isfinite(grid["bitget_depth_imbalance_l1"].to_numpy(float)))
        z_ex = manual_avg_rank_z(grid["external_ofi_consensus_l1"].to_numpy(float),
                                 q & np.isfinite(grid["external_ofi_consensus_l1"].to_numpy(float)))
        derived = q & np.isfinite(z_di) & np.isfinite(z_ex)
        d = np.where(derived, (z_di + z_ex) / 2.0, np.nan)
        thr = manual_type7(np.abs(d[derived]), q_level)
        return z_di, z_ex, derived, d, thr

    def test_g0_rank_composite(self):
        grid = grid_of(raw_grid(seed=17, n=500))
        rows = ref.compute_block_rows("SYN17_session", "BTC", grid)
        for qq in ref.QUANTILES_V2:
            for h in ref.DIAGNOSTIC_HORIZONS_MS:
                z_di, z_ex, derived, d, thr = self._g0_expected(grid, qq, h)
                r = one(rows, axis="HG", variant_id="G0", quantile=qq, horizon_ms=h)
                self.assertAlmostEqual(r["calculated_threshold"], thr, delta=1e-12)
                f = fwd_of(grid, h)
                thr_used = r["calculated_threshold"]
                with np.errstate(invalid="ignore"):
                    mask = derived & (d != 0) & (np.abs(d) >= thr_used) & np.isfinite(f)
                pre = np.flatnonzero(mask).tolist()
                acc = manual_greedy(pre, max(10, h // 100))
                self.assertEqual(r["pre_overlap_n"], len(pre))
                self.assertEqual(r["accepted_n"], len(acc))
                self.assertEqual(r["accepted_positions_sha256"],
                                 sha(b"".join(struct.pack("<q", p) for p in acc)))
                self.assertIsNone(r["exact_spacing_pair_n"])
                self.assertIsNone(r["gate_zero_n"])
                if acc:
                    signed = [np.sign(d[p]) * f[p] for p in acc]
                    self.assertAlmostEqual(r["mean_signed_bps"], sum(signed) / len(acc), delta=1e-9)
                    self.assertEqual(r["hit_rate"], sum(1 for x in signed if x > 0) / len(acc))
                hb = one(rows, axis="HB", feature_family="depthL1_extOFI", variant_id="B0", horizon_ms=h)
                g09 = one(rows, axis="HG", variant_id="G0", quantile=0.9, horizon_ms=h)
                self.assertEqual(hb["pre_overlap_n"], g09["pre_overlap_n"])
                self.assertEqual(hb["accepted_positions_sha256"], g09["accepted_positions_sha256"])

    def test_c0_composite(self):
        grid = grid_of(raw_grid(seed=18, n=500))
        rows = ref.compute_block_rows("SYN18_session", "ETH", grid)
        q = quality(grid)
        gap = grid["bitget_gap_to_fair_bps"].to_numpy(float)
        for qq in ref.QUANTILES_V2:
            for h in ref.DIAGNOSTIC_HORIZONS_MS:
                z_di, z_ex, derived, d, _ = self._g0_expected(grid, qq, h)
                d_ext = np.where(np.isfinite(z_di) & np.isfinite(z_ex), (z_di + z_ex) / 2, np.nan)
                dom = q & np.isfinite(gap)
                r = one(rows, axis="HC", variant_id="C0", quantile=qq, horizon_ms=h)
                self.assertAlmostEqual(r["calculated_threshold"], manual_type7(np.abs(gap[dom]), qq),
                                       delta=1e-12)
                f = fwd_of(grid, h)
                with np.errstate(invalid="ignore"):
                    mask = (dom & np.isfinite(d_ext) & (gap != 0) & (np.abs(gap) >= r["calculated_threshold"])
                            & (d_ext != 0) & (np.sign(d_ext) == -np.sign(gap)) & np.isfinite(f))
                pre = np.flatnonzero(mask).tolist()
                acc = manual_greedy(pre, max(10, h // 100))
                self.assertEqual(r["pre_overlap_n"], len(pre))
                self.assertEqual(r["accepted_positions_sha256"],
                                 ref.accepted_positions_fingerprint(acc))
                if acc:
                    signed = [-np.sign(gap[p]) * f[p] for p in acc]
                    self.assertAlmostEqual(r["mean_signed_bps"], sum(signed) / len(acc), delta=1e-9)

    def test_g1_raw_signs_and_parent_subset(self):
        n = 400
        rng = np.random.default_rng(19)
        di = np.round(rng.normal(size=n), 1)
        ext = np.abs(rng.normal(size=n)) + 0.01      # all positive raw values
        grid = self._clean(n, bitget_depth_imbalance_l1=di, external_ofi_consensus_l1=ext)
        rows = ref.compute_block_rows("SYN19_session", "BTC", grid)
        q = quality(grid)
        for qq in ref.QUANTILES_V2:
            tz_thr = manual_type7(np.abs(di[q & np.isfinite(di) & (di != 0)]), qq)
            hz = one(rows, axis="HZ", feature_family="depth_imbalance_l1", variant_id="TZ", quantile=qq)
            self.assertLessEqual(abs(hz["calculated_threshold"] - tz_thr), 1e-12)
            for h in ref.DIAGNOSTIC_HORIZONS_MS:
                r = one(rows, axis="HG", variant_id="G1", quantile=qq, horizon_ms=h)
                self.assertEqual(r["calculated_threshold"], hz["calculated_threshold"])
                f = fwd_of(grid, h)
                thr = r["calculated_threshold"]
                parent = q & np.isfinite(di) & (di != 0) & (np.abs(di) >= thr) & np.isfinite(f)
                g1 = parent & (di > 0)       # ext raw positive => only positive di confirm
                self.assertEqual(r["pre_overlap_n"], int(g1.sum()))
                self.assertLessEqual(r["pre_overlap_n"], int(parent.sum()))
                acc = manual_greedy(np.flatnonzero(g1).tolist(), max(10, h // 100))
                self.assertEqual(r["accepted_positions_sha256"], ref.accepted_positions_fingerprint(acc))
                if acc:
                    self.assertAlmostEqual(r["mean_signed_bps"],
                                           sum(f[p] for p in acc) / len(acc), delta=1e-9)

    def test_c1_raw_signs_and_gap_subset(self):
        n = 400
        rng = np.random.default_rng(20)
        gap = np.round(rng.normal(size=n), 1)
        di = -np.sign(gap) * (np.abs(rng.normal(size=n)) + 0.01)
        ext = -np.sign(gap) * (np.abs(rng.normal(size=n)) + 0.01)
        ext[::3] = -ext[::3]
        grid = self._clean(n, bitget_gap_to_fair_bps=gap, bitget_depth_imbalance_l1=di,
                           external_ofi_consensus_l1=ext)
        rows = ref.compute_block_rows("SYN20_session", "ETH", grid)
        q = quality(grid)
        for qq in ref.QUANTILES_V2:
            thr = manual_type7(np.abs(gap[q & np.isfinite(gap)]), qq)
            for h in ref.DIAGNOSTIC_HORIZONS_MS:
                r = one(rows, axis="HC", variant_id="C1", quantile=qq, horizon_ms=h)
                self.assertAlmostEqual(r["calculated_threshold"], thr, delta=1e-12)
                f = fwd_of(grid, h)
                t = r["calculated_threshold"]
                parent = q & np.isfinite(gap) & (gap != 0) & (np.abs(gap) >= t) & np.isfinite(f)
                c1 = parent & (np.sign(di) == -np.sign(gap)) & (np.sign(ext) == -np.sign(gap)) & (di != 0) & (ext != 0)
                self.assertEqual(r["pre_overlap_n"], int(c1.sum()))
                self.assertLess(r["pre_overlap_n"], int(parent.sum()) + 1)
                acc = manual_greedy(np.flatnonzero(c1).tolist(), max(10, h // 100))
                self.assertEqual(r["accepted_positions_sha256"], ref.accepted_positions_fingerprint(acc))

    def test_hb_simple_families_use_t0_domain(self):
        n = 300
        s = np.zeros(n)                              # 80% exact zeros
        s[::5] = np.linspace(-3.0, 3.1, len(s[::5]))  # distinct non-zero values
        grid = self._clean(n, bitget_ofi_norm_l1=s)
        rows = ref.compute_block_rows("SYN21_session", "BTC", grid)
        t0 = one(rows, axis="HZ", feature_family="bitget_ofi", variant_id="T0", quantile=0.9)
        tz = one(rows, axis="HZ", feature_family="bitget_ofi", variant_id="TZ", quantile=0.9)
        self.assertNotEqual(t0["calculated_threshold"], tz["calculated_threshold"])
        for h in ref.DIAGNOSTIC_HORIZONS_MS:
            for v in ("B0", "B1"):
                hb = one(rows, axis="HB", feature_family="bitget_ofi", variant_id=v, horizon_ms=h)
                self.assertEqual(hb["calculated_threshold"], t0["calculated_threshold"])
                self.assertEqual(hb["quantile"], 0.9)
                f = fwd_of(grid, h)
                q = quality(grid)
                mask = (q & np.isfinite(s) & (s != 0) & (np.abs(s) >= hb["calculated_threshold"])
                        & np.isfinite(f))
                pre = np.flatnonzero(mask).tolist()
                self.assertEqual(hb["pre_overlap_n"], len(pre))
                self.assertEqual(hb["exact_spacing_pair_n"],
                                 sum(1 for i in range(len(pre) - 1)
                                     if pre[i + 1] - pre[i] == max(10, h // 100)))
                acc = manual_greedy(pre, max(10, h // 100), strict=(v == "B1"))
                self.assertEqual(hb["accepted_positions_sha256"], ref.accepted_positions_fingerprint(acc))

    def test_hb_gap_local_ofi_alignment(self):
        n = 300
        rng = np.random.default_rng(22)
        gap = np.round(rng.normal(size=n), 1)
        align = rng.choice([-1.0, 0.0, 1.0, np.nan], n)
        grid = self._clean(n, bitget_gap_to_fair_bps=gap, bitget_fair_ofi_alignment=align)
        rows = ref.compute_block_rows("SYN22_session", "ETH", grid)
        q = quality(grid)
        for h in ref.DIAGNOSTIC_HORIZONS_MS:
            r = one(rows, axis="HB", feature_family="gap_localOFI", variant_id="B0", horizon_ms=h)
            f = fwd_of(grid, h)
            mask = (q & np.isfinite(gap) & np.isfinite(align) & (gap != 0)
                    & (np.abs(gap) >= r["calculated_threshold"]) & (align == 1.0) & np.isfinite(f))
            self.assertEqual(r["pre_overlap_n"], int(mask.sum()))
            acc = manual_greedy(np.flatnonzero(mask).tolist(), max(10, h // 100))
            if acc:
                self.assertAlmostEqual(r["mean_signed_bps"],
                                       sum(-np.sign(gap[p]) * f[p] for p in acc) / len(acc), delta=1e-9)

    def test_hb_gap_depth_l1(self):
        n = 300
        grid = grid_of(raw_grid(seed=23, n=n))
        rows = ref.compute_block_rows("SYN23_session", "BTC", grid)
        q = quality(grid)
        gap = grid["bitget_gap_to_fair_bps"].to_numpy(float)
        di = grid["bitget_depth_imbalance_l1"].to_numpy(float)
        for h in ref.DIAGNOSTIC_HORIZONS_MS:
            r = one(rows, axis="HB", feature_family="gap_depthL1", variant_id="B0", horizon_ms=h)
            f = fwd_of(grid, h)
            with np.errstate(invalid="ignore"):
                mask = (q & np.isfinite(gap) & np.isfinite(di) & (gap != 0)
                        & (np.abs(gap) >= r["calculated_threshold"]) & (di != 0)
                        & (np.sign(di) == -np.sign(gap)) & np.isfinite(f))
            self.assertEqual(r["pre_overlap_n"], int(mask.sum()))

    def test_block_row_counts(self):
        rows = ref.compute_block_rows("SYN24_session", "BTC", grid_of(raw_grid(seed=24)))
        self.assertEqual(len(rows), 144)
        counts = {a: sum(1 for r in rows if r["axis"] == a) for a in ref.AXES}
        self.assertEqual(counts, {"HZ": 72, "HG": 18, "HC": 18, "HB": 36})
        for r in rows:
            ref.validate_row(r)
            self.assertEqual(r["protocol_version"], "V1_2_NEW36_VALIDATION_PROTOCOL_V2")

    def test_hz_features_are_v2_appendix_b(self):
        self.assertEqual(ref.HZ_FEATURES, (
            "bitget_ofi", "bitget_trade_flow", "depth_imbalance_l1", "depth_imbalance_l5",
            "external_ofi", "external_trade_flow", "fair_accel_100ms", "fair_gap_reversion",
            "leader_gap_100ms", "leader_gap_200ms", "leader_gap_500ms", "leader_gap_1000ms"))
        self.assertEqual(ref.HB_FAMILIES, ("depth_imbalance_l1", "gap_depthL1", "gap_localOFI",
                                           "bitget_ofi", "depthL1_extOFI", "gap_depth_extOFI"))


# ---------------------------------------------------------------------------
# D. AMB-R3 structural validation and loader
# ---------------------------------------------------------------------------

CLEAN = b"PAR1" + b"payload-bytes" + b"PAR1"


def fake_md(buf: bytes):
    return (False, "synthetic metadata failure") if b"BADMETA" in buf else (True, "")


def write_zip(path: Path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


def sync_parts(indices, fmt="sync_grid_100ms/part-{:03d}.parquet"):
    return [(fmt.format(i), CLEAN) for i in indices]


class TestD_R3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cov(self, entries, expected):
        z = write_zip(self.dir / f"s{random.random()}.zip", entries)
        v = ref.validate_all_parquet(z, metadata_checker=fake_md)
        return v, ref.evaluate_sync_grid_coverage(v, expected)

    def test_c1_observed_min_anchor_expected_none(self):
        v, (ok, reason) = self.cov(sync_parts(range(5, 395)), None)
        sd = v.per_dir["sync_grid_100ms"]
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertTrue(sd.sequence_ok)
        self.assertEqual(sd.sequence_detail, "contiguous 0..394")
        self.assertEqual(sd.missing_parts, [])
        self.assertEqual(sd.parts_expected, 390)
        self.assertEqual(sd.files_seen, 390)

    def test_c2_expected_count_mismatch(self):
        _, (ok, reason) = self.cov(sync_parts(range(5, 395)), 395)
        self.assertFalse(ok)
        self.assertIn("expects 395 parts, found 390", reason)

    def test_c3_tail_truncation(self):
        self.assertFalse(self.cov(sync_parts(range(0, 390)), 400)[1][0])
        self.assertEqual(self.cov(sync_parts(range(0, 390)), None)[1], (True, None))

    def test_single_part_lo_gt_zero_valid(self):
        v, res = self.cov(sync_parts([7]), None)
        self.assertEqual(res, (True, None))
        self.assertTrue(v.per_dir["sync_grid_100ms"].sequence_ok)

    def test_c4_cross_bucket_substring_capture(self):
        entries = sync_parts(range(10)) + [("sync_grid_100ms_old/part-000.parquet", b"garbage-bytes")]
        v, (ok, reason) = self.cov(entries, None)
        self.assertTrue(v.per_dir["sync_grid_100ms"].sequence_ok)
        self.assertEqual(v.per_dir["_unassigned_"].files_seen, 1)
        self.assertIn("sync_grid_100ms_old/part-000.parquet: (unassigned dir) PAR1 missing", v.failures)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("sync_grid_100ms: "))
        self.assertNotIn("sequence invalid", reason)
        self.assertNotIn("authoritative manifest", reason)

    def test_substring_capture_from_other_dataset_dir(self):
        entries = sync_parts(range(3)) + [("normalized_trades/x_sync_grid_100ms_part-0.parquet", b"bad!bad!bad")]
        v, (ok, _) = self.cov(entries, 3)
        self.assertEqual(v.per_dir["normalized_trades"].files_seen, 1)
        self.assertFalse(ok)

    def test_c5_head_plus_tail(self):
        entries = sync_parts([0, 1, 2]) + [("sync_grid_100ms/part-003.parquet", b"XXXXpayloadYYYY")] \
            + sync_parts(range(4, 10))
        v, (ok, _) = self.cov(entries, None)
        self.assertIn("sync_grid_100ms/part-003.parquet: PAR1 magic missing (head+tail)", v.failures)
        self.assertEqual(v.parquet_metadata_checked, 10)
        self.assertTrue(v.per_dir["sync_grid_100ms"].sequence_ok)
        self.assertFalse(ok)
        v2, _ = self.cov([("sync_grid_100ms/part-000.parquet", b"XXXXpayloadPAR1"),
                          ("sync_grid_100ms/part-001.parquet", b"PAR1payloadYYYY")], None)
        self.assertIn("sync_grid_100ms/part-000.parquet: PAR1 magic missing (head)", v2.failures)
        self.assertIn("sync_grid_100ms/part-001.parquet: PAR1 magic missing (tail)", v2.failures)

    def test_c6_size_below_8_routing(self):
        entries = sync_parts([0, 1, 2]) + [("sync_grid_100ms/part-003.parquet", b"PAR1x")] \
            + sync_parts(range(4, 10))
        v, (ok, _) = self.cov(entries, 10)
        self.assertIn("sync_grid_100ms/part-003.parquet: size 5 < 8", v.failures)
        sd = v.per_dir["sync_grid_100ms"]
        self.assertTrue(sd.sequence_ok)
        self.assertEqual(sd.files_seen, 10)
        self.assertEqual(v.parquet_metadata_failed, 1)
        self.assertEqual(v.parquet_magic_failed, 1)
        self.assertFalse(ok)

    def test_c7_all_unparseable_overwrite(self):
        v, (ok, reason) = self.cov([("sync_grid_100ms/a.parquet", CLEAN),
                                    ("sync_grid_100ms/b.parquet", CLEAN)], None)
        sd = v.per_dir["sync_grid_100ms"]
        self.assertEqual(sd.files_seen, 2)
        self.assertFalse(sd.sequence_ok)
        self.assertEqual(sd.sequence_detail, "no parquet files present")
        self.assertEqual(sd.unparseable_names, ["sync_grid_100ms/a.parquet", "sync_grid_100ms/b.parquet"])
        self.assertFalse(ok)
        self.assertEqual(reason, "sync_grid_100ms: sequence invalid (no parquet files present)")

    def test_sequence_detail_table(self):
        cases = {
            (0, 1, 3): "missing 1",
            (0, 1, 1, 2): "duplicated 1",
            (0, 2, 2, 5, 5): "missing 3, duplicated 2",   # missing {1,3,4}; distinct dups {2,5}
        }
        for idx, detail in cases.items():
            entries = [(f"sync_grid_100ms/d{k}/part-{i}.parquet", CLEAN) for k, i in enumerate(idx)]
            v, (ok, reason) = self.cov(entries, None)
            self.assertEqual(v.per_dir["sync_grid_100ms"].sequence_detail, detail)
            self.assertFalse(ok)
            self.assertIn("sequence invalid", reason)
        v, _ = self.cov([("sync_grid_100ms/x.parquet", CLEAN)] + sync_parts([0, 2]), None)
        self.assertEqual(v.per_dir["sync_grid_100ms"].sequence_detail,
                         "1 entries not matching part-XXX.parquet; missing 1")
        self.assertIn("sync_grid_100ms: missing part indices sample=[1]", v.failures)
        v, _ = self.cov([("sync_grid_100ms/x.parquet", CLEAN)] + sync_parts([0, 1]), None)
        self.assertEqual(v.per_dir["sync_grid_100ms"].sequence_detail,
                         "1 entries not matching part-XXX.parquet")

    def test_leading_zero_index_parse(self):
        self.assertEqual(ref.parse_part_index("x/y/part-0000269.parquet"), 269)
        self.assertIsNone(ref.parse_part_index("x/part-abc.parquet"))
        self.assertEqual(ref.parse_part_index("dir9/7.parquet"), 7)

    def test_c8_classification(self):
        self.assertEqual(ref.classify_parquet_entry("normalized_books/sync_grid_100ms/part-1.parquet"),
                         "sync_grid_100ms")
        self.assertEqual(ref.classify_parquet_entry("session_xyz/sync_grid_100ms/part-001.parquet"),
                         "sync_grid_100ms")
        self.assertEqual(ref.classify_parquet_entry("sync_grid_100ms_old/part-000.parquet"), "_unassigned_")
        self.assertEqual(ref.classify_parquet_entry("sync_grid_100ms_v2/part-000.parquet"), "_unassigned_")
        self.assertEqual(ref.classify_parquet_entry("normalized_trades/part-1.parquet"), "normalized_trades")

    def test_c10_expected_zero_with_parts(self):
        _, (ok, reason) = self.cov(sync_parts(range(4)), 0)
        self.assertFalse(ok)
        self.assertIn("expects 0 parts, found 4", reason)

    def test_c11_no_parts_precedes_count(self):
        for expected in (400, 0, None):
            _, res = self.cov([("normalized_books/part-000.parquet", CLEAN)], expected)
            self.assertEqual(res, (False, "sync_grid_100ms: no parquet parts present"))

    def test_c12_metadata_failure_routing(self):
        entries = sync_parts([0, 1]) + [("sync_grid_100ms/part-002.parquet", b"PAR1BADMETAPAR1")] \
            + sync_parts(range(3, 10))
        v, (ok, _) = self.cov(entries, 10)
        self.assertTrue(any(f.startswith("sync_grid_100ms/part-002.parquet: metadata unreadable (")
                            for f in v.failures))
        self.assertTrue(v.per_dir["sync_grid_100ms"].sequence_ok)
        self.assertEqual(v.parquet_magic_failed, 0)
        self.assertFalse(ok)

    def test_unassigned_failure_strings(self):
        v, _ = self.cov(sync_parts([0]) + [("other/a.parquet", b"tiny"),
                                           ("other/b.parquet", b"PAR1BADMETAPAR1")], None)
        self.assertIn("other/a.parquet: (unassigned dir) size < min", v.failures)
        self.assertIn("other/b.parquet: (unassigned dir) metadata: synthetic metadata failure", v.failures)
        self.assertEqual(v.per_dir["_unassigned_"].sequence_detail, "not enforced (unassigned)")

    def test_pyarrow_unavailable_branch(self):
        saved = ref._pq, ref._PYARROW_IMPORT_ERROR
        try:
            ref._pq, ref._PYARROW_IMPORT_ERROR = None, ImportError("synthetic")
            ok, err = ref.check_parquet_metadata(CLEAN)
            self.assertFalse(ok)
            self.assertEqual(err, "pyarrow not available: synthetic")
        finally:
            ref._pq, ref._PYARROW_IMPORT_ERROR = saved

    @unittest.skipIf(ref._pq is None, "pyarrow not installed in this interpreter")
    def test_real_pyarrow_metadata_checker(self):  # pragma: no cover - env dependent
        import pyarrow as pa
        sink = io.BytesIO()
        ref._pq.write_table(pa.table({"a": [1, 2]}), sink)
        self.assertEqual(ref.check_parquet_metadata(sink.getvalue()), (True, ""))
        ok, _ = ref.check_parquet_metadata(b"PAR1garbagePAR1")
        self.assertFalse(ok)

    # ---- PENDING vs STRUCTURAL_INVALID, caching, loader -------------------

    def _db(self, rows):
        p = self.dir / f"db{random.random()}.sqlite"
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE qa_runs (id INTEGER PRIMARY KEY, sync_grid_file_count INTEGER)")
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id VARCHAR, current_qa_run_id INTEGER)")
        conn.execute("CREATE UNIQUE INDEX ix_sessions_session_id ON sessions (session_id)")
        for i, (sid, count) in enumerate(rows, start=1):
            conn.execute("INSERT INTO qa_runs (id, sync_grid_file_count) VALUES (?, ?)", (i, count))
            conn.execute("INSERT INTO sessions (session_id, current_qa_run_id) VALUES (?, ?)", (sid, i))
        conn.commit()
        conn.close()
        return p

    def test_c9_filenotfound_pending_vs_coverage_invalid(self):
        calls = []

        def missing(sid):
            calls.append(sid)
            raise FileNotFoundError(sid)

        src = ref.SessionSource(missing, db_path=None, metadata_checker=fake_md)
        self.assertEqual(src.validate_part_coverage("SYN_A"), (True, None))
        self.assertIn("SYN_A", src._part_coverage_cache)
        self.assertEqual(src.block("SYN_A", "BTC").status, "PENDING")
        self.assertEqual(src.block("SYN_A", "ETH").status, "PENDING")

        z = write_zip(self.dir / "bad.zip", sync_parts([0, 2]))
        src2 = ref.SessionSource(lambda sid: z, db_path=None, metadata_checker=fake_md)
        out = src2.block("SYN_B", "BTC")
        self.assertEqual(out.status, "STRUCTURAL_INVALID")
        self.assertIn("sequence invalid", out.reason)

    def test_coverage_cached_per_session(self):
        z = write_zip(self.dir / "ok.zip", sync_parts([0, 1]))
        n = {"v": 0}
        original = ref.validate_all_parquet

        def counting(path, **kw):
            n["v"] += 1
            return original(path, **kw)

        src = ref.SessionSource(lambda sid: z, db_path=None, metadata_checker=fake_md)
        ref.validate_all_parquet = counting
        try:
            src.validate_part_coverage("SYN_C")
            src.validate_part_coverage("SYN_C")
        finally:
            ref.validate_all_parquet = original
        self.assertEqual(n["v"], 1)

    def test_authoritative_count_from_db(self):
        db = self._db([("SYN_D", 2), ("SYN_E", None), ("SYN_F", 0)])
        self.assertEqual(ref.authoritative_sync_grid_count(db, "SYN_D"), 2)
        self.assertIsNone(ref.authoritative_sync_grid_count(db, "SYN_E"))
        self.assertEqual(ref.authoritative_sync_grid_count(db, "SYN_F"), 0)
        self.assertIsNone(ref.authoritative_sync_grid_count(db, "SYN_absent"))
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO sessions (session_id, current_qa_run_id) VALUES ('SYN_G', NULL)")
        conn.execute("INSERT INTO sessions (session_id, current_qa_run_id) VALUES ('SYN_H', 999)")
        conn.commit()
        conn.close()
        self.assertIsNone(ref.authoritative_sync_grid_count(db, "SYN_G"))
        self.assertIsNone(ref.authoritative_sync_grid_count(db, "SYN_H"))
        z = write_zip(self.dir / "c.zip", sync_parts([0, 1, 2]))
        src = ref.SessionSource(lambda sid: z, db_path=db, metadata_checker=fake_md)
        self.assertFalse(src.validate_part_coverage("SYN_D")[0])
        self.assertEqual(src.validate_part_coverage("SYN_absent"), (True, None))

    def test_entry_selection(self):
        names = ["sync_grid_100ms/ETHUSDT/part-0.parquet", "sync_grid_100ms/BTCUSDT/part-1.parquet",
                 "sync_grid_100ms/BTCUSDT/part-0.parquet", "BTC/other.parquet",
                 "sync_grid_100ms/BTCUSDT/readme.txt"]
        self.assertEqual(ref.select_sync_grid_entries(names, "BTC"),
                         ["sync_grid_100ms/BTCUSDT/part-0.parquet", "sync_grid_100ms/BTCUSDT/part-1.parquet"])
        names = ["BTC_run/sync_grid_100ms/part-0.parquet", "x/sync_grid_100ms/part-0.parquet"]
        self.assertEqual(ref.select_sync_grid_entries(names, "BTC"), ["BTC_run/sync_grid_100ms/part-0.parquet"])
        names = ["sync_grid_100ms/b/part-0.parquet", "sync_grid_100ms/a/part-1.parquet",
                 "sync_grid_100ms_old/part-0.parquet"]
        self.assertEqual(ref.select_sync_grid_entries(names, "ETH"),
                         ["sync_grid_100ms/a/part-1.parquet", "sync_grid_100ms/b/part-0.parquet",
                          "sync_grid_100ms_old/part-0.parquet"])
        self.assertEqual(ref.select_sync_grid_entries(["Sync_Grid_100ms/BTC.parquet"], "BTC"), [])
        self.assertEqual(ref.select_sync_grid_entries(["sync_grid_100ms/btc.parquet"], "BTC"),
                         ["sync_grid_100ms/btc.parquet"])

    @staticmethod
    def _payload(df: pd.DataFrame) -> bytes:
        return b"PAR1" + df.to_csv(index=False).encode() + b"PAR1"

    @staticmethod
    def _reader(buf: bytes) -> pd.DataFrame:
        return pd.read_csv(io.BytesIO(buf[4:-4]))

    def test_loader_filter_concat_and_cases(self):
        a = pd.DataFrame({"asset": ["BTC", "ETH", "BTC"], "v": [1, 2, 3]})
        b = pd.DataFrame({"v": [9]})
        z = write_zip(self.dir / "l.zip", [("sync_grid_100ms/part-001.parquet", self._payload(a)),
                                          ("sync_grid_100ms/part-000.parquet", self._payload(b))])
        out = ref.load_asset_frame(z, "BTC", parquet_reader=self._reader)
        self.assertEqual(out["v"].tolist(), [9, 1, 3])     # sorted: part-000 first; no-asset part kept
        self.assertEqual(list(out.index), [0, 1, 2])
        z2 = write_zip(self.dir / "l2.zip", [("sync_grid_100ms/part-000.parquet",
                                              self._payload(pd.DataFrame({"asset": ["ETH"], "v": [1]})))])
        with self.assertRaisesRegex(ref.BlockDataUnavailableError, "Case D"):
            ref.load_asset_frame(z2, "BTC", parquet_reader=self._reader)
        z3 = write_zip(self.dir / "l3.zip", [("normalized_books/part-000.parquet", CLEAN)])
        with self.assertRaisesRegex(ref.BlockDataUnavailableError, "Case B"):
            ref.load_asset_frame(z3, "BTC", parquet_reader=lambda b: (_ for _ in ()).throw(AssertionError()))
        src = ref.SessionSource(lambda sid: z2, db_path=None, parquet_reader=self._reader,
                                metadata_checker=fake_md)
        self.assertEqual(src.block("SYN_I", "BTC").status, "STRUCTURAL_INVALID")

    def test_corrupt_grid_structural(self):
        df = pd.DataFrame({"asset": ["BTC", "BTC"], "local_ts_ms": [1, 1], "sample_monotonic_ns": [5, 5],
                           "bitget_mid": [1.0, 2.0]})
        z = write_zip(self.dir / "cg.zip", [("sync_grid_100ms/part-000.parquet", self._payload(df))])
        src = ref.SessionSource(lambda sid: z, db_path=None, parquet_reader=self._reader,
                                metadata_checker=fake_md)
        out = src.block("SYN_J", "BTC")
        self.assertEqual(out.status, "STRUCTURAL_INVALID")
        self.assertIn("CORRUPT_GRID", out.reason)


# ---------------------------------------------------------------------------
# E. NNC-1 / NNC-2
# ---------------------------------------------------------------------------

GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="syn", GIT_AUTHOR_EMAIL="syn@example.invalid",
               GIT_COMMITTER_NAME="syn", GIT_COMMITTER_EMAIL="syn@example.invalid")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=GIT_ENV)


def make_repo(root: Path, *, commit=True, object_format=None, contents=None):
    root.mkdir(parents=True, exist_ok=True)
    init = ["init", "-q"] + ([f"--object-format={object_format}"] if object_format else [])
    git(root, *init)
    git(root, "config", "core.autocrlf", "false")
    contents = contents or {
        ref.REFERENCE_SOURCE_PATHS[0]: b"# synthetic module\r\nx = 1\n",
        ref.REFERENCE_SOURCE_PATHS[1]: b"# synthetic tests\n",
        ref.REFERENCE_SOURCE_PATHS[2]: b"synthetic attestation\n",
        "backend/unrelated.py": b"u = 0\n",
    }
    for rel, data in contents.items():
        p = root.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    if commit:
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "synthetic")
    return root


class TestE_Provenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_head_and_sources(self):
        repo = make_repo(self.dir / "r")
        head = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        commit = ref.resolve_runtime_source_commit(repo)
        self.assertEqual(commit, head)
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        src = ref.compute_reference_source_sha256(repo, commit)
        self.assertEqual(tuple(src), ref.REFERENCE_SOURCE_PATHS)
        self.assertEqual(src[ref.REFERENCE_SOURCE_PATHS[0]], sha(b"# synthetic module\r\nx = 1\n"))

    def test_head_value_validation(self):
        for bad in ("A" * 40, "a" * 39, "a" * 41, "a" * 64, "g" * 40, "", None, 12, "a" * 40 + "\n"):
            with self.assertRaises(ref.ProvenanceError):
                ref.validate_runtime_source_commit_value(bad)
        self.assertEqual(ref.validate_runtime_source_commit_value("0" * 40), "0" * 40)

    def test_unresolvable_head(self):
        with self.assertRaises(ref.ProvenanceError):
            ref.resolve_runtime_source_commit(make_repo(self.dir / "empty", commit=False))
        (self.dir / "plain").mkdir()
        with self.assertRaises(ref.ProvenanceError):
            ref.resolve_runtime_source_commit(self.dir / "plain")
        with self.assertRaises(ref.ProvenanceError):
            ref.resolve_runtime_source_commit(self.dir, git_executable=str(self.dir / "no-such-git"))

    def test_sha256_object_format_rejected(self):
        repo = make_repo(self.dir / "r256", object_format="sha256")
        self.assertEqual(len(git(repo, "rev-parse", "HEAD").stdout.decode().strip()), 64)
        with self.assertRaises(ref.ProvenanceError):
            ref.resolve_runtime_source_commit(repo)

    def _fake_git(self, output: str) -> str:
        p = self.dir / f"fakegit{random.random()}.sh"
        p.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def test_resolved_but_nonconforming_head(self):
        for out in ("ABCDEF" + "0" * 34, "abc1234", "  " + "f" * 40 + "\n"):
            fake = self._fake_git(out)
            if out.startswith("  "):
                self.assertEqual(ref.resolve_runtime_source_commit(self.dir, git_executable=fake), "f" * 40)
            else:
                with self.assertRaises(ref.ProvenanceError):
                    ref.resolve_runtime_source_commit(self.dir, git_executable=fake)

    def test_dirty_in_scope_fails(self):
        repo = make_repo(self.dir / "r")
        commit = ref.resolve_runtime_source_commit(repo)
        target = repo.joinpath(*ref.REFERENCE_SOURCE_PATHS[2].split("/"))
        target.write_bytes(b"modified\n")
        with self.assertRaises(ref.ProvenanceError):
            ref.compute_reference_source_sha256(repo, commit)
        git(repo, "add", "-A")                 # staged but not committed still fails
        with self.assertRaises(ref.ProvenanceError):
            ref.compute_reference_source_sha256(repo, commit)

    def test_line_ending_change_in_scope_fails(self):
        repo = make_repo(self.dir / "r")
        commit = ref.resolve_runtime_source_commit(repo)
        target = repo.joinpath(*ref.REFERENCE_SOURCE_PATHS[0].split("/"))
        target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n"))
        with self.assertRaises(ref.ProvenanceError):
            ref.compute_reference_source_sha256(repo, commit)

    def test_untracked_in_scope_fails(self):
        contents = {ref.REFERENCE_SOURCE_PATHS[0]: b"a\n", ref.REFERENCE_SOURCE_PATHS[1]: b"b\n"}
        repo = make_repo(self.dir / "r", contents=contents)
        att = repo.joinpath(*ref.REFERENCE_SOURCE_PATHS[2].split("/"))
        att.write_bytes(b"untracked\n")
        commit = ref.resolve_runtime_source_commit(repo)
        with self.assertRaises(ref.ProvenanceError):
            ref.compute_reference_source_sha256(repo, commit)
        att.unlink()
        with self.assertRaises(ref.ProvenanceError):
            ref.compute_reference_source_sha256(repo, commit)

    def test_unrelated_dirty_file_passes(self):
        repo = make_repo(self.dir / "r")
        commit = ref.resolve_runtime_source_commit(repo)
        (repo / "backend" / "unrelated.py").write_bytes(b"changed\n")
        (repo / "scratch.txt").write_bytes(b"untracked\n")
        src = ref.compute_reference_source_sha256(repo, commit)
        self.assertEqual(len(src), 3)

    def test_repo_root_must_be_toplevel(self):
        repo = make_repo(self.dir / "r")
        commit = ref.resolve_runtime_source_commit(repo)
        with self.assertRaises(ref.ProvenanceError):
            ref.compute_reference_source_sha256(repo / "backend", commit)


# ---------------------------------------------------------------------------
# F. NNC-3
# ---------------------------------------------------------------------------


class FakeDist:
    def __init__(self, name, version):
        self.metadata = {} if name is None else {"Name": name}
        self.version = version


class TestF_Environment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.req = Path(self.tmp.name) / "requirements.txt"
        self.req.write_bytes(b"pyarrow==17.0.0\r\nnumpy\n")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, dists, version="17.0.0", **kw):
        return ref.build_runtime_environment_identity(
            requirements_path=self.req, distributions=dists,
            pyarrow_version_loader=lambda: version, implementation_name="cpython",
            python_version="3.11.16 (synthetic)", **kw)

    def test_identity_construction_normalization_sort_duplicates(self):
        dists = [FakeDist("Zope.Interface", "6.0"), FakeDist("A--__..B", "1"),
                 FakeDist("foo_bar.baz", "1.0.0.POST1"), FakeDist("b", "10"),
                 FakeDist("b", "2"), FakeDist("B", "2")]
        ident = self.build(dists)
        self.assertEqual(set(ident), {"python", "installed_distributions", "requirements_sha256",
                                      "pyarrow_version"})
        self.assertEqual(ident["python"], {"implementation": "cpython", "version": "3.11.16 (synthetic)"})
        self.assertEqual(ident["installed_distributions"], [
            ["a-b", "1"], ["b", "10"], ["b", "2"], ["b", "2"],
            ["foo-bar-baz", "1.0.0.POST1"], ["zope-interface", "6.0"]])
        self.assertEqual(ident["requirements_sha256"], sha(b"pyarrow==17.0.0\r\nnumpy\n"))
        self.assertEqual(ident["pyarrow_version"], "17.0.0")
        self.assertEqual(ref.normalize_distribution_name("Foo__Bar-.-Baz"), "foo-bar-baz")

    def test_order_independence(self):
        dists = [FakeDist("x", "1"), FakeDist("a", "2"), FakeDist("m", "3")]
        self.assertEqual(self.build(dists), self.build(list(reversed(dists))))

    def test_canonical_bytes_and_digest(self):
        ident = self.build([FakeDist("pkg", "1.0\u00e9")])
        data = ref.canonical_environment_identity_bytes(ident)
        self.assertFalse(data.endswith(b"\n"))
        self.assertNotIn(b": ", data)
        self.assertNotIn(b", ", data.replace(b"(synthetic)", b""))
        self.assertIn(b"\\u00e9", data)
        self.assertEqual(data, json.dumps(ident, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True).encode())
        self.assertTrue(data.startswith(b'{"installed_distributions":'))
        self.assertEqual(ref.runtime_environment_identity_sha256(ident), sha(data))

    def test_failures(self):
        with self.assertRaises(ref.EnvironmentIdentityError):
            self.build([FakeDist("", "1")])
        with self.assertRaises(ref.EnvironmentIdentityError):
            self.build([FakeDist(None, "1")])
        with self.assertRaises(ref.EnvironmentIdentityError):
            self.build([FakeDist("x", "")])
        with self.assertRaises(ref.EnvironmentIdentityError):
            self.build([FakeDist("x", None)])
        with self.assertRaises(ref.EnvironmentIdentityError):
            self.build([FakeDist("x", "1")], version="16.1.0")

        def missing():
            raise ImportError("No module named 'pyarrow'")

        with self.assertRaises(ref.EnvironmentIdentityError):
            ref.build_runtime_environment_identity(
                requirements_path=self.req, distributions=[], pyarrow_version_loader=missing)
        with self.assertRaises(ref.EnvironmentIdentityError):
            ref.build_runtime_environment_identity(
                requirements_path=Path(self.tmp.name) / "absent.txt", distributions=[],
                pyarrow_version_loader=lambda: "17.0.0")

    def test_default_loader_fails_without_module_level_pyarrow(self):
        saved = ref._pq, ref._PYARROW_IMPORT_ERROR
        try:
            ref._pq, ref._PYARROW_IMPORT_ERROR = None, ImportError("synthetic")
            with self.assertRaises(ref.EnvironmentIdentityError):
                ref.build_runtime_environment_identity(requirements_path=self.req, distributions=[])
        finally:
            ref._pq, ref._PYARROW_IMPORT_ERROR = saved

    def test_live_python_identity_members(self):
        ident = ref.build_runtime_environment_identity(requirements_path=self.req,
                                                       pyarrow_version_loader=lambda: "17.0.0")
        self.assertEqual(ident["python"]["implementation"], sys.implementation.name)
        self.assertEqual(ident["python"]["version"], sys.version)
        self.assertEqual(ident["installed_distributions"], sorted(ident["installed_distributions"]))

    def test_full_object_and_hash_equality(self):
        a = self.build([FakeDist("x", "1")])
        b = json.loads(json.dumps(a))
        ha, hb = ref.runtime_environment_identity_sha256(a), ref.runtime_environment_identity_sha256(b)
        ref.verify_environment_equivalence(a, ha, b, hb)
        c = self.build([FakeDist("x", "2")])
        with self.assertRaises(ref.EnvironmentIdentityError):
            ref.verify_environment_equivalence(a, ha, c, ref.runtime_environment_identity_sha256(c))
        with self.assertRaises(ref.EnvironmentIdentityError):
            ref.verify_environment_equivalence(a, ha, b, "0" * 64)
        d = dict(b)
        d["python"] = {"implementation": "cpython", "version": "3.12.0"}
        with self.assertRaises(ref.EnvironmentIdentityError):
            ref.verify_environment_equivalence(a, ha, d, ref.runtime_environment_identity_sha256(d))
        e = dict(b)
        e["extra"] = 1
        with self.assertRaises(ref.EnvironmentIdentityError):
            ref.verify_environment_equivalence(a, ha, e, ref.runtime_environment_identity_sha256(e))


# ---------------------------------------------------------------------------
# G. NNC-4
# ---------------------------------------------------------------------------


class TestG_SessionUniqueness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self, ddl, rows=("SYN_1", "SYN_2")):
        p = self.dir / f"{random.random()}.sqlite"
        conn = sqlite3.connect(p)
        for stmt in ddl:
            conn.execute(stmt)
        for r in rows:
            conn.execute("INSERT INTO sessions (session_id) VALUES (?)", (r,))
        conn.commit()
        conn.close()
        return p

    TABLE = "CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id VARCHAR, current_qa_run_id INTEGER)"

    def check(self, path, ok):
        before = path.read_bytes()
        if ok:
            ref.verify_session_id_uniqueness(path)
        else:
            with self.assertRaises(ref.SessionUniquenessError):
                ref.verify_session_id_uniqueness(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-journal").exists())

    def test_expected_unique_index_passes(self):
        self.check(self.db([self.TABLE, "CREATE UNIQUE INDEX ix_sessions_session_id ON sessions (session_id)"]), True)

    def test_unique_constraint_passes(self):
        self.check(self.db(["CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id VARCHAR UNIQUE, "
                            "current_qa_run_id INTEGER)"]), True)

    def test_duplicate_rows_fail(self):
        self.check(self.db([self.TABLE, "CREATE INDEX ix_sessions_session_id ON sessions (session_id)"],
                           rows=("SYN_1", "SYN_1")), False)

    def test_no_unique_mechanism_fails(self):
        self.check(self.db([self.TABLE]), False)
        self.check(self.db([self.TABLE, "CREATE INDEX ix_sessions_session_id ON sessions (session_id)"]), False)

    def test_partial_fails(self):
        self.check(self.db([self.TABLE, "CREATE UNIQUE INDEX ux ON sessions (session_id) "
                                        "WHERE session_id IS NOT NULL"]), False)

    def test_composite_fails(self):
        self.check(self.db([self.TABLE, "CREATE UNIQUE INDEX ux ON sessions (session_id, current_qa_run_id)"]), False)

    def test_expression_index_fails(self):
        self.check(self.db([self.TABLE, "CREATE UNIQUE INDEX ux ON sessions (lower(session_id))"]), False)

    def test_unverifiable_fails_closed(self):
        with self.assertRaises(ref.SessionUniquenessError):
            ref.verify_session_id_uniqueness(self.dir / "absent.sqlite")
        p = self.dir / "notable.sqlite"
        sqlite3.connect(p).close()
        with self.assertRaises(ref.SessionUniquenessError):
            ref.verify_session_id_uniqueness(p)
        q = self.dir / "garbage.sqlite"
        q.write_bytes(b"not a database at all" * 10)
        with self.assertRaises(ref.SessionUniquenessError):
            ref.verify_session_id_uniqueness(q)
        self.assertEqual(q.read_bytes(), b"not a database at all" * 10)


# ---------------------------------------------------------------------------
# H. One-shot / pre-application boundary and synthetic end-to-end
# ---------------------------------------------------------------------------

FIXED_IDENTITY = {
    "python": {"implementation": "cpython", "version": "synthetic"},
    "installed_distributions": [["pyarrow", "17.0.0"]],
    "requirements_sha256": "0" * 64,
    "pyarrow_version": "17.0.0",
}


class TestH_OneShotBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.repo = make_repo(self.dir / "repo")
        self.db_ok = self._db(unique=True, dup=False)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self, *, unique, dup, counts=None):
        p = self.dir / f"rt{random.random()}.sqlite"
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE qa_runs (id INTEGER PRIMARY KEY, sync_grid_file_count INTEGER)")
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_id VARCHAR, current_qa_run_id INTEGER)")
        if unique:
            conn.execute("CREATE UNIQUE INDEX ix_sessions_session_id ON sessions (session_id)")
        for i, sid in enumerate(SYN_SESSIONS, start=1):
            conn.execute("INSERT INTO qa_runs VALUES (?, ?)", (i, (counts or {}).get(sid)))
            conn.execute("INSERT INTO sessions (session_id, current_qa_run_id) VALUES (?, ?)", (sid, i))
        if dup:
            conn.execute("INSERT INTO sessions (session_id) VALUES (?)", (SYN_SESSIONS[0],))
        conn.commit()
        conn.close()
        return p

    def resolver(self, sid):
        self.calls.append(sid)
        raise AssertionError("NEW36-equivalent input must not be touched before gates pass")

    def run_gen(self, **over):
        kw = dict(csv_path=self.dir / "out.csv", manifest_path=self.dir / "out.json",
                  zip_resolver=self.resolver, db_path=self.db_ok,
                  recorded_environment_identity=FIXED_IDENTITY,
                  recorded_environment_identity_sha256=ref.runtime_environment_identity_sha256(FIXED_IDENTITY),
                  session_ids=SYN_SESSIONS, repo_root=self.repo,
                  environment_identity_builder=lambda: json.loads(json.dumps(FIXED_IDENTITY)))
        kw.update(over)
        state = ref.ApplicationState()
        kw["state"] = state
        return kw, state

    def assert_blocked(self, exc_type, **over):
        kw, state = self.run_gen(**over)
        with self.assertRaises(exc_type):
            ref.generate_reference_artifacts(**kw)
        self.assertEqual(self.calls, [])
        self.assertFalse(state.application_invocation_started)
        self.assertFalse(Path(kw["csv_path"]).exists() and Path(kw["csv_path"]).stat().st_size > 0
                         and kw["csv_path"] == self.dir / "out.csv")

    def test_inventory_gate(self):
        self.assert_blocked(ref.InventoryError, session_ids=SYN_SESSIONS[:11])
        self.assert_blocked(ref.InventoryError, session_ids=SYN_SESSIONS[:11] + SYN_SESSIONS[:1])

    def test_nnc1_gate(self):
        (self.dir / "plain").mkdir()
        self.assert_blocked(ref.ProvenanceError, repo_root=self.dir / "plain")

    def test_nnc2_gate(self):
        self.repo.joinpath(*ref.REFERENCE_SOURCE_PATHS[1].split("/")).write_bytes(b"dirty\n")
        self.assert_blocked(ref.ProvenanceError)

    def test_nnc3_gate(self):
        other = json.loads(json.dumps(FIXED_IDENTITY))
        other["installed_distributions"].append(["numpy", "2.0"])
        self.assert_blocked(ref.EnvironmentIdentityError, environment_identity_builder=lambda: other)
        self.assert_blocked(ref.EnvironmentIdentityError, recorded_environment_identity=None)

        def no_pyarrow():
            raise ref.EnvironmentIdentityError("import pyarrow.parquet failed")

        self.assert_blocked(ref.EnvironmentIdentityError, environment_identity_builder=no_pyarrow)

    def test_nnc4_gate(self):
        self.assert_blocked(ref.SessionUniquenessError, db_path=self._db(unique=False, dup=True))
        self.assert_blocked(ref.SessionUniquenessError, db_path=self._db(unique=False, dup=False))
        self.assert_blocked(ref.SessionUniquenessError, db_path=None)

    def test_existing_output_gate(self):
        (self.dir / "out.json").write_bytes(b"{}")
        self.assert_blocked(ref.OutputPathError)

    # ---- synthetic end-to-end -------------------------------------------

    def _session_zips(self):
        paths = {}
        for k, sid in enumerate(SYN_SESSIONS):
            frames = []
            for j, asset in enumerate(ref.ASSETS):
                g = raw_grid(n=420, seed=100 + 2 * k + j)
                g.insert(0, "asset", asset)
                frames.append(g)
            both = pd.concat(frames, ignore_index=True)
            half = len(both) // 2
            parts = [both.iloc[:half], both.iloc[half:]]
            entries = [(f"sync_grid_100ms/part-{i:03d}.parquet",
                        b"PAR1" + p.to_csv(index=False).encode() + b"PAR1") for i, p in enumerate(parts)]
            paths[sid] = write_zip(self.dir / f"{sid}.zip", entries)
        return paths

    def test_end_to_end_synthetic(self):
        zips = self._session_zips()
        db = self._db(unique=True, dup=False, counts={sid: 2 for sid in SYN_SESSIONS})
        kw, state = self.run_gen(db_path=db, zip_resolver=lambda sid: zips[sid],
                                 parquet_reader=lambda b: pd.read_csv(io.BytesIO(b[4:-4])),
                                 metadata_checker=fake_md)
        result = ref.generate_reference_artifacts(**kw)
        self.assertTrue(state.application_invocation_started)
        self.assertEqual(result.row_counts, {"HZ": 1728, "HG": 432, "HC": 432, "HB": 864})
        csv_bytes = Path(result.csv_path).read_bytes()
        man_bytes = Path(result.manifest_path).read_bytes()
        self.assertEqual(result.csv_sha256, sha(csv_bytes))
        obj = ref.validate_reference_manifest_bytes(man_bytes, session_ids=SYN_SESSIONS, csv_bytes=csv_bytes)
        self.assertEqual(obj["runtime_source_commit"], git(self.repo, "rev-parse", "HEAD").stdout.decode().strip())
        self.assertEqual(obj["actual_total_row_count"], 3456)
        self.assertEqual(obj["session_ids"], list(SYN_SESSIONS))
        text = csv_bytes.decode("utf-8")
        lines = text.split("\n")
        self.assertEqual(len(lines), 3456 + 2)
        self.assertEqual(lines[1].split(",")[:6],
                         ["V1_2_NEW36_VALIDATION_PROTOCOL_V2", SYN_SESSIONS[0], "BTC", "HZ", "bitget_ofi", "T0"])
        for bad in ("nan", "inf"):
            self.assertNotIn("," + bad + ",", text)
        # One-shot: artifacts are never overwritten.
        kw2, _ = self.run_gen(db_path=db, zip_resolver=lambda sid: zips[sid])
        kw2["csv_path"], kw2["manifest_path"] = result.csv_path, result.manifest_path
        with self.assertRaises(ref.OutputPathError):
            ref.generate_reference_artifacts(**kw2)

    def test_structural_failure_after_start_writes_nothing(self):
        zips = self._session_zips()
        del zips[SYN_SESSIONS[5]]

        def resolver(sid):
            if sid not in zips:
                raise FileNotFoundError(sid)
            return zips[sid]

        kw, state = self.run_gen(zip_resolver=resolver,
                                 parquet_reader=lambda b: pd.read_csv(io.BytesIO(b[4:-4])),
                                 metadata_checker=fake_md)
        with self.assertRaises(ref.StructuralApplicationFailure) as cm:
            ref.generate_reference_artifacts(**kw)
        self.assertEqual(cm.exception.classification, "PENDING")
        self.assertTrue(state.application_invocation_started)
        self.assertFalse(Path(kw["csv_path"]).exists())
        self.assertFalse(Path(kw["manifest_path"]).exists())


class TestEngineAllowlist(unittest.TestCase):
    def test_engine_symbols_subset_of_a001_allowlist(self):
        allow = {"GRID_MS", "FEATURE_MAP", "SIMPLE_FEATURES", "QUANTILES", "HORIZONS_SIMPLE_MS",
                 "HORIZONS_COMPOSITE_MS", "build_canonical_grid", "_quality_admissible_mask",
                 "_rank_signed_uniform", "_quantile_type7", "_build_forward_return_array",
                 "_greedy_overlap_filter"}
        self.assertTrue(set(ref.ENGINE_SYMBOLS_USED) <= allow)
        self.assertEqual(ref.A001_ENGINE_ALLOWLIST, frozenset(allow))

    def test_engine_identity_pinned(self):
        self.assertEqual(sha(ref.ENGINE_PATH.read_bytes()), ref.ENGINE_SHA256)


if __name__ == "__main__":
    unittest.main()
