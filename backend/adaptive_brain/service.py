from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

from .adapters.bitget_public import BitgetPublicConfig, BitgetPublicTickerStream
from .advisers import AdviserHub
from .brain import AdaptiveBrain
from .builtin_advisers import NewsPolicyAdviser, RegimeMomentumAdviser
from .market_state import MarketState
from .news import RSSFeed, RSSNewsPoller
from .news_intelligence import NewsIntelligenceEngine
from .paper import PaperConfig, PaperPortfolioSimulator
from .paper_lifecycle import PaperLifecycleManager
from .partner_signals import PartnerSignalStore
from .orchestrator import ContinuousPaperOrchestrator, OrchestratorConfig
from .remote_advisers import RemoteAdviserConfig, RemoteJSONAdviser
from .regime import RegimeDetector
from .schema import MarketTick, Observation, SourceType


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RuntimeConfig:
    network_enabled: bool
    market_symbols: tuple[str, ...]
    market_inst_type: str
    persist_market_every_seconds: float
    rss_poll_seconds: float


class BrainRuntimeService:
    """Coordinates read-only network ingestion + paper runtime context."""

    def __init__(
        self,
        brain: AdaptiveBrain,
        config: RuntimeConfig | None = None,
    ):
        self.brain = brain
        self.config = config or self._config_from_env()
        self.market = MarketState()
        self.regime = RegimeDetector()
        self.paper = PaperPortfolioSimulator(brain.memory, PaperConfig())
        self.paper_lifecycle = PaperLifecycleManager(
            brain.memory,
            self.paper,
        )
        self.advisers = AdviserHub()
        self.advisers.register(RegimeMomentumAdviser())
        self.advisers.register(NewsPolicyAdviser())
        self.remote_advisers: list[RemoteJSONAdviser] = []
        self._remote_adviser_config_error: str | None = None
        self._register_remote_advisers_from_env()
        self.partners = PartnerSignalStore(brain.memory)
        self.news = NewsIntelligenceEngine(
            brain.memory,
            tracked_assets=self.config.market_symbols,
        )
        self.orchestrator = ContinuousPaperOrchestrator(
            brain=brain,
            market=self.market,
            regime=self.regime,
            news=self.news,
            advisers=self.advisers,
            partners=self.partners,
            paper=self.paper,
            lifecycle=self.paper_lifecycle,
            config=OrchestratorConfig(
                enabled=_env_bool("SUPERBOT_BRAIN_AUTO_PAPER", False),
                min_cycle_seconds=max(
                    1.0,
                    float(os.environ.get(
                        "SUPERBOT_BRAIN_ORCH_MIN_CYCLE_SECONDS",
                        "15",
                    )),
                ),
                min_signal_count=max(
                    2,
                    int(os.environ.get(
                        "SUPERBOT_BRAIN_ORCH_MIN_SIGNALS",
                        "2",
                    )),
                ),
                default_equity=max(
                    1.0,
                    float(os.environ.get(
                        "SUPERBOT_BRAIN_PAPER_EQUITY",
                        "10000",
                    )),
                ),
                default_stop_distance_bps=max(
                    5.0,
                    float(os.environ.get(
                        "SUPERBOT_BRAIN_PAPER_STOP_BPS",
                        "100",
                    )),
                ),
            ),
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_market_persist: dict[str, float] = {}
        self._last_error: str | None = None
        self._rss_inserted = 0
        self._last_paper_lifecycle: dict | None = None

    def _register_remote_advisers_from_env(self) -> None:
        raw = os.environ.get(
            "SUPERBOT_BRAIN_REMOTE_ADVISERS_JSON",
            "",
        ).strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("remote adviser config must be a JSON list")
            for item in data:
                config = RemoteAdviserConfig(
                    adviser_id=str(item["adviser_id"]),
                    version=str(item.get("version", "1.0")),
                    url=str(item["url"]),
                    enabled=bool(item.get("enabled", False)),
                    api_key_env=(
                        str(item["api_key_env"])
                        if item.get("api_key_env")
                        else None
                    ),
                    timeout_seconds=float(
                        item.get("timeout_seconds", 15.0)
                    ),
                    min_interval_seconds=float(
                        item.get("min_interval_seconds", 60.0)
                    ),
                    max_calls_per_hour=int(
                        item.get("max_calls_per_hour", 60)
                    ),
                )
                adviser = RemoteJSONAdviser(config)
                self.remote_advisers.append(adviser)
                self.advisers.register(adviser)
        except Exception as exc:
            self._remote_adviser_config_error = (
                f"{type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _config_from_env() -> RuntimeConfig:
        symbols = tuple(
            s.strip().upper()
            for s in os.environ.get(
                "SUPERBOT_BRAIN_MARKET_SYMBOLS", "BTCUSDT,ETHUSDT"
            ).split(",")
            if s.strip()
        )
        return RuntimeConfig(
            network_enabled=_env_bool("SUPERBOT_BRAIN_NETWORK_ENABLED", False),
            market_symbols=symbols or ("BTCUSDT", "ETHUSDT"),
            market_inst_type=os.environ.get(
                "SUPERBOT_BRAIN_MARKET_INST_TYPE", "usdt-futures"
            ).strip(),
            persist_market_every_seconds=max(
                1.0,
                float(
                    os.environ.get(
                        "SUPERBOT_BRAIN_MARKET_PERSIST_SECONDS", "5"
                    )
                ),
            ),
            rss_poll_seconds=max(
                30.0,
                float(os.environ.get("SUPERBOT_BRAIN_RSS_POLL_SECONDS", "300")),
            ),
        )

    @staticmethod
    def _rss_feeds_from_env() -> list[RSSFeed]:
        # Verified official public feeds. These activate only when the global
        # brain network switch is enabled.
        feeds: list[RSSFeed] = [
            RSSFeed(
                source_id="sec-press-releases",
                url="https://www.sec.gov/news/pressreleases.rss",
                topic="regulation",
                confidence=0.98,
            ),
            RSSFeed(
                source_id="fed-all-press",
                url="https://www.federalreserve.gov/feeds/press_all.xml",
                topic="macro",
                confidence=0.98,
            ),
            RSSFeed(
                source_id="fed-monetary-policy",
                url="https://www.federalreserve.gov/feeds/press_monetary.xml",
                topic="fed-policy",
                confidence=0.99,
            ),
        ]

        raw = os.environ.get("SUPERBOT_BRAIN_RSS_FEEDS_JSON", "").strip()
        if raw:
            data = json.loads(raw)
            for item in data:
                feeds.append(
                    RSSFeed(
                        source_id=str(item["source_id"]),
                        url=str(item["url"]),
                        topic=str(item.get("topic", "market-news")),
                        confidence=float(item.get("confidence", 0.5)),
                    )
                )
        return feeds

    async def start(self) -> None:
        if self._tasks or not self.config.network_enabled:
            return
        self._stop = asyncio.Event()

        ticker = BitgetPublicTickerStream(
            BitgetPublicConfig(
                symbols=self.config.market_symbols,
                inst_type=self.config.market_inst_type,
            )
        )
        self._tasks.append(
            asyncio.create_task(
                ticker.stream(
                    self._on_tick,
                    self._stop,
                    self._on_market_error,
                ),
                name="superbot-bitget-public-ticker",
            )
        )

        feeds = self._rss_feeds_from_env()
        if feeds:
            self._tasks.append(
                asyncio.create_task(
                    self._rss_loop(
                        RSSNewsPoller(
                            feeds,
                            user_agent=os.environ.get(
                                "SUPERBOT_BRAIN_NEWS_USER_AGENT",
                                "SuperBotResearch/0.3 (+https://github.com/alexiptv82/SuperBot-Research-Console)",
                            ),
                        )
                    ),
                    name="superbot-rss-news",
                )
            )

    async def stop(self) -> None:
        self._stop.set()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _on_market_error(self, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"
        self.brain.memory.touch_source(
            "bitget-public-ticker",
            SourceType.MARKET.value,
            error=True,
        )

    async def _on_tick(self, tick: MarketTick) -> None:
        self.market.update(tick)
        self.regime.update(tick)
        orch_result = await self.orchestrator.on_tick(tick)
        self._last_paper_lifecycle = orch_result.get("lifecycle")
        self.brain.memory.touch_source(
            "bitget-public-ticker",
            SourceType.MARKET.value,
        )

        now = time.monotonic()
        last = self._last_market_persist.get(tick.symbol, 0.0)
        if now - last < self.config.persist_market_every_seconds:
            return
        self._last_market_persist[tick.symbol] = now

        features = {
            "last_price": tick.last_price,
            "mid_price": tick.mid,
            "spread_bps": tick.spread_bps,
        }
        if tick.funding_rate is not None:
            features["funding_rate"] = tick.funding_rate
        if tick.open_interest is not None:
            features["open_interest"] = tick.open_interest

        for feature, value in features.items():
            self.brain.ingest(
                Observation(
                    source_id="bitget-public-ticker",
                    source_type=SourceType.MARKET,
                    symbol=tick.symbol,
                    feature=feature,
                    value=float(value),
                    confidence=1.0,
                    ttl_seconds=60,
                    metadata={
                        "venue": tick.venue,
                        "category": tick.category,
                        "venue_ts_ms": tick.venue_ts_ms,
                    },
                )
            )

    async def _rss_loop(self, poller: RSSNewsPoller) -> None:
        while not self._stop.is_set():
            try:
                items = await poller.poll()
                for item in items:
                    result = await self.ingest_news_item(item)
                    if result["inserted"]:
                        self._rss_inserted += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.config.rss_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def ingest_news_item(self, item):
        inserted = self.brain.ingest_evidence(item)
        assessments = self.news.assess(item) if inserted else []
        paper_actions = []

        # Automatic action is PAPER-only. Critical negative events may close
        # open paper positions for the affected tracked asset. No live order
        # client exists in this runtime.
        for assessment in assessments:
            if assessment.asset == "GLOBAL":
                continue
            policy = self.news.policy_for(assessment.asset)
            if policy.force_exit:
                tick = self.market.get(assessment.asset)
                if tick is not None:
                    closed = self.paper.close_symbol_positions(
                        assessment.asset,
                        tick,
                    )
                    paper_actions.extend(
                        {
                            "asset": assessment.asset,
                            "action": "FORCE_EXIT_PAPER",
                            "position_id": p["position_id"],
                        }
                        for p in closed
                    )

        learned_outcomes = (
            self.orchestrator.learn_from_closed_actions(paper_actions)
            if paper_actions
            else []
        )

        return {
            "inserted": inserted,
            "assessments": [a.to_dict() for a in assessments],
            "paper_actions": paper_actions,
            "learned_outcomes": learned_outcomes,
        }

    def status(self) -> dict:
        return {
            "network_enabled": self.config.network_enabled,
            "running": any(not t.done() for t in self._tasks),
            "tasks": [t.get_name() for t in self._tasks if not t.done()],
            "market_symbols": list(self.config.market_symbols),
            "market_inst_type": self.config.market_inst_type,
            "market": self.market.status(),
            "rss_inserted": self._rss_inserted,
            "news_policies": {
                symbol: self.news.policy_for(symbol).to_dict()
                for symbol in self.config.market_symbols
            },
            "paper_lifecycle_last": self._last_paper_lifecycle,
            "advisers": self.advisers.status(),
            "remote_advisers": [
                adviser.status() for adviser in self.remote_advisers
            ],
            "remote_adviser_config_error": self._remote_adviser_config_error,
            "orchestrator": self.orchestrator.status(),
            "last_error": self._last_error,
        }
