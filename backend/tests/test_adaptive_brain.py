from pathlib import Path
import pytest
from adaptive_brain.brain import AdaptiveBrain, BrainConfig
from adaptive_brain.memory import BrainMemory
from adaptive_brain.research_bridge import ResearchBridge
from adaptive_brain.schema import Action, BrainMode, ExpertSignal, PortfolioState, SourceType

def make_brain(tmp_path:Path):
    b=tmp_path/"backend"; (b/"recovery"/"reports"/"v1_2").mkdir(parents=True)
    return AdaptiveBrain(BrainMemory(tmp_path/"brain.db"),ResearchBridge(b),BrainConfig(mode=BrainMode.PAPER,decision_threshold=.18,min_experts=2))

def sig(name,direction,confidence=.9):
    return ExpertSignal(name,SourceType.MODEL,"BTCUSDT",direction,confidence,20.0)

def test_abstain(tmp_path):
    d=make_brain(tmp_path).decide("BTCUSDT",[sig("a",1)])
    assert d.action==Action.FLAT and d.risk_budget_fraction==0

def test_consensus(tmp_path):
    d=make_brain(tmp_path).decide("BTCUSDT",[sig("a",1),sig("b",1)])
    assert d.action==Action.LONG and 0<d.risk_budget_fraction<=.0025

def test_risk_gate(tmp_path):
    d=make_brain(tmp_path).decide("BTCUSDT",[sig("a",1),sig("b",1)],PortfolioState(daily_pnl_fraction=-.02))
    assert d.action==Action.FLAT and "DAILY_LOSS_LIMIT" in d.rationale

def test_learning(tmp_path):
    brain=make_brain(tmp_path); d=brain.decide("BTCUSDT",[sig("bull",1),sig("bear",-1)])
    a=brain.memory.get_weight("bull"); b=brain.memory.get_weight("bear")
    brain.record_outcome(d.decision_id,30,0)
    assert brain.memory.get_weight("bull")>a and brain.memory.get_weight("bear")<b

def test_live_refused(tmp_path):
    b=tmp_path/"backend"; (b/"recovery"/"reports"/"v1_2").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        AdaptiveBrain(BrainMemory(tmp_path/"brain.db"),ResearchBridge(b),BrainConfig(mode=BrainMode.LIVE))
