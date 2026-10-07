from __future__ import annotations

import asyncio
from pathlib import Path

from adaptive_brain.advisers import AdviserContext, AdviserHub, opinion_to_signal
from adaptive_brain.brain import AdaptiveBrain, BrainConfig
from adaptive_brain.drift import DriftMonitor
from adaptive_brain.memory import BrainMemory
from adaptive_brain.model_registry import ModelRegistry
from adaptive_brain.paper import PaperPortfolioSimulator
from adaptive_brain.paper_lifecycle import PaperLifecycleManager
from adaptive_brain.research_bridge import ResearchBridge
from adaptive_brain.schema import (
    Action,
    BrainMode,
    ExpertSignal,
    MarketTick,
    SourceType,
)
from adaptive_brain.source_reputation import SourceReputationEngine
from adaptive_brain.strategy_router import StrategyRouter


def make_brain(tmp_path: Path) -> AdaptiveBrain:
    backend_root = tmp_path / "backend"
    (backend_root / "recovery" / "reports" / "v1_2").mkdir(parents=True)
    return AdaptiveBrain(
        BrainMemory(tmp_path / "brain.db"),
        ResearchBridge(backend_root),
        BrainConfig(mode=BrainMode.PAPER, min_experts=2, decision_threshold=0.18),
    )


def add_source_history(
    memory: BrainMemory,
    source_id: str,
    *,
    correct: bool,
    reward: float,
    n: int,
    prefix: str,
) -> None:
    for i in range(n):
        memory.add_source_outcome(
            source_id=source_id,
            source_type=SourceType.MODEL.value,
            decision_id=f"{prefix}-{i}",
            correct=correct,
            reward=reward,
            pnl_bps=reward * 10.0,
        )


def sig(name: str, direction: float = 1.0, confidence: float = 0.8, **metadata):
    return ExpertSignal(
        expert_id=name,
        source_type=SourceType.MODEL,
        symbol="BTCUSDT",
        direction=direction,
        confidence=confidence,
        expected_edge_bps=20.0,
        metadata=metadata,
    )


def tick(price: float) -> MarketTick:
    return MarketTick(
        venue="bitget",
        category="usdt-futures",
        symbol="BTCUSDT",
        last_price=price,
        bid=price - 0.05,
        ask=price + 0.05,
    )


def test_reputation_shrinks_small_sample_then_rewards_history(tmp_path: Path):
    brain = make_brain(tmp_path)
    rep = SourceReputationEngine(brain.memory)

    add_source_history(
        brain.memory,
        "model-a",
        correct=True,
        reward=1.0,
        n=2,
        prefix="small",
    )
    small = rep.assess("model-a")
    assert 1.0 < small.weight_multiplier < 1.10

    add_source_history(
        brain.memory,
        "model-a",
        correct=True,
        reward=1.0,
        n=40,
        prefix="large",
    )
    large = rep.assess("model-a")
    assert large.samples == 42
    assert large.weight_multiplier > small.weight_multiplier
    assert large.weight_multiplier <= 1.25


def test_drift_detects_recent_collapse(tmp_path: Path):
    brain = make_brain(tmp_path)
    add_source_history(
        brain.memory,
        "drifty",
        correct=True,
        reward=0.8,
        n=25,
        prefix="baseline",
    )
    add_source_history(
        brain.memory,
        "drifty",
        correct=False,
        reward=-0.8,
        n=20,
        prefix="recent",
    )

    assessment = DriftMonitor(brain.memory).assess("drifty")
    assert assessment.drift_detected is True
    assert assessment.status == "DRIFT"
    assert assessment.weight_multiplier < 1.0
    assert "RECENT_ACCURACY_DEGRADATION" in assessment.reasons


def test_router_prefers_reliable_source_and_applies_regime_affinity(tmp_path: Path):
    brain = make_brain(tmp_path)
    add_source_history(
        brain.memory,
        "good",
        correct=True,
        reward=0.9,
        n=35,
        prefix="good",
    )
    add_source_history(
        brain.memory,
        "bad",
        correct=False,
        reward=-0.9,
        n=35,
        prefix="bad",
    )

    router = StrategyRouter(
        SourceReputationEngine(brain.memory),
        DriftMonitor(brain.memory),
    )
    routed, audit = router.route(
        [
            sig("good", regime_affinity=["TREND_UP"]),
            sig("bad", regime_affinity=["RANGE"]),
        ],
        "TREND_UP",
    )
    by_id = {s.expert_id: s for s in routed}
    assert by_id["good"].confidence > by_id["bad"].confidence
    assert by_id["good"].metadata["regime_multiplier"] > 1.0
    assert by_id["bad"].metadata["regime_multiplier"] < 1.0
    assert len(audit) == 2


def test_challenger_can_be_promoted_after_outperformance(tmp_path: Path):
    brain = make_brain(tmp_path)
    registry = ModelRegistry(
        brain.memory,
        SourceReputationEngine(brain.memory),
        DriftMonitor(brain.memory),
    )
    registry.register("model", "1", role="CHAMPION")
    registry.register("model", "2", role="CHALLENGER")

    champ = "model@1"
    challenger = "model@2"

    for i in range(40):
        brain.memory.add_source_outcome(
            source_id=champ,
            source_type=SourceType.MODEL.value,
            decision_id=f"champ-{i}",
            correct=(i % 5 != 0),
            reward=0.20,
            pnl_bps=2.0,
        )
        brain.memory.add_source_outcome(
            source_id=challenger,
            source_type=SourceType.MODEL.value,
            decision_id=f"chall-{i}",
            correct=True,
            reward=0.90,
            pnl_bps=9.0,
        )

    decision = registry.evaluate(challenger, champ)
    assert decision.allowed is True

    result = registry.promote(challenger, champ)
    assert result["champion"]["expert_id"] == challenger
    assert registry.current_champion()["expert_id"] == challenger


def test_paper_lifecycle_marks_and_auto_stops(tmp_path: Path):
    brain = make_brain(tmp_path)
    decision = brain.decide(
        "BTCUSDT",
        [sig("a", confidence=0.9), sig("b", confidence=0.9)],
    )
    sim = PaperPortfolioSimulator(brain.memory)
    opened = sim.open_from_decision(
        decision.decision_id,
        tick(100.0),
        equity=10000.0,
        stop_distance_bps=100.0,
    )
    lifecycle = PaperLifecycleManager(brain.memory, sim)

    result = lifecycle.process_tick(tick(98.0))
    assert result["marks"]
    assert result["actions"]
    assert result["actions"][0]["action"] == "STOP_LOSS_PAPER"
    assert sim.get_position(opened["position_id"])["status"] == "CLOSED"
    assert lifecycle.marks(opened["position_id"])


def test_model_adviser_contract_is_versioned_and_secret_free():
    signal = opinion_to_signal(
        adviser_id="semantic-news",
        version="1.0",
        symbol="BTCUSDT",
        direction=0.5,
        confidence=0.7,
        expected_edge_bps=12.0,
    )
    assert signal.expert_id == "semantic-news@1.0"
    assert signal.source_type == SourceType.MODEL
    assert signal.metadata["adviser_version"] == "1.0"

    hub = AdviserHub()
    assert hub.list() == []


def test_brain_records_source_outcomes_and_uses_router_next_time(tmp_path: Path):
    brain = make_brain(tmp_path)

    for i in range(12):
        d = brain.decide(
            "BTCUSDT",
            [sig("alpha", confidence=0.9), sig("beta", confidence=0.9)],
        )
        brain.record_outcome(
            d.decision_id,
            market_move_bps=30.0,
            pnl_bps=15.0,
        )

    assert brain.memory.metrics()["source_outcome_count"] == 24
    next_decision = brain.decide(
        "BTCUSDT",
        [
            sig("alpha", confidence=0.8, regime_affinity=["TREND_UP"]),
            sig("beta", confidence=0.8, regime_affinity=["TREND_UP"]),
        ],
        context={"regime": "TREND_UP"},
    )
    assert next_decision.action == Action.LONG
    assert any(
        "source_reputation_multiplier" in snap["metadata"]
        for snap in next_decision.expert_snapshot
    )
    assert "ROUTER_APPLIED=2" in next_decision.rationale
