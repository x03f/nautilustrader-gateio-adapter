"""Unit tests for :class:`gateio_nt.http.client.GateioHttpClient`.

All HTTP traffic is served by ``httpx.MockTransport`` — no network access and no
credentials are involved. Placeholder key/secret strings exercise the signing
code path only.

The retry-safety tests are the regression cover for the finding that mutating
requests (order submission, wallet transfer, margin borrow) were transparently
replayed on 5xx and on network timeouts, which can execute an order twice.

The teardown tests at the end are the regression cover for the transport gate:
"stopped accepting" is a state of its own, separate from "closed", because a
request parked in the rate limiter used to wake to a closed socket pool and
raise a bare ``RuntimeError`` that no handler in this adapter classifies.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from gateio_nt.common.constants import (
    GATEIO_API_PREFIX,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
)
from gateio_nt.common.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
)
from gateio_nt.common.signing import sign_request
from gateio_nt.http.client import (
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


async def test_the_missing_credential_message_names_the_pair_for_this_host():
    """A testnet client must not send the operator to the mainnet variables.

    Exporting only the mainnet pair for a testnet run signs with the wrong key
    against the testnet host, and Gate.io answers ``INVALID_SIGNATURE`` — a
    missing account that reads like a signing bug.
    """
    mainnet = make_client(lambda request: httpx.Response(200, json=[]))
    testnet = make_client(lambda request: httpx.Response(200, json=[]))
    testnet.base_url = GATEIO_HTTP_TESTNET

    with pytest.raises(GateioError) as on_mainnet:
        await mainnet.get("/spot/accounts", signed=True)
    with pytest.raises(GateioError) as on_testnet:
        await testnet.get("/spot/accounts", signed=True)

    assert "GATE_API_KEY / GATE_API_SECRET" in on_mainnet.value.message
    assert "GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET" in on_testnet.value.message


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


async def test_a_replayed_cancel_that_is_never_answered_is_ambiguous():
    """Replaying makes a duplicate harmless, not the outcome known.

    A ``DELETE`` is replayed, so it used to end in a plain ``NETWORK_ERROR``,
    which reads as "this definitely did not happen". For a cancel that is the
    wrong half of the truth: the venue may have applied it and lost the answer.
    """
    log: list[httpx.Request] = []
    handler = counting_handler([httpx.ReadTimeout("timed out")], log)
    client = make_client(handler, api_key="k", api_secret="s", max_retries=3)

    with pytest.raises(GateioRequestAmbiguousError) as excinfo:
        await client.delete("/spot/orders/1001", params={"currency_pair": "BTC_USDT"})

    assert len(log) == 3
    assert excinfo.value.label == "REQUEST_AMBIGUOUS"
    assert "reconcile" in excinfo.value.message


async def test_a_request_that_never_left_the_process_stays_definitive():
    """No byte was sent on any attempt, so the venue cannot have seen it."""
    log: list[httpx.Request] = []
    handler = counting_handler([httpx.ConnectError("connection refused")], log)
    client = make_client(handler, api_key="k", api_secret="s", max_retries=3)

    with pytest.raises(GateioError) as excinfo:
        await client.delete("/spot/orders/1001", params={"currency_pair": "BTC_USDT"})

    assert len(log) == 3
    assert excinfo.value.label == "NETWORK_ERROR"
    assert not isinstance(excinfo.value, GateioRequestAmbiguousError)


async def test_an_answered_attempt_makes_later_unsent_failures_ambiguous():
    """The venue answered once; a later connect failure cannot unsay that."""
    log: list[httpx.Request] = []
    handler = counting_handler(
        [
            httpx.Response(500, json={"label": "SERVER_ERROR", "message": "internal"}),
            httpx.ConnectError("connection refused"),
        ],
        log,
    )
    client = make_client(handler, api_key="k", api_secret="s", max_retries=3)

    with pytest.raises(GateioRequestAmbiguousError):
        await client.delete("/spot/orders/1001", params={"currency_pair": "BTC_USDT"})

    assert len(log) == 3


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


async def test_a_venue_that_will_not_state_its_time_leaves_the_client_usable():
    """The execution client reads the clock on connect and must survive failing.

    The deadline is a protection, not a precondition: when ``/spot/time`` will
    not answer, the header stays off — which is how every request behaved before
    the clock was read at all — and orders still go out. Nothing here may leave
    the client believing it knows the venue clock.
    """
    orders: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/spot/time"):
            return httpx.Response(500, json={"label": "SERVER_ERROR", "message": "unavailable"})
        orders.append(request.headers)
        return httpx.Response(200, json=ACCEPTED_ORDER)

    client = make_client(handler, api_key="k", api_secret="s")
    with pytest.raises(GateioServerError):
        await client.sync_time()

    assert client.clock_synced is False
    assert client._expiry_ms() is None
    assert client._time_offset_ms == 0, "an unread clock must not shift the signed timestamp"

    await client.post("/spot/orders", body=ORDER_BODY, expiring=True)
    assert EXPIRY_HEADER not in orders[-1]


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


# -- the transport gate: nothing leaves the process once teardown begins ------
#
# Every "nothing was sent" assertion below carries a positive control: the same
# spy transport records a request while the gate is open, so an assertion of
# `len(log) == 1` cannot be satisfied by a spy that was never wired up.

CANCEL_PARAMS: dict[str, Any] = {"contract": "BTC_USDT"}
CANCELLED = {"id": "1001", "status": "cancelled"}


class _ParkingLimiter:
    """A limiter that can hold one acquisition open, as pacing and 429 backoff do.

    This is the window the gate exists for: the request has passed every entry
    check in ``request()`` and owns no connection yet.
    """

    def __init__(self) -> None:
        self.acquired = 0
        self.backoff = 0.0
        self.parked = asyncio.Event()
        self.released = asyncio.Event()
        self.park_next = False

    async def acquire(self) -> None:
        self.acquired += 1
        if self.park_next:
            self.park_next = False
            self.parked.set()
            await self.released.wait()

    def on_rate_limited(self) -> None:
        self.backoff = 0.5

    def on_success(self) -> None:
        self.backoff = 0.0


async def test_a_cancel_parked_in_the_limiter_is_refused_rather_than_sent():
    """T1. The node stops while a cancel waits its turn in the rate limiter.

    On ``main`` this request wakes to a socket pool that has already been
    closed, and ``httpx`` raises ``RuntimeError("Cannot send a request, as the
    client has been closed.")``. That is not an ``httpx.HTTPError``, so the
    attempt loop never sees it, nothing classifies it, and the callers that
    catch only ``GateioError`` let it out raw — a cancel sweep that reports
    success while the order is still live.
    """
    log: list[httpx.Request] = []
    client = make_client(
        counting_handler([httpx.Response(200, json=CANCELLED)], log),
        api_key="k",
        api_secret="s",
    )
    limiter = _ParkingLimiter()
    client._limiter = limiter  # type: ignore[assignment]

    # Positive control: this very spy records a cancel while the gate is open.
    assert await client.delete("/futures/usdt/orders/1000", params=CANCEL_PARAMS) == CANCELLED
    assert len(log) == 1

    limiter.park_next = True
    parked = asyncio.ensure_future(
        client.delete("/futures/usdt/orders/1001", params=CANCEL_PARAMS),
    )
    await asyncio.wait_for(limiter.parked.wait(), timeout=2.0)

    await client.close()  # the node stops while the cancel is still parked
    limiter.released.set()

    with pytest.raises(GateioError) as excinfo:
        await parked

    assert excinfo.value.label == "CLIENT_CLOSED"
    assert not isinstance(excinfo.value, GateioRequestAmbiguousError), (
        "no byte of this request left the process, so the outcome is definitive"
    )
    assert len(log) == 1, "a request reached the wire after the transport was released"


async def test_a_gate_closed_between_attempts_reports_an_unknown_outcome():
    """T2. Attempt 1 reached the venue; the node stops before attempt 2.

    ``CLIENT_CLOSED`` would be a lie here and an expensive one:
    ``is_ambiguous_outcome`` treats a plain ``GateioError`` as definitive, so a
    cancel Gate.io may already have applied would be answered with
    ``OrderCancelRejected``.
    """
    from gateio_nt.execution import is_ambiguous_outcome

    log: list[httpx.Request] = []
    client = make_client(
        counting_handler([httpx.ReadError("connection reset by peer")], log),
        api_key="k",
        api_secret="s",
        max_retries=3,
    )

    async def _retry_delay(attempt: int) -> None:
        await client.close()  # the node stops during the retry backoff

    client._retry_delay = _retry_delay  # type: ignore[assignment]

    with pytest.raises(GateioRequestAmbiguousError) as excinfo:
        await client.delete("/futures/usdt/orders/1001", params=CANCEL_PARAMS)

    assert len(log) == 1
    assert excinfo.value.label == "REQUEST_AMBIGUOUS"
    assert "reconcile" in excinfo.value.message
    assert is_ambiguous_outcome(excinfo.value) is True


async def test_closing_one_owner_gates_the_transport_for_every_other_owner():
    """T3. The gate is global to the shared transport, on purpose and by design.

    The data client, the execution client and the instrument provider share one
    transport, and the background work that has to be stopped — the instrument
    reload — reaches the venue through the shared provider rather than through
    any one client's namespaces. A gate scoped to an owner would not cover it.
    The cost is stated here and in ``docs/architecture.md``: a component that
    stops on its own mutes REST for the others. NautilusTrader's engines
    disconnect all of their clients together, so this is the hand-written
    ``disconnect()`` case, not the node-shutdown case.
    """
    log: list[httpx.Request] = []
    client = make_client(counting_handler([httpx.Response(200, json={"server_time": 1})], log))
    client.acquire()  # data client
    client.acquire()  # execution client

    # Positive control: the spy records traffic while both owners are running.
    assert await client.get("/spot/time") == {"server_time": 1}
    assert len(log) == 1

    await client.close()  # the data client stops first

    assert client.owner_count == 1
    assert client.is_closed is False, "the pool must survive while an owner holds it"
    assert client.is_accepting is False

    with pytest.raises(GateioError) as excinfo:
        await client.get("/spot/time")

    assert excinfo.value.label == "CLIENT_CLOSED"
    assert len(log) == 1


async def test_close_waits_for_the_request_already_on_the_wire():
    """T4. A cancel on the wire gets the venue's real answer, not a guess.

    Without the drain the pool is torn down under it and the caller is handed
    ``REQUEST_AMBIGUOUS`` for an order the venue had in fact cancelled — the
    reconciliation that resolves ambiguity cannot run, because the node is
    stopping.
    """
    journal: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=CANCELLED)

    client = make_client(handler, api_key="k", api_secret="s")

    async def _cancel() -> None:
        payload = await client.delete("/futures/usdt/orders/1001", params=CANCEL_PARAMS)
        journal.append(f"answered:{payload['status']}")

    async def _teardown() -> None:
        await asyncio.sleep(0.02)
        await client.close()
        journal.append("closed")

    started = time.monotonic()
    await asyncio.gather(_cancel(), _teardown())
    elapsed = time.monotonic() - started

    assert journal == ["answered:cancelled", "closed"], (
        f"close() did not wait for the request already on the wire: {journal}"
    )
    assert elapsed >= 0.25


async def test_close_gives_up_on_a_venue_that_never_answers():
    """T5. The drain is bounded, and what breaks out of it is still classified.

    The transport models what a real socket does, which is what the platform's
    own budget assumes: the venue never answers, ``aclose()`` breaks the pending
    read, and ``httpx`` reports ``ReadError`` (measured on httpx 0.28.1 — an
    ``httpx.HTTPError``, and not one of ``_UNSENT_ERRORS``, so it classifies as
    "reached the venue"). What the caller must never see is a raw
    ``RuntimeError``.
    """

    class _NeverAnswering(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.torn_down = asyncio.Event()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            await self.torn_down.wait()
            raise httpx.ReadError("the connection was closed while awaiting the response")

        async def aclose(self) -> None:
            self.torn_down.set()

    transport = _NeverAnswering()
    client = GateioHttpClient(api_key="k", api_secret="s", max_retries=3)
    client._client = httpx.AsyncClient(transport=transport, base_url=GATEIO_HTTP_MAINNET)
    client._limiter = _RecordingLimiter()  # type: ignore[assignment]

    async def _retry_delay(attempt: int) -> None:
        return None

    client._retry_delay = _retry_delay  # type: ignore[assignment]

    cancel = asyncio.ensure_future(
        client.delete("/futures/usdt/orders/1001", params=CANCEL_PARAMS),
    )
    while not transport.requests:
        await asyncio.sleep(0)

    started = time.monotonic()
    await client.close()
    elapsed = time.monotonic() - started

    # What the caller is handed matters more than the timing: a raw
    # `RuntimeError` is the failure this whole change exists to remove.
    with pytest.raises(GateioError) as excinfo:
        await cancel

    assert isinstance(excinfo.value, GateioRequestAmbiguousError)
    assert len(transport.requests) == 1
    # The bound is the platform's own budget for external connections, 2 s. The
    # node's whole disconnect budget is 10 s and the sockets already spend up to
    # 5 s each, so a drain that helped itself to more would be the drop that
    # prints "Timed out waiting for engines to disconnect".
    assert 1.5 <= elapsed <= 3.5, f"the drain was not bounded by the platform budget: {elapsed}"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.Response(400, json={"label": "INVALID_PARAM_VALUE", "message": "bad"}), GateioError),
        (httpx.ReadTimeout("timed out"), GateioRequestAmbiguousError),
    ],
    ids=["venue-refusal", "answer-never-arrived"],
)
async def test_a_failed_request_leaves_no_drain_owing(failure: Any, expected: type):
    """T6. The in-flight count is released on every exit, not just the happy one.

    A counter left standing after a failure is a silent defect: nothing is
    wrong until the node stops, and then every shutdown pays the full drain
    timeout for a transport that has nothing on the wire.
    """
    log: list[httpx.Request] = []
    client = make_client(counting_handler([failure], log), api_key="k", api_secret="s")

    with pytest.raises(expected):
        await client.post("/spot/orders", body=ORDER_BODY)

    started = time.monotonic()
    await client.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"close() waited for a request that had already failed: {elapsed}"
    assert len(log) == 1


async def test_the_gate_is_read_with_no_await_left_before_the_send():
    """T7. The check is the last statement before the send, and must stay there.

    The guarantee is not the check itself but its position: on a single-threaded
    loop, "gate open" and "request sent" are one step only while nothing between
    them can yield. ``_build_headers`` is the nearest neighbour, so it is where
    the stop is driven from. Moving the check one line up makes this fail.
    """
    log: list[httpx.Request] = []
    client = make_client(
        counting_handler([httpx.Response(200, json=CANCELLED)], log),
        api_key="k",
        api_secret="s",
    )

    # Positive control: this spy records a cancel while the gate is open.
    assert await client.delete("/futures/usdt/orders/1000", params=CANCEL_PARAMS) == CANCELLED
    assert len(log) == 1

    build_headers = client._build_headers

    def _stop_while_building(*args: Any, **kwargs: Any) -> Any:
        client.stop_accepting()
        return build_headers(*args, **kwargs)

    client._build_headers = _stop_while_building  # type: ignore[assignment]

    with pytest.raises(GateioError) as excinfo:
        await client.delete("/futures/usdt/orders/1001", params=CANCEL_PARAMS)

    assert excinfo.value.label == "CLIENT_CLOSED"
    assert len(log) == 1, "the request was sent although the gate shut while headers were built"


async def test_the_drain_waits_for_every_request_on_the_wire_not_just_the_first():
    """A stopping node has several requests out at once; that is the normal case.

    The drain is a counter, not a flag, and the difference only shows with more
    than one request in flight: a node winding down sweeps open orders per
    product. If the wait ended at the first answer, the pool would be torn down
    under the rest — which is the very thing the drain exists to prevent, so the
    counted region has to be pinned rather than assumed.
    """
    release_first: asyncio.Event = asyncio.Event()
    release_rest: asyncio.Event = asyncio.Event()
    client: GateioHttpClient

    #: Whether the pool was already closed when each answer came back. Anything
    #: but ``False`` everywhere means the drain let go too early.
    pool_closed_at_answer: list[bool] = []

    class _AnswersInTwoWaves(httpx.AsyncBaseTransport):
        seen = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            mine = _AnswersInTwoWaves.seen
            _AnswersInTwoWaves.seen += 1
            await (release_first if mine == 0 else release_rest).wait()
            pool_closed_at_answer.append(client._client.is_closed)
            return httpx.Response(200, json={})

    client = GateioHttpClient().acquire()
    client._client = httpx.AsyncClient(base_url=GATEIO_HTTP_MAINNET, transport=_AnswersInTwoWaves())

    in_flight = [
        asyncio.create_task(client.request("GET", f"/spot/tickers?i={i}")) for i in range(3)
    ]
    for _ in range(100):
        if client._inflight == 3:
            break
        await asyncio.sleep(0.01)
    assert client._inflight == 3, (
        f"the probe never got three requests on the wire: {client._inflight}"
    )

    teardown = asyncio.create_task(client.close())
    await asyncio.sleep(0.01)
    release_first.set()  # One answer lands; two are still out.
    await asyncio.sleep(0.05)
    release_rest.set()
    await asyncio.wait_for(teardown, timeout=3.0)
    await asyncio.gather(*in_flight, return_exceptions=True)

    assert len(pool_closed_at_answer) == 3, "the probe lost an answer"
    assert not any(pool_closed_at_answer), (
        "the drain released the socket pool while requests were still on the wire: "
        f"pool-closed-at-answer = {pool_closed_at_answer}"
    )


async def test_a_cancelled_teardown_still_closes_the_socket_pool():
    """The drain must not become a window in which the pool is lost forever.

    ``close()`` latches ``_closed`` and only then waits for the requests already
    on the wire, and ``_closed`` is what makes every later ``close()`` a no-op.
    A cancellation landing inside that wait would therefore leave a pool that
    reports itself closed and that nothing in the process can ever close again —
    an open connection to the venue, held until the process dies.

    The window is on the platform's own path, not an exotic one:
    ``LiveExecutionClient.disconnect()`` schedules the teardown with a bare
    ``loop.create_task``, nothing bounds it, and ``TradingNode.dispose()``
    cancels whatever the disconnect budget did not finish.
    """

    class _NeverAnswers(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    client = GateioHttpClient().acquire()
    client._client = httpx.AsyncClient(base_url=GATEIO_HTTP_MAINNET, transport=_NeverAnswers())

    in_flight = asyncio.create_task(client.request("GET", "/spot/tickers"))
    await asyncio.sleep(0)  # let it reach the wire and register as undrained

    teardown = asyncio.create_task(client.close())
    await asyncio.sleep(0)  # let it reach the drain
    teardown.cancel()
    await asyncio.gather(teardown, return_exceptions=True)
    await asyncio.sleep(0)  # let the shielded close finish

    assert client._client.is_closed, (
        "the teardown reported the transport closed but never closed the socket pool, "
        "and the idempotence guard means no later call can"
    )

    in_flight.cancel()
    await asyncio.gather(in_flight, return_exceptions=True)


async def test_stop_accepting_is_idempotent_and_sticky():
    """The verb behaves as NautilusTrader's own ``cancel_all_requests()`` does."""
    client = make_client(lambda r: httpx.Response(200, json={}))

    assert client.is_accepting is True
    client.stop_accepting()
    client.stop_accepting()
    assert client.is_accepting is False
    assert client.is_closed is False, "the gate is not the pool"

    await client.close()
    assert client.is_accepting is False


async def test_the_factory_replaces_a_gated_transport_that_still_has_owners():
    """T12. "Spent" is two states now, and the factory has to know both.

    A client that stops while another still holds a reference leaves the cached
    transport gated with ``owner_count`` above zero and nothing closed. A
    factory that checks only ``is_closed`` hands the next node in the process a
    live-looking transport that refuses every request.
    """
    from gateio_nt import factories

    first = factories.get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)
    first.acquire()  # data client
    first.acquire()  # execution client
    await first.close()  # the data client stops first

    assert first.owner_count == 1
    assert first.is_closed is False
    assert first.is_accepting is False

    second = factories.get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)

    assert second is not first
    assert second.is_accepting is True
    assert second.is_closed is False

    log: list[httpx.Request] = []
    second._client = httpx.AsyncClient(
        transport=httpx.MockTransport(counting_handler([httpx.Response(200, json={"a": 1})], log)),
        base_url=GATEIO_HTTP_MAINNET,
    )
    assert await second.get("/spot/time") == {"a": 1}
    assert len(log) == 1

    await first._client.aclose()
    await second.close()
