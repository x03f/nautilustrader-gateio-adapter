"""Regression tests for ``nautilus_gateio.websocket.client.GateioWebSocketClient``.

No sockets are opened: the ``websockets`` connect function is replaced with a
stub that records the handshake and echoes acknowledgements. No credentials are
read.
"""

from __future__ import annotations

import asyncio
import json
import logging
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


# -- s4: logging goes through the platform, not the standard library ---------
#
# The transport used to log through `logging.getLogger(__name__)`. Nothing that
# happens on a socket - a reconnect, a subscription that could not be replayed,
# a malformed frame - reached the Nautilus log file or obeyed `log_level`,
# `log_level_file` or `log_component_levels`. `instruments.py` already used the
# platform `Logger`, so the package contradicted itself. See
# docs/concepts/logging.md ("Python and Nautilus components: log directly
# through the Nautilus Logger") and the in-tree reference transport
# adapters/binance/websocket/client.py:75.


class RecordingLogger:
    """Stands in for ``nautilus_trader.common.component.Logger``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.records: list[tuple[str, str]] = []

    def debug(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.records.append(("DEBUG", message))

    def info(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.records.append(("INFO", message))

    def warning(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.records.append(("WARNING", message))

    def error(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.records.append(("ERROR", message))

    def exception(self, message: str, ex: BaseException) -> None:
        self.records.append(("ERROR", f"{message}: {ex!r}"))

    def levels(self, needle: str) -> list[str]:
        return [level for level, message in self.records if needle in message]


@pytest.fixture()
def recording_logger(monkeypatch: pytest.MonkeyPatch) -> list[RecordingLogger]:
    """Capture what the transport sends to the platform's logging subsystem."""
    built: list[RecordingLogger] = []

    def _factory(name: str) -> RecordingLogger:
        logger = RecordingLogger(name)
        built.append(logger)
        return logger

    # `raising=False` so that a package still logging through the standard
    # library reaches the assertions below and fails on what it did, rather
    # than erroring in the fixture on a missing attribute.
    monkeypatch.setattr(ws_client_module, "Logger", _factory, raising=False)
    return built


def test_the_transport_logs_through_the_platform_logger(
    recording_logger: list[RecordingLogger],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed frame must be visible to operator log configuration.

    Two halves, and the standard-library half is the one that used to hold: the
    message must arrive at the platform logger, and nothing may be emitted
    through the standard library, where the Nautilus log file and level
    configuration cannot see it.
    """
    caplog.set_level(logging.DEBUG)
    client = make_client()

    client._dispatch("this is not json")

    leaked = [record.getMessage() for record in caplog.records if "nautilus_gateio" in record.name]
    assert leaked == [], f"logged through the standard library: {leaked}"
    assert recording_logger, "the transport built no platform Logger"
    assert recording_logger[0].levels("malformed frame") == ["WARNING"]


def test_the_transport_logger_is_named_for_component_filtering(
    recording_logger: list[RecordingLogger],
) -> None:
    """`log_component_levels` matches on the component name, exactly.

    The class name is what the in-tree reference transport registers, and using
    it means an operator can quiet or raise this transport alone without
    touching the clients above it.
    """
    make_client()
    assert recording_logger, "the transport built no platform Logger"
    assert recording_logger[0].name == "GateioWebSocketClient"


async def test_a_failed_replay_is_reported_through_the_platform_logger(
    recording_logger: list[RecordingLogger],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A subscription lost across a reconnect is an operator-visible event.

    Losing a channel silently is the shape of the defect: the message existed,
    but only in the standard library, so it never reached the log file the
    operator reads nor obeyed the level configured for it.
    """
    caplog.set_level(logging.DEBUG)
    client = make_client()
    subscription = ws_client_module._Subscription(
        channel="futures.trades",
        payload=["BTC_USDT"],
        auth=False,
    )
    client._subscriptions[subscription.key] = subscription

    # No socket is open, so replaying raises WS_NOT_CONNECTED.
    await client._after_reconnect()

    assert client.subscribe_failures == 1
    leaked = [record.getMessage() for record in caplog.records if "nautilus_gateio" in record.name]
    assert leaked == [], f"logged through the standard library: {leaked}"
    assert recording_logger, "the transport built no platform Logger"
    assert recording_logger[0].levels("Failed to replay subscription") == ["ERROR"]


# -- s4: no background task escapes the transport's registry -----------------
#
# `disconnect` cancelled exactly three named attributes. The proactive close
# started by a venue "upgrade" notification and the task wrapping an async
# handler's coroutine were in neither, so they were held only by the event loop:
# collectable while suspended, and never awaited at shutdown. The platform's own
# teardown (nautilus_trader/live/cancellation.py) snapshots strong references,
# cancels, and gathers with a bound.


async def test_disconnect_awaits_the_task_wrapping_an_async_handler(
    fake_transport: dict[str, Any],
) -> None:
    """An async handler is an advertised extension point, so it must be tracked.

    The handler's coroutine used to be scheduled with a bare
    ``loop.create_task``: nothing held a reference to it and ``disconnect``
    could not wait for it, so a shutdown mid-message dropped the message and
    left "Task was destroyed but it is pending" behind.
    """
    started = asyncio.Event()
    ended = asyncio.Event()

    async def _handle() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            ended.set()

    client = GateioWebSocketClient(
        url="wss://example.invalid/v4/ws",
        product=GateioProductType.PERP,
        handler=lambda _message: _handle(),
        ack_timeout_secs=0.2,
    )
    await client.connect()
    client._dispatch(json.dumps({"channel": "futures.trades", "event": "update", "result": []}))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await client.disconnect()

    assert ended.is_set(), "disconnect returned while the handler task was still pending"
    assert client._tasks == set()


async def test_disconnect_awaits_the_proactive_close_from_an_upgrade_notice(
    fake_transport: dict[str, Any],
) -> None:
    """The venue's "upgrade" notice starts a close; shutdown must not race it."""
    client = make_client()
    await client.connect()
    connection = fake_transport["connections"][0]

    async def _slow_close() -> None:
        # A real close is a round trip; the delay is what makes the race
        # between the proactive close and shutdown observable at all.
        await asyncio.sleep(0.2)
        connection.closed = True

    connection.close = _slow_close  # type: ignore[method-assign]

    before = set(asyncio.all_tasks())
    client._handle_system(
        {
            "channel": "futures.system",
            "event": "update",
            "result": {"type": "upgrade", "msg": "server upgrade"},
        },
    )
    spawned = set(asyncio.all_tasks()) - before
    assert spawned, "the upgrade notification must schedule a proactive close"
    await asyncio.sleep(0)  # let the close begin, as it would on a live socket

    await client.disconnect()

    assert all(task.done() for task in spawned), "disconnect left the proactive close pending"


async def test_a_failing_async_handler_is_reported_through_the_platform_logger(
    fake_transport: dict[str, Any],
    recording_logger: list[RecordingLogger],
) -> None:
    """A tracked task also means a failure has somewhere to be reported.

    An untracked task that raises surfaces only as asyncio's "exception was
    never retrieved" warning when the garbage collector eventually gets to it,
    which is neither timely nor visible to the operator's log configuration.
    """

    async def _explode() -> None:
        raise RuntimeError("handler exploded")

    client = GateioWebSocketClient(
        url="wss://example.invalid/v4/ws",
        product=GateioProductType.PERP,
        handler=lambda _message: _explode(),
        ack_timeout_secs=0.2,
    )
    await client.connect()
    client._dispatch(json.dumps({"channel": "futures.trades", "event": "update", "result": []}))
    # One turn for the task to run, one more for its done callback to fire.
    await asyncio.sleep(0.01)

    assert recording_logger, "the transport built no platform Logger"
    assert recording_logger[0].levels("handler exploded") == ["ERROR"]
    await client.disconnect()
