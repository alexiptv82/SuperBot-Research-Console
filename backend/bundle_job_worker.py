"""Async multipart-bundle finalization worker.

Rationale
---------

Prior to this module, ``POST /api/bundles/multipart/{id}/assemble``
kept a single Cloudflare HTTP request open while performing:

    * 2.14 GiB streaming concatenation of five parts,
    * SHA256 verification,
    * outer ZIP validation,
    * per-session extraction + deterministic QA on 11 inner ZIPs.

Cloudflare's origin timeout (~100 s) closed the connection before the
work finished, surfacing as
``The origin web server returned an invalid or incomplete response``
in the browser, while the server actually completed the work.

This module lifts the entire heavy pipeline out of the request
lifecycle:

    * ``BundleJobManager.enqueue`` returns immediately with a job id;
    * a single-threaded background executor runs the real work;
    * every stage transition + counter is persisted to the
      ``bundle_jobs`` SQLite table so a browser refresh (or a full
      backend restart) can reattach to an in-flight or already-finished
      job by ``job_id``.

Design invariants (do not weaken):

    * **Client disconnect is NOT a failure**: Cloudflare closing the
      finalize request, or the browser navigating away, has no effect
      on the worker. Parts + assembled bundle only die on COMPLETE, on
      a deterministic validation failure that makes them unusable
      (bad SHA, bad ZIP), or on TTL cleanup.
    * **Idempotent**: re-enqueueing the same multipart_id while a job
      is active returns the existing job. Once a job succeeds, its
      row remains queryable forever.
    * **Bounded memory**: the module delegates every byte-level
      operation to ``MultipartBundleController.assemble`` and
      ``bundle_import.import_bundle``, both of which stream in 1 MiB
      chunks. Nothing here loads any part or the assembled bundle
      into RAM.
    * **Quantitative firewall preserved**: this module never opens
      inner parquet files itself; it only orchestrates the existing
      QA pipeline which already enforces the NEW36 firewall.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from audit import log_event
from database import SessionLocal
from models import BundleJob

logger = logging.getLogger("superbot.bundle_jobs")


# ---------------------------------------------------------------------------
# Public status constants
# ---------------------------------------------------------------------------

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETE = "COMPLETE"
STATUS_FAILED = "FAILED"
STATUS_RECOVERABLE = "RECOVERABLE"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_FAILED})
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})
RESUMABLE_STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_RUNNING, STATUS_RECOVERABLE, STATUS_FAILED}
)

STAGE_QUEUED = "QUEUED"
STAGE_PREPARING = "PREPARING"
STAGE_REASSEMBLING = "REASSEMBLING"
STAGE_VERIFYING_SHA256 = "VERIFYING_SHA256"
STAGE_VERIFYING_ZIP = "VERIFYING_ZIP"
STAGE_IMPORTING = "IMPORTING"
STAGE_CLEANUP = "CLEANUP"
STAGE_COMPLETE = "COMPLETE"
STAGE_FAILED = "FAILED"

# TTL for abandoned jobs (worker crashed, no updates for this long).
JOB_STALE_TTL_SECONDS = 6 * 60 * 60  # 6h; aligned with multipart TTL.


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def job_to_dict(job: BundleJob) -> dict:
    return {
        "job_id": job.id,
        "kind": job.kind,
        "multipart_id": job.multipart_id,
        "bundle_filename": job.bundle_filename,
        "expected_sha256": job.expected_sha256,
        "actual_sha256": job.actual_sha256,
        "bytes_total": job.bytes_total,
        "bytes_processed": job.bytes_processed,
        "parts_present": job.parts_present,
        "sessions_total": job.sessions_total,
        "sessions_processed": job.sessions_processed,
        "sessions_passed": job.sessions_passed,
        "sessions_failed": job.sessions_failed,
        "status": job.status,
        "current_stage": job.current_stage,
        "stage_detail": job.stage_detail,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "result_summary": job.result_summary,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "parts_map": job.parts_map,
    }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class BundleJobError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


ImportRunner = Callable[[OrmSession, Path, str, str, "BundleJob"], dict]
"""Callback signature for the actual bundle-import work.

The worker calls ``import_runner(db, outer_path, filename, digest, job)``.
The runner is responsible for the ZIP validation + per-session ingestion
and for calling ``job.mutate_progress(...)`` (via
``BundleJobManager.update_progress``) as it progresses.
"""


class BundleJobManager:
    """Thread-safe registry + background executor for bundle jobs.

    Only one instance should exist per process; ``server.py`` builds it
    at import time.
    """

    def __init__(
        self,
        *,
        multipart_controller,
        import_runner: ImportRunner,
        max_workers: int = 1,
    ) -> None:
        self.multipart_controller = multipart_controller
        self.import_runner = import_runner
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="bundle-job"
        )
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Enqueue / query
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        db: OrmSession,
        multipart_id: str,
        bundle_filename: str,
        expected_sha256: str,
        bytes_total: int,
        parts_present: int,
        sessions_total: int,
        parts_map: list[dict] | None = None,
    ) -> BundleJob:
        """Create + submit a job for the given multipart session.

        Idempotent: if a non-terminal job already exists for the same
        ``multipart_id`` it is returned as-is. If a terminal job
        exists, it is also returned (client sees COMPLETE / FAILED
        and can choose next action).

        ``parts_map`` (optional but strongly recommended) is a list
        of ``{part_index, upload_id, part_name, expected_size}`` dicts.
        Persisting it lets the manager rebuild the in-memory multipart
        session after a process restart from the 5 .part files that
        survived on disk, so a browser refresh + backend restart no
        longer strands 2.14 GiB of upload.
        """
        existing = self._latest_job_for_multipart(db, multipart_id)
        if existing is not None and existing.status in ACTIVE_STATUSES:
            return existing

        job = BundleJob(
            kind="old36_multipart",
            multipart_id=multipart_id,
            bundle_filename=bundle_filename,
            expected_sha256=expected_sha256,
            bytes_total=bytes_total,
            parts_present=parts_present,
            sessions_total=sessions_total,
            status=STATUS_QUEUED,
            current_stage=STAGE_QUEUED,
            stage_detail="job queued",
            parts_map=parts_map,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        fut = self._executor.submit(self._run_job, job.id)
        with self._lock:
            self._futures[job.id] = fut
        return job

    def get(self, db: OrmSession, job_id: str) -> BundleJob:
        row = db.execute(
            select(BundleJob).where(BundleJob.id == job_id)
        ).scalars().first()
        if row is None:
            raise BundleJobError(404, f"Unknown job_id: {job_id}")
        return row

    def latest_active(self, db: OrmSession) -> BundleJob | None:
        return db.execute(
            select(BundleJob)
            .where(BundleJob.status.in_(list(ACTIVE_STATUSES)))
            .order_by(BundleJob.created_at.desc())
        ).scalars().first()

    def _latest_job_for_multipart(
        self, db: OrmSession, multipart_id: str
    ) -> BundleJob | None:
        return db.execute(
            select(BundleJob)
            .where(BundleJob.multipart_id == multipart_id)
            .order_by(BundleJob.created_at.desc())
        ).scalars().first()

    # ------------------------------------------------------------------
    # Progress helpers (called from worker + import runner)
    # ------------------------------------------------------------------

    def update(
        self,
        db: OrmSession,
        job_id: str,
        *,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        stage_detail: Optional[str] = None,
        bytes_processed: Optional[int] = None,
        actual_sha256: Optional[str] = None,
        sessions_total: Optional[int] = None,
        sessions_processed: Optional[int] = None,
        sessions_passed: Optional[int] = None,
        sessions_failed: Optional[int] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        result_summary: Optional[dict] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        handoff_path: Optional[str] = None,
    ) -> BundleJob:
        job = self.get(db, job_id)
        if status is not None:
            job.status = status
        if stage is not None:
            job.current_stage = stage
        if stage_detail is not None:
            job.stage_detail = stage_detail
        if bytes_processed is not None:
            job.bytes_processed = int(bytes_processed)
        if actual_sha256 is not None:
            job.actual_sha256 = actual_sha256
        if sessions_total is not None:
            job.sessions_total = int(sessions_total)
        if sessions_processed is not None:
            job.sessions_processed = int(sessions_processed)
        if sessions_passed is not None:
            job.sessions_passed = int(sessions_passed)
        if sessions_failed is not None:
            job.sessions_failed = int(sessions_failed)
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        if result_summary is not None:
            job.result_summary = result_summary
        if started_at is not None:
            job.started_at = started_at
        if finished_at is not None:
            job.finished_at = finished_at
        if handoff_path is not None:
            job.handoff_path = handoff_path
        job.updated_at = _utcnow_iso()
        db.commit()
        db.refresh(job)
        return job

    # ------------------------------------------------------------------
    # Worker body
    # ------------------------------------------------------------------

    def _run_job(self, job_id: str) -> None:
        """Background-thread entry point. Owns its own DB session."""
        db = SessionLocal()
        try:
            self.update(
                db,
                job_id,
                status=STATUS_RUNNING,
                stage=STAGE_PREPARING,
                stage_detail="starting worker",
                started_at=_utcnow_iso(),
            )
            job = self.get(db, job_id)
            multipart_id = job.multipart_id
            if not multipart_id:
                self._fail(db, job_id, "NO_MULTIPART_ID", "job missing multipart_id")
                return

            # 1. Reassemble the parts + verify SHA256. This step is
            #    deterministic: on failure we still keep the uploaded
            #    parts so the user does not have to re-upload 2.14 GiB.
            self.update(
                db,
                job_id,
                stage=STAGE_REASSEMBLING,
                stage_detail="concatenating 5 parts",
            )
            self.update(
                db,
                job_id,
                stage=STAGE_REASSEMBLING,
                stage_detail="concatenating 5 parts",
                bytes_processed=0,
            )

            def _on_reassemble_progress(bytes_written: int) -> None:
                # Persist a heartbeat every ~64 MiB so the frontend
                # can distinguish a slow disk from a dead worker.
                try:
                    self.update(
                        db,
                        job_id,
                        bytes_processed=int(bytes_written),
                    )
                except Exception:  # noqa: BLE001
                    # A failed heartbeat must never abort assembly
                    # (e.g. transient DB lock).
                    pass

            try:
                session, outer_path, digest = self.multipart_controller.assemble(
                    multipart_id, on_progress=_on_reassemble_progress
                )
            except Exception as exc:  # MultipartBundleError or unexpected
                self._fail(
                    db,
                    job_id,
                    "REASSEMBLY_FAILED",
                    getattr(exc, "detail", None) or str(exc),
                )
                return

            # 2. Move the assembled bundle OUT of the multipart workdir
            #    so the worker (not the multipart controller) owns its
            #    lifetime. Parts stay on disk until the job reaches
            #    COMPLETE (see step 5).
            handoff_dir = self._handoff_dir()
            handoff_dir.mkdir(parents=True, exist_ok=True)
            handoff_path = handoff_dir / f"{job_id}-{session.workdir.name}.zip"
            os.replace(outer_path, handoff_path)

            self.update(
                db,
                job_id,
                stage=STAGE_VERIFYING_SHA256,
                stage_detail="sha256 verified",
                actual_sha256=digest,
                bytes_processed=job.bytes_total,
                handoff_path=str(handoff_path),
            )

            # 3. Verify + import the outer ZIP. The runner is
            #    responsible for opening the ZIP, running the frozen
            #    outer-bundle validator, and dispatching each inner
            #    session ZIP through the shared QA pipeline. It calls
            #    back into ``self.update`` as sessions advance.
            self.update(
                db,
                job_id,
                stage=STAGE_VERIFYING_ZIP,
                stage_detail="opening outer archive",
            )
            try:
                job_row = self.get(db, job_id)
                result = self.import_runner(
                    db,
                    handoff_path,
                    job_row.bundle_filename,
                    digest,
                    job_row,
                )
            except Exception as exc:
                logger.exception("bundle-job %s: import runner crashed", job_id)
                # NOTE: the parts have already been consumed by
                # ``multipart_controller.assemble`` (they only exist as
                # the reassembled bundle). We keep the handoff bundle
                # so an admin can retry manually via the same job id.
                self._fail(
                    db,
                    job_id,
                    "IMPORT_FAILED",
                    f"{type(exc).__name__}: {exc}",
                )
                return

            # 4. Success -> COMPLETE. Clean up parts + assembled bundle.
            self.update(
                db,
                job_id,
                stage=STAGE_CLEANUP,
                stage_detail="removing temporary artefacts",
            )
            try:
                self.multipart_controller.consume(multipart_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "bundle-job %s: cleanup of multipart session failed", job_id
                )
            try:
                handoff_path.unlink(missing_ok=True)
            except OSError:
                pass

            self.update(
                db,
                job_id,
                status=STATUS_COMPLETE,
                stage=STAGE_COMPLETE,
                stage_detail="import complete",
                result_summary=result,
                finished_at=_utcnow_iso(),
            )
            log_event(
                db,
                event_type="bundle.job.complete",
                message=f"job {job_id}: multipart bundle import complete",
                outcome="PASS",
                payload={"job_id": job_id, "multipart_id": multipart_id},
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 - top-level guard
            logger.exception("bundle-job %s: worker crashed", job_id)
            tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            try:
                self._fail(db, job_id, "WORKER_CRASH", tb)
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()
            with self._lock:
                self._futures.pop(job_id, None)

    def _fail(
        self,
        db: OrmSession,
        job_id: str,
        code: str,
        message: str,
    ) -> None:
        self.update(
            db,
            job_id,
            status=STATUS_FAILED,
            stage=STAGE_FAILED,
            stage_detail=message,
            error_code=code,
            error_message=message,
            finished_at=_utcnow_iso(),
        )
        try:
            log_event(
                db,
                event_type="bundle.job.failed",
                message=f"job {job_id}: {code}: {message}",
                outcome="FAIL",
                payload={"job_id": job_id, "error_code": code},
            )
            db.commit()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def _handoff_dir(self) -> Path:
        # Same directory the sync flow uses for consistency.
        return Path(self.multipart_controller.base_dir).parent / "bundle_jobs_handoff"

    def is_alive(self, job_id: str) -> bool:
        """Return True if a worker future for ``job_id`` is currently
        executing in this process.

        Used to distinguish a slow-but-live job from an orphaned
        RUNNING row (worker thread crashed, or process was recycled).
        """
        with self._lock:
            fut = self._futures.get(job_id)
        if fut is None:
            return False
        return not fut.done()

    def startup_recover_orphans(self, db: OrmSession) -> list[str]:
        """On backend startup / manager re-init, every persisted job
        in ``QUEUED`` or ``RUNNING`` is by definition orphaned: no
        in-memory worker survived the restart. Flip them to
        ``RECOVERABLE`` with a stable error code so the frontend can
        surface a "Riprendi" button instead of an eternal spinner.

        Called at FastAPI startup. Idempotent.
        """
        rows = db.execute(
            select(BundleJob).where(BundleJob.status.in_(list(ACTIVE_STATUSES)))
        ).scalars().all()
        recovered: list[str] = []
        for row in rows:
            # Never rewrite a job that IS attached to a live worker
            # (e.g. very fast startup path where the executor already
            # has a fresh future). Belt-and-suspenders check.
            if self.is_alive(row.id):
                continue
            self.update(
                db,
                row.id,
                status=STATUS_RECOVERABLE,
                stage=row.current_stage,  # keep stage; only the status flips
                stage_detail="worker lost (process restart); safe to resume",
                error_code="WORKER_LOST",
            )
            recovered.append(row.id)
            try:
                log_event(
                    db,
                    event_type="bundle.job.recoverable",
                    message=(
                        f"job {row.id}: marked RECOVERABLE at startup "
                        f"(previous status={row.status}, stage={row.current_stage})"
                    ),
                    outcome="INFO",
                    payload={"job_id": row.id, "prev_status": row.status},
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            pass
        return recovered

    def resume(self, db: OrmSession, job_id: str) -> BundleJob:
        """Re-submit an orphaned job to the worker executor.

        Allowed for jobs in ``RECOVERABLE``, ``FAILED`` (after a
        transient error such as ENOSPC that has since been resolved),
        or ``RUNNING`` where ``is_alive`` proves the worker is dead.

        Idempotent: if a live worker is already attached to this job,
        the existing future is preserved and the current row is
        returned unchanged.
        """
        job = self.get(db, job_id)
        if self.is_alive(job_id):
            return job
        if job.status == STATUS_COMPLETE:
            return job
        if job.status not in RESUMABLE_STATUSES:
            raise BundleJobError(
                409,
                f"job {job_id} status={job.status} is not resumable",
            )
        if not job.multipart_id:
            raise BundleJobError(409, "job has no multipart_id, cannot resume")

        # Verify the multipart session still owns 5 fully-uploaded
        # parts so a resume never silently reruns against a partial
        # upload. If the multipart controller lost the in-memory
        # session (backend restart), rebuild it from the persisted
        # parts_map + the .part files still on disk.
        try:
            mp_session = self.multipart_controller.get(job.multipart_id)
        except Exception:
            mp_session = None
        if mp_session is None:
            if not job.parts_map:
                raise BundleJobError(
                    409,
                    f"multipart session {job.multipart_id} not in memory and "
                    "job has no persisted parts_map to rebuild from",
                )
            try:
                mp_session = self.multipart_controller.rebuild_from_parts_map(
                    multipart_id=job.multipart_id,
                    expected_sha256=job.expected_sha256,
                    expected_total_size=job.bytes_total,
                    parts_map=job.parts_map,
                )
            except Exception as exc:  # noqa: BLE001
                raise BundleJobError(
                    409,
                    f"failed to rebuild multipart session {job.multipart_id}: {exc}",
                ) from exc

        # If the assembly already succeeded once (assembled=True) but
        # the import phase died, we must NOT re-run assemble (it would
        # raise 409). We forbid this path for now and surface a clear
        # message; the FAILED row's handoff_path can be inspected by
        # an operator.
        if mp_session.assembled:
            raise BundleJobError(
                409,
                "reassembly already completed; resume of the import phase is "
                "not supported yet - the handoff bundle remains on disk under "
                "bundle_jobs_handoff/ for manual replay",
            )

        # Wipe any partial reassembled file so the worker starts clean.
        for name in ("part-reassembled",):
            stub = mp_session.workdir / f"OLD36_REFERENCE_BUNDLE.zip.{name}"
            try:
                stub.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

        # Reset progress counters + status. Keep the SAME row id so
        # polling / recovery keeps pointing at the same job.
        self.update(
            db,
            job_id,
            status=STATUS_QUEUED,
            stage=STAGE_QUEUED,
            stage_detail="job re-queued for resume",
            bytes_processed=0,
            actual_sha256=None,
            sessions_processed=0,
            sessions_passed=0,
            sessions_failed=0,
            error_code=None,
            error_message=None,
            result_summary=None,
            started_at=None,
            finished_at=None,
        )
        try:
            log_event(
                db,
                event_type="bundle.job.resume",
                message=f"job {job_id}: resume requested",
                outcome="INFO",
                payload={"job_id": job_id, "multipart_id": job.multipart_id},
            )
            db.commit()
        except Exception:  # noqa: BLE001
            pass

        fut = self._executor.submit(self._run_job, job_id)
        with self._lock:
            self._futures[job_id] = fut
        return self.get(db, job_id)

    def sweep_stale(self, db: OrmSession) -> int:
        """Mark abandoned jobs FAILED after JOB_STALE_TTL_SECONDS."""
        now = time.time()
        rows = db.execute(
            select(BundleJob).where(BundleJob.status.in_(list(ACTIVE_STATUSES)))
        ).scalars().all()
        marked = 0
        for row in rows:
            try:
                updated_ts = datetime.fromisoformat(row.updated_at).timestamp()
            except Exception:  # noqa: BLE001
                updated_ts = now
            if (now - updated_ts) > JOB_STALE_TTL_SECONDS:
                self._fail(db, row.id, "STALE_TIMEOUT", "no worker updates within TTL")
                marked += 1
        return marked

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        """Test hook: block until worker for ``job_id`` returns."""
        with self._lock:
            fut = self._futures.get(job_id)
        if fut is not None:
            fut.result(timeout=timeout)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)


__all__ = [
    "ACTIVE_STATUSES",
    "RESUMABLE_STATUSES",
    "TERMINAL_STATUSES",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_COMPLETE",
    "STATUS_FAILED",
    "STATUS_RECOVERABLE",
    "STAGE_QUEUED",
    "STAGE_PREPARING",
    "STAGE_REASSEMBLING",
    "STAGE_VERIFYING_SHA256",
    "STAGE_VERIFYING_ZIP",
    "STAGE_IMPORTING",
    "STAGE_CLEANUP",
    "STAGE_COMPLETE",
    "STAGE_FAILED",
    "JOB_STALE_TTL_SECONDS",
    "BundleJobError",
    "BundleJobManager",
    "job_to_dict",
]
