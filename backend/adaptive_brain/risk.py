from __future__ import annotations
from dataclasses import dataclass
from .schema import Action, BrainMode, PortfolioState

@dataclass(frozen=True)
class RiskConfig:
    max_risk_budget_per_trade: float = 0.0025
    max_daily_loss_fraction: float = 0.01
    max_drawdown_fraction: float = 0.05
    max_gross_exposure_fraction: float = 0.25
    max_symbol_exposure_fraction: float = 0.08
    min_confidence: float = 0.20

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    risk_budget_fraction: float
    reasons: list[str]

class RiskGovernor:
    def __init__(self, config: RiskConfig|None=None): self.config=config or RiskConfig()

    def ensure_mode_allowed(self, mode: BrainMode):
        if mode == BrainMode.LIVE:
            raise RuntimeError("LIVE execution disabled in Adaptive Brain v0; PAPER/RESEARCH only.")

    def assess(self, action: Action, confidence: float, raw_score: float, state: PortfolioState) -> RiskDecision:
        if action == Action.FLAT: return RiskDecision(True,0.0,["ABSTAIN"])
        reasons=[]
        if confidence < self.config.min_confidence: reasons.append("CONFIDENCE_BELOW_RISK_MINIMUM")
        if state.daily_pnl_fraction <= -self.config.max_daily_loss_fraction: reasons.append("DAILY_LOSS_LIMIT")
        if state.drawdown_fraction >= self.config.max_drawdown_fraction: reasons.append("DRAWDOWN_LIMIT")
        if state.gross_exposure_fraction >= self.config.max_gross_exposure_fraction: reasons.append("GROSS_EXPOSURE_LIMIT")
        if abs(state.symbol_exposure_fraction) >= self.config.max_symbol_exposure_fraction: reasons.append("SYMBOL_EXPOSURE_LIMIT")
        if reasons: return RiskDecision(False,0.0,reasons)
        budget=self.config.max_risk_budget_per_trade*confidence*min(1.0,abs(raw_score))
        return RiskDecision(True,max(0.0,min(self.config.max_risk_budget_per_trade,budget)),["RISK_GATE_PASS"])
