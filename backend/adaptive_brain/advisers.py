from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

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
    """Fault-isolated adviser registry.

    Advisers run concurrently. One slow/failing adviser must not block the
    entire decision loop.
    """

    def __init__(self, per_adviser_timeout_seconds: float = 20.0):
        self._advisers: dict[str, ModelAdviser] = {}
        self.per_adviser_timeout_seconds = per_adviser_timeout_seconds
        self._last_errors: dict[str, str] = {}

    def register(self, adviser: ModelAdviser) -> None:
        key = f"{adviser.adviser_id}@{adviser.version}"
        self._advisers[key] = adviser

    def list(self) -> list[str]:
        return sorted(self._advisers)

    def status(self) -> dict[str, Any]:
        return {
            "registered": self.list(),
            "last_errors": dict(self._last_errors),
            "per_adviser_timeout_seconds": self.per_adviser_timeout_seconds,
        }

    async def _one(
        self,
        key: str,
        adviser: ModelAdviser,
        context: AdviserContext,
    ) -> ExpertSignal | None:
        try:
            signal = await asyncio.wait_for(
                adviser.advise(context),
                timeout=self.per_adviser_timeout_seconds,
            )
            self._last_errors.pop(key, None)
            return signal.normalized() if signal is not None else None
        except Exception as exc:
            self._last_errors[key] = f"{type(exc).__name__}: {exc}"
            return None

    async def collect(self, context: AdviserContext) -> list[ExpertSignal]:
        keys = sorted(self._advisers)
        rows = await asyncio.gather(*[
            self._one(key, self._advisers[key], context)
            for key in keys
        ])
        return [row for row in rows if row is not None]


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
