from __future__ import annotations

from .advisers import AdviserContext
from .schema import ExpertSignal, SourceType


class RegimeMomentumAdviser:
    adviser_id = "regime-momentum"
    version = "1.0"

    async def advise(self, context: AdviserContext) -> ExpertSignal | None:
        regime = (context.regime or "").upper()
        market = context.market or {}
        momentum_bps = market.get("momentum_bps")
        if momentum_bps is None:
            return None

        if regime == "TREND_UP":
            direction = 1.0
        elif regime == "TREND_DOWN":
            direction = -1.0
        else:
            return None

        confidence = min(0.65, 0.25 + abs(float(momentum_bps)) / 300.0)
        return ExpertSignal(
            expert_id=f"{self.adviser_id}@{self.version}",
            source_type=SourceType.MODEL,
            symbol=context.symbol,
            direction=direction,
            confidence=confidence,
            expected_edge_bps=min(30.0, abs(float(momentum_bps)) * 0.15),
            metadata={
                "regime_affinity": [regime],
                "adviser_type": "deterministic-baseline",
            },
        )


class NewsPolicyAdviser:
    adviser_id = "news-policy"
    version = "1.0"

    async def advise(self, context: AdviserContext) -> ExpertSignal | None:
        policy = context.news_policy or {}
        if policy.get("pause_new_entries") or policy.get("force_exit"):
            return None

        sentiment = float(policy.get("sentiment_score", 0.0))
        if abs(sentiment) < 0.35:
            return None

        confidence = min(0.70, 0.30 + abs(sentiment) * 0.40)
        return ExpertSignal(
            expert_id=f"{self.adviser_id}@{self.version}",
            source_type=SourceType.MODEL,
            symbol=context.symbol,
            direction=1.0 if sentiment > 0 else -1.0,
            confidence=confidence,
            expected_edge_bps=abs(sentiment) * 20.0,
            metadata={
                "adviser_type": "deterministic-baseline",
                "news_sentiment": sentiment,
            },
        )
