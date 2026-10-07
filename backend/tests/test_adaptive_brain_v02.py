from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from adaptive_brain.adapters.bitget_public import BitgetPublicTickerStream
from adaptive_brain.brain import AdaptiveBrain, BrainConfig
from adaptive_brain.memory import BrainMemory
from adaptive_brain.news import RSSFeed, RSSNewsPoller
from adaptive_brain.paper import PaperPortfolioSimulator
from adaptive_brain.regime import RegimeConfig, RegimeDetector
from adaptive_brain.research_bridge import ResearchBridge
from adaptive_brain.schema import (
    Action,
    BrainMode,
    EvidenceItem,
    ExpertSignal,
    MarketRegime,
    MarketTick,
    SourceType,
)


def make_brain(tmp_path: Path) -> AdaptiveBrain:
    backend_root = tmp_path / "backend"
    (backend_root / "recovery" / "reports" / "v1_2").mkdir(parents=True)
    return AdaptiveBrain(
        memory=BrainMemory(tmp_path / "brain.db"),
        research_bridge=ResearchBridge(backend_root),
        config=BrainConfig(
            mode=BrainMode.PAPER,
            decision_threshold=0.18,
            min_experts=2,
        ),
    )


def sig(name: str, direction: float, confidence: float = 0.9) -> ExpertSignal:
    return ExpertSignal(
        expert_id=name,
        source_type=SourceType.MODEL,
        symbol="BTCUSDT",
        direction=direction,
        confidence=confidence,
        expected_edge_bps=20.0,
    )


def tick(price: float, bid_offset: float = 1.0, ask_offset: float = 1.0) -> MarketTick:
    return MarketTick(
        venue="bitget",
        category="usdt-futures",
        symbol="BTCUSDT",
        last_price=price,
        bid=price - bid_offset,
        ask=price + ask_offset,
    )


def test_evidence_is_content_deduplicated(tmp_path: Path):
    brain = make_brain(tmp_path)
    item = EvidenceItem(
        source_id="news-a",
        source_type=SourceType.NEWS,
        topic="macro",
        title="Example headline",
        body="Same content",
        url="https://example.com/a",
    )
    assert brain.ingest_evidence(item) is True
    assert brain.ingest_evidence(item) is False
    assert brain.memory.metrics()["evidence_count"] == 1


def test_learning_uses_decision_snapshot_even_after_signal_ttl(tmp_path: Path):
    brain = make_brain(tmp_path)
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    expired = ExpertSignal(
        expert_id="old-but-valid-at-decision",
        source_type=SourceType.MODEL,
        symbol="BTCUSDT",
        direction=1.0,
        confidence=1.0,
        observed_at=old_time,
        ttl_seconds=1,
    )
    before = brain.memory.get_weight(expired.expert_id)
    brain.ensemble.learn([expired], market_move_bps=30.0)
    assert brain.memory.get_weight(expired.expert_id) > before


def test_source_registry_tracks_signal_and_outcome(tmp_path: Path):
    brain = make_brain(tmp_path)
    decision = brain.decide("BTCUSDT", [sig("model-a", 1), sig("model-b", 1)])
    brain.record_outcome(decision.decision_id, market_move_bps=25.0, pnl_bps=15.0)
    rows = {row["source_id"]: row for row in brain.memory.list_sources()}
    assert rows["model-a"]["signal_count"] == 1
    assert rows["model-a"]["correct_count"] == 1
    assert rows["model-a"]["accuracy"] == 1.0


def test_regime_detector_finds_trend_up():
    detector = RegimeDetector(
        RegimeConfig(
            window=25,
            min_points=10,
            trend_threshold_bps=10.0,
            high_vol_threshold_bps=1000.0,
        )
    )
    result = None
    for i in range(12):
        result = detector.update(tick(100.0 + i * 0.2, 0.01, 0.01))
    assert result is not None
    assert result["regime"] == MarketRegime.TREND_UP.value
    assert result["momentum_bps"] > 10.0


def test_paper_simulator_charges_costs_and_closes(tmp_path: Path):
    brain = make_brain(tmp_path)
    decision = brain.decide("BTCUSDT", [sig("model-a", 1), sig("model-b", 1)])
    sim = PaperPortfolioSimulator(brain.memory)
    opened = sim.open_from_decision(
        decision.decision_id,
        tick(100.0, 0.05, 0.05),
        equity=10000.0,
        stop_distance_bps=100.0,
    )
    assert opened["status"] == "OPEN"
    assert opened["open_fee"] > 0

    closed = sim.close(opened["position_id"], tick(103.0, 0.05, 0.05))
    assert closed["status"] == "CLOSED"
    assert closed["close_fee"] > 0
    assert closed["realized_pnl"] > 0
    assert sim.portfolio()["closed_count"] == 1


def test_bitget_v3_public_ticker_parser():
    payload = {
        "arg": {
            "instType": "usdt-futures",
            "topic": "ticker",
            "symbol": "BTCUSDT",
        },
        "data": [{
            "lastPrice": "100000",
            "bid1Price": "99999",
            "ask1Price": "100001",
            "bid1Size": "2.5",
            "ask1Size": "3.0",
            "markPrice": "100000.5",
            "indexPrice": "99998.5",
            "fundingRate": "0.0001",
            "openInterest": "12345",
        }],
        "ts": 1760000000000,
    }
    rows = BitgetPublicTickerStream.parse_message(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "BTCUSDT"
    assert row.mid == 100000.0
    assert row.spread_bps > 0
    assert row.funding_rate == 0.0001


def test_rss_parser_creates_news_evidence():
    xml = """<?xml version="1.0"?>
    <rss><channel><item>
      <title>Market event</title>
      <description>Example body</description>
      <link>https://example.com/news</link>
      <pubDate>Wed, 07 Oct 2026 05:00:00 GMT</pubDate>
    </item></channel></rss>"""
    rows = RSSNewsPoller.parse_xml(
        RSSFeed(source_id="example-rss", url="https://example.com/rss"),
        xml,
    )
    assert len(rows) == 1
    assert rows[0].source_type == SourceType.NEWS
    assert rows[0].title == "Market event"
