"""Unit tests for :class:`nautilus_gateio.websocket.GateioWebSocketClient`.

All tests feed JSON messages straight into the message handler — no sockets
are opened and no network access happens.
"""

from __future__ import annotations

import json
from typing import Any

from nautilus_gateio.websocket import GateioWebSocketClient


def candle_message(
    t: int,
    name: str = "1m_BTC_USDT",
    closed: bool = True,
    o: float = 100.0,
    h: float = 101.0,
    low: float = 99.0,
    c: float = 100.5,
    v: float = 12.5,
) -> str:
    """Build a ``spot.candlesticks`` update message as Gate.io sends it.

    ``a`` carries the BASE-currency amount (the requested ``v``); the wire
    field ``v`` is the QUOTE-currency turnover, modeled as ``v * c`` to
    mirror the real feed.
    """
    return json.dumps(
        {
            "time": t,
            "channel": "spot.candlesticks",
            "event": "update",
            "result": {
                "t": str(t),
                "v": str(v * c),
                "c": str(c),
                "h": str(h),
                "l": str(low),
                "o": str(o),
                "n": name,
                "a": str(v),
                "w": closed,
            },
        }
    )


def trade_message(price: float = 100.0, amount: float = 0.5) -> str:
    return json.dumps(
        {
            "time": 1700000000,
            "channel": "spot.trades",
            "event": "update",
            "result": {
                "id": 1,
                "create_time_ms": "1700000000000",
                "currency_pair": "BTC_USDT",
                "side": "buy",
                "price": str(price),
                "amount": str(amount),
            },
        }
    )


def make_client() -> tuple[GateioWebSocketClient, list[dict[str, Any]], list[Any]]:
    bars: list[dict[str, Any]] = []
    trades: list[Any] = []
    client = GateioWebSocketClient(on_bar=bars.append, on_trade=trades.append)
    return client, bars, trades


# -- candle handling --------------------------------------------------------


def test_only_closed_candles_are_emitted():
    client, bars, _ = make_client()
    client._handle_message(candle_message(1700000000, closed=False))
    assert bars == []

    client._handle_message(candle_message(1700000000, closed=True))
    assert len(bars) == 1


def test_duplicate_timestamp_is_dropped():
    client, bars, _ = make_client()
    client._handle_message(candle_message(1700000000))
    client._handle_message(candle_message(1700000000))
    assert len(bars) == 1


def test_out_of_order_bar_is_dropped():
    client, bars, _ = make_client()
    client._handle_message(candle_message(1700000060))
    client._handle_message(candle_message(1700000000))  # older than last emitted
    assert len(bars) == 1
    assert bars[0]["ts"] == 1700000060 * 1000


def test_gap_detection_on_1m_interval():
    client, bars, _ = make_client()
    client._handle_message(candle_message(1700000000))
    client._handle_message(candle_message(1700000000 + 300))  # 5 min hole on a 1m feed
    assert len(bars) == 2
    assert bars[0]["gap"] is False
    assert bars[1]["gap"] is True
    assert client.gaps_detected == 1


def test_bar_fields_parsed_from_channel_name():
    client, bars, _ = make_client()
    client._handle_message(
        candle_message(1700000000, name="1m_BTC_USDT", o=99.5, h=101.25, low=98.75, c=100.0, v=7.5)
    )
    bar = bars[0]
    assert bar["pair"] == "BTC_USDT"
    assert bar["interval"] == "1m"
    assert bar["ts"] == 1700000000000
    assert bar["open"] == 99.5
    assert bar["high"] == 101.25
    assert bar["low"] == 98.75
    assert bar["close"] == 100.0
    assert bar["volume"] == 7.5  # BASE amount ("a" field) — same units as REST candles
    assert bar["quote_volume"] == 7.5 * 100.0  # QUOTE turnover ("v" field)
    assert bar["gap"] is False


# -- trades and routing -----------------------------------------------------


def test_trades_callback_receives_result():
    client, _, trades = make_client()
    client._handle_message(trade_message(price=123.45, amount=0.25))
    assert len(trades) == 1
    assert trades[0]["price"] == "123.45"
    assert trades[0]["currency_pair"] == "BTC_USDT"


def test_handle_message_routes_channels_and_counts_messages():
    client, bars, trades = make_client()
    # A subscribe acknowledgement is counted but produces no callback.
    client._handle_message(
        json.dumps(
            {"channel": "spot.candlesticks", "event": "subscribe", "result": {"status": "success"}}
        )
    )
    client._handle_message(candle_message(1700000000))
    client._handle_message(trade_message())

    assert client.messages == 3
    assert len(bars) == 1
    assert len(trades) == 1


# -- reconnect backoff ------------------------------------------------------


def test_next_backoff_doubles_and_caps():
    client = GateioWebSocketClient(max_backoff=30.0)
    delays = [client._next_backoff() for _ in range(7)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_backoff_resets_after_successful_connect():
    client = GateioWebSocketClient(max_backoff=30.0)
    client._backoff = 16.0
    assert client._next_backoff() == 16.0
    # run() executes ``self._backoff = 1.0`` right after a successful connect;
    # simulate that reset and check the schedule restarts from the beginning.
    client._backoff = 1.0
    assert client._next_backoff() == 1.0
    assert client._next_backoff() == 2.0


# -- metrics and subscriptions ----------------------------------------------


def test_metrics_shape():
    client, _, _ = make_client()
    metrics = client.metrics()
    assert set(metrics) == {"reconnect_count", "gaps_detected", "messages", "last_event_ms"}
    assert metrics == {
        "reconnect_count": 0,
        "gaps_detected": 0,
        "messages": 0,
        "last_event_ms": 0,
    }

    client._handle_message(candle_message(1700000000))
    metrics = client.metrics()
    assert metrics["messages"] == 1
    assert metrics["last_event_ms"] == 1700000000000


def test_subscribe_candles_uppercases_pair():
    client = GateioWebSocketClient()
    client.subscribe_candles("btc_usdt", interval="5m")
    client.subscribe_trades("eth_usdt")
    assert ("spot.candlesticks", ["5m", "BTC_USDT"]) in client._subscriptions
    assert ("spot.trades", ["ETH_USDT"]) in client._subscriptions
