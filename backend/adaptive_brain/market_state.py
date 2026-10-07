from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from .schema import MarketTick


class MarketState:
    """Thread-safe latest-tick cache. Persistence is handled separately."""

    def __init__(self):
        self._lock = threading.RLock()
        self._latest: dict[str, MarketTick] = {}
        self._count: dict[str, int] = defaultdict(int)

    def update(self, tick: MarketTick) -> None:
        with self._lock:
            self._latest[tick.symbol.upper()] = tick
            self._count[tick.symbol.upper()] += 1

    def get(self, symbol: str) -> MarketTick | None:
        with self._lock:
            return self._latest.get(symbol.upper())

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "symbols": sorted(self._latest),
                "tick_counts": dict(self._count),
                "latest": {k: v.to_dict() for k, v in self._latest.items()},
            }
