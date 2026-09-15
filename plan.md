# SuperBot Research Console V1 — plan.md

## 1) Objectives
- Deliver **SuperBot Research Console V1**: deterministic QA web app for **MultiVenue 3H session ZIPs** (upload → QA → registry → checkpoints).
- Enforce frozen constraints: **collector SHA256 = `e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3`**, no tuning, no LLM, no trading.
- Provide **dedup + immutable QA runs + validated-hours tracking** toward OLD36 / NEW12 / TOTAL48 / NEW36 / TOTAL72.
- Implement **tolerant, adaptable manifest parsing**: missing critical runtime fields ⇒ **UNRESOLVED** (never guessed).
- Be **GitHub + Docker + Railway-ready** with **SQLite (SQLAlchemy)**, DB path via `SUPERBOT_DB_PATH` (default `/app/backend/data/superbot.db`).
- Add **single-owner password gate** (env `SUPERBOT_PASSWORD`) with signed server session cookie.

## 2) Implementation Steps

### Phase 1 — POC (skip=true)
- Skip standalone POC script (no external integrations).
- Use **pytest synthetic fixtures** as the isolation gate for the core workflow (ZIP ingest → safe extract → QA verdict → registry write).

### Phase 2 — V1 App Development (MVP)
**Backend (FastAPI, /api, 0.0.0.0:8001)**
1. **Data model (SQLAlchemy + Alembic-lite create_all)**
   - Tables: `sessions` (session_id canonical row), `qa_runs` (append-only per processing), `audit_log` (append-only), `raw_files` (retention state).
   - Ensure: reprocessing creates new `qa_runs`; never overwrite.
2. **Auth**
   - `/api/auth/login` (password check) → signed session cookie.
   - `/api/auth/logout`, `/api/auth/me`.
   - Middleware/dep to protect **all** app APIs.
3. **Upload + ingestion**
   - `/api/sessions/upload` accepts 1..N ZIPs + `retain_raw` flag.
   - Compute ZIP SHA256; attempt to derive `session_id` (from filename; if unknown mark provisional).
   - Store raw ZIP temporarily; apply ZIP security rules (zip-slip, limits).
4. **Deterministic QA engine (core)**
   - ZIP: readable + CRC check.
   - Collector: detect collector SHA256 from available evidence; mismatch ⇒ FAIL; unknown ⇒ UNRESOLVED.
   - Manifest/runtime: tolerant parser with key registry + synonyms; missing critical fields ⇒ UNRESOLVED.
   - Dataset structure: detect presence/counts of `sync_grid_100ms`, `normalized_books`, `normalized_trades`; missing/duplicate/unexpected parts ⇒ FAIL/WARNING per policy.
   - Parquet: magic bytes + footer metadata readability (no full loads).
   - Reconnects: record summary; recovered reconnect ≠ auto-fail.
   - Output: per-check breakdown + `verdict` (PASS/PASS_WITH_WARNING/FAIL/UNRESOLVED), `failure_reasons`, `warnings`.
5. **Duplicate detection**
   - Use `(session_id, zip_sha256)` to classify: NEW / EXACT_DUPLICATE / SAME_SESSION_DIFFERENT_FILE / CONFLICT.
   - Enforce: duplicates never add validated hours twice.
6. **Validated-hours + checkpoints**
   - `validated_hours` computed only from QA runs that qualify (policy: PASS and PASS_WITH_WARNING count; FAIL/UNRESOLVED count 0).
   - Checkpoint view: OLD36 / NEW12 / TOTAL48 / NEW36 / TOTAL72 + `72H_DATA_QA_READY` when required sessions are QA-qualified.
   - Implement **FrozenAnalysisEngine stub module** with status `NOT_CONFIGURED` and no approximation.
7. **Raw ZIP retention**
   - Default: delete raw ZIP after PASS; keep on FAIL/UNRESOLVED/crash or `retain_raw=true`.
   - Track retention state and expose via API/UI.
8. **Reports + export**
   - Per-session + global exports: JSON, CSV, Markdown.
   - QA Reports page backed by `/api/reports/...` endpoints.
9. **System/Audit log**
   - Append-only audit rows: upload, dedup decision, QA verdict, retention decision, reprocess.

**Frontend (React, 3000)**
10. **Login gate** (password form; maintain session).
11. Implement required pages (§12.2):
    - Overview (validated hours + readiness)
    - Upload Sessions (drag/drop, progress, retain toggle)
    - Session Registry (filter/sort/status)
    - Session Detail (all §12.3 fields + per-check breakdown)
    - QA Reports (downloads)
    - Checkpoints (progress bars + readiness flags)
    - Project Policy (render frozen rules verbatim)
    - System / Audit Log

**Phase 2 user stories (at least 5)**
1. As the owner, I must log in with an env password before I can access any page.
2. As the owner, I drag-and-drop ZIPs and see a per-file verdict (PASS/WARNING/FAIL/UNRESOLVED) with reasons.
3. As the owner, duplicates are detected (EXACT_DUPLICATE / SAME_SESSION_DIFFERENT_FILE / CONFLICT) and hours are not double-counted.
4. As the owner, missing manifest/runtime fields produce UNRESOLVED with a list of missing critical fields.
5. As the owner, I can open a Session Detail page and see every registry field + QA breakdown + retention state.

### Phase 3 — Testing, Docker, and Handoff Readiness
1. **Pytest suite (§14.2)** using small synthetic fixtures (<30s):
   - valid session, corrupted ZIP, wrong collector hash, exact duplicate, same session diff hash,
     missing Parquet part, duplicate Parquet part, corrupted Parquet metadata,
     watchdog true, nonzero exit code, writer error, recovered reconnect, unresolved reconnect,
     zip-slip safe path handling.
2. **End-to-end test pass** with testing agent: login → upload → registry → detail → exports → checkpoints.
3. **Docker + docs**
   - Add Dockerfile(s) + compose notes; ensure SQLite path uses persistent mount.
   - Add `README.md` + `.env.example` documenting `SUPERBOT_DB_PATH`, `SUPERBOT_PASSWORD`.
4. **Preview readiness step (critical)**
   - After app is up, user uploads first real 3H ZIP.
   - Validate/adapt manifest-field discovery (without guessing); update parser mapping if needed.

**Phase 3 user stories (at least 5)**
1. As a developer, I run `pytest` and all §14.2 tests finish in under 30 seconds.
2. As the owner, I can download registry-wide CSV/JSON/MD reports for audit.
3. As the owner, PASS deletes raw ZIP by default while FAIL/UNRESOLVED always retain it.
4. As the owner, reprocessing creates a new QA run and old runs remain visible.
5. As a developer, I can build the Docker image and run it with a mounted volume for SQLite.

## 3) Next Actions
- Implement Phase 2 backend core (models → auth → upload → QA engine → dedup → checkpoints → exports → audit log).
- Implement Phase 2 frontend pages + styling + session-based routing.
- Add pytest fixtures + full §14.2 coverage.
- Run testing agent E2E.
- Prepare Docker + README + `.env.example`.
- After Preview: ingest first real ZIP and adjust manifest key mapping to eliminate UNRESOLVED where appropriate.

## 4) Success Criteria
- All 8 required pages exist and are auth-protected.
- Uploading ZIPs produces deterministic QA verdicts and immutable registry entries.
- Duplicate detection works per spec; validated hours never double-count.
- Missing/unknown critical manifest fields yield UNRESOLVED (no guessed values) and prevent PASS.
- Checkpoints compute OLD36/NEW12/TOTAL48/NEW36/TOTAL72 and `72H_DATA_QA_READY` correctly.
- `FrozenAnalysisEngine` is present and clearly `NOT_CONFIGURED` (no fake analysis).
- Pytest covers all §14.2 scenarios and runs fast with synthetic fixtures.
- Repo is Docker-buildable and Railway-ready with SQLite on persistent volume.
