from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .drift import DriftMonitor
from .memory import BrainMemory
from .source_reputation import SourceReputationEngine
from .schema import utcnow_iso


@dataclass(frozen=True)
class PromotionConfig:
    min_challenger_samples: int = 30
    min_quality_advantage: float = 0.05
    min_reward_advantage: float = 0.05


@dataclass(frozen=True)
class PromotionDecision:
    allowed: bool
    challenger_expert_id: str
    champion_expert_id: str | None
    reasons: list[str]
    challenger_reputation: dict[str, Any]
    champion_reputation: dict[str, Any] | None
    challenger_drift: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelRegistry:
    """Versioned champion/challenger registry.

    Promotion decisions are data-driven and auditable. v0.4 never rewrites
    source code or downloads arbitrary models.
    """

    ROLES = {"CHAMPION", "CHALLENGER", "INACTIVE"}

    def __init__(
        self,
        memory: BrainMemory,
        reputation: SourceReputationEngine,
        drift: DriftMonitor,
        config: PromotionConfig | None = None,
    ):
        self.memory = memory
        self.reputation = reputation
        self.drift = drift
        self.config = config or PromotionConfig()

    @staticmethod
    def expert_id(model_id: str, version: str) -> str:
        return f"{model_id.strip()}@{version.strip()}"

    def register(
        self,
        model_id: str,
        version: str,
        role: str = "CHALLENGER",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        role = role.upper()
        if role not in self.ROLES:
            raise ValueError(f"unsupported role: {role}")
        expert_id = self.expert_id(model_id, version)
        now = utcnow_iso()
        with self.memory._connect() as c:
            if role == "CHAMPION":
                c.execute(
                    "UPDATE model_registry SET role='CHALLENGER' WHERE role='CHAMPION'"
                )
            c.execute(
                """INSERT INTO model_registry(
                   expert_id,model_id,version,role,enabled,created_at,promoted_at,
                   metadata_json
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(expert_id) DO UPDATE SET
                   role=excluded.role,
                   enabled=excluded.enabled,
                   metadata_json=excluded.metadata_json""",
                (
                    expert_id,
                    model_id.strip(),
                    version.strip(),
                    role,
                    0 if role == "INACTIVE" else 1,
                    now,
                    now if role == "CHAMPION" else None,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
        return self.get(expert_id)

    def get(self, expert_id: str) -> dict[str, Any] | None:
        with self.memory._connect() as c:
            row = c.execute(
                "SELECT * FROM model_registry WHERE expert_id=?",
                (expert_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        return d

    def list(self) -> list[dict[str, Any]]:
        with self.memory._connect() as c:
            rows = c.execute(
                "SELECT * FROM model_registry ORDER BY role,model_id,version"
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["enabled"] = bool(d["enabled"])
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            out.append(d)
        return out

    def current_champion(self) -> dict[str, Any] | None:
        with self.memory._connect() as c:
            row = c.execute(
                """SELECT * FROM model_registry
                   WHERE role='CHAMPION' AND enabled=1
                   ORDER BY promoted_at DESC LIMIT 1"""
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        return d

    def evaluate(
        self,
        challenger_expert_id: str,
        champion_expert_id: str | None = None,
    ) -> PromotionDecision:
        challenger_entry = self.get(challenger_expert_id)
        challenger = self.reputation.assess(challenger_expert_id)
        drift = self.drift.assess(challenger_expert_id)
        champion_id = champion_expert_id
        if champion_id is None:
            champion = self.current_champion()
            champion_id = champion["expert_id"] if champion else None

        champion_rep = (
            self.reputation.assess(champion_id) if champion_id else None
        )

        reasons: list[str] = []
        if challenger_entry is None:
            reasons.append("CHALLENGER_NOT_REGISTERED")
        elif not challenger_entry["enabled"]:
            reasons.append("CHALLENGER_DISABLED")
        elif challenger_entry["role"] not in {"CHALLENGER", "CHAMPION"}:
            reasons.append("CHALLENGER_ROLE_NOT_ELIGIBLE")

        if challenger.samples < self.config.min_challenger_samples:
            reasons.append("INSUFFICIENT_CHALLENGER_SAMPLES")
        if drift.drift_detected:
            reasons.append("CHALLENGER_DRIFT_DETECTED")

        if champion_rep is not None:
            if (
                challenger.quality_score
                < champion_rep.quality_score + self.config.min_quality_advantage
            ):
                reasons.append("QUALITY_ADVANTAGE_NOT_MET")
            c_reward = challenger.avg_reward or 0.0
            p_reward = champion_rep.avg_reward or 0.0
            if c_reward < p_reward + self.config.min_reward_advantage:
                reasons.append("REWARD_ADVANTAGE_NOT_MET")
        else:
            if challenger.quality_score < 0.55:
                reasons.append("MINIMUM_QUALITY_NOT_MET")

        return PromotionDecision(
            allowed=not reasons,
            challenger_expert_id=challenger_expert_id,
            champion_expert_id=champion_id,
            reasons=reasons or ["PROMOTION_CRITERIA_MET"],
            challenger_reputation=challenger.to_dict(),
            champion_reputation=champion_rep.to_dict() if champion_rep else None,
            challenger_drift=drift.to_dict(),
        )

    def promote(
        self,
        challenger_expert_id: str,
        champion_expert_id: str | None = None,
    ) -> dict[str, Any]:
        decision = self.evaluate(challenger_expert_id, champion_expert_id)
        if not decision.allowed:
            raise ValueError(";".join(decision.reasons))

        challenger = self.get(challenger_expert_id)
        if challenger is None:
            raise KeyError(f"unregistered challenger: {challenger_expert_id}")

        now = utcnow_iso()
        with self.memory._connect() as c:
            c.execute(
                "UPDATE model_registry SET role='CHALLENGER' WHERE role='CHAMPION'"
            )
            c.execute(
                """UPDATE model_registry SET
                   role='CHAMPION',enabled=1,promoted_at=?
                   WHERE expert_id=?""",
                (now, challenger_expert_id),
            )
        return {
            "decision": decision.to_dict(),
            "champion": self.get(challenger_expert_id),
        }
