"""Recovery sandbox allowlist.

Only ids in OLD36_REFERENCE_SESSIONS are permitted for quantitative
raw-ZIP access during validator recovery. Any NEW36 id, unknown id,
or None input raises ``NEW36QuantitativeFirewallError`` (subclass
of ``PermissionError`` so callers cannot silently bypass it).
"""
from __future__ import annotations

from checkpoint_registry import (
    OLD36_REFERENCE_SESSIONS,
    is_new36_registered,
    is_old36_reference,
)

_OLD36_REF_SET = frozenset(OLD36_REFERENCE_SESSIONS)


class NEW36QuantitativeFirewallError(PermissionError):
    """Raised when the recovery sandbox is asked to open a raw ZIP
    that is not in the OLD36_REFERENCE allowlist.

    This is a hard firewall: NEW36 must remain quantitatively sealed
    while the historical validator is being recovered.
    """


def assert_recovery_allowed(session_id: str | None) -> None:
    """Raise if ``session_id`` cannot be read quantitatively during
    recovery. Returns None on success (allowlisted OLD36 reference)."""
    if session_id is None:
        raise NEW36QuantitativeFirewallError(
            "recovery sandbox rejects None session_id"
        )
    if is_new36_registered(session_id):
        raise NEW36QuantitativeFirewallError(
            f"recovery sandbox refuses NEW36 session_id {session_id!r}: "
            "NEW36 must remain quantitatively sealed during recovery."
        )
    if not is_old36_reference(session_id):
        raise NEW36QuantitativeFirewallError(
            f"recovery sandbox rejects unknown session_id {session_id!r}: "
            "only OLD36_REFERENCE sessions may be opened for recovery."
        )
    # Belt + suspenders: cross-check the frozen set directly.
    if session_id not in _OLD36_REF_SET:  # pragma: no cover - defensive
        raise NEW36QuantitativeFirewallError(
            f"session_id {session_id!r} not in OLD36_REFERENCE_SESSIONS"
        )


def allowed_session_ids() -> tuple[str, ...]:
    """Frozen ordered tuple of session_ids the sandbox may open."""
    return OLD36_REFERENCE_SESSIONS
