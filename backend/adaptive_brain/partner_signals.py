from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .memory import BrainMemory
from .schema import ExpertSignal, SourceType


@dataclass(frozen=True)
class PartnerSignal:
    partner_id: str
    symbol: str
    direction: float
    confidence: float
    expected_edge_bps: float = 0.0
    observed_at: str | None = None
    ttl_seconds: int = 120
    metadata: dict[str, Any] | None = None

    def to_expert_signal(self) -> ExpertSignal:
        return ExpertSignal(
            expert_id=self.partner_id.strip(),
            source_type=SourceType.TRADER,
            symbol=self.symbol.strip().upper(),
            direction=max(-1.0, min(1.0, float(self.direction))),
            confidence=max(0.0, min(1.0, float(self.confidence))),
            expected_edge_bps=float(self.expected_edge_bps),
            observed_at=self.observed_at or datetime.now(timezone.utc).isoformat(),
            ttl_seconds=max(1, int(self.ttl_seconds)),
            metadata=dict(self.metadata or {}),
        )


class PartnerSignalStore:
    """Persistent normalized partner/trader signals with TTL filtering."""

    def __init__(self, memory: BrainMemory):
        self.memory = memory

    def add(self, signal: PartnerSignal) -> dict[str, Any]:
        expert = signal.to_expert_signal()
        self.memory.add_external_signal(expert)
        self.memory.touch_source(
            expert.expert_id,
            expert.source_type.value,
        )
        return expert.to_dict()

    def fresh_for_symbol(self, symbol: str) -> list[ExpertSignal]:
        rows = self.memory.fresh_external_signals(
            symbol=symbol.upper(),
            source_type=SourceType.TRADER.value,
        )
        return [
            ExpertSignal(
                expert_id=row["expert_id"],
                source_type=SourceType(row["source_type"]),
                symbol=row["symbol"],
                direction=float(row["direction"]),
                confidence=float(row["confidence"]),
                expected_edge_bps=float(row["expected_edge_bps"]),
                observed_at=row["observed_at"],
                ttl_seconds=int(row["ttl_seconds"]),
                metadata=dict(row["metadata"]),
            )
            for row in rows
        ]
