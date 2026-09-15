"""Audit log helper."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session as OrmSession

from models import AuditLog


def log_event(
    db: OrmSession,
    event_type: str,
    message: str = "",
    session_id: str | None = None,
    qa_run_id: str | None = None,
    outcome: str | None = None,
    payload: Any = None,
    actor: str = "owner",
) -> AuditLog:
    entry = AuditLog(
        event_type=event_type,
        message=message,
        session_id=session_id,
        qa_run_id=qa_run_id,
        outcome=outcome,
        payload=payload,
        actor=actor,
    )
    db.add(entry)
    db.flush()
    return entry
