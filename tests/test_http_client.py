"""Unit tests for :class:`nautilus_gateio.http.GateioHttpClient`.

All HTTP traffic is served by ``httpx.MockTransport`` — no network access and
no credentials are involved. Placeholder key/secret strings are used only to
exercise the signing code path.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nautilus_gateio.constants import GATEIO_API_PREFIX, GATEIO_HTTP_MAINNET
from nautilus_gateio.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
    LiveOrdersDisabledError,
    OrderValidationError,
)
from nautilus_gateio.http import GateioHttpClient
from nautilus_gateio.signing import sign_request

API = GATEIO_API_PREFIX


class _NoWaitLimiter:
    """Rate limiter stub so tests never sleep."""

    def wait(self) -> None:
        pass

    def on_429(self) -> None:
        pass

    def on_ok(self) -> None:
        pass


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    api_key: str = "",
    api_secret: str = "",
    live_orders: bool = False,
) -> GateioHttpClient:
    """Build a client whose transport is a MockTransport around ``handler``."""
    client = GateioHttpClient(api_key=api_key, api_secret=api_secret, live_orders=live_orders)
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=GATEIO_HTTP_MAINNET
    )
    client._limiter = _NoWaitLimiter()
    return client


def raw_order(
    order_id: str = "1001",
    text: str = "t-test-1",
    pair: str = "BTC_USDT",
    side: str = "buy",
    status: str = "open",
    amount: str = "1",
    filled: str = "0",
    price: str = "100",
) -> dict[str, Any]:
    """A synthetic Gate.io spot order payload."""
    return {
        "id": order_id,
        "text": text,
        "currency_pair": pair,
        "side": side,
        "type": "limit",
        "price": price,
        "amount": amount,
        "filled_total": filled,
        "left": amount,
        "status": status,
        "create_time_ms": "1700000000000",
        "time_in_force": "gtc",
    }


BALANCES_JSON = [
    {"currency": "USDT", "available": "100.5", "locked": "2"},
    {"currency": "BTC", "available": "0.25", "locked": "0"},
]


# -- public market data -----------------------------------------------------


def test_candles_parsed_and_sorted_oldest_first():
    # Rows deliberately out of order; format: [ts, quote_vol, close, high, low, open, base_vol]
    rows = [
        ["1700000120", "1", "101", "102", "99", "100", "5"],
        ["1700000000", "1", "100", "101", "98", "99", "4"],
        ["1700000060", "1", "100.5", "101.5", "98.5", "99.5", "4.5"],
    ]
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=rows)

    client = make_client(handler)
    candles = client.candles("btc_usdt", interval="1m", limit=3)

    assert seen["path"] == f"{API}/spot/candlesticks"
    assert seen["params"]["currency_pair"] == "BTC_USDT"
    assert seen["params"]["interval"] == "1m"
    assert [c["ts"] for c in candles] == [1700000000000, 1700000060000, 1700000120000]
    first = candles[0]
    assert first["open"] == 99.0
    assert first["high"] == 101.0
    assert first["low"] == 98.0
    assert first["close"] == 100.0
    assert first["volume"] == 4.0


def test_currency_pair_endpoint_path_and_parsing():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "id": "BTC_USDT",
                "base": "BTC",
                "quote": "USDT",
                "amount_precision": 6,
                "precision": 2,
                "min_base_amount": "0.00001",
                "min_quote_amount": "3",
                "fee": "0.2",
                "trade_status": "tradable",
            },
        )

    client = make_client(handler)
    spec = client.currency_pair("btc_usdt")

    assert seen["path"] == f"{API}/spot/currency_pairs/BTC_USDT"
    assert spec["pair"] == "BTC_USDT"
    assert spec["price_precision"] == 2
    assert spec["amount_precision"] == 6
    assert spec["min_quote_amount"] == 3.0


# -- signing ----------------------------------------------------------------


def test_private_request_carries_valid_signature_headers():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        return httpx.Response(200, json=BALANCES_JSON)

    client = make_client(handler, api_key="test-key", api_secret="test-secret")
    client._signed_timestamp = lambda: "1700000000"  # freeze for determinism
    client.balances()

    expected = sign_request(
        "GET", f"{API}/spot/accounts", "", "", "test-key", "test-secret", "1700000000"
    )
    headers = seen["headers"]
    assert headers["KEY"] == "test-key"
    assert headers["Timestamp"] == "1700000000"
    assert headers["SIGN"] == expected["SIGN"]


# -- retry and error translation --------------------------------------------


def test_429_then_200_is_retried_and_succeeds():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, json={"label": "TOO_MANY_REQUESTS", "message": "slow down"})
        return httpx.Response(200, json=BALANCES_JSON)

    client = make_client(handler, api_key="k", api_secret="s")
    balances = client.balances()

    assert len(calls) == 2
    assert balances["USDT"]["available"] == 100.5


def test_persistent_429_raises_after_retries():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, json={"label": "TOO_MANY_REQUESTS", "message": "slow down"})

    client = make_client(handler, api_key="k", api_secret="s")
    with pytest.raises(GateioError) as excinfo:
        client.balances()

    assert excinfo.value.status == 429
    assert excinfo.value.label == "TOO_MANY_REQUESTS"
    assert len(calls) == 3  # default retries


def test_400_with_label_raises_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"label": "INVALID_CURRENCY", "message": "unknown pair"})

    client = make_client(handler, api_key="k", api_secret="s")
    with pytest.raises(GateioClientError) as excinfo:
        client.balances()

    assert excinfo.value.status == 400
    assert excinfo.value.label == "INVALID_CURRENCY"
    assert "unknown pair" in str(excinfo.value)


def test_500_raises_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"label": "SERVER_ERROR", "message": "internal"})

    client = make_client(handler, api_key="k", api_secret="s")
    with pytest.raises(GateioServerError) as excinfo:
        client.balances()

    assert excinfo.value.status == 500


# -- account state ----------------------------------------------------------


def test_balances_parsing():
    client = make_client(lambda r: httpx.Response(200, json=BALANCES_JSON), "k", "s")
    balances = client.balances()

    assert balances == {
        "USDT": {"available": 100.5, "locked": 2.0},
        "BTC": {"available": 0.25, "locked": 0.0},
    }


def test_open_orders_flattens_grouped_response():
    grouped = [
        {
            "currency_pair": "BTC_USDT",
            "orders": [raw_order("1", "t-a"), raw_order("2", "t-b")],
        },
        {"currency_pair": "ETH_USDT", "orders": [raw_order("3", "t-c", pair="ETH_USDT")]},
    ]
    client = make_client(lambda r: httpx.Response(200, json=grouped), "k", "s")
    orders = client.open_orders()

    assert [o["id"] for o in orders] == ["1", "2", "3"]
    assert orders[0]["client_id"] == "t-a"
    assert orders[2]["pair"] == "ETH_USDT"


def test_open_orders_plain_list_flattened():
    # A plain (ungrouped) list of order objects is parsed the same way as
    # the grouped [{currency_pair, orders: [...]}] response shape.
    plain = [raw_order("7", "t-plain")]
    client = make_client(lambda r: httpx.Response(200, json=plain), "k", "s")
    orders = client.open_orders("BTC_USDT")

    assert len(orders) == 1
    assert orders[0]["id"] == "7"
    assert orders[0]["client_id"] == "t-plain"


def test_find_by_client_id():
    grouped = [
        {
            "currency_pair": "BTC_USDT",
            "orders": [raw_order("1", "t-a"), raw_order("2", "t-target")],
        }
    ]
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = str(request.url.query, "ascii")
        return httpx.Response(200, json=grouped)

    client = make_client(handler, "k", "s")

    found = client.find_by_client_id("BTC_USDT", "t-target")
    assert found is not None
    assert found["id"] == "2"
    assert "currency_pair=BTC_USDT" in seen["query"]

    assert client.find_by_client_id("BTC_USDT", "t-missing") is None


# -- live-orders safety switch ----------------------------------------------


def test_mutating_calls_raise_when_live_orders_disabled():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    client = make_client(handler, api_key="k", api_secret="s", live_orders=False)

    with pytest.raises(LiveOrdersDisabledError):
        client.place_order("BTC_USDT", "buy", 0.01, price=100.0)
    with pytest.raises(LiveOrdersDisabledError):
        client.cancel_order("1", "BTC_USDT")
    with pytest.raises(LiveOrdersDisabledError):
        client.cancel_all("BTC_USDT")
    assert calls == []  # nothing ever reached the transport


def test_emergency_stop_is_safe_to_call_while_disabled():
    # emergency_stop is deliberately callable in any state: with live orders
    # already disabled it performs no HTTP calls and reports the disabled state.
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=[])

    client = make_client(handler, api_key="k", api_secret="s", live_orders=False)
    result = client.emergency_stop()

    assert result == {"cancelled": [], "live_orders_disabled": True}
    assert client.live_orders is False
    assert calls == []


def test_place_order_without_credentials_raises_value_error():
    client = make_client(lambda r: httpx.Response(200, json={}), live_orders=True)
    with pytest.raises(ValueError):
        client.place_order("BTC_USDT", "buy", 0.01, price=100.0)


# -- validated order placement ----------------------------------------------


def test_place_order_validated_rejects_before_any_http_call(btc_spec):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    client = make_client(handler, api_key="k", api_secret="s", live_orders=True)

    # Notional 0.001 is far below min_quote_amount=3.0 in the spec.
    with pytest.raises(OrderValidationError):
        client.place_order_validated("BTC_USDT", "buy", amount=0.00001, price=100.0, spec=btc_spec)

    assert calls == []  # validation failed before any request was made


def test_place_order_validated_idempotent_on_existing_client_id(btc_spec):
    grouped = [{"currency_pair": "BTC_USDT", "orders": [raw_order("55", "t-dup")]}]
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(200, json=raw_order("99", "t-dup"))
        return httpx.Response(200, json=grouped)

    client = make_client(handler, api_key="k", api_secret="s", live_orders=True)
    result = client.place_order_validated(
        "BTC_USDT", "buy", amount=0.05, price=100.0, client_id="t-dup", spec=btc_spec
    )

    assert result["idempotent"] is True
    assert result["id"] == "55"
    assert posts == []  # no duplicate submission


# -- emergency stop ---------------------------------------------------------


def test_emergency_stop_cancels_then_hard_disables():
    grouped = [{"currency_pair": "BTC_USDT", "orders": [raw_order("42", "t-open")]}]
    deletes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deletes.append(request.url.path)
            return httpx.Response(200, json=raw_order("42", "t-open", status="cancelled"))
        return httpx.Response(200, json=grouped)

    client = make_client(handler, api_key="k", api_secret="s", live_orders=True)
    result = client.emergency_stop("BTC_USDT")

    assert result["live_orders_disabled"] is True
    assert len(result["cancelled"]) == 1
    assert result["cancelled"][0]["id"] == "42"
    assert deletes == [f"{API}/spot/orders/42"]
    assert client.live_orders is False
    with pytest.raises(LiveOrdersDisabledError):
        client.place_order("BTC_USDT", "buy", 0.01, price=100.0)


# -- request body defaults --------------------------------------------------


def test_market_order_defaults_to_ioc():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=raw_order())

    client = make_client(handler, api_key="k", api_secret="s", live_orders=True)
    client.place_order("BTC_USDT", "buy", "10", order_type="market")

    body = bodies[0]
    assert body["type"] == "market"
    assert body["time_in_force"] == "ioc"
    assert "price" not in body
    assert body["amount"] == "10"
    assert body["text"].startswith("t-")


def test_limit_order_defaults_to_gtc():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=raw_order())

    client = make_client(handler, api_key="k", api_secret="s", live_orders=True)
    client.place_order("BTC_USDT", "buy", 0.01, price=100.0)

    body = bodies[0]
    assert body["type"] == "limit"
    assert body["time_in_force"] == "gtc"
    assert body["price"] == "100.0"


# -- time synchronization ---------------------------------------------------


def test_sync_time_offset_applied_to_signed_timestamp():
    offset_ms = 5_000_000  # pretend the exchange clock is 5000 s ahead

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"server_time": int(time.time() * 1000) + offset_ms})

    client = make_client(handler)
    measured = client.sync_time()

    assert abs(measured - offset_ms) < 2_000  # allow for handler latency
    signed = int(client._signed_timestamp())
    assert abs(signed - (time.time() + offset_ms / 1000)) < 5
