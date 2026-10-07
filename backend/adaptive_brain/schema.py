from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class SourceType(str, Enum):
    MARKET="market"; NEWS="news"; MODEL="model"; TRADER="trader"; RESEARCH="research"; ONCHAIN="onchain"; INTERNAL="internal"

class Action(str, Enum):
    LONG="LONG"; SHORT="SHORT"; FLAT="FLAT"

class BrainMode(str, Enum):
    PAPER="PAPER"; RESEARCH="RESEARCH"; LIVE="LIVE"

@dataclass(frozen=True)
class Observation:
    source_id: str
    source_type: SourceType
    symbol: str
    feature: str
    value: float
    confidence: float
    observed_at: str = field(default_factory=utcnow_iso)
    ttl_seconds: int = 300
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ExpertSignal:
    expert_id: str
    source_type: SourceType
    symbol: str
    direction: float
    confidence: float
    expected_edge_bps: float = 0.0
    observed_at: str = field(default_factory=utcnow_iso)
    ttl_seconds: int = 120
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> "ExpertSignal":
        return ExpertSignal(
            expert_id=self.expert_id.strip(),
            source_type=self.source_type,
            symbol=self.symbol.strip().upper(),
            direction=max(-1.0, min(1.0, float(self.direction))),
            confidence=max(0.0, min(1.0, float(self.confidence))),
            expected_edge_bps=float(self.expected_edge_bps),
            observed_at=self.observed_at,
            ttl_seconds=max(1, int(self.ttl_seconds)),
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        d=asdict(self.normalized()); d["source_type"]=self.source_type.value; return d

@dataclass(frozen=True)
class PortfolioState:
    equity: float = 1.0
    daily_pnl_fraction: float = 0.0
    drawdown_fraction: float = 0.0
    gross_exposure_fraction: float = 0.0
    symbol_exposure_fraction: float = 0.0

@dataclass(frozen=True)
class BrainDecision:
    decision_id: str
    created_at: str
    symbol: str
    action: Action
    confidence: float
    raw_score: float
    risk_budget_fraction: float
    mode: BrainMode
    rationale: list[str]
    expert_snapshot: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        d=asdict(self); d["action"]=self.action.value; d["mode"]=self.mode.value; return d
