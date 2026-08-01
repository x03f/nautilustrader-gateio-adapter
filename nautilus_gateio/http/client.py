"""Base asynchronous HTTP client for the Gate.io API v4.

Responsibilities:

* signing private requests (HMAC-SHA512, see :mod:`nautilus_gateio.common.signing`);
* pacing requests and backing off on HTTP 429;
* translating error payloads into the typed :mod:`nautilus_gateio.common.errors` hierarchy;
* replaying only those failed requests whose replay provably cannot change the
  outcome at the venue (see `Retry safety`_);
* exposing a single ``request`` entry point for the typed per-product namespaces.

Scope note: this adapter implements Gate.io's *trading* APIs (spot, margin,
perpetual and delivery futures, options) plus internal transfers between the
account's own trading wallets. Withdrawals and unrelated products (Earn, Gate
Pay, P2P, Copy Trading) are simply not implemented; restricting what an
individual API key may do is a matter for the key's permissions.

.. _Retry safety:

Retry safety
------------
A transparent retry of a mutating request can execute it twice. Gate.io offers
no request-level idempotency token, so this client never replays a request
unless the replay is provably harmless:

* ``GET``, ``HEAD``, ``OPTIONS`` and ``DELETE`` are idempotent — Gate.io's
  cancel endpoints report ``ORDER_NOT_FOUND`` / ``ORDER_CLOSED`` on a second
  cancel rather than performing a second action — so these are replayed on
  transient failures.
* ``POST``, ``PUT`` and ``PATCH`` (order submission and amendment, wallet
  transfers, margin borrow/repay, leverage and position changes) are replayed
  **only** when the venue has stated that the request was rejected before it was
  processed: HTTP 429, or the labels in :data:`UNPROCESSED_LABELS`.
* Every other failure of a mutating request is raised, never replayed. When the
  outcome is genuinely unknown — the request was already on the wire, or the
  venue answered 5xx — the error raised is a
  :class:`GateioRequestAmbiguousError`, which tells the caller to reconcile the
  order/transfer state instead of resubmitting.
* Replaying does not make an outcome known. A request that reached the venue and
  was never answered, however many times it was replayed, also raises
  :class:`GateioRequestAmbiguousError`; ``NETWORK_ERROR`` is reserved for the
  case where no byte of any attempt left this process. A cancel is the reason
  this matters: ``DELETE`` is replayed, so it would otherwise report a
  definitive failure for an order the venue had in fact already cancelled.

The client id Gate.io carries in an order's ``text`` field is *not* treated as
an idempotency key here. The venue rejects a duplicate ``text`` with
``REPEATED_CREATION`` only while it still knows the id; an order that filled and
closed inside the retry window would not be deduplicated, so a replay built on
``text`` alone is not provably safe. The execution layer should instead resolve
an ambiguous submission by querying the order by its client id.

The client attaches Gate.io's ``x-gate-exptime`` header (a submission deadline in
milliseconds) to order-mutating requests. That does not make a replay safe, but
it bounds how late a request delayed in flight may still be accepted, which
bounds how long the ambiguity can last. The header is emitted only once
:meth:`GateioHttpClient.sync_time` has measured the venue clock offset, because
an unsynchronised clock would otherwise expire valid requests. The execution
client makes that measurement when it connects, so the deadline is in force for
the whole session; a client used on its own must call :meth:`sync_time` itself,
or set ``order_expiry_ms=0`` and accept unbounded submissions knowingly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from types import TracebackType
from typing import Any, Final

import httpx
from nautilus_trader.live.cancellation import DEFAULT_FUTURE_CANCELLATION_TIMEOUT

from nautilus_gateio.common.constants import (
    DEFAULT_HTTP_TIMEOUT_SECS,
    DEFAULT_MAX_REQUESTS_PER_SECOND,
    GATEIO_API_PREFIX,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
)
from nautilus_gateio.common.credentials import (
    ENV_API_KEY,
    ENV_API_SECRET,
    ENV_TESTNET_API_KEY,
    ENV_TESTNET_API_SECRET,
)
from nautilus_gateio.common.errors import (
    GateioError,
    GateioServerError,
    error_from_response,
    should_retry,
)
from nautilus_gateio.common.signing import sign_request

#: HTTP methods whose replay cannot change the outcome at the venue.
IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})

#: Gate.io labels proving the request was rejected *before* being processed.
#: A mutating request that fails with one of these may be replayed safely.
UNPROCESSED_LABELS: Final[frozenset[str]] = frozenset(
    {
        "TOO_MANY_REQUESTS",
        "REQUEST_EXPIRED",
    }
)

#: Gate.io's submission-deadline header, in milliseconds since the epoch.
EXPIRY_HEADER: Final[str] = "x-gate-exptime"

#: Offset between this host's clock and the venue's, in milliseconds, beyond
#: which a caller of :meth:`GateioHttpClient.sync_time` should tell the operator.
#: The offset corrects what this client sends; it cannot correct the timestamps
#: the platform records, which are read from the local clock.
CLOCK_DRIFT_WARNING_MS: Final[int] = 5_000

#: Default submission deadline applied to order-mutating requests, in
#: milliseconds. Generous enough to absorb ordinary latency while still bounding
#: how late a delayed request may be accepted.
DEFAULT_ORDER_EXPIRY_MS: Final[int] = 10_000

#: Transport failures that occur before any byte of the request is sent, which
#: means the venue cannot have seen it. Replaying these is safe for any method.
_UNSENT_ERRORS: Final[tuple[type[httpx.HTTPError], ...]] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)

_RETRY_DELAY_INITIAL_SECS: Final[float] = 1.0
_RETRY_DELAY_MAX_SECS: Final[float] = 5.0


class GateioRequestAmbiguousError(GateioError):
    """A request failed after it may already have reached the venue.

    Either it was not replayed, because a replay could execute it twice, or it
    was replayed and never answered. Whether the venue applied it is unknown:
    the caller must reconcile (query the order by client id, poll the transfer
    status) before deciding to resubmit. This is a
    :class:`~nautilus_gateio.common.errors.GateioError`, so existing handlers
    still catch it.
    """


class GateioAmbiguousServerError(GateioServerError, GateioRequestAmbiguousError):
    """Gate.io answered 5xx to a mutating request, so the outcome is unknown.

    A 5xx can be raised either before or after the venue accepted the request,
    and it was deliberately not replayed. Subclasses
    :class:`~nautilus_gateio.common.errors.GateioServerError` so that code
    branching on server errors is unaffected, and
    :class:`GateioRequestAmbiguousError` so that a single ``isinstance`` check
    identifies "this mutation may or may not have been applied".
    """


class RateLimiter:
    """Minimum-interval pacing with exponential backoff on HTTP 429."""

    def __init__(self, max_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND) -> None:
        self.min_interval = 1.0 / max_per_second
        self._last = 0.0
        self._backoff = 0.0

    @property
    def backoff(self) -> float:
        """The extra delay currently added to every acquisition, in seconds."""
        return self._backoff

    async def acquire(self) -> None:
        delay = max(0.0, self.min_interval - (time.monotonic() - self._last)) + self._backoff
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.monotonic()

    def on_rate_limited(self) -> None:
        self._backoff = min(self._backoff * 2 + 0.5, 10.0)

    def on_success(self) -> None:
        self._backoff *= 0.5
        if self._backoff < 1e-3:
            self._backoff = 0.0


class GateioHttpClient:
    """Shared transport for every Gate.io REST namespace.

    The instance is shared by the data client, the execution client and the
    instrument provider, so its shutdown is reference counted: each owner calls
    :meth:`acquire` when it takes the client and :meth:`close` when it releases
    it, and the underlying transport is closed exactly once, by the last owner.
    :meth:`close` is idempotent and safe to call from a client that never
    acquired.

    Shutdown has **two** states, not one, and they are deliberately separate:

    * *accepting* (:attr:`is_accepting`, closed by :meth:`stop_accepting`) is
      the gate — "no further request will leave this process". It is synchronous,
      sticky and global to the transport;
    * *closed* (:attr:`is_closed`, set by the last :meth:`close`) is the socket
      pool — "there are no connections left".

    Conflating the two is what made teardown leak. A request already parked in
    the rate limiter has passed every entry check; when the pool was closed
    underneath it, ``httpx`` raised a bare ``RuntimeError`` ("Cannot send a
    request, as the client has been closed."), which is *not* an
    ``httpx.HTTPError``, so the attempt loop in :meth:`request` never saw it,
    never retried and never classified it. The gate refuses that request before
    the send instead, with a typed adapter error.

    Parameters
    ----------
    api_key, api_secret : str
        Credentials for private endpoints; public endpoints work without them.
    base_url : str
        REST host (mainnet by default).
    max_requests_per_second : float
        Client-side pacing target.
    timeout_secs : float
        Per-request timeout. Must be above ``0``: httpx treats ``0`` as "expire
        immediately", so every request fails before it is sent and is reported
        as a network error the network never saw.
    max_retries : int
        Total attempts for a request whose replay is provably safe; see
        `Retry safety`_. Mutating requests are not replayed regardless. Must be
        at least ``1``: the attempt loop runs ``max_retries`` times, so ``0``
        means the request is never sent, and the caller is then told
        ``TOO_MANY_REQUESTS`` about a request that never left the process.
    order_expiry_ms : int
        Submission deadline attached to order-mutating requests via
        ``x-gate-exptime``. Applied only after :meth:`sync_time` has measured
        the venue clock offset, which the execution client does on connect; set
        to ``0`` to disable the header entirely.

    Raises
    ------
    ValueError
        If ``max_retries`` is below ``1``, or ``timeout_secs`` is not a finite
        number above ``0``.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = GATEIO_HTTP_MAINNET,
        max_requests_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND,
        timeout_secs: float = DEFAULT_HTTP_TIMEOUT_SECS,
        max_retries: int = 3,
        order_expiry_ms: int = DEFAULT_ORDER_EXPIRY_MS,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self.base_url = base_url
        if max_retries < 1:
            # This used to be `max(1, max_retries)`. Silently promoting the
            # operator's number meant a deployment that asked for no retries got
            # one attempt and nothing said so; and the config layer now refuses
            # the same value outright, so clamping here would leave the two
            # doors disagreeing about what `max_retries=0` means.
            raise ValueError(
                f"`max_retries` must be at least 1 (the attempt loop runs it as a count), "
                f"was {max_retries}",
            )
        if not math.isfinite(timeout_secs) or timeout_secs <= 0:
            # Same reasoning as `max_retries`, and the same door: this
            # constructor is public and the README drives it directly. Zero is
            # not "no limit" to httpx, it is "already expired", so every request
            # would be refused before it left the process and reported as
            # `NETWORK_ERROR` — a venue failure the venue never had.
            raise ValueError(
                f"`timeout_secs` must be a finite number above 0 (0 expires every request "
                f"before it is sent), was {timeout_secs!r}",
            )
        self.max_retries = max_retries
        self.order_expiry_ms = order_expiry_ms
        self._limiter = RateLimiter(max_requests_per_second)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_secs,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self._time_offset_ms = 0
        self._clock_synced = False
        self._owners = 0
        self._closed = False
        #: The gate. `False` means no further request leaves this process.
        self._accepting = True
        #: Requests currently between `self._client.request(...)` and its return.
        self._inflight = 0
        #: Set exactly while `_inflight` is zero, so `close` can await quiet.
        self._drained = asyncio.Event()
        self._drained.set()

    # -- lifecycle ---------------------------------------------------------

    def stop_accepting(self) -> None:
        """Close the gate: no further request leaves this process.

        Synchronous, idempotent, never raises, and **sticky** — there is no
        ``reopen()``. NautilusTrader's own adapters expose the same verb as
        ``cancel_all_requests()`` (``adapters/bybit/execution.py``,
        ``adapters/okx/execution.py``, ``adapters/kraken/data.py``,
        ``adapters/bitmex/data.py``) and it is sticky there too.

        This is the first statement of both clients' ``_disconnect``, which is
        what makes the *order* of everything below it immaterial: whoever births
        a request during teardown — a WebSocket frame, a sleeping poller, a
        retry armed before the stop — meets the gate rather than a half-closed
        socket pool.

        The gate is **global to this transport**, which is shared by the data
        client, the execution client and the instrument provider. A component
        that stops therefore mutes REST for every other holder. That is what the
        bundled adapters do (``adapters/bybit/factories.py`` hands one HTTP
        client to both clients and both call the gate in ``_disconnect``), and it
        is safe under NautilusTrader because the engines disconnect all of their
        clients together (``live/data_engine.py``, ``live/execution_engine.py``)
        and the kernel disconnects both engines in succession. Code that calls
        ``disconnect()`` on one client by hand while another keeps trading is the
        case this does not serve; see ``docs/architecture.md``.
        """
        self._accepting = False

    @property
    def is_accepting(self) -> bool:
        """Whether the transport still lets new requests onto the wire."""
        return self._accepting

    def acquire(self) -> GateioHttpClient:
        """Register an owner of this shared transport.

        Every component that holds the client (data client, execution client,
        instrument provider) calls this once when it takes ownership, and
        :meth:`close` once when it releases it. Returns ``self`` so the call can
        be chained onto a factory lookup.
        """
        if self._closed:
            raise GateioError(
                0,
                "CLIENT_CLOSED",
                "cannot acquire a closed GateioHttpClient; build a new one",
            )
        self._owners += 1
        return self

    async def close(self) -> None:
        """Release one owner's reference and close the transport on the last.

        Idempotent: closing an already-closed client, or closing without ever
        having acquired, is a no-op rather than an error, so shutdown paths do
        not need to coordinate.

        Two things happen before the pool is torn down, and both are visible to
        every other owner:

        * the gate closes unconditionally, even when this is *not* the last
          owner, because nothing may be handed a pool that is about to
          disappear (see :meth:`stop_accepting`);
        * the last owner then waits, briefly, for the requests already on the
          wire to be answered, so a cancel in flight receives the venue's real
          answer instead of a fabricated ``REQUEST_AMBIGUOUS``. The wait is
          bounded by NautilusTrader's own budget for external connections
          (``DEFAULT_FUTURE_CANCELLATION_TIMEOUT``, 2 s) because the node's whole
          disconnect budget is ``timeout_disconnection`` (10 s by default) and
          the WebSockets already spend most of it.

        Note that ``is_closed`` flips *before* the drain, so a second concurrent
        call returns while the first is still waiting for ``aclose()``. Callers
        that need "the pool is gone" must await the call that owns the close.
        """
        # The gate always precedes the pool.
        self.stop_accepting()
        if self._owners > 0:
            self._owners -= 1
            if self._owners > 0:
                return
        if self._closed:
            return
        self._closed = True
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._drained.wait(), DEFAULT_FUTURE_CANCELLATION_TIMEOUT)
        finally:
            # The drain introduced an await between latching `_closed` and
            # closing the pool, and `_closed` is what makes every later call a
            # no-op. A cancellation landing in that window would therefore leave
            # a pool that reports itself closed and that nothing in the process
            # can ever close again — and the window is reached on the platform's
            # own path, because `TradingNode.dispose()` cancels whatever the
            # disconnect budget did not finish. `contextlib.suppress(TimeoutError)`
            # does not catch `CancelledError`, so the close has to be in a
            # `finally`; shielded, because a task being torn down can be
            # cancelled again while it is running its own cleanup, and the pool
            # must be released either way.
            await asyncio.shield(self._client.aclose())

    @property
    def is_closed(self) -> bool:
        """Whether the underlying transport has been closed."""
        return self._closed

    @property
    def owner_count(self) -> int:
        """The number of components currently holding this transport."""
        return self._owners

    async def __aenter__(self) -> GateioHttpClient:
        return self.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _credential_env_names(self) -> str:
        """Name the environment variables *this* client would have read.

        A testnet client that is told to set the mainnet pair sends the operator
        into the fallback trap: with only the mainnet key exported, a testnet run
        signs with it against the testnet host and the venue answers
        ``INVALID_SIGNATURE``, which reads like a signing bug and is a missing
        account. So the message names the pair the configured host actually
        resolves.
        """
        if self.base_url == GATEIO_HTTP_TESTNET:
            return f"{ENV_TESTNET_API_KEY} / {ENV_TESTNET_API_SECRET}"
        return f"{ENV_API_KEY} / {ENV_API_SECRET}"

    # -- clock -------------------------------------------------------------

    async def sync_time(self) -> int:
        """Measure the offset between the local clock and the venue's.

        Signatures embed a timestamp, so a drifting clock produces
        ``INVALID_SIGNATURE`` errors; the offset is applied to every subsequent
        signed request. It also enables the ``x-gate-exptime`` submission
        deadline, which is withheld until the offset is known.

        The measurement is a single reading, so the network round trip inflates
        it by roughly the one-way delay — immaterial against a deadline measured
        in seconds, and it is not a substitute for a synchronised host clock:
        the platform timestamps its own events from the local clock, which this
        offset does not touch. Beyond :data:`CLOCK_DRIFT_WARNING_MS` the caller
        is expected to say so.
        """
        payload = await self.request("GET", "/spot/time")
        server_ms = int(payload["server_time"])
        self._time_offset_ms = server_ms - int(time.time() * 1000)
        self._clock_synced = True
        return self._time_offset_ms

    @property
    def clock_synced(self) -> bool:
        """Whether :meth:`sync_time` has measured the venue clock offset."""
        return self._clock_synced

    def _venue_time_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _timestamp(self) -> str:
        return str(self._venue_time_ms() // 1000)

    def _expiry_ms(self) -> int | None:
        """The ``x-gate-exptime`` value for a request submitted now, if enabled."""
        if not self._clock_synced or self.order_expiry_ms <= 0:
            return None
        return self._venue_time_ms() + self.order_expiry_ms

    # -- retry policy ------------------------------------------------------

    @staticmethod
    def _is_idempotent(method: str) -> bool:
        return method.upper() in IDEMPOTENT_METHODS

    async def _retry_delay(self, attempt: int) -> None:
        """Wait before replaying a request; exponential in ``attempt``, capped."""
        await asyncio.sleep(
            min(_RETRY_DELAY_INITIAL_SECS * 2 ** (attempt - 1), _RETRY_DELAY_MAX_SECS)
        )

    # -- core request ------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        signed: bool = False,
        *,
        expiring: bool = False,
    ) -> Any:
        """Perform a Gate.io API call and return the decoded payload.

        ``params`` are URL query parameters (``None`` values are dropped) and
        ``body`` is JSON-encoded. Signed requests require credentials. Set
        ``expiring`` on order-mutating endpoints that accept Gate.io's
        ``x-gate-exptime`` submission deadline.

        Failures are replayed only when a replay is provably harmless; see
        `Retry safety`_. A mutating request whose outcome is unknown raises
        :class:`GateioRequestAmbiguousError`.
        """
        if self._closed or not self._accepting:
            raise GateioError(
                0,
                "CLIENT_CLOSED",
                "the GateioHttpClient is no longer accepting requests; nothing was sent",
            )
        if signed and not self.has_credentials:
            raise GateioError(
                401,
                "MISSING_CREDENTIALS",
                f"this endpoint requires API credentials (set {self._credential_env_names()})",
            )

        method = method.upper()
        idempotent = self._is_idempotent(method)
        query = _encode_query(params)
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url_path = GATEIO_API_PREFIX + path
        url = url_path + (f"?{query}" if query else "")

        last_error: Exception | None = None
        reached_venue = False
        for attempt in range(1, self.max_retries + 1):
            final_attempt = attempt >= self.max_retries
            await self._limiter.acquire()
            headers = self._build_headers(method, url_path, query, payload, signed, expiring)
            # The last statement before the send, with no `await` between the two:
            # this attempt may have slept in the limiter or in `_retry_delay`
            # while the node started shutting down. Anything inserted in between
            # reopens the window silently, which is why a test drives the gate
            # shut from inside `_build_headers`.
            if not self._accepting:
                if reached_venue:
                    # An earlier attempt was answered, so `CLIENT_CLOSED` would
                    # be a lie: the execution layer treats a plain `GateioError`
                    # as a definitive outcome and would reject a cancel Gate.io
                    # may already have applied.
                    raise GateioRequestAmbiguousError(
                        0,
                        "REQUEST_AMBIGUOUS",
                        f"{method} {path} reached the venue and the transport stopped accepting "
                        "before it was answered; it may or may not have been applied - "
                        "reconcile before resubmitting",
                    ) from last_error
                raise GateioError(
                    0,
                    "CLIENT_CLOSED",
                    f"{method} {path} was not sent: the transport stopped accepting requests "
                    "(the client is disconnecting)",
                )
            try:
                # `finally`, not the success path: a raise out of the error
                # branches below would otherwise leave the counter up forever and
                # make every later shutdown pay the full drain timeout in
                # silence. The counted region is the send alone — a request
                # parked in `_retry_delay` is not on the wire and must not hold
                # the drain open.
                self._inflight += 1
                self._drained.clear()
                try:
                    response = await self._client.request(
                        method,
                        url,
                        headers=headers,
                        content=payload or None,
                    )
                finally:
                    self._inflight -= 1
                    if self._inflight == 0:
                        self._drained.set()
            except httpx.HTTPError as exc:
                unsent = isinstance(exc, _UNSENT_ERRORS)
                reached_venue = reached_venue or not unsent
                if not idempotent and not unsent:
                    # The request was already on the wire: replaying it could
                    # execute it twice, so surface the ambiguity instead.
                    raise GateioRequestAmbiguousError(
                        0,
                        "REQUEST_AMBIGUOUS",
                        f"{method} {path} failed after the request was sent ({str(exc)[:160]}); "
                        "it may or may not have been applied - reconcile before resubmitting",
                    ) from exc
                last_error = exc
                if final_attempt:
                    if reached_venue:
                        # An idempotent request may be replayed safely, but a
                        # replay makes a duplicate harmless, not the outcome
                        # known: a cancel Gate.io applied and could not report
                        # back is indistinguishable here from one it never
                        # received. Only the caller can resolve that, and it
                        # must not be told the command definitively failed.
                        raise GateioRequestAmbiguousError(
                            0,
                            "REQUEST_AMBIGUOUS",
                            f"{method} {path} reached the venue and was never answered "
                            f"({str(exc)[:160]}); it may or may not have been applied - "
                            "reconcile before resubmitting",
                        ) from exc
                    # Every attempt failed before a byte left this process, so
                    # the venue cannot have seen the request.
                    raise GateioError(0, "NETWORK_ERROR", str(exc)[:200]) from exc
                await self._retry_delay(attempt)
                continue

            # The venue answered, whatever the status: a later attempt that
            # fails before sending no longer proves the request was unseen.
            reached_venue = True

            if response.status_code == 429:
                # Rejected by the venue's rate limiter before processing, so a
                # replay cannot duplicate the effect of any method.
                self._limiter.on_rate_limited()
                last_error = error_from_response(429, "TOO_MANY_REQUESTS", "rate limited")
                if final_attempt:
                    raise last_error
                continue

            self._limiter.on_success()

            if response.status_code >= 400:
                try:
                    data = response.json()
                except Exception:
                    data = {"label": "HTTP_ERROR", "message": response.text[:200]}
                error = error_from_response(
                    response.status_code,
                    str(data.get("label", "")),
                    str(data.get("message", data.get("detail", ""))),
                )
                replayable = idempotent or error.label in UNPROCESSED_LABELS
                if replayable and should_retry(error) and not final_attempt:
                    last_error = error
                    await self._retry_delay(attempt)
                    continue
                if not idempotent and isinstance(error, GateioServerError):
                    # 5xx may be reported before or after the venue applied the
                    # request; do not replay, and say the outcome is unknown.
                    raise GateioAmbiguousServerError(error.status, error.label, error.message)
                raise error

            if not response.content:
                return None
            return response.json()

        raise last_error or GateioError(429, "TOO_MANY_REQUESTS", "retries exhausted")

    def _build_headers(
        self,
        method: str,
        url_path: str,
        query: str,
        payload: str,
        signed: bool,
        expiring: bool,
    ) -> dict[str, str] | None:
        """Build per-attempt headers: signature and submission deadline.

        Both are recomputed on every attempt, because a stale timestamp fails
        signature validation and a stale deadline expires the request.
        """
        headers: dict[str, str] = {}
        if signed:
            headers.update(
                sign_request(
                    method,
                    url_path,
                    query,
                    payload,
                    self._api_key,
                    self._api_secret,
                    self._timestamp(),
                )
            )
        if expiring:
            expiry = self._expiry_ms()
            if expiry is not None:
                headers[EXPIRY_HEADER] = str(expiry)
        return headers or None

    # -- convenience -------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None, signed: bool = False):
        return await self.request("GET", path, params=params, signed=signed)

    async def post(
        self,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        signed: bool = True,
        *,
        expiring: bool = False,
    ):
        return await self.request(
            "POST", path, params=params, body=body, signed=signed, expiring=expiring
        )

    async def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        signed: bool = True,
        *,
        expiring: bool = False,
    ):
        return await self.request(
            "DELETE", path, params=params, body=body, signed=signed, expiring=expiring
        )

    async def put(
        self,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        *,
        expiring: bool = False,
    ):
        return await self.request(
            "PUT", path, params=params, body=body, signed=True, expiring=expiring
        )

    async def patch(
        self,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        *,
        expiring: bool = False,
    ):
        return await self.request(
            "PATCH", path, params=params, body=body, signed=True, expiring=expiring
        )


def _encode_query(params: dict[str, Any] | None) -> str:
    """Encode query parameters the way Gate.io signs them: ``k=v`` joined by ``&``.

    ``None`` values are omitted and booleans are lower-cased, matching the venue's
    expectations. Keys are kept in insertion order — Gate.io signs the literal
    query string, so the encoded form and the transmitted form must agree.
    """
    if not params:
        return ""
    parts = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{key}={value}")
    return "&".join(parts)
