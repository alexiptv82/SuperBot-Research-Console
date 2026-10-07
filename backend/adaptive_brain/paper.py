from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .memory import BrainMemory
from .schema import Action, MarketTick, utcnow_iso


@dataclass(frozen=True)
class PaperConfig:
    fee_bps: float = 6.0
    slippage_bps: float = 2.0
    max_notional_fraction_of_equity: float = 1.0
    min_stop_distance_bps: float = 5.0


class PaperPortfolioSimulator:
    """Paper-only execution with spread, fee and slippage costs.

    The simulator has no exchange client and cannot place real orders.
    """

    def __init__(self, memory: BrainMemory, config: PaperConfig | None = None):
        self.memory = memory
        self.config = config or PaperConfig()

    def _entry_price(self, action: Action, tick: MarketTick) -> float:
        base = tick.ask if action == Action.LONG else tick.bid
        slip = self.config.slippage_bps / 10000.0
        return base * (1.0 + slip if action == Action.LONG else 1.0 - slip)

    def _exit_price(self, side: str, tick: MarketTick) -> float:
        slip = self.config.slippage_bps / 10000.0
        if side == Action.LONG.value:
            return tick.bid * (1.0 - slip)
        return tick.ask * (1.0 + slip)

    def open_from_decision(
        self,
        decision_id: str,
        tick: MarketTick,
        equity: float,
        stop_distance_bps: float,
    ) -> dict:
        d = self.memory.get_decision(decision_id)
        if d is None:
            raise KeyError(f"unknown decision_id: {decision_id}")
        action = Action(d["action"])
        if action == Action.FLAT:
            raise ValueError("cannot paper-execute FLAT decision")
        if equity <= 0:
            raise ValueError("equity must be > 0")
        stop_bps = max(self.config.min_stop_distance_bps, float(stop_distance_bps))
        risk_budget = float(d["risk_budget_fraction"])
        if risk_budget <= 0:
            raise ValueError("decision has zero risk budget")

        risk_cash = equity * risk_budget
        notional = risk_cash / (stop_bps / 10000.0)
        notional = min(notional, equity * self.config.max_notional_fraction_of_equity)
        entry = self._entry_price(action, tick)
        quantity = notional / entry
        open_fee = notional * self.config.fee_bps / 10000.0

        position_id = str(uuid.uuid4())
        fill_id = str(uuid.uuid4())
        now = utcnow_iso()
        with self.memory._connect() as c:
            c.execute(
                """INSERT INTO paper_positions(
                   position_id,decision_id,symbol,side,quantity,notional,entry_price,
                   opened_at,status,stop_distance_bps,open_fee,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    position_id, decision_id, d["symbol"], action.value, quantity,
                    notional, entry, now, "OPEN", stop_bps, open_fee,
                    json.dumps({"venue": tick.venue, "spread_bps": tick.spread_bps}, sort_keys=True),
                ),
            )
            c.execute(
                "INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?)",
                (
                    fill_id, position_id, "OPEN", entry, quantity, open_fee, now,
                    json.dumps({"paper": True}, sort_keys=True),
                ),
            )
        return self.get_position(position_id)

    def close(self, position_id: str, tick: MarketTick) -> dict:
        p = self.get_position(position_id)
        if p is None:
            raise KeyError(f"unknown position_id: {position_id}")
        if p["status"] != "OPEN":
            raise ValueError("position is not open")
        price = self._exit_price(p["side"], tick)
        qty = float(p["quantity"])
        entry = float(p["entry_price"])
        side_mult = 1.0 if p["side"] == Action.LONG.value else -1.0
        gross_pnl = side_mult * (price - entry) * qty
        close_notional = price * qty
        close_fee = close_notional * self.config.fee_bps / 10000.0
        realized = gross_pnl - float(p["open_fee"]) - close_fee
        now = utcnow_iso()
        fill_id = str(uuid.uuid4())
        with self.memory._connect() as c:
            c.execute(
                """UPDATE paper_positions SET
                   closed_at=?,close_price=?,status='CLOSED',
                   close_fee=?,realized_pnl=? WHERE position_id=?""",
                (now, price, close_fee, realized, position_id),
            )
            c.execute(
                "INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?)",
                (
                    fill_id, position_id, "CLOSE", price, qty, close_fee, now,
                    json.dumps({"paper": True}, sort_keys=True),
                ),
            )
        return self.get_position(position_id)

    def get_position(self, position_id: str) -> dict | None:
        with self.memory._connect() as c:
            r = c.execute(
                "SELECT * FROM paper_positions WHERE position_id=?", (position_id,)
            ).fetchone()
        return dict(r) if r else None

    def close_symbol_positions(self, symbol: str, tick: MarketTick) -> list[dict]:
        with self.memory._connect() as c:
            rows = c.execute(
                """SELECT position_id FROM paper_positions
                   WHERE symbol=? AND status='OPEN'
                   ORDER BY opened_at""",
                (symbol.upper(),),
            ).fetchall()
        closed = []
        for row in rows:
            closed.append(self.close(str(row["position_id"]), tick))
        return closed

    def portfolio(self) -> dict:
        with self.memory._connect() as c:
            rows = c.execute(
                "SELECT * FROM paper_positions ORDER BY opened_at DESC"
            ).fetchall()
        positions = [dict(r) for r in rows]
        return {
            "position_count": len(positions),
            "open_count": sum(1 for p in positions if p["status"] == "OPEN"),
            "closed_count": sum(1 for p in positions if p["status"] == "CLOSED"),
            "realized_pnl": sum(float(p["realized_pnl"]) for p in positions),
            "positions": positions,
        }
