"""Regression tests for ``nautilus_gateio.websocket.client.GateioWebSocketClient``.

No sockets are opened: the ``websockets`` connect function is replaced with a
stub that records the handshake and echoes acknowledgements. No credentials are
read.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioError
from nautilus_gateio.websocket import client as ws_client_module
from nautilus_gateio.websocket.client import (
    SIZE_DECIMAL_HEADER,
    GateioWebSocketClient,
    is_transient_ws_error,
)

SIZE_DECIMAL_KEY = next(iter(SIZE_DECIMAL_HEADER))


class FakeConnection:
    """Minimal stand-in for ``websockets.asyncio.client.ClientConnection``."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.auto_ack = True
        self.ack_error: dict[str, Any] | None = None
        self._inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        if not self.auto_ack or message.get("event") not in ("subscribe", "unsubscribe"):
            return
        await self._inbox.put(
            json.dumps(
                {
                    "id": message.get("id"),
                    "channel": message["channel"],
                    "event": message["event"],
                    "error": self.ack_error,
                    "result": {"status": "fail" if self.ack_error else "success"},
                },
            ),
        )

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        self.closed = True

    def subscribed_channels(self) -> list[str]:
        return [m["channel"] for m in self.sent if m.get("event") == "subscribe"]


@pytest.fixture()
def fake_transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``connect`` and record every handshake."""
    state: dict[str, Any] = {"handshakes": [], "connections": []}

    async def _connect(url: str, **kwargs: Any) -> FakeConnection:
        state["handshakes"].append({"url": url, **kwargs})
        connection = FakeConnection()
        state["connections"].append(connection)
        return connection

    monkeypatch.setattr(ws_client_module, "connect", _connect)
    return state


def make_client(
    product: GateioProductType = GateioProductType.PERP,
    **kwargs: Any,
) -> GateioWebSocketClient:
    return GateioWebSocketClient(
        url="wss://example.invalid/v4/ws",
        product=product,
        handler=lambda message: None,
        ack_timeout_secs=0.2,
        **kwargs,
    )


# -- fractional size opt-in (md-01) ------------------------------------------


async def test_size_decimal_header_is_not_sent_by_default(fake_transport: dict[str, Any]) -> None:
    """The adapter must not ask for sizes its instruments cannot represent.

    Regression for md-01: the header was sent unconditionally on every futures
    socket, so the venue pushed fractional contract sizes while every contract
    instrument reports ``size_precision = 0``.
    """
    client = make_client(GateioProductType.PERP)
    await client._open()
    headers = fake_transport["handshakes"][0]["additional_headers"]
    assert headers is None or SIZE_DECIMAL_KEY not in headers
    await client._close()


async def test_size_decimal_header_is_sent_when_explicitly_requested(
    fake_transport: dict[str, Any],
) -> None:
    client = make_client(GateioProductType.PERP, size_decimal=True)
    await client._open()
    assert fake_transport["handshakes"][0]["additional_headers"][SIZE_DECIMAL_KEY] == "1"
    await client._close()


async def test_size_decimal_header_is_never_sent_on_spot(fake_transport: dict[str, Any]) -> None:
    """Spot sizes are already strings; the header is a futures-only concept."""
    client = make_client(GateioProductType.SPOT, size_decimal=True)
    await client._open()
    assert fake_transport["handshakes"][0]["additional_headers"] is None
    await client._close()


# -- transient subscribe failures (md-05) ------------------------------------


async def test_subscribe_while_disconnected_keeps_the_subscription_for_replay() -> None:
    """A subscribe issued inside the reconnect window must not be forgotten.

    Regression for md-05: any exception removed the subscription from the replay
    set, so a channel requested while the socket was down was never subscribed
    again and the stream stayed silent for the life of the process.
    """
    client = make_client()
    assert not client.is_connected

    with pytest.raises(GateioError) as excinfo:
        await client.subscribe("futures.order_book_update", ["BTC_USDT", "100ms", "100"])

    assert excinfo.value.label == "WS_NOT_CONNECTED"
    assert client.subscriptions == [
        ("futures.order_book_update", ["BTC_USDT", "100ms", "100"]),
    ]
    assert client.subscribe_failures == 1


async def test_subscribe_ack_timeout_keeps_the_subscription_for_replay(
    fake_transport: dict[str, Any],
) -> None:
    client = make_client()
    await client._open()
    fake_transport["connections"][0].auto_ack = False  # the venue never answers

    with pytest.raises(GateioError) as excinfo:
        await client.subscribe("futures.trades", ["BTC_USDT"])

    assert excinfo.value.label == "WS_ACK_TIMEOUT"
    assert client.subscriptions == [("futures.trades", ["BTC_USDT"])]
    await client._close()


async def test_subscribe_rejected_by_the_venue_drops_the_subscription(
    fake_transport: dict[str, Any],
) -> None:
    """A refusal is permanent: replaying it would only be refused again."""
    client = make_client()
    await client._open()
    fake_transport["connections"][0].ack_error = {"code": 2, "message": "unknown channel"}
    client._run_task = asyncio.get_running_loop().create_task(client._run_forever())
    client._stopped = False

    with pytest.raises(GateioError) as excinfo:
        await client.subscribe("futures.nonsense", ["BTC_USDT"])

    assert excinfo.value.label == "WS_REQUEST_REJECTED"
    assert client.subscriptions == []
    client._stopped = True
    client._run_task.cancel()
    await asyncio.gather(client._run_task, return_exceptions=True)
    await client._close()


async def test_a_transiently_failed_subscription_is_replayed_after_reconnect(
    fake_transport: dict[str, Any],
) -> None:
    """The kept subscription is what makes the recovery observable end to end."""
    client = make_client()
    with pytest.raises(GateioError):
        await client.subscribe("futures.book_ticker", ["BTC_USDT"])

    # A connection comes up and the replay runs.
    await client._open()
    client._run_task = asyncio.get_running_loop().create_task(client._run_forever())
    client._stopped = False
    await client._after_reconnect()

    assert fake_transport["connections"][0].subscribed_channels() == ["futures.book_ticker"]
    client._stopped = True
    client._run_task.cancel()
    await asyncio.gather(client._run_task, return_exceptions=True)
    await client._close()


# -- helper ------------------------------------------------------------------


def test_is_transient_ws_error_classification() -> None:
    assert is_transient_ws_error(GateioError(0, "WS_NOT_CONNECTED", "down"))
    assert is_transient_ws_error(GateioError(0, "WS_ACK_TIMEOUT", "late"))
    assert not is_transient_ws_error(GateioError(0, "WS_REQUEST_REJECTED", "no such channel"))
    assert not is_transient_ws_error(GateioError(401, "MISSING_CREDENTIALS", "private"))
    assert not is_transient_ws_error(ValueError("not a venue error"))
