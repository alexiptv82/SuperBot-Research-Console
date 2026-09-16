"""SuperBot Research Console V1 \u2014 FastAPI application.

Deterministic QA over MultiVenue 3H session ZIPs. No exchange credentials,
no trading, no LLM at runtime. All routes are prefixed with ``/api`` per
platform ingress requirements.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session as OrmSession
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from audit import log_event
from auth import (
    SESSION_COOKIE,
    check_password,
    clear_session_cookie,
    issue_session_cookie,
    verify_session,
)
from checkpoint_registry import (
    CHECKPOINT_OLD36_REFERENCE,
    OLD36_REFERENCE_SESSIONS,
    CHECKPOINT_UNASSIGNED,
    checkpoint_batch_for,
)
from checkpoints import compute_checkpoints
from constants import (
    DUP_EXACT_DUPLICATE,
    DUP_NEW,
    FROZEN_COLLECTOR_SHA256,
    VERDICT_PASS,
    VERDICT_PASS_WITH_WARNING,
)
from database import get_db, init_db
from dedup import classify_upload, compute_validated_hours
from frozen_engine import current_status as engine_status
from models import AuditLog, QARun, RawFile, Session as SessionModel
from qa_engine import run_qa
from raw_storage import (
    canonical_path as raw_canonical_path,
    persist_bytes as raw_persist_bytes,
    persist_from_path as raw_persist_from_path,
    prune_orphan_blobs as raw_prune_orphan_blobs,
)
from reports import to_csv, to_json, to_markdown
from uploads import DEFAULT_CHUNK_SIZE, MAX_UPLOAD_BYTES, UploadError, UploadManager
from zip_security import sha256_of_source

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("superbot")

app = FastAPI(title="SuperBot Research Console V1")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path(os.environ.get("SUPERBOT_DATA_DIR", "/app/backend/data"))
RAW_DIR = DATA_DIR / "raw_zips"
RAW_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
BUNDLE_UPLOAD_DIR = DATA_DIR / "bundle_uploads"

# 3 GiB ceiling for the outer OLD36 bundle (real bundle is ~2.3 GiB).
# Single-session upload cap is untouched (SUPERBOT_MAX_UPLOAD_MB).
BUNDLE_MAX_BYTES = 3 * 1024 * 1024 * 1024

upload_manager = UploadManager(UPLOAD_DIR)
bundle_manager = UploadManager(
    BUNDLE_UPLOAD_DIR,
    max_upload_bytes=BUNDLE_MAX_BYTES,
    label="bundle",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    logger.info("SuperBot Research Console V1 ready. DB=%s", os.environ.get("SUPERBOT_DB_PATH"))


@app.on_event("shutdown")
def _shutdown() -> None:
    try:
        bundle_job_manager.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> str:
    return verify_session(request)


# ---------------------------------------------------------------------------
# Pydantic response schemas (minimal; kept close to the DB shape)
# ---------------------------------------------------------------------------


class LoginBody(BaseModel):
    password: str


class UploadResultItem(BaseModel):
    filename: str
    session_id: str | None
    file_sha256: str
    duplicate_status: str
    verdict: str
    validated_hours: float
    qa_run_id: str
    failure_reasons: list[str]
    warnings: list[str]
    missing_fields: list[str]
    retained: bool


# ---------------------------------------------------------------------------
# Health + policy (public)
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "superbot-research-console-v1"}


@app.get("/api/policy")
def policy() -> dict:
    """Return frozen project policy that the console must never change."""
    return {
        "collector_sha256": FROZEN_COLLECTOR_SHA256,
        "verdicts": ["PASS", "PASS_WITH_WARNING", "FAIL", "UNRESOLVED"],
        "duplicate_states": [
            "NEW",
            "EXACT_DUPLICATE",
            "SAME_SESSION_DIFFERENT_FILE",
            "CONFLICT",
        ],
        "frozen_horizons": ["100ms", "200ms", "500ms", "1s", "2s", "5s", "10s", "30s"],
        "economic_hurdle_bps": 15,
        "quantiles": ["q80", "q90", "q95"],
        "frozen_rules": [
            "Later deterministic measurements supersede prose.",
            "Narrative AI output never overrides a numeric FAIL.",
            "Collector code/hash frozen.",
            "q80/q90/q95 definitions frozen; q90 primary.",
            "Signal, composite and weight definitions frozen.",
            "Overlap rules frozen.",
            "Data-quality filters frozen.",
            "Horizon set frozen: 100ms, 200ms, 500ms, 1s, 2s, 5s, 10s, 30s.",
            "15 bps economic hurdle binding for standalone directional promotion.",
            "100ms\u20135s execution timing interpretation; 10s\u201330s markout / adverse-selection.",
        ],
        "prohibitions_v1": [
            "No exchange credentials.",
            "No create/cancel order endpoints.",
            "No live positions, withdrawals, or trading buttons.",
            "No paper trading in V1.",
            "No AI runtime dependency required for QA.",
            "No modification of the frozen collector.",
            "No modification of the frozen quantitative methodology.",
            "No production trading deployment.",
        ],
        "engine": engine_status().__dict__,
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response, db: OrmSession = Depends(get_db)) -> dict:
    if not check_password(body.password):
        log_event(db, "auth.login_failed", "Invalid password attempt", outcome="FAIL")
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")
    issue_session_cookie(response, request)
    log_event(db, "auth.login_success", "Owner logged in", outcome="PASS")
    db.commit()
    return {"authenticated": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: OrmSession = Depends(get_db), _: str = Depends(require_auth)) -> dict:
    clear_session_cookie(response, request)
    log_event(db, "auth.logout", "Owner logged out", outcome="PASS")
    db.commit()
    return {"authenticated": False}


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    try:
        verify_session(request)
        return {"authenticated": True, "actor": "owner"}
    except HTTPException:
        return {"authenticated": False}


# ---------------------------------------------------------------------------
# Upload + QA
# ---------------------------------------------------------------------------


def _persist_raw_bytes(data: bytes, sha256: str) -> Path:
    """Retain a bytes blob at the canonical SHA256-keyed path.

    Reuses the existing file when we have already retained this exact
    binary. See ``raw_storage.persist_bytes``.
    """
    return raw_persist_bytes(RAW_DIR, data, sha256)


def _persist_raw_path(src: Path, sha256: str) -> Path:
    """Retain an already-on-disk file at the canonical SHA256-keyed path.

    If the canonical file already exists, ``src`` is unlinked and the
    canonical path is reused. See ``raw_storage.persist_from_path``.
    """
    return raw_persist_from_path(RAW_DIR, src, sha256)


def _auto_checkpoint(session_id: str | None) -> str:
    """Auto-assign the checkpoint batch label from the frozen NEW36 registry.

    Explicitly does NOT infer from filename or timestamp: only exact
    membership in the frozen list matters. Unknown ids -> UNASSIGNED.
    """
    return checkpoint_batch_for(session_id)


def _process_source(
    db: OrmSession,
    source,  # bytes | Path
    filename: str,
    retain_raw: bool,
    checkpoint_hint_override: str | None,
) -> UploadResultItem:
    """Run QA against a bytes blob OR an on-disk file. When ``source`` is
    a :class:`Path` we never load the file into RAM; QA + SHA256 both
    stream from disk."""
    is_path = isinstance(source, Path)

    # 1. Run QA (streams from disk if source is a path).
    report = run_qa(str(source) if is_path else source, filename)

    # 2. Duplicate detection uses session_id + file SHA256 (\u00a712.4)
    dup_status = classify_upload(db, report.session_id, report.file_sha256)

    # 3. Compute validated_hours contribution
    duration_hours = None
    try:
        duration_hours = float(report.fields.get("duration_hours")) if report.fields.get("duration_hours") else None
    except (TypeError, ValueError):
        duration_hours = None
    validated_hours = compute_validated_hours(report.verdict, dup_status, duration_hours)

    # 4. Resolve / create Session row
    session_key = report.session_id or f"UNKNOWN::{report.file_sha256[:12]}"
    session_row = db.execute(
        select(SessionModel).where(SessionModel.session_id == session_key)
    ).scalars().first()
    if session_row is None:
        session_row = SessionModel(session_id=session_key)
        db.add(session_row)
        db.flush()
    session_row.last_seen_at = datetime.now(timezone.utc).isoformat()
    checkpoint_hint = checkpoint_hint_override or _auto_checkpoint(report.session_id)
    # Always set the batch label - never leave it null, so the Registry
    # UI clearly shows UNASSIGNED for anything outside the frozen NEW36.
    if checkpoint_hint_override:
        session_row.checkpoint_hint = checkpoint_hint_override
    elif not session_row.checkpoint_hint or session_row.checkpoint_hint == CHECKPOINT_UNASSIGNED:
        session_row.checkpoint_hint = checkpoint_hint or CHECKPOINT_UNASSIGNED

    # 5. Create immutable QA run
    qa_run = QARun(
        session_pk=session_row.id,
        session_id=session_key,
        original_filename=filename,
        source_file_sha256=report.file_sha256,
        collector_sha256=(
            str(report.fields.get("collector_sha256")).lower()
            if report.fields.get("collector_sha256") is not None
            else None
        ),
        start_time=report.fields.get("start_time"),
        end_time=report.fields.get("end_time"),
        duration_hours=duration_hours,
        operational_status=report.verdict,
        duplicate_status=dup_status,
        zip_crc_status=report.checks.get("zip", {}).get("status"),
        manifest_status=report.checks.get("manifest", {}).get("status"),
        runtime_status=report.checks.get("runtime", {}).get("status"),
        watchdog_status=(
            "TRUE" if report.fields.get("watchdog") else ("FALSE" if report.fields.get("watchdog") is False else None)
        ),
        exit_code=int(report.fields["exit_code"]) if isinstance(report.fields.get("exit_code"), (int, float)) else None,
        sync_grid_file_count=report.fields.get("sync_grid_file_count"),
        books_file_count=report.fields.get("books_file_count"),
        trades_file_count=report.fields.get("trades_file_count"),
        parquet_total=report.fields.get("parquet_total"),
        parquet_magic_status=report.fields.get("parquet_magic_status"),
        sync_sequence_status=report.fields.get("sync_sequence_status"),
        books_sequence_status=report.fields.get("books_sequence_status"),
        trades_sequence_status=report.fields.get("trades_sequence_status"),
        missed_ticks=report.fields.get("missed_ticks"),
        theoretical_ticks=report.fields.get("theoretical_ticks"),
        missed_tick_pct=report.fields.get("missed_tick_pct"),
        lag_gt_50ms=report.fields.get("lag_gt_50ms"),
        max_lag_ms=report.fields.get("max_lag_ms"),
        writer_errors=report.fields.get("writer_errors"),
        websocket_errors=report.fields.get("websocket_errors"),
        reconnect_count=report.fields.get("reconnect_count"),
        reconnect_summary=report.fields.get("reconnect_summary"),
        unknown_side_count=report.fields.get("unknown_side_count"),
        final_buffer_status=(
            str(report.fields.get("final_buffer_status")) if report.fields.get("final_buffer_status") is not None else None
        ),
        validated_hours=validated_hours,
        failure_reasons=list(report.failure_reasons),
        warnings=list(report.warnings),
        checks=report.checks,
        manifest_raw=report.manifest_raw,
        missing_fields=list(report.missing_fields),
    )
    db.add(qa_run)
    db.flush()
    session_row.current_qa_run_id = qa_run.id

    # 6. Retention policy per \u00a712.7
    should_retain = retain_raw or report.verdict not in (VERDICT_PASS, VERDICT_PASS_WITH_WARNING)
    if should_retain:
        if is_path:
            stored = _persist_raw_path(source, report.file_sha256)
        else:
            stored = _persist_raw_bytes(source, report.file_sha256)
        rf = RawFile(
            qa_run_id=qa_run.id,
            stored_path=str(stored),
            size_bytes=stored.stat().st_size,
            retained=True,
            retention_reason=(
                "user_toggle" if retain_raw and report.verdict in (VERDICT_PASS, VERDICT_PASS_WITH_WARNING)
                else "non_pass_verdict"
            ),
        )
        db.add(rf)
    else:
        # For path sources, delete the assembled .part / temp file.
        if is_path:
            try:
                Path(source).unlink(missing_ok=True)
            except OSError:
                pass
        rf = RawFile(
            qa_run_id=qa_run.id,
            stored_path=None,
            size_bytes=None,
            retained=False,
            retention_reason="pass_default_delete",
            deleted_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(rf)

    # 7. Audit
    log_event(
        db,
        event_type="upload.qa",
        session_id=session_key,
        qa_run_id=qa_run.id,
        outcome=report.verdict,
        message=f"Uploaded {filename}; dup={dup_status}; hours={validated_hours}",
        payload={"failure_reasons": report.failure_reasons, "warnings": report.warnings},
    )

    return UploadResultItem(
        filename=filename,
        session_id=report.session_id,
        file_sha256=report.file_sha256,
        duplicate_status=dup_status,
        verdict=report.verdict,
        validated_hours=validated_hours,
        qa_run_id=qa_run.id,
        failure_reasons=list(report.failure_reasons),
        warnings=list(report.warnings),
        missing_fields=list(report.missing_fields),
        retained=should_retain,
    )


# Back-compat alias for existing tests / small in-memory uploads.
def _process_bytes(db, data, filename, retain_raw, checkpoint_hint_override):
    return _process_source(db, data, filename, retain_raw, checkpoint_hint_override)



@app.post("/api/sessions/upload")
async def upload_sessions(
    files: list[UploadFile] = File(...),
    retain_raw: bool = Form(False),
    checkpoint_hint: str | None = Form(None),
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Legacy single-shot multipart upload.

    Kept for backwards compatibility and unit tests with small synthetic
    ZIPs. Real (100\u2013500 MB+) sessions MUST go through the chunked
    endpoints below (/api/uploads/*), which stream to disk and are
    resilient to proxy body-size limits.
    """
    results: list[UploadResultItem] = []
    # Cap the legacy endpoint tightly so nobody accidentally uses it for
    # a huge ZIP and hits the proxy limit / times out.
    legacy_cap = 64 * 1024 * 1024  # 64 MiB
    for f in files:
        data = bytearray()
        while True:
            chunk = await f.read(1024 * 1024)
            if not chunk:
                break
            if len(data) + len(chunk) > legacy_cap:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        f"File exceeds {legacy_cap} bytes on the legacy endpoint. "
                        "Use the chunked upload API (/api/uploads/init, "
                        "/api/uploads/{id}/chunk/{index}, /api/uploads/{id}/complete)."
                    ),
                )
            data.extend(chunk)
        try:
            item = _process_bytes(
                db, bytes(data), f.filename or "unknown.zip", retain_raw, checkpoint_hint
            )
            results.append(item)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Upload processing failed for %s", f.filename)
            log_event(
                db,
                event_type="upload.error",
                message=f"crash processing {f.filename}: {exc}",
                outcome="FAIL",
            )
            raise HTTPException(status_code=500, detail=f"Processing failed: {exc}")
    db.commit()
    return {"count": len(results), "results": [r.model_dump() for r in results]}


# ---------------------------------------------------------------------------
# Chunked upload API (supports large session ZIPs up to SUPERBOT_MAX_UPLOAD_MB)
# ---------------------------------------------------------------------------


class UploadInitBody(BaseModel):
    filename: str
    total_size: int
    chunk_size: int | None = None
    retain_raw: bool = False
    checkpoint_hint: str | None = None


class UploadCompleteBody(BaseModel):
    sha256: str | None = None


@app.get("/api/uploads/limits")
def uploads_limits(_: str = Depends(require_auth)) -> dict:
    return {
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "recommended_chunk_size": DEFAULT_CHUNK_SIZE,
    }


@app.post("/api/uploads/init")
def uploads_init(body: UploadInitBody, _: str = Depends(require_auth)) -> dict:
    try:
        session = upload_manager.init(
            filename=body.filename,
            total_size=body.total_size,
            chunk_size=body.chunk_size or DEFAULT_CHUNK_SIZE,
            retain_raw=body.retain_raw,
            checkpoint_hint=body.checkpoint_hint,
        )
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return session.to_dict()


@app.get("/api/uploads/{upload_id}")
def uploads_status(upload_id: str, _: str = Depends(require_auth)) -> dict:
    try:
        session = upload_manager.get(upload_id)
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return session.to_dict()


@app.post("/api/uploads/{upload_id}/chunk/{index}")
async def uploads_chunk(
    upload_id: str,
    index: int,
    request: Request,
    _: str = Depends(require_auth),
) -> dict:
    # Read the raw binary body without any multipart parsing. Bounded by
    # MAX_CHUNK_BYTES via the manager.
    data = await request.body()
    try:
        session = upload_manager.write_chunk(upload_id, index, data)
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {
        "upload_id": session.id,
        "index": index,
        "received_count": len(session.received),
        "total_chunks": session.total_chunks,
        "progress": len(session.received) / session.total_chunks,
    }


@app.post("/api/uploads/{upload_id}/complete")
def uploads_complete(
    upload_id: str,
    body: UploadCompleteBody,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    # 1. Assemble + verify size and SHA256.
    try:
        session, digest = upload_manager.finalize(upload_id, body.sha256)
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    part_path = session.part_path
    filename = session.filename
    retain_raw = session.retain_raw
    checkpoint_hint = session.checkpoint_hint
    # 2. Drop the session from the manager \u2014 we take ownership of the
    # file for QA (rename on retention, unlink otherwise).
    upload_manager.consume(upload_id)
    # 3. Run QA + registry + audit against the on-disk path (no RAM blowup).
    try:
        item = _process_source(
            db, part_path, filename, retain_raw, checkpoint_hint
        )
        db.commit()
    except Exception as exc:
        logger.exception("Chunked upload finalize failed for %s", filename)
        # Clean up the assembled file if QA blew up mid-processing.
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        log_event(
            db,
            event_type="upload.error",
            message=f"finalize crash for {filename}: {exc}",
            outcome="FAIL",
        )
        db.commit()
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}")
    result = item.model_dump()
    result["server_sha256"] = digest
    return result


@app.delete("/api/uploads/{upload_id}")
def uploads_abort(upload_id: str, _: str = Depends(require_auth)) -> dict:
    upload_manager.abort(upload_id)
    return {"upload_id": upload_id, "aborted": True}


@app.post("/api/uploads/cleanup_stale")
def uploads_cleanup_stale(_: str = Depends(require_auth)) -> dict:
    return {"removed": upload_manager.cleanup_stale()}


# ---------------------------------------------------------------------------
# OLD36 bundle import (usability wrapper over single-session ingestion)
# ---------------------------------------------------------------------------


class BundleInitBody(BaseModel):
    filename: str
    total_size: int
    chunk_size: int | None = None


class BundleCompleteBody(BaseModel):
    sha256: str | None = None


@app.get("/api/bundles/limits")
def bundles_limits(_: str = Depends(require_auth)) -> dict:
    return {
        "max_bundle_bytes": BUNDLE_MAX_BYTES,
        "recommended_chunk_size": DEFAULT_CHUNK_SIZE,
        "expected_inner_sessions": len(OLD36_REFERENCE_SESSIONS),
    }


@app.post("/api/bundles/init")
def bundles_init(body: BundleInitBody, _: str = Depends(require_auth)) -> dict:
    try:
        session = bundle_manager.init(
            filename=body.filename,
            total_size=body.total_size,
            chunk_size=body.chunk_size or DEFAULT_CHUNK_SIZE,
            retain_raw=False,
            checkpoint_hint=CHECKPOINT_OLD36_REFERENCE,
        )
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return session.to_dict()


@app.get("/api/bundles/{upload_id}")
def bundles_status(upload_id: str, _: str = Depends(require_auth)) -> dict:
    try:
        session = bundle_manager.get(upload_id)
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return session.to_dict()


@app.post("/api/bundles/{upload_id}/chunk/{index}")
async def bundles_chunk(
    upload_id: str,
    index: int,
    request: Request,
    _: str = Depends(require_auth),
) -> dict:
    data = await request.body()
    try:
        session = bundle_manager.write_chunk(upload_id, index, data)
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {
        "upload_id": session.id,
        "index": index,
        "received_count": len(session.received),
        "total_chunks": session.total_chunks,
        "progress": len(session.received) / session.total_chunks,
    }


@app.delete("/api/bundles/{upload_id}")
def bundles_abort(upload_id: str, _: str = Depends(require_auth)) -> dict:
    bundle_manager.abort(upload_id)
    return {"upload_id": upload_id, "aborted": True}


@app.post("/api/bundles/cleanup_stale")
def bundles_cleanup_stale(_: str = Depends(require_auth)) -> dict:
    return {"removed": bundle_manager.cleanup_stale()}


# ---------------------------------------------------------------------------
# OLD36 multipart bundle import (5 raw binary parts \u2014 transport wrapper)
# ---------------------------------------------------------------------------

from multipart_bundle import (
    MultipartBundleController,
    MultipartBundleError,
)
import multipart_bundle as _mp_mod
from bundle_job_worker import (
    ACTIVE_STATUSES as JOB_ACTIVE_STATUSES,
    BundleJobError,
    BundleJobManager,
    STAGE_IMPORTING,
    STATUS_COMPLETE,
    job_to_dict,
)

MULTIPART_WORK_DIR = DATA_DIR / "bundle_multipart"
multipart_controller = MultipartBundleController(
    upload_manager=bundle_manager,
    base_dir=MULTIPART_WORK_DIR,
)


def _bundle_import_runner(
    db: OrmSession,
    outer_path: Path,
    filename: str,
    digest: str,
    job,  # BundleJob (avoid circular import in signature)
) -> dict:
    """Import runner invoked by the async bundle-job worker.

    Runs outer-archive validation + per-session ingestion, streaming
    progress updates back into the ``bundle_jobs`` row so the
    frontend poller sees live 1/11 .. 11/11 progression.
    """
    from bundle_import import (
        BundleImportError,
        import_bundle,
        summarize as bundle_summarize,
    )

    def _process_inner(inner_path, inner_name, checkpoint_hint):
        item = _process_source(db, inner_path, inner_name, True, checkpoint_hint)
        db.commit()
        return item.model_dump()

    passed = 0
    failed = 0

    def _on_progress(stage: str, payload: dict) -> None:
        nonlocal passed, failed
        if stage == "validated":
            bundle_job_manager.update(
                db,
                job.id,
                stage=STAGE_IMPORTING,
                stage_detail=(
                    f"importing session 0/{payload.get('found', 0)}"
                ),
                sessions_total=int(payload.get("found") or job.sessions_total),
                sessions_processed=0,
                sessions_passed=0,
                sessions_failed=0,
            )
        elif stage == "session_start":
            idx = int(payload.get("index") or 0)
            total = int(payload.get("total") or 0)
            sid = str(payload.get("session_id") or "")
            bundle_job_manager.update(
                db,
                job.id,
                stage=STAGE_IMPORTING,
                stage_detail=f"importing session {idx}/{total} ({sid[:24]})",
            )
        elif stage == "session_end":
            idx = int(payload.get("index") or 0)
            total = int(payload.get("total") or 0)
            if payload.get("ok"):
                passed += 1
            else:
                failed += 1
            bundle_job_manager.update(
                db,
                job.id,
                sessions_processed=idx,
                sessions_passed=passed,
                sessions_failed=failed,
                stage_detail=f"completed session {idx}/{total}",
            )
        # "cleanup" transitions are handled by the worker itself.

    log_event(
        db,
        event_type="bundle.import.start",
        message=f"bundle finalize: {filename} sha256={digest[:16]}\u2026",
        outcome="INFO",
    )
    db.commit()

    try:
        results = import_bundle(
            outer_path, process_inner=_process_inner, on_progress=_on_progress
        )
    except BundleImportError as exc:
        log_event(
            db,
            event_type="bundle.import.reject",
            message=f"{filename}: {exc}",
            outcome="FAIL",
        )
        db.commit()
        raise

    summary = bundle_summarize(results)
    log_event(
        db,
        event_type="bundle.import.complete",
        message=(
            f"{filename}: ok={summary['ok']} failed={summary['failed']} "
            f"total={summary['total']}"
        ),
        outcome="PASS" if summary["failed"] == 0 else "PARTIAL",
    )
    db.commit()

    return {
        "bundle_filename": filename,
        "bundle_sha256": digest,
        "results": summary,
        "old36_reference": compute_checkpoints(db)["old36_reference"],
        "multipart_reassembly": {
            "bundle_name": filename,
            "bundle_size": job.bytes_total,
            "bundle_sha256": digest,
        },
    }


bundle_job_manager = BundleJobManager(
    multipart_controller=multipart_controller,
    import_runner=_bundle_import_runner,
    max_workers=1,
)


class MultipartPartDecl(BaseModel):
    name: str
    size: int


class MultipartInitBody(BaseModel):
    parts: list[MultipartPartDecl]


@app.get("/api/bundles/multipart/manifest")
def bundles_multipart_manifest(_: str = Depends(require_auth)) -> dict:
    """Publish the frozen multipart manifest so the browser can
    validate its file selection BEFORE calling init."""
    return {
        "bundle_name": _mp_mod.EXPECTED_BUNDLE_NAME,
        "bundle_total_size": _mp_mod.EXPECTED_BUNDLE_TOTAL_SIZE,
        "bundle_sha256": _mp_mod.EXPECTED_BUNDLE_SHA256,
        "parts": [{"name": n, "size": s} for n, s in _mp_mod.EXPECTED_PARTS],
    }


@app.post("/api/bundles/multipart/init")
def bundles_multipart_init(
    body: MultipartInitBody, _: str = Depends(require_auth)
) -> dict:
    try:
        session = multipart_controller.init(
            [p.model_dump() for p in body.parts]
        )
    except MultipartBundleError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return session.to_dict()


@app.get("/api/bundles/multipart/{multipart_id}")
def bundles_multipart_status(
    multipart_id: str, _: str = Depends(require_auth)
) -> dict:
    try:
        return multipart_controller.status(multipart_id)
    except MultipartBundleError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


@app.post(
    "/api/bundles/multipart/{multipart_id}/assemble",
    status_code=status.HTTP_202_ACCEPTED,
)
def bundles_multipart_assemble(
    multipart_id: str,
    response: Response,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Kick off the async multipart-bundle finalize job.

    Returns 202 Accepted immediately with a ``job_id`` the client can
    poll. Reassembly, SHA256 verification, outer-ZIP validation and
    per-session ingestion all happen in a background worker so that
    Cloudflare / browser disconnects cannot destroy in-flight work.
    """
    try:
        session = multipart_controller.get(multipart_id)
    except MultipartBundleError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    # Fast pre-check: every part must be fully uploaded before we
    # accept the job. This is cheap (just filesystem sizes) and lets
    # us reject early with 400 rather than surfacing the error through
    # the background job.
    parts_present = 0
    for slot in session.slots:
        try:
            us = bundle_manager.get(slot.upload_id)
        except UploadError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"part {slot.part_name}: {exc.detail}",
            )
        if len(us.received) != us.total_chunks:
            missing = us.total_chunks - len(us.received)
            raise HTTPException(
                status_code=400,
                detail=f"part {slot.part_name}: {missing} chunk(s) missing",
            )
        parts_present += 1

    job = bundle_job_manager.enqueue(
        db=db,
        multipart_id=multipart_id,
        bundle_filename=_mp_mod.EXPECTED_BUNDLE_NAME,
        expected_sha256=_mp_mod.EXPECTED_BUNDLE_SHA256,
        bytes_total=_mp_mod.EXPECTED_BUNDLE_TOTAL_SIZE,
        parts_present=parts_present,
        sessions_total=len(OLD36_REFERENCE_SESSIONS),
    )
    log_event(
        db,
        event_type="bundle.job.enqueued",
        message=(
            f"job {job.id} enqueued for multipart_id={multipart_id} "
            f"({parts_present} parts present)"
        ),
        outcome="INFO",
        payload={"job_id": job.id, "multipart_id": multipart_id},
    )
    db.commit()
    response.headers["Location"] = f"/api/bundles/jobs/{job.id}"
    return {"job_id": job.id, "status": job.status, "job": job_to_dict(job)}


@app.get("/api/bundles/jobs/active")
def bundles_jobs_active(
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Return the most-recent non-terminal job, if any.

    Used by the frontend to reattach to an in-flight multipart-bundle
    finalize after a browser refresh or full page reload.
    """
    job = bundle_job_manager.latest_active(db)
    if job is None:
        return {"job": None}
    return {"job": job_to_dict(job)}


@app.get("/api/bundles/jobs/{job_id}")
def bundles_jobs_get(
    job_id: str,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    try:
        job = bundle_job_manager.get(db, job_id)
    except BundleJobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {"job": job_to_dict(job)}


@app.post("/api/bundles/jobs/cleanup_stale")
def bundles_jobs_cleanup_stale(
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    return {"marked_failed": bundle_job_manager.sweep_stale(db)}


@app.delete("/api/bundles/multipart/{multipart_id}")
def bundles_multipart_abort(
    multipart_id: str, _: str = Depends(require_auth)
) -> dict:
    multipart_controller.abort(multipart_id)
    return {"multipart_id": multipart_id, "aborted": True}


@app.post("/api/bundles/multipart/cleanup_stale")
def bundles_multipart_cleanup_stale(_: str = Depends(require_auth)) -> dict:
    return {"removed": multipart_controller.cleanup_stale()}


def _run_bundle_import_from_path(
    db: OrmSession,
    outer_path: Path,
    filename: str,
    digest: str,
) -> dict:
    """Run inner-ZIP validation + per-session ingestion for an
    already-assembled outer OLD36 bundle. Deletes the outer bytes at
    the end regardless of outcome. Shared by the direct single-bundle
    finalize path and the multipart-assemble path."""
    from bundle_import import (
        BundleImportError,
        import_bundle,
        summarize as bundle_summarize,
    )

    def _process_inner(inner_path, inner_name, checkpoint_hint):
        item = _process_source(
            db, inner_path, inner_name, True, checkpoint_hint
        )
        db.commit()
        return item.model_dump()

    log_event(
        db,
        event_type="bundle.import.start",
        message=f"bundle finalize: {filename} sha256={digest[:16]}\u2026",
        outcome="INFO",
    )
    db.commit()

    try:
        results = import_bundle(outer_path, process_inner=_process_inner)
    except BundleImportError as exc:
        log_event(
            db, event_type="bundle.import.reject",
            message=f"{filename}: {exc}", outcome="FAIL",
        )
        db.commit()
        try:
            outer_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Bundle import crashed for %s", filename)
        log_event(
            db, event_type="bundle.import.error",
            message=f"{filename}: {exc}", outcome="FAIL",
        )
        db.commit()
        try:
            outer_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Bundle import failed: {exc}")

    summary = bundle_summarize(results)
    log_event(
        db, event_type="bundle.import.complete",
        message=(
            f"{filename}: ok={summary['ok']} failed={summary['failed']} "
            f"total={summary['total']}"
        ),
        outcome="PASS" if summary["failed"] == 0 else "PARTIAL",
    )
    db.commit()

    try:
        outer_path.unlink(missing_ok=True)
    except OSError:
        pass

    return {
        "bundle_filename": filename,
        "bundle_sha256": digest,
        "results": summary,
        "old36_reference": compute_checkpoints(db)["old36_reference"],
    }


@app.post("/api/bundles/{upload_id}/complete")
def bundles_complete(
    upload_id: str,
    body: BundleCompleteBody,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Finalize the outer bundle upload, then validate + dispatch its
    inner session ZIPs through the existing single-session ingestion
    pipeline. The outer archive is transport only; it is unlinked
    after the response is built regardless of success or failure."""
    try:
        session, digest = bundle_manager.finalize(upload_id, body.sha256)
    except UploadError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    outer_path = session.part_path
    filename = session.filename
    bundle_manager.consume(upload_id)
    return _run_bundle_import_from_path(db, outer_path, filename, digest)


# ---------------------------------------------------------------------------
# Registry + detail
# ---------------------------------------------------------------------------


def _qa_run_to_dict(run: QARun) -> dict[str, Any]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "original_filename": run.original_filename,
        "uploaded_at": run.uploaded_at,
        "source_file_sha256": run.source_file_sha256,
        "collector_sha256": run.collector_sha256,
        "start_time": run.start_time,
        "end_time": run.end_time,
        "duration_hours": run.duration_hours,
        "operational_status": run.operational_status,
        "duplicate_status": run.duplicate_status,
        "zip_crc_status": run.zip_crc_status,
        "manifest_status": run.manifest_status,
        "runtime_status": run.runtime_status,
        "watchdog_status": run.watchdog_status,
        "exit_code": run.exit_code,
        "sync_grid_file_count": run.sync_grid_file_count,
        "books_file_count": run.books_file_count,
        "trades_file_count": run.trades_file_count,
        "parquet_total": run.parquet_total,
        "parquet_magic_status": run.parquet_magic_status,
        "sync_sequence_status": run.sync_sequence_status,
        "books_sequence_status": run.books_sequence_status,
        "trades_sequence_status": run.trades_sequence_status,
        "missed_ticks": run.missed_ticks,
        "theoretical_ticks": run.theoretical_ticks,
        "missed_tick_pct": run.missed_tick_pct,
        "lag_gt_50ms": run.lag_gt_50ms,
        "max_lag_ms": run.max_lag_ms,
        "writer_errors": run.writer_errors,
        "websocket_errors": run.websocket_errors,
        "reconnect_count": run.reconnect_count,
        "reconnect_summary": run.reconnect_summary,
        "unknown_side_count": run.unknown_side_count,
        "final_buffer_status": run.final_buffer_status,
        "validated_hours": run.validated_hours,
        "failure_reasons": run.failure_reasons or [],
        "warnings": run.warnings or [],
        "checks": run.checks or {},
        "manifest_raw": run.manifest_raw,
        "missing_fields": run.missing_fields or [],
        "qa_timestamp": run.qa_timestamp,
        "retained": bool(run.raw_file and run.raw_file.retained),
        "retention_reason": run.raw_file.retention_reason if run.raw_file else None,
    }


@app.get("/api/sessions")
def list_sessions(
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = None,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """List latest QA run per session_id."""
    stmt = select(SessionModel)
    rows = db.execute(stmt).scalars().all()
    result = []
    for s in rows:
        run = None
        if s.current_qa_run_id:
            run = db.get(QARun, s.current_qa_run_id)
        if run is None:
            run = db.execute(
                select(QARun).where(QARun.session_pk == s.id).order_by(desc(QARun.uploaded_at))
            ).scalars().first()
        if run is None:
            continue
        if status_filter and run.operational_status != status_filter:
            continue
        if q and q.lower() not in (run.session_id or "").lower() and q.lower() not in (run.original_filename or "").lower():
            continue
        item = _qa_run_to_dict(run)
        item["checkpoint_hint"] = s.checkpoint_hint
        item["first_seen_at"] = s.first_seen_at
        item["last_seen_at"] = s.last_seen_at
        result.append(item)
    result.sort(key=lambda x: x["uploaded_at"], reverse=True)
    return {"count": len(result), "sessions": result}


@app.get("/api/sessions/{session_id}")
def get_session(
    session_id: str,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    s = db.execute(select(SessionModel).where(SessionModel.session_id == session_id)).scalars().first()
    if s is None:
        raise HTTPException(status_code=404, detail="Session not found")
    runs = db.execute(
        select(QARun).where(QARun.session_pk == s.id).order_by(desc(QARun.uploaded_at))
    ).scalars().all()
    return {
        "session_id": s.session_id,
        "checkpoint_hint": s.checkpoint_hint,
        "first_seen_at": s.first_seen_at,
        "last_seen_at": s.last_seen_at,
        "current_qa_run_id": s.current_qa_run_id,
        "qa_runs": [_qa_run_to_dict(r) for r in runs],
    }


@app.post("/api/sessions/{session_id}/reprocess")
def reprocess_session(
    session_id: str,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    """Re-run QA on the retained raw ZIP of the latest run, if available."""
    s = db.execute(select(SessionModel).where(SessionModel.session_id == session_id)).scalars().first()
    if s is None or not s.current_qa_run_id:
        raise HTTPException(status_code=404, detail="Session not found")
    latest = db.get(QARun, s.current_qa_run_id)
    if not latest or not latest.raw_file or not latest.raw_file.retained or not latest.raw_file.stored_path:
        raise HTTPException(status_code=409, detail="No retained raw ZIP to reprocess")
    src = latest.raw_file.stored_path
    if not os.path.exists(src):
        raise HTTPException(status_code=410, detail="Retained raw ZIP missing on disk")
    with open(src, "rb") as fh:
        data = fh.read()
    item = _process_bytes(
        db, data, latest.original_filename, retain_raw=True, checkpoint_hint_override=s.checkpoint_hint
    )
    log_event(
        db,
        event_type="session.reprocess",
        session_id=session_id,
        qa_run_id=item.qa_run_id,
        outcome=item.verdict,
        message=f"Reprocessed session {session_id}",
    )
    db.commit()
    return item.model_dump()


# ---------------------------------------------------------------------------
# Checkpoints + engine
# ---------------------------------------------------------------------------


@app.get("/api/checkpoints")
def checkpoints(db: OrmSession = Depends(get_db), _: str = Depends(require_auth)) -> dict:
    return compute_checkpoints(db)


@app.get("/api/reference/old36")
def reference_old36(db: OrmSession = Depends(get_db), _: str = Depends(require_auth)) -> dict:
    """Detailed OLD36 raw-reference availability.

    Read-only telemetry: NEVER affects milestone totals. The OLD36
    baseline (36.0h) is an immutable project constant.
    """
    return compute_checkpoints(db)["old36_reference"]


@app.get("/api/engine")
def engine(_: str = Depends(require_auth)) -> dict:
    return engine_status().__dict__


class CheckpointAssign(BaseModel):
    checkpoint_hint: str | None


@app.post("/api/sessions/{session_id}/checkpoint")
def assign_checkpoint(
    session_id: str,
    body: CheckpointAssign,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    s = db.execute(select(SessionModel).where(SessionModel.session_id == session_id)).scalars().first()
    if s is None:
        raise HTTPException(status_code=404, detail="Session not found")
    old = s.checkpoint_hint
    s.checkpoint_hint = body.checkpoint_hint
    log_event(
        db,
        event_type="session.checkpoint_assigned",
        session_id=session_id,
        message=f"checkpoint_hint {old} -> {body.checkpoint_hint}",
        outcome="PASS",
    )
    db.commit()
    return {"session_id": session_id, "checkpoint_hint": s.checkpoint_hint}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _all_latest_runs(db: OrmSession) -> list[QARun]:
    sessions = db.execute(select(SessionModel)).scalars().all()
    runs: list[QARun] = []
    for s in sessions:
        if s.current_qa_run_id:
            r = db.get(QARun, s.current_qa_run_id)
            if r:
                runs.append(r)
    runs.sort(key=lambda r: r.uploaded_at, reverse=True)
    return runs


@app.get("/api/reports/export")
def export_reports(
    fmt: str = Query("json", pattern="^(json|csv|md)$"),
    session_id: str | None = None,
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> Response:
    if session_id:
        s = db.execute(select(SessionModel).where(SessionModel.session_id == session_id)).scalars().first()
        if s is None:
            raise HTTPException(status_code=404, detail="Session not found")
        runs = db.execute(
            select(QARun).where(QARun.session_pk == s.id).order_by(desc(QARun.uploaded_at))
        ).scalars().all()
    else:
        runs = _all_latest_runs(db)

    if fmt == "json":
        body = to_json(runs)
        return Response(content=body, media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename=superbot_report.json"})
    if fmt == "csv":
        body = to_csv(runs)
        return Response(content=body, media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=superbot_report.csv"})
    body = to_markdown(runs)
    return Response(content=body, media_type="text/markdown",
                    headers={"Content-Disposition": f"attachment; filename=superbot_report.md"})


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@app.get("/api/audit")
def audit_log(
    event_type: str | None = None,
    outcome: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    db: OrmSession = Depends(get_db),
    _: str = Depends(require_auth),
) -> dict:
    stmt = select(AuditLog).order_by(desc(AuditLog.ts))
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if outcome:
        stmt = stmt.where(AuditLog.outcome == outcome)
    stmt = stmt.limit(limit)
    rows = db.execute(stmt).scalars().all()
    return {
        "count": len(rows),
        "events": [
            {
                "id": r.id,
                "ts": r.ts,
                "actor": r.actor,
                "event_type": r.event_type,
                "session_id": r.session_id,
                "qa_run_id": r.qa_run_id,
                "message": r.message,
                "outcome": r.outcome,
                "payload": r.payload,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Overview aggregate
# ---------------------------------------------------------------------------


@app.get("/api/overview")
def overview(db: OrmSession = Depends(get_db), _: str = Depends(require_auth)) -> dict:
    sessions = db.execute(select(SessionModel)).scalars().all()
    session_count = len(sessions)
    latest_runs = _all_latest_runs(db)
    verdict_counts = {"PASS": 0, "PASS_WITH_WARNING": 0, "FAIL": 0, "UNRESOLVED": 0}
    for r in latest_runs:
        verdict_counts[r.operational_status] = verdict_counts.get(r.operational_status, 0) + 1
    recent = [_qa_run_to_dict(r) for r in latest_runs[:8]]
    return {
        "sessions": session_count,
        "verdict_counts": verdict_counts,
        "recent": recent,
        "checkpoints": compute_checkpoints(db),
        "engine": engine_status().__dict__,
        "collector_sha256": FROZEN_COLLECTOR_SHA256,
    }
