"""Live market-data client for Gate.io spot.

Transport model
---------------
WebSocket (``spot.candlesticks``) is the primary transport; a REST polling
loop is the automatic fallback if the WebSocket fails. Only *closed* bars are
emitted. Historical bars are served via REST for ``request_bars``.

Synthetic quotes
----------------
When ``emit_synthetic_quotes`` is enabled (default), each closed bar also
emits a ``QuoteTick`` positioned around the bar close (+/- 0.5 basis point,
unit sizes). This exists so quote-driven execution simulations receive
top-of-book updates; the ticks are NOT real market quotes. See
``docs/market-data.md`` for the full caveat.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.data.messages import RequestBars, SubscribeBars, UnsubscribeBars
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.objects import Price, Quantity

from nautilus_gateio.config import GateioDataClientConfig
from nautilus_gateio.constants import (
    GATEIO_API_PREFIX,
    NAUTILUS_TO_GATEIO_INTERVAL,
)
from nautilus_gateio.symbols import instrument_id_to_gate_pair
from nautilus_gateio.websocket import GateioWebSocketClient

# Aggregation-step seconds for supported bar specs (gap detection in poll mode).
_SPEC_SECONDS: dict[str, int] = {
    "1-MINUTE": 60,
    "5-MINUTE": 300,
    "15-MINUTE": 900,
    "30-MINUTE": 1_800,
    "1-HOUR": 3_600,
    "4-HOUR": 14_400,
    "8-HOUR": 28_800,
    "1-DAY": 86_400,
}


def bar_spec_of(bar_type: BarType) -> str:
    """Extract the ``N-UNIT`` aggregation token from a ``BarType`` string."""
    text = str(bar_type)
    for token in _SPEC_SECONDS:
        if token in text:
            return token
    return "1-MINUTE"


class GateioDataClient(LiveMarketDataClient):
    """Gate.io spot market-data client (public data, no credentials required)."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        config: GateioDataClientConfig | None = None,
    ) -> None:
        config = config or GateioDataClientConfig()
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=None,  # multi-venue routing not used; instruments carry the venue
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._config_local = config
        self._http = httpx.AsyncClient(
            base_url=config.base_url_http + GATEIO_API_PREFIX, timeout=15.0
        )
        self._ws_clients: dict[BarType, GateioWebSocketClient] = {}
        self._stream_tasks: dict[BarType, asyncio.Task] = {}
        self._last_poll_ts: dict[BarType, int] = {}
        self.reconnect_count = 0
        self.gaps_detected = 0
        self.last_event_ms = 0

    # -- lifecycle ---------------------------------------------------------

    async def _connect(self) -> None:
        await self._instrument_provider.initialize()
        transport = "websocket" if self._config_local.use_websocket else "rest-polling"
        self._log.info(f"Gate.io data client connected (transport: {transport})")

    async def _disconnect(self) -> None:
        for ws in self._ws_clients.values():
            ws.stop()
        for task in self._stream_tasks.values():
            task.cancel()
        self._ws_clients.clear()
        self._stream_tasks.clear()
        await self._http.aclose()
        self._log.info("Gate.io data client disconnected")

    # -- subscriptions -----------------------------------------------------

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        bar_type = command.bar_type
        if bar_type in self._stream_tasks:
            return
        pair = instrument_id_to_gate_pair(bar_type.instrument_id)
        transport = "websocket" if self._config_local.use_websocket else "rest-polling"
        self._log.info(f"Subscribing to bars {bar_type} (Gate.io {pair}, {transport})")
        if self._config_local.use_websocket:
            self._stream_tasks[bar_type] = self.create_task(self._stream_ws(bar_type, pair))
        else:
            self._stream_tasks[bar_type] = self.create_task(self._stream_poll(bar_type, pair))

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        bar_type = command.bar_type
        ws = self._ws_clients.pop(bar_type, None)
        if ws:
            ws.stop()
        task = self._stream_tasks.pop(bar_type, None)
        if task:
            task.cancel()

    # -- bar emission ------------------------------------------------------

    def _instrument_precisions(self, bar_type: BarType) -> tuple[int, int]:
        instrument = self._cache.instrument(
            bar_type.instrument_id
        ) or self._instrument_provider.find(bar_type.instrument_id)
        if instrument is not None:
            return instrument.price_precision, instrument.size_precision
        return 2, 4

    def _emit_bar(
        self,
        bar_type: BarType,
        ohlcv: dict[str, Any],
        price_precision: int,
        size_precision: int,
    ) -> None:
        ts_ms = int(ohlcv["ts"])
        ts_event = ts_ms * 1_000_000
        ts_init = int(time.time() * 1e9)
        bar = Bar(
            bar_type,
            Price(round(ohlcv["open"], price_precision), price_precision),
            Price(round(ohlcv["high"], price_precision), price_precision),
            Price(round(ohlcv["low"], price_precision), price_precision),
            Price(round(ohlcv["close"], price_precision), price_precision),
            Quantity(round(ohlcv["volume"], size_precision), size_precision),
            ts_event,
            ts_init,
        )
        self._handle_data(bar)
        self.last_event_ms = ts_ms
        if ohlcv.get("gap"):
            self.gaps_detected += 1
        if self._config_local.emit_synthetic_quotes:
            close = float(ohlcv["close"])
            half_spread = close * 0.00005
            quote = QuoteTick(
                instrument_id=bar_type.instrument_id,
                bid_price=Price(round(close - half_spread, price_precision), price_precision),
                ask_price=Price(round(close + half_spread, price_precision), price_precision),
                bid_size=Quantity(1.0, size_precision),
                ask_size=Quantity(1.0, size_precision),
                ts_event=ts_event,
                ts_init=ts_init,
            )
            self._handle_data(quote)

    # -- websocket transport -----------------------------------------------

    async def _stream_ws(self, bar_type: BarType, pair: str) -> None:
        spec = bar_spec_of(bar_type)
        interval = NAUTILUS_TO_GATEIO_INTERVAL.get(spec, "1m")
        price_precision, size_precision = self._instrument_precisions(bar_type)

        def on_bar(ohlcv: dict[str, Any]) -> None:
            try:
                self._emit_bar(bar_type, ohlcv, price_precision, size_precision)
            except Exception as exc:  # never kill the transport on one bad bar
                self._log.warning(f"WS bar handling error: {str(exc)[:100]}")

        ws = GateioWebSocketClient(on_bar=on_bar, url=self._config_local.base_url_ws)
        ws.subscribe_candles(pair, interval)
        self._ws_clients[bar_type] = ws
        try:
            await ws.run()
        except Exception as exc:
            self._log.warning(f"WebSocket failed, falling back to REST polling: {str(exc)[:100]}")
            await self._stream_poll(bar_type, pair)
        finally:
            # Remove the finished stream before folding its counters into the
            # totals, so metrics() never double-counts it.
            self._ws_clients.pop(bar_type, None)
            self.reconnect_count += ws.reconnect_count
            self.gaps_detected += ws.gaps_detected

    # -- REST polling transport (fallback) ----------------------------------

    async def _stream_poll(self, bar_type: BarType, pair: str) -> None:
        spec = bar_spec_of(bar_type)
        interval = NAUTILUS_TO_GATEIO_INTERVAL.get(spec, "1m")
        step_ms = _SPEC_SECONDS[spec] * 1000
        price_precision, size_precision = self._instrument_precisions(bar_type)
        poll_errors = 0
        while True:
            try:
                response = await self._http.get(
                    "/spot/candlesticks",
                    params={"currency_pair": pair, "interval": interval, "limit": 3},
                )
                response.raise_for_status()
                rows = response.json()
                rows.sort(key=lambda r: int(float(r[0])))
                if len(rows) >= 2:
                    row = rows[-2]  # last CLOSED candle
                    ts_ms = int(float(row[0])) * 1000
                    previous = self._last_poll_ts.get(bar_type)
                    if previous != ts_ms:
                        gap = previous is not None and ts_ms - previous > step_ms * 1.5
                        self._emit_bar(
                            bar_type,
                            {
                                "ts": ts_ms,
                                "open": float(row[5]),
                                "high": float(row[3]),
                                "low": float(row[4]),
                                "close": float(row[2]),
                                "volume": float(row[6]),
                                "gap": gap,
                            },
                            price_precision,
                            size_precision,
                        )
                        self._last_poll_ts[bar_type] = ts_ms
                poll_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                poll_errors += 1
                self.reconnect_count += 1
                self._log.warning(
                    f"Polling error for {bar_type}: {str(exc)[:100]} (retry {poll_errors})"
                )
                await asyncio.sleep(min(2 ** min(poll_errors, 5), 30))
            await asyncio.sleep(self._config_local.poll_interval_secs)

    # -- historical requests -------------------------------------------------

    async def _request_bars(self, request: RequestBars) -> None:
        bar_type = request.bar_type
        pair = instrument_id_to_gate_pair(bar_type.instrument_id)
        spec = bar_spec_of(bar_type)
        interval = NAUTILUS_TO_GATEIO_INTERVAL.get(spec, "1m")
        price_precision, size_precision = self._instrument_precisions(bar_type)
        params: dict[str, Any] = {
            "currency_pair": pair,
            "interval": interval,
            "limit": min(int(request.limit) or 500, 1000),
        }
        if request.start is not None:
            params["from"] = int(request.start.timestamp())
        if request.end is not None:
            params["to"] = int(request.end.timestamp())
        try:
            response = await self._http.get("/spot/candlesticks", params=params)
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            self._log.error(f"Bar request failed for {bar_type}: {str(exc)[:120]}")
            return
        rows.sort(key=lambda r: int(float(r[0])))
        bars: list[Bar] = []
        for row in rows:
            ts_ms = int(float(row[0])) * 1000
            bars.append(
                Bar(
                    bar_type,
                    Price(round(float(row[5]), price_precision), price_precision),
                    Price(round(float(row[3]), price_precision), price_precision),
                    Price(round(float(row[4]), price_precision), price_precision),
                    Price(round(float(row[2]), price_precision), price_precision),
                    Quantity(round(float(row[6]), size_precision), size_precision),
                    ts_ms * 1_000_000,
                    ts_ms * 1_000_000,
                )
            )
        self._handle_bars(bar_type, bars, request.id, request.start, request.end, request.params)
        self._log.info(f"Delivered {len(bars)} historical bars for {bar_type}")

    # -- metrics -------------------------------------------------------------

    def metrics(self) -> dict[str, int]:
        """Transport reliability counters aggregated across streams."""
        ws_reconnects = sum(ws.reconnect_count for ws in self._ws_clients.values())
        ws_gaps = sum(ws.gaps_detected for ws in self._ws_clients.values())
        return {
            "reconnect_count": self.reconnect_count + ws_reconnects,
            "gaps_detected": self.gaps_detected + ws_gaps,
            "last_event_ms": self.last_event_ms,
        }
