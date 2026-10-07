from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .schema import BrainDecision, EvidenceItem, Observation, utcnow_iso


class BrainMemory:
    """Persistent memory isolated from the frozen Research Console database."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init(self) -> None:
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS observations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              observed_at TEXT NOT NULL,
              source_id TEXT NOT NULL,
              source_type TEXT NOT NULL,
              symbol TEXT NOT NULL,
              feature TEXT NOT NULL,
              value REAL NOT NULL,
              confidence REAL NOT NULL,
              ttl_seconds INTEGER NOT NULL,
              metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decisions(
              decision_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              symbol TEXT NOT NULL,
              action TEXT NOT NULL,
              confidence REAL NOT NULL,
              raw_score REAL NOT NULL,
              risk_budget_fraction REAL NOT NULL,
              mode TEXT NOT NULL,
              rationale_json TEXT NOT NULL,
              expert_snapshot_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outcomes(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              decision_id TEXT NOT NULL UNIQUE,
              realized_at TEXT NOT NULL,
              market_move_bps REAL NOT NULL,
              pnl_bps REAL NOT NULL,
              max_adverse_excursion_bps REAL,
              metadata_json TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
            );

            CREATE TABLE IF NOT EXISTS expert_weights(
              expert_id TEXT PRIMARY KEY,
              weight REAL NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence_items(
              fingerprint TEXT PRIMARY KEY,
              source_id TEXT NOT NULL,
              source_type TEXT NOT NULL,
              topic TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              url TEXT NOT NULL,
              published_at TEXT,
              observed_at TEXT NOT NULL,
              confidence REAL NOT NULL,
              metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_registry(
              source_id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              signal_count INTEGER NOT NULL DEFAULT 0,
              correct_count INTEGER NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              last_seen_at TEXT,
              last_error_at TEXT,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_positions(
              position_id TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL,
              symbol TEXT NOT NULL,
              side TEXT NOT NULL,
              quantity REAL NOT NULL,
              notional REAL NOT NULL,
              entry_price REAL NOT NULL,
              opened_at TEXT NOT NULL,
              closed_at TEXT,
              close_price REAL,
              status TEXT NOT NULL,
              stop_distance_bps REAL NOT NULL,
              open_fee REAL NOT NULL,
              close_fee REAL NOT NULL DEFAULT 0,
              realized_pnl REAL NOT NULL DEFAULT 0,
              metadata_json TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
            );

            CREATE TABLE IF NOT EXISTS paper_fills(
              fill_id TEXT PRIMARY KEY,
              position_id TEXT NOT NULL,
              fill_type TEXT NOT NULL,
              price REAL NOT NULL,
              quantity REAL NOT NULL,
              fee REAL NOT NULL,
              filled_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              FOREIGN KEY(position_id) REFERENCES paper_positions(position_id)
            );
            """)

    def add_observation(self, o: Observation) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO observations(observed_at,source_id,source_type,symbol,feature,value,confidence,ttl_seconds,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (o.observed_at, o.source_id, o.source_type.value, o.symbol.upper(), o.feature,
                 float(o.value), float(o.confidence), int(o.ttl_seconds),
                 json.dumps(o.metadata, sort_keys=True)),
            )
        self.touch_source(o.source_id, o.source_type.value)

    @staticmethod
    def evidence_fingerprint(item: EvidenceItem) -> str:
        material = "\n".join([
            item.source_id.strip(),
            item.source_type.value,
            item.topic.strip(),
            item.title.strip(),
            item.body.strip(),
            item.url.strip(),
            item.published_at or "",
        ]).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def add_evidence(self, item: EvidenceItem) -> bool:
        fp = self.evidence_fingerprint(item)
        with self._connect() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO evidence_items(
                   fingerprint,source_id,source_type,topic,title,body,url,published_at,
                   observed_at,confidence,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fp, item.source_id, item.source_type.value, item.topic, item.title,
                    item.body, item.url, item.published_at, item.observed_at,
                    max(0.0, min(1.0, float(item.confidence))),
                    json.dumps(item.metadata, sort_keys=True),
                ),
            )
            inserted = cur.rowcount == 1
        self.touch_source(item.source_id, item.source_type.value)
        return inserted

    def evidence_count(self) -> int:
        with self._connect() as c:
            row = c.execute("SELECT COUNT(*) n FROM evidence_items").fetchone()
        return int(row["n"])

    def add_decision(self, d: BrainDecision) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    d.decision_id, d.created_at, d.symbol, d.action.value,
                    float(d.confidence), float(d.raw_score),
                    float(d.risk_budget_fraction), d.mode.value,
                    json.dumps(d.rationale), json.dumps(d.expert_snapshot, sort_keys=True),
                ),
            )
        for s in d.expert_snapshot:
            sid = str(s.get("expert_id", "")).strip()
            stype = str(s.get("source_type", "internal"))
            if sid:
                self.touch_source(sid, stype, signal=True)

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return dict(r) if r else None

    def add_outcome(
        self,
        decision_id: str,
        market_move_bps: float,
        pnl_bps: float,
        max_adverse_excursion_bps: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO outcomes(decision_id,realized_at,market_move_bps,pnl_bps,max_adverse_excursion_bps,metadata_json) VALUES(?,?,?,?,?,?)",
                (
                    decision_id, utcnow_iso(), float(market_move_bps), float(pnl_bps),
                    max_adverse_excursion_bps, json.dumps(metadata or {}, sort_keys=True),
                ),
            )

    def get_weight(self, expert_id: str, default: float = 1.0) -> float:
        with self._connect() as c:
            r = c.execute("SELECT weight FROM expert_weights WHERE expert_id=?", (expert_id,)).fetchone()
        return float(r["weight"]) if r else float(default)

    def set_weight(self, expert_id: str, weight: float) -> None:
        with self._connect() as c:
            c.execute(
                """INSERT INTO expert_weights(expert_id,weight,updated_at) VALUES(?,?,?)
                   ON CONFLICT(expert_id) DO UPDATE SET
                     weight=excluded.weight,updated_at=excluded.updated_at""",
                (expert_id, float(weight), utcnow_iso()),
            )

    def weights(self) -> dict[str, float]:
        with self._connect() as c:
            rows = c.execute("SELECT expert_id,weight FROM expert_weights ORDER BY expert_id").fetchall()
        return {r["expert_id"]: float(r["weight"]) for r in rows}

    def touch_source(
        self,
        source_id: str,
        source_type: str,
        *,
        signal: bool = False,
        correct: bool | None = None,
        error: bool = False,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as c:
            c.execute(
                """INSERT INTO source_registry(
                     source_id,source_type,enabled,signal_count,correct_count,error_count,
                     last_seen_at,last_error_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     source_type=excluded.source_type,
                     signal_count=source_registry.signal_count + ?,
                     correct_count=source_registry.correct_count + ?,
                     error_count=source_registry.error_count + ?,
                     last_seen_at=?,
                     last_error_at=CASE WHEN ?=1 THEN ? ELSE source_registry.last_error_at END,
                     updated_at=?""",
                (
                    source_id, source_type, 1, int(signal), int(correct is True), int(error),
                    now, now if error else None, now,
                    int(signal), int(correct is True), int(error),
                    now, int(error), now, now,
                ),
            )

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM source_registry ORDER BY source_type,source_id"
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            n = int(d["signal_count"])
            d["accuracy"] = (float(d["correct_count"]) / n) if n else None
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def metrics(self) -> dict[str, Any]:
        with self._connect() as c:
            r = c.execute(
                """SELECT COUNT(*) n,AVG(pnl_bps) avg_pnl_bps,
                   AVG(CASE WHEN pnl_bps>0 THEN 1.0 ELSE 0.0 END) hit_rate,
                   MIN(pnl_bps) worst,MAX(pnl_bps) best FROM outcomes"""
            ).fetchone()
        return {
            "outcome_count": int(r["n"] or 0),
            "avg_pnl_bps": r["avg_pnl_bps"],
            "hit_rate": r["hit_rate"],
            "worst_pnl_bps": r["worst"],
            "best_pnl_bps": r["best"],
            "evidence_count": self.evidence_count(),
            "source_count": len(self.list_sources()),
        }
