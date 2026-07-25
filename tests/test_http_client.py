"""Unit tests for :class:`nautilus_gateio.http.client.GateioHttpClient`.

All HTTP traffic is served by ``httpx.MockTransport`` — no network access and no
credentials are involved. Placeholder key/secret strings exercise the signing
code path only.

The retry-safety tests are the regression cover for the finding that mutating
requests (order submission, wallet transfer, margin borrow) were transparently
replayed on 5xx and on network timeouts, which can execute an order twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nautilus_gateio.common.constants import GATEIO_API_PREFIX, GATEIO_HTTP_MAINNET
from nautilus_gateio.common.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
)
from nautilus_gateio.common.signing import sign_request
from nautilus_gateio.http.client import (
    EXPIRY_HEADER,
    IDEMPOTENT_METHODS,
    GateioAmbiguousServerError,
    GateioHttpClient,
    GateioRequestAmbiguousError,
    RateLimiter,
)

API = GATEIO_API_PREFIX

Handler = Callable[[httpx.Request], httpx.Response]


class _RecordingLimiter:
    """Rate limiter stub that records pacing decisions without ever sleeping."""

    def __init__(self) -> None:
        self.acquired = 0
        self.rate_limited = 0
        self.successes = 0
        self.backoff = 0.0

    async def acquire(self) -> None:
        self.acquired += 1

    def on_rate_limited(self) -> None:
        self.rate_limited += 1
        self.backoff = min(self.backoff * 2 + 0.5, 10.0)

    def on_success(self) -> None:
        self.successes += 1
        self.backoff *= 0.5


def make_client(
    handler: Handler,
    api_key: str = "",
    api_secret: str = "",
    max_retries: int = 3,
) -> GateioHttpClient:
    """Build a client whose transport is a ``MockTransport`` around ``handler``.

    Both the rate limiter and the retry backoff are replaced with recorders so
    the tests neither sleep nor hide how often the client backed off.
    """
    client = GateioHttpClient(
        api_key=api_key,
        api_secret=api_secret,
        max_retries=max_retries,
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=GATEIO_HTTP_MAINNET,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    client._limiter = _RecordingLimiter()  # type: ignore[assignment]
    client.backoffs = []  # type: ignore[attr-defined]

    async def _retry_delay(attempt: int) -> None:
        client.backoffs.append(attempt)  # type: ignore[attr-defined]

    client._retry_delay = _retry_delay  # type: ignore[assignment]
    return client


def counting_handler(
    responses: list[httpx.Response | Exception],
    log: list[httpx.Request],
) -> Handler:
    """Serve ``responses`` in order, recording every request that was made."""

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        item = responses[min(len(log) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return handler


ORDER_BODY: dict[str, Any] = {
    "currency_pair": "BTC_USDT",
    "side": "buy",
    "amount": "0.001",
    "price": "50000",
    "type": "limit",
    "time_in_force": "gtc",
    "text": "t-ng-1",
}

ACCEPTED_ORDER: dict[str, Any] = {"id": "1001", "text": "t-ng-1", "status": "open"}


# -- signing ----------------------------------------------------------------


async def test_signed_request_carries_key_timestamp_and_signature_headers():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["url"] = request.url
        return httpx.Response(200, json=[])

    client = make_client(handler, api_key="test-key", api_secret="test-secret")
    client._timestamp = lambda: "1700000000"  # type: ignore[method-assign]
    await client.get("/spot/accounts", params={"currency": "USDT"}, signed=True)

    expected = sign_request(
        "GET",
        f"{API}/spot/accounts",
        "currency=USDT",
        "",
        "test-key",
        "test-secret",
        "1700000000",
    )
    headers = seen["headers"]
    assert headers["KEY"] == "test-key"
    assert headers["Timestamp"] == "1700000000"
    assert headers["SIGN"] == expected["SIGN"]
    assert seen["url"].raw_path.decode() == f"{API}/spot/accounts?currency=USDT"


async def test_public_request_is_not_signed():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        return httpx.Response(200, json={"server_time": 1})

    client = make_client(handler, api_key="k", api_secret="s")
    await client.get("/spot/time")

    assert "KEY" not in seen["headers"]
    assert "SIGN" not in seen["headers"]


async def test_signed_request_without_credentials_never_reaches_the_transport():
    log: list[httpx.Request] = []
    client = make_client(counting_handler([httpx.Response(200, json=[])], log))

    with pytest.raises(GateioError) as excinfo:
        await client.get("/spot/accounts", signed=True)

    assert excinfo.value.status == 401
    assert excinfo.value.label == "MISSING_CREDENTIALS"
    assert log == []


async def test_signature_covers_the_literal_json_body():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["content"] = request.content
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    client._timestamp = lambda: "1700000000"  # type: ignore[method-assign]
    await client.post("/spot/orders", body={"a": 1, "b": "x"})

    payload = seen["content"].decode()
    assert payload == '{"a":1,"b":"x"}'
    expected = sign_request("POST", f"{API}/spot/orders", "", payload, "k", "s", "1700000000")
    assert seen["headers"]["SIGN"] == expected["SIGN"]


# -- retry safety: mutating requests are not replayed ------------------------


async def test_500_on_post_spot_orders_is_not_retried():
    """Regression: a 5xx on order submission must not resubmit the order."""
    log: list[httpx.Request] = []
    handler = counting_handler(
        [httpx.Response(500, json={"label": "SERVER_ERROR", "message": "internal"})],
        log,
    )
    client = make_client(handler, api_key="k", api_secret="s")

    with pytest.raises(GateioServerError) as excinfo:
        await client.post("/spot/orders", body=ORDER_BODY)

    assert len(log) == 1, "the order POST was replayed after a 5xx"
    assert client.backoffs == []  # type: ignore[attr-defined]
    # The outcome is unknown, and the error says so without hiding the 5xx.
    assert isinstance(excinfo.value, GateioAmbiguousServerError)
    assert isinstance(excinfo.value, GateioRequestAmbiguousError)
    assert excinfo.value.status == 500
    assert excinfo.value.label == "SERVER_ERROR"


async def test_read_timeout_on_post_spot_orders_is_not_retried():
    """Regression: a timeout after the request was sent must not resubmit it."""
    log: list[httpx.Request] = []
    handler = counting_handler([httpx.ReadTimeout("timed out")], log)
    client = make_client(handler, api_key="k", api_secret="s")

    with pytest.raises(GateioRequestAmbiguousError) as excinfo:
        await client.post("/spot/orders", body=ORDER_BODY)

    assert len(log) == 1, "the order POST was replayed after a read timeout"
    assert excinfo.value.label == "REQUEST_AMBIGUOUS"
    assert "reconcile" in excinfo.value.message


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/spot/orders", ORDER_BODY),
        ("POST", "/futures/usdt/orders", {"contract": "BTC_USDT", "size": 1}),
        ("POST", "/options/orders", {"contract": "BTC_USDT-20260101-70000-C", "size": 1}),
        ("POST", "/wallet/transfers", {"currency": "USDT", "from": "spot", "to": "futures"}),
        ("POST", "/margin/uni/loans", {"currency": "USDT", "amount": "10"}),
        ("PUT", "/futures/usdt/orders/1", {"size": 2}),
        ("PATCH", "/spot/orders/1", {"amount": "2"}),
    ],
)
async def test_no_mutating_request_is_replayed_on_5xx(method: str, path: str, body: dict):
    """Every fund- or order-moving call follows the same non-retry rule."""
    log: list[httpx.Request] = []
    handler = counting_handler(
        [httpx.Response(502, json={"label": "SERVER_ERROR", "message": "bad gateway"})],
        log,
    )
    client = make_client(handler, api_key="k", api_secret="s")

    with pytest.raises(GateioRequestAmbiguousError):
        await client.request(method, path, body=body, signed=True)

    assert len(log) == 1


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/wallet/transfers", {"currency": "USDT", "from": "spot", "to": "futures"}),
        ("POST", "/margin/uni/loans", {"currency": "USDT", "amount": "10"}),
    ],
)
async def test_transfer_and_borrow_are_not_replayed_on_timeout(method: str, path: str, body: dict):
    log: list[httpx.Request] = []
    handler = counting_handler([httpx.ReadTimeout("timed out")], log)
    client = make_client(handler, api_key="k", api_secret="s")

    with pytest.raises(GateioRequestAmbiguousError):
        await client.request(method, path, body=body, signed=True)

    assert len(log) == 1


async def test_4xx_on_a_mutating_request_is_a_plain_client_error():
    """A deterministic rejection is not ambiguous and must not be flagged as such."""
    log: list[httpx.Request] = []
    handler = counting_handler(
        [httpx.Response(400, json={"label": "INVALID_PARAM_VALUE", "message": "bad amount"})],
        log,
    )
    client = make_client(handler, api_key="k", api_secret="s")

    with pytest.raises(GateioClientError) as excinfo:
        await client.post("/spot/orders", body=ORDER_BODY)

    assert not isinstance(excinfo.value, GateioRequestAmbiguousError)
    assert excinfo.value.label == "INVALID_PARAM_VALUE"
    assert len(log) == 1


async def test_connect_error_on_a_post_is_replayed_because_nothing_was_sent():
    """A failure to establish the connection proves the venue never saw it."""
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    result = await client.post("/spot/orders", body=ORDER_BODY)

    assert result == ACCEPTED_ORDER
    assert len(log) == 2
    assert client.backoffs == [1]  # type: ignore[attr-defined]


async def test_request_expired_label_on_a_post_is_replayed():
    """REQUEST_EXPIRED proves the venue rejected the request before processing."""
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(
                400, json={"label": "REQUEST_EXPIRED", "message": "deadline passed"}
            )
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    result = await client.post("/spot/orders", body=ORDER_BODY)

    assert result == ACCEPTED_ORDER
    assert len(log) == 2


# -- retry safety: idempotent requests are still replayed --------------------


async def test_get_is_retried_on_5xx():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(503, json={"label": "SERVER_ERROR", "message": "busy"})
        return httpx.Response(200, json=[{"currency": "USDT"}])

    client = make_client(handler, api_key="k", api_secret="s")
    result = await client.get("/spot/accounts", signed=True)

    assert result == [{"currency": "USDT"}]
    assert len(log) == 2
    assert client.backoffs == [1]  # type: ignore[attr-defined]


async def test_get_is_retried_on_read_timeout():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"server_time": 1700000000000})

    client = make_client(handler)
    result = await client.get("/spot/time")

    assert result == {"server_time": 1700000000000}
    assert len(log) == 2


async def test_delete_is_retried_on_5xx_because_cancelling_twice_is_harmless():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(500, json={"label": "SERVER_ERROR", "message": "internal"})
        return httpx.Response(200, json={"id": "1001", "status": "cancelled"})

    client = make_client(handler, api_key="k", api_secret="s")
    result = await client.delete("/spot/orders/1001", params={"currency_pair": "BTC_USDT"})

    assert result == {"id": "1001", "status": "cancelled"}
    assert len(log) == 2


async def test_persistent_5xx_on_a_get_raises_after_max_retries():
    log: list[httpx.Request] = []
    handler = counting_handler(
        [httpx.Response(500, json={"label": "SERVER_ERROR", "message": "internal"})],
        log,
    )
    client = make_client(handler, api_key="k", api_secret="s", max_retries=3)

    with pytest.raises(GateioServerError):
        await client.get("/spot/accounts", signed=True)

    assert len(log) == 3
    assert client.backoffs == [1, 2]  # type: ignore[attr-defined]


async def test_idempotent_method_set_is_explicit():
    assert IDEMPOTENT_METHODS == {"GET", "HEAD", "OPTIONS", "DELETE"}


# -- rate limiting -----------------------------------------------------------


async def test_429_on_a_get_is_retried_with_backoff():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(429, json={"label": "TOO_MANY_REQUESTS", "message": "slow down"})
        return httpx.Response(200, json=[])

    client = make_client(handler, api_key="k", api_secret="s")
    await client.get("/spot/accounts", signed=True)

    assert len(log) == 2
    limiter = client._limiter
    assert limiter.rate_limited == 1  # type: ignore[attr-defined]
    assert limiter.backoff > 0.0  # type: ignore[attr-defined]
    assert limiter.acquired == 2  # type: ignore[attr-defined]


async def test_429_on_an_order_post_is_retried_because_it_was_never_processed():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(429, json={"label": "TOO_MANY_REQUESTS", "message": "slow down"})
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    result = await client.post("/spot/orders", body=ORDER_BODY)

    assert result == ACCEPTED_ORDER
    assert len(log) == 2
    assert client._limiter.rate_limited == 1  # type: ignore[attr-defined]


async def test_persistent_429_raises_after_max_retries():
    log: list[httpx.Request] = []
    handler = counting_handler(
        [httpx.Response(429, json={"label": "TOO_MANY_REQUESTS", "message": "slow down"})],
        log,
    )
    client = make_client(handler, api_key="k", api_secret="s", max_retries=3)

    with pytest.raises(GateioError) as excinfo:
        await client.get("/spot/accounts", signed=True)

    assert excinfo.value.status == 429
    assert excinfo.value.label == "TOO_MANY_REQUESTS"
    assert len(log) == 3


async def test_rate_limiter_backoff_grows_and_decays():
    limiter = RateLimiter(max_per_second=100.0)
    assert limiter.backoff == 0.0

    limiter.on_rate_limited()
    assert limiter.backoff == pytest.approx(0.5)
    limiter.on_rate_limited()
    assert limiter.backoff == pytest.approx(1.5)

    for _ in range(20):
        limiter.on_rate_limited()
    assert limiter.backoff <= 10.0

    limiter.on_success()
    assert limiter.backoff == pytest.approx(5.0)


async def test_rate_limiter_applies_its_backoff_when_acquiring():
    limiter = RateLimiter(max_per_second=1000.0)
    limiter.on_rate_limited()  # backoff = 0.5s

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.45


# -- error translation -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "label", "expected"),
    [
        (400, "INVALID_PARAM_VALUE", GateioClientError),
        (401, "INVALID_SIGNATURE", GateioClientError),
        (404, "ORDER_NOT_FOUND", GateioClientError),
        (500, "SERVER_ERROR", GateioServerError),
    ],
)
async def test_error_status_maps_to_the_typed_error(status: int, label: str, expected: type):
    handler = counting_handler(
        [httpx.Response(status, json={"label": label, "message": "boom"})], []
    )
    client = make_client(handler, api_key="k", api_secret="s", max_retries=1)

    with pytest.raises(expected) as excinfo:
        await client.get("/spot/accounts", signed=True)

    assert excinfo.value.status == status
    assert excinfo.value.label == label
    assert excinfo.value.message == "boom"
    assert f"Gate.io {status} {label}: boom" == str(excinfo.value)


async def test_error_label_is_preserved_verbatim():
    """Labels drive caller branching, so they must never be rewritten."""
    handler = counting_handler(
        [httpx.Response(400, json={"label": "REPEATED_CREATION", "message": "duplicate text"})],
        [],
    )
    client = make_client(handler, api_key="k", api_secret="s")

    with pytest.raises(GateioClientError) as excinfo:
        await client.post("/spot/orders", body=ORDER_BODY)

    assert excinfo.value.label == "REPEATED_CREATION"
    assert excinfo.value.message == "duplicate text"


async def test_non_json_error_body_is_translated_without_crashing():
    handler = counting_handler([httpx.Response(503, text="<html>gateway error</html>")], [])
    client = make_client(handler, max_retries=1)

    with pytest.raises(GateioServerError) as excinfo:
        await client.get("/spot/time")

    assert excinfo.value.label == "HTTP_ERROR"
    assert "gateway error" in excinfo.value.message


async def test_error_body_using_detail_instead_of_message():
    handler = counting_handler(
        [httpx.Response(400, json={"label": "BAD_REQUEST", "detail": "malformed"})], []
    )
    client = make_client(handler, max_retries=1)

    with pytest.raises(GateioClientError) as excinfo:
        await client.get("/spot/time")

    assert excinfo.value.message == "malformed"


# -- request encoding --------------------------------------------------------


async def test_query_encoding_drops_none_and_lowercases_booleans():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw_path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={})

    client = make_client(handler)
    await client.get(
        "/spot/order_book",
        params={"currency_pair": "BTC_USDT", "interval": None, "with_id": True, "limit": 10},
    )

    assert seen["raw_path"] == f"{API}/spot/order_book?currency_pair=BTC_USDT&with_id=true&limit=10"


async def test_empty_response_body_returns_none():
    client = make_client(lambda r: httpx.Response(204), api_key="k", api_secret="s")
    assert await client.post("/margin/uni/loans", body={"currency": "USDT"}) is None


# -- submission deadline (x-gate-exptime) ------------------------------------


async def test_expiry_header_is_withheld_until_the_clock_is_synced():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    assert client.clock_synced is False
    await client.post("/spot/orders", body=ORDER_BODY, expiring=True)

    assert EXPIRY_HEADER not in seen["headers"]


async def test_expiry_header_is_sent_after_sync_time():
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        if request.url.path.endswith("/spot/time"):
            return httpx.Response(200, json={"server_time": int(time.time() * 1000)})
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    await client.sync_time()
    assert client.clock_synced is True

    await client.post("/spot/orders", body=ORDER_BODY, expiring=True)

    expiry = int(seen[-1][EXPIRY_HEADER])
    now_ms = int(time.time() * 1000)
    assert now_ms < expiry <= now_ms + client.order_expiry_ms + 2_000


async def test_expiry_header_is_absent_on_non_expiring_requests():
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        if request.url.path.endswith("/spot/time"):
            return httpx.Response(200, json={"server_time": int(time.time() * 1000)})
        return httpx.Response(200, json=[])

    client = make_client(handler, api_key="k", api_secret="s")
    await client.sync_time()
    await client.get("/spot/accounts", signed=True)

    assert EXPIRY_HEADER not in seen[-1]


async def test_expiry_header_can_be_disabled():
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        if request.url.path.endswith("/spot/time"):
            return httpx.Response(200, json={"server_time": int(time.time() * 1000)})
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    client.order_expiry_ms = 0
    await client.sync_time()
    await client.post("/spot/orders", body=ORDER_BODY, expiring=True)

    assert EXPIRY_HEADER not in seen[-1]


async def test_sync_time_offset_is_applied_to_the_signed_timestamp():
    offset_ms = 5_000_000  # pretend the venue clock is 5000 s ahead

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"server_time": int(time.time() * 1000) + offset_ms})

    client = make_client(handler)
    measured = await client.sync_time()

    assert abs(measured - offset_ms) < 2_000
    signed = int(client._timestamp())
    assert abs(signed - (time.time() + offset_ms / 1000)) < 5


# -- transport ownership and shutdown ---------------------------------------


async def test_close_is_reference_counted_across_owners():
    client = make_client(lambda r: httpx.Response(200, json={}))

    client.acquire()  # data client
    client.acquire()  # execution client
    assert client.owner_count == 2

    await client.close()
    assert client.is_closed is False, "the transport closed while an owner still held it"
    assert client._client.is_closed is False

    await client.close()
    assert client.is_closed is True
    assert client._client.is_closed is True


async def test_close_is_idempotent_without_any_owner():
    client = make_client(lambda r: httpx.Response(200, json={}))

    await client.close()
    await client.close()

    assert client.is_closed is True
    assert client.owner_count == 0


async def test_requests_after_close_fail_with_a_clear_error():
    log: list[httpx.Request] = []
    client = make_client(counting_handler([httpx.Response(200, json={})], log))
    await client.close()

    with pytest.raises(GateioError) as excinfo:
        await client.get("/spot/time")

    assert excinfo.value.label == "CLIENT_CLOSED"
    assert log == []


async def test_acquire_after_close_is_refused():
    client = make_client(lambda r: httpx.Response(200, json={}))
    await client.close()

    with pytest.raises(GateioError) as excinfo:
        client.acquire()

    assert excinfo.value.label == "CLIENT_CLOSED"


async def test_async_context_manager_acquires_and_closes():
    client = make_client(lambda r: httpx.Response(200, json={"server_time": 1}))

    async with client as acquired:
        assert acquired is client
        assert client.owner_count == 1
        assert await client.get("/spot/time") == {"server_time": 1}

    assert client.is_closed is True
    assert client._client.is_closed is True
