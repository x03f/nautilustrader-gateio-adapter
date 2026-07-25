"""Resilient WebSocket transport for a single Gate.io endpoint.

One :class:`GateioWebSocketClient` owns exactly one connection to one Gate.io
WebSocket host. Gate.io partitions its streams by product — spot, USDT
perpetuals, BTC-settled perpetuals, delivery futures and options each have their
own host — so a multi-product session runs several of these side by side.

The class is transport only: it connects, authenticates subscriptions, keeps the
connection alive, replays subscriptions after a reconnect and hands decoded
messages to a callback. It contains no product or trading logic; see
:mod:`nautilus_gateio.websocket.public` and
:mod:`nautilus_gateio.websocket.private` for the typed channel helpers.

Protocol summary (Gate.io WebSocket v4)
--------------------------------------
Requests are JSON objects ``{time, id, channel, event, payload[, auth]}``.
Every subscribe or unsubscribe is acknowledged with the same ``channel``,
``event`` and ``id``; the acknowledgement is successful when ``error`` is null,
which is what this client keys on (``result.status`` is documented as always
being present and is not authoritative).

Private subscriptions carry an ``auth`` object signed over
``channel=<channel>&event=<event>&time=<time>``; the signature must be attached
to unsubscribe requests as well.

Fractional futures sizes
------------------------
Gate.io offers an opt-in, ``X-Gate-Size-Decimal: 1``, sent as a handshake header
on a futures or delivery connection. With it the venue pushes exact, possibly
fractional sizes as strings on every size-bearing futures channel; without it it
truncates them toward zero ("the size of 1.1, 1.5, and 1.7 will be 1").

**This adapter does not opt in by default.** A Nautilus ``Quantity`` on a Gate.io
contract instrument is a whole number of contracts — every contract instrument
this adapter builds reports ``size_precision = 0`` — so a fractional size has no
representation in the data that would be published, and requesting one would
only mean discarding the fraction one layer later. Truncating toward zero at the
venue and truncating toward zero in the adapter produce the same numbers, and
the adapter does the latter anyway (see
:func:`nautilus_gateio.data.venue_quantity`) so that it stays correct if the
venue ever pushes fractions regardless of the header.

Pass ``size_decimal=True`` to opt in. Do so only together with an instrument
layer that derives ``size_precision`` from the venue, otherwise fractional
levels below one contract are published as absent.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from nautilus_gateio.common.constants import (
    CHANNEL_PREFIX,
    DEFAULT_WS_MAX_BACKOFF_SECS,
    DEFAULT_WS_RECV_TIMEOUT_SECS,
)
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioError
from nautilus_gateio.common.signing import ws_auth_payload

#: Handshake header that makes futures and delivery push exact (possibly
#: fractional) sizes as strings instead of truncating them toward zero. Opt-in;
#: see the module docstring for why the adapter leaves it off.
SIZE_DECIMAL_HEADER: dict[str, str] = {"X-Gate-Size-Decimal": "1"}

#: Largest inbound frame accepted; comfortably above a 400-level book snapshot.
MAX_FRAME_BYTES: int = 8 * 1024 * 1024

_SUBSCRIBE = "subscribe"
_UNSUBSCRIBE = "unsubscribe"

#: Error labels describing a failure of the transport rather than a decision by
#: the venue. A subscription that fails this way is kept and replayed on the
#: next connection; only an outright venue rejection removes it.
WS_TRANSIENT_LABELS: frozenset[str] = frozenset({"WS_NOT_CONNECTED", "WS_ACK_TIMEOUT"})


def is_transient_ws_error(error: Exception) -> bool:
    """Return whether ``error`` is a transport failure worth replaying.

    ``WS_NOT_CONNECTED`` is exactly the state of the reconnect window, and
    ``WS_ACK_TIMEOUT`` means the venue did not answer in time — neither is a
    refusal, so the subscription is still wanted.
    """
    return isinstance(error, GateioError) and error.label in WS_TRANSIENT_LABELS


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Subscription:
    """A subscription to replay after a reconnect."""

    channel: str
    payload: Any
    auth: bool

    @property
    def key(self) -> str:
        return f"{self.channel}|{json.dumps(self.payload, sort_keys=True, default=str)}"


@dataclass(slots=True)
class _PendingRequest:
    """A subscribe or unsubscribe request awaiting its acknowledgement."""

    channel: str
    event: str
    future: asyncio.Future


class GateioWebSocketClient:
    """A single, self-healing Gate.io WebSocket connection.

    Parameters
    ----------
    url : str
        Endpoint to connect to, e.g. ``wss://api.gateio.ws/ws/v4/``.
    product : GateioProductType
        Product served by ``url``. Selects the channel prefix used for the
        application-level ping and whether the size-decimal header is sent.
    handler : Callable[[dict], Any]
        Called with every decoded data message (subscription acknowledgements
        and pongs are consumed internally). A coroutine return value is
        scheduled on the event loop; exceptions are logged and never break the
        receive loop.
    api_key, api_secret : str
        Credentials for private channels. Public streams need neither.
    loop : asyncio.AbstractEventLoop, optional
        Event loop to run background tasks on; defaults to the running loop.
    heartbeat_secs : float
        Idle interval after which an application-level ping is sent.
    max_backoff : float
        Ceiling for the exponential reconnect backoff.
    recv_timeout_secs : float
        A connection that delivers nothing for this long is considered dead and
        is recycled.
    ack_timeout_secs : float
        How long to wait for a subscribe or unsubscribe acknowledgement.
    size_decimal : bool
        Send the ``X-Gate-Size-Decimal: 1`` handshake header on futures and
        delivery connections, opting into fractional sizes. Off by default; see
        the module docstring.
    on_reconnect : Callable[[], Any], optional
        Invoked after a reconnect, once the previous subscriptions have been
        replayed. Owners of local order books use this to resnapshot, since
        Gate.io offers no stream resumption.
    """

    def __init__(
        self,
        url: str,
        product: GateioProductType,
        handler: Callable[[dict[str, Any]], Any],
        api_key: str = "",
        api_secret: str = "",
        loop: asyncio.AbstractEventLoop | None = None,
        heartbeat_secs: float = 20.0,
        max_backoff: float = DEFAULT_WS_MAX_BACKOFF_SECS,
        *,
        recv_timeout_secs: float = DEFAULT_WS_RECV_TIMEOUT_SECS,
        ack_timeout_secs: float = 10.0,
        size_decimal: bool = False,
        on_reconnect: Callable[[], Any] | None = None,
    ) -> None:
        self.url = url
        self.product = product
        self.handler = handler
        self.heartbeat_secs = heartbeat_secs
        self.max_backoff = max_backoff
        self.recv_timeout_secs = recv_timeout_secs
        self.ack_timeout_secs = ack_timeout_secs
        self.size_decimal = size_decimal
        self.on_reconnect = on_reconnect

        self._api_key = api_key
        self._api_secret = api_secret
        self._loop = loop

        self._ws: ClientConnection | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._resubscribe_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._stopped = True

        self._request_ids = itertools.count(1)
        self._pending: dict[int, _PendingRequest] = {}
        self._subscriptions: dict[str, _Subscription] = {}

        # -- counters, for health reporting --------------------------------
        self.reconnects = 0
        self.messages_received = 0
        self.subscribe_failures = 0
        self.last_message_ns = 0
        self.last_pong_ns = 0

    # -- properties --------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Whether a live connection is currently established."""
        return self._ws is not None

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def subscriptions(self) -> list[tuple[str, Any]]:
        """The active subscriptions as ``(channel, payload)`` pairs."""
        return [(sub.channel, sub.payload) for sub in self._subscriptions.values()]

    @property
    def ping_channel(self) -> str:
        """The application-level ping channel for this product."""
        return f"{CHANNEL_PREFIX[self.product]}.ping"

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Open the connection and start the background receive loop.

        The initial handshake is awaited so that startup failures surface
        immediately; every later disconnection is handled by the receive loop.
        """
        if self._run_task is not None:
            return
        self._loop = self._loop or asyncio.get_running_loop()
        self._stopped = False
        await self._open()
        self._run_task = self._loop.create_task(self._run_forever(), name="gateio-ws-recv")
        self._heartbeat_task = self._loop.create_task(
            self._heartbeat_loop(), name="gateio-ws-heartbeat"
        )

    async def disconnect(self) -> None:
        """Close the connection and stop all background work."""
        self._stopped = True
        for task in (self._heartbeat_task, self._resubscribe_task, self._run_task):
            if task is not None:
                task.cancel()
        for task in (self._heartbeat_task, self._resubscribe_task, self._run_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                logger.debug("Background task ended with %s during disconnect", exc)
        self._heartbeat_task = None
        self._resubscribe_task = None
        self._run_task = None
        await self._close()
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()

    async def _open(self) -> None:
        headers = (
            dict(SIZE_DECIMAL_HEADER) if (self.size_decimal and self.product.is_futures) else None
        )
        self._ws = await connect(
            self.url,
            additional_headers=headers,
            max_size=MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
            close_timeout=5,
        )
        self.last_message_ns = time.time_ns()
        logger.debug("Connected to %s", self.url)

    async def _close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover - closing a dead socket
                pass

    # -- subscriptions -----------------------------------------------------

    async def subscribe(
        self,
        channel: str,
        payload: Any = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        """Subscribe to ``channel`` and wait for the venue's acknowledgement.

        The subscription is remembered and replayed automatically after a
        reconnect. Gate.io treats repeated subscriptions as additive, so
        replaying is safe.

        Parameters
        ----------
        channel : str
            Fully qualified channel name, e.g. ``spot.trades``.
        payload : Any
            Channel payload — a list for every channel, or, for the options
            underlying-scoped form, an object.
        auth : bool
            Sign the request with the configured credentials (private channels).

        Returns
        -------
        dict
            The acknowledgement message.

        Raises
        ------
        GateioError
            If the venue rejects the subscription or does not acknowledge it.
            A transport failure (``WS_NOT_CONNECTED`` during a reconnect window,
            ``WS_ACK_TIMEOUT``) still raises, but keeps the subscription
            registered so it is replayed on the next connection; only a venue
            rejection removes it. Callers can tell the two apart with
            :func:`is_transient_ws_error`.
        """
        subscription = _Subscription(channel=channel, payload=payload, auth=auth)
        self._subscriptions[subscription.key] = subscription
        try:
            return await self._request(channel, _SUBSCRIBE, payload, auth)
        except Exception as exc:
            self.subscribe_failures += 1
            if is_transient_ws_error(exc):
                logger.warning(
                    "Subscription to %s failed transiently (%s); keeping it for replay",
                    channel,
                    exc,
                )
                raise
            self._subscriptions.pop(subscription.key, None)
            raise

    async def unsubscribe(
        self,
        channel: str,
        payload: Any = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        """Unsubscribe from ``channel`` and stop replaying it after reconnects."""
        subscription = _Subscription(channel=channel, payload=payload, auth=auth)
        self._subscriptions.pop(subscription.key, None)
        return await self._request(channel, _UNSUBSCRIBE, payload, auth)

    async def _request(
        self,
        channel: str,
        event: str,
        payload: Any,
        auth: bool,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise GateioError(0, "WS_NOT_CONNECTED", f"not connected to {self.url}")

        request_id = next(self._request_ids)
        message = self._build_request(channel, event, payload, auth, request_id)
        loop = self._loop or asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = _PendingRequest(channel=channel, event=event, future=future)
        try:
            await self._send(message)
            ack = await asyncio.wait_for(future, timeout=self.ack_timeout_secs)
        except TimeoutError as exc:
            raise GateioError(
                0,
                "WS_ACK_TIMEOUT",
                f"no acknowledgement for {event} {channel} within {self.ack_timeout_secs}s",
            ) from exc
        finally:
            self._pending.pop(request_id, None)

        error = ack.get("error")
        if error:
            code = error.get("code", 0) if isinstance(error, dict) else 0
            text = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise GateioError(int(code or 0), "WS_REQUEST_REJECTED", f"{channel}: {text}")
        return ack

    def _build_request(
        self,
        channel: str,
        event: str,
        payload: Any,
        auth: bool,
        request_id: int,
    ) -> dict[str, Any]:
        timestamp = int(time.time())
        message: dict[str, Any] = {
            "time": timestamp,
            "id": request_id,
            "channel": channel,
            "event": event,
        }
        if payload is not None:
            message["payload"] = payload
        if auth:
            if not self.has_credentials:
                raise GateioError(
                    401,
                    "MISSING_CREDENTIALS",
                    f"channel {channel} is private and requires API credentials",
                )
            message["auth"] = ws_auth_payload(
                channel, event, timestamp, self._api_key, self._api_secret
            )
        return message

    async def _send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise GateioError(0, "WS_NOT_CONNECTED", f"not connected to {self.url}")
        async with self._send_lock:
            await ws.send(json.dumps(message))

    # -- receive loop ------------------------------------------------------

    async def _run_forever(self) -> None:
        """Own the connection: receive until it breaks, then reconnect."""
        while not self._stopped:
            try:
                await self._receive_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any transport failure is recoverable
                logger.warning("WebSocket receive failed on %s: %s", self.url, exc)
            if self._stopped:
                break
            await self._reconnect()

    async def _receive_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        while not self._stopped:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout_secs)
            except TimeoutError:
                logger.warning(
                    "No message from %s for %.0fs, recycling the connection",
                    self.url,
                    self.recv_timeout_secs,
                )
                return
            except ConnectionClosed as exc:
                logger.info("Connection to %s closed: %s", self.url, exc)
                return
            self.messages_received += 1
            self.last_message_ns = time.time_ns()
            self._dispatch(raw)

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Discarding malformed frame from %s", self.url)
            return
        if not isinstance(message, dict):
            return

        channel = message.get("channel", "")
        event = message.get("event")

        if event in (_SUBSCRIBE, _UNSUBSCRIBE):
            self._resolve_ack(message)
            return

        if channel.endswith(".pong"):
            self.last_pong_ns = time.time_ns()
            return

        if channel.endswith(".system"):
            self._handle_system(message)
            return

        self._invoke_handler(message)

    def _resolve_ack(self, message: dict[str, Any]) -> None:
        """Hand an acknowledgement to whichever request is waiting for it.

        Gate.io echoes the request ``id`` on every product; the channel and
        event pairing is used as a fallback so an acknowledgement is never lost
        if the venue omits it.
        """
        pending: _PendingRequest | None = None
        request_id = message.get("id")
        if request_id is not None:
            try:
                pending = self._pending.pop(int(request_id), None)
            except (TypeError, ValueError):
                pending = None
        if pending is None:
            channel = message.get("channel")
            event = message.get("event")
            for key, candidate in self._pending.items():
                if candidate.channel == channel and candidate.event == event:
                    pending = self._pending.pop(key)
                    break
        if pending is not None and not pending.future.done():
            pending.future.set_result(message)

    def _handle_system(self, message: dict[str, Any]) -> None:
        """React to a service notification such as a planned upgrade."""
        result = message.get("result")
        kind = result.get("type") if isinstance(result, dict) else None
        text = result.get("msg") if isinstance(result, dict) else None
        logger.info("Service notification from %s: %s %s", self.url, kind, text)
        if kind == "upgrade" and self._ws is not None:
            # The venue is about to close the socket; reconnect proactively.
            loop = self._loop or asyncio.get_running_loop()
            loop.create_task(self._close())

    def _invoke_handler(self, message: dict[str, Any]) -> None:
        try:
            outcome = self.handler(message)
        except Exception:  # noqa: BLE001 - a faulty handler must not kill the stream
            logger.exception("WebSocket handler raised for channel %s", message.get("channel"))
            return
        if asyncio.iscoroutine(outcome):
            loop = self._loop or asyncio.get_running_loop()
            loop.create_task(outcome)

    # -- reconnection ------------------------------------------------------

    async def _reconnect(self) -> None:
        """Reopen the connection with exponential backoff and replay subscriptions."""
        await self._close()
        backoff = 0.5
        while not self._stopped:
            try:
                await self._open()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep retrying any connect failure
                delay = min(backoff, self.max_backoff)
                delay *= 0.75 + random.random() * 0.5  # noqa: S311 - jitter, not cryptography
                logger.warning(
                    "Reconnect to %s failed (%s); retrying in %.1fs", self.url, exc, delay
                )
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            self.reconnects += 1
            logger.info("Reconnected to %s (attempt count %d)", self.url, self.reconnects)
            loop = self._loop or asyncio.get_running_loop()
            if self._resubscribe_task is not None and not self._resubscribe_task.done():
                self._resubscribe_task.cancel()
            self._resubscribe_task = loop.create_task(
                self._after_reconnect(), name="gateio-ws-resubscribe"
            )
            return

    async def _after_reconnect(self) -> None:
        """Replay every active subscription, then notify the owner."""
        for subscription in list(self._subscriptions.values()):
            try:
                await self._request(
                    subscription.channel,
                    _SUBSCRIBE,
                    subscription.payload,
                    subscription.auth,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report and continue with the rest
                self.subscribe_failures += 1
                logger.error(
                    "Failed to replay subscription %s %s: %s",
                    subscription.channel,
                    subscription.payload,
                    exc,
                )
        if self.on_reconnect is None:
            return
        try:
            outcome = self.on_reconnect()
            if asyncio.iscoroutine(outcome):
                await outcome
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the callback is owner code
            logger.exception("on_reconnect callback failed for %s", self.url)

    # -- heartbeat ---------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send an application-level ping whenever the stream goes quiet.

        Gate.io answers ``<product>.ping`` with ``<product>.pong`` and resets the
        server-side idle timer. The protocol-level ping issued by the transport
        already keeps the socket alive; this additionally proves the venue's
        application layer is still processing requests on a quiet stream.
        """
        while not self._stopped:
            await asyncio.sleep(1.0)
            if self._ws is None or self.heartbeat_secs <= 0:
                continue
            idle_secs = (time.time_ns() - self.last_message_ns) / 1e9
            if idle_secs < self.heartbeat_secs:
                continue
            try:
                await self._send({"time": int(time.time()), "channel": self.ping_channel})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the receive loop handles recovery
                logger.debug("Heartbeat ping failed on %s: %s", self.url, exc)

    # -- diagnostics -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of the connection counters for health reporting."""
        return {
            "url": self.url,
            "product": self.product.value,
            "connected": self.is_connected,
            "subscriptions": len(self._subscriptions),
            "reconnects": self.reconnects,
            "messages_received": self.messages_received,
            "subscribe_failures": self.subscribe_failures,
            "last_message_ns": self.last_message_ns,
            "last_pong_ns": self.last_pong_ns,
        }
