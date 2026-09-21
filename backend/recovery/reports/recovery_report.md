# FrozenAnalysisEngine — Phase 2 Recovery Report

Generated: 2026-09-21T06:43:29.119908+00:00

## FrozenAnalysisEngine

- Status: **NOT_CONFIGURED**  (unchanged; recovery is preparatory only)
- Directional alpha: **REJECTED** (unchanged)
- Live trading: **NOT AUTHORIZED** (unchanged)

## NEW36 firewall

- The recovery sandbox refuses to open any NEW36 raw ZIP.
- Enforced by `recovery.allowlist.assert_recovery_allowed`.
- Coverage: `tests/test_recovery_sandbox_firewall.py`.

## OLD36 raw-reference availability

- Sessions present: **11 / 11**
- Nominal hours present: **36.0 / 36.0 h**
- Raw-ready for reproduction: **YES**

### Missing OLD36 raw sessions

_none_

## Golden regression summary

- Total golden rows across CP24/CP36 CSVs: **11449**
- Total quantitative comparison instances: **11471** (wide multi-scope files expand 1 row into multiple instances)
- METADATA_ONLY files excluded from instances (session-audit collector/QA diagnostics): **3**
- Matched: **192**
- Failed:  **11279**
- Pending raw OLD36: **0**

See `golden_regression_summary.csv` and `golden_regression_failures.csv`.

## Rule status

- EXPLICIT_EVIDENCE: 12
- MATH_UNIQUE: 3
- INFERRED_PENDING: 3
- AMBIGUOUS: 4
- MISSING: 7

See `recovery_rules.json` and `recovery_provenance.json`.

## Next step

All 11 OLD36 raw sessions are imported. Wire the `reproduce_row` hook and re-run the harness.
