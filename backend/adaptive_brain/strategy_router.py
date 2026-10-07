from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .drift import DriftMonitor
from .schema import ExpertSignal
from .source_reputation import SourceReputationEngine


@dataclass(frozen=True)
class RouterConfig:
    min_multiplier: float = 0.50
    max_multiplier: float = 1.25
    regime_match_multiplier: float = 1.08
    regime_mismatch_multiplier: float = 0.90


class StrategyRouter:
    """Applies bounded source-quality and regime context before the ensemble."""

    def __init__(
        self,
        reputation: SourceReputationEngine,
        drift: DriftMonitor,
        config: RouterConfig | None = None,
    ):
        self.reputation = reputation
        self.drift = drift
        self.config = config or RouterConfig()

    def route(
        self,
        signals: list[ExpertSignal],
        regime: str | None,
    ) -> tuple[list[ExpertSignal], list[dict[str, Any]]]:
        adjusted: list[ExpertSignal] = []
        audit: list[dict[str, Any]] = []

        for raw in signals:
            s = raw.normalized()
            rep = self.reputation.assess(s.expert_id)
            drift = self.drift.assess(s.expert_id)

            multiplier = rep.weight_multiplier * drift.weight_multiplier
            regime_factor = 1.0
            affinities = s.metadata.get("regime_affinity") or []
            if isinstance(affinities, str):
                affinities = [affinities]
            affinities = [str(x).upper() for x in affinities]

            if regime and affinities:
                if str(regime).upper() in affinities:
                    regime_factor = self.config.regime_match_multiplier
                else:
                    regime_factor = self.config.regime_mismatch_multiplier
                multiplier *= regime_factor

            multiplier = max(
                self.config.min_multiplier,
                min(self.config.max_multiplier, multiplier),
            )
            new_conf = max(0.0, min(1.0, s.confidence * multiplier))
            md = dict(s.metadata)
            md.update({
                "router_original_confidence": s.confidence,
                "router_multiplier": multiplier,
                "source_reputation_multiplier": rep.weight_multiplier,
                "drift_multiplier": drift.weight_multiplier,
                "regime_multiplier": regime_factor,
                "drift_status": drift.status,
            })

            adjusted.append(
                ExpertSignal(
                    expert_id=s.expert_id,
                    source_type=s.source_type,
                    symbol=s.symbol,
                    direction=s.direction,
                    confidence=new_conf,
                    expected_edge_bps=s.expected_edge_bps,
                    observed_at=s.observed_at,
                    ttl_seconds=s.ttl_seconds,
                    metadata=md,
                )
            )
            audit.append({
                "expert_id": s.expert_id,
                "original_confidence": s.confidence,
                "adjusted_confidence": new_conf,
                "multiplier": multiplier,
                "reputation": rep.to_dict(),
                "drift": drift.to_dict(),
                "regime": regime,
            })

        return adjusted, audit
