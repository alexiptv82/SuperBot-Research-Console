from __future__ import annotations
from typing import Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .brain import AdaptiveBrain
from .schema import ExpertSignal, Observation, PortfolioState, SourceType

router=APIRouter(prefix="/api/brain",tags=["adaptive-brain"])
brain=AdaptiveBrain.from_env()

class ObservationBody(BaseModel):
    source_id:str; source_type:SourceType; symbol:str; feature:str; value:float
    confidence:float=Field(ge=0,le=1); ttl_seconds:int=Field(default=300,ge=1); metadata:dict[str,Any]=Field(default_factory=dict)

class ExpertSignalBody(BaseModel):
    expert_id:str; source_type:SourceType; symbol:str
    direction:float=Field(ge=-1,le=1); confidence:float=Field(ge=0,le=1)
    expected_edge_bps:float=0.0; ttl_seconds:int=Field(default=120,ge=1); metadata:dict[str,Any]=Field(default_factory=dict)

class PortfolioBody(BaseModel):
    equity:float=1.0; daily_pnl_fraction:float=0.0; drawdown_fraction:float=0.0
    gross_exposure_fraction:float=0.0; symbol_exposure_fraction:float=0.0

class DecisionBody(BaseModel):
    symbol:str; signals:list[ExpertSignalBody]; portfolio:PortfolioBody=Field(default_factory=PortfolioBody)

class OutcomeBody(BaseModel):
    decision_id:str; market_move_bps:float; pnl_bps:float
    max_adverse_excursion_bps:float|None=None; metadata:dict[str,Any]=Field(default_factory=dict)

@router.get("/status")
def status(): return brain.status()

@router.post("/observe")
def observe(b:ObservationBody):
    brain.ingest(Observation(b.source_id,b.source_type,b.symbol.upper(),b.feature,b.value,b.confidence,ttl_seconds=b.ttl_seconds,metadata=b.metadata))
    return {"accepted":True}

@router.post("/decide")
def decide(b:DecisionBody):
    signals=[ExpertSignal(s.expert_id,s.source_type,s.symbol.upper(),s.direction,s.confidence,s.expected_edge_bps,ttl_seconds=s.ttl_seconds,metadata=s.metadata) for s in b.signals]
    return brain.decide(b.symbol,signals,PortfolioState(**b.portfolio.model_dump())).to_dict()

@router.post("/outcome")
def outcome(b:OutcomeBody):
    try:
        w=brain.record_outcome(b.decision_id,b.market_move_bps,b.pnl_bps,b.max_adverse_excursion_bps,b.metadata)
    except KeyError as e: raise HTTPException(status_code=404,detail=str(e)) from e
    except Exception as e: raise HTTPException(status_code=409,detail=str(e)) from e
    return {"accepted":True,"expert_weights":w}

@router.get("/weights")
def weights(): return {"expert_weights":brain.memory.weights()}

@router.get("/metrics")
def metrics(): return brain.memory.metrics()
