from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any

from .advisers import AdviserContext, AdviserHub
from .brain import AdaptiveBrain
from .market_state import MarketState
from .news_intelligence import NewsIntelligenceEngine
from .paper import PaperPortfolioSimulator
from .paper_lifecycle import PaperLifecycleManager
from .partner_signals import PartnerSignalStore
from .regime import RegimeDetector
from .schema import Action, MarketTick, PortfolioState


@dataclass(frozen=True)
class OrchestratorConfig:
    enabled: bool = False
    min_cycle_seconds: float = 15.0
    min_signal_count: int = 2
    default_equity: float = 10000.0
    default_stop_distance_bps: float = 100.0
    one_open_position_per_symbol: bool = True


class ContinuousPaperOrchestrator:
    """End-to-end adaptive PAPER loop.

    Tick -> context -> advisers/partners -> decision -> PAPER open -> lifecycle
    -> outcome attribution -> source/model learning.

    There is no exchange execution client in this class.
    """

    def __init__(
        self,
        *,
        brain: AdaptiveBrain,
        market: MarketState,
        regime: RegimeDetector,
        news: NewsIntelligenceEngine,
        advisers: AdviserHub,
        partners: PartnerSignalStore,
        paper: PaperPortfolioSimulator,
        lifecycle: PaperLifecycleManager,
        config: OrchestratorConfig | None = None,
    ):
        self.brain = brain
        self.market = market
        self.regime = regime
        self.news = news
        self.advisers = advisers
        self.partners = partners
        self.paper = paper
        self.lifecycle = lifecycle
        self.config = config or OrchestratorConfig()
        self._last_cycle: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._metrics = {
            "cycles": 0,
            "decisions": 0,
            "paper_opens": 0,
            "paper_closes": 0,
            "outcomes_recorded": 0,
            "skipped_disabled": 0,
            "skipped_cooldown": 0,
            "skipped_insufficient_signals": 0,
            "skipped_existing_position": 0,
        }
        self._last_result: dict[str, Any] | None = None

    async def on_tick(self, tick: MarketTick) -> dict[str, Any]:
        lifecycle_result = self.lifecycle.process_tick(tick)
        learned = self.learn_from_closed_actions(lifecycle_result.get("actions") or [])

        if not self.config.enabled:
            self._metrics["skipped_disabled"] += 1
            result = {
                "enabled": False,
                "symbol": tick.symbol,
                "lifecycle": lifecycle_result,
                "learned_outcomes": learned,
                "decision": None,
                "paper_open": None,
            }
            self._last_result = result
            return result

        async with self._lock:
            now = time.monotonic()
            last = self._last_cycle.get(tick.symbol, 0.0)
            if now - last < self.config.min_cycle_seconds:
                self._metrics["skipped_cooldown"] += 1
                result = {
                    "enabled": True,
                    "symbol": tick.symbol,
                    "lifecycle": lifecycle_result,
                    "learned_outcomes": learned,
                    "decision": None,
                    "paper_open": None,
                    "skip_reason": "COOLDOWN",
                }
                self._last_result = result
                return result

            self._last_cycle[tick.symbol] = now
            self._metrics["cycles"] += 1

            regime_ctx = self.regime.snapshot(tick.symbol)
            news_policy = self.news.policy_for(tick.symbol).to_dict()
            adviser_context = AdviserContext(
                symbol=tick.symbol,
                regime=regime_ctx.get("regime"),
                market={
                    **tick.to_dict(),
                    "momentum_bps": regime_ctx.get("momentum_bps"),
                    "volatility_bps": regime_ctx.get("volatility_bps"),
                },
                news_policy=news_policy,
                features={},
            )

            adviser_signals = await self.advisers.collect(adviser_context)
            partner_signals = self.partners.fresh_for_symbol(tick.symbol)
            signals = adviser_signals + partner_signals

            if len(signals) < self.config.min_signal_count:
                self._metrics["skipped_insufficient_signals"] += 1
                result = {
                    "enabled": True,
                    "symbol": tick.symbol,
                    "lifecycle": lifecycle_result,
                    "learned_outcomes": learned,
                    "signals": [s.to_dict() for s in signals],
                    "decision": None,
                    "paper_open": None,
                    "skip_reason": "INSUFFICIENT_SIGNALS",
                }
                self._last_result = result
                return result

            symbol_open_positions = self.paper.list_open_positions(tick.symbol)
            all_open_positions = self.paper.list_open_positions()
            portfolio = self._portfolio_state(
                all_open_positions,
                tick.symbol,
            )

            decision = self.brain.decide(
                tick.symbol,
                signals,
                portfolio=portfolio,
                context={
                    **regime_ctx,
                    "news_policy": news_policy,
                },
            )
            self._metrics["decisions"] += 1

            opened = None
            if decision.action != Action.FLAT and decision.risk_budget_fraction > 0:
                if (
                    self.config.one_open_position_per_symbol
                    and symbol_open_positions
                ):
                    self._metrics["skipped_existing_position"] += 1
                else:
                    opened = self.paper.open_from_decision(
                        decision.decision_id,
                        tick,
                        equity=self.config.default_equity,
                        stop_distance_bps=self.config.default_stop_distance_bps,
                    )
                    self._metrics["paper_opens"] += 1

            result = {
                "enabled": True,
                "symbol": tick.symbol,
                "regime": regime_ctx,
                "news_policy": news_policy,
                "signals": [s.to_dict() for s in signals],
                "decision": decision.to_dict(),
                "paper_open": opened,
                "lifecycle": lifecycle_result,
                "learned_outcomes": learned,
            }
            self._last_result = result
            return result

    def _portfolio_state(
        self,
        all_open_positions: list[dict[str, Any]],
        symbol: str,
    ) -> PortfolioState:
        equity = max(1.0, float(self.config.default_equity))
        gross_notional = sum(
            float(p["notional"]) for p in all_open_positions
        )
        symbol_notional = sum(
            float(p["notional"])
            for p in all_open_positions
            if str(p["symbol"]).upper() == symbol.upper()
        )
        return PortfolioState(
            equity=equity,
            gross_exposure_fraction=gross_notional / equity,
            symbol_exposure_fraction=symbol_notional / equity,
        )

    def learn_from_closed_actions(
        self,
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        learned: list[dict[str, Any]] = []
        for action in actions:
            position = self.paper.get_position(action["position_id"])
            if not position:
                continue
            decision_id = position["decision_id"]
            if self.brain.memory.has_outcome(decision_id):
                continue

            entry = float(position["entry_price"])
            close_price = float(position["close_price"])
            if entry <= 0:
                continue

            market_move_bps = (close_price - entry) / entry * 10000.0
            notional = max(1e-12, float(position["notional"]))
            pnl_bps = float(position["realized_pnl"]) / notional * 10000.0

            weights = self.brain.record_outcome(
                decision_id=decision_id,
                market_move_bps=market_move_bps,
                pnl_bps=pnl_bps,
                metadata={
                    "paper": True,
                    "paper_position_id": position["position_id"],
                    "paper_exit_reason": action["action"],
                },
            )
            self._metrics["paper_closes"] += 1
            self._metrics["outcomes_recorded"] += 1
            learned.append({
                "decision_id": decision_id,
                "position_id": position["position_id"],
                "market_move_bps": market_move_bps,
                "pnl_bps": pnl_bps,
                "expert_weights": weights,
            })
        return learned

    def status(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "metrics": dict(self._metrics),
            "last_result": self._last_result,
        }
