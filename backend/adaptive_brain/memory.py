from __future__ import annotations
import json, sqlite3
from pathlib import Path
from typing import Any
from .schema import BrainDecision, Observation, utcnow_iso

class BrainMemory:
    def __init__(self, db_path: str | Path):
        self.db_path=Path(db_path); self.db_path.parent.mkdir(parents=True, exist_ok=True); self._init()

    def _connect(self):
        c=sqlite3.connect(self.db_path, timeout=30.0); c.row_factory=sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON"); return c

    def _init(self):
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS observations(
              id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL, source_id TEXT NOT NULL,
              source_type TEXT NOT NULL, symbol TEXT NOT NULL, feature TEXT NOT NULL, value REAL NOT NULL,
              confidence REAL NOT NULL, ttl_seconds INTEGER NOT NULL, metadata_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(
              decision_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
              confidence REAL NOT NULL, raw_score REAL NOT NULL, risk_budget_fraction REAL NOT NULL, mode TEXT NOT NULL,
              rationale_json TEXT NOT NULL, expert_snapshot_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outcomes(
              id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE, realized_at TEXT NOT NULL,
              market_move_bps REAL NOT NULL, pnl_bps REAL NOT NULL, max_adverse_excursion_bps REAL,
              metadata_json TEXT NOT NULL, FOREIGN KEY(decision_id) REFERENCES decisions(decision_id));
            CREATE TABLE IF NOT EXISTS expert_weights(
              expert_id TEXT PRIMARY KEY, weight REAL NOT NULL, updated_at TEXT NOT NULL);
            """)

    def add_observation(self, o: Observation):
        with self._connect() as c:
            c.execute("INSERT INTO observations(observed_at,source_id,source_type,symbol,feature,value,confidence,ttl_seconds,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
              (o.observed_at,o.source_id,o.source_type.value,o.symbol.upper(),o.feature,float(o.value),float(o.confidence),int(o.ttl_seconds),json.dumps(o.metadata,sort_keys=True)))

    def add_decision(self, d: BrainDecision):
        with self._connect() as c:
            c.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
              (d.decision_id,d.created_at,d.symbol,d.action.value,float(d.confidence),float(d.raw_score),float(d.risk_budget_fraction),d.mode.value,json.dumps(d.rationale),json.dumps(d.expert_snapshot,sort_keys=True)))

    def get_decision(self, decision_id: str) -> dict[str,Any] | None:
        with self._connect() as c: r=c.execute("SELECT * FROM decisions WHERE decision_id=?",(decision_id,)).fetchone()
        return dict(r) if r else None

    def add_outcome(self, decision_id: str, market_move_bps: float, pnl_bps: float, max_adverse_excursion_bps=None, metadata=None):
        with self._connect() as c:
            c.execute("INSERT INTO outcomes(decision_id,realized_at,market_move_bps,pnl_bps,max_adverse_excursion_bps,metadata_json) VALUES(?,?,?,?,?,?)",
              (decision_id,utcnow_iso(),float(market_move_bps),float(pnl_bps),max_adverse_excursion_bps,json.dumps(metadata or {},sort_keys=True)))

    def get_weight(self, expert_id: str, default: float=1.0) -> float:
        with self._connect() as c: r=c.execute("SELECT weight FROM expert_weights WHERE expert_id=?",(expert_id,)).fetchone()
        return float(r["weight"]) if r else float(default)

    def set_weight(self, expert_id: str, weight: float):
        with self._connect() as c:
            c.execute("INSERT INTO expert_weights(expert_id,weight,updated_at) VALUES(?,?,?) ON CONFLICT(expert_id) DO UPDATE SET weight=excluded.weight,updated_at=excluded.updated_at",(expert_id,float(weight),utcnow_iso()))

    def weights(self) -> dict[str,float]:
        with self._connect() as c: rows=c.execute("SELECT expert_id,weight FROM expert_weights ORDER BY expert_id").fetchall()
        return {r["expert_id"]:float(r["weight"]) for r in rows}

    def metrics(self):
        with self._connect() as c:
            r=c.execute("SELECT COUNT(*) n,AVG(pnl_bps) avg_pnl_bps,AVG(CASE WHEN pnl_bps>0 THEN 1.0 ELSE 0.0 END) hit_rate,MIN(pnl_bps) worst,MAX(pnl_bps) best FROM outcomes").fetchone()
        return {"outcome_count":int(r["n"] or 0),"avg_pnl_bps":r["avg_pnl_bps"],"hit_rate":r["hit_rate"],"worst_pnl_bps":r["worst"],"best_pnl_bps":r["best"]}
