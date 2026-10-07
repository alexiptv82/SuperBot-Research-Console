from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp

from ..schema import MarketTick, utcnow_iso


@dataclass(frozen=True)
class BitgetPublicConfig:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    inst_type: str = "usdt-futures"
    websocket_url: str = "wss://ws.bitget.com/v3/ws/public"
    heartbeat_seconds: float = 30.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0


class BitgetPublicTickerStream:
    """Read-only Bitget v3 public ticker stream.

    No credentials are accepted. The adapter subscribes only to public ticker
    channels and emits normalized MarketTick records.
    """

    def __init__(self, config: BitgetPublicConfig | None = None):
        self.config = config or BitgetPublicConfig()

    def subscription_message(self) -> dict:
        return {
            "op": "subscribe",
            "args": [
                {
                    "instType": self.config.inst_type,
                    "topic": "ticker",
                    "symbol": s.upper(),
                }
                for s in self.config.symbols
            ],
        }

    @staticmethod
    def parse_message(payload: dict) -> list[MarketTick]:
        arg = payload.get("arg") or {}
        if arg.get("topic") != "ticker":
            return []
        category = str(arg.get("instType") or "")
        symbol = str(arg.get("symbol") or "").upper()
        out: list[MarketTick] = []
        for row in payload.get("data") or []:
            try:
                out.append(
                    MarketTick(
                        venue="bitget",
                        category=category,
                        symbol=symbol,
                        last_price=float(row["lastPrice"]),
                        bid=float(row.get("bid1Price") or row["lastPrice"]),
                        ask=float(row.get("ask1Price") or row["lastPrice"]),
                        bid_size=float(row.get("bid1Size") or 0.0),
                        ask_size=float(row.get("ask1Size") or 0.0),
                        mark_price=float(row["markPrice"]) if row.get("markPrice") not in (None, "") else None,
                        index_price=float(row["indexPrice"]) if row.get("indexPrice") not in (None, "") else None,
                        funding_rate=float(row["fundingRate"]) if row.get("fundingRate") not in (None, "") else None,
                        open_interest=float(row["openInterest"]) if row.get("openInterest") not in (None, "") else None,
                        venue_ts_ms=int(payload.get("ts") or row.get("ts")) if (payload.get("ts") or row.get("ts")) else None,
                        observed_at=utcnow_iso(),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def stream(
        self,
        on_tick: Callable[[MarketTick], Awaitable[None]],
        stop_event: asyncio.Event,
        on_error: Callable[[Exception], Awaitable[None]] | None = None,
    ) -> None:
        backoff = self.config.reconnect_min_seconds
        while not stop_event.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        self.config.websocket_url,
                        heartbeat=None,
                        autoping=True,
                    ) as ws:
                        await ws.send_json(self.subscription_message())
                        backoff = self.config.reconnect_min_seconds
                        heartbeat = asyncio.create_task(
                            self._heartbeat(ws, stop_event)
                        )
                        try:
                            async for msg in ws:
                                if stop_event.is_set():
                                    break
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    if msg.data == "pong":
                                        continue
                                    try:
                                        payload = json.loads(msg.data)
                                    except json.JSONDecodeError:
                                        continue
                                    for tick in self.parse_message(payload):
                                        await on_tick(tick)
                                elif msg.type in (
                                    aiohttp.WSMsgType.ERROR,
                                    aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.CLOSE,
                                ):
                                    break
                        finally:
                            heartbeat.cancel()
                            await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if on_error is not None:
                    await on_error(exc)
                if stop_event.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(self.config.reconnect_max_seconds, backoff * 2.0)

    async def _heartbeat(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(self.config.heartbeat_seconds)
            if not stop_event.is_set():
                await ws.send_str("ping")
