from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

from .adapters.bitget_public import BitgetPublicConfig, BitgetPublicTickerStream
from .brain import AdaptiveBrain
from .market_state import MarketState
from .news import RSSFeed, RSSNewsPoller
from .paper import PaperConfig, PaperPortfolioSimulator
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
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_market_persist: dict[str, float] = {}
        self._last_error: str | None = None
        self._rss_inserted = 0

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
        raw = os.environ.get("SUPERBOT_BRAIN_RSS_FEEDS_JSON", "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        feeds: list[RSSFeed] = []
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
                    self._rss_loop(RSSNewsPoller(feeds)),
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
                    if self.brain.ingest_evidence(item):
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

    def status(self) -> dict:
        return {
            "network_enabled": self.config.network_enabled,
            "running": any(not t.done() for t in self._tasks),
            "tasks": [t.get_name() for t in self._tasks if not t.done()],
            "market_symbols": list(self.config.market_symbols),
            "market_inst_type": self.config.market_inst_type,
            "market": self.market.status(),
            "rss_inserted": self._rss_inserted,
            "last_error": self._last_error,
        }
