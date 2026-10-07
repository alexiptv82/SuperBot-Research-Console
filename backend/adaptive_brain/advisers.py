from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from .schema import ExpertSignal, SourceType


@dataclass(frozen=True)
class AdviserContext:
    symbol: str
    regime: str | None
    market: dict[str, Any]
    news_policy: dict[str, Any]
    features: dict[str, Any]


class ModelAdviser(Protocol):
    adviser_id: str
    version: str

    async def advise(self, context: AdviserContext) -> ExpertSignal | None:
        ...


class AdviserHub:
    """In-process adviser registry.

    v0.4 intentionally ships no vendor-specific LLM client and no secrets.
    External model adapters can be registered later behind this interface.
    """

    def __init__(self):
        self._advisers: dict[str, ModelAdviser] = {}

    def register(self, adviser: ModelAdviser) -> None:
        key = f"{adviser.adviser_id}@{adviser.version}"
        self._advisers[key] = adviser

    def list(self) -> list[str]:
        return sorted(self._advisers)

    async def collect(self, context: AdviserContext) -> list[ExpertSignal]:
        out: list[ExpertSignal] = []
        for key in sorted(self._advisers):
            signal = await self._advisers[key].advise(context)
            if signal is None:
                continue
            out.append(signal.normalized())
        return out


def opinion_to_signal(
    *,
    adviser_id: str,
    version: str,
    symbol: str,
    direction: float,
    confidence: float,
    expected_edge_bps: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> ExpertSignal:
    md = dict(metadata or {})
    md["adviser_id"] = adviser_id
    md["adviser_version"] = version
    return ExpertSignal(
        expert_id=f"{adviser_id}@{version}",
        source_type=SourceType.MODEL,
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        expected_edge_bps=expected_edge_bps,
        metadata=md,
    )
