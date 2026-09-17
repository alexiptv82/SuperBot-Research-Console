"""SQLAlchemy ORM models for SuperBot Research Console V1.

Design notes:
- ``sessions`` is the canonical per-session row (unique by ``session_id``).
- ``qa_runs`` is append-only: reprocessing a session inserts a new row and
  never overwrites an older run. The latest run is referenced by
  ``sessions.current_qa_run_id`` for convenience.
- ``audit_log`` is append-only and immutable in practice (no updates issued).
- ``raw_files`` tracks retention state per QA run.

All timestamps are stored as ISO-8601 UTC strings for portability across
SQLite / Postgres and to keep JSON export trivial.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Session(Base):
    """Canonical per-session record. Unique on ``session_id``."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    first_seen_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    last_seen_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    # Checkpoint hint parsed from filename / manifest if present
    checkpoint_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    # Convenience pointer to the latest qa_run.id
    current_qa_run_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("qa_runs.id", use_alter=True, name="fk_sessions_current_qa_run"), nullable=True
    )

    qa_runs: Mapped[list["QARun"]] = relationship(
        "QARun",
        back_populates="session",
        foreign_keys="QARun.session_pk",
        cascade="all, delete-orphan",
        order_by="QARun.uploaded_at.desc()",
    )


class QARun(Base):
    """Immutable per-processing QA run. Append-only."""

    __tablename__ = "qa_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_pk: Mapped[str] = mapped_column(String, ForeignKey("sessions.id"), index=True)

    # Identity
    session_id: Mapped[str] = mapped_column(String, index=True)
    original_filename: Mapped[str] = mapped_column(String)
    uploaded_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, index=True)
    source_file_sha256: Mapped[str] = mapped_column(String, index=True)
    collector_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    start_time: Mapped[str | None] = mapped_column(String, nullable=True)
    end_time: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_hours: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Verdict + dedup
    operational_status: Mapped[str] = mapped_column(String, index=True)  # PASS/PASS_WITH_WARNING/FAIL/UNRESOLVED
    duplicate_status: Mapped[str] = mapped_column(String, index=True)

    # Per-area statuses
    zip_crc_status: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_status: Mapped[str | None] = mapped_column(String, nullable=True)
    runtime_status: Mapped[str | None] = mapped_column(String, nullable=True)
    watchdog_status: Mapped[str | None] = mapped_column(String, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Dataset structure counts
    sync_grid_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    books_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trades_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parquet_total: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Parquet + sequence status
    parquet_magic_status: Mapped[str | None] = mapped_column(String, nullable=True)
    sync_sequence_status: Mapped[str | None] = mapped_column(String, nullable=True)
    books_sequence_status: Mapped[str | None] = mapped_column(String, nullable=True)
    trades_sequence_status: Mapped[str | None] = mapped_column(String, nullable=True)

    # Runtime counters
    missed_ticks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    theoretical_ticks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missed_tick_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    lag_gt_50ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_lag_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    writer_errors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    websocket_errors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reconnect_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reconnect_summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    unknown_side_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_buffer_status: Mapped[str | None] = mapped_column(String, nullable=True)

    # Hour accounting
    validated_hours: Mapped[float] = mapped_column(Float, default=0.0)

    # Reason arrays + raw QA report snapshot
    failure_reasons: Mapped[Any] = mapped_column(JSON, default=list)
    warnings: Mapped[Any] = mapped_column(JSON, default=list)
    checks: Mapped[Any] = mapped_column(JSON, default=dict)
    manifest_raw: Mapped[Any] = mapped_column(JSON, nullable=True)
    missing_fields: Mapped[Any] = mapped_column(JSON, default=list)

    qa_timestamp: Mapped[str] = mapped_column(String, default=_utcnow_iso)

    session: Mapped[Session] = relationship(
        "Session", back_populates="qa_runs", foreign_keys=[session_pk]
    )
    raw_file: Mapped["RawFile | None"] = relationship(
        "RawFile", back_populates="qa_run", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_qa_runs_session_upload", "session_id", "uploaded_at"),
    )


class RawFile(Base):
    """Retention state for the uploaded ZIP behind a QA run."""

    __tablename__ = "raw_files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    qa_run_id: Mapped[str] = mapped_column(String, ForeignKey("qa_runs.id"), unique=True)
    stored_path: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retained: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_reason: Mapped[str] = mapped_column(String, default="")
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)

    qa_run: Mapped[QARun] = relationship("QARun", back_populates="raw_file")


class BundleJob(Base):
    """Async multipart bundle finalization job.

    Persists the full lifecycle of a multipart-bundle reassembly + import
    so that Cloudflare / client disconnects cannot destroy in-flight work
    and browser refreshes can rejoin an active job.

    Rows are append-only in intent; ``status``/``current_stage`` /
    counters are updated in-place by the background worker (one row
    per job).
    """

    __tablename__ = "bundle_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String, default="old36_multipart", index=True)
    multipart_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    handoff_path: Mapped[str | None] = mapped_column(String, nullable=True)
    bundle_filename: Mapped[str] = mapped_column(String, default="")
    expected_sha256: Mapped[str] = mapped_column(String, default="")
    actual_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    bytes_total: Mapped[int] = mapped_column(Integer, default=0)
    bytes_processed: Mapped[int] = mapped_column(Integer, default=0)
    parts_present: Mapped[int] = mapped_column(Integer, default=0)
    sessions_total: Mapped[int] = mapped_column(Integer, default=0)
    sessions_processed: Mapped[int] = mapped_column(Integer, default=0)
    sessions_passed: Mapped[int] = mapped_column(Integer, default=0)
    sessions_failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="QUEUED", index=True)
    current_stage: Mapped[str] = mapped_column(String, default="QUEUED")
    stage_detail: Mapped[str] = mapped_column(String, default="")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    # Persisted mapping of part_index -> {upload_id, part_name,
    # expected_size} so a backend restart can rebuild the in-memory
    # multipart session from the 5 .part files that survived on disk.
    parts_map: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, index=True)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, index=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)


class AuditLog(Base):
    """Append-only immutable audit trail."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ts: Mapped[str] = mapped_column(String, default=_utcnow_iso, index=True)
    actor: Mapped[str] = mapped_column(String, default="owner")
    event_type: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    qa_run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[Any] = mapped_column(JSON, nullable=True)
