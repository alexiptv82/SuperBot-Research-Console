from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import verify_session

from .brain import AdaptiveBrain
from .schema import EvidenceItem, ExpertSignal, Observation, PortfolioState, SourceType
from .service import BrainRuntimeService


router = APIRouter(prefix="/api/brain", tags=["adaptive-brain"])
brain = AdaptiveBrain.from_env()
runtime = BrainRuntimeService(brain)


def require_brain_auth(request: Request) -> str:
    return verify_session(request)


class ObservationBody(BaseModel):
    source_id: str
    source_type: SourceType
    symbol: str
    feature: str
    value: float
    confidence: float = Field(ge=0.0, le=1.0)
    ttl_seconds: int = Field(default=300, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceBody(BaseModel):
    source_id: str
    source_type: SourceType
    topic: str
    title: str
    body: str = ""
    url: str = ""
    published_at: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExpertSignalBody(BaseModel):
    expert_id: str
    source_type: SourceType
    symbol: str
    direction: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expected_edge_bps: float = 0.0
    ttl_seconds: int = Field(default=120, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PortfolioBody(BaseModel):
    equity: float = 1.0
    daily_pnl_fraction: float = 0.0
    drawdown_fraction: float = 0.0
    gross_exposure_fraction: float = 0.0
    symbol_exposure_fraction: float = 0.0


class DecisionBody(BaseModel):
    symbol: str
    signals: list[ExpertSignalBody]
    portfolio: PortfolioBody = Field(default_factory=PortfolioBody)


class OutcomeBody(BaseModel):
    decision_id: str
    market_move_bps: float
    pnl_bps: float
    max_adverse_excursion_bps: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperOpenBody(BaseModel):
    decision_id: str
    equity: float = Field(gt=0.0)
    stop_distance_bps: float = Field(gt=0.0)


class PaperCloseBody(BaseModel):
    position_id: str


@router.on_event("startup")
async def _brain_startup() -> None:
    await runtime.start()


@router.on_event("shutdown")
async def _brain_shutdown() -> None:
    await runtime.stop()


@router.get("/status")
def status(_: str = Depends(require_brain_auth)) -> dict:
    out = brain.status()
    out["runtime"] = runtime.status()
    return out


@router.post("/observe")
def observe(
    body: ObservationBody,
    _: str = Depends(require_brain_auth),
) -> dict:
    brain.ingest(
        Observation(
            source_id=body.source_id,
            source_type=body.source_type,
            symbol=body.symbol.upper(),
            feature=body.feature,
            value=body.value,
            confidence=body.confidence,
            ttl_seconds=body.ttl_seconds,
            metadata=body.metadata,
        )
    )
    return {"accepted": True}


@router.post("/evidence")
async def evidence(
    body: EvidenceBody,
    _: str = Depends(require_brain_auth),
) -> dict:
    item = EvidenceItem(
        source_id=body.source_id,
        source_type=body.source_type,
        topic=body.topic,
        title=body.title,
        body=body.body,
        url=body.url,
        published_at=body.published_at,
        confidence=body.confidence,
        metadata=body.metadata,
    )
    result = await runtime.ingest_news_item(item)
    return {"accepted": True, **result}


@router.post("/decide")
def decide(
    body: DecisionBody,
    _: str = Depends(require_brain_auth),
) -> dict:
    signals = [
        ExpertSignal(
            expert_id=s.expert_id,
            source_type=s.source_type,
            symbol=s.symbol.upper(),
            direction=s.direction,
            confidence=s.confidence,
            expected_edge_bps=s.expected_edge_bps,
            ttl_seconds=s.ttl_seconds,
            metadata=s.metadata,
        )
        for s in body.signals
    ]
    regime_context = runtime.regime.snapshot(body.symbol)
    news_policy = runtime.news.policy_for(body.symbol).to_dict()
    context = {
        **regime_context,
        "news_policy": news_policy,
    }
    return brain.decide(
        body.symbol,
        signals,
        PortfolioState(**body.portfolio.model_dump()),
        context=context,
    ).to_dict()


@router.post("/outcome")
def outcome(
    body: OutcomeBody,
    _: str = Depends(require_brain_auth),
) -> dict:
    try:
        weights = brain.record_outcome(
            decision_id=body.decision_id,
            market_move_bps=body.market_move_bps,
            pnl_bps=body.pnl_bps,
            max_adverse_excursion_bps=body.max_adverse_excursion_bps,
            metadata=body.metadata,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"accepted": True, "expert_weights": weights}


@router.get("/weights")
def weights(_: str = Depends(require_brain_auth)) -> dict:
    return {"expert_weights": brain.memory.weights()}


@router.get("/metrics")
def metrics(_: str = Depends(require_brain_auth)) -> dict:
    return brain.memory.metrics()


@router.get("/sources")
def sources(_: str = Depends(require_brain_auth)) -> dict:
    rows = brain.memory.list_sources()
    return {"count": len(rows), "sources": rows}


@router.get("/market")
def market(_: str = Depends(require_brain_auth)) -> dict:
    return runtime.market.status()


@router.get("/regime/{symbol}")
def regime(symbol: str, _: str = Depends(require_brain_auth)) -> dict:
    return runtime.regime.snapshot(symbol)


@router.get("/news/policy/{symbol}")
def news_policy(symbol: str, _: str = Depends(require_brain_auth)) -> dict:
    return runtime.news.policy_for(symbol).to_dict()


@router.get("/news/status")
def news_status(_: str = Depends(require_brain_auth)) -> dict:
    return {
        "tracked_assets": list(runtime.news.tracked_assets),
        "policies": {
            symbol: runtime.news.policy_for(symbol).to_dict()
            for symbol in runtime.news.tracked_assets
        },
        "official_default_sources": [
            "SEC press releases RSS",
            "Federal Reserve all press releases RSS",
            "Federal Reserve monetary policy RSS",
        ],
    }


@router.get("/runtime")
def runtime_status(_: str = Depends(require_brain_auth)) -> dict:
    return runtime.status()


@router.post("/paper/open")
def paper_open(
    body: PaperOpenBody,
    _: str = Depends(require_brain_auth),
) -> dict:
    decision = brain.memory.get_decision(body.decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="decision not found")
    tick = runtime.market.get(decision["symbol"])
    if tick is None:
        raise HTTPException(status_code=409, detail="no live market tick for decision symbol")
    try:
        return runtime.paper.open_from_decision(
            decision_id=body.decision_id,
            tick=tick,
            equity=body.equity,
            stop_distance_bps=body.stop_distance_bps,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/paper/close")
def paper_close(
    body: PaperCloseBody,
    _: str = Depends(require_brain_auth),
) -> dict:
    position = runtime.paper.get_position(body.position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="position not found")
    tick = runtime.market.get(position["symbol"])
    if tick is None:
        raise HTTPException(status_code=409, detail="no live market tick for position symbol")
    try:
        return runtime.paper.close(body.position_id, tick)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/paper/portfolio")
def paper_portfolio(_: str = Depends(require_brain_auth)) -> dict:
    return runtime.paper.portfolio()
