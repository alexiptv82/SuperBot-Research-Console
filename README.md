# SuperBot Research Console V1

Deterministic QA web application for MultiVenue 3H session ZIPs.
Phase 1 of the SuperBot Trading Project handoff (2026-09-15).

> **V1 scope:** upload session ZIPs → deterministic QA → immutable
> registry → validated-hours tracking toward OLD36 / NEW12 / TOTAL48
> / NEW36 / TOTAL72. No trading. No exchange credentials. No paper
> trading. No LLM at runtime.

Frozen collector SHA256:
`e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3`

## Stack

- Backend: FastAPI + SQLAlchemy + SQLite (portable to Postgres later)
- Frontend: React (CRA + shadcn/ui + Tailwind)
- Storage: single persistent volume (`SUPERBOT_DATA_DIR`) for the SQLite DB
  and retained raw ZIPs; no object storage, no Redis, no extra services
- Deployment target: single Railway service + persistent volume

## Local development (this Emergent template)

1. Backend and frontend are managed by supervisor:
   ```
   supervisorctl status
   ```
2. Backend env vars live in `backend/.env`. Frontend uses
   `REACT_APP_BACKEND_URL` from `frontend/.env` — do not modify.
3. Default password is `superbot`. Change `SUPERBOT_PASSWORD` before deploying.

### Run tests

```
cd backend
python -m pytest tests/ -q
```

All 16 scenarios from handoff §14.2 must pass in under 1s.

## Deploying to Railway

1. Push the repo to GitHub.
2. Create a new Railway project. Add a single service using this repo.
3. Set the following environment variables (see `.env.example`):
   - `SUPERBOT_PASSWORD` — single-owner password
   - `SUPERBOT_SESSION_SECRET` — 32+ char secret for session cookies
   - `SUPERBOT_DATA_DIR=/data`
   - `SUPERBOT_DB_PATH=/data/superbot.db`
   - `SUPERBOT_MAX_UPLOAD_MB=1024`
   - `CORS_ORIGINS` — frontend origin
4. Attach a persistent volume mounted at `/data`.
5. Build with the provided `Dockerfile`. Health check: `GET /api/health`.
6. Deploy the React frontend separately (`yarn build` → static hosting)
   and point `REACT_APP_BACKEND_URL` at the backend service URL.

## Frozen rules & prohibitions

See the in-app **Project Policy** page (`/policy`) or `GET /api/policy`:

- Collector hash frozen
- q80 / q90 / q95 definitions frozen; q90 primary
- Signal, composite, weight definitions frozen
- Horizon set frozen: 100ms, 200ms, 500ms, 1s, 2s, 5s, 10s, 30s
- 15 bps economic hurdle binding for standalone directional promotion
- **FrozenAnalysisEngine remains `NOT_CONFIGURED`** in V1. Do NOT
  approximate signal formulas from prose. Wire it only when the exact
  frozen validator is delivered.

## Data model highlights

- `sessions` — one row per canonical session_id.
- `qa_runs` — **append-only**; reprocessing creates a new run without
  overwriting old ones.
- `raw_files` — retention state; ZIP is deleted on PASS by default,
  retained on FAIL / UNRESOLVED / user toggle.
- `audit_log` — append-only ledger of every upload, verdict, retention
  decision and reprocess.

## Manifest tolerance

The manifest parser uses an alias registry (see
`backend/manifest_parser.py::FIELD_ALIASES`). Missing critical fields
(`exit_code`, `watchdog`, `collector_sha256`) yield `UNRESOLVED` and
never a fabricated PASS. Add real keys to the registry once the first
real MultiVenue 3H ZIP is validated through the console.

## Verdict vocabulary

`PASS`, `PASS_WITH_WARNING`, `FAIL`, `UNRESOLVED`. No new verdicts.

## Duplicate detection

`NEW`, `EXACT_DUPLICATE`, `SAME_SESSION_DIFFERENT_FILE`, `CONFLICT`.
Duplicates never add validated hours twice.
