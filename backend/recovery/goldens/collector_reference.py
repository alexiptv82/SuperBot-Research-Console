"""
Bitget Multi-Venue Microstructure Collector V2
PUBLIC MARKET DATA ONLY — no API keys, no account access, no orders.

Target venue:
- Bitget USDT perpetuals

External perp venues:
- Binance USDⓈ-M futures
- OKX USDT swaps
- Bybit USDT linear perpetuals

Design:
- raw normalized event stream + synchronized 100 ms grid
- Bitget books5 L5 depth captured event-level
- public/aggressor trades on all venues
- local/exchange timestamps + receive latency
- causal venue-basis EWMA
- basis-adjusted equal-weight geometric fair price
- median adjusted fair-price challenger
- external dispersion
- 100/200/500/1000 ms leader impulse diagnostics
- Bitget OFI / trade flow / L1+L5 depth
- background Parquet writer
- monotonic fixed-rate sampler
- restart-safe sessions
- research only, no execution
"""

import asyncio
import json
import math
import os
import queue
import statistics
import time
import uuid
from dataclasses import dataclass
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import websockets

# ----------------------------- configuration -----------------------------

ASSETS = ("BTC", "ETH")
VENUES = ("bitget", "binance", "okx", "bybit")
CONNECTIONS = ("bitget", "binance_public", "binance_market", "okx", "bybit")
EXTERNAL_PERP_VENUES = ("binance", "okx", "bybit")

SYMBOLS = {
    "bitget": {"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
    "binance": {"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
    "okx": {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP"},
    "bybit": {"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
}

WS = {
    "bitget": "wss://ws.bitget.com/v3/ws/public",
    "binance_public": "wss://fstream.binance.com/public/stream",
    "binance_market": "wss://fstream.binance.com/market/stream",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
    "bybit": "wss://stream.bybit.com/v5/public/linear",
}

SAMPLE_MS = 100
RUN_MINUTES = float(os.environ.get("MV2_RUN_MINUTES", "60"))
PERIODIC_FLUSH_SECONDS = 30
PROGRESS_SECONDS = 15
BINANCE_RAW_QTY_SAMPLE_MS = 20
BASIS_HALFLIFE_SECONDS = 60.0
BASIS_ALPHA = 1.0 - math.exp(-math.log(2.0) * (SAMPLE_MS / 1000.0) / BASIS_HALFLIFE_SECONDS)

GRID_FLUSH_ROWS = 20_000
BOOK_FLUSH_ROWS = 150_000
TRADE_FLUSH_ROWS = 150_000

FRESH_MS = {
    "bitget": 1000,
    "binance": 1000,
    "okx": 1000,
    "bybit": 1000,
}

BASE_ROOT = Path(os.environ.get("BITGET_MULTIVENUE_V2_ROOT", "/content/Bitget_MultiVenue_Microstructure_V2"))
SESSION_ID = os.environ.get("MV2_SESSION_ID") or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
ROOT = BASE_ROOT / "sessions" / SESSION_ID
DIR_GRID = ROOT / "sync_grid_100ms"
DIR_BOOK = ROOT / "normalized_books"
DIR_TRADE = ROOT / "normalized_trades"
DIR_LOG = ROOT / "logs"
for p in (BASE_ROOT, ROOT, DIR_GRID, DIR_BOOK, DIR_TRADE, DIR_LOG):
    p.mkdir(parents=True, exist_ok=True)


def wall_ms():
    return int(time.time() * 1000)


def mono_ns():
    return time.monotonic_ns()


def fnum(x):
    try:
        return float(x)
    except Exception:
        return np.nan


def inum(x, default=None):
    try:
        return int(x)
    except Exception:
        return default


def iso_to_ms(s):
    if not s:
        return None
    try:
        ss = str(s)
        if ss.endswith("Z"):
            ss = ss[:-1] + "+00:00"
        return int(datetime.fromisoformat(ss).timestamp() * 1000)
    except Exception:
        return None


def div(a, b):
    return a / b if np.isfinite(b) and abs(b) > 1e-12 else np.nan


def robust_median(values):
    z = [float(x) for x in values if np.isfinite(x)]
    return float(np.median(z)) if z else np.nan


@dataclass
class VenueAssetState:
    venue: str
    asset: str
    symbol: str
    market_type: str
    bid: float = np.nan
    bid_qty: float = np.nan
    ask: float = np.nan
    ask_qty: float = np.nan
    bid_depth_l5: float = np.nan
    ask_depth_l5: float = np.nan
    exchange_ts_ms: int | None = None
    local_recv_ts_ms: int | None = None
    seq: str | int | None = None
    book_count_window: int = 0
    ofi_raw_window: float = 0.0
    buy_qty_window: float = 0.0
    sell_qty_window: float = 0.0
    trade_count_window: int = 0
    last_trade_price: float = np.nan
    last_trade_ts_ms: int | None = None
    last_raw_book_persist_ms: int | None = None

    def update_book(self, bid, bid_qty, ask, ask_qty, exchange_ts, recv_ts, seq=None, bid_depth_l5=np.nan, ask_depth_l5=np.nan):
        old_bid, old_bq = self.bid, self.bid_qty
        old_ask, old_aq = self.ask, self.ask_qty

        new_bid, new_bq = old_bid, old_bq
        new_ask, new_aq = old_ask, old_aq
        if np.isfinite(bid) and np.isfinite(bid_qty):
            new_bid, new_bq = float(bid), float(bid_qty)
        if np.isfinite(ask) and np.isfinite(ask_qty):
            new_ask, new_aq = float(ask), float(ask_qty)

        price_changed = (
            not all(np.isfinite(x) for x in (old_bid, old_ask))
            or (np.isfinite(new_bid) and new_bid != old_bid)
            or (np.isfinite(new_ask) and new_ask != old_ask)
        )

        inc = 0.0
        if all(np.isfinite(x) for x in (old_bid, old_bq, old_ask, old_aq, new_bid, new_bq, new_ask, new_aq)):
            if new_bid >= old_bid:
                inc += new_bq
            if new_bid <= old_bid:
                inc -= old_bq
            if new_ask <= old_ask:
                inc -= new_aq
            if new_ask >= old_ask:
                inc += old_aq

        self.bid, self.bid_qty = new_bid, new_bq
        self.ask, self.ask_qty = new_ask, new_aq
        if np.isfinite(bid_depth_l5): self.bid_depth_l5 = float(bid_depth_l5)
        if np.isfinite(ask_depth_l5): self.ask_depth_l5 = float(ask_depth_l5)
        self.ofi_raw_window += inc
        self.book_count_window += 1
        self.exchange_ts_ms = exchange_ts
        self.local_recv_ts_ms = recv_ts
        self.seq = seq
        return inc, price_changed

    def update_trade(self, side_taker, price, qty, trade_ts):
        if side_taker == "buy":
            self.buy_qty_window += qty
        elif side_taker == "sell":
            self.sell_qty_window += qty
        self.trade_count_window += 1
        self.last_trade_price = price
        self.last_trade_ts_ms = trade_ts

    def snapshot_and_reset(self, sample_ts_ms):
        mid = (self.bid + self.ask) / 2 if np.isfinite(self.bid) and np.isfinite(self.ask) else np.nan
        spread = (self.ask / self.bid - 1) * 10000 if self.bid > 0 and self.ask > 0 else np.nan
        age_recv = sample_ts_ms - self.local_recv_ts_ms if self.local_recv_ts_ms is not None else np.nan
        age_exchange = sample_ts_ms - self.exchange_ts_ms if self.exchange_ts_ms is not None else np.nan
        latency = self.local_recv_ts_ms - self.exchange_ts_ms if self.local_recv_ts_ms is not None and self.exchange_ts_ms is not None else np.nan
        depth_den = self.bid_qty + self.ask_qty if np.isfinite(self.bid_qty) and np.isfinite(self.ask_qty) else np.nan
        depth_imb = div(self.bid_qty - self.ask_qty, depth_den)
        ofi_norm = div(self.ofi_raw_window, depth_den)
        tq = self.buy_qty_window + self.sell_qty_window
        trade_imb = div(self.buy_qty_window - self.sell_qty_window, tq)
        fresh = bool(np.isfinite(age_recv) and age_recv <= FRESH_MS[self.venue])

        l5_den = self.bid_depth_l5 + self.ask_depth_l5 if np.isfinite(self.bid_depth_l5) and np.isfinite(self.ask_depth_l5) else np.nan
        depth_imb_l5 = div(self.bid_depth_l5 - self.ask_depth_l5, l5_den)
        microprice_l1 = (
            (self.ask * self.bid_qty + self.bid * self.ask_qty) / (self.bid_qty + self.ask_qty)
            if all(np.isfinite(x) for x in (self.bid, self.ask, self.bid_qty, self.ask_qty)) and (self.bid_qty + self.ask_qty) > 0
            else np.nan
        )

        out = {
            "mid": mid,
            "bid": self.bid,
            "bid_qty": self.bid_qty,
            "ask": self.ask,
            "ask_qty": self.ask_qty,
            "bid_depth_l5": self.bid_depth_l5,
            "ask_depth_l5": self.ask_depth_l5,
            "depth_imbalance_l5": depth_imb_l5,
            "microprice_l1": microprice_l1,
            "spread_bps": spread,
            "book_age_recv_ms": age_recv,
            "book_age_exchange_ms": age_exchange,
            "book_latency_wall_ms": latency,
            "fresh": fresh,
            "depth_imbalance_l1": depth_imb,
            "ofi_raw_window": self.ofi_raw_window,
            "ofi_norm_l1": ofi_norm,
            "book_count_window": self.book_count_window,
            "trade_imbalance_window": trade_imb,
            "trade_count_window": self.trade_count_window,
            "last_trade_price": self.last_trade_price,
            "last_trade_age_ms": sample_ts_ms - self.last_trade_ts_ms if self.last_trade_ts_ms is not None else np.nan,
        }

        self.ofi_raw_window = 0.0
        self.book_count_window = 0
        self.buy_qty_window = 0.0
        self.sell_qty_window = 0.0
        self.trade_count_window = 0
        return out


class MultiVenueCollector:
    def __init__(self):
        self.states = {}
        for venue in VENUES:
            for asset in ASSETS:
                market_type = "USDT_PERP"
                self.states[(venue, asset)] = VenueAssetState(venue, asset, SYMBOLS[venue][asset], market_type)

        self.lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.write_queue = asyncio.Queue()
        self.grid_buf = []
        self.book_buf = []
        self.trade_buf = []
        self.log_buf = []
        self.parts = {"grid": 0, "book": 0, "trade": 0}
        self.prev_sample_mono_ns = None
        self.event_id = 0
        self.conn_generation = {c: 0 for c in CONNECTIONS}
        self.conn_id = {c: None for c in CONNECTIONS}
        self.basis_ewma_bps = {(v, a): np.nan for v in EXTERNAL_PERP_VENUES for a in ASSETS}
        self.price_history = {a: deque(maxlen=32) for a in ASSETS}
        self.prev_fair_ret_100ms = {a: np.nan for a in ASSETS}
        self.counters = {
            "books": {v: 0 for v in VENUES},
            "books_stored": {v: 0 for v in VENUES},
            "trades": {v: 0 for v in VENUES},
            "messages": {c: 0 for c in CONNECTIONS},
            "errors": {c: 0 for c in CONNECTIONS},
            "reconnects": {c: 0 for c in CONNECTIONS},
            "unknown_side": {v: 0 for v in VENUES},
            "sampler_missed_ticks_total": 0,
            "sampler_lag_events_gt50ms": 0,
            "writer_queue_max": 0,
            "writer_errors": 0,
        }

    def next_event_id(self):
        self.event_id += 1
        return self.event_id

    def log(self, kind, venue=None, **extra):
        self.log_buf.append({
            "session_id": SESSION_ID,
            "local_ts_ms": wall_ms(),
            "local_monotonic_ns": mono_ns(),
            "kind": kind,
            "venue": venue,
            "connection_generation": self.conn_generation.get(venue) if venue else None,
            "conn_id": self.conn_id.get(venue) if venue else None,
            **extra,
        })

    def write_manifest(self):
        m = {
            "collector": "Bitget_MultiVenue_Microstructure_Collector_V2",
            "session_id": SESSION_ID,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "run_minutes": RUN_MINUTES,
            "sample_ms": SAMPLE_MS,
            "venues": VENUES,
            "connections": CONNECTIONS,
            "assets": ASSETS,
            "symbols": SYMBOLS,
            "ws": WS,
            "external_perp_consensus": EXTERNAL_PERP_VENUES,
            "fair_price_baseline": "causal basis-adjusted equal-weight geometric mean; >=2 fresh external perps",
            "fair_price_challenger": "causal basis-adjusted median",
            "basis_halflife_seconds": BASIS_HALFLIFE_SECONDS,
            "notes": [
                "Public data only; no orders.",
                "Bitget is the target venue.",
                "Binance/OKX/Bybit are external USDT-perp venues.",
                "Bybit participates in fair price but is not assumed to be a fixed leader.",
                "Bitget books5 persists L5 aggregate depth and flattened top 5 levels.",
                "External venues retain BBO + public/aggressor trade streams.",
                "Raw normalized event stream plus synchronized 100ms grid.",
                "All disk/Drive writes occur in a background thread.",
                "100ms sampler uses monotonic deadlines and explicit missed-tick telemetry.",
                "Venue basis is causal: prior EWMA is used for current adjusted fair price, then updated.",
                "Leader impulse features on the grid are 100/200/500/1000ms; raw events remain available for sub-100ms research.",
                "Cross-venue trade_id and seq are normalized before Parquet serialization.",
                "Research-only collector; no trading credentials or order path."
            ],
        }
        (ROOT / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
        with open(BASE_ROOT / "session_index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"session_id": SESSION_ID, "created_utc": m["created_utc"], "path": str(ROOT)}) + "\n")

    def chunk_path(self, directory, prefix, key):
        part = self.parts[key]
        self.parts[key] += 1
        return directory / f"{prefix}_{SESSION_ID}_{part:07d}.parquet"

    def _detach(self, kind):
        if kind == "grid": attr, d, prefix = "grid_buf", DIR_GRID, "sync_grid_100ms"
        elif kind == "book": attr, d, prefix = "book_buf", DIR_BOOK, "books"
        elif kind == "trade": attr, d, prefix = "trade_buf", DIR_TRADE, "trades"
        else: raise ValueError(kind)
        rows = getattr(self, attr)
        if not rows: return None
        setattr(self, attr, [])
        return {"type": "parquet", "path": str(self.chunk_path(d, prefix, kind)), "rows": rows}

    def _detach_logs(self):
        if not self.log_buf: return None
        rows = self.log_buf; self.log_buf = []
        return {"type": "jsonl", "path": str(DIR_LOG / "events.jsonl"), "rows": rows}

    def _status(self):
        return {
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "session_id": SESSION_ID,
            "counters": self.counters,
            "connection_generation": self.conn_generation,
            "conn_id": self.conn_id,
            "writer_queue_size": self.write_queue.qsize(),
            "parts": self.parts,
            "buffers": {"grid": len(self.grid_buf), "book": len(self.book_buf), "trade": len(self.trade_buf), "logs": len(self.log_buf)},
        }

    def queue_job(self, job):
        if job is None: return
        self.write_queue.put_nowait(job)
        self.counters["writer_queue_max"] = max(self.counters["writer_queue_max"], self.write_queue.qsize())

    def threshold(self, kind):
        lim = {"grid": GRID_FLUSH_ROWS, "book": BOOK_FLUSH_ROWS, "trade": TRADE_FLUSH_ROWS}[kind]
        buf = {"grid": self.grid_buf, "book": self.book_buf, "trade": self.trade_buf}[kind]
        if len(buf) >= lim:
            self.queue_job(self._detach(kind))

    async def enqueue_flush_all(self, status=True):
        async with self.lock:
            for k in ("grid", "book", "trade"):
                self.queue_job(self._detach(k))
            self.queue_job(self._detach_logs())
            if status:
                self.queue_job({"type": "json", "path": str(ROOT / "runtime_status.json"), "payload": self._status()})

    @staticmethod
    def write_sync(job):
        if job["type"] == "parquet":
            df = pd.DataFrame(job["rows"])
            df.to_parquet(job["path"], index=False, compression="zstd")
            return
        if job["type"] == "jsonl":
            with open(job["path"], "a", encoding="utf-8") as f:
                for r in job["rows"]:
                    f.write(json.dumps(r, default=str) + "\n")
            return
        if job["type"] == "json":
            Path(job["path"]).write_text(json.dumps(job["payload"], indent=2, default=str), encoding="utf-8")
            return
        raise ValueError(job["type"])

    async def writer(self):
        while True:
            job = await self.write_queue.get()
            try:
                if job is None:
                    return
                try:
                    await asyncio.to_thread(self.write_sync, job)
                except Exception as exc:
                    self.counters["writer_errors"] += 1
                    err = {
                        "utc": datetime.now(timezone.utc).isoformat(),
                        "session_id": SESSION_ID,
                        "job_type": job.get("type"),
                        "job_path": job.get("path"),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    print("[WRITER_ERROR]", json.dumps(err, default=str), flush=True)
                    try:
                        with open(ROOT / "writer_errors.jsonl", "a", encoding="utf-8") as f:
                            f.write(json.dumps(err, default=str) + "\n")
                    except Exception:
                        pass
                    # Stop the QA quickly, but keep the writer alive so queued jobs
                    # are task_done() and the main coroutine cannot deadlock on join().
                    self.stop_event.set()
            finally:
                self.write_queue.task_done()

    def add_book(self, venue, asset, bid, bq, ask, aq, exchange_ts, recv_ts, recv_mono,
                 seq=None, server_ts=None, raw_type=None, bid_depth_l5=np.nan, ask_depth_l5=np.nan,
                 levels=None):
        st = self.states[(venue, asset)]
        inc, price_changed = st.update_book(
            bid, bq, ask, aq, exchange_ts, recv_ts, seq,
            bid_depth_l5=bid_depth_l5, ask_depth_l5=ask_depth_l5
        )
        self.counters["books"][venue] += 1

        persist = True
        if venue == "binance":
            persist = (
                price_changed
                or st.last_raw_book_persist_ms is None
                or recv_ts - st.last_raw_book_persist_ms >= BINANCE_RAW_QTY_SAMPLE_MS
            )
        if not persist:
            return

        st.last_raw_book_persist_ms = recv_ts
        mid = (st.bid + st.ask) / 2 if np.isfinite(st.bid) and np.isfinite(st.ask) else np.nan
        rec = {
            "session_id": SESSION_ID, "local_event_id": self.next_event_id(),
            "venue": venue, "asset": asset, "symbol": st.symbol, "market_type": st.market_type,
            "local_recv_ts_ms": recv_ts, "local_monotonic_ns": recv_mono,
            "exchange_ts_ms": exchange_ts, "server_ts_ms": server_ts,
            "seq": None if seq is None else str(seq), "raw_type": None if raw_type is None else str(raw_type),
            "bid": st.bid, "bid_qty": st.bid_qty, "ask": st.ask, "ask_qty": st.ask_qty, "mid": mid,
            "bid_depth_l5": st.bid_depth_l5, "ask_depth_l5": st.ask_depth_l5,
            "depth_imbalance_l5": div(st.bid_depth_l5 - st.ask_depth_l5, st.bid_depth_l5 + st.ask_depth_l5)
                if np.isfinite(st.bid_depth_l5) and np.isfinite(st.ask_depth_l5) else np.nan,
            "spread_bps": (st.ask / st.bid - 1) * 10000 if st.bid > 0 and st.ask > 0 else np.nan,
            "ofi_increment_raw": inc, "price_changed": bool(price_changed),
            "latency_wall_ms": recv_ts - exchange_ts if exchange_ts is not None else np.nan,
        }
        if levels is not None:
            bids, asks = levels
            for i in range(5):
                if i < len(bids):
                    rec[f"bid_px_{i+1}"] = fnum(bids[i][0]); rec[f"bid_qty_{i+1}"] = fnum(bids[i][1])
                else:
                    rec[f"bid_px_{i+1}"] = np.nan; rec[f"bid_qty_{i+1}"] = np.nan
                if i < len(asks):
                    rec[f"ask_px_{i+1}"] = fnum(asks[i][0]); rec[f"ask_qty_{i+1}"] = fnum(asks[i][1])
                else:
                    rec[f"ask_px_{i+1}"] = np.nan; rec[f"ask_qty_{i+1}"] = np.nan
        self.book_buf.append(rec)
        self.counters["books_stored"][venue] += 1
        self.threshold("book")

    def add_trade(self, venue, asset, price, qty, side_taker, exchange_ts, recv_ts, recv_mono,
                  trade_id=None, seq=None, raw_type=None, include_flow=True,
                  normal_nonrpi_qty=np.nan, rpi_qty_est=np.nan):
        st = self.states[(venue, asset)]
        valid_side = side_taker in ("buy", "sell")
        if not valid_side:
            self.counters["unknown_side"][venue] += 1
        include_flow_effective = bool(include_flow and valid_side)
        if include_flow_effective:
            st.update_trade(side_taker, price, qty, exchange_ts)
        # Cross-venue schema hygiene:
        # Binance aggregate trade IDs are numeric while several other venues expose strings/UUIDs.
        # Normalize IDs before a mixed-venue DataFrame reaches PyArrow.
        trade_id_text = None if trade_id is None else str(trade_id)
        seq_text = None if seq is None else str(seq)
        raw_type_text = None if raw_type is None else str(raw_type)

        self.trade_buf.append({
            "session_id": SESSION_ID, "local_event_id": self.next_event_id(),
            "venue": venue, "asset": asset, "symbol": st.symbol, "market_type": st.market_type,
            "local_recv_ts_ms": recv_ts, "local_monotonic_ns": recv_mono,
            "exchange_ts_ms": exchange_ts, "seq": seq_text, "trade_id": trade_id_text, "raw_type": raw_type_text,
            "price": price, "qty": qty, "side_taker": side_taker,
            "normal_nonrpi_qty": normal_nonrpi_qty, "rpi_qty_est": rpi_qty_est,
            "included_in_live_flow": include_flow_effective,
            "latency_wall_ms": recv_ts - exchange_ts if exchange_ts is not None else np.nan,
        })
        self.counters["trades"][venue] += 1
        self.threshold("trade")

    async def sampler(self):
        period_ns = int(SAMPLE_MS * 1_000_000)
        next_tick = mono_ns() + period_ns
        while not self.stop_event.is_set():
            await asyncio.sleep(max(0.0, (next_tick - mono_ns()) / 1e9))
            actual = mono_ns()
            late_ns = max(0, actual - next_tick)
            missed = int(late_ns // period_ns) if late_ns >= period_ns else 0
            if missed:
                self.counters["sampler_missed_ticks_total"] += missed
                next_tick += missed * period_ns
            lag_ms = max(0.0, (actual - next_tick) / 1e6)
            if lag_ms > 50:
                self.counters["sampler_lag_events_gt50ms"] += 1
                self.log("SAMPLER_LAG", lag_ms=lag_ms, missed_ticks=missed)

            interval_ms = np.nan if self.prev_sample_mono_ns is None else (actual - self.prev_sample_mono_ns) / 1e6
            self.prev_sample_mono_ns = actual
            ts = wall_ms()

            async with self.lock:
                for asset in ASSETS:
                    row = {
                        "session_id": SESSION_ID,
                        "local_ts_ms": ts,
                        "sample_monotonic_ns": actual,
                        "sample_interval_ms": interval_ms,
                        "sampler_lag_ms": lag_ms,
                        "sampler_missed_ticks": missed,
                        "asset": asset,
                    }
                    snaps = {}
                    for venue in VENUES:
                        s = self.states[(venue, asset)].snapshot_and_reset(ts)
                        snaps[venue] = s
                        for k, v in s.items():
                            row[f"{venue}_{k}"] = v

                    bg = snaps["bitget"]["mid"]
                    valid_bg = bool(np.isfinite(bg) and bg > 0)

                    adjusted = []
                    adjusted_map = {}
                    raw_basis_map = {}
                    for v in EXTERNAL_PERP_VENUES:
                        vmid = snaps[v]["mid"]
                        fresh = bool(snaps[v]["fresh"])
                        if valid_bg and fresh and np.isfinite(vmid) and vmid > 0:
                            raw_basis = math.log(vmid / bg) * 10000.0
                            raw_basis_map[v] = raw_basis
                            prior_basis = self.basis_ewma_bps[(v, asset)]
                            row[f"{v}_raw_basis_bps"] = raw_basis
                            row[f"{v}_basis_ewma_prior_bps"] = prior_basis
                            if np.isfinite(prior_basis):
                                adj = vmid / math.exp(prior_basis / 10000.0)
                                adjusted.append(adj)
                                adjusted_map[v] = adj
                                row[f"{v}_adjusted_mid"] = adj
                            else:
                                row[f"{v}_adjusted_mid"] = np.nan
                        else:
                            row[f"{v}_raw_basis_bps"] = np.nan
                            row[f"{v}_basis_ewma_prior_bps"] = self.basis_ewma_bps[(v, asset)]
                            row[f"{v}_adjusted_mid"] = np.nan

                    fair_geo = np.nan
                    fair_median = np.nan
                    if len(adjusted) >= 2 and all(x > 0 for x in adjusted):
                        fair_geo = float(math.exp(np.mean(np.log(np.asarray(adjusted, float)))))
                        fair_median = float(np.median(adjusted))

                    row["external_perp_fresh_count"] = int(sum(
                        bool(snaps[v]["fresh"]) and np.isfinite(snaps[v]["mid"]) and snaps[v]["mid"] > 0
                        for v in EXTERNAL_PERP_VENUES
                    ))
                    row["adjusted_fair_venue_count"] = len(adjusted)
                    row["fair_price_equal_geo"] = fair_geo
                    row["fair_price_adjusted_median"] = fair_median
                    row["bitget_gap_to_fair_bps"] = (bg / fair_geo - 1.0) * 10000.0 if valid_bg and np.isfinite(fair_geo) and fair_geo > 0 else np.nan

                    if len(adjusted) >= 2 and np.isfinite(fair_geo) and fair_geo > 0:
                        row["external_perp_dispersion_bps"] = (max(adjusted) - min(adjusted)) / fair_geo * 10000.0
                    else:
                        row["external_perp_dispersion_bps"] = np.nan

                    # Flow consensus: dimensionless quantities only.
                    tis = [snaps[v]["trade_imbalance_window"] for v in EXTERNAL_PERP_VENUES if np.isfinite(snaps[v]["trade_imbalance_window"])]
                    ofis = [snaps[v]["ofi_norm_l1"] for v in EXTERNAL_PERP_VENUES if np.isfinite(snaps[v]["ofi_norm_l1"])]
                    row["external_trade_imbalance_consensus"] = float(np.mean(tis)) if tis else np.nan
                    row["external_ofi_consensus_l1"] = float(np.mean(ofis)) if ofis else np.nan
                    row["external_trade_active_venues"] = len(tis)
                    row["external_ofi_active_venues"] = len(ofis)

                    # Multi-horizon leader impulse on the synchronized grid.
                    hist = self.price_history[asset]
                    hist.append((ts, fair_geo, bg))
                    for ms, steps in ((100,1),(200,2),(500,5),(1000,10)):
                        fair_ret = np.nan
                        bg_ret = np.nan
                        if len(hist) > steps:
                            _, fair_old, bg_old = hist[-(steps+1)]
                            if np.isfinite(fair_geo) and np.isfinite(fair_old) and fair_geo > 0 and fair_old > 0:
                                fair_ret = math.log(fair_geo / fair_old) * 10000.0
                            if valid_bg and np.isfinite(bg_old) and bg_old > 0:
                                bg_ret = math.log(bg / bg_old) * 10000.0
                        row[f"fair_ret_{ms}ms_bps"] = fair_ret
                        row[f"bitget_ret_{ms}ms_bps"] = bg_ret
                        row[f"leader_gap_{ms}ms_bps"] = fair_ret - bg_ret if np.isfinite(fair_ret) and np.isfinite(bg_ret) else np.nan

                    fair100 = row["fair_ret_100ms_bps"]
                    row["fair_accel_100ms_bps"] = fair100 - self.prev_fair_ret_100ms[asset] if np.isfinite(fair100) and np.isfinite(self.prev_fair_ret_100ms[asset]) else np.nan
                    self.prev_fair_ret_100ms[asset] = fair100

                    # Target-venue execution/adverse-selection context.
                    local_ofi = snaps["bitget"]["ofi_norm_l1"]
                    local_ti = snaps["bitget"]["trade_imbalance_window"]
                    gap = row["bitget_gap_to_fair_bps"]
                    row["bitget_fair_ofi_alignment"] = (
                        float(np.sign(-gap) * np.sign(local_ofi))
                        if np.isfinite(gap) and np.isfinite(local_ofi) and gap != 0 and local_ofi != 0 else np.nan
                    )
                    row["bitget_fair_trade_alignment"] = (
                        float(np.sign(-gap) * np.sign(local_ti))
                        if np.isfinite(gap) and np.isfinite(local_ti) and gap != 0 and local_ti != 0 else np.nan
                    )

                    # Causal basis update occurs AFTER the current fair-price calculation.
                    for v, raw_basis in raw_basis_map.items():
                        prev = self.basis_ewma_bps[(v, asset)]
                        self.basis_ewma_bps[(v, asset)] = raw_basis if not np.isfinite(prev) else ((1.0 - BASIS_ALPHA) * prev + BASIS_ALPHA * raw_basis)

                    self.grid_buf.append(row)
                self.threshold("grid")
            next_tick += period_ns

    async def periodic_flush(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(PERIODIC_FLUSH_SECONDS)
            await self.enqueue_flush_all(True)

    # ----------------------------- Bitget -----------------------------
    async def bitget_once(self):
        venue = "bitget"; self.conn_generation[venue] += 1; self.log("CONNECT_ATTEMPT", venue)
        async with websockets.connect(WS[venue], ping_interval=None, close_timeout=5, max_size=None) as ws:
            self.log("CONNECTED", venue)
            args=[]
            for asset in ASSETS:
                sym=SYMBOLS[venue][asset]
                args += [
                    {"instType":"usdt-futures","topic":"books5","symbol":sym},
                    {"instType":"usdt-futures","topic":"publicTrade","symbol":sym},
                ]
            await ws.send(json.dumps({"op":"subscribe","args":args}))
            async def hb():
                while not self.stop_event.is_set():
                    await asyncio.sleep(25); await ws.send("ping")
            h=asyncio.create_task(hb())
            try:
                async for msg in ws:
                    recv=wall_ms(); mn=mono_ns(); self.counters["messages"][venue]+=1
                    if msg=="pong": continue
                    p=json.loads(msg)
                    if p.get("event") == "subscribe":
                        self.conn_id[venue]=p.get("connId") or self.conn_id[venue]; continue
                    if p.get("event") == "error":
                        self.counters["errors"][venue]+=1; self.log("WS_ERROR",venue,payload=p); continue
                    arg=p.get("arg") or {}; sym=arg.get("symbol"); topic=arg.get("topic")
                    asset=next((a for a in ASSETS if SYMBOLS[venue][a]==sym),None)
                    if asset is None: continue
                    async with self.lock:
                        if topic=="books5":
                            for d in p.get("data") or []:
                                b=d.get("b") or []; a=d.get("a") or []
                                bid,bq=(fnum(b[0][0]),fnum(b[0][1])) if b else (np.nan,np.nan)
                                ask,aq=(fnum(a[0][0]),fnum(a[0][1])) if a else (np.nan,np.nan)
                                bid_l5=float(np.nansum([fnum(x[1]) for x in b[:5]])) if b else np.nan
                                ask_l5=float(np.nansum([fnum(x[1]) for x in a[:5]])) if a else np.nan
                                ex=inum(d.get("ts"),inum(p.get("ts"),recv))
                                self.add_book(
                                    venue,asset,bid,bq,ask,aq,ex,recv,mn,d.get("seq"),inum(p.get("ts")),p.get("action"),
                                    bid_depth_l5=bid_l5,ask_depth_l5=ask_l5,levels=(b[:5],a[:5])
                                )
                        elif topic=="publicTrade":
                            snapshot=p.get("action")=="snapshot"
                            for d in p.get("data") or []:
                                ex=inum(d.get("T"),inum(p.get("ts"),recv)); side=str(d.get("S","")).lower()
                                self.add_trade(venue,asset,fnum(d.get("p")),fnum(d.get("v")),side,ex,recv,mn,d.get("i"),d.get("seq"),p.get("action"),not snapshot)
            finally: h.cancel()

    # ----------------------------- Binance -----------------------------
    async def binance_public_once(self):
        conn="binance_public"; venue="binance"
        self.conn_generation[conn]+=1; self.log("CONNECT_ATTEMPT",conn)
        async with websockets.connect(WS[conn], ping_interval=20, ping_timeout=30, close_timeout=5, max_size=None) as ws:
            self.log("CONNECTED",conn)
            params=[f"{SYMBOLS[venue][asset].lower()}@bookTicker" for asset in ASSETS]
            await ws.send(json.dumps({"method":"SUBSCRIBE","params":params,"id":"books"}))
            async for msg in ws:
                recv=wall_ms(); mn=mono_ns(); self.counters["messages"][conn]+=1
                p=json.loads(msg)
                if "result" in p and p.get("id")=="books": continue
                d=p.get("data",p)
                if d.get("st") not in (None,1): continue
                if d.get("e")!="bookTicker": continue
                sym=d.get("s"); asset=next((a for a in ASSETS if SYMBOLS[venue][a]==sym),None)
                if asset is None: continue
                async with self.lock:
                    ex=inum(d.get("T"),inum(d.get("E"),recv))
                    self.add_book(
                        venue,asset,
                        fnum(d.get("b")),fnum(d.get("B")),fnum(d.get("a")),fnum(d.get("A")),
                        ex,recv,mn,d.get("u"),inum(d.get("E")),"bookTicker"
                    )

    async def binance_market_once(self):
        conn="binance_market"; venue="binance"
        self.conn_generation[conn]+=1; self.log("CONNECT_ATTEMPT",conn)
        async with websockets.connect(WS[conn], ping_interval=20, ping_timeout=30, close_timeout=5, max_size=None) as ws:
            self.log("CONNECTED",conn)
            params=[f"{SYMBOLS[venue][asset].lower()}@aggTrade" for asset in ASSETS]
            await ws.send(json.dumps({"method":"SUBSCRIBE","params":params,"id":"trades"}))
            async for msg in ws:
                recv=wall_ms(); mn=mono_ns(); self.counters["messages"][conn]+=1
                p=json.loads(msg)
                if "result" in p and p.get("id")=="trades": continue
                d=p.get("data",p)
                if d.get("st") not in (None,1): continue
                if d.get("e")!="aggTrade": continue
                sym=d.get("s"); asset=next((a for a in ASSETS if SYMBOLS[venue][a]==sym),None)
                if asset is None: continue
                async with self.lock:
                    ex=inum(d.get("T"),inum(d.get("E"),recv))
                    side="sell" if bool(d.get("m")) else "buy"
                    qty=fnum(d.get("q"))
                    nq=fnum(d.get("nq"))
                    rpi_est=(qty-nq) if np.isfinite(qty) and np.isfinite(nq) else np.nan
                    if np.isfinite(rpi_est):
                        rpi_est=max(0.0,rpi_est)
                    self.add_trade(
                        venue,asset,fnum(d.get("p")),qty,side,ex,recv,mn,
                        d.get("a"),None,"aggTrade",True,
                        normal_nonrpi_qty=nq,rpi_qty_est=rpi_est
                    )

    # ----------------------------- OKX -----------------------------
    async def okx_once(self):
        venue="okx"; self.conn_generation[venue]+=1; self.log("CONNECT_ATTEMPT",venue)
        async with websockets.connect(WS[venue], ping_interval=None, close_timeout=5, max_size=None) as ws:
            self.log("CONNECTED",venue)
            args=[]
            for asset in ASSETS:
                s=SYMBOLS[venue][asset]; args += [{"channel":"bbo-tbt","instId":s},{"channel":"trades","instId":s}]
            await ws.send(json.dumps({"op":"subscribe","args":args}))
            async def hb():
                while not self.stop_event.is_set():
                    await asyncio.sleep(20); await ws.send("ping")
            h=asyncio.create_task(hb())
            try:
                async for msg in ws:
                    recv=wall_ms(); mn=mono_ns(); self.counters["messages"][venue]+=1
                    if msg=="pong": continue
                    p=json.loads(msg)
                    if p.get("event") == "error": self.counters["errors"][venue]+=1; self.log("WS_ERROR",venue,payload=p); continue
                    if p.get("event") == "subscribe": continue
                    arg=p.get("arg") or {}; channel=arg.get("channel"); sym=arg.get("instId")
                    asset=next((a for a in ASSETS if SYMBOLS[venue][a]==sym),None)
                    if asset is None: continue
                    async with self.lock:
                        if channel=="bbo-tbt":
                            for d in p.get("data") or []:
                                b=d.get("bids") or []; a=d.get("asks") or []
                                bid,bq=(fnum(b[0][0]),fnum(b[0][1])) if b else (np.nan,np.nan)
                                ask,aq=(fnum(a[0][0]),fnum(a[0][1])) if a else (np.nan,np.nan)
                                ex=inum(d.get("ts"),recv)
                                self.add_book(venue,asset,bid,bq,ask,aq,ex,recv,mn,d.get("seqId"),None,"bbo-tbt")
                        elif channel=="trades":
                            for d in p.get("data") or []:
                                ex=inum(d.get("ts"),recv); side=str(d.get("side","")).lower()
                                self.add_trade(venue,asset,fnum(d.get("px")),fnum(d.get("sz")),side,ex,recv,mn,d.get("tradeId"),d.get("seqId"),"trades",True)
            finally: h.cancel()

    # ----------------------------- Bybit -----------------------------
    async def bybit_once(self):
        venue="bybit"; self.conn_generation[venue]+=1; self.log("CONNECT_ATTEMPT",venue)
        async with websockets.connect(WS[venue], ping_interval=None, close_timeout=5, max_size=None) as ws:
            self.log("CONNECTED",venue)
            args=[]
            for asset in ASSETS:
                s=SYMBOLS[venue][asset]; args += [f"orderbook.1.{s}",f"publicTrade.{s}"]
            await ws.send(json.dumps({"op":"subscribe","args":args,"req_id":"mv1"}))
            async def hb():
                while not self.stop_event.is_set():
                    await asyncio.sleep(20); await ws.send(json.dumps({"op":"ping"}))
            h=asyncio.create_task(hb())
            try:
                async for msg in ws:
                    recv=wall_ms(); mn=mono_ns(); self.counters["messages"][venue]+=1
                    p=json.loads(msg)
                    if p.get("op") in ("subscribe","ping","pong"): continue
                    topic=str(p.get("topic", "")); d=p.get("data")
                    async with self.lock:
                        if topic.startswith("orderbook.1.") and isinstance(d,dict):
                            sym=d.get("s"); asset=next((a for a in ASSETS if SYMBOLS[venue][a]==sym),None)
                            if asset is None: continue
                            b=d.get("b") or []; a=d.get("a") or []
                            bid,bq=(fnum(b[0][0]),fnum(b[0][1])) if b else (np.nan,np.nan)
                            ask,aq=(fnum(a[0][0]),fnum(a[0][1])) if a else (np.nan,np.nan)
                            ex=inum(d.get("cts"),inum(p.get("ts"),recv))
                            self.add_book(venue,asset,bid,bq,ask,aq,ex,recv,mn,d.get("seq",d.get("u")),inum(p.get("ts")),p.get("type"))
                        elif topic.startswith("publicTrade.") and isinstance(d,list):
                            for t in d:
                                sym=t.get("s"); asset=next((a for a in ASSETS if SYMBOLS[venue][a]==sym),None)
                                if asset is None: continue
                                ex=inum(t.get("T"),inum(p.get("ts"),recv)); side=str(t.get("S","")).lower()
                                self.add_trade(venue,asset,fnum(t.get("p")),fnum(t.get("v")),side,ex,recv,mn,t.get("i"),t.get("seq"),p.get("type"),True)
            finally: h.cancel()

    async def receiver_loop(self, venue, once_fn):
        backoff=1
        while not self.stop_event.is_set():
            try:
                await once_fn(); backoff=1
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.counters["errors"][venue]+=1
                self.counters["reconnects"][venue]+=1
                self.log("RECONNECT",venue,error_type=type(exc).__name__,error=str(exc),backoff_seconds=backoff)
                await asyncio.sleep(backoff); backoff=min(30,backoff*2)

    async def progress_monitor(self):
        started = time.monotonic()
        while not self.stop_event.is_set():
            await asyncio.sleep(PROGRESS_SECONDS)
            elapsed = time.monotonic() - started
            b = self.counters["books"]
            t = self.counters["trades"]
            m = self.counters["messages"]
            print(
                f"[PROGRESS {elapsed/60:.1f}/{RUN_MINUTES:.1f} min] "
                f"books BG={b['bitget']:,} BN={b['binance']:,} OKX={b['okx']:,} BY={b['bybit']:,} | "
                f"trades BG={t['bitget']:,} BN={t['binance']:,} OKX={t['okx']:,} BY={t['bybit']:,} | "
                f"missed={self.counters['sampler_missed_ticks_total']} "
                f"writerQ={self.write_queue.qsize()} "
                f"writerErr={self.counters['writer_errors']} "
                f"BNmsg={m['binance_public'] + m['binance_market']:,}",
                flush=True,
            )

    async def stop_after(self):
        await asyncio.sleep(RUN_MINUTES * 60)
        self.log("TIME_LIMIT_REACHED")
        self.stop_event.set()

    async def run(self):
        self.write_manifest(); self.log("COLLECTOR_START")
        writer_task=asyncio.create_task(self.writer())
        tasks=[
            asyncio.create_task(self.receiver_loop("bitget",self.bitget_once)),
            asyncio.create_task(self.receiver_loop("binance_public",self.binance_public_once)),
            asyncio.create_task(self.receiver_loop("binance_market",self.binance_market_once)),
            asyncio.create_task(self.receiver_loop("okx",self.okx_once)),
            asyncio.create_task(self.receiver_loop("bybit",self.bybit_once)),
            asyncio.create_task(self.sampler()),
            asyncio.create_task(self.periodic_flush()),
            asyncio.create_task(self.progress_monitor()),
            asyncio.create_task(self.stop_after()),
        ]
        try:
            await self.stop_event.wait()
        finally:
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.enqueue_flush_all(True)
            self.log("COLLECTOR_STOP")
            await self.enqueue_flush_all(True)
            await self.write_queue.join()
            await self.write_queue.put(None)
            await writer_task
        print("COLLECTION COMPLETE:", ROOT)


if __name__ == "__main__":
    asyncio.run(MultiVenueCollector().run())
