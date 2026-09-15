"""Frozen NEW36 confirmation-batch registry.

This module encodes the *explicit* project registry that binds specific
session IDs to milestone checkpoints. It is intentionally NOT a heuristic:
no timestamp parsing, no filename inference. Only exact-match membership
against the frozen list decides whether an uploaded session participates
in NEW12 / NEW36 milestone accounting.

Rules (per project directive):

- ``OLD36`` is a historical validated baseline of 36.0h. The OLD36 raw
  sessions are NOT being re-imported into V1; OLD36 is a project
  constant.
- ``NEW12`` is the FIXED first-four subset of the NEW36 list (order
  preserved from the handoff). A later session must never substitute
  for a failed/unresolved slot.
- ``NEW36`` is all 12 registered confirmation sessions.
- Any unknown session_id -> ``UNASSIGNED``.
- Milestone completion uses the nominal ``SESSION_NOMINAL_HOURS`` (3.0h)
  per valid registered session, not ``wrapper_elapsed_hours``. The
  wrapper telemetry is retained separately (see ``QARun.duration_hours``
  / ``validated_hours``) and never distorts milestone totals.
"""
from __future__ import annotations

from constants import (
    CHECKPOINT_NEW12,
    CHECKPOINT_NEW36,
    VERDICT_PASS,
    VERDICT_PASS_WITH_WARNING,
)

# ---------------------------------------------------------------------------
# Immutable project constants
# ---------------------------------------------------------------------------

#: Historical validated baseline. Immutable. Do NOT derive from live rows.
OLD36_VALIDATED_HOURS: float = 36.0

#: Nominal duration of every registered confirmation session (§12.1 "3H").
SESSION_NOMINAL_HOURS: float = 3.0

#: Sentinel checkpoint label used for any session that is not in the
#: registered NEW36 batch. Auto-tagged on upload; never inferred.
CHECKPOINT_UNASSIGNED: str = "UNASSIGNED"

#: Verdicts that count toward milestone completion (§12.4 hour rules).
VALID_MILESTONE_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_PASS, VERDICT_PASS_WITH_WARNING}
)

# ---------------------------------------------------------------------------
# Frozen NEW36 confirmation batch (ORDER MATTERS - NEW12 is the first 4)
# ---------------------------------------------------------------------------

#: Ordered NEW36 registry. NEW12 is derived as the first four entries.
NEW36_SESSION_IDS: tuple[str, ...] = (
    "20260910T123759Z_5ab0c1b2",
    "20260911T040213Z_43a798b7",
    "20260911T081446Z_28b5a901",
    "20260911T113249Z_276d8c3b",
    "20260911T145009Z_37923a7a",
    "20260911T220737Z_5d45563e",
    "20260912T072730Z_adb96088",
    "20260912T163326Z_16b9ca74",
    "20260912T213151Z_9f9a0088",
    "20260914T050815Z_3b68aabf",
    "20260914T133522Z_6141ec8b",
    "20260914T192945Z_1b899057",
)

#: Fixed first-four NEW12 subset. NOT "any first 4 that pass".
NEW12_SESSION_IDS: tuple[str, ...] = NEW36_SESSION_IDS[:4]

# Guardrails: the file is frozen. If someone edits the tuple wrong,
# fail loud at import time rather than silently miscount milestones.
assert len(NEW36_SESSION_IDS) == 12, "NEW36 must contain exactly 12 session_ids"
assert len(set(NEW36_SESSION_IDS)) == 12, "NEW36 must contain unique session_ids"
assert len(NEW12_SESSION_IDS) == 4, "NEW12 must contain exactly 4 session_ids"
assert set(NEW12_SESSION_IDS).issubset(set(NEW36_SESSION_IDS)), (
    "NEW12 must be a strict subset of NEW36"
)

# Fast membership set for auto-tagging.
_NEW36_SET: frozenset[str] = frozenset(NEW36_SESSION_IDS)
_NEW12_SET: frozenset[str] = frozenset(NEW12_SESSION_IDS)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def checkpoint_batch_for(session_id: str | None) -> str:
    """Return the frozen checkpoint batch label for a session_id.

    Only exact-match membership decides the label. Never infer from
    timestamps or filenames.
    """
    if session_id and session_id in _NEW36_SET:
        return CHECKPOINT_NEW36
    return CHECKPOINT_UNASSIGNED


def is_new36_registered(session_id: str | None) -> bool:
    return bool(session_id) and session_id in _NEW36_SET


def is_new12_registered(session_id: str | None) -> bool:
    return bool(session_id) and session_id in _NEW12_SET


def new36_index(session_id: str | None) -> int | None:
    """1-based position within the frozen NEW36 tuple, or None."""
    if not session_id or session_id not in _NEW36_SET:
        return None
    return NEW36_SESSION_IDS.index(session_id) + 1


def is_valid_milestone_verdict(verdict: str | None) -> bool:
    return verdict in VALID_MILESTONE_VERDICTS
