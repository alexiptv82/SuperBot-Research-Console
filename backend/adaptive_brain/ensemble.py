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
    def __init__(self, memory: BrainMemory, config: EnsembleConfig|None=None):
        self.memory=memory; self.config=config or EnsembleConfig()

    @staticmethod
    def _fresh(s: ExpertSignal) -> bool:
        try:
            ts=datetime.fromisoformat(s.observed_at.replace("Z","+00:00"))
            if ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        age=(datetime.now(timezone.utc)-ts).total_seconds()
        return 0 <= age <= s.ttl_seconds

    def usable(self, signals: list[ExpertSignal]) -> list[ExpertSignal]:
        out=[]
        for raw in signals:
            s=raw.normalized()
            if s.expert_id and self._fresh(s): out.append(s)
        return out

    def score(self, signals: list[ExpertSignal]):
        usable=self.usable(signals)
        if not usable: return 0.0, {}
        rw={s.expert_id:max(self.config.min_weight,min(self.config.max_weight,self.memory.get_weight(s.expert_id))) for s in usable}
        denom=sum(rw.values()); nw={k:v/denom for k,v in rw.items()}
        total=0.0
        for s in usable:
            edge=1.0
            if s.expected_edge_bps:
                edge=max(0.25,math.tanh(abs(s.expected_edge_bps)/self.config.edge_scale_bps))
            total += nw[s.expert_id]*s.direction*s.confidence*edge
        return max(-1.0,min(1.0,total)), nw

    def learn(self, signals: list[ExpertSignal], market_move_bps: float):
        for s in self.usable(signals):
            old=self.memory.get_weight(s.expert_id)
            reward=max(-1.0,min(1.0,(s.direction*float(market_move_bps))/self.config.reward_scale_bps))*s.confidence
            new=old*math.exp(self.config.learning_rate*reward)
            self.memory.set_weight(s.expert_id,max(self.config.min_weight,min(self.config.max_weight,new)))
        return self.memory.weights()
