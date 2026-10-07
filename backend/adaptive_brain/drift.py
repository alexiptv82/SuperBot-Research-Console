from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .memory import BrainMemory


@dataclass(frozen=True)
class DriftConfig:
    recent_window: int = 20
    baseline_window: int = 60
    min_recent: int = 10
    min_baseline: int = 20
    accuracy_drop_threshold: float = 0.18
    reward_drop_threshold: float = 0.30
    drift_multiplier: float = 0.65


@dataclass(frozen=True)
class DriftAssessment:
    source_id: str
    status: str
    recent_samples: int
    baseline_samples: int
    recent_accuracy: float | None
    baseline_accuracy: float | None
    recent_reward: float | None
    baseline_reward: float | None
    accuracy_drop: float | None
    reward_drop: float | None
    drift_detected: bool
    weight_multiplier: float
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriftMonitor:
    """Compares recent source performance with its own older baseline."""

    def __init__(
        self,
        memory: BrainMemory,
        config: DriftConfig | None = None,
    ):
        self.memory = memory
        self.config = config or DriftConfig()

    @staticmethod
    def _stats(rows: list[dict]) -> tuple[float | None, float | None]:
        labeled = [r for r in rows if r["correct"] is not None]
        accuracy = (
            sum(int(r["correct"]) for r in labeled) / len(labeled)
            if labeled
            else None
        )
        reward = (
            sum(float(r["reward"]) for r in rows) / len(rows)
            if rows
            else None
        )
        return accuracy, reward

    def assess(self, source_id: str) -> DriftAssessment:
        rows = self.memory.source_outcomes(source_id, newest_first=True)
        recent = rows[: self.config.recent_window]
        baseline = rows[
            self.config.recent_window:
            self.config.recent_window + self.config.baseline_window
        ]

        recent_acc, recent_reward = self._stats(recent)
        baseline_acc, baseline_reward = self._stats(baseline)

        if (
            len(recent) < self.config.min_recent
            or len(baseline) < self.config.min_baseline
        ):
            return DriftAssessment(
                source_id=source_id,
                status="INSUFFICIENT_DATA",
                recent_samples=len(recent),
                baseline_samples=len(baseline),
                recent_accuracy=recent_acc,
                baseline_accuracy=baseline_acc,
                recent_reward=recent_reward,
                baseline_reward=baseline_reward,
                accuracy_drop=None,
                reward_drop=None,
                drift_detected=False,
                weight_multiplier=1.0,
                reasons=["INSUFFICIENT_HISTORY"],
            )

        accuracy_drop = (
            (baseline_acc - recent_acc)
            if baseline_acc is not None and recent_acc is not None
            else None
        )
        reward_drop = (
            (baseline_reward - recent_reward)
            if baseline_reward is not None and recent_reward is not None
            else None
        )

        reasons: list[str] = []
        if (
            accuracy_drop is not None
            and accuracy_drop >= self.config.accuracy_drop_threshold
        ):
            reasons.append("RECENT_ACCURACY_DEGRADATION")
        if (
            reward_drop is not None
            and reward_drop >= self.config.reward_drop_threshold
        ):
            reasons.append("RECENT_REWARD_DEGRADATION")

        drift = bool(reasons)
        return DriftAssessment(
            source_id=source_id,
            status="DRIFT" if drift else "STABLE",
            recent_samples=len(recent),
            baseline_samples=len(baseline),
            recent_accuracy=recent_acc,
            baseline_accuracy=baseline_acc,
            recent_reward=recent_reward,
            baseline_reward=baseline_reward,
            accuracy_drop=accuracy_drop,
            reward_drop=reward_drop,
            drift_detected=drift,
            weight_multiplier=self.config.drift_multiplier if drift else 1.0,
            reasons=reasons or ["NO_MATERIAL_DRIFT"],
        )

    def all(self) -> list[dict[str, Any]]:
        return [
            self.assess(source_id).to_dict()
            for source_id in sorted(self.memory.source_outcome_sources())
        ]
