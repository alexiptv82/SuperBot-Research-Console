"""Blessed raw-ZIP access for the recovery workflow.

Every raw-ZIP read during Phase 2 recovery MUST go through
``open_reference_zip`` (or the module-level ``iter_reference_sessions``
helper). Attempts to open NEW36 or unknown sessions raise
``NEW36QuantitativeFirewallError``.

This module never returns quantitative NEW36 data. Operational QA
metadata about NEW36 remains visible through the main runtime API
(that is not this module).
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import select

from database import SessionLocal
from models import QARun, RawFile, Session as SessionModel

from .allowlist import (
    NEW36QuantitativeFirewallError,
    allowed_session_ids,
    assert_recovery_allowed,
)


@dataclass
class ReferenceSessionHandle:
    session_id: str
    stored_path: Path
    nominal_hours: float
    operational_verdict: str | None


def _resolve_stored_path(session_id: str) -> Path | None:
    """Return the canonical retained raw-ZIP path for ``session_id``
    or None if the raw file is not yet imported."""
    with SessionLocal() as db:
        row = db.execute(
            select(SessionModel).where(SessionModel.session_id == session_id)
        ).scalars().first()
        if row is None or row.current_qa_run_id is None:
            return None
        run = db.get(QARun, row.current_qa_run_id)
        if run is None:
            return None
        rf = db.execute(
            select(RawFile).where(RawFile.qa_run_id == run.id)
        ).scalars().first()
        if rf is None or not rf.retained or not rf.stored_path:
            return None
        p = Path(rf.stored_path)
        return p if p.exists() else None


def reference_session_status(session_id: str) -> ReferenceSessionHandle | None:
    """Metadata for one OLD36_REFERENCE session (path may be None if
    not yet imported). Raises for non-allowlisted ids.
    """
    assert_recovery_allowed(session_id)
    from checkpoint_registry import OLD36_REFERENCE_NOMINAL_HOURS

    stored = _resolve_stored_path(session_id)
    verdict = None
    with SessionLocal() as db:
        row = db.execute(
            select(SessionModel).where(SessionModel.session_id == session_id)
        ).scalars().first()
        if row is not None and row.current_qa_run_id:
            run = db.get(QARun, row.current_qa_run_id)
            verdict = run.operational_status if run is not None else None
    if stored is None:
        return None
    return ReferenceSessionHandle(
        session_id=session_id,
        stored_path=stored,
        nominal_hours=OLD36_REFERENCE_NOMINAL_HOURS[session_id],
        operational_verdict=verdict,
    )


def open_reference_zip(session_id: str) -> zipfile.ZipFile:
    """Open the retained raw ZIP for an OLD36_REFERENCE session.

    Raises ``NEW36QuantitativeFirewallError`` for any non-allowlisted
    id (including every NEW36 session). Raises ``FileNotFoundError``
    if the id is allowlisted but has not been imported yet.
    """
    assert_recovery_allowed(session_id)
    stored = _resolve_stored_path(session_id)
    if stored is None:
        raise FileNotFoundError(
            f"OLD36_REFERENCE session {session_id} not yet imported "
            "(no retained raw ZIP found in SuperBot storage)."
        )
    return zipfile.ZipFile(str(stored), "r")


def iter_reference_sessions() -> Iterator[ReferenceSessionHandle]:
    """Yield handles for every currently-imported OLD36 reference
    session, in the frozen registry order. Missing sessions are
    skipped (not surfaced as errors) so the caller can report
    availability without crashing.
    """
    for sid in allowed_session_ids():
        h = reference_session_status(sid)
        if h is not None:
            yield h


def availability_snapshot() -> dict:
    """Return a compact summary of the raw-reference set."""
    from checkpoint_registry import OLD36_REFERENCE_NOMINAL_HOURS

    ids = allowed_session_ids()
    handles = {h.session_id: h for h in iter_reference_sessions()}
    return {
        "expected_sessions": len(ids),
        "present_sessions": len(handles),
        "missing_sessions": [sid for sid in ids if sid not in handles],
        "present_nominal_hours": round(
            sum(h.nominal_hours for h in handles.values()), 4
        ),
        "expected_nominal_hours": round(
            sum(OLD36_REFERENCE_NOMINAL_HOURS[s] for s in ids), 4
        ),
    }


__all__ = [
    "ReferenceSessionHandle",
    "reference_session_status",
    "open_reference_zip",
    "iter_reference_sessions",
    "availability_snapshot",
    "NEW36QuantitativeFirewallError",
]
