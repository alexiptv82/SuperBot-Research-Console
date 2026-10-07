from __future__ import annotations
import json, os, uuid
from dataclasses import dataclass
from pathlib import Path
from .ensemble import AdaptiveExpertEnsemble, EnsembleConfig
from .memory import BrainMemory
from .research_bridge import ResearchBridge
from .risk import RiskConfig, RiskGovernor
from .schema import Action, BrainDecision, BrainMode, ExpertSignal, Observation, PortfolioState, SourceType, utcnow_iso

@dataclass(frozen=True)
class BrainConfig:
    mode: BrainMode = BrainMode.PAPER
    decision_threshold: float = 0.18
    min_experts: int = 2

class AdaptiveBrain:
    VERSION="0.1.0"

    def __init__(self,memory:BrainMemory,research_bridge:ResearchBridge,config:BrainConfig|None=None,risk_config:RiskConfig|None=None,ensemble_config:EnsembleConfig|None=None):
        self.memory=memory; self.research_bridge=research_bridge; self.config=config or BrainConfig()
        self.risk=RiskGovernor(risk_config); self.ensemble=AdaptiveExpertEnsemble(memory,ensemble_config)
        self.risk.ensure_mode_allowed(self.config.mode)

    @classmethod
    def from_env(cls):
        backend_root=Path(__file__).resolve().parents[1]
        data_dir=Path(os.environ.get("SUPERBOT_DATA_DIR",backend_root/"data"))
        db_path=os.environ.get("SUPERBOT_BRAIN_DB_PATH",str(data_dir/"adaptive_brain"/"brain_memory.db"))
        mode=BrainMode(os.environ.get("SUPERBOT_BRAIN_MODE","PAPER").upper())
        return cls(BrainMemory(db_path),ResearchBridge(backend_root),BrainConfig(mode=mode))

    def ingest(self,o:Observation):
        if not 0<=o.confidence<=1: raise ValueError("confidence must be in [0,1]")
        self.memory.add_observation(o)

    def decide(self,symbol:str,signals:list[ExpertSignal],portfolio:PortfolioState|None=None):
        self.risk.ensure_mode_allowed(self.config.mode)
        symbol=symbol.strip().upper(); portfolio=portfolio or PortfolioState()
        usable=[s for s in self.ensemble.usable(signals) if s.symbol.upper()==symbol]
        rationale=[]; nw={}
        if len(usable)<self.config.min_experts:
            action=Action.FLAT; score=0.0; confidence=0.0; rationale.append("INSUFFICIENT_FRESH_EXPERTS")
        else:
            score,nw=self.ensemble.score(usable); confidence=abs(score)
            if abs(score)<self.config.decision_threshold:
                action=Action.FLAT; rationale.append("ENSEMBLE_BELOW_DECISION_THRESHOLD")
            else:
                action=Action.LONG if score>0 else Action.SHORT; rationale.append("ENSEMBLE_DIRECTION_ACCEPTED")
        rd=self.risk.assess(action,confidence,score,portfolio)
        if not rd.allowed:
            action=Action.FLAT; confidence=0.0; risk_budget=0.0; rationale.extend(rd.reasons)
        else:
            risk_budget=rd.risk_budget_fraction; rationale.extend(rd.reasons)
        snap=[]
        for s in usable:
            d=s.to_dict(); d["ensemble_weight"]=nw.get(s.expert_id,0.0); snap.append(d)
        d=BrainDecision(str(uuid.uuid4()),utcnow_iso(),symbol,action,confidence,score,risk_budget,self.config.mode,rationale,snap)
        self.memory.add_decision(d); return d

    def record_outcome(self,decision_id:str,market_move_bps:float,pnl_bps:float,max_adverse_excursion_bps=None,metadata=None):
        d=self.memory.get_decision(decision_id)
        if d is None: raise KeyError(f"unknown decision_id: {decision_id}")
        raw=json.loads(d["expert_snapshot_json"])
        signals=[ExpertSignal(
            expert_id=s["expert_id"],source_type=SourceType(s["source_type"]),symbol=s["symbol"],
            direction=float(s["direction"]),confidence=float(s["confidence"]),expected_edge_bps=float(s.get("expected_edge_bps",0.0)),
            observed_at=s["observed_at"],ttl_seconds=int(s.get("ttl_seconds",120)),metadata=dict(s.get("metadata") or {})
        ) for s in raw]
        self.memory.add_outcome(decision_id,market_move_bps,pnl_bps,max_adverse_excursion_bps,metadata)
        return self.ensemble.learn(signals,market_move_bps)

    def status(self):
        return {"version":self.VERSION,"mode":self.config.mode.value,"live_execution_enabled":False,
                "decision_threshold":self.config.decision_threshold,"min_experts":self.config.min_experts,
                "expert_weights":self.memory.weights(),"learning_metrics":self.memory.metrics(),
                "research_bridge":self.research_bridge.snapshot(),
                "safety":{"learner_can_change_risk_limits":False,"learner_can_place_orders":False,"research_console_mutation":False}}
