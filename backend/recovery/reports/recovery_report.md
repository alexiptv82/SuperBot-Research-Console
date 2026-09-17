# FrozenAnalysisEngine — Phase 2 Recovery Report

Generated: 2026-09-17T06:01:00.427889+00:00

## FrozenAnalysisEngine

- Status: **NOT_CONFIGURED**  (unchanged; recovery is preparatory only)
- Directional alpha: **REJECTED** (unchanged)
- Live trading: **NOT AUTHORIZED** (unchanged)

## NEW36 firewall

- The recovery sandbox refuses to open any NEW36 raw ZIP.
- Enforced by `recovery.allowlist.assert_recovery_allowed`.
- Coverage: `tests/test_recovery_sandbox_firewall.py`.

## OLD36 raw-reference availability

- Sessions present: **0 / 11**
- Nominal hours present: **0 / 36.0 h**
- Raw-ready for reproduction: **NO**

### Missing OLD36 raw sessions

- `20260905T073818Z_e44d99bd`
- `20260906T054317Z_7973d176`
- `20260906T092548Z_d18fd1e3`
- `20260906T221530Z_db18dc51`
- `20260907T070729Z_48d293bf`
- `20260907T124300Z_6ac966eb`
- `20260907T160215Z_ee5b0782`
- `20260907T221751Z_3abdfd02`
- `20260908T074322Z_1057297f`
- `20260908T120838Z_3cfee030`
- `20260909T171030Z_1952d031`

## Golden regression summary

- Total golden rows across CP24/CP36 CSVs: **11449**
- Matched: **0**
- Failed:  **0**
- Pending raw OLD36: **11449**

See `golden_regression_summary.csv` and `golden_regression_failures.csv`.

## Rule status

- EXPLICIT_EVIDENCE: 12
- MATH_UNIQUE: 3
- INFERRED_PENDING: 3
- AMBIGUOUS: 4
- MISSING: 7

See `recovery_rules.json` and `recovery_provenance.json`.

## Next step

Upload the 11 OLD36_REFERENCE raw session ZIPs via the normal chunked uploader. Each session_id will be auto-tagged `OLD36_REFERENCE`; content-addressed retention deduplicates identical uploads; milestone totals are untouched.
