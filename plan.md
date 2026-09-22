# SUPERBOT V1.2 — Stage3 Implementation Plan (Implementation + Focused Tests Only)

## 1) Objectives
- Implement **Stage3 candidate-only diagnostics generator** per frozen spec: **HZ_FINAL, HG_FINAL, HC_FINAL, HB_FINAL** (no cross-product) totaling **576 rows**.
- Enforce **fail-closed invariants I1–I21** before any artifact write.
- Ensure **golden isolation** (runtime module contains no golden reads/imports/paths).
- Add **focused tests only** and run **only** `pytest backend/tests/test_v1_2_stage3.py`.
- Maintain Stage2 lineage integrity: no changes to Stage1/Stage2/engine/FrozenAnalysisEngine, no real Stage3 run, no OLD36 RAW, no NEW36 quantitative access.

## 2) Implementation Steps

### Phase 1 — Core POC (Isolation)
User stories:
1. As a developer, I want Stage3 row generation to work on **synthetic contexts only**, so no real data is touched.
2. As a developer, I want deterministic **576-row assembly** by axis, so I can validate counts and keys.
3. As a developer, I want invariants to fail-closed before any write, so no partial artifacts are produced.
4. As a developer, I want fingerprinting to exactly match Stage1/Stage2 encoding, so outputs are comparable later.
5. As an auditor, I want golden isolation checks, so runtime cannot accidentally read goldens.

Steps:
- **Precheck (read-only)**
  - Record `git rev-parse HEAD` and `git status --short`; compare to expected `f89049e...` and report drift.
  - Verify SHA256 unchanged for:
    - `backend/recovery/v1_2_stage2.py` = `1612824c...`
    - `backend/tests/test_v1_2_stage2.py` = `24877fe...`
  - If unexpected drift in frozen sources: **STOP** with `STAGE3_IMPLEMENTATION_FAIL_PRECHECK`.
- Create **only**:
  - `backend/recovery/v1_2_stage3.py`
  - `backend/tests/test_v1_2_stage3.py`
- Build the Stage3 **core library** inside `v1_2_stage3.py`:
  - Constants: diagnostic version, axes, variants, quantiles, horizons.
  - Feature-set drift guard: assert exact **12 HZ features** match engine semantics at import/runtime precheck.
  - Fingerprint helper: Stage1/Stage2 int64-LE concat → SHA256.
  - Type-7 quantile threshold helper (abs(signal), finite-only; TZ excludes zeros).
  - Row model + fixed CSV schema serializer (Python `csv` module, `\n` lineterminator, `repr(float)`, None→empty).
- Implement **axis generators** that operate on injected synthetic ctx/data (no RAW readers):
  - **HZ**: T0/TZ + classification + threshold fields; expected rows = 288.
  - **HG**: depthL1_extOFI G0 drift guard, G1 gate logic with depth_imbalance_l1 threshold; expected rows = 72.
  - **HC**: gap_depth_extOFI C0 drift guard, C1 raw-sign conjunction; expected rows = 72.
  - **HB**: 6 families × horizons × sessions × assets × (B0/B1) with exact-spacing pair counting; expected rows = 144.
- Implement **invariant suite I1–I21** (pure functions) run on the full row list **before any write**.
- Implement writer API (production path fixed):
  - `run_stage3_diagnostics()` writes to `backend/recovery/reports/v1_2/` with FileExistsError guards.
  - Must not accept output_dir param.
  - Must refuse protected names.
  - Write CSV bytes, compute sha/size, then write manifest deterministic JSON.
  - Note: tests will validate writer behavior but will not execute real Stage3 on OLD36.

### Phase 2 — V1 App Development (Core module + tests)
User stories:
1. As a developer, I want a single entrypoint to generate the Stage3 artifact deterministically (without running it in tests).
2. As a developer, I want strict schema enforcement, so every axis writes the same column order.
3. As a developer, I want matrix membership checks, so no accidental cartesian products occur.
4. As a developer, I want drift guards (I17) to detect engine semantic changes.
5. As an auditor, I want the manifest to include source SHA256s and safety flags.

Steps:
- Flesh out implementations for each axis to match the frozen spec exactly:
  - Ensure correct null/empty field semantics per axis.
  - Ensure HB exact_spacing_pair_n computed using set-based O(N).
  - Ensure drift guard compares required metrics with tolerance `<=1e-12`.
  - Ensure G1 uses HZ-T0 depth_imbalance_l1 threshold (I18) and subset constraints (I19).
  - Ensure C1 subset of C0 threshold-crossing set (I20).
- Implement manifest schema exactly:
  - Deterministic keys, `sort_keys=True`, no timestamp.
  - Safety fields: `new36_opened=false`, `golden_artifacts_read_by_generator=false`, `v1_1_stage1_stage2_modified=false`.
  - Include `source_sha256` for both new files.
- Implement static **golden isolation guard** (I21): source scan for forbidden tokens/paths/imports.

### Phase 3 — Focused Testing & Validation
User stories:
1. As a tester, I want unit tests that prove HZ T0/TZ handling of zeros/NaNs and classification.
2. As a tester, I want tests proving HG/HC alternative variants use RAW sign logic where specified.
3. As a tester, I want HB boundary strictness and exact_spacing_pair_n correctness (non-adjacent pairs).
4. As a tester, I want writer tests ensuring deterministic CSV bytes (newline, None formatting, column order).
5. As an auditor, I want tests ensuring golden isolation and no real Stage3 execution.

Tests to implement in `test_v1_2_stage3.py` (synthetic-only):
- HZ: 12-feature set guard; finite/nonzero/zero counts; zero_fraction; all 3 classes incl tied T0==TZ with zero_n>0; type7 threshold behavior; 288 rows.
- HG: G0 drift guard (synthetic metrics); G1 threshold equals HZ-T0 depth_imbalance_l1; raw sign direction; external_ofi finite/nonzero/sign match; gate_zero_n; subset invariant; 72 rows.
- HC: C0 drift guard; C1 raw di_l1 sign + raw external_ofi sign; explicit synthetic example where z-sign differs from raw; subset invariant; 72 rows.
- HB: boundary >= vs >; exact_spacing_pair_n correctness for [0,5,10] spacing=10 → 1; exact_spacing_pair_n==0 implies identical B0/B1 outputs; B1.accepted_n<=B0.accepted_n; 144 rows.
- Fingerprint: int64-LE encoding; order independence; empty hash.
- I17/I21: drift guard failure cases (missing ctx.metrics/key); static scan forbids golden references.
- Writer: fixed path/no output_dir; no overwrite; schema order; `\n` newline; None empty field; deterministic hash/size (for a synthetic mini-run).

Execution:
- Run only: `pytest backend/tests/test_v1_2_stage3.py`

### Phase 4 — Post-Implementation Scope Verification
User stories:
1. As a maintainer, I want proof only the authorized files changed.
2. As an auditor, I want Stage2 frozen hashes unchanged.
3. As a reviewer, I want a clean summary report for code audit readiness.
4. As a maintainer, I want confirmation no real Stage3 was executed.
5. As an auditor, I want confirmation no OLD36 RAW/NEW36/goldens were accessed.

Steps:
- Verify only the two new files were created/changed.
- Report any platform metadata drift separately (e.g., `.emergent`).
- Do not commit; if auto-commit occurs, report HEAD.

## 3) Next Actions
1. Run precheck commands (HEAD/status) and Stage2 SHA256 verification; decide pass/fail.
2. Create `backend/recovery/v1_2_stage3.py` skeleton with: constants, schema writer, fingerprint, threshold/type7, invariant framework.
3. Implement HZ axis generator first + unit tests for classification/threshold logic.
4. Implement HB axis generator + tests for boundary and exact_spacing_pair_n.
5. Implement HG/HC variants + drift/subset invariants + tests.
6. Implement writer + manifest + golden-isolation static guard + tests.
7. Run `pytest backend/tests/test_v1_2_stage3.py` and fix until green.

## 4) Success Criteria
- Precheck passes (or drift is only untracked artifacts/metadata) and Stage2 SHA256s match expected.
- Only two files created: `v1_2_stage3.py`, `test_v1_2_stage3.py` (no other source modifications).
- Stage3 generator produces exactly: HZ=288, HG=72, HC=72, HB=144, TOTAL=576, with fixed schema and null semantics.
- All invariants I1–I21 implemented and covered by focused tests; fail-closed before any write.
- Golden isolation: static scan passes; runtime has no golden read/import/path.
- Tests: `pytest backend/tests/test_v1_2_stage3.py` passes; no other test suites run.
- No real Stage3 execution; no OLD36 RAW; no NEW36 quantitative open; no golden artifacts read.