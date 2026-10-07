from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .ensemble import AdaptiveExpertEnsemble, EnsembleConfig
from .memory import BrainMemory
from .research_bridge import ResearchBridge
from .risk import RiskConfig, RiskGovernor
from .schema import (
    Action,
    BrainDecision,
    BrainMode,
    EvidenceItem,
    ExpertSignal,
    Observation,
    PortfolioState,
    SourceType,
    utcnow_iso,
)


@dataclass(frozen=True)
class BrainConfig:
    mode: BrainMode = BrainMode.PAPER
    decision_threshold: float = 0.18
    min_experts: int = 2


class AdaptiveBrain:
    VERSION = "0.2.0"

    def __init__(
        self,
        memory: BrainMemory,
        research_bridge: ResearchBridge,
        config: BrainConfig | None = None,
        risk_config: RiskConfig | None = None,
        ensemble_config: EnsembleConfig | None = None,
    ):
        self.memory = memory
        self.research_bridge = research_bridge
        self.config = config or BrainConfig()
        self.risk = RiskGovernor(risk_config)
        self.ensemble = AdaptiveExpertEnsemble(memory, ensemble_config)
        self.risk.ensure_mode_allowed(self.config.mode)

    @classmethod
    def from_env(cls) -> "AdaptiveBrain":
        backend_root = Path(__file__).resolve().parents[1]
        data_dir = Path(os.environ.get("SUPERBOT_DATA_DIR", backend_root / "data"))
        db_path = os.environ.get(
            "SUPERBOT_BRAIN_DB_PATH",
            str(data_dir / "adaptive_brain" / "brain_memory.db"),
        )
        mode = BrainMode(os.environ.get("SUPERBOT_BRAIN_MODE", "PAPER").upper())
        return cls(
            memory=BrainMemory(db_path),
            research_bridge=ResearchBridge(backend_root),
            config=BrainConfig(mode=mode),
        )

    def ingest(self, observation: Observation) -> None:
        if not 0.0 <= observation.confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        self.memory.add_observation(observation)

    def ingest_evidence(self, item: EvidenceItem) -> bool:
        if not 0.0 <= item.confidence <= 1.0:
            raise ValueError("evidence confidence must be in [0,1]")
        return self.memory.add_evidence(item)

    def decide(
        self,
        symbol: str,
        signals: list[ExpertSignal],
        portfolio: PortfolioState | None = None,
        context: dict | None = None,
    ) -> BrainDecision:
        self.risk.ensure_mode_allowed(self.config.mode)
        symbol = symbol.strip().upper()
        portfolio = portfolio or PortfolioState()

        usable = [
            s for s in self.ensemble.usable(signals)
            if s.symbol.upper() == symbol
        ]
        rationale: list[str] = []
        normalized_weights: dict[str, float] = {}

        if len(usable) < self.config.min_experts:
            action = Action.FLAT
            score = 0.0
            confidence = 0.0
            rationale.append("INSUFFICIENT_FRESH_EXPERTS")
        else:
            score, normalized_weights = self.ensemble.score(usable)

            news_policy = (context or {}).get("news_policy") or {}
            if score > 0:
                score *= float(news_policy.get("long_weight_multiplier", 1.0))
            elif score < 0:
                score *= float(news_policy.get("short_weight_multiplier", 1.0))
            score = max(-1.0, min(1.0, score))
            confidence = abs(score)

            if news_policy.get("pause_new_entries"):
                action = Action.FLAT
                confidence = 0.0
                rationale.append("NEWS_PAUSE_NEW_ENTRIES")
            elif abs(score) < self.config.decision_threshold:
                action = Action.FLAT
                rationale.append("ENSEMBLE_BELOW_DECISION_THRESHOLD")
            else:
                action = Action.LONG if score > 0 else Action.SHORT
                rationale.append("ENSEMBLE_DIRECTION_ACCEPTED")

            if news_policy.get("force_exit"):
                action = Action.FLAT
                confidence = 0.0
                rationale.append("NEWS_FORCE_EXIT_CONTEXT")

        if context and context.get("regime"):
            rationale.append(f"REGIME={context['regime']}")
        if context and context.get("news_policy"):
            np = context["news_policy"]
            rationale.append(
                "NEWS_SENTIMENT="
                + str(round(float(np.get("sentiment_score", 0.0)), 4))
            )
            rationale.append(
                "NEWS_VOLATILITY="
                + str(round(float(np.get("volatility_score", 0.0)), 4))
            )

        risk_decision = self.risk.assess(action, confidence, score, portfolio)
        if not risk_decision.allowed:
            action = Action.FLAT
            confidence = 0.0
            risk_budget = 0.0
            rationale.extend(risk_decision.reasons)
        else:
            risk_budget = risk_decision.risk_budget_fraction
            news_policy = (context or {}).get("news_policy") or {}
            if risk_budget > 0:
                size_multiplier = max(
                    0.0,
                    min(1.0, float(news_policy.get("size_multiplier", 1.0))),
                )
                risk_budget *= size_multiplier
                if size_multiplier < 1.0:
                    rationale.append(
                        f"NEWS_SIZE_MULTIPLIER={round(size_multiplier, 4)}"
                    )
            rationale.extend(risk_decision.reasons)

        snapshot = []
        for signal in usable:
            d = signal.to_dict()
            d["ensemble_weight"] = normalized_weights.get(signal.expert_id, 0.0)
            snapshot.append(d)

        decision = BrainDecision(
            decision_id=str(uuid.uuid4()),
            created_at=utcnow_iso(),
            symbol=symbol,
            action=action,
            confidence=confidence,
            raw_score=score,
            risk_budget_fraction=risk_budget,
            mode=self.config.mode,
            rationale=rationale,
            expert_snapshot=snapshot,
        )
        self.memory.add_decision(decision)
        return decision

    def record_outcome(
        self,
        decision_id: str,
        market_move_bps: float,
        pnl_bps: float,
        max_adverse_excursion_bps: float | None = None,
        metadata: dict | None = None,
    ) -> dict[str, float]:
        decision = self.memory.get_decision(decision_id)
        if decision is None:
            raise KeyError(f"unknown decision_id: {decision_id}")

        raw_signals = json.loads(decision["expert_snapshot_json"])
        signals = [
            ExpertSignal(
                expert_id=s["expert_id"],
                source_type=SourceType(s["source_type"]),
                symbol=s["symbol"],
                direction=float(s["direction"]),
                confidence=float(s["confidence"]),
                expected_edge_bps=float(s.get("expected_edge_bps", 0.0)),
                observed_at=s["observed_at"],
                ttl_seconds=int(s.get("ttl_seconds", 120)),
                metadata=dict(s.get("metadata") or {}),
            )
            for s in raw_signals
        ]

        self.memory.add_outcome(
            decision_id=decision_id,
            market_move_bps=market_move_bps,
            pnl_bps=pnl_bps,
            max_adverse_excursion_bps=max_adverse_excursion_bps,
            metadata=metadata,
        )

        for signal in signals:
            product = signal.direction * float(market_move_bps)
            correct = None if product == 0 else product > 0
            self.memory.touch_source(
                signal.expert_id,
                signal.source_type.value,
                correct=correct,
            )

        return self.ensemble.learn(signals, market_move_bps)

    def status(self) -> dict:
        return {
            "version": self.VERSION,
            "mode": self.config.mode.value,
            "live_execution_enabled": False,
            "decision_threshold": self.config.decision_threshold,
            "min_experts": self.config.min_experts,
            "expert_weights": self.memory.weights(),
            "learning_metrics": self.memory.metrics(),
            "research_bridge": self.research_bridge.snapshot(),
            "safety": {
                "learner_can_change_risk_limits": False,
                "learner_can_place_orders": False,
                "research_console_mutation": False,
                "private_exchange_api_present": False,
            },
        }
