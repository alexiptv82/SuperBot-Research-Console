from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adaptive_brain.advisers import AdviserContext, AdviserHub
from adaptive_brain.brain import AdaptiveBrain, BrainConfig
from adaptive_brain.builtin_advisers import NewsPolicyAdviser, RegimeMomentumAdviser
from adaptive_brain.market_state import MarketState
from adaptive_brain.news_intelligence import NewsIntelligenceEngine
from adaptive_brain.orchestrator import ContinuousPaperOrchestrator, OrchestratorConfig
from adaptive_brain.remote_advisers import RemoteAdviserConfig, RemoteJSONAdviser
from adaptive_brain.paper import PaperPortfolioSimulator
from adaptive_brain.paper_lifecycle import PaperLifecycleManager
from adaptive_brain.partner_signals import PartnerSignal, PartnerSignalStore
from adaptive_brain.regime import RegimeDetector
from adaptive_brain.research_bridge import ResearchBridge
from adaptive_brain.schema import BrainMode, MarketTick


def make_brain(tmp_path: Path) -> AdaptiveBrain:
    backend_root = tmp_path / "backend"
    (backend_root / "recovery" / "reports" / "v1_2").mkdir(parents=True)
    return AdaptiveBrain(
        memory=__import__(
            "adaptive_brain.memory", fromlist=["BrainMemory"]
        ).BrainMemory(tmp_path / "brain.db"),
        research_bridge=ResearchBridge(backend_root),
        config=BrainConfig(mode=BrainMode.PAPER, min_experts=2, decision_threshold=0.18),
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


def make_orchestrator(
    tmp_path: Path,
    *,
    enabled: bool = True,
    min_cycle_seconds: float = 0.0,
    stop_distance_bps: float = 100.0,
):
    brain = make_brain(tmp_path)
    market = MarketState()
    regime = RegimeDetector()
    news = NewsIntelligenceEngine(brain.memory, ("BTCUSDT",))
    advisers = AdviserHub()
    partners = PartnerSignalStore(brain.memory)
    paper = PaperPortfolioSimulator(brain.memory)
    lifecycle = PaperLifecycleManager(brain.memory, paper)
    orch = ContinuousPaperOrchestrator(
        brain=brain,
        market=market,
        regime=regime,
        news=news,
        advisers=advisers,
        partners=partners,
        paper=paper,
        lifecycle=lifecycle,
        config=OrchestratorConfig(
            enabled=enabled,
            min_cycle_seconds=min_cycle_seconds,
            min_signal_count=2,
            default_equity=10000.0,
            default_stop_distance_bps=stop_distance_bps,
        ),
    )
    return brain, market, regime, partners, paper, orch


def add_two_long_partners(partners: PartnerSignalStore):
    partners.add(
        PartnerSignal(
            partner_id="trader-a",
            symbol="BTCUSDT",
            direction=1.0,
            confidence=0.9,
            expected_edge_bps=25.0,
            ttl_seconds=600,
        )
    )
    partners.add(
        PartnerSignal(
            partner_id="trader-b",
            symbol="BTCUSDT",
            direction=1.0,
            confidence=0.85,
            expected_edge_bps=20.0,
            ttl_seconds=600,
        )
    )


def test_partner_store_keeps_latest_fresh_signal_per_partner(tmp_path: Path):
    brain = make_brain(tmp_path)
    store = PartnerSignalStore(brain.memory)
    store.add(
        PartnerSignal(
            partner_id="p1",
            symbol="BTCUSDT",
            direction=-1.0,
            confidence=0.4,
            ttl_seconds=600,
        )
    )
    store.add(
        PartnerSignal(
            partner_id="p1",
            symbol="BTCUSDT",
            direction=1.0,
            confidence=0.9,
            ttl_seconds=600,
        )
    )
    expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.add(
        PartnerSignal(
            partner_id="expired",
            symbol="BTCUSDT",
            direction=1.0,
            confidence=1.0,
            observed_at=expired_at,
            ttl_seconds=1,
        )
    )

    fresh = store.fresh_for_symbol("BTCUSDT")
    assert len(fresh) == 1
    assert fresh[0].expert_id == "p1"
    assert fresh[0].direction == 1.0


def test_orchestrator_disabled_never_creates_decision(tmp_path: Path):
    brain, market, regime, partners, paper, orch = make_orchestrator(
        tmp_path, enabled=False
    )
    t = tick(100.0)
    market.update(t)
    regime.update(t)
    result = asyncio.run(orch.on_tick(t))
    assert result["enabled"] is False
    assert result["decision"] is None
    assert paper.portfolio()["position_count"] == 0


def test_orchestrator_opens_paper_from_two_partner_signals(tmp_path: Path):
    brain, market, regime, partners, paper, orch = make_orchestrator(
        tmp_path,
        stop_distance_bps=500.0,
    )
    add_two_long_partners(partners)

    t = tick(100.0)
    market.update(t)
    regime.update(t)
    result = asyncio.run(orch.on_tick(t))

    assert result["decision"] is not None
    assert result["decision"]["action"] == "LONG"
    assert result["paper_open"] is not None
    assert result["paper_open"]["status"] == "OPEN"
    assert paper.portfolio()["open_count"] == 1


def test_one_open_position_per_symbol_guard(tmp_path: Path):
    brain, market, regime, partners, paper, orch = make_orchestrator(
        tmp_path,
        stop_distance_bps=500.0,
    )
    add_two_long_partners(partners)

    t = tick(100.0)
    market.update(t)
    regime.update(t)
    first = asyncio.run(orch.on_tick(t))
    assert first["paper_open"] is not None

    second = asyncio.run(orch.on_tick(tick(100.2)))
    assert second["paper_open"] is None
    assert paper.portfolio()["open_count"] == 1
    assert orch.status()["metrics"]["skipped_existing_position"] >= 1


def test_closed_paper_trade_records_brain_outcome_once(tmp_path: Path):
    brain, market, regime, partners, paper, orch = make_orchestrator(
        tmp_path,
        min_cycle_seconds=999.0,
    )
    add_two_long_partners(partners)

    first_tick = tick(100.0)
    market.update(first_tick)
    regime.update(first_tick)
    opened = asyncio.run(orch.on_tick(first_tick))["paper_open"]
    assert opened is not None
    decision_id = opened["decision_id"]

    adverse = tick(98.0)
    market.update(adverse)
    regime.update(adverse)
    result = asyncio.run(orch.on_tick(adverse))

    assert result["learned_outcomes"]
    assert brain.memory.has_outcome(decision_id) is True
    assert brain.memory.metrics()["outcome_count"] == 1
    assert orch.status()["metrics"]["outcomes_recorded"] == 1

    # Reprocessing another tick cannot duplicate the same decision outcome.
    again = tick(97.5)
    market.update(again)
    regime.update(again)
    asyncio.run(orch.on_tick(again))
    assert brain.memory.metrics()["outcome_count"] == 1


def test_builtin_advisers_emit_versioned_signals():
    context = AdviserContext(
        symbol="BTCUSDT",
        regime="TREND_UP",
        market={"momentum_bps": 80.0},
        news_policy={
            "sentiment_score": 0.8,
            "pause_new_entries": False,
            "force_exit": False,
        },
        features={},
    )
    regime_signal = asyncio.run(RegimeMomentumAdviser().advise(context))
    news_signal = asyncio.run(NewsPolicyAdviser().advise(context))

    assert regime_signal is not None
    assert news_signal is not None
    assert regime_signal.expert_id == "regime-momentum@1.0"
    assert news_signal.expert_id == "news-policy@1.0"
    assert regime_signal.direction == 1.0
    assert news_signal.direction == 1.0


def test_v05_version_and_live_stays_disabled(tmp_path: Path):
    brain = make_brain(tmp_path)
    status = brain.status()
    assert status["version"] == "0.5.0"
    assert status["mode"] == "PAPER"
    assert status["live_execution_enabled"] is False
    assert status["safety"]["learner_can_place_orders"] is False
    assert status["safety"]["private_exchange_api_present"] is False


def test_remote_adviser_contract_parses_and_clamps():
    config = RemoteAdviserConfig(
        adviser_id="external-model",
        version="2.1",
        url="https://example.com/signal",
        enabled=False,
        min_interval_seconds=60,
        max_calls_per_hour=10,
    )
    config.validate()

    signal = RemoteJSONAdviser.parse_response(
        adviser_id="external-model",
        version="2.1",
        symbol="BTCUSDT",
        payload={
            "direction": 1.7,
            "confidence": 1.4,
            "expected_edge_bps": 18.0,
            "metadata": {"provider": "bridge-test"},
        },
    )
    assert signal.expert_id == "external-model@2.1"
    assert signal.direction == 1.0
    assert signal.confidence == 1.0
    assert signal.metadata["adviser_type"] == "remote-json-bridge"


def test_adviser_hub_isolates_failing_adviser():
    class Good:
        adviser_id = "good"
        version = "1"

        async def advise(self, context):
            from adaptive_brain.schema import ExpertSignal, SourceType
            return ExpertSignal(
                expert_id="good@1",
                source_type=SourceType.MODEL,
                symbol=context.symbol,
                direction=1.0,
                confidence=0.8,
            )

    class Bad:
        adviser_id = "bad"
        version = "1"

        async def advise(self, context):
            raise RuntimeError("simulated adviser failure")

    hub = AdviserHub(per_adviser_timeout_seconds=1.0)
    hub.register(Good())
    hub.register(Bad())
    context = AdviserContext(
        symbol="BTCUSDT",
        regime="TREND_UP",
        market={},
        news_policy={},
        features={},
    )
    rows = asyncio.run(hub.collect(context))
    assert len(rows) == 1
    assert rows[0].expert_id == "good@1"
    assert "bad@1" in hub.status()["last_errors"]
