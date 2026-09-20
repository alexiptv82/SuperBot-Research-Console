"""Generate RECONSTRUCTION_V1.1_RECOVERED_METADATA.json — ONE TIME,
FORENSIC, GOLDEN-vs-GOLDEN ONLY.

This script is intentionally standalone and is NEVER imported by the
runtime harness. It:

  1. Recovers the exact session_id membership of every historical
     aggregate scope (ALL36, OLD24/ALL24, NEW12, WEEKEND12, WEEKDAY12)
     from PRIMARY EXPLICIT evidence already present in the golden
     artifacts (session_audit_*.csv session_id/period columns,
     cross-checked against block-level golden CSVs' own session/
     period columns and against the frozen
     ``checkpoint_registry.OLD36_REFERENCE_SESSIONS`` registry).

  2. Recovers the historical (session_id, asset) identity behind every
     min_block/max_block numeric value in every aggregate-schema
     golden CSV, by matching that value against the corresponding
     GOLDEN block-level CSV rows (same scope, same feature/state, same
     horizon_ms, same q, N > 0) using EXACT float equality
     (tolerance = 0) after CSV parsing — no candidate reconstruction
     output is read, computed, or referenced anywhere in this script.

SAFETY INVARIANTS:
  - Never opens NEW36 data.
  - Never imports/calls recovery.engine.reconstruct_block or any
    candidate-reconstruction code path.
  - Never modifies any golden CSV, the frozen methodology spec, or the
    frozen validation spec.
  - If any cross-check fails, or if exact-tolerance matching does not
    recover 100% of identities, the script raises loudly rather than
    silently degrading (mirrors METADATA_SCOPE_INCONSISTENCY /
    METADATA_IDENTITY_RECOVERY_MISMATCH / METADATA_MATCH_TOLERANCE_
    REQUIRED policy from the task instructions).

Run manually (never at runtime):
    cd backend && python3 -m recovery.tools.generate_recovered_metadata
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

GOLDENS_DIR = BACKEND_DIR / "recovery" / "goldens"
SPECS_DIR = BACKEND_DIR / "recovery" / "specs"
OUTPUT_PATH = SPECS_DIR / "RECONSTRUCTION_V1.1_RECOVERED_METADATA.json"

METHODOLOGY_SPEC = SPECS_DIR / "RECONSTRUCTION_V1.1_SPEC.txt"
VALIDATION_SPEC = SPECS_DIR / "RECONSTRUCTION_V1.1_VALIDATION_SPEC.txt"
METHODOLOGY_SHA256_EXPECTED = (
    "02ecaf121ed9ef5f90f30b9db203889ecda114f8547d83244f5f0f7c27453875"
)
VALIDATION_SHA256_EXPECTED = (
    "30aea96229dbd96f1c98419ecd76105db171d79dc16bba29338b9650a9558d29"
)


class MetadataScopeInconsistency(Exception):
    pass


class MetadataIdentityRecoveryMismatch(Exception):
    pass


class MetadataMatchToleranceRequired(Exception):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# PART A: scope membership recovery
# ---------------------------------------------------------------------------

def recover_scopes() -> dict:
    from checkpoint_registry import OLD36_REFERENCE_SESSIONS

    audit_24h = pd.read_csv(GOLDENS_DIR / "CP24" / "session_audit_24h.csv")
    audit_new12 = pd.read_csv(GOLDENS_DIR / "CP36" / "session_audit_new12.csv")
    audit_36h = pd.read_csv(GOLDENS_DIR / "CP36" / "session_audit_36h.csv")

    simple_24h = pd.read_csv(GOLDENS_DIR / "CP24" / "simple_q80_q90_q95_block_level_24h.csv")
    simple_new12 = pd.read_csv(GOLDENS_DIR / "CP36" / "simple_block_level_new12.csv")

    all36 = tuple(sorted(OLD36_REFERENCE_SESSIONS))
    if set(audit_36h["session_id"]) != set(all36):
        raise MetadataScopeInconsistency(
            "ALL36 mismatch: checkpoint_registry.OLD36_REFERENCE_SESSIONS != "
            "CP36/session_audit_36h.csv session_id column"
        )

    old24 = tuple(sorted(audit_24h["session_id"]))
    if set(simple_24h["session"]) != set(old24):
        raise MetadataScopeInconsistency(
            "OLD24 mismatch: CP24/session_audit_24h.csv vs "
            "CP24/simple_q80_q90_q95_block_level_24h.csv session column"
        )
    if len(old24) != 7:
        raise MetadataScopeInconsistency(f"OLD24 expected 7 sessions, found {len(old24)}")

    new12 = tuple(sorted(audit_new12["session_id"]))
    if set(simple_new12["session"]) != set(new12):
        raise MetadataScopeInconsistency(
            "NEW12 mismatch: CP36/session_audit_new12.csv vs "
            "CP36/simple_block_level_new12.csv session column"
        )
    if len(new12) != 4:
        raise MetadataScopeInconsistency(f"NEW12 expected 4 sessions, found {len(new12)}")
    if set(all36) - set(old24) != set(new12):
        raise MetadataScopeInconsistency("ALL36 - OLD24 != NEW12")

    we_rows = audit_24h[audit_24h["period"] == "WE"]
    wd_rows = audit_24h[audit_24h["period"] == "WD"]
    weekend12 = tuple(sorted(we_rows["session_id"]))
    weekday12 = tuple(sorted(wd_rows["session_id"]))
    if len(weekend12) != 3:
        raise MetadataScopeInconsistency(f"WEEKEND12 expected 3 sessions, found {len(weekend12)}")
    if len(weekday12) != 4:
        raise MetadataScopeInconsistency(f"WEEKDAY12 expected 4 sessions, found {len(weekday12)}")
    if set(weekend12) | set(weekday12) != set(old24):
        raise MetadataScopeInconsistency("WEEKEND12 union WEEKDAY12 != OLD24")
    if set(weekend12) & set(weekday12):
        raise MetadataScopeInconsistency("WEEKEND12 intersects WEEKDAY12")

    block_period = pd.concat([simple_24h[["session", "period"]]]).drop_duplicates()
    block_we = set(block_period[block_period["period"] == "WE"]["session"])
    block_wd = set(block_period[block_period["period"] == "WD"]["session"])
    if block_we != set(weekend12) or block_wd != set(weekday12):
        raise MetadataScopeInconsistency(
            "WE/WD block-level period column disagrees with session_audit_24h.csv"
        )

    scopes = {
        "ALL36": {
            "session_ids": list(all36),
            "count": len(all36),
            "evidence_source": [
                "checkpoint_registry.OLD36_REFERENCE_SESSIONS",
                "CP36/session_audit_36h.csv#session_id",
            ],
            "recovery_status": "EXPLICIT",
        },
        "OLD24": {
            "aliases": ["ALL24", "old24", "24h"],
            "session_ids": list(old24),
            "count": len(old24),
            "evidence_source": [
                "CP24/session_audit_24h.csv#session_id",
                "CP24/simple_q80_q90_q95_block_level_24h.csv#session",
            ],
            "recovery_status": "EXPLICIT",
        },
        "NEW12": {
            "aliases": ["new12"],
            "session_ids": list(new12),
            "count": len(new12),
            "evidence_source": [
                "CP36/session_audit_new12.csv#session_id",
                "CP36/simple_block_level_new12.csv#session",
                "MATH_UNIQUE cross-check: ALL36 - OLD24 == NEW12",
            ],
            "recovery_status": "EXPLICIT",
        },
        "WEEKEND12": {
            "session_ids": list(weekend12),
            "count": len(weekend12),
            "evidence_source": [
                'CP24/session_audit_24h.csv#period=="WE"',
                'CP24/simple_q80_q90_q95_block_level_24h.csv#period=="WE"',
            ],
            "recovery_status": "EXPLICIT",
        },
        "WEEKDAY12": {
            "session_ids": list(weekday12),
            "count": len(weekday12),
            "evidence_source": [
                'CP24/session_audit_24h.csv#period=="WD"',
                'CP24/simple_q80_q90_q95_block_level_24h.csv#period=="WD"',
            ],
            "recovery_status": "EXPLICIT",
        },
    }

    cross_checks = {
        "all36_matches_registry": True,
        "all36_equals_old24_union_new12": True,
        "old24_intersection_new12_empty": True,
        "weekend12_union_weekday12_equals_old24": True,
        "weekend12_intersection_weekday12_empty": True,
    }

    return scopes, cross_checks


# ---------------------------------------------------------------------------
# PART B/C: block-identity recovery (golden-vs-golden, exact match only)
# ---------------------------------------------------------------------------

NARROW_AGGREGATE_FILES = [
    ("CP24/simple_aggregates_24h.csv", "simple", "period", None, None, "feature"),
    ("CP24/composite_aggregates_24h.csv", "composite", "period", None, None, "feature"),
    ("CP24/fair_gap_dispersion_aggregates_24h.csv", "dispersion", "period", None, None, "state"),
    ("CP36/simple_horizon_profile_all36_q90.csv", "simple", "filename", "ALL36", 0.90, "feature"),
    ("CP36/composite_horizon_profile_all36_q90.csv", "composite", "filename", "ALL36", 0.90, "feature"),
    ("CP36/simple_horizon_profile_new12_q90.csv", "simple", "filename", "NEW12", 0.90, "feature"),
    ("CP36/composite_horizon_profile_new12_q90.csv", "composite", "filename", "NEW12", 0.90, "feature"),
    ("CP36/simple_sensitivity_all36_30s.csv", "simple", "filename", "ALL36", None, "feature"),
    ("CP36/composite_sensitivity_all36_30s.csv", "composite", "filename", "ALL36", None, "feature"),
    ("CP36/simple_sensitivity_new12_30s.csv", "simple", "filename", "NEW12", None, "feature"),
    ("CP36/composite_sensitivity_new12_30s.csv", "composite", "filename", "NEW12", None, "feature"),
    ("CP36/fair_gap_dispersion_all36.csv", "dispersion", "filename", "ALL36", None, "state"),
    ("CP36/fair_gap_dispersion_new12.csv", "dispersion", "filename", "NEW12", None, "state"),
]
# horizon default for sensitivity_*_30s files is 30000ms
_SENSITIVITY_FILES = {
    "CP36/simple_sensitivity_all36_30s.csv", "CP36/composite_sensitivity_all36_30s.csv",
    "CP36/simple_sensitivity_new12_30s.csv", "CP36/composite_sensitivity_new12_30s.csv",
}

WIDE_FILE = "CP24/weekend_vs_weekday_selected_30s.csv"
WIDE_FILE_SCOPES = ("WEEKEND12", "WEEKDAY12", "ALL24")


def _load_block_pools():
    simple_24h = pd.read_csv(GOLDENS_DIR / "CP24" / "simple_q80_q90_q95_block_level_24h.csv")
    composite_24h = pd.read_csv(GOLDENS_DIR / "CP24" / "composite_q80_q90_q95_block_level_24h.csv")
    simple_new12 = pd.read_csv(GOLDENS_DIR / "CP36" / "simple_block_level_new12.csv")
    composite_new12 = pd.read_csv(GOLDENS_DIR / "CP36" / "composite_block_level_new12.csv")
    disp_24h = pd.read_csv(GOLDENS_DIR / "CP24" / "fair_gap_dispersion_block_level_24h.csv")
    disp_new12 = pd.read_csv(GOLDENS_DIR / "CP36" / "fair_gap_dispersion_block_level_new12.csv")
    return {
        "simple": pd.concat([simple_24h, simple_new12], ignore_index=True),
        "composite": pd.concat([composite_24h, composite_new12], ignore_index=True),
        "dispersion": pd.concat([disp_24h, disp_new12], ignore_index=True),
    }


def recover_block_identities(scopes: dict) -> tuple[dict, dict]:
    scope_sessions = {name: set(v["session_ids"]) for name, v in scopes.items()}
    # ALL24 alias -> OLD24
    scope_sessions["ALL24"] = scope_sessions["OLD24"]

    pools = _load_block_pools()

    block_identities: dict[str, list] = {}
    total = 0
    min_unique = max_unique = 0
    min_tie = max_tie = 0
    min_unrec = max_unrec = 0

    def resolve_identity(pool_pos: pd.DataFrame, target_value: float):
        exact = pool_pos[pool_pos["mean_signed_bps"] == target_value]
        return exact

    for rel, kind, scope_from, scope_name_fixed, q_default, feature_col in NARROW_AGGREGATE_FILES:
        df = pd.read_csv(GOLDENS_DIR / rel)
        horizon_default = 30000 if rel in _SENSITIVITY_FILES else None
        entries = []
        for row_index, row in df.iterrows():
            scope_name = row["period"] if scope_from == "period" else scope_name_fixed
            sessions = scope_sessions[scope_name]
            pool = pools[kind]
            pool = pool[pool["session"].isin(sessions) & (pool[feature_col] == row[feature_col])]
            h = row["horizon_ms"] if ("horizon_ms" in row and pd.notna(row.get("horizon_ms"))) else horizon_default
            if h is not None:
                pool = pool[pool["horizon_ms"] == h]
            q = row["q"] if ("q" in row and pd.notna(row.get("q"))) else q_default
            if q is not None and "q" in pool.columns:
                pool = pool[pool["q"] == q]
            pool_pos = pool[pool["N"] > 0]

            total += 1
            entry = {
                "row_index": int(row_index),
                "scope": scope_name,
                feature_col: row[feature_col],
                "horizon_ms": int(h) if h is not None else None,
                "q": float(q) if q is not None else None,
            }
            for col, unique_key, tie_key, unrec_key in [
                ("min_block", "min", "min", "min"),
                ("max_block", "max", "max", "max"),
            ]:
                target = row[col]
                matches = resolve_identity(pool_pos, target)
                if len(matches) == 1:
                    m = matches.iloc[0]
                    entry[col] = {"session_id": m["session"], "asset": m["asset"]}
                    if col == "min_block":
                        min_unique += 1
                    else:
                        max_unique += 1
                elif len(matches) > 1:
                    tied = matches.sort_values(["session", "asset"]).iloc[0]
                    entry[col] = {"session_id": tied["session"], "asset": tied["asset"]}
                    entry[f"{col}_tie_broken"] = True
                    if col == "min_block":
                        min_tie += 1
                    else:
                        max_tie += 1
                else:
                    entry[col] = None
                    if col == "min_block":
                        min_unrec += 1
                    else:
                        max_unrec += 1
            entries.append(entry)
        block_identities[rel] = entries

    # Wide multi-scope file: weekend_vs_weekday_selected_30s.csv
    wide_df = pd.read_csv(GOLDENS_DIR / WIDE_FILE)
    wide_entries = []
    for row_index, row in wide_df.iterrows():
        kind = row["type"]
        pool_base = pools[kind]
        pool_base = pool_base[
            (pool_base["feature"] == row["feature"]) & (pool_base["horizon_ms"] == 30000)
        ]
        if "q" in pool_base.columns:
            pool_base = pool_base[pool_base["q"] == 0.90]
        for scope_name in WIDE_FILE_SCOPES:
            sessions = scope_sessions[scope_name]
            pool = pool_base[pool_base["session"].isin(sessions)]
            pool_pos = pool[pool["N"] > 0]
            total += 1
            entry = {
                "row_index": int(row_index),
                "scope": scope_name,
                "feature": row["feature"],
                "type": kind,
                "horizon_ms": 30000,
                "q": 0.90,
            }
            for col in ("min_block", "max_block"):
                target = row[f"{scope_name}_{col}"]
                matches = resolve_identity(pool_pos, target)
                if len(matches) == 1:
                    m = matches.iloc[0]
                    entry[col] = {"session_id": m["session"], "asset": m["asset"]}
                    if col == "min_block": min_unique += 1
                    else: max_unique += 1
                elif len(matches) > 1:
                    tied = matches.sort_values(["session", "asset"]).iloc[0]
                    entry[col] = {"session_id": tied["session"], "asset": tied["asset"]}
                    entry[f"{col}_tie_broken"] = True
                    if col == "min_block": min_tie += 1
                    else: max_tie += 1
                else:
                    entry[col] = None
                    if col == "min_block": min_unrec += 1
                    else: max_unrec += 1
            wide_entries.append(entry)
    block_identities[WIDE_FILE] = wide_entries

    summary = {
        "total_rows": total,
        "min_unique": min_unique,
        "max_unique": max_unique,
        "min_tie_resolved": min_tie,
        "max_tie_resolved": max_tie,
        "min_ambiguous": 0,
        "max_ambiguous": 0,
        "min_unrecoverable": min_unrec,
        "max_unrecoverable": max_unrec,
        "matching_method": "exact_float_equality_after_csv_parse",
        "matching_tolerance": 0,
    }

    if min_unrec or max_unrec:
        raise MetadataIdentityRecoveryMismatch(
            f"Exact matching left {min_unrec} min / {max_unrec} max identities "
            f"unrecoverable — refusing to silently widen tolerance."
        )
    if total != 1604:
        raise MetadataIdentityRecoveryMismatch(
            f"Expected 1604 aggregate identity-requiring rows, found {total}."
        )
    if min_unique != 1604 or max_unique != 1604:
        raise MetadataIdentityRecoveryMismatch(
            f"Expected 1604/1604 unique min and max identities, found "
            f"min={min_unique} max={max_unique}."
        )

    return block_identities, summary


AGGREGATE_SCOPE_FILE_ROUTING = {
    # narrow files with an explicit `period` column
    "CP24/simple_aggregates_24h.csv": {"scope_source": "period_column", "kind": "simple"},
    "CP24/composite_aggregates_24h.csv": {"scope_source": "period_column", "kind": "composite"},
    "CP24/fair_gap_dispersion_aggregates_24h.csv": {"scope_source": "period_column", "kind": "dispersion"},
    # narrow files, scope implied by filename
    "CP36/simple_horizon_profile_all36_q90.csv": {"scope_source": "filename", "scope": "ALL36", "kind": "simple"},
    "CP36/composite_horizon_profile_all36_q90.csv": {"scope_source": "filename", "scope": "ALL36", "kind": "composite"},
    "CP36/simple_horizon_profile_new12_q90.csv": {"scope_source": "filename", "scope": "NEW12", "kind": "simple"},
    "CP36/composite_horizon_profile_new12_q90.csv": {"scope_source": "filename", "scope": "NEW12", "kind": "composite"},
    "CP36/simple_sensitivity_all36_30s.csv": {"scope_source": "filename", "scope": "ALL36", "kind": "simple"},
    "CP36/composite_sensitivity_all36_30s.csv": {"scope_source": "filename", "scope": "ALL36", "kind": "composite"},
    "CP36/simple_sensitivity_new12_30s.csv": {"scope_source": "filename", "scope": "NEW12", "kind": "simple"},
    "CP36/composite_sensitivity_new12_30s.csv": {"scope_source": "filename", "scope": "NEW12", "kind": "composite"},
    "CP36/fair_gap_dispersion_all36.csv": {"scope_source": "filename", "scope": "ALL36", "kind": "dispersion"},
    "CP36/fair_gap_dispersion_new12.csv": {"scope_source": "filename", "scope": "NEW12", "kind": "dispersion"},
    # wide multi-scope files
    "CP24/weekend_vs_weekday_selected_30s.csv": {
        "scope_source": "wide_columns",
        "scopes": ["WEEKEND12", "WEEKDAY12", "ALL24"],
        "kind": "mixed",
    },
    "CP36/selected_q90_30s_comparison_24h_new12_all36.csv": {
        "scope_source": "wide_columns_prefixed",
        "scope_prefix_map": {"old24": "OLD24", "new12": "NEW12", "all36": "ALL36"},
        "kind": "mixed",
        "note": "no min_block/max_block columns present in this file",
    },
}


def build() -> dict:
    methodology_sha = _sha256(METHODOLOGY_SPEC)
    validation_sha = _sha256(VALIDATION_SPEC)
    if methodology_sha != METHODOLOGY_SHA256_EXPECTED:
        raise MetadataScopeInconsistency("Frozen methodology spec hash changed — refusing to proceed.")
    if validation_sha != VALIDATION_SHA256_EXPECTED:
        raise MetadataScopeInconsistency("Frozen validation spec hash changed — refusing to proceed.")

    scopes, cross_checks = recover_scopes()
    block_identities, id_summary = recover_block_identities(scopes)

    doc = {
        "schema_version": 1,
        "generated_by": "backend/recovery/tools/generate_recovered_metadata.py",
        "generation_note": (
            "Recovered exclusively from existing golden CP24/CP36 artifacts "
            "(session_audit_*.csv, block-level golden CSVs) via golden-vs-"
            "golden exact numeric matching. No RECONSTRUCTION_V1.1 candidate "
            "output was computed or used. No NEW36 data was opened."
        ),
        "methodology_sha256": methodology_sha,
        "validation_sha256": validation_sha,
        "scopes": scopes,
        "cross_checks": cross_checks,
        "aggregate_scope_file_routing": AGGREGATE_SCOPE_FILE_ROUTING,
        "block_identities": block_identities,
        "identity_recovery_summary": id_summary,
    }
    return doc


def main() -> None:
    doc = build()
    OUTPUT_PATH.write_text(json.dumps(doc, indent=2, sort_keys=False), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")
    print("identity_recovery_summary:", json.dumps(doc["identity_recovery_summary"], indent=2))
    print("sha256:", _sha256(OUTPUT_PATH))


if __name__ == "__main__":
    main()
