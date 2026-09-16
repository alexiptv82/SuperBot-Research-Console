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

#: Historical raw-reference batch. Used for validator recovery only;
#: these uploads DO NOT contribute to milestone totals (the OLD36
#: baseline stays at ``OLD36_VALIDATED_HOURS``, an immutable
#: constant, regardless of whether the raw ZIPs are present).
CHECKPOINT_OLD36_REFERENCE: str = "OLD36_REFERENCE"

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
# Frozen OLD36 historical raw-reference registry
# ---------------------------------------------------------------------------
#
# These 11 session_ids are the frozen historical dataset that produced
# the CP24 and CP36 checkpoint outputs. They are supplied for VALIDATOR
# RECOVERY purposes only:
#
# - Their raw ZIPs may be uploaded through the normal chunked pipeline.
# - Auto-tagged as ``OLD36_REFERENCE`` on upload.
# - Deterministic operational QA still runs.
# - Content-addressed raw retention + duplicate protection still apply.
# - They DO NOT contribute to milestone totals. OLD36 baseline stays
#   locked at ``OLD36_VALIDATED_HOURS`` (36.0h), regardless of how
#   many historical sessions are physically present.
# - Unknown historical IDs must NOT be inferred into OLD36_REFERENCE.
#
# Per-session nominal durations sum to exactly 36.0h and are used
# ONLY for raw-availability reporting (X of 11 sessions, Y of 36.0h
# nominal). They never affect the 36.0h baseline math.

OLD36_REFERENCE_SESSIONS: tuple[str, ...] = (
    "20260905T073818Z_e44d99bd",  # 6.0h
    "20260906T054317Z_7973d176",  # 3.0h
    "20260906T092548Z_d18fd1e3",  # 3.0h
    "20260906T221530Z_db18dc51",  # 3.0h
    "20260907T070729Z_48d293bf",  # 3.0h
    "20260907T124300Z_6ac966eb",  # 3.0h
    "20260907T160215Z_ee5b0782",  # 3.0h
    "20260907T221751Z_3abdfd02",  # 3.0h
    "20260908T074322Z_1057297f",  # 3.0h
    "20260908T120838Z_3cfee030",  # 3.0h
    "20260909T171030Z_1952d031",  # 3.0h
)

OLD36_REFERENCE_NOMINAL_HOURS: dict[str, float] = {
    "20260905T073818Z_e44d99bd": 6.0,
    "20260906T054317Z_7973d176": 3.0,
    "20260906T092548Z_d18fd1e3": 3.0,
    "20260906T221530Z_db18dc51": 3.0,
    "20260907T070729Z_48d293bf": 3.0,
    "20260907T124300Z_6ac966eb": 3.0,
    "20260907T160215Z_ee5b0782": 3.0,
    "20260907T221751Z_3abdfd02": 3.0,
    "20260908T074322Z_1057297f": 3.0,
    "20260908T120838Z_3cfee030": 3.0,
    "20260909T171030Z_1952d031": 3.0,
}

# Sanity gates (fail fast at import time if the registry is edited wrong).
assert len(OLD36_REFERENCE_SESSIONS) == 11, "OLD36_REFERENCE must contain 11 session_ids"
assert len(set(OLD36_REFERENCE_SESSIONS)) == 11, "OLD36_REFERENCE ids must be unique"
assert set(OLD36_REFERENCE_NOMINAL_HOURS.keys()) == set(OLD36_REFERENCE_SESSIONS), (
    "OLD36_REFERENCE_NOMINAL_HOURS keys must match OLD36_REFERENCE_SESSIONS"
)
assert abs(sum(OLD36_REFERENCE_NOMINAL_HOURS.values()) - OLD36_VALIDATED_HOURS) < 1e-9, (
    "OLD36_REFERENCE nominal hours must sum to OLD36_VALIDATED_HOURS (36.0)"
)
# OLD36_REFERENCE and NEW36 must never overlap (independent dataset identity).
assert set(OLD36_REFERENCE_SESSIONS).isdisjoint(set(NEW36_SESSION_IDS)), (
    "OLD36_REFERENCE and NEW36 registries must be disjoint"
)

_OLD36_REF_SET: frozenset[str] = frozenset(OLD36_REFERENCE_SESSIONS)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def checkpoint_batch_for(session_id: str | None) -> str:
    """Return the frozen checkpoint batch label for a session_id.

    Only exact-match membership decides the label. Never infer from
    timestamps or filenames.

    Precedence (mutually exclusive):
    1. NEW36 — the 12 confirmation-batch ids.
    2. OLD36_REFERENCE — the 11 historical raw-reference ids used for
       validator recovery ONLY (they never contribute milestone hours).
    3. UNASSIGNED — everything else.
    """
    if session_id and session_id in _NEW36_SET:
        return CHECKPOINT_NEW36
    if session_id and session_id in _OLD36_REF_SET:
        return CHECKPOINT_OLD36_REFERENCE
    return CHECKPOINT_UNASSIGNED


def is_new36_registered(session_id: str | None) -> bool:
    return bool(session_id) and session_id in _NEW36_SET


def is_new12_registered(session_id: str | None) -> bool:
    return bool(session_id) and session_id in _NEW12_SET


def is_old36_reference(session_id: str | None) -> bool:
    """True iff ``session_id`` is one of the 11 historical raw-reference
    sessions supplied for validator recovery."""
    return bool(session_id) and session_id in _OLD36_REF_SET


def new36_index(session_id: str | None) -> int | None:
    """1-based position within the frozen NEW36 tuple, or None."""
    if not session_id or session_id not in _NEW36_SET:
        return None
    return NEW36_SESSION_IDS.index(session_id) + 1


def is_valid_milestone_verdict(verdict: str | None) -> bool:
    return verdict in VALID_MILESTONE_VERDICTS
