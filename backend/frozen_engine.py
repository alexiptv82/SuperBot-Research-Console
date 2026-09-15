"""FrozenAnalysisEngine — isolated V1 stub.

Per §12.8 of the handoff: this module MUST NOT approximate signal formulas
from prose. In V1 it stays ``NOT_CONFIGURED`` and refuses to produce any
quantitative verdict. Its shape lives here so the integration point is
ready when the frozen validator is delivered.
"""
from __future__ import annotations

from dataclasses import dataclass

from constants import ENGINE_NOT_CONFIGURED


@dataclass
class EngineStatus:
    status: str
    reason: str
    accepts_input: bool


def current_status() -> EngineStatus:
    return EngineStatus(
        status=ENGINE_NOT_CONFIGURED,
        reason=(
            "FrozenAnalysisEngine is intentionally NOT_CONFIGURED in Research "
            "Console V1. It must never approximate signal definitions, "
            "composites, weights, quantiles or the 15 bps hurdle from prose. "
            "Wire this module only when the exact frozen quantitative validator "
            "is delivered."
        ),
        accepts_input=False,
    )


def run(payload: object) -> dict:  # pragma: no cover - explicit refusal
    raise RuntimeError(
        "FrozenAnalysisEngine.run() is disabled in V1: NOT_CONFIGURED."
    )
