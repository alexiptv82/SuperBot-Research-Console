from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .memory import BrainMemory
from .schema import ExpertSignal


@dataclass(frozen=True)
class EnsembleConfig:
    learning_rate: float = 0.18
    reward_scale_bps: float = 25.0
    min_weight: float = 0.10
    max_weight: float = 10.0
    edge_scale_bps: float = 20.0


class AdaptiveExpertEnsemble:
    def __init__(self, memory: BrainMemory, config: EnsembleConfig | None = None):
        self.memory = memory
        self.config = config or EnsembleConfig()

    @staticmethod
    def _fresh(signal: ExpertSignal, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            ts = datetime.fromisoformat(signal.observed_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        age = (now - ts).total_seconds()
        return 0 <= age <= signal.ttl_seconds

    def usable(self, signals: list[ExpertSignal]) -> list[ExpertSignal]:
        out: list[ExpertSignal] = []
        for raw in signals:
            signal = raw.normalized()
            if signal.expert_id and self._fresh(signal):
                out.append(signal)
        return out

    def score(self, signals: list[ExpertSignal]) -> tuple[float, dict[str, float]]:
        usable = self.usable(signals)
        if not usable:
            return 0.0, {}

        raw_weights = {
            s.expert_id: max(
                self.config.min_weight,
                min(self.config.max_weight, self.memory.get_weight(s.expert_id)),
            )
            for s in usable
        }
        denom = sum(raw_weights.values())
        normalized = {k: v / denom for k, v in raw_weights.items()}

        total = 0.0
        for signal in usable:
            edge_factor = 1.0
            if signal.expected_edge_bps:
                edge_factor = max(
                    0.25,
                    math.tanh(abs(signal.expected_edge_bps) / self.config.edge_scale_bps),
                )
            total += (
                normalized[signal.expert_id]
                * signal.direction
                * signal.confidence
                * edge_factor
            )
        return max(-1.0, min(1.0, total)), normalized

    def learn(
        self,
        decision_snapshot_signals: list[ExpertSignal],
        market_move_bps: float,
    ) -> dict[str, float]:
        """Learn from the immutable decision-time snapshot.

        Freshness is intentionally NOT re-evaluated here. A signal that was
        fresh when a decision was created must still receive credit/blame when
        its outcome becomes known later.
        """
        for raw in decision_snapshot_signals:
            signal = raw.normalized()
            if not signal.expert_id:
                continue
            old = self.memory.get_weight(signal.expert_id)
            reward = max(
                -1.0,
                min(
                    1.0,
                    (signal.direction * float(market_move_bps))
                    / self.config.reward_scale_bps,
                ),
            ) * signal.confidence
            new = old * math.exp(self.config.learning_rate * reward)
            self.memory.set_weight(
                signal.expert_id,
                max(self.config.min_weight, min(self.config.max_weight, new)),
            )
        return self.memory.weights()
