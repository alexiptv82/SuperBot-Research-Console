from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceType(str, Enum):
    MARKET = "market"
    NEWS = "news"
    MODEL = "model"
    TRADER = "trader"
    RESEARCH = "research"
    ONCHAIN = "onchain"
    INTERNAL = "internal"


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class BrainMode(str, Enum):
    PAPER = "PAPER"
    RESEARCH = "RESEARCH"
    LIVE = "LIVE"


class MarketRegime(str, Enum):
    INSUFFICIENT = "INSUFFICIENT"
    RANGE = "RANGE"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    HIGH_VOL = "HIGH_VOL"


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
class EvidenceItem:
    source_id: str
    source_type: SourceType
    topic: str
    title: str
    body: str = ""
    url: str = ""
    published_at: str | None = None
    observed_at: str = field(default_factory=utcnow_iso)
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketTick:
    venue: str
    category: str
    symbol: str
    last_price: float
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    venue_ts_ms: int | None = None
    observed_at: str = field(default_factory=utcnow_iso)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last_price

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0 or self.bid <= 0 or self.ask <= 0:
            return 0.0
        return (self.ask - self.bid) / mid * 10000.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mid"] = self.mid
        d["spread_bps"] = self.spread_bps
        return d


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
        d = asdict(self.normalized())
        d["source_type"] = self.source_type.value
        return d


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
        d = asdict(self)
        d["action"] = self.action.value
        d["mode"] = self.mode.value
        return d
