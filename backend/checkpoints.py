"""Checkpoint accounting for OLD36 / NEW12 / TOTAL48 / NEW36 / TOTAL72."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from constants import (
    CHECKPOINT_NEW12,
    CHECKPOINT_NEW36,
    CHECKPOINT_OLD36,
    CHECKPOINT_TARGETS,
    CHECKPOINT_TOTAL48,
    CHECKPOINT_TOTAL72,
)
from dedup import session_total_validated_hours
from models import Session as SessionModel


def _hours_for(db: OrmSession, checkpoint_hint: str | None) -> float:
    """Total validated hours for sessions tagged with a given checkpoint hint."""
    stmt = select(SessionModel).where(SessionModel.checkpoint_hint == checkpoint_hint)
    total = 0.0
    for s in db.execute(stmt).scalars().all():
        total += session_total_validated_hours(db, s.session_id)
    return total


def compute_checkpoints(db: OrmSession) -> dict:
    old36 = _hours_for(db, CHECKPOINT_OLD36)
    new12 = _hours_for(db, CHECKPOINT_NEW12)
    # NEW36 is the total of NEW12 + NEW36-only-labeled sessions (the handoff
    # treats NEW36 as the union of the first 12h + the additional 24h).
    # We report NEW36 as the sum of everything labeled NEW12 or NEW36.
    new36_only = _hours_for(db, CHECKPOINT_NEW36)
    new36 = new12 + new36_only
    total48 = old36 + new12
    total72 = old36 + new36
    ready = total72 >= CHECKPOINT_TARGETS[CHECKPOINT_TOTAL72]

    return {
        "checkpoints": {
            CHECKPOINT_OLD36: {
                "hours": round(old36, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_OLD36],
            },
            CHECKPOINT_NEW12: {
                "hours": round(new12, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_NEW12],
            },
            CHECKPOINT_TOTAL48: {
                "hours": round(total48, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_TOTAL48],
            },
            CHECKPOINT_NEW36: {
                "hours": round(new36, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_NEW36],
            },
            CHECKPOINT_TOTAL72: {
                "hours": round(total72, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_TOTAL72],
            },
        },
        "data_qa_ready": ready,
        "note": (
            "72H_DATA_QA_READY reflects operational/data QA only. "
            "CONFIRMED_EXECUTION_STRUCTURE is not declarable until the frozen "
            "quantitative validator (FrozenAnalysisEngine) is delivered."
        ),
    }
