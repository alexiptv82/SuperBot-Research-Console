"""Duplicate detection and validated-hours logic."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from constants import (
    DUP_CONFLICT,
    DUP_EXACT_DUPLICATE,
    DUP_NEW,
    DUP_SAME_SESSION_DIFFERENT_FILE,
    SESSION_HOURS,
    VERDICT_PASS,
    VERDICT_PASS_WITH_WARNING,
)
from models import QARun, Session as SessionModel


def classify_upload(
    db: OrmSession, session_id: str | None, file_sha256: str
) -> str:
    """Return one of the four duplicate states per §12.4.

    - EXACT_DUPLICATE: same file SHA256 already seen (may or may not match session_id).
    - SAME_SESSION_DIFFERENT_FILE: same session_id already seen with a different SHA256.
    - CONFLICT: different session_id under an existing SHA256 (should not happen with SHA256 uniqueness, but caught defensively).
    - NEW: neither is registered yet.
    """
    existing_hash = db.execute(
        select(QARun).where(QARun.source_file_sha256 == file_sha256)
    ).scalars().first()

    if existing_hash is not None:
        if session_id and existing_hash.session_id and existing_hash.session_id != session_id:
            return DUP_CONFLICT
        return DUP_EXACT_DUPLICATE

    if session_id:
        same_sess = db.execute(
            select(QARun).where(QARun.session_id == session_id)
        ).scalars().first()
        if same_sess is not None:
            return DUP_SAME_SESSION_DIFFERENT_FILE

    return DUP_NEW


def compute_validated_hours(
    verdict: str, duplicate_status: str, duration_hours: float | None
) -> float:
    """Compute validated_hours contribution for this QA run.

    Rules (§12.4 + §12.6):
    - Duplicates never add hours twice.
    - Only PASS and PASS_WITH_WARNING contribute.
    - Default hours per session = SESSION_HOURS (3.0). If the manifest
      reports a duration_hours we prefer that.
    """
    if duplicate_status in (DUP_EXACT_DUPLICATE, DUP_SAME_SESSION_DIFFERENT_FILE, DUP_CONFLICT):
        return 0.0
    if verdict not in (VERDICT_PASS, VERDICT_PASS_WITH_WARNING):
        return 0.0
    if duration_hours and duration_hours > 0:
        return float(duration_hours)
    return SESSION_HOURS


def session_total_validated_hours(db: OrmSession, session_id: str) -> float:
    """Validated hours contributed by a session, computed consistently
    across reprocessing events (\u00a712.4: duplicates never add hours twice).

    Rule:
    - If any run for this session_id is PASS or PASS_WITH_WARNING, credit
      the session with that run's ``duration_hours`` (or SESSION_HOURS if
      the manifest didn't expose duration). This is intentionally
      idempotent: re-running QA on the retained raw ZIP after a parser
      fix upgrades the session's hours the moment a passing run exists,
      without ever double-counting.
    - Otherwise return 0.
    """
    from constants import SESSION_HOURS as _SESSION_HOURS

    runs = db.execute(
        select(QARun.operational_status, QARun.duration_hours, QARun.validated_hours)
        .where(QARun.session_id == session_id)
        .order_by(QARun.uploaded_at.desc())
    ).all()
    for status, duration, hours in runs:
        if status in (VERDICT_PASS, VERDICT_PASS_WITH_WARNING):
            if hours and hours > 0:
                return float(hours)
            if duration and duration > 0:
                return float(duration)
            return float(_SESSION_HOURS)
    return 0.0
