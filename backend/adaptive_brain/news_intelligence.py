from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .memory import BrainMemory
from .schema import EvidenceItem, utcnow_iso


@dataclass(frozen=True)
class NewsAssessment:
    fingerprint: str
    asset: str
    sentiment_score: float
    sentiment_label: str
    event_type: str
    severity: str
    volatility_score: float
    horizon_minutes: int
    confidence: float
    created_at: str
    rationale: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NewsPolicy:
    asset: str
    sentiment_score: float
    volatility_score: float
    size_multiplier: float
    long_weight_multiplier: float
    short_weight_multiplier: float
    pause_new_entries: bool
    force_exit: bool
    critical_events: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class NewsIntelligenceEngine:
    """Deterministic first-stage news intelligence.

    It classifies events/sentiment and creates bounded risk-policy context.
    A future model-based semantic classifier can replace/augment the scorer
    without changing the hard risk policy contract.
    """

    POSITIVE = {
        "approval": 2.0, "approved": 2.0, "adoption": 1.5, "partnership": 1.0,
        "surge": 1.0, "record high": 1.5, "upgrade": 1.0, "inflows": 1.0,
        "rate cut": 1.5, "cuts rates": 1.5, "easing": 1.0, "settlement": 0.5,
        "launch": 0.5, "profit": 0.5, "beats estimates": 1.0,
    }
    NEGATIVE = {
        "hack": -2.5, "hacked": -2.5, "exploit": -2.5, "breach": -2.0,
        "drain": -2.0, "lawsuit": -1.5, "sues": -1.5, "charges": -1.0,
        "ban": -2.0, "banned": -2.0, "crackdown": -1.5, "outflows": -1.0,
        "liquidation": -1.0, "bankruptcy": -2.5, "insolvency": -2.5,
        "downgrade": -1.0, "rate hike": -1.5, "raises rates": -1.5,
        "higher rates": -1.0, "misses estimates": -1.0, "fraud": -2.0,
    }

    ASSET_ALIASES = {
        "BTCUSDT": ("bitcoin", "btc"),
        "ETHUSDT": ("ethereum", "ether", "eth"),
        "SOLUSDT": ("solana", "sol"),
        "XRPUSDT": ("xrp", "ripple"),
        "BNBUSDT": ("bnb", "binance coin"),
        "ADAUSDT": ("cardano", "ada"),
        "DOGEUSDT": ("dogecoin", "doge"),
        "AVAXUSDT": ("avalanche", "avax"),
        "LINKUSDT": ("chainlink", "link"),
    }

    def __init__(
        self,
        memory: BrainMemory,
        tracked_assets: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
        lookback_minutes: int = 180,
    ):
        self.memory = memory
        self.tracked_assets = tuple(a.upper() for a in tracked_assets)
        self.lookback_minutes = lookback_minutes

    @staticmethod
    def _label(score: float) -> str:
        if score >= 0.25:
            return "POSITIVE"
        if score <= -0.25:
            return "NEGATIVE"
        return "NEUTRAL"

    def _assets(self, text: str, event_type: str) -> list[str]:
        text = text.lower()
        found: list[str] = []
        for symbol in self.tracked_assets:
            base = symbol
            for quote in ("USDT", "USD", "USDC"):
                if base.endswith(quote):
                    base = base[: -len(quote)]
                    break
            aliases = set(self.ASSET_ALIASES.get(symbol, ()))
            aliases.add(base.lower())
            aliases.add(symbol.lower())
            for alias in aliases:
                if len(alias) <= 3:
                    if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text):
                        found.append(symbol)
                        break
                elif alias in text:
                    found.append(symbol)
                    break

        if found:
            return sorted(set(found))

        if event_type in {
            "FED_RATES", "SEC_REGULATION", "REGULATION",
            "MACRO", "SYSTEMIC_HACK", "MARKET_STRUCTURE",
        }:
            return list(self.tracked_assets)

        return ["GLOBAL"]

    @staticmethod
    def _event_type(text: str) -> tuple[str, str, int, list[str]]:
        t = text.lower()
        reasons: list[str] = []
        if any(k in t for k in ("hack", "hacked", "exploit", "breach", "drain")):
            reasons.append("SECURITY_EVENT")
            if any(k in t for k in ("exchange", "bridge", "protocol")):
                return "SYSTEMIC_HACK", "CRITICAL", 240, reasons
            return "HACK_EXPLOIT", "CRITICAL", 240, reasons

        if any(k in t for k in ("fomc", "federal reserve", "fed ")) and any(
            k in t for k in ("rate", "interest", "basis point", "inflation")
        ):
            reasons.append("FED_MONETARY_POLICY")
            return "FED_RATES", "HIGH", 360, reasons

        if "sec" in t and any(
            k in t for k in (
                "crypto", "etf", "token", "exchange", "custody",
                "securities law", "enforcement", "lawsuit", "charges",
            )
        ):
            reasons.append("SEC_CRYPTO_REGULATORY")
            return "SEC_REGULATION", "HIGH", 720, reasons

        if any(k in t for k in ("regulation", "regulator", "legislation", "ban", "license")):
            reasons.append("REGULATORY_EVENT")
            return "REGULATION", "HIGH", 720, reasons

        if any(k in t for k in ("earnings", "revenue", "profit", "guidance", "eps")):
            reasons.append("EARNINGS_EVENT")
            return "EARNINGS", "MEDIUM", 240, reasons

        if any(k in t for k in ("etf", "inflows", "outflows")):
            reasons.append("ETF_FLOW_EVENT")
            return "ETF", "MEDIUM", 240, reasons

        if any(k in t for k in ("cpi", "jobs report", "payrolls", "gdp", "inflation")):
            reasons.append("MACRO_EVENT")
            return "MACRO", "HIGH", 360, reasons

        if any(k in t for k in ("listing", "delisting", "market structure", "trading halt")):
            reasons.append("MARKET_STRUCTURE_EVENT")
            return "MARKET_STRUCTURE", "MEDIUM", 180, reasons

        return "GENERAL_NEWS", "LOW", 120, reasons

    def _sentiment(self, text: str, event_type: str) -> tuple[float, list[str]]:
        t = text.lower()
        raw = 0.0
        reasons: list[str] = []
        for phrase, weight in self.POSITIVE.items():
            if phrase in t:
                raw += weight
                reasons.append(f"POS:{phrase}")
        for phrase, weight in self.NEGATIVE.items():
            if phrase in t:
                raw += weight
                reasons.append(f"NEG:{phrase}")

        if event_type in ("SYSTEMIC_HACK", "HACK_EXPLOIT"):
            raw = min(raw, -2.5)
        if event_type == "FED_RATES":
            if "rate cut" in t or "cuts rates" in t:
                raw += 1.5
            if "rate hike" in t or "raises rates" in t:
                raw -= 1.5
        score = math.tanh(raw / 3.0)
        return max(-1.0, min(1.0, score)), reasons

    @staticmethod
    def _volatility_score(severity: str, event_type: str) -> float:
        base = {"LOW": 0.20, "MEDIUM": 0.45, "HIGH": 0.75, "CRITICAL": 1.0}[severity]
        if event_type in ("FED_RATES", "SYSTEMIC_HACK", "HACK_EXPLOIT"):
            base = max(base, 0.90)
        return min(1.0, base)

    def assess(self, item: EvidenceItem) -> list[NewsAssessment]:
        text = f"{item.title}\n{item.body}"
        event_type, severity, horizon, event_reasons = self._event_type(text)
        sentiment, sentiment_reasons = self._sentiment(text, event_type)
        confidence = max(0.0, min(1.0, float(item.confidence)))
        fingerprint = self.memory.evidence_fingerprint(item)
        assets = self._assets(text, event_type)
        created_at = utcnow_iso()
        volatility = self._volatility_score(severity, event_type)

        assessments = [
            NewsAssessment(
                fingerprint=fingerprint,
                asset=asset,
                sentiment_score=sentiment,
                sentiment_label=self._label(sentiment),
                event_type=event_type,
                severity=severity,
                volatility_score=volatility,
                horizon_minutes=horizon,
                confidence=confidence,
                created_at=created_at,
                rationale=event_reasons + sentiment_reasons,
            )
            for asset in assets
        ]
        for assessment in assessments:
            self.memory.add_news_assessment(assessment.to_dict())
        return assessments

    def policy_for(self, asset: str) -> NewsPolicy:
        asset = asset.upper()
        rows = self.memory.recent_news_assessments(
            asset=asset,
            lookback_minutes=self.lookback_minutes,
        )
        if not rows:
            return NewsPolicy(
                asset=asset,
                sentiment_score=0.0,
                volatility_score=0.0,
                size_multiplier=1.0,
                long_weight_multiplier=1.0,
                short_weight_multiplier=1.0,
                pause_new_entries=False,
                force_exit=False,
                critical_events=[],
            )

        now = datetime.now(timezone.utc)
        sentiment_num = 0.0
        weight_sum = 0.0
        volatility = 0.0
        critical_events: list[str] = []
        force_exit = False

        for row in rows:
            try:
                ts = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = now
            age_min = max(0.0, (now - ts).total_seconds() / 60.0)
            horizon = max(30.0, float(row["horizon_minutes"]))
            decay = math.exp(-age_min / horizon)
            confidence = float(row["confidence"])
            w = max(0.05, confidence) * decay

            sentiment_num += float(row["sentiment_score"]) * w
            weight_sum += w
            volatility = max(
                volatility,
                float(row["volatility_score"]) * decay,
            )

            if row["severity"] == "CRITICAL" and decay > 0.35:
                critical_events.append(str(row["event_type"]))
                if float(row["sentiment_score"]) <= -0.50:
                    force_exit = True

        sentiment = sentiment_num / weight_sum if weight_sum else 0.0
        size_multiplier = max(0.15, 1.0 - 0.70 * volatility)
        pause = bool(critical_events) or volatility >= 0.92

        long_mult = 1.0
        short_mult = 1.0
        if sentiment >= 0.35:
            long_mult = min(1.25, 1.0 + 0.25 * sentiment)
            short_mult = max(0.80, 1.0 - 0.20 * sentiment)
        elif sentiment <= -0.35:
            short_mult = min(1.25, 1.0 + 0.25 * abs(sentiment))
            long_mult = max(0.80, 1.0 - 0.20 * abs(sentiment))

        return NewsPolicy(
            asset=asset,
            sentiment_score=max(-1.0, min(1.0, sentiment)),
            volatility_score=max(0.0, min(1.0, volatility)),
            size_multiplier=size_multiplier,
            long_weight_multiplier=long_mult,
            short_weight_multiplier=short_mult,
            pause_new_entries=pause,
            force_exit=force_exit,
            critical_events=sorted(set(critical_events)),
        )
