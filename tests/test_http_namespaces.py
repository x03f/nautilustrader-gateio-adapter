"""Unit tests for the typed Gate.io REST namespaces.

Every namespace is exercised through ``httpx.MockTransport``: no network access,
no credentials. The tests assert the wire contract each namespace promises —
path, query parameters, HTTP method and signing — plus the scope guards that
stop an unscoped call from cancelling or moving more than the caller asked for.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from collections.abc import Callable
from types import ModuleType
from typing import Any

import httpx
import pytest

from gateio_nt.common.constants import GATEIO_API_PREFIX, GATEIO_HTTP_MAINNET
from gateio_nt.common.errors import GateioClientError, GateioServerError
from gateio_nt.http import futures as futures_module
from gateio_nt.http import options as options_module
from gateio_nt.http import spot as spot_module
from gateio_nt.http.client import (
    EXPIRY_HEADER,
    GateioHttpClient,
    GateioRequestAmbiguousError,
)
from gateio_nt.http.futures import GateioFuturesHttpAPI
from gateio_nt.http.margin import GateioMarginHttpAPI
from gateio_nt.http.options import GateioOptionsHttpAPI
from gateio_nt.http.spot import GateioSpotHttpAPI
from gateio_nt.http.wallet import ALLOWED_TRANSFER_ACCOUNTS, GateioWalletHttpAPI

API = GATEIO_API_PREFIX

Handler = Callable[[httpx.Request], httpx.Response]


class _NoWaitLimiter:
    """Rate limiter stub so the tests never sleep."""

    backoff = 0.0

    async def acquire(self) -> None:
        pass

    def on_rate_limited(self) -> None:
        pass

    def on_success(self) -> None:
        pass


class Recorder:
    """Captures every request the namespaces make and replays canned responses."""

    def __init__(self, response: Any = None, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response if response is not None else {}
        self._status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, json=self._response)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no request was made"
        return self.requests[-1]

    @property
    def path(self) -> str:
        return self.last.url.path

    @property
    def params(self) -> dict[str, str]:
        return dict(self.last.url.params)

    @property
    def body(self) -> Any:
        return json.loads(self.last.content) if self.last.content else None


def make_client(handler: Handler, signed: bool = True, max_retries: int = 3) -> GateioHttpClient:
    client = GateioHttpClient(
        api_key="test-key" if signed else "",
        api_secret="test-secret" if signed else "",
        max_retries=max_retries,
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=GATEIO_HTTP_MAINNET,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    client._limiter = _NoWaitLimiter()  # type: ignore[assignment]

    async def _retry_delay(attempt: int) -> None:
        pass

    client._retry_delay = _retry_delay  # type: ignore[assignment]
    return client


def failing_handler(status: int, label: str) -> tuple[Handler, list[httpx.Request]]:
    """A handler that always fails, plus the log of requests it received."""
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        return httpx.Response(status, json={"label": label, "message": "failure"})

    return handler, log


# -- spot --------------------------------------------------------------------


async def test_spot_create_order_posts_the_body_verbatim():
    rec = Recorder({"id": "1001", "status": "open"})
    spot = GateioSpotHttpAPI(make_client(rec))

    body = {"currency_pair": "BTC_USDT", "side": "buy", "amount": "1", "text": "t-ng-1"}
    await spot.create_order(body)

    assert rec.last.method == "POST"
    assert rec.path == f"{API}/spot/orders"
    assert rec.body == body
    assert rec.last.headers["KEY"] == "test-key"


async def test_spot_cancel_all_requires_a_pair():
    """Omitting the pair would cancel every order in the account."""
    rec = Recorder([])
    spot = GateioSpotHttpAPI(make_client(rec))

    with pytest.raises(TypeError):
        await spot.cancel_all()  # type: ignore[call-arg]

    assert rec.requests == []


async def test_spot_cancel_all_scopes_the_delete():
    rec = Recorder([])
    spot = GateioSpotHttpAPI(make_client(rec))

    await spot.cancel_all("BTC_USDT", side="sell")

    assert rec.last.method == "DELETE"
    assert rec.path == f"{API}/spot/orders"
    assert rec.params == {"currency_pair": "BTC_USDT", "side": "sell"}


async def test_spot_public_endpoint_is_unsigned():
    rec = Recorder([])
    spot = GateioSpotHttpAPI(make_client(rec))

    await spot.currency_pairs()

    assert "KEY" not in rec.last.headers
    assert "SIGN" not in rec.last.headers


async def test_spot_fee_is_signed_despite_the_public_path():
    rec = Recorder({"user_id": 1, "maker_fee": "0.002"})
    spot = GateioSpotHttpAPI(make_client(rec))

    await spot.fee("BTC_USDT")

    assert rec.path == f"{API}/spot/fee"
    assert rec.last.headers["KEY"] == "test-key"


async def test_spot_candlesticks_maps_frm_to_the_from_parameter():
    rec = Recorder([])
    spot = GateioSpotHttpAPI(make_client(rec))

    await spot.candlesticks("BTC_USDT", interval="5m", frm=100, to=200, limit=50)

    assert rec.params == {
        "currency_pair": "BTC_USDT",
        "interval": "5m",
        "limit": "50",
        "from": "100",
        "to": "200",
    }


async def test_spot_order_submission_is_not_retried_on_5xx():
    """Regression: a retried order submission can execute twice."""
    handler, log = failing_handler(500, "SERVER_ERROR")
    spot = GateioSpotHttpAPI(make_client(handler))

    with pytest.raises(GateioServerError) as excinfo:
        await spot.create_order({"currency_pair": "BTC_USDT", "side": "buy", "amount": "1"})

    assert len(log) == 1
    assert isinstance(excinfo.value, GateioRequestAmbiguousError)


async def test_spot_amend_order_is_not_retried_on_5xx():
    handler, log = failing_handler(500, "SERVER_ERROR")
    spot = GateioSpotHttpAPI(make_client(handler))

    with pytest.raises(GateioRequestAmbiguousError):
        await spot.amend_order("1001", "BTC_USDT", {"amount": "2"})

    assert len(log) == 1


async def test_spot_cancel_is_retried_on_5xx():
    """Cancelling twice is harmless, so the transport may replay it."""
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(500, json={"label": "SERVER_ERROR", "message": "internal"})
        return httpx.Response(200, json={"id": "1001", "status": "cancelled"})

    spot = GateioSpotHttpAPI(make_client(handler))
    result = await spot.cancel_order("1001", "BTC_USDT")

    assert result["status"] == "cancelled"
    assert len(log) == 2


async def test_spot_order_carries_the_submission_deadline_once_synced():
    rec = Recorder({"id": "1001"})
    client = make_client(rec)
    client._clock_synced = True
    spot = GateioSpotHttpAPI(client)

    await spot.create_order({"currency_pair": "BTC_USDT", "side": "buy", "amount": "1"})

    assert EXPIRY_HEADER in rec.last.headers


# -- futures -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("settle", "delivery", "base"),
    [
        ("usdt", False, "/futures/usdt"),
        ("btc", False, "/futures/btc"),
        ("usdt", True, "/delivery/usdt"),
    ],
)
async def test_futures_base_path_selects_product_and_settlement(
    settle: str, delivery: bool, base: str
):
    rec = Recorder([])
    api = GateioFuturesHttpAPI(make_client(rec), settle=settle, delivery=delivery)

    assert api.base_path == base
    await api.contracts()
    assert rec.path == f"{API}{base}/contracts"


async def test_futures_cancel_all_scopes_the_delete_to_one_contract():
    rec = Recorder([])
    api = GateioFuturesHttpAPI(make_client(rec), settle="usdt")

    await api.cancel_all("BTC_USDT", side="ask")

    assert rec.last.method == "DELETE"
    assert rec.path == f"{API}/futures/usdt/orders"
    assert rec.params == {"contract": "BTC_USDT", "side": "ask"}


async def test_futures_cancel_all_requires_a_contract():
    rec = Recorder([])
    api = GateioFuturesHttpAPI(make_client(rec), settle="usdt")

    with pytest.raises(TypeError):
        await api.cancel_all()  # type: ignore[call-arg]

    assert rec.requests == []


async def test_futures_order_submission_is_not_retried_on_5xx():
    handler, log = failing_handler(502, "SERVER_ERROR")
    api = GateioFuturesHttpAPI(make_client(handler), settle="usdt")

    with pytest.raises(GateioRequestAmbiguousError):
        await api.create_order({"contract": "BTC_USDT", "size": 500, "price": "0", "tif": "ioc"})

    assert len(log) == 1


async def test_futures_amend_is_not_retried_on_5xx():
    handler, log = failing_handler(500, "SERVER_ERROR")
    api = GateioFuturesHttpAPI(make_client(handler), settle="usdt")

    with pytest.raises(GateioRequestAmbiguousError):
        await api.amend_order("1001", {"size": 100})

    assert len(log) == 1


# -- options -----------------------------------------------------------------


async def test_options_cancel_all_requires_a_scope():
    """Regression: an unscoped call cancels every resting option in the account."""
    rec = Recorder([])
    api = GateioOptionsHttpAPI(make_client(rec))

    with pytest.raises(ValueError) as excinfo:
        await api.cancel_all()

    assert "contract" in str(excinfo.value)
    assert "underlying" in str(excinfo.value)
    assert rec.requests == [], "an unscoped cancel reached the venue"


async def test_options_cancel_all_rejects_a_side_only_scope():
    rec = Recorder([])
    api = GateioOptionsHttpAPI(make_client(rec))

    with pytest.raises(ValueError):
        await api.cancel_all(side="bid")

    assert rec.requests == []


@pytest.mark.parametrize("blank", ["", None])
async def test_options_cancel_all_rejects_blank_scope_values(blank: str | None):
    rec = Recorder([])
    api = GateioOptionsHttpAPI(make_client(rec))

    with pytest.raises(ValueError):
        await api.cancel_all(contract=blank, underlying=blank)

    assert rec.requests == []


async def test_options_cancel_all_accepts_a_contract_scope():
    rec = Recorder([])
    api = GateioOptionsHttpAPI(make_client(rec))

    await api.cancel_all(contract="BTC_USDT-20260101-70000-C")

    assert rec.last.method == "DELETE"
    assert rec.path == f"{API}/options/orders"
    assert rec.params == {"contract": "BTC_USDT-20260101-70000-C"}


async def test_options_cancel_all_accepts_an_underlying_scope():
    rec = Recorder([])
    api = GateioOptionsHttpAPI(make_client(rec))

    await api.cancel_all(underlying="BTC_USDT")

    assert rec.params == {"underlying": "BTC_USDT"}


async def test_options_order_submission_is_not_retried_on_5xx():
    handler, log = failing_handler(500, "SERVER_ERROR")
    api = GateioOptionsHttpAPI(make_client(handler))

    with pytest.raises(GateioRequestAmbiguousError):
        await api.create_order({"contract": "BTC_USDT-20260101-70000-C", "size": 1})

    assert len(log) == 1


# -- wallet ------------------------------------------------------------------


async def test_wallet_transfer_builds_the_internal_transfer_body():
    rec = Recorder({"tx_id": 42})
    wallet = GateioWalletHttpAPI(make_client(rec))

    await wallet.transfer("USDT", "spot", "futures", "10", settle="usdt")

    assert rec.last.method == "POST"
    assert rec.path == f"{API}/wallet/transfers"
    assert rec.body == {
        "currency": "USDT",
        "from": "spot",
        "to": "futures",
        "amount": "10",
        "settle": "usdt",
    }


@pytest.mark.parametrize("account", ["withdrawal", "sub_account", "external", ""])
async def test_wallet_transfer_rejects_non_internal_wallets(account: str):
    rec = Recorder({})
    wallet = GateioWalletHttpAPI(make_client(rec))

    with pytest.raises(ValueError):
        await wallet.transfer("USDT", "spot", account, "10")
    with pytest.raises(ValueError):
        await wallet.transfer("USDT", account, "spot", "10")

    assert rec.requests == []
    assert account not in ALLOWED_TRANSFER_ACCOUNTS


async def test_wallet_transfer_requires_settle_for_contract_wallets():
    rec = Recorder({})
    wallet = GateioWalletHttpAPI(make_client(rec))

    with pytest.raises(ValueError) as excinfo:
        await wallet.transfer("USDT", "spot", "futures", "10")

    assert "settle" in str(excinfo.value)
    assert rec.requests == []


async def test_wallet_transfer_is_not_retried_on_5xx():
    """Regression: a retried transfer can move the funds twice."""
    handler, log = failing_handler(500, "SERVER_ERROR")
    wallet = GateioWalletHttpAPI(make_client(handler))

    with pytest.raises(GateioRequestAmbiguousError):
        await wallet.transfer("USDT", "spot", "futures", "10", settle="usdt")

    assert len(log) == 1


async def test_wallet_transfer_is_not_retried_on_timeout():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        raise httpx.ReadTimeout("timed out")

    wallet = GateioWalletHttpAPI(make_client(handler))

    with pytest.raises(GateioRequestAmbiguousError) as excinfo:
        await wallet.transfer("USDT", "spot", "futures", "10", settle="usdt")

    assert len(log) == 1
    assert "reconcile" in excinfo.value.message


async def test_wallet_status_read_is_retried_on_5xx():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(500, json={"label": "SERVER_ERROR", "message": "internal"})
        return httpx.Response(200, json={"tx_id": "42", "status": "SUCCESS"})

    wallet = GateioWalletHttpAPI(make_client(handler))
    result = await wallet.transfer_status("42")

    assert result["status"] == "SUCCESS"
    assert len(log) == 2


# -- margin ------------------------------------------------------------------


async def test_margin_borrow_is_not_retried_on_5xx():
    """Regression: a retried borrow can draw the loan twice."""
    handler, log = failing_handler(500, "SERVER_ERROR")
    margin = GateioMarginHttpAPI(make_client(handler))

    with pytest.raises(GateioRequestAmbiguousError):
        await margin.borrow_or_repay(
            {"currency": "USDT", "currency_pair": "BTC_USDT", "type": "margin", "amount": "100"}
        )

    assert len(log) == 1


async def test_margin_borrow_is_not_retried_on_timeout():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        raise httpx.ReadTimeout("timed out")

    margin = GateioMarginHttpAPI(make_client(handler))

    with pytest.raises(GateioRequestAmbiguousError):
        await margin.borrow_or_repay({"currency": "USDT", "amount": "100"})

    assert len(log) == 1


async def test_margin_accounts_read_is_retried_on_5xx():
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if len(log) == 1:
            return httpx.Response(503, json={"label": "SERVER_ERROR", "message": "busy"})
        return httpx.Response(200, json=[{"currency_pair": "BTC_USDT"}])

    margin = GateioMarginHttpAPI(make_client(handler))
    result = await margin.accounts()

    assert result == [{"currency_pair": "BTC_USDT"}]
    assert len(log) == 2


# -- error surfacing through the namespaces ---------------------------------


async def test_namespace_client_error_keeps_the_gateio_label():
    handler, _ = failing_handler(400, "INVALID_CURRENCY_PAIR")
    spot = GateioSpotHttpAPI(make_client(handler))

    with pytest.raises(GateioClientError) as excinfo:
        await spot.create_order({"currency_pair": "NOPE", "side": "buy", "amount": "1"})

    assert excinfo.value.label == "INVALID_CURRENCY_PAIR"
    assert not isinstance(excinfo.value, GateioRequestAmbiguousError)


# -- the submission deadline, on every mutating path -------------------------
#
# `tests/test_http_client.py` proves the transport emits `x-gate-exptime` when
# it is asked to, and `tests/test_docs.py` proves the execution client measures
# the clock offset that unlocks it. Neither says anything about who *asks*: both
# hand `expiring=True` to the transport themselves. The tests below are the
# missing half — they drive the namespace methods the execution client actually
# calls and read the header off the wire, so removing `expiring=True` from any
# one of them fails here even though the transport still works perfectly.


@dataclasses.dataclass(frozen=True)
class _MutatingCall:
    """One state-changing REST call, with the deadline Gate.io declares for it.

    ``deadline`` is not this package's preference. It is whether Gate.io's own
    API specification lists the ``x-gate-exptime`` parameter on that operation,
    read from the generated ``gate-api`` client (7.2.100, "Version of the API:
    v4.106.100"), which Gate publishes from the same source as the reference
    documentation. The venue declares it on the plain spot and futures order
    endpoints — submit, batch submit, cancel, cancel-all, amend, and the spot
    batch cancel — and on nothing else this package calls.

    The one entry that is behaviour rather than venue documentation is the
    delivery product line: Gate.io declares the header under ``/futures`` and
    not under ``/delivery``, while :class:`GateioFuturesHttpAPI` serves both
    from one set of methods and therefore sends it on both. That is recorded
    here as it is, and said as such in ``docs/configuration.md``.
    """

    namespace: str
    module: str
    method: str
    deadline: bool
    invoke: Callable[[Any], Any]


def _spot(client: GateioHttpClient) -> GateioSpotHttpAPI:
    return GateioSpotHttpAPI(client)


def _futures(client: GateioHttpClient) -> GateioFuturesHttpAPI:
    return GateioFuturesHttpAPI(client, settle="usdt")


def _delivery(client: GateioHttpClient) -> GateioFuturesHttpAPI:
    return GateioFuturesHttpAPI(client, settle="usdt", delivery=True)


def _options(client: GateioHttpClient) -> GateioOptionsHttpAPI:
    return GateioOptionsHttpAPI(client)


NAMESPACE_FACTORIES: dict[str, Callable[[GateioHttpClient], Any]] = {
    "spot": _spot,
    "futures": _futures,
    "delivery": _delivery,
    "options": _options,
}

ORDER = {"currency_pair": "BTC_USDT", "side": "buy", "amount": "1", "text": "t-ng-1"}
FUTURES_ORDER = {"contract": "BTC_USDT", "size": 500, "price": "60000", "text": "t-ng-1"}

MUTATING_CALLS: tuple[_MutatingCall, ...] = (
    # -- spot: the venue declares the deadline on all six order endpoints
    _MutatingCall("spot", "spot", "create_order", True, lambda ns: ns.create_order(ORDER)),
    _MutatingCall(
        "spot", "spot", "create_batch_orders", True, lambda ns: ns.create_batch_orders([ORDER])
    ),
    _MutatingCall(
        "spot", "spot", "cancel_order", True, lambda ns: ns.cancel_order("1001", "BTC_USDT")
    ),
    _MutatingCall("spot", "spot", "cancel_all", True, lambda ns: ns.cancel_all("BTC_USDT")),
    _MutatingCall(
        "spot",
        "spot",
        "cancel_batch",
        True,
        lambda ns: ns.cancel_batch([{"currency_pair": "BTC_USDT", "id": "1001"}]),
    ),
    _MutatingCall(
        "spot",
        "spot",
        "amend_order",
        True,
        lambda ns: ns.amend_order("1001", "BTC_USDT", {"amount": "2"}),
    ),
    # -- spot: the venue declares it on none of these
    _MutatingCall(
        "spot", "spot", "countdown_cancel_all", False, lambda ns: ns.countdown_cancel_all(30)
    ),
    _MutatingCall(
        "spot",
        "spot",
        "create_price_order",
        False,
        lambda ns: ns.create_price_order({"market": "BTC_USDT"}),
    ),
    _MutatingCall(
        "spot", "spot", "cancel_price_order", False, lambda ns: ns.cancel_price_order("77")
    ),
    _MutatingCall(
        "spot",
        "spot",
        "cancel_price_orders",
        False,
        lambda ns: ns.cancel_price_orders(market="BTC_USDT"),
    ),
    # -- perpetual futures: declared on the five order endpoints
    _MutatingCall(
        "futures", "futures", "create_order", True, lambda ns: ns.create_order(FUTURES_ORDER)
    ),
    _MutatingCall(
        "futures",
        "futures",
        "create_batch_orders",
        True,
        lambda ns: ns.create_batch_orders([FUTURES_ORDER]),
    ),
    _MutatingCall("futures", "futures", "cancel_order", True, lambda ns: ns.cancel_order("1001")),
    _MutatingCall("futures", "futures", "cancel_all", True, lambda ns: ns.cancel_all("BTC_USDT")),
    _MutatingCall(
        "futures",
        "futures",
        "amend_order",
        True,
        lambda ns: ns.amend_order("1001", {"size": 400}),
    ),
    # -- perpetual futures: declared on none of these
    _MutatingCall(
        "futures", "futures", "countdown_cancel_all", False, lambda ns: ns.countdown_cancel_all(30)
    ),
    _MutatingCall(
        "futures",
        "futures",
        "create_price_order",
        False,
        lambda ns: ns.create_price_order({"initial": {"contract": "BTC_USDT"}}),
    ),
    _MutatingCall(
        "futures", "futures", "cancel_price_order", False, lambda ns: ns.cancel_price_order("77")
    ),
    _MutatingCall(
        "futures",
        "futures",
        "cancel_price_orders",
        False,
        lambda ns: ns.cancel_price_orders(contract="BTC_USDT"),
    ),
    # -- futures account settings: not order endpoints, and none declare it
    _MutatingCall(
        "futures",
        "futures",
        "update_position_leverage",
        False,
        lambda ns: ns.update_position_leverage("BTC_USDT", "5"),
    ),
    _MutatingCall(
        "futures",
        "futures",
        "update_position_margin",
        False,
        lambda ns: ns.update_position_margin("BTC_USDT", "10"),
    ),
    _MutatingCall(
        "futures",
        "futures",
        "update_position_risk_limit",
        False,
        lambda ns: ns.update_position_risk_limit("BTC_USDT", "1000000"),
    ),
    _MutatingCall(
        "futures",
        "futures",
        "set_position_cross_mode",
        False,
        lambda ns: ns.set_position_cross_mode("BTC_USDT", "CROSS"),
    ),
    _MutatingCall("futures", "futures", "set_dual_mode", False, lambda ns: ns.set_dual_mode(False)),
    # -- delivery: same methods, a path Gate.io does not document the header for
    _MutatingCall(
        "delivery", "futures", "create_order", True, lambda ns: ns.create_order(FUTURES_ORDER)
    ),
    _MutatingCall("delivery", "futures", "cancel_order", True, lambda ns: ns.cancel_order("1001")),
    _MutatingCall("delivery", "futures", "cancel_all", True, lambda ns: ns.cancel_all("BTC_USDT")),
    # -- options: the venue declares the header nowhere in this API
    _MutatingCall(
        "options",
        "options",
        "create_order",
        False,
        lambda ns: ns.create_order({"contract": "BTC-1", "size": 1, "price": "10"}),
    ),
    _MutatingCall("options", "options", "cancel_order", False, lambda ns: ns.cancel_order("1001")),
    _MutatingCall(
        "options", "options", "cancel_all", False, lambda ns: ns.cancel_all(contract="BTC-1")
    ),
    _MutatingCall(
        "options", "options", "countdown_cancel_all", False, lambda ns: ns.countdown_cancel_all(30)
    ),
)


@pytest.mark.parametrize(
    "call",
    MUTATING_CALLS,
    ids=[f"{c.namespace}.{c.method}" for c in MUTATING_CALLS],
)
async def test_mutating_call_sends_the_deadline_gateio_declares_for_it(call: _MutatingCall):
    """Drive each call and read `x-gate-exptime` off the request it produced.

    Fails on ``expiring=True`` being dropped from any single namespace method:
    delete it from ``GateioSpotHttpAPI.cancel_batch`` and the ``spot.cancel_batch``
    case fails, from ``GateioFuturesHttpAPI.amend_order`` and ``futures.amend_order``
    and ``delivery.amend_order`` fail. It fails the other way too — adding
    ``expiring=True`` to ``create_price_order`` fails ``spot.create_price_order``,
    because the pages that now name the price orders as an exception would be
    describing a request the client no longer sends.
    """
    rec = Recorder()
    client = make_client(rec)
    client._clock_synced = True  # what the execution client's connect achieves

    await call.invoke(NAMESPACE_FACTORIES[call.namespace](client))

    sent = EXPIRY_HEADER in rec.last.headers
    assert sent is call.deadline, (
        f"{call.namespace}.{call.method} sent {'a' if sent else 'no'} {EXPIRY_HEADER} deadline "
        f"on {rec.last.method} {rec.path}, and Gate.io's specification says it should send "
        f"{'one' if call.deadline else 'none'}"
    )


def _transport_mutations(module: ModuleType) -> set[str]:
    """Namespace methods in ``module`` that change state through the transport.

    Found by reading the module's own source rather than by listing them here,
    so that a namespace method added later cannot escape the table above: an
    order endpoint nobody classified is an order endpoint nobody decided the
    deadline for.
    """
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute) or func.attr not in (
                "post",
                "delete",
                "put",
                "patch",
            ):
                continue
            target = func.value
            if isinstance(target, ast.Attribute) and target.attr == "_client":
                found.add(node.name)
    return found


@pytest.mark.parametrize(
    ("module", "expected"),
    [(spot_module, 10), (futures_module, 14), (options_module, 4)],
    ids=["spot", "futures", "options"],
)
def test_every_mutating_namespace_method_is_classified(module: ModuleType, expected: int):
    """No state-changing call in an order namespace may skip the table above.

    Fails on a new endpoint arriving unclassified: add
    ``async def create_tpsl_order(self, body): return await
    self._client.post("/spot/tpsl_orders", body=body)`` to ``spot.py`` and this
    fails with ``create_tpsl_order``, which is the moment to look up whether
    Gate.io declares ``x-gate-exptime`` for it. Fails too if a method loses its
    entry while keeping its endpoint.

    The counts are asserted so that the comparison cannot pass by both sides
    going empty — a source scan that matched nothing would otherwise agree with
    a table that listed nothing.
    """
    in_source = _transport_mutations(module)
    classified = {
        c.method for c in MUTATING_CALLS if c.module == module.__name__.rsplit(".", 1)[-1]
    }

    assert len(in_source) == expected, (
        f"{module.__name__} makes {len(in_source)} mutating transport calls, not {expected}; "
        f"update the deadline table and this count together: {sorted(in_source)}"
    )
    assert in_source == classified, (
        "these mutating methods carry no entry in MUTATING_CALLS, so nothing checks whether "
        f"they send a submission deadline: {sorted(in_source - classified)}; and these entries "
        f"name methods that no longer mutate anything: {sorted(classified - in_source)}"
    )
