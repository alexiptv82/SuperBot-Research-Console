from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass

from .schema import MarketRegime, MarketTick


@dataclass(frozen=True)
class RegimeConfig:
    window: int = 40
    min_points: int = 20
    trend_threshold_bps: float = 35.0
    high_vol_threshold_bps: float = 18.0


class RegimeDetector:
    """Small, deterministic online regime classifier for runtime context."""

    def __init__(self, config: RegimeConfig | None = None):
        self.config = config or RegimeConfig()
        self._prices: dict[str, deque[float]] = {}

    def update(self, tick: MarketTick) -> dict:
        symbol = tick.symbol.upper()
        q = self._prices.setdefault(symbol, deque(maxlen=self.config.window))
        if tick.mid > 0:
            q.append(float(tick.mid))
        return self.snapshot(symbol)

    def snapshot(self, symbol: str) -> dict:
        symbol = symbol.upper()
        prices = list(self._prices.get(symbol, ()))
        if len(prices) < self.config.min_points:
            return {
                "symbol": symbol,
                "regime": MarketRegime.INSUFFICIENT.value,
                "points": len(prices),
                "momentum_bps": None,
                "volatility_bps": None,
            }

        rets = []
        for a, b in zip(prices, prices[1:]):
            if a > 0 and b > 0:
                rets.append(math.log(b / a))
        momentum_bps = math.log(prices[-1] / prices[0]) * 10000.0
        volatility_bps = statistics.pstdev(rets) * 10000.0 if len(rets) > 1 else 0.0

        if volatility_bps >= self.config.high_vol_threshold_bps:
            regime = MarketRegime.HIGH_VOL
        elif momentum_bps >= self.config.trend_threshold_bps:
            regime = MarketRegime.TREND_UP
        elif momentum_bps <= -self.config.trend_threshold_bps:
            regime = MarketRegime.TREND_DOWN
        else:
            regime = MarketRegime.RANGE

        return {
            "symbol": symbol,
            "regime": regime.value,
            "points": len(prices),
            "momentum_bps": momentum_bps,
            "volatility_bps": volatility_bps,
        }
