"""Gate.io spot WebSocket client for public market data.

Subscribes to ``spot.candlesticks`` and ``spot.trades`` channels on
``wss://api.gateio.ws/ws/v4/`` and delivers parsed events through callbacks.

Reliability behavior (unit tested):

* only *closed* candles are emitted (``window_close`` flag);
* duplicate bars (same timestamp) are dropped;
* bars older than the last emitted bar are dropped (out-of-order guard);
* gaps between consecutive bars are detected and counted, and the affected
  bar carries ``gap=True``;
* the connection reconnects with exponential backoff (capped), resets the
  backoff after a successful connect, and replays all subscriptions.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import websockets

from nautilus_gateio.constants import GATEIO_INTERVAL_MS, GATEIO_WS_MAINNET

BarCallback = Callable[[dict[str, Any]], None]
TradeCallback = Callable[[Any], None]


class GateioWebSocketClient:
    """Public spot WebSocket client (no credentials required)."""

    def __init__(
        self,
        on_bar: BarCallback | None = None,
        on_trade: TradeCallback | None = None,
        url: str = GATEIO_WS_MAINNET,
        max_backoff: float = 30.0,
    ) -> None:
        self.on_bar = on_bar
        self.on_trade = on_trade
        self.url = url
        self.max_backoff = max_backoff
        self._subscriptions: list[tuple[str, list[str]]] = []
        self._last_bar_ts: dict[tuple[str, str], int] = {}
        self.reconnect_count = 0
        self.gaps_detected = 0
        self.messages = 0
        self.last_event_ms = 0
        self._stop = False
        self._backoff = 1.0

    # -- subscriptions -----------------------------------------------------

    def subscribe_candles(self, pair: str, interval: str = "1m") -> None:
        self._subscriptions.append(("spot.candlesticks", [interval, pair.upper()]))

    def subscribe_trades(self, pair: str) -> None:
        self._subscriptions.append(("spot.trades", [pair.upper()]))

    async def _send_subscriptions(self, ws: Any) -> None:
        for channel, payload in self._subscriptions:
            await ws.send(
                json.dumps(
                    {
                        "time": int(time.time()),
                        "channel": channel,
                        "event": "subscribe",
                        "payload": payload,
                    }
                )
            )

    # -- message handling --------------------------------------------------

    def _next_backoff(self) -> float:
        """Return the current reconnect delay and advance the backoff schedule."""
        delay = min(self._backoff, self.max_backoff)
        self._backoff = min(self._backoff * 2, self.max_backoff)
        return delay

    def _handle_candle(self, result: dict[str, Any]) -> None:
        # result: {t, v, c, h, l, o, n: '1m_BTC_USDT', a, w: window_close}
        name = result.get("n", "")
        interval, _, pair = name.partition("_")
        if not pair:
            pair = result.get("currency_pair", "")
        ts_ms = int(float(result["t"])) * 1000
        key = (pair, interval)
        if not result.get("w", result.get("window_close", False)):
            return  # only closed candles
        previous = self._last_bar_ts.get(key)
        if previous is not None and ts_ms <= previous:
            return  # duplicate or out-of-order bar
        interval_ms = GATEIO_INTERVAL_MS.get(interval, 60_000)
        gap = previous is not None and ts_ms - previous > interval_ms * 1.5
        if gap:
            self.gaps_detected += 1
        self._last_bar_ts[key] = ts_ms
        self.last_event_ms = ts_ms
        if self.on_bar is None:
            return
        self.on_bar(
            {
                "pair": pair,
                "interval": interval,
                "ts": ts_ms,
                "open": float(result["o"]),
                "high": float(result["h"]),
                "low": float(result["l"]),
                "close": float(result["c"]),
                # "a" is the BASE-currency amount; "v" is QUOTE-currency turnover.
                # Base volume matches the REST candlestick volume field.
                "volume": float(result.get("a", 0) or 0),
                "quote_volume": float(result.get("v", 0) or 0),
                "gap": gap,
            }
        )

    def _handle_message(self, message: str | bytes) -> None:
        self.messages += 1
        data = json.loads(message)
        channel = data.get("channel")
        if channel == "spot.candlesticks" and data.get("event") == "update":
            result = data.get("result")
            for item in result if isinstance(result, list) else [result]:
                if item:
                    self._handle_candle(item)
        elif channel == "spot.trades" and data.get("event") == "update" and self.on_trade:
            self.on_trade(data.get("result"))

    # -- run loop ----------------------------------------------------------

    async def run(self, max_seconds: float | None = None) -> None:
        """Connect and process messages until :meth:`stop` (or ``max_seconds``)."""
        started = time.time()
        while not self._stop:
            try:
                async with websockets.connect(self.url, open_timeout=10, ping_interval=20) as ws:
                    await self._send_subscriptions(ws)
                    self._backoff = 1.0  # successful connect resets backoff
                    while not self._stop:
                        recv_timeout = 30.0
                        if max_seconds is not None:
                            remaining = max_seconds - (time.time() - started)
                            if remaining <= 0:
                                self._stop = True
                                break
                            recv_timeout = min(recv_timeout, remaining)
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                        except TimeoutError:
                            # Quiet stream: re-check the deadline / keep the
                            # connection (server pings keep it alive).
                            continue
                        self._handle_message(message)
            except asyncio.CancelledError:
                break
            except Exception:
                if self._stop:
                    break
                self.reconnect_count += 1
                await asyncio.sleep(self._next_backoff())

    def stop(self) -> None:
        self._stop = True

    def metrics(self) -> dict[str, int]:
        """Transport reliability counters (reconnects, gaps, last event time)."""
        return {
            "reconnect_count": self.reconnect_count,
            "gaps_detected": self.gaps_detected,
            "messages": self.messages,
            "last_event_ms": self.last_event_ms,
        }
