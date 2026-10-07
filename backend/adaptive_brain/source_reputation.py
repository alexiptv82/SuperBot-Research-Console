from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .memory import BrainMemory


@dataclass(frozen=True)
class ReputationConfig:
    half_life_hours: float = 168.0
    prior_strength: float = 8.0
    reward_scale_bps: float = 25.0
    min_multiplier: float = 0.60
    max_multiplier: float = 1.25
    sample_scale: float = 15.0


@dataclass(frozen=True)
class SourceReputation:
    source_id: str
    samples: int
    effective_samples: float
    accuracy: float | None
    avg_reward: float | None
    quality_score: float
    confidence: float
    weight_multiplier: float
    error_count: int
    last_seen_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceReputationEngine:
    """Time-decayed source quality with Bayesian shrinkage.

    New/small-sample sources stay close to neutral (multiplier ~= 1.0).
    Influence only moves materially after repeated observed outcomes.
    """

    def __init__(
        self,
        memory: BrainMemory,
        config: ReputationConfig | None = None,
    ):
        self.memory = memory
        self.config = config or ReputationConfig()

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def assess(self, source_id: str) -> SourceReputation:
        rows = self.memory.source_outcomes(source_id)
        registry = {
            row["source_id"]: row for row in self.memory.list_sources()
        }.get(source_id, {})

        if not rows:
            return SourceReputation(
                source_id=source_id,
                samples=0,
                effective_samples=0.0,
                accuracy=None,
                avg_reward=None,
                quality_score=0.5,
                confidence=0.0,
                weight_multiplier=1.0,
                error_count=int(registry.get("error_count") or 0),
                last_seen_at=registry.get("last_seen_at"),
            )

        now = datetime.now(timezone.utc)
        decay_lambda = math.log(2.0) / max(1e-9, self.config.half_life_hours)
        weighted_total = 0.0
        weighted_correct = 0.0
        weighted_reward = 0.0
        reward_weight = 0.0
        raw_correct = 0
        raw_labeled = 0

        for row in rows:
            age_h = max(
                0.0,
                (now - self._parse_ts(str(row["realized_at"]))).total_seconds()
                / 3600.0,
            )
            w = math.exp(-decay_lambda * age_h)
            weighted_total += w
            correct = row["correct"]
            if correct is not None:
                raw_labeled += 1
                raw_correct += int(correct)
                weighted_correct += w * float(correct)
            reward = float(row["reward"])
            weighted_reward += w * reward
            reward_weight += w

        prior = self.config.prior_strength
        bayes_accuracy = (
            weighted_correct + 0.5 * prior
        ) / (weighted_total + prior)

        avg_reward = (
            weighted_reward / reward_weight if reward_weight > 0 else 0.0
        )
        reward_quality = (math.tanh(avg_reward) + 1.0) / 2.0
        quality = 0.70 * bayes_accuracy + 0.30 * reward_quality

        error_count = int(registry.get("error_count") or 0)
        sample_count = len(rows)
        error_rate_proxy = error_count / max(1.0, sample_count + error_count)
        quality = max(0.0, min(1.0, quality - 0.15 * error_rate_proxy))

        confidence = 1.0 - math.exp(
            -weighted_total / max(1e-9, self.config.sample_scale)
        )
        raw_multiplier = 1.0 + (quality - 0.5) * 0.8
        shrunk_multiplier = 1.0 + (raw_multiplier - 1.0) * confidence
        multiplier = max(
            self.config.min_multiplier,
            min(self.config.max_multiplier, shrunk_multiplier),
        )

        return SourceReputation(
            source_id=source_id,
            samples=sample_count,
            effective_samples=weighted_total,
            accuracy=(raw_correct / raw_labeled) if raw_labeled else None,
            avg_reward=avg_reward,
            quality_score=quality,
            confidence=confidence,
            weight_multiplier=multiplier,
            error_count=error_count,
            last_seen_at=registry.get("last_seen_at"),
        )

    def all(self) -> list[dict[str, Any]]:
        source_ids = {row["source_id"] for row in self.memory.list_sources()}
        source_ids.update(self.memory.source_outcome_sources())
        return [
            self.assess(source_id).to_dict()
            for source_id in sorted(source_ids)
        ]
