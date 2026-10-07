from __future__ import annotations

import asyncio
from pathlib import Path

from adaptive_brain.brain import AdaptiveBrain, BrainConfig
from adaptive_brain.memory import BrainMemory
from adaptive_brain.news_intelligence import NewsIntelligenceEngine
from adaptive_brain.paper import PaperPortfolioSimulator
from adaptive_brain.research_bridge import ResearchBridge
from adaptive_brain.schema import (
    Action,
    BrainMode,
    EvidenceItem,
    ExpertSignal,
    MarketTick,
    SourceType,
)
from adaptive_brain.service import BrainRuntimeService, RuntimeConfig


def make_brain(tmp_path: Path) -> AdaptiveBrain:
    backend_root = tmp_path / "backend"
    (backend_root / "recovery" / "reports" / "v1_2").mkdir(parents=True)
    return AdaptiveBrain(
        BrainMemory(tmp_path / "brain.db"),
        ResearchBridge(backend_root),
        BrainConfig(mode=BrainMode.PAPER, min_experts=2, decision_threshold=0.18),
    )


def sig(name: str, direction: float) -> ExpertSignal:
    return ExpertSignal(
        expert_id=name,
        source_type=SourceType.MODEL,
        symbol="BTCUSDT",
        direction=direction,
        confidence=0.9,
        expected_edge_bps=20.0,
    )


def tick(price: float = 100.0) -> MarketTick:
    return MarketTick(
        venue="bitget",
        category="usdt-futures",
        symbol="BTCUSDT",
        last_price=price,
        bid=price - 0.05,
        ask=price + 0.05,
    )


def test_positive_asset_news_creates_long_bias(tmp_path: Path):
    brain = make_brain(tmp_path)
    engine = NewsIntelligenceEngine(brain.memory, ("BTCUSDT",))
    item = EvidenceItem(
        source_id="news",
        source_type=SourceType.NEWS,
        topic="crypto",
        title="SEC approved Bitcoin ETF as inflows surge",
        body="Bitcoin adoption and ETF inflows reached a record high.",
        confidence=0.95,
    )
    assessments = engine.assess(item)
    assert assessments
    assert assessments[0].asset == "BTCUSDT"
    assert assessments[0].sentiment_label == "POSITIVE"
    policy = engine.policy_for("BTCUSDT")
    assert policy.sentiment_score > 0.35
    assert policy.long_weight_multiplier > 1.0
    assert policy.size_multiplier <= 1.0


def test_critical_hack_pauses_and_force_exits(tmp_path: Path):
    brain = make_brain(tmp_path)
    engine = NewsIntelligenceEngine(brain.memory, ("BTCUSDT",))
    item = EvidenceItem(
        source_id="security-feed",
        source_type=SourceType.NEWS,
        topic="security",
        title="Bitcoin exchange hacked after critical exploit",
        body="The exchange reported a breach and asset drain.",
        confidence=0.99,
    )
    assessments = engine.assess(item)
    assert assessments[0].severity == "CRITICAL"
    policy = engine.policy_for("BTCUSDT")
    assert policy.pause_new_entries is True
    assert policy.force_exit is True
    assert policy.size_multiplier < 0.5


def test_brain_news_pause_overrides_long_consensus(tmp_path: Path):
    brain = make_brain(tmp_path)
    decision = brain.decide(
        "BTCUSDT",
        [sig("a", 1.0), sig("b", 1.0)],
        context={
            "regime": "HIGH_VOL",
            "news_policy": {
                "sentiment_score": -0.9,
                "volatility_score": 1.0,
                "size_multiplier": 0.15,
                "long_weight_multiplier": 0.8,
                "short_weight_multiplier": 1.2,
                "pause_new_entries": True,
                "force_exit": True,
            },
        },
    )
    assert decision.action == Action.FLAT
    assert decision.risk_budget_fraction == 0.0
    assert "NEWS_PAUSE_NEW_ENTRIES" in decision.rationale


def test_news_volatility_reduces_paper_risk_budget(tmp_path: Path):
    brain = make_brain(tmp_path)
    baseline = brain.decide("BTCUSDT", [sig("a", 1), sig("b", 1)])
    reduced = brain.decide(
        "BTCUSDT",
        [sig("a", 1), sig("b", 1)],
        context={
            "news_policy": {
                "sentiment_score": 0.0,
                "volatility_score": 0.8,
                "size_multiplier": 0.4,
                "long_weight_multiplier": 1.0,
                "short_weight_multiplier": 1.0,
                "pause_new_entries": False,
                "force_exit": False,
            }
        },
    )
    assert reduced.action == Action.LONG
    assert 0 < reduced.risk_budget_fraction < baseline.risk_budget_fraction


def test_runtime_critical_news_closes_open_paper_position(tmp_path: Path):
    brain = make_brain(tmp_path)
    runtime = BrainRuntimeService(
        brain,
        RuntimeConfig(
            network_enabled=False,
            market_symbols=("BTCUSDT",),
            market_inst_type="usdt-futures",
            persist_market_every_seconds=5,
            rss_poll_seconds=300,
        ),
    )
    runtime.market.update(tick(100.0))
    decision = brain.decide("BTCUSDT", [sig("a", 1), sig("b", 1)])
    opened = runtime.paper.open_from_decision(
        decision.decision_id,
        runtime.market.get("BTCUSDT"),
        equity=10000.0,
        stop_distance_bps=100.0,
    )
    assert opened["status"] == "OPEN"

    item = EvidenceItem(
        source_id="security-feed",
        source_type=SourceType.NEWS,
        topic="security",
        title="Bitcoin exchange hacked in major exploit",
        body="Critical breach caused a large asset drain.",
        confidence=0.99,
    )
    result = asyncio.run(runtime.ingest_news_item(item))
    assert result["paper_actions"]
    closed = runtime.paper.get_position(opened["position_id"])
    assert closed["status"] == "CLOSED"
