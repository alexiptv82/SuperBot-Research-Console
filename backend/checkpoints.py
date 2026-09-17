"""Checkpoint accounting for OLD36 / NEW12 / TOTAL48 / NEW36 / TOTAL72.

Milestone math is deterministic and driven by the frozen NEW36
registry (see ``checkpoint_registry.py``):

- OLD36 is a project-level immutable constant (36.0h). It is NOT
  derived from live rows.
- NEW12 is the FIXED first-four subset of the NEW36 registered list.
  A later session cannot substitute for a failed slot.
- NEW36 is the full 12-entry registered list.
- A registered session contributes ``SESSION_NOMINAL_HOURS`` (3.0h)
  to milestone totals if any of its QA runs has a valid milestone
  verdict (PASS or PASS_WITH_WARNING). Duplicates never double-count
  because a session_id can only appear once in the registered list.
- ``wrapper_elapsed_hours`` (e.g. 3.001899) is kept as telemetry and
  never distorts milestone totals.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from checkpoint_registry import (
    NEW12_SESSION_IDS,
    NEW36_SESSION_IDS,
    OLD36_REFERENCE_NOMINAL_HOURS,
    OLD36_REFERENCE_SESSIONS,
    OLD36_VALIDATED_HOURS,
    SESSION_NOMINAL_HOURS,
    is_valid_milestone_verdict,
    new36_index,
)
from constants import (
    CHECKPOINT_NEW12,
    CHECKPOINT_NEW36,
    CHECKPOINT_OLD36,
    CHECKPOINT_TARGETS,
    CHECKPOINT_TOTAL48,
    CHECKPOINT_TOTAL72,
)
from models import QARun, Session as SessionModel


def _latest_valid_run(db: OrmSession, session_id: str) -> QARun | None:
    """Return the most recent QA run for ``session_id`` with a valid verdict."""
    stmt = (
        select(QARun)
        .where(QARun.session_id == session_id)
        .order_by(QARun.uploaded_at.desc())
    )
    for run in db.execute(stmt).scalars().all():
        if is_valid_milestone_verdict(run.operational_status):
            return run
    return None


def _session_row(db: OrmSession, session_id: str) -> SessionModel | None:
    return db.execute(
        select(SessionModel).where(SessionModel.session_id == session_id)
    ).scalars().first()


def _summarize_registered(db: OrmSession, ids: tuple[str, ...]) -> dict:
    """Compute nominal milestone hours + per-session status for a slot list."""
    details: list[dict] = []
    valid = 0
    telemetry_hours = 0.0
    for sid in ids:
        row = _session_row(db, sid)
        run = _latest_valid_run(db, sid) if row is not None else None
        is_valid = run is not None
        if is_valid:
            valid += 1
            if run.duration_hours is not None:
                telemetry_hours += float(run.duration_hours)
        latest_run = None
        if row is not None:
            latest_stmt = (
                select(QARun)
                .where(QARun.session_id == sid)
                .order_by(QARun.uploaded_at.desc())
            )
            latest_run = db.execute(latest_stmt).scalars().first()
        details.append(
            {
                "session_id": sid,
                "registered_index": new36_index(sid),
                "present": row is not None,
                "latest_verdict": (
                    latest_run.operational_status if latest_run is not None else None
                ),
                "milestone_valid": is_valid,
                "nominal_hours": SESSION_NOMINAL_HOURS if is_valid else 0.0,
                "wrapper_elapsed_hours": (
                    float(run.duration_hours)
                    if is_valid and run.duration_hours is not None
                    else None
                ),
            }
        )
    return {
        "expected_slots": len(ids),
        "valid_slots": valid,
        "nominal_hours": round(valid * SESSION_NOMINAL_HOURS, 4),
        "telemetry_wrapper_hours": round(telemetry_hours, 6),
        "sessions": details,
    }


def _summarize_old36_reference(db: OrmSession) -> dict:
    """Report which of the 11 historical raw-reference sessions are
    present in SuperBot storage AND whether the canonical raw
    payload actually exists on disk.

    IMPORTANT: this NEVER affects milestone totals. OLD36 baseline
    stays locked at ``OLD36_VALIDATED_HOURS``. These figures are
    purely raw-availability telemetry used to gate the validator
    recovery workflow.

    The distinction between ``present`` (session row exists in
    metadata) and ``raw_retained`` (canonical SHA-addressed ZIP is
    both marked ``retained=True`` and readable on disk) is critical:
    a session can be metadata-present with the raw payload silently
    lost by an EXDEV / ENOSPC bug in a previous import. Quantitative
    recovery requires ``raw_retained``, not ``present``.
    """
    from pathlib import Path

    from models import RawFile  # local import to avoid cycles

    details: list[dict] = []
    present = 0
    raw_retained_count = 0
    hours_present = 0.0
    hours_raw_retained = 0.0
    for sid in OLD36_REFERENCE_SESSIONS:
        row = _session_row(db, sid)
        latest_run = None
        if row is not None:
            latest_run = db.execute(
                select(QARun)
                .where(QARun.session_id == sid)
                .order_by(QARun.uploaded_at.desc())
            ).scalars().first()
        # A session's raw payload is only truly usable for the
        # recovery pipeline when SOME qa_run for this session has a
        # ``RawFile(retained=True)`` whose stored_path resolves on
        # disk. We iterate ALL qa_runs (not just the latest) so a
        # partial import that landed retention on an earlier attempt
        # is still counted as recoverable.
        raw_path: str | None = None
        raw_retained = False
        if row is not None:
            raw_rows = db.execute(
                select(RawFile, QARun)
                .join(QARun, QARun.id == RawFile.qa_run_id)
                .where(QARun.session_id == sid)
                .where(RawFile.retained == True)  # noqa: E712
            ).all()
            for rf, _qa in raw_rows:
                if rf.stored_path and Path(rf.stored_path).exists():
                    raw_path = rf.stored_path
                    raw_retained = True
                    break
        nominal = OLD36_REFERENCE_NOMINAL_HOURS[sid]
        is_present = row is not None
        if is_present:
            present += 1
            hours_present += nominal
        if raw_retained:
            raw_retained_count += 1
            hours_raw_retained += nominal
        details.append(
            {
                "session_id": sid,
                "nominal_hours": nominal,
                "present": is_present,
                "raw_retained": raw_retained,
                "raw_path": raw_path,
                "latest_verdict": (
                    latest_run.operational_status if latest_run is not None else None
                ),
                "checkpoint_hint": row.checkpoint_hint if row is not None else None,
            }
        )
    return {
        "expected_sessions": len(OLD36_REFERENCE_SESSIONS),
        "present_sessions": present,
        "raw_retained_sessions": raw_retained_count,
        "expected_nominal_hours": OLD36_VALIDATED_HOURS,
        "present_nominal_hours": round(hours_present, 4),
        "raw_retained_nominal_hours": round(hours_raw_retained, 4),
        "raw_ready": raw_retained_count == len(OLD36_REFERENCE_SESSIONS),
        "milestone_impact_hours": 0.0,
        "note": (
            "OLD36 baseline stays fixed at OLD36_VALIDATED_HOURS (36.0h) "
            "regardless of how many raw-reference sessions are physically "
            "imported. `present_sessions` counts session metadata; "
            "`raw_retained_sessions` counts sessions whose canonical raw "
            "ZIP actually exists on disk. Only `raw_retained_sessions` is "
            "usable for validator-recovery."
        ),
        "sessions": details,
    }


def compute_checkpoints(db: OrmSession) -> dict:
    """Return the full milestone / checkpoint report.

    Numbers reported for OLD36 / NEW12 / NEW36 / TOTAL48 / TOTAL72 are
    all in NOMINAL hours. Wrapper telemetry is exposed alongside for
    transparency but never affects milestone completion.
    """
    new12_summary = _summarize_registered(db, NEW12_SESSION_IDS)
    new36_summary = _summarize_registered(db, NEW36_SESSION_IDS)

    old36 = OLD36_VALIDATED_HOURS
    new12 = new12_summary["nominal_hours"]
    new36 = new36_summary["nominal_hours"]
    total48 = old36 + new12
    total72 = old36 + new36

    ready = total72 >= CHECKPOINT_TARGETS[CHECKPOINT_TOTAL72]

    return {
        "checkpoints": {
            CHECKPOINT_OLD36: {
                "hours": round(old36, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_OLD36],
                "source": "project_baseline_constant",
            },
            CHECKPOINT_NEW12: {
                "hours": round(new12, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_NEW12],
                "valid_slots": new12_summary["valid_slots"],
                "expected_slots": new12_summary["expected_slots"],
                "telemetry_wrapper_hours": new12_summary["telemetry_wrapper_hours"],
            },
            CHECKPOINT_TOTAL48: {
                "hours": round(total48, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_TOTAL48],
                "formula": "OLD36 + NEW12",
            },
            CHECKPOINT_NEW36: {
                "hours": round(new36, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_NEW36],
                "valid_slots": new36_summary["valid_slots"],
                "expected_slots": new36_summary["expected_slots"],
                "telemetry_wrapper_hours": new36_summary["telemetry_wrapper_hours"],
            },
            CHECKPOINT_TOTAL72: {
                "hours": round(total72, 4),
                "target": CHECKPOINT_TARGETS[CHECKPOINT_TOTAL72],
                "formula": "OLD36 + NEW36",
            },
        },
        "registered_batch": {
            "NEW12": new12_summary["sessions"],
            "NEW36": new36_summary["sessions"],
        },
        "old36_reference": _summarize_old36_reference(db),
        "data_qa_ready": ready,
        "note": (
            "Milestone hours are NOMINAL (3.0h per registered confirmation "
            "session). wrapper_elapsed_hours is telemetry only. "
            "72H_DATA_QA_READY reflects operational/data QA only. "
            "CONFIRMED_EXECUTION_STRUCTURE is not declarable until the "
            "frozen quantitative validator (FrozenAnalysisEngine) is delivered."
        ),
    }
