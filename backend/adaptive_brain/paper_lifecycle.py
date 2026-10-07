from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .memory import BrainMemory
from .paper import PaperPortfolioSimulator
from .schema import Action, MarketTick, utcnow_iso


@dataclass(frozen=True)
class PaperLifecycleConfig:
    take_profit_r: float = 2.0
    max_holding_minutes: int = 1440
    mark_persist_seconds: float = 5.0


class PaperLifecycleManager:
    """Automatic PAPER mark-to-market and bounded exits.

    It has no exchange client. Every automatic action is persisted as PAPER.
    """

    def __init__(
        self,
        memory: BrainMemory,
        simulator: PaperPortfolioSimulator,
        config: PaperLifecycleConfig | None = None,
    ):
        self.memory = memory
        self.simulator = simulator
        self.config = config or PaperLifecycleConfig()
        self._last_mark_monotonic: dict[str, float] = {}

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    @staticmethod
    def _mark_price(position: dict[str, Any], tick: MarketTick) -> float:
        if position["side"] == Action.LONG.value:
            return tick.bid if tick.bid > 0 else tick.mid
        return tick.ask if tick.ask > 0 else tick.mid

    def process_tick(self, tick: MarketTick) -> dict[str, Any]:
        positions = self.simulator.list_open_positions(tick.symbol)
        marks: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for position in positions:
            price = self._mark_price(position, tick)
            entry = float(position["entry_price"])
            qty = float(position["quantity"])
            side_mult = (
                1.0 if position["side"] == Action.LONG.value else -1.0
            )
            gross_unrealized = side_mult * (price - entry) * qty
            unrealized = gross_unrealized - float(position["open_fee"])

            stop_distance_cash = (
                entry
                * qty
                * float(position["stop_distance_bps"])
                / 10000.0
            )
            r_multiple = (
                unrealized / stop_distance_cash
                if stop_distance_cash > 0
                else 0.0
            )
            held_minutes = (
                now - self._parse_ts(position["opened_at"])
            ).total_seconds() / 60.0

            mark = {
                "position_id": position["position_id"],
                "symbol": position["symbol"],
                "mark_price": price,
                "unrealized_pnl": unrealized,
                "r_multiple": r_multiple,
                "held_minutes": held_minutes,
                "created_at": utcnow_iso(),
            }
            now_mono = time.monotonic()
            last_mark = self._last_mark_monotonic.get(
                position["position_id"],
                0.0,
            )
            if (
                now_mono - last_mark
                >= self.config.mark_persist_seconds
            ):
                self.memory.add_paper_mark(mark)
                self._last_mark_monotonic[position["position_id"]] = now_mono
                marks.append(mark)

            reason = None
            if r_multiple <= -1.0:
                reason = "STOP_LOSS_PAPER"
            elif r_multiple >= self.config.take_profit_r:
                reason = "TAKE_PROFIT_PAPER"
            elif held_minutes >= self.config.max_holding_minutes:
                reason = "TIME_EXIT_PAPER"

            if reason:
                closed = self.simulator.close(
                    position["position_id"],
                    tick,
                    reason=reason,
                )
                actions.append({
                    "position_id": position["position_id"],
                    "symbol": position["symbol"],
                    "action": reason,
                    "close_price": closed["close_price"],
                    "realized_pnl": closed["realized_pnl"],
                })

        return {
            "symbol": tick.symbol,
            "marks": marks,
            "actions": actions,
        }

    def marks(self, position_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.memory.paper_marks(position_id, limit=limit)
