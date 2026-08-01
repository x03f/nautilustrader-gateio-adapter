"""Execution client for Gate.io.

One client trades every configured product — spot (optionally on a margin
ledger), USDT perpetual futures, BTC-settled (inverse) perpetual futures, USDT
delivery futures and USDT-settled options — through a single Nautilus account.

Account model
-------------
Gate.io keeps a **separate wallet per product**. Funds do not flow between them
implicitly: an account with USDT in the spot wallet cannot open a futures
position until the balance is transferred (:meth:`GateioExecutionClient.transfer`).
The client aggregates the wallets of the enabled products into one Nautilus
account and logs a warning at startup so the segregation is never a surprise.

The account is :class:`AccountType.CASH` only when spot is the sole configured
product *and* it trades the plain spot ledger; every other combination is a
margin account. Position mode is one-way (:class:`OmsType.NETTING`); hedge
(dual) mode is detected at connect and refused with an explanatory error, since
silently changing a venue-side account setting is never this client's decision.

Event sources
-------------
The private WebSocket is the primary event source: ``orders`` drive the order
lifecycle, ``usertrades`` drive fills, ``balances`` drive account state, and
``positions`` are parsed and logged but never published as reports — REST is the
single reconciliation source for positions, which keeps a fill from producing
two competing views of the same position. A REST poll
(``account_polling_interval_secs``) refreshes account state as a safety net.

Order translation
-----------------
=========================  ====================================================
Nautilus order             Gate.io encoding
=========================  ====================================================
MARKET (spot SELL)         ``type=market``, ``amount`` = base quantity
MARKET (spot BUY, quote)   ``type=market``, ``amount`` = quote amount
MARKET (spot BUY, base)    aggressive IOC ``limit`` bounded by the pair slippage
MARKET (derivatives)       ``price="0"`` with ``tif="ioc"``
LIMIT                      ``price``, ``tif`` gtc/ioc/fok, post-only -> ``poc``
STOP_*/ *_IF_TOUCHED       the product's price-triggered endpoint
=========================  ====================================================

Anything Gate.io cannot express without changing the order's meaning (GTD, DAY,
AT_THE_OPEN, reduce-only on spot, a quote-denominated quantity anywhere but a
spot market buy) is refused with an explicit reason rather than silently
altered. The whole request is built before the submission is announced, so
every such refusal arrives as ``OrderDenied`` — the platform's event for an
order Nautilus itself will not submit — and never as ``OrderSubmitted``
followed by an ``OrderRejected`` that would blame Gate.io for a decision this
client made. ``OrderRejected`` is emitted only for a refusal the venue actually
answered with.

Sizes on futures, delivery and options are **contract counts**: the Nautilus
``Quantity`` is the number of contracts and is sent as a signed integer, positive
for a buy and negative for a sell.

Order lists
-----------
An order list with no contingency is a set of independent orders: it is grouped
by product and sent through the venue's batch endpoint where one exists and the
group fits it, and one order at a time otherwise. A **contingent** list — a
bracket, or anything carrying ``linked_order_ids`` — is refused in full, because
Gate.io's attached take-profit / stop-loss returns no order id for the attached
leg and NautilusTrader needs three addressable orders. Contingent orders reach
this venue through the platform's own ``OrderEmulator`` instead; see
:meth:`GateioExecutionClient._submit_order_list`.

Client order ids
----------------
Gate.io's ``text`` field carries the client order id. It must start with ``t-``,
hold at most 28 further characters and use only ``[0-9A-Za-z_.-]``. A Nautilus
:class:`ClientOrderId` that fits is embedded verbatim, so the mapping is
recoverable from the venue alone after a restart; one that does not fit is
replaced by a generated id and the pair is kept in an in-memory alias table.

Conditional-order identity
--------------------------
A price-triggered order lives in its own id space. Gate.io arms it under an
"auto order" id and, when the trigger fires, creates a **different** order with
a **different** id; from that moment every update and every fill names the new
id. Both identities stay meaningful, so the client keeps both
(:class:`GateioTriggerLink`) and indexes them in each direction:

    armed id  <->  client order id  <->  fired id

The armed id is the only handle that can disarm the order and the only key the
venue's price-order listings answer to; the fired id is what every subsequent
event carries. The rebase itself goes through ``OrderUpdated`` with
``venue_order_id_modified=True``, which is the only event NautilusTrader accepts
carrying a venue order id different from the one already on the order.

The map is rebuilt from the venue after a restart, never assumed to be in
memory: futures and delivery echo the client id in ``initial.text`` and publish
the fired order as ``trade_id`` on the price order, while **spot publishes no
client id at all** — a spot ``put`` block has no such field (its ``text`` is an
order-source marker like ``api``) — so the only link is ``fired_order_id`` on
the price order. Reconciliation therefore lists armed *and* finished price
orders, and a fired order that arrives on the stream with no recoverable
identity triggers a re-read of this client's armed orders rather than being
reported as somebody else's.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from functools import partial
from typing import Any, Final

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor, LogLevel
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    BatchCancelOrders,
    CancelAllOrders,
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    QueryAccount,
    SubmitOrder,
    SubmitOrderList,
)
from nautilus_trader.execution.reports import (
    ExecutionMassStatus,
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import (
    AccountType,
    ContingencyType,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
    TriggerType,
    order_type_to_str,
    time_in_force_to_str,
    trigger_type_to_str,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import (
    AccountBalance,
    Currency,
    MarginBalance,
    Money,
    Price,
    Quantity,
)
from nautilus_trader.model.orders import Order

from nautilus_gateio.common.constants import (
    CLIENT_ORDER_ID_MAX_BODY,
    CLIENT_ORDER_ID_PREFIX,
    GATEIO,
    GATEIO_VENUE,
)
from nautilus_gateio.common.enums import (
    GateioFinishAs,
    GateioProductType,
    GateioSpotAccountMode,
    GateioTimeInForce,
    liquidity_side_from_gateio,
    order_side_from_gateio,
    order_side_to_gateio,
    order_status_from_gateio,
    order_type_from_gateio,
    time_in_force_from_gateio,
    time_in_force_to_gateio,
)
from nautilus_gateio.common.errors import (
    GateioError,
    GateioServerError,
    OrderValidationError,
    UnsupportedOrderError,
    WalletNotProvisionedError,
    WalletQueryRefusedError,
)
from nautilus_gateio.common.parsing import (
    timestamp_to_nanos,
    to_decimal,
    to_exact_decimal,
    to_int,
    to_lot_count,
)
from nautilus_gateio.common.signing import generate_client_order_id
from nautilus_gateio.common.symbols import (
    gateio_to_instrument_id,
    instrument_id_to_gateio,
    parse_option_symbol,
)
from nautilus_gateio.config import GateioExecClientConfig, validate_products
from nautilus_gateio.http.client import GateioHttpClient, GateioRequestAmbiguousError
from nautilus_gateio.http.futures import GateioFuturesHttpAPI
from nautilus_gateio.http.margin import GateioMarginHttpAPI, require_wallet
from nautilus_gateio.http.options import GateioOptionsHttpAPI
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.http.wallet import GateioWalletHttpAPI
from nautilus_gateio.websocket.private import ALL, GateioPrivateWebSocket


class PositionStatusUnavailable(Exception):
    """The venue was asked about a position and no answer came back.

    Raised rather than returned, because a returned list cannot say this.
    NautilusTrader distinguishes "the venue says flat" from "the venue did not
    answer" only by whether
    :meth:`GateioExecutionClient.generate_position_status_reports` raises:
    ``LiveExecutionEngine._did_position_status_query_fail`` skips a venue whose
    query raised, and the startup path counts the raise as a failed
    reconciliation rather than reconciling against nothing. Swallowing the
    failure and answering with silence — or worse, with FLAT — hands the engine a
    claim the venue never made, and the engine squares the book against it with a
    RECONCILIATION order and an inferred fill.
    """


class FillReportsUnavailable(Exception):
    """A fill query answered for some products and failed for others.

    The engine keeps one brake against squaring a position to flat: it refuses to
    do it when the trade listing failed, so that the *next* cycle can apply the
    venue's own closing trade instead of an inferred one
    (``LiveExecutionEngine._process_cached_position_discrepancies`` guards the
    squaring with ``not had_fill_query_errors``). That flag is set from one place
    and one place only — ``_query_and_find_missing_fills`` sets it when a client's
    ``generate_fill_reports`` *raises*. A client that logs a per-product failure
    and returns what it collected reports the failure as "no fills", and the brake
    never engages: the position is closed with a synthetic trade id and zero
    commission, and because it is then no longer open it is never queried again,
    so the real trade arriving later is never applied. The loss is permanent.

    The reports gathered before the failure are carried on the exception rather
    than discarded, because the two callers want different things from it. The
    position paths want the failure; the recovery paths (startup mass status,
    reconnect) want everything the venue *did* answer, since a mass status is
    reconciled against the orders it names and cannot square a position to flat on
    its own. Raising a bare error would make one 5xx on the options trade listing
    throw away the order and fill recovery a restart exists to perform.
    """

    def __init__(self, message: str, reports: list[FillReport]) -> None:
        super().__init__(message)
        #: Everything the venue did answer for, before and after the failure.
        self.reports: list[FillReport] = reports


class OrderReportsUnavailable(Exception):
    """An order listing was asked for and could not be answered in full.

    Raised both for a product whose listing endpoint failed and for a listing
    row whose deciding field this client cannot read (REC-06). The two are the
    same answer: what the venue holds is unknown, and a report set that merely
    omits the unreadable part is indistinguishable from "the venue has no such
    order" — a cached order the omitted row would have closed then stays open
    locally with nothing but a debug line downstream (the engine's
    ``_validate_reconciliation_state`` only warns), an open/closed disagreement
    with the venue that nothing repairs.

    The behaviour this buys from the engine cannot fabricate, verified against
    the installed source: at startup this client's ``generate_mass_status``
    turns the raise into a ``None`` mass status, ``reconcile_execution_state``
    returns False, and the kernel refuses to start the trader
    (system/kernel.py) — the same posture the platform's own base
    ``generate_mass_status`` takes for any raising report query
    (live/execution_client.py:440-514). On the continuous open-order check the
    raise is swallowed per client — ``_query_order_status_reports`` gathers
    with ``return_exceptions=True``, logs an ERROR and continues
    (live/execution_engine.py:1571-1580) — so the check proceeds treating this
    client's answer as empty. Under the default ``open_check_open_only=True``
    that is harmless: orders missing from the answer are only debug-logged.
    With ``open_check_open_only=False`` an own order whose row stays
    unreadable is counted missing on every cycle, and once
    ``open_check_missing_retries`` is exhausted the engine resolves it with a
    fabricated REJECTED or CANCELED (live/execution_engine.py:1465-1516) — a
    documented limitation of running that non-default mode against this
    alpha. On the single-order query the caller catches and answers ``None``,
    which the inflight check treats as an unanswered query.

    The reports parsed before the failure are carried for the caller that can
    use a partial answer loudly; the startup path deliberately does not.
    """

    def __init__(self, message: str, reports: list[OrderStatusReport]) -> None:
        super().__init__(message)
        #: Everything that was parsed before the failure.
        self.reports: list[OrderStatusReport] = reports


#: Order types this client can express on Gate.io.
SUPPORTED_ORDER_TYPES: Final[frozenset[OrderType]] = frozenset(
    {
        OrderType.MARKET,
        OrderType.LIMIT,
        OrderType.STOP_MARKET,
        OrderType.STOP_LIMIT,
        OrderType.MARKET_IF_TOUCHED,
        OrderType.LIMIT_IF_TOUCHED,
    },
)

#: Order types routed to a product's price-triggered ("auto order") endpoint.
CONDITIONAL_ORDER_TYPES: Final[frozenset[OrderType]] = frozenset(
    {
        OrderType.STOP_MARKET,
        OrderType.STOP_LIMIT,
        OrderType.MARKET_IF_TOUCHED,
        OrderType.LIMIT_IF_TOUCHED,
    },
)

#: Price-triggered spot orders name the plain spot ledger ``normal`` where a
#: regular order says ``spot``; cross margin has no representation here.
PRICE_ORDER_ACCOUNTS: Final[dict[GateioSpotAccountMode, str]] = {
    GateioSpotAccountMode.SPOT: "normal",
    GateioSpotAccountMode.MARGIN: "margin",
    GateioSpotAccountMode.UNIFIED: "unified",
}

#: Gate.io error labels meaning "this post-only order would have taken liquidity".
POST_ONLY_LABELS: Final[frozenset[str]] = frozenset(
    {"ORDER_POC_IMMEDIATE", "POC_FILL_IMMEDIATELY"},
)

#: Terminal ``finish_as`` reason for a post-only order that would have taken.
POST_ONLY_FINISH_AS: Final[str] = "poc"

#: Gate.io error labels answering a cancel with "there is no such live order".
#:
#: Gate.io's own error tables class these as benign idempotent races on cancel
#: — the futures table words it "treat cancel as done" — and this client's
#: transport replays ``DELETE`` on a transient failure (see `http/client.py`),
#: so they are also the ordinary answer to a cancellation it already performed.
#: Reporting one as a refusal reopens the order here while the venue holds
#: nothing (see `_resolve_cancel_of_a_vanished_order`).
#:
#: ``CANCEL_FAIL`` and ``NO_CHANGE`` are deliberately absent. Gate.io lists
#: them beside these as benign, but neither says the order is gone, and reading
#: "the cancel did not happen" as "the order is closed" is the same defect
#: pointing the other way.
CANCEL_ALREADY_DONE_LABELS: Final[frozenset[str]] = frozenset(
    {"ORDER_NOT_FOUND", "ORDER_CLOSED", "ORDER_CANCELLED", "ORDER_FINISHED"},
)

#: Gate.io trigger rules: 1 = fire when price >= trigger, 2 = when price <= trigger.
TRIGGER_RULE_ABOVE: Final[int] = 1
TRIGGER_RULE_BELOW: Final[int] = 2

#: Spot expresses the same two rules as literal comparison strings.
SPOT_TRIGGER_RULES: Final[dict[int, str]] = {
    TRIGGER_RULE_ABOVE: ">=",
    TRIGGER_RULE_BELOW: "<=",
}

#: Nautilus trigger type -> Gate.io futures ``trigger.price_type``.
FUTURES_TRIGGER_PRICE_TYPES: Final[dict[TriggerType, int]] = {
    TriggerType.DEFAULT: 0,
    TriggerType.LAST_PRICE: 0,
    TriggerType.MARK_PRICE: 1,
    TriggerType.INDEX_PRICE: 2,
}

#: Trigger types a spot price order can carry.
#:
#: Gate.io's spot trigger object is ``{price, rule, expiration}`` and has no
#: price-type field at all, so the venue's own "market price" is the only
#: reference available: ``DEFAULT`` and ``LAST_PRICE`` name it, and nothing else
#: can be expressed. Spot has no mark or index price to name in the first place,
#: so this is a narrower set than the futures one by the venue's design rather
#: than by omission.
SPOT_TRIGGER_TYPES: Final[frozenset[TriggerType]] = frozenset(
    {TriggerType.DEFAULT, TriggerType.LAST_PRICE},
)

#: Fallback slippage cap for a base-denominated spot market buy, used when the
#: pair definition carries no ``slippage`` field.
DEFAULT_SPOT_SLIPPAGE: Final[Decimal] = Decimal("0.05")

#: Window used by the report builders when the command carries no start time.
DEFAULT_LOOKBACK_SECS: Final[int] = 24 * 60 * 60

#: Largest number of orders Gate.io cancels in one spot batch request.
SPOT_CANCEL_BATCH_SIZE: Final[int] = 20

#: Caps on ``POST /spot/batch_orders``: at most four currency pairs, and at most
#: ten orders on any one of them (`http/spot.py`, :meth:`create_batch_orders`).
SPOT_BATCH_MAX_PAIRS: Final[int] = 4
SPOT_BATCH_MAX_ORDERS_PER_PAIR: Final[int] = 10

#: Cap on ``POST /futures/{settle}/batch_orders`` (`http/futures.py`).
FUTURES_BATCH_MAX_ORDERS: Final[int] = 10

#: Products whose REST namespace has a batch-order endpoint at all.
#:
#: Delivery futures and options have none: ``GateioFuturesHttpAPI`` raises
#: ``ValueError`` for delivery (`http/futures.py`, ``_require_perpetual``) and
#: ``GateioOptionsHttpAPI`` has no such method. An order list on those products
#: is submitted one order at a time, which loses nothing — an order list with no
#: contingency is a set of independent orders (see :meth:`_submit_order_list`).
BATCHABLE_PRODUCTS: Final[frozenset[GateioProductType]] = frozenset(
    {GateioProductType.SPOT, GateioProductType.PERP, GateioProductType.INVERSE},
)

#: Page size used when listing orders and fills.
REPORT_PAGE_LIMIT: Final[int] = 100

#: Hard cap on pages fetched for one report query, so a pathological venue
#: response can never turn reconciliation into an unbounded request loop.
MAX_REPORT_PAGES: Final[int] = 20

#: Products whose single-order endpoint resolves a client id given in ``text``.
#:
#: Gate.io documents the path parameter of ``GET /spot/orders/{order_id}`` and
#: ``GET /futures/{settle}/orders/{order_id}`` as the venue order id *or* the
#: user custom id (the ``text`` field), valid while the order rests and for a
#: short window after it finishes. ``GET /delivery/{settle}/orders/{order_id}``
#: and ``GET /options/orders/{order_id}`` document the venue-assigned id only,
#: so an order on those products is located by scanning the order listings —
#: see :meth:`GateioExecutionClient._report_by_client_order_id`.
CLIENT_ID_ADDRESSABLE_PRODUCTS: Final[frozenset[GateioProductType]] = frozenset(
    {GateioProductType.SPOT, GateioProductType.PERP, GateioProductType.INVERSE},
)

#: Characters Gate.io accepts in the ``text`` field after the ``t-`` prefix.
_TEXT_BODY_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^[0-9A-Za-z_.\-]{{1,{CLIENT_ORDER_ID_MAX_BODY}}}$",
)


def first_timestamp_ns(payload: dict[str, Any], *keys: str) -> int:
    """Return the first present, non-zero timestamp among ``keys``, in nanoseconds."""
    for key in keys:
        value = payload.get(key)
        if value in (None, "", 0, "0"):
            continue
        nanos = timestamp_to_nanos(value)
        if nanos:
            return nanos
    return 0


def exact_first_timestamp_ns(payload: dict[str, Any], *keys: str) -> int:
    """Like :func:`first_timestamp_ns`, but a stated-and-unreadable value raises.

    For the timestamps that decide money. A fill's execution time orders the
    fills the engine applies (reconciliation applies them in list order, and
    the first applied fill sets a floor under which later reports are skipped
    as already accounted for — installed live/execution_engine.py:3721-3736,
    3400-3416), and it feeds the staleness memory's latest-booked stamp. The
    forgiving reader skipped an unreadable value and the caller then stamped
    the row with local now — a fabricated time that sorts the fill last and
    outranks every honest venue stamp. Absent keys still answer 0: absence is
    the venue making no statement, and the caller decides what that means.
    """
    for key in keys:
        value = payload.get(key)
        if value in (None, "", 0, "0"):
            continue
        try:
            to_exact_decimal(value)
        except ValueError:
            raise ValueError(f"the '{key}' timestamp cannot be read: {value!r}") from None
        nanos = timestamp_to_nanos(value)
        if nanos:
            return nanos
    return 0


def require_field(payload: dict[str, Any], key: str, *, what: str) -> Any:
    """Return a field the payload must state, or refuse to read the payload.

    For deciding fields only: a payload that omits one is not making a smaller
    claim, it is a shape this client does not know how to read.
    """
    if key not in payload:
        raise ValueError(f"the {what} has no '{key}' field")
    return payload[key]


def exact_lots(payload: dict[str, Any], key: str, *, what: str) -> int:
    """Read a required whole-contract count strictly, naming field and value."""
    value = require_field(payload, key, what=what)
    try:
        return to_lot_count(value)
    except ValueError as e:
        raise ValueError(f"the {what}'s '{key}' field decides the answer: {e}") from None


def exact_decimal_field(payload: dict[str, Any], key: str, *, what: str) -> Decimal:
    """Read a required decimal field strictly, naming field and value."""
    value = require_field(payload, key, what=what)
    try:
        return to_exact_decimal(value)
    except ValueError as e:
        raise ValueError(f"the {what}'s '{key}' field decides the answer: {e}") from None


def optional_exact_decimal(
    payload: dict[str, Any],
    key: str,
    *,
    what: str,
    absent: str = "0",
) -> Decimal:
    """Read an optional decimal field strictly.

    Absent, ``None`` and ``""`` all mean the venue stated nothing, which takes
    the documented default (a fee that is not charged, a price that makes the
    order a market order). A value the venue did state and this client cannot
    read raises — the difference between a smaller claim and a failed read.
    """
    value = payload.get(key)
    if value in (None, ""):
        return Decimal(absent)
    try:
        return to_exact_decimal(value)
    except ValueError as e:
        raise ValueError(f"the {what}'s '{key}' field decides the answer: {e}") from None


def venue_symbol_of(payload: dict[str, Any]) -> str:
    """Return the Gate.io symbol a private payload refers to."""
    for key in ("currency_pair", "contract", "market"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def is_cash_buy_payload(payload: dict[str, Any]) -> bool:
    """Return whether a spot payload describes a quote-denominated cash buy.

    Gate.io denominates a spot MARKET BUY in the quote currency: ``amount`` is
    cash to spend, ``left`` is cash unspent, and ``filled_total`` is cash
    spent, while ``filled_amount`` is the base quantity bought. Mixing the two
    denominations is the venue's own documented trap, so every reader that
    compares an order's quantities asks this first.

    ``type == "market"`` with ``side == "buy"`` identifies the cash buy exactly,
    because a *base*-denominated spot market buy never reaches the venue as a
    market order: this client submits it as an aggressive limit order instead
    (`_build_aggressive_spot_buy`). The caller establishes that the payload is
    a spot one.
    """
    return (
        str(payload.get("type") or "").lower() == "market"
        and str(payload.get("side") or "").lower() == "buy"
    )


def is_ambiguous_outcome(error: BaseException) -> bool:
    """Return whether ``error`` leaves the venue's handling of a command unknown.

    NautilusTrader allows an execution client to emit ``OrderRejected``,
    ``OrderCancelRejected`` or ``OrderModifyRejected`` only for a *definitive*
    outcome — the venue must have refused the command (concepts/live.md, "Order
    command outcome policy"). Every other failure is ambiguous, and an ambiguous
    command must be left in its in-flight state for the engine to resolve.

    The classification is by exception type rather than by message or label,
    because only the transport knows whether the request reached the venue:

    * :class:`GateioRequestAmbiguousError` — the request was on the wire, and was
      either deliberately not replayed or replayed without ever being answered;
    * :class:`GateioServerError` — Gate.io answered 5xx, which it can do either
      before or after applying the request;
    * anything that is not a :class:`GateioError` — the adapter itself failed
      around a request that may already have been applied, typically while
      reading the response.

    A plain :class:`GateioError` is definitive. A 4xx is the venue's own refusal
    (live.md names HTTP 400, 401, 403 and 429 as proof of non-acceptance), and
    the status-0 errors this adapter raises — ``NETWORK_ERROR``,
    ``CLIENT_CLOSED``, ``MISSING_CREDENTIALS`` — are raised only when no byte of
    the request left the process.

    :class:`OrderValidationError` and :class:`UnsupportedOrderError` are the
    adapter's own pre-flight refusals, raised before anything is sent, so they
    are definitive too however deep in a submit path they occur.
    """
    if isinstance(error, OrderValidationError | UnsupportedOrderError):
        return False
    if isinstance(error, GateioRequestAmbiguousError | GateioServerError):
        return True
    return not isinstance(error, GateioError)


def trigger_rule(
    order_side: OrderSide,
    order_type: OrderType,
    trigger_price: Decimal,
    last_price: Decimal | None,
) -> int:
    """Return the Gate.io trigger rule for a conditional order.

    Gate.io does not model "stop" and "if touched" separately: it takes a bare
    comparison rule and pins it to the current market — rule ``1`` (fire at or
    above) requires a trigger strictly *above* the last price at submission and
    rule ``2`` (fire at or below) one strictly *below*, and violating that is
    refused at submission. One rule per order is therefore submittable, and it
    is the one the market implies.

    The order type is not thereby redundant, because the two are only
    interchangeable while the order is well formed. A stop is placed away from
    the market in the direction of the trade (BUY above, SELL below); an
    if-touched order towards it (BUY below, SELL above). For a well-formed order
    the market-derived rule and the type-derived rule agree and this function is
    a no-op check. When they disagree — a BUY ``STOP_MARKET`` whose trigger sits
    below the market, or a stop whose level the market has already breached —
    the only rule Gate.io will accept encodes the *other* order type: a breakout
    entry armed as a dip buy, in the same direction, with no event to say so.

    So the disagreement is refused rather than resolved. Refusing costs nothing
    that was available: the alternative is not "submit the order as asked", it
    is a venue rejection, because the type-derived rule is exactly the one the
    last-price constraint forbids. What it buys is a reason, and a caller that
    genuinely wants a trigger already in the market can emulate the order
    locally, where the platform releases it immediately instead.

    Without a last price nothing can contradict anything, so the type-derived
    rule stands on its own and the venue makes the final call.
    """
    if order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
        implied = TRIGGER_RULE_ABOVE if order_side == OrderSide.BUY else TRIGGER_RULE_BELOW
    else:
        implied = TRIGGER_RULE_BELOW if order_side == OrderSide.BUY else TRIGGER_RULE_ABOVE

    if last_price is None or last_price <= 0:
        return implied

    from_market = TRIGGER_RULE_ABOVE if trigger_price >= last_price else TRIGGER_RULE_BELOW
    if from_market != implied:
        raise UnsupportedOrderError(
            f"a {order_type_to_str(order_type)} to "
            f"{'BUY' if order_side == OrderSide.BUY else 'SELL'} triggers "
            f"{'above' if implied == TRIGGER_RULE_ABOVE else 'below'} the market, but the "
            f"trigger price {trigger_price} is "
            f"{'at or above' if from_market == TRIGGER_RULE_ABOVE else 'below'} the last price "
            f"{last_price}. Gate.io takes only a comparison rule and requires it to agree with "
            f"the market, so this order can be armed only as the opposite conditional type. "
            f"Correct the trigger price, submit the type the level implies, or emulate the "
            f"order locally with `emulation_trigger`",
        )
    return from_market


#: Order types NautilusTrader accepts an ``OrderTriggered`` event for. Gate.io
#: also arms stop-market style orders, but for those the venue-order-id rebase
#: is the whole transition (see ``_maybe_swap_trigger_venue_order_id``).
TRIGGERABLE_ORDER_TYPES: Final[frozenset[OrderType]] = frozenset(
    {OrderType.STOP_LIMIT, OrderType.TRAILING_STOP_LIMIT, OrderType.LIMIT_IF_TOUCHED}
)

#: ``status`` values a price-triggered order reports while it is still armed.
ARMED_TRIGGER_STATUSES: Final[frozenset[str]] = frozenset({"", "open"})

#: Terminal order statuses a late fill can still be booked from.
#:
#: Gate.io does not order ``*.orders`` against ``*.usertrades``, so the message
#: that closes an order can win the race against the fill that caused it. Whether
#: that fill can still be applied is decided by the platform's own order state
#: table, and among the terminal statuses it holds exactly two entries —
#: ``(CANCELED, PARTIALLY_FILLED)`` and ``(CANCELED, FILLED)``, both annotated
#: "Real world possibility" (installed model/orders/base.pyx:132-133). Every
#: other terminal status raises ``InvalidStateTrigger``, which
#: ``LiveExecutionEngine._reconcile_fill_report`` (live/execution_engine.py:3379)
#: catches and turns into a log line, so a fill routed there is not reconciled —
#: it is discarded.
FILLABLE_TERMINAL_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {OrderStatus.CANCELED},
)

#: Local order statuses in which an ACCEPTED order status report can be handed
#: to the engine on its own. ``_handle_order_status_transitions`` generates an
#: ``OrderAccepted`` for any order not already accepted, and the platform's
#: state table (installed model/orders/base.pyx) accepts that trigger only from
#: INITIALIZED and SUBMITTED. From PARTIALLY_FILLED the engine's
#: ``_apply_event_to_order`` would swallow the refusal as a warning rather than
#: fail, but the report would then also be claiming a filled quantity below the
#: order's, which is a disagreement to report rather than to act on.
RESTATABLE_ORDER_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {OrderStatus.INITIALIZED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED},
)

#: Report statuses for which ``_handle_order_status_transitions`` returns a
#: verdict instead of falling through to ``_handle_fill_quantity_mismatch``.
#:
#: Read off the installed engine (live/execution_engine.py:3237-3305): each of
#: these has a branch ending in ``return True``, so no report carrying one of
#: them can reach the inferred-fill path. They are still not interchangeable —
#: every branch but ACCEPTED's *acts* on the order (rejects, triggers, cancels or
#: expires it) rather than merely restating its quantity, which is why
#: :meth:`GateioExecutionClient._restate_from_listing` sends only ACCEPTED alone
#: and leaves the rest to the grouped hand-over, where the trades under the
#: report are reconciled before the terminal event is generated.
SHORT_CIRCUIT_REPORT_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {
        OrderStatus.REJECTED,
        OrderStatus.ACCEPTED,
        OrderStatus.TRIGGERED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
    },
)

#: The statuses Gate.io publishes on a spot order row. The cash-market-buy
#: guard in `_parse_spot_order_fields` must classify the row before it can say
#: whether a base-denominated quantity exists, so a status outside this set is
#: an unreadable deciding field, not an open order.
SPOT_ORDER_STATUSES: Final[frozenset[str]] = frozenset({"open", "closed", "cancelled"})

#: How many event-loop turns to give a rebasing ``OrderUpdated`` before declaring
#: that it did not arrive. Three would do — the engine's enqueuer schedules the
#: put with ``call_soon_threadsafe``, its queue task then wakes and applies the
#: event — and the margin is there so the bound never becomes the reason a
#: correct rebase is reported as lost. The point of the bound is only that a
#: stopped engine costs a log line instead of a hang.
VENUE_ORDER_ID_REBASE_TURNS: Final[int] = 100

#: Local order statuses an ``OrderRejected`` can still be applied from.
#:
#: Read off the installed platform's own state table (model/orders/base.pyx):
#: ``REJECTED`` is reachable from these six and from nothing else. The entry
#: that is missing matters — there is no ``PARTIALLY_FILLED -> REJECTED``, so a
#: venue payload saying the order finished as post-only, arriving after a fill
#: has been booked against it here, would raise ``InvalidStateTrigger`` inside
#: the execution engine if it were reported as a rejection.
REJECTABLE_ORDER_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {
        OrderStatus.INITIALIZED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED,
        OrderStatus.TRIGGERED,
        OrderStatus.PENDING_UPDATE,
        OrderStatus.PENDING_CANCEL,
    },
)


class GateioTriggerLink:
    """The durable two-way identity of one price-triggered order.

    Gate.io arms a conditional order under one id and, when the trigger fires,
    creates a **brand new order with a different id**. Both ids stay meaningful
    for the whole life of the order: the armed id is the only handle that can
    disarm it, and the fired id is the handle every subsequent order update,
    cancel and fill uses. Discarding either one loses the order.

    Spot makes the link mandatory rather than merely convenient. The ``put``
    block of a spot price order has no client-id field at all — its ``text`` is
    an order-source marker (``api``/``web``/``app``) — so the Nautilus client
    order id cannot be embedded in the fired order and can only be recovered by
    following ``fired_order_id`` back to the armed order. The link is therefore
    rebuilt during reconciliation from the price-order listing endpoints, and on
    futures additionally from ``initial.text``.

    Parameters
    ----------
    product : GateioProductType
        The product the armed order lives on (it selects the REST namespace).
    armed_id : str
        The venue id of the armed ("auto") order.
    client_order_id : ClientOrderId
        The Nautilus client order id both ids belong to.
    fired_id : str, optional
        The venue id of the order the trigger created, once it is known.

    """

    __slots__ = ("armed_id", "client_order_id", "fired_id", "product")

    def __init__(
        self,
        product: GateioProductType,
        armed_id: str,
        client_order_id: ClientOrderId,
        fired_id: str | None = None,
    ) -> None:
        self.product = product
        self.armed_id = armed_id
        self.client_order_id = client_order_id
        self.fired_id = fired_id

    @property
    def is_armed(self) -> bool:
        """Return whether the order is still waiting for its trigger."""
        return self.fired_id is None

    @property
    def venue_order_id(self) -> VenueOrderId:
        """Return the id that currently identifies the order at the venue."""
        return VenueOrderId(self.fired_id or self.armed_id)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(product={self.product.value}, armed_id={self.armed_id!r}, "
            f"client_order_id={self.client_order_id.value!r}, fired_id={self.fired_id!r})"
        )


class GateioOrderRequest:
    """A Gate.io request body, built in full before anything is sent.

    This type exists to hold a boundary the platform draws in its order state
    machine. ``OrderDenied`` is reachable only from ``INITIALIZED`` (installed
    1.230.0, ``model/orders/base.pyx`` state table: ``(INITIALIZED, DENIED)``
    and ``(RELEASED, DENIED)`` are its only entries), so a refusal this client
    decides on its own has to be decided **before** ``generate_order_submitted``
    or it cannot be expressed at all. Building the body is what decides those
    refusals: every one of them is a function of the order object and the
    instrument, and none of them consults Gate.io.

    Separating the build from the send makes the boundary structural rather
    than a list of checks somebody has to remember to hoist: a refusal added to
    a builder later lands on the denial side by construction, because the
    builder runs before the submission is announced.

    Parameters
    ----------
    body : dict[str, Any]
        The JSON body to send.
    is_trigger : bool
        Whether the body addresses a price-order endpoint rather than the
        regular order endpoint.
    trigger_price : Price, optional
        The trigger price, kept for the log line the trigger path writes once
        the venue has armed the order.
    trigger_rule : int, optional
        The venue trigger rule, kept for the same log line.

    """

    __slots__ = ("body", "is_trigger", "trigger_price", "trigger_rule")

    def __init__(
        self,
        body: dict[str, Any],
        is_trigger: bool = False,
        trigger_price: Price | None = None,
        trigger_rule: int | None = None,
    ) -> None:
        self.body = body
        self.is_trigger = is_trigger
        self.trigger_price = trigger_price
        self.trigger_rule = trigger_rule

    def __repr__(self) -> str:
        return f"{type(self).__name__}(is_trigger={self.is_trigger}, body={self.body!r})"


class GateioExecutionClient(LiveExecutionClient):
    """Provides an execution client for Gate.io.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client_id : ClientId
        The client id.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    instrument_provider : InstrumentProvider
        The instrument provider (configured for the same products).
    http_client : GateioHttpClient
        The shared REST transport.
    config : GateioExecClientConfig
        The client configuration.

    Raises
    ------
    ValueError
        If the configured product set is empty or not served by the configured
        environment. The configuration struct is frozen, so this validation
        happens here rather than on the struct.

    Warnings
    --------
    Gate.io wallets are segregated per product. Balances are aggregated across
    the wallets of the enabled products, but funds must be moved explicitly with
    :meth:`transfer` before another product can use them.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: Any,
        http_client: GateioHttpClient,
        config: GateioExecClientConfig,
    ) -> None:
        products = validate_products(config.products, config.environment)
        spot_mode = config.spot_account_mode

        cash_account = set(products) == {GateioProductType.SPOT} and not spot_mode.is_margin
        account_type = AccountType.CASH if cash_account else AccountType.MARGIN

        if spot_mode.is_margin:
            # Spot margin ledgers settle borrowed balances, which a cash account
            # otherwise refuses to hold as a negative balance.
            try:
                AccountFactory.register_cash_borrowing(GATEIO)
            except KeyError:
                pass  # Already registered by another client instance

        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=GATEIO_VENUE,
            oms_type=OmsType.NETTING,
            account_type=account_type,
            base_currency=None,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )

        self._set_account_id(AccountId(f"{GATEIO}-master"))

        self._config = config
        self._products: tuple[GateioProductType, ...] = products
        self._spot_mode = spot_mode
        self._account_type = account_type

        # REST namespaces
        self._http_client = http_client
        self._spot_http = GateioSpotHttpAPI(http_client)
        self._margin_http = GateioMarginHttpAPI(http_client)
        self._options_http = GateioOptionsHttpAPI(http_client)
        self._wallet_http = GateioWalletHttpAPI(http_client)
        self._futures_http: dict[GateioProductType, GateioFuturesHttpAPI] = {
            GateioProductType.PERP: GateioFuturesHttpAPI(http_client, settle="usdt"),
            GateioProductType.INVERSE: GateioFuturesHttpAPI(http_client, settle="btc"),
            GateioProductType.FUT: GateioFuturesHttpAPI(
                http_client,
                settle="usdt",
                delivery=True,
            ),
        }

        # WebSocket connections, one per product
        self._ws_clients: dict[GateioProductType, GateioPrivateWebSocket] = {}
        self._user_id: str | None = None

        # Client order id <-> Gate.io `text` aliases
        self._text_by_client_order_id: dict[ClientOrderId, str] = {}
        self._client_order_id_by_text: dict[str, ClientOrderId] = {}
        self._generated_text: re.Pattern[str] = re.compile(
            rf"^{re.escape(CLIENT_ORDER_ID_PREFIX + config.client_order_id_tag)}-\d+$",
        )

        # Price-triggered ("auto") orders keep their own id space. Both the armed
        # id and the fired id are retained for the life of the order and indexed
        # in each direction, so the identity survives the transition and a
        # restart (see `GateioTriggerLink`).
        self._trigger_links: dict[ClientOrderId, GateioTriggerLink] = {}
        self._trigger_by_armed_id: dict[str, GateioTriggerLink] = {}
        self._trigger_by_fired_id: dict[str, GateioTriggerLink] = {}
        #: Venue order ids whose fired-order resolution has already been attempted.
        self._trigger_resolution_attempts: set[str] = set()

        # Fills already applied, so a replayed usertrade cannot fill twice
        self._applied_trade_ids: dict[ClientOrderId, set[str]] = {}
        #: Spot cash buys in flight: {client order id: (base credited so far,
        #: base quantity stated on the order)}. Gate.io denominates a spot
        #: market buy in the quote currency and states its base total only when
        #: the order finishes, so until then the order carries a bound built
        #: from the venue's own fills (see `_raise_cash_buy_bound`). The order
        #: object cannot be read for either figure: it still carries its
        #: pre-update state while the execution engine's queue is undrained.
        self._cash_buy_bounds: dict[ClientOrderId, tuple[Decimal, Decimal | None]] = {}

        # Last known wallet state per currency: {currency: (total, free)}
        self._balances: dict[str, tuple[Decimal, Decimal]] = {}
        # Gate.io keeps a separate wallet per product, so balances are tracked
        # per wallet and only then aggregated per currency. A stream update for
        # one wallet must never overwrite another wallet's contribution.
        self._wallet_balances: dict[GateioProductType, dict[str, tuple[Decimal, Decimal]]] = {}
        # A Unified Account reports one cross-product balance per currency that
        # already subsumes the per-product wallets, so it replaces them in the
        # aggregate instead of being added to them.
        self._unified_balances: dict[str, tuple[Decimal, Decimal]] = {}
        # Margins are kept per product for the same reason balances are: a poll
        # that could not read one wallet must not delete that wallet's margin
        # from the snapshot, because `MarginAccount.apply` replaces its stores
        # from the event rather than merging with what it already held.
        self._margins_by_product: dict[
            GateioProductType,
            dict[InstrumentId | Currency, MarginBalance],
        ] = {}

        self._account_poll_task: asyncio.Task | None = None
        #: Last private-stream event per product, used as the reconciliation
        #: lookback anchor after a reconnect.
        self._last_stream_event_ns: dict[GateioProductType, int] = {}

        #: What REST recovery booked, per instrument: (signed base delta of the
        #: fills it applied, latest fill timestamp in ns). This is the memory
        #: behind `_position_answer_is_stale`: recovery reads the position
        #: listing and the trade listing at different instants, so a position
        #: row can predate a trade both listings' venue has already matched.
        #: An entry is written by `_record_recovery_bookings` for every fill
        #: booked onto an instrument this node held prior knowledge for — a
        #: cached order the fill extended, or a pre-existing open position
        #: (see that method for why either suffices and why neither is
        #: dropped) — and cleared only on venue proof: a position answer for
        #: the instrument that contains the booked trades, or one stamped
        #: strictly after them.
        self._recovery_booked: dict[InstrumentId, tuple[Decimal, int]] = {}
        #: The venue's own timestamp of the last parsed position row, per
        #: instrument, exactly as stated: 0 when the row stated none. The
        #: report's `ts_last` cannot serve the staleness rule because it falls
        #: back to local now — a stamp this client fabricated, which postdates
        #: any booked trade by construction and silently bypassed the rule
        #: (R7C-02).
        self._position_row_venue_ts: dict[InstrumentId, int] = {}

        self._log.info(f"Products: {', '.join(p.value for p in products)}", LogColor.BLUE)
        self._log.info(f"Spot account mode: {spot_mode.value}", LogColor.BLUE)
        self._log.info(f"Account type: {account_type.name}", LogColor.BLUE)
        self._log.info(f"Environment: {config.environment}", LogColor.BLUE)

    # -- properties --------------------------------------------------------

    @property
    def products(self) -> tuple[GateioProductType, ...]:
        """Return the products this client trades."""
        return self._products

    @property
    def user_id(self) -> str | None:
        """Return the numeric Gate.io account id used by the private channels."""
        return self._user_id

    @property
    def spot_account(self) -> str:
        """Return the ``account`` value sent with spot orders."""
        return self._spot_mode.value

    # -- lifecycle ---------------------------------------------------------

    async def _connect(self) -> None:
        await self._instrument_provider.initialize()
        self._cache_instruments()

        self._log.warning(
            "Gate.io keeps a separate wallet per product; balances are aggregated across the "
            "enabled products but funds must be moved with `transfer()` before another product "
            "can use them",
        )

        await self._load_user_id()
        await self._assert_one_way_position_mode()
        await self._update_account_state()
        await self._await_account_registered()

        for product in self._products:
            await self._connect_private_websocket(product)

        interval = self._config.account_polling_interval_secs
        if interval and interval > 0:
            self._account_poll_task = self.create_task(
                self._poll_account_state(interval),
                log_msg="poll_account_state",
            )

    async def _disconnect(self) -> None:
        # Release this client's share of the transport. It is reference counted,
        # so the socket pool closes only once every holder has let go (seam-08).
        await self._http_client.close()
        if self._account_poll_task is not None:
            self._account_poll_task.cancel()
            self._account_poll_task = None

        for product, ws_client in self._ws_clients.items():
            try:
                await ws_client.disconnect()
            except Exception as e:  # noqa: BLE001 - shutdown must not raise
                self._log.warning(f"Error disconnecting {product.value} WebSocket: {e}")
        self._ws_clients.clear()

    def _cache_instruments(self) -> None:
        """Publish the provider's instruments and currencies into the cache."""
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)
        for instrument in self._instrument_provider.get_all().values():
            self._cache.add_instrument(instrument)

    async def _load_user_id(self) -> None:
        """Fetch the numeric account id required by the derivative private channels."""
        try:
            payload = await self._wallet_http.fee()
        except GateioError as e:
            self._log.warning(f"Cannot read /wallet/fee ({e.label}); falling back to /spot/fee")
            try:
                payload = await self._spot_http.fee()
            except GateioError as inner:
                payload = {}
                self._log.warning(f"Cannot read the account user id: {inner}")

        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        self._user_id = str(user_id) if user_id not in (None, "") else None

        needs_user_id = any(not p.is_spot for p in self._products)
        if needs_user_id and self._user_id is None:
            raise RuntimeError(
                "Gate.io private futures, delivery and options channels require the account's "
                "numeric user id, which could not be read from /wallet/fee or /spot/fee; check "
                "that the API key has read permission for the account",
            )

    async def _assert_one_way_position_mode(self) -> None:
        """Refuse to run against a futures account in hedge (dual) position mode.

        Nautilus nets positions per instrument, so a venue holding a separate
        long and short leg for the same contract cannot be reconciled. The
        remedy is a venue-side setting the operator must change deliberately;
        this client never changes it.
        """
        for product in self._products:
            if not product.is_perpetual:
                continue  # Delivery futures and options have no hedge mode
            api = self._futures_http[product]
            try:
                account = await require_wallet(
                    api.accounts(),
                    f"the {product.value} futures wallet",
                )
            except WalletNotProvisionedError as e:
                self._log.warning(f"Skipping position mode check for {product.value}: {e}")
                continue

            mode = str(account.get("position_mode") or "single").lower()
            if account.get("in_dual_mode") or mode != "single":
                raise RuntimeError(
                    f"Gate.io {product.value} account is in '{mode}' position mode "
                    f"(hedge/dual). This client trades one-way (netted) positions only. "
                    f"Close all positions and pending orders, switch the account back to "
                    f"one-way mode in the Gate.io interface or via POST "
                    f"/futures/{product.settle}/dual_mode?dual_mode=false, then restart.",
                )

    async def _connect_private_websocket(self, product: GateioProductType) -> None:
        url = self._config.resolve_ws_url(product)
        api_key, api_secret = self._credentials()
        ws_client = GateioPrivateWebSocket(
            product=product,
            handler=partial(self._handle_ws_message, product),
            api_key=api_key,
            api_secret=api_secret,
            user_id=self._user_id,
            url=url,
            testnet=self._config.is_testnet,
            loop=self._loop,
            on_reconnect=partial(self._handle_ws_reconnect, product),
        )
        self._ws_clients[product] = ws_client
        await ws_client.connect()
        await self._subscribe_private_channels(product, ws_client)
        self._log.info(f"Connected private WebSocket for {product.value}: {url}")

    async def _subscribe_private_channels(
        self,
        product: GateioProductType,
        ws_client: GateioPrivateWebSocket,
    ) -> None:
        await ws_client.subscribe_orders(symbols=[ALL])
        await ws_client.subscribe_user_trades(symbols=[ALL])
        await ws_client.subscribe_balances()
        if not product.is_spot:
            await ws_client.subscribe_positions(symbols=[ALL])
        if self._spot_mode.is_margin and product.is_spot:
            self._log.info(
                "Spot margin ledger balances (borrowed and interest) are refreshed by the "
                "REST account poll; the plain spot balance stream does not carry them",
            )

    def _handle_ws_reconnect(self, product: GateioProductType) -> None:
        """Reconcile after a reconnect: Gate.io offers no private stream replay."""
        self._log.warning(
            f"{product.value} private WebSocket reconnected; refreshing account state and "
            f"re-querying orders and fills over the outage window",
        )
        self.create_task(
            self._reconcile_after_reconnect(product),
            log_msg="reconnect_reconciliation",
        )

    async def _reconcile_after_reconnect(self, product: GateioProductType) -> None:
        """Re-query orders and fills that the private stream could not replay.

        Gate.io offers neither replay nor resume on any private channel and
        publishes no sequence numbers on ``*.orders``, ``*.usertrades`` or
        ``*.balances``, so every transition that happened while the socket was
        down is simply never delivered. Refreshing the account state alone would
        leave the order and fill gaps unreconciled, so the reconnect runs the
        same REST queries reconciliation uses and feeds the results back through
        the execution engine, which de-duplicates them by venue order id and
        venue trade id.

        The results are handed over as **one** ``ExecutionMassStatus`` rather
        than as a stream of individual reports. That is not a cosmetic choice.
        ``ExecEngine.reconcile_execution_report`` reconciles each report against
        the local order *in isolation*, and neither report is self-sufficient:

        * an order status report alone states a filled quantity without the
          trades that produced it, so the engine closes the difference with an
          **inferred** fill carrying a synthetic trade id. The venue's own trade
          then arrives with the real id, which the engine cannot recognise as the
          same execution: it is either applied on top (4 lots recorded as 8) or,
          when the inferred fill is stamped later than the real one, discarded —
          and with it the only key by which any later replay could be matched;
        * a fill report alone cannot restate the order's quantity, so a fill
          against an order the venue has since amended is applied to a quantity
          that cannot hold it. ``Order.apply(OrderUpdated)`` never triggers a
          terminal status, so a restatement arriving after the last fill leaves
          the order at zero remaining quantity and permanently PARTIALLY_FILLED.

        Merely sending the fills before the orders fixes the first half and
        creates the second. ``_reconcile_execution_mass_status`` takes an order
        report *together with* its trades: it restates the quantity first,
        applies the venue's own trades under their own ids next, and only then
        infers anything for a residual difference. That is exactly the order
        these two failure modes require, and it is the same path startup
        reconciliation already uses.

        Grouping alone is not enough, though, because it makes delivery of a
        trade *conditional on the report it was grouped under* — both on that
        report's status and on the engine's duplicate filter keeping the report
        at all. See :meth:`_hand_over_unapplied_fills` for what that costs, and
        for why the sweep afterwards may not simply re-offer the trade on its
        own.
        """
        await self._update_account_state()

        anchor_ns = self._last_stream_event_ns.get(product, 0)
        now_ns = self._clock.timestamp_ns()
        lookback_ns = DEFAULT_LOOKBACK_SECS * 1_000_000_000
        start_ns = max(anchor_ns, now_ns - lookback_ns) if anchor_ns else now_ns - lookback_ns
        start = unix_nanos_to_dt(start_ns)

        try:
            order_reports = await self.generate_order_status_reports(
                GenerateOrderStatusReports(
                    instrument_id=None,
                    start=start,
                    end=None,
                    open_only=False,
                    command_id=UUID4(),
                    ts_init=self._clock.timestamp_ns(),
                    log_receipt_level=LogLevel.DEBUG,
                ),
            )
            try:
                fill_reports = await self.generate_fill_reports(
                    GenerateFillReports(
                        instrument_id=None,
                        venue_order_id=None,
                        start=start,
                        end=None,
                        command_id=UUID4(),
                        ts_init=self._clock.timestamp_ns(),
                    ),
                )
            except FillReportsUnavailable as e:
                # The reconnect pass hands the engine order reports whose
                # filled quantities the missing trades were meant to back;
                # reconciling that partial pairing makes the engine mint
                # commission-less inferred fills for the difference (the
                # confident-zero refutation drove exactly this through the
                # installed engine). Keeping the pre-reconnect state is
                # stale-but-honest, and the next reconnect or restart repairs
                # it from a listing that answers in full.
                self._log.error(
                    f"Cannot reconcile after the {product.value} reconnect: the trade "
                    f"listing did not answer in full ({e}). Keeping local state until a "
                    f"complete answer; the next reconnect or restart recovers it",
                )
                return
        except Exception as e:  # noqa: BLE001 - a failed re-query must not kill the client
            self._log.error(f"Cannot reconcile after the {product.value} reconnect: {e}")
            return

        mass_status = ExecutionMassStatus(
            client_id=self.id,
            account_id=self.account_id,
            venue=self.venue,
            report_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        mass_status.add_order_reports(reports=order_reports)
        mass_status.add_fill_reports(reports=fill_reports)

        # The order reports are indexed before the hand-over because
        # `_reconcile_execution_mass_status` rewrites `mass_status`'s own tables
        # in place (`_adjust_mass_status_fills` reassigns `_order_reports` and
        # `_deduplicate_mass_status_orders` deletes from it), so reading them
        # back out of the mass status afterwards would not show what the venue
        # answered.
        listed_orders = {
            report.venue_order_id: report
            for report in order_reports
            if report.venue_order_id is not None
        }

        # The venue may be reporting an order under an id the local order does
        # not hold — a conditional that fired during the outage is the ordinary
        # case — and every execution grouped under that id would be refused by
        # the order until it is rebased.
        await self._adopt_reported_venue_order_ids(order_reports)

        # Snapshot which venue trades are not yet on their orders before any of
        # them is booked: everything in this list is a trade this pass books —
        # by the grouped hand-over or by the sweep — that a position row read
        # in the same window may predate. The orders and the instruments with
        # open positions held right now are snapshotted with it, because they
        # are the prior knowledge that decides which of these trades arm the
        # stale-answer memory (`_record_recovery_bookings`) — everything taken
        # before anything books, so a position this pass opens can never count
        # as pre-existing (that would re-freeze the fresh-cache restart,
        # R7C-01).
        unbooked_before = [r for r in fill_reports if not self._fill_is_booked(r)]
        known_before = {order.client_order_id for order in self._cache.orders()}
        positions_before = {
            position.instrument_id for position in self._cache.positions_open(venue=None)
        }
        self._record_recovery_bookings(unbooked_before, known_before, positions_before)

        self._send_mass_status_report(mass_status)
        try:
            await self._hand_over_unapplied_fills(fill_reports, listed_orders)
        except FillReportsUnavailable as e:
            # The sweep found a venue-named trade whose order statement did not
            # come back readably. On this route the grouped hand-over above has
            # already applied what it could; the remaining executions stand at
            # the venue, and the next reconnect or restart re-reads the order
            # and books them. Stale-but-honest, as with the failed trade
            # listing above (REC-08).
            self._log.error(
                f"Cannot finish reconciling after the {product.value} reconnect: {e}. "
                f"Keeping local state until a complete answer; the next reconnect or "
                f"restart recovers it",
            )
            return

        self._log.info(
            f"Reconciled {len(order_reports)} order and {len(fill_reports)} fill reports after "
            f"the {product.value} reconnect",
            LogColor.GREEN,
        )

    async def _hand_over_unapplied_fills(
        self,
        fill_reports: list[FillReport],
        listed_orders: dict[VenueOrderId, OrderStatusReport],
    ) -> None:
        """Book, one order at a time, every recovered trade not yet on its order.

        Both recovery routes end here. On a reconnect it runs *after* the
        grouped hand-over, sweeping what that pass did not book; on a restart
        it runs *inside* :meth:`generate_mass_status`, before the engine has
        reconciled anything, because after that method returns the client has
        no correct moment left (see the docstring there for why the engine's
        own reconciliation cannot be relied on to book these).

        Grouping a trade under its order report is what lets the engine restate
        the order's quantity before applying the trade, but it also makes
        delivery of the trade conditional on that report. Both conditions bite
        on an ordinary reconnect:

        * ``LiveExecutionEngine._reconcile_order_report`` asks
          ``_handle_order_status_transitions`` about the *order report* first,
          and that returns "reconciled" — before the ``for trade in trades`` loop
          is ever reached — for ``ACCEPTED``, ``TRIGGERED``, ``REJECTED``, and
          for ``CANCELED``/``EXPIRED`` when the local order already holds that
          status;
        * before any of that, ``_deduplicate_mass_status_orders``
          (live/execution_engine.py:2084-2120) deletes an order report whose
          cached order has the same status, filled quantity, instrument and side
          — **and deletes the fills grouped under it in the same breath**. The
          test does not look at the quantity, so the exact pairing this recovery
          exists to catch (venue snapshot still fully open, trade already in the
          trade listing, local order untouched) is dropped in full: the grouped
          pass never runs at all for it.

        Two further drops have nothing to do with either: a report whose
        reconciliation raises ``InvalidStateTrigger`` (caught per order,
        remaining trades abandoned), and a fill whose venue order id has no order
        report at all.

        Rather than predict which applies — the CANCELED and EXPIRED branches
        depend on local order state, so no static rule can — this checks the
        outcome. ``_send_mass_status_report`` dispatches synchronously over the
        message bus and the engine applies reconciliation fills immediately, so
        by the time it returns the cache already shows which trade ids were
        booked.

        What is left has to be re-offered **with the venue's own statement of the
        order**, never on its own. A ``FillReport`` cannot restate a quantity,
        and ``Order.apply(OrderUpdated)`` never triggers a terminal status
        (model/orders/base.pyx applies an update without an FSM trigger), so an
        execution applied to a quantity the venue can no longer reach leaves the
        order in ``cache.orders_open()`` permanently — through further
        reconnects and through a restart, because every later pass restates the
        quantity *after* the fill and no restatement can close an order. That is
        a worse outcome than the missed trade this sweep exists to repair, so the
        order is restated first (:meth:`_restate_from_listing`) and the trade is
        only re-offered alone once the order can actually take it
        (:meth:`_reoffer_recovered_fills`).

        The sweep works **per order, not per trade**. An aggressive order walks
        the book, so Gate.io routinely answers with several trades against one
        order id, and a trade re-offered on its own would then be handed to
        ``_reconcile_execution_mass_status`` alongside an order report claiming
        the *total* filled quantity: the engine closes that difference with an
        inferred fill carrying a synthetic trade id and no commission
        (``_handle_fill_quantity_mismatch``, live/execution_engine.py:3164), and
        the next real trade is rejected on top of it as a duplicate or an
        overfill. Losing a real venue trade id to a fabricated one costs the fee
        that trade paid — a spot buy's fee is withheld in the base currency, so
        the position is overstated by it — and destroys the only key by which a
        later replay of that trade could be recognised.

        Raises :class:`FillReportsUnavailable` when a venue-named trade cannot
        be booked because no readable statement of its order could be obtained
        at all (REC-08): each calling pass turns that into its own honest
        refusal — a ``None`` startup mass status, a kept reconnect state.
        """
        unapplied: dict[VenueOrderId | None, list[FillReport]] = {}
        for report in fill_reports:
            if self._fill_is_booked(report):
                continue
            unapplied.setdefault(report.venue_order_id, []).append(report)

        for venue_order_id, reports in unapplied.items():
            self._log.warning(
                f"{len(reports)} recovered trade(s) "
                f"({', '.join(r.trade_id.value for r in reports)}) are not yet booked "
                f"for {venue_order_id!r}; offering them with the venue's own statement "
                f"of the order",
            )
            self._restate_from_listing(listed_orders.get(venue_order_id))
            await self._reoffer_recovered_fills(reports)

    def _record_recovery_bookings(
        self,
        unbooked_before: list[FillReport],
        known_before: set[ClientOrderId],
        positions_before: set[InstrumentId],
    ) -> None:
        """Remember, per instrument, the venue trades this recovery pass books.

        ``unbooked_before`` are the venue trades the listings named that were
        not on their orders when recovery began — everything this pass sets
        out to book; ``known_before`` and ``positions_before`` are the orders
        and the instruments with open positions the cache held at the same
        instant. All three are snapshotted before the pass books anything, so
        nothing this pass creates can count as prior knowledge. The signed
        sum and the latest venue timestamp feed
        :meth:`_position_answer_is_stale`: a position answer that fails to
        contain these trades, and cannot be shown to be newer than them, is a
        stale read rather than a venue statement that the trades did not
        happen.

        The invariant (REC-07): the protection holds for **every** venue
        trade this pass books onto an instrument this node held prior
        knowledge for — knowledge being a cached order the trade extended,
        *or* a cached open position on the instrument — regardless of the
        provenance of the order the trade rode (cache-held, adopted,
        external) and regardless of the net delta of the bookings. The
        memory then clears only on venue proof (see the reader). The set
        recorded is what the listings named, not what the in-call sweep
        managed to book: a trade the sweep leaves unbooked with its order in
        the cache is booked moments later by the engine's own reconciliation
        of the very mass status this pass returns — after any post-sweep
        arming would have run (and a sweep that cannot obtain any statement
        of the order now refuses the whole mass status instead, REC-08). By
        the time a position answer is judged, a recorded trade is either in
        the cache (so agreement clears the memory) or genuinely missing from
        the book (so withholding is the correct fail-safe: the alternative
        is the engine squaring the gap with a fabricated execution).

        The one case that arms nothing is a trade on an instrument with no
        pre-existing position, riding an order this node never held —
        fresh-cache recovery of history. There the pre-booking book is
        emptiness, not flatness: nothing the memory could refuse is
        refutable, and arming it anyway is what froze the ordinary
        no-database restart of a closed partial-window round trip against the
        venue's *current* flat row for the length of the lookback (R7C-01).
        Round eight keyed that exception per ORDER, where the memory and the
        erasure are per INSTRUMENT: one fill booked onto an adopted order
        left the whole instrument unarmed, and a stale answer erased the
        pre-existing position together with the adopted bookings while
        reconciliation reported success (REC-07, R8-F1). The pre-existing
        open position is exactly the knowledge that makes a disagreeing,
        unprovably-fresh answer refutable, so it is what widens the arming
        here.
        """
        for report in unbooked_before:
            order = self._order_of_report(report)
            extends_known = order is not None and order.client_order_id in known_before
            if not extends_known and report.instrument_id not in positions_before:
                self._log.debug(
                    f"Not arming the stale-answer memory for {report.trade_id.value}: "
                    f"neither its order nor an open {report.instrument_id} position was "
                    f"in the cache when recovery began, so there is no prior book a "
                    f"position answer could be refuted against",
                )
                continue
            quantity = report.last_qty.as_decimal()
            signed = quantity if report.order_side == OrderSide.BUY else -quantity
            delta, latest_ts = self._recovery_booked.get(
                report.instrument_id,
                (Decimal(0), 0),
            )
            # The delta is a diagnostic beside latest_ts: the reader decides
            # on freshness and agreement alone. Two bounds are accepted as
            # such rather than closed. A pass that fails between arming and
            # booking re-records the same still-unbooked trades on the next
            # attempt, so the delta in the reader's log line inflates across
            # retries while the max keeps latest_ts exact. And an entry
            # armed for a SPOT instrument is inert: spot position queries
            # answer before the staleness rule is consulted, so nothing
            # reads it and nothing ever pops it. Neither bound touches money
            # or availability.
            self._recovery_booked[report.instrument_id] = (
                delta + signed,
                max(latest_ts, report.ts_event),
            )

    def _position_answer_is_stale(
        self,
        instrument_id: InstrumentId,
        signed_qty: Decimal,
        venue_ts_ns: int,
    ) -> bool:
        """Return whether a position answer predates trades recovery just booked.

        Recovery reads the order listing, the trade listing and the position
        listing as separate requests at separate instants, so a match landing
        between two of those reads produces a position row that does not yet
        contain a trade the trade listing already names. The engine cannot see
        that: it takes every position report as the venue's current truth and
        squares the local book against it with a reconciliation order and an
        inferred fill (`_reconcile_position_report_netting`, installed
        live/execution_engine.py) — deleting the venue trade id, price and fee
        this client just booked and replacing them with an execution nobody
        made. The trade listing is the finer-grained and later read, so a row
        that cannot contain the trades it names, and cannot be shown to
        postdate them, loses — whatever it reads.

        The test, in this order:

        * no trades were booked for the instrument in this recovery — every
          answer stands (the ordinary case, and the reason the periodic
          position check keeps working);
        * the row is stamped strictly after the last booked trade — it is the
          fresher statement and stands, whatever it says. Equal stamps do not
          qualify: both listings report whole seconds, so a row written just
          before a trade in the same second is indistinguishable from one
          written just after, and the reading that cannot misstate money is
          the trade listing's. ``venue_ts_ns`` is the venue's own stamp; a row
          that stated none arrives as 0, never as local now, because a stamp
          this client fabricated postdates any booked trade by construction
          and would bypass this rule silently (R7C-02);
        * the answer equals the local book as it now stands — it contains the
          booked trades, so it is current; it stands and clears the memory;
        * **anything else is withheld.** Not only the answer equal to the
          pre-booking book: any answer that does not contain the booked
          trades and cannot be shown to postdate them supports no claim. The
          previous rule withheld exactly the pre-booking book and believed
          everything staler — an absent row, or a kept zero-size row
          predating a position booked in an earlier session, matched neither
          book and was taken as a current statement, so the engine squared a
          pre-existing position to FLAT with a fabricated execution and
          reported success (REC-05). The staler the answer, the more
          confidently it was applied. Withholding degrades to the fail-safe
          refusal instead: the engine hears an unanswered query, startup
          reconciliation returns False, and the kernel refuses to start until
          the venue produces a row this test can tell apart.

        These two proofs — a strictly-later venue stamp, or agreement with
        the post-booking book — are the only ways an armed memory clears, for
        every entry alike: the net delta of the bookings plays no part in the
        decision (REC-07, R8-F2 — see the body). The memory is kept on a
        withheld answer so a later fresher or agreeing answer can clear it,
        and it dies with the process — so the protection is exactly one
        restart deep: a venue still serving the stale row to the *next*
        process meets a pass that books nothing new, arms nothing, and
        squares to the row. That residual is documented rather than closed
        (persisting the memory means persisting it somewhere a restart reads,
        which this alpha does not do). What arms the memory — every booking
        on an instrument this node held prior knowledge for, whatever the
        order's provenance — is stated on
        :meth:`_record_recovery_bookings`, together with the one deliberate
        gap (fresh-cache reconstruction, R7C-01). Withholding itself pauses
        position reconciliation for the instrument; it never books or
        unbooks anything.
        """
        entry = self._recovery_booked.get(instrument_id)
        if entry is None:
            return False
        # The signed delta is recorded for the log line below and for
        # operators reading the memory; it takes no part in the decision. An
        # earlier form popped the entry at delta == 0 before any comparison,
        # and a zero-net outage round trip — ordinary strategy behaviour —
        # disarmed the instrument, so the very next stale row erased the
        # pre-existing position beneath the round trip (REC-07, R8-F2). A
        # non-empty booking set that nets to zero still cannot be contained
        # in an answer that disagrees with the post-booking book.
        delta, latest_ts = entry
        if venue_ts_ns > latest_ts:
            self._recovery_booked.pop(instrument_id, None)
            return False
        cache_net = Decimal(0)
        for position in self._cache.positions_open(venue=None, instrument_id=instrument_id):
            cache_net += position.signed_decimal_qty()
        if signed_qty == cache_net:
            self._recovery_booked.pop(instrument_id, None)
            return False
        self._log.debug(
            f"Withholding the {instrument_id} position answer of {signed_qty}: the cache "
            f"holds {cache_net} after this recovery booked a net {delta:+}, and the "
            f"answer's stamp does not postdate the booked trades",
        )
        return True

    def _withhold_stale_position_reports(
        self,
        reports: list[PositionStatusReport],
    ) -> list[PositionStatusReport]:
        """Keep only the position reports the booked trades do not refute.

        Runs after the recovery sweep, on the account-wide answer gathered for
        the startup mass status. A withheld row is not handed to the engine at
        all: the engine reconciles a mass status' position reports against the
        cache *after* the orders and fills, so by then the cache already
        carries the booked trades and a pre-trade row would square them away.
        The engine re-queries any open position the mass status does not
        cover, and that query answers honestly — with the fresher row once the
        venue has caught up, or with `PositionStatusUnavailable` while the
        answer is still the stale one.
        """
        kept: list[PositionStatusReport] = []
        for report in reports:
            if self._position_answer_is_stale(
                report.instrument_id,
                report.signed_decimal_qty,
                # The venue's own stamp of the parsed row, 0 when it stated
                # none; `report.ts_last` falls back to local now, which would
                # read as fresher than any booked trade (R7C-02).
                self._position_row_venue_ts.get(report.instrument_id, 0),
            ):
                self._log.warning(
                    f"Withholding the {report.instrument_id} position row from the mass "
                    f"status: it does not contain venue trades this recovery just booked "
                    f"and is not stamped after them, so it is a stale read, not a "
                    f"statement that those trades did not happen",
                )
                continue
            kept.append(report)
        return kept

    def _prune_reports_the_sweep_outran(
        self,
        order_reports: list[OrderStatusReport],
        booked_now: list[FillReport],
    ) -> list[OrderStatusReport]:
        """Drop order snapshots that predate executions this pass just booked.

        Considered are only reports about orders the sweep just advanced, and
        of those only the statuses that fall through the engine's status
        short-circuits into ``_handle_fill_quantity_mismatch`` (installed
        live/execution_engine.py:3164) while claiming *fewer* fills than the
        order now carries. That branch books nothing either way, but it reads
        the disagreement as duplicate fills or corrupted cache, logs it at
        ERROR and fails the reconciliation — and a failed startup
        reconciliation stops the whole node (system/kernel.py: a False from
        ``reconcile_execution_state`` aborts start). The snapshot is not
        wrong, it is old: the trade listing, read moments later, named the
        executions and this client booked them.

        An ACCEPTED snapshot is deliberately kept even when it is equally
        stale: the engine short-circuits on it without reaching the mismatch
        handler (a re-ACCEPT of a filled order is swallowed as an idempotent
        transition), so it is harmless — and keeping it preserves the mass
        status as the venue's answer rather than this client's edit of it.
        """
        outran = {
            order.client_order_id
            for report in booked_now
            if (order := self._order_of_report(report)) is not None
        }
        if not outran:
            return order_reports
        kept: list[OrderStatusReport] = []
        for report in order_reports:
            order = self._order_of_report(report)
            if (
                order is not None
                and order.client_order_id in outran
                and report.order_status not in SHORT_CIRCUIT_REPORT_STATUSES
                and report.filled_qty < order.filled_qty
            ):
                self._log.info(
                    f"Withholding the stale {report.venue_order_id!r} order snapshot "
                    f"(filled {report.filled_qty}) from the mass status: the order already "
                    f"carries {order.filled_qty} from trades this recovery just booked",
                )
                continue
            kept.append(report)
        return kept

    async def _adopt_reported_venue_order_ids(
        self,
        order_reports: list[OrderStatusReport],
    ) -> None:
        """Move local orders onto the venue order id the venue is reporting them under.

        Gate.io arms a price-triggered order under one id and creates a **new
        order with a different id** when the trigger fires. Across a restart the
        cached order still holds the armed id, while the venue's order listing —
        and every trade it produced — speaks under the fired one. Nothing in
        reconciliation bridges that: ``create_order_filled_event``
        (live/reconciliation.py:381) stamps the fill with
        ``report.venue_order_id``, ``Order.apply`` refuses an ``OrderFilled``
        whose venue order id differs from the order's, and ``_reconcile_fill_report``
        (live/execution_engine.py:3379-3383) catches that ``ValueError`` and logs
        it away. The execution is simply gone.

        ``OrderUpdated`` is the one carrier the platform lets change a venue
        order id, and the engine emits one only through ``_should_update``
        (:3307-3318), which compares quantity, price and trigger price and never
        the venue order id. So the rebase has to come from this client, before
        the reports are handed over — which is what
        :meth:`_maybe_swap_trigger_venue_order_id` already does for stream
        events, and this is the same call on the reconciliation path.

        The wait afterwards is not a delay, it is the outcome check. A client's
        events reach the order through ``ExecEngine.process``, which
        ``LiveExecutionEngine.process`` (:467-482) hands to a ``ThrottledEnqueuer``
        that schedules the put with ``loop.call_soon_threadsafe`` for a separate
        task to drain — so the update is applied a few loop turns later, whereas
        the reports go over ``msgbus.send`` and are reconciled inline. Yielding
        until the order object actually carries the new id is the only way to
        know the fill that follows can land, and it is bounded so that an engine
        which is not draining its queue costs a log line rather than a hang.
        """
        pending: list[tuple[Order, VenueOrderId]] = []
        ts_now = self._clock.timestamp_ns()
        for report in order_reports:
            venue_order_id = report.venue_order_id
            if venue_order_id is None:
                continue
            order = self._order_of_report(report)
            if order is None or order.is_closed:
                continue
            if order.venue_order_id is None or order.venue_order_id == venue_order_id:
                continue
            self._maybe_swap_trigger_venue_order_id(order, venue_order_id, ts_now)
            if self._cache.venue_order_id(order.client_order_id) == venue_order_id:
                # The rebase was accepted (or had already been emitted): the
                # cache mapping is written synchronously by it, so it is the one
                # signal that does not depend on event delivery. Where it was
                # declined the order is left alone and nothing is waited for.
                pending.append((order, venue_order_id))

        for _ in range(VENUE_ORDER_ID_REBASE_TURNS):
            if all(order.venue_order_id == value for order, value in pending):
                return
            await asyncio.sleep(0)

        for order, value in pending:
            if order.venue_order_id != value:
                self._log.error(
                    f"{order.client_order_id!r} still holds venue order id "
                    f"{order.venue_order_id} after the rebase onto {value} was emitted; "
                    f"every execution Gate.io reports under {value} will be refused by the "
                    f"order and lost",
                )

    def _restate_from_listing(self, order_report: OrderStatusReport | None) -> None:
        """Put the venue's own order quantity on the order, before its trade lands.

        The restatement has to reach the order *synchronously*, and only one of
        the two channels an execution client has does that.
        ``generate_order_updated`` publishes to ``ExecEngine.process``, which
        ``LiveExecutionEngine.process`` enqueues (live/execution_engine.py) for a
        separate task to drain, while ``_send_mass_status_report`` and
        ``_send_fill_report`` are ``msgbus.send`` to a handler that runs inline.
        An update emitted as an event would therefore be applied *after* the fill
        that is sent right behind it — the ordering that leaves the order stuck
        open. The report channel restates the order through the engine's own
        ``_should_update`` -> ``_generate_order_updated`` -> ``_handle_event``,
        all inline, before this method returns.

        What may be restated is decided by one question — *can this report make
        the engine mint an inferred fill?* — answered off the installed engine
        rather than guessed. ``_reconcile_order_report``
        (live/execution_engine.py:3093-3107) reaches
        ``_handle_fill_quantity_mismatch`` only when
        ``_handle_order_status_transitions`` returns ``None``, and that method
        (:3237-3305) returns a verdict instead — short-circuiting — for REJECTED,
        ACCEPTED, TRIGGERED, CANCELED and EXPIRED reports. Inside
        ``_handle_fill_quantity_mismatch`` (:3164-3235) an inferred fill is
        generated on exactly one branch: ``report.filled_qty > order.filled_qty``
        with the order still open. Equal quantities return having generated
        nothing, and a report claiming *fewer* fills than the order holds logs a
        diagnostic and returns False without generating anything either.

        So the guard admits:

        * an ACCEPTED report against an order in :data:`RESTATABLE_ORDER_STATUSES`
          — the branch short-circuits, and the status set is what keeps the
          ``OrderAccepted`` it also emits from being one the FSM refuses;
        * any report that falls through, provided its filled quantity does not
          exceed the order's — the one combination in which the inferred fill
          cannot be reached.

        and refuses the remaining short-circuiting statuses. Not for the inferred
        fill, which they cannot produce either, but because their branches do
        something other than restate: they reject, trigger, cancel or expire the
        order. Sent alone, ahead of the trade this sweep is about to re-offer,
        a CANCELED or EXPIRED report closes the order *before* its execution is
        booked — the reverse of the order ``_reconcile_execution_mass_status``
        uses, where those branches reconcile the trades grouped under the report
        first. Those reports reach the order through the grouped hand-over,
        which is where they belong.

        The old guard was ACCEPTED-only, which is far narrower than its own
        stated reason and misses the pairing that matters most: a local order
        that already carries a fill. ``_deduplicate_mass_status_orders``
        (:2028-2120) compares status, filled quantity, instrument and side, and
        **not** quantity, so a venue snapshot that has reduced the order while
        still reporting the filled quantity the cache holds is deleted as an
        exact duplicate together with every trade grouped under it. That is
        precisely ``report.filled_qty == order.filled_qty`` — the branch that
        generates nothing — and refusing to restate there leaves the order
        working against a remainder the venue has already cut.
        """
        if order_report is None:
            return
        order = self._order_of_report(order_report)
        if order is None or order.is_closed:
            return
        if order.quantity == order_report.quantity:
            return  # Nothing to restate
        if order.is_quote_quantity:
            # A Gate.io spot market buy is submitted as a quote-currency cash
            # amount, so its quantity is not comparable with the base-denominated
            # quantity of the venue's own report; the conversion belongs to the
            # grouped hand-over, which restates and applies in one pass.
            return
        if order_report.order_status == OrderStatus.ACCEPTED:
            if order.status not in RESTATABLE_ORDER_STATUSES:
                return
        elif order_report.order_status in SHORT_CIRCUIT_REPORT_STATUSES:
            return
        elif order_report.filled_qty > order.filled_qty:
            return
        self._log.info(
            f"Restating {order.client_order_id!r} from {order.quantity} to "
            f"{order_report.quantity} before re-offering its recovered trades: Gate.io's own "
            f"order listing is what the recovered executions have to add up to",
        )
        self._send_order_status_report(order_report)

    async def _reoffer_recovered_fills(self, reports: list[FillReport]) -> None:
        """Re-offer one order's recovered trades by the route that can book them.

        Each trade is offered on its own for as long as the order can take it,
        which is the cheapest route and the one that keeps every venue trade id.
        The moment one cannot be taken, that trade and every trade after it go
        over together with the venue's own statement of the order — together,
        because a hand-over carrying one trade of several makes the engine infer
        the rest.
        """
        for index, report in enumerate(reports):
            order = self._order_of_report(report)
            if order is None or order.is_closed or not self._order_can_take(order, report):
                # Either the order is not this client's to restate, or its
                # quantity cannot hold this execution (a venue-side reduction the
                # listing did not carry, or a spot market buy still denominated
                # in the quote currency, whose quantity is a cash amount and not
                # a quantity at all). A lone fill would be applied to it
                # regardless and the order could never reach a terminal status
                # again, so the order is re-read and they are handed over
                # together, which is the one path that restates before it
                # applies.
                await self._hand_over_fills_with_their_order(reports[index:])
                return
            self._send_fill_report(report)

    def _order_of_report(self, report: OrderStatusReport | FillReport) -> Order | None:
        """Return the local order a report is about, if this client holds one.

        The venue order id is the fallback and not the primary key because a
        report parsed out of a Gate.io listing carries a client order id only
        when the venue echoed the text this client wrote on the order.
        """
        client_order_id = report.client_order_id
        if client_order_id is None and report.venue_order_id is not None:
            client_order_id = self._cache.client_order_id(report.venue_order_id)
        return self._cache.order(client_order_id) if client_order_id is not None else None

    @staticmethod
    def _order_can_take(order: Order, report: FillReport) -> bool:
        """Return whether this execution fits the order as Nautilus now holds it.

        ``is_quote_quantity`` is the first test because it is a units question,
        not a size one: a Gate.io spot market buy is submitted as a quote-currency
        cash amount, so its quantity is not comparable with the base-denominated
        quantity of its own fills. 590 USDT is numerically larger than 0.01 BTC
        and means nothing next to it.
        """
        if order.is_quote_quantity:
            return False
        return order.leaves_qty >= report.last_qty

    def _hand_over_fill(self, report: FillReport) -> None:
        """Give the engine one execution that no order event can carry.

        A standalone ``FillReport`` lands in
        ``LiveExecutionEngine._reconcile_fill_report_single``, which resolves the
        order it belongs to **only** through
        ``cache.client_order_id(report.venue_order_id)`` (installed 1.230.0,
        live/execution_engine.py:2183-2200). ``report.client_order_id`` is read
        afterwards as a consistency check, never as a fallback. With that index
        empty the engine logs "FillReport received before OrderStatusReport ...
        deferring reconciliation" and returns False — and nothing ever comes back
        for it: ``reconcile_execution_report`` (:1816) keeps no deferral queue,
        and the sole retry loop (``_reconcile_missing_fills``, :1236) is driven by
        ``position_check_interval_secs``, which defaults to ``None``.
        concepts/execution.md ("External order is created from the fill") and
        concepts/live.md ("Fill reports arriving before order status reports are
        deferred") describe a version that behaves differently from the one we
        build against; the installed source decides.

        So an unindexed trade is not handed over on its own. The venue is asked
        for the order it belongs to, and the two go over together as an
        ``ExecutionMassStatus`` — the path ``_reconcile_execution_mass_status``
        (:1878-1910) implements: it creates the external order from the order
        report, indexes its venue order id, and only then applies the trades
        grouped under that id, each keeping its own ``trade_id`` and commission.

        The gate is the cached order object, not the index (REC-08): the engine
        indexes the ids of an order report it declined to adopt
        (``filter_unclaimed_external_orders``, installed
        live/execution_engine.py:1915-1925), and a lone fill sent at such a
        dangling entry crashes in ``_find_order_by_venue_order_id(order_side=None)``
        -> ``Cache.orders(side=None)`` — TypeError("an integer is required") —
        instead of deferring. Only an order the cache actually holds can take a
        single report; everything else goes the grouped route, whose
        no-statement raise is caught here and logged as a standing loss, since
        a stream-driven hand-over has no reconciliation pass to refuse.
        """
        client_order_id = self._cache.client_order_id(report.venue_order_id)
        if client_order_id is not None and self._cache.order(client_order_id) is not None:
            self._send_fill_report(report)
            return
        self.create_task(
            self._hand_over_stream_fill(report),
            log_msg=f"hand_over_fill_{report.trade_id.value}",
        )

    async def _hand_over_stream_fill(self, report: FillReport) -> None:
        """Run the grouped hand-over for a stream fill, absorbing its refusal.

        On the recovery routes a :class:`FillReportsUnavailable` from the
        grouped hand-over makes the pass refuse (a ``None`` mass status, a
        kept reconnect state). A stream fill has no pass to refuse — the next
        reconciliation is the retry — so the raise becomes the loss report the
        stream route owes and nothing else.
        """
        try:
            await self._hand_over_fills_with_their_order([report])
        except FillReportsUnavailable as e:
            self._log.error(
                f"Cannot book stream trade {report.trade_id.value}: {e}. The execution "
                f"stands at the venue and is recovered by the next reconciliation pass",
            )

    async def _hand_over_fills_with_their_order(self, reports: list[FillReport]) -> None:
        """Re-read one order and hand it over with every trade recovered for it.

        ``reports`` must all name the same venue order: the engine groups trades
        under an order report by venue order id, and the whole benefit of the
        grouped path is that ``_reconcile_execution_mass_status`` applies *all* of
        them, each under its own trade id and its own commission, before
        ``_handle_fill_quantity_mismatch`` compares the totals. Handing over a
        subset makes that comparison disagree by the trades left out, and the
        engine closes the difference with an inferred fill carrying a synthetic
        id and no commission — replacing real executions with a fabricated one.

        When the grouped hand-over leaves trades unbooked, what remains depends
        on one question — *does the cache hold the order object?* — and the
        answer decides between three exits (REC-08):

        * the order is cached: the single-report channel can attach the
          executions, and does, loudly (the last-resort loop below);
        * the order is not cached but the venue's statement was delivered: the
          engine heard the statement and declined to adopt the order
          (``filter_unclaimed_external_orders``), which is its configured
          ruling — the executions are excluded with their order and the
          exclusion is logged, never overridden and never escalated;
        * neither order nor statement: the trade is stated by the venue and
          cannot be attributed honestly, so this method raises
          :class:`FillReportsUnavailable` for the calling pass to refuse —
          the startup sweep turns it into a refused mass status, the reconnect
          keeps its stale-but-honest state, and the stream route logs the
          standing loss for the next reconciliation to repair.

        The index alone decides nothing. Live validation caught the previous
        reading — "the order is at least indexed, so the single-report path can
        still attach the executions" — booking a lone fill at an index entry the
        engine had written for an order it refused to create, which crashed the
        whole mass status inside ``Cache.orders(side=None)`` (see the comment at
        the check below). A dangling index is the engine's filtering read back
        as a promise: the swallowed-refusal-as-presence shape again, one level
        up from the payload readers.
        """
        if not reports:
            return
        first = reports[0]
        order_report = await self.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=first.instrument_id,
                client_order_id=None,
                venue_order_id=first.venue_order_id,
                command_id=UUID4(),
                ts_init=self._clock.timestamp_ns(),
            ),
        )
        if order_report is not None:
            mass_status = ExecutionMassStatus(
                client_id=self.id,
                account_id=self.account_id,
                venue=self.venue,
                report_id=UUID4(),
                ts_init=self._clock.timestamp_ns(),
            )
            # The engine groups trades under an order report by venue order id,
            # so the fills have to be added alongside the report of the very
            # order the venue attributed them to — which is the one that was just
            # queried by that id.
            mass_status.add_order_reports(reports=[order_report])
            mass_status.add_fill_reports(reports=reports)
            self._send_mass_status_report(mass_status)

        unbooked = [report for report in reports if not self._fill_is_booked(report)]
        if not unbooked:
            return

        # What the last-resort single-report channel needs is the cached order
        # OBJECT, and a bare index entry is not one. The engine indexes the ids
        # of an order report it has just declined to adopt —
        # `_reconcile_execution_mass_status` (installed
        # live/execution_engine.py:1915-1925) writes `add_venue_order_id` after
        # `filter_unclaimed_external_orders` dropped the order inside
        # `_generate_order` — so "indexed" proves only that the engine has heard
        # the id. A lone fill sent at such a dangling entry does not defer: the
        # engine resolves the index, misses the order, and falls into
        # `_find_order_by_venue_order_id(order_side=None)`, whose
        # `Cache.orders(side=None)` call the Cython signature refuses with
        # TypeError("an integer is required") — the crash that took down live
        # startup reconciliation (REC-08).
        client_order_id = self._cache.client_order_id(first.venue_order_id)
        order = self._cache.order(client_order_id) if client_order_id is not None else None

        if order is None:
            if order_report is not None:
                # Gate.io delivered its statement, the grouped hand-over offered
                # it, and the engine declined to adopt the order — its configured
                # ruling on unclaimed external orders (or an instrument it does
                # not carry). The executions are excluded together with their
                # order; recording that ruling is honest, overriding it is not.
                for report in unbooked:
                    self._log.warning(
                        f"Trade {report.trade_id.value} of {report.last_qty} at "
                        f"{report.last_px} on {report.instrument_id} is not booked: the "
                        f"execution engine did not adopt the venue's statement of order "
                        f"{report.venue_order_id!r} (unclaimed external orders can be "
                        f"filtered by configuration), so the execution is excluded "
                        f"together with its order",
                    )
                return
            # No statement and no order: Gate.io names this trade, was asked for
            # the order it belongs to, and no readable statement came back.
            # Nothing can book the execution honestly — the engine discards a
            # fill report whose venue order id it has never seen — so the
            # failure is raised for the calling pass to refuse. An unanswered
            # question is not the absence of the trade.
            raise FillReportsUnavailable(
                f"Gate.io did not deliver a readable statement of order "
                f"{first.venue_order_id!r} for recovered trade(s) "
                f"({', '.join(report.trade_id.value for report in unbooked)}); "
                f"{', '.join(f'{report.last_qty} at {report.last_px}' for report in unbooked)} "
                f"on {first.instrument_id} cannot be booked without it",
                unbooked,
            )

        # The grouped hand-over did not book them either — the re-read answered
        # with a status that short-circuits the trade loop, or the engine's
        # duplicate filter removed the order report and its trades with it. The
        # order itself is in the cache, so the single-report path can still
        # attach the executions. It is taken last and never silently: the order
        # may be left with a quantity it can no longer reach, and an execution
        # recorded against an order that stays open is a smaller loss than an
        # execution not recorded at all.
        for report in unbooked:
            self._log.error(
                f"Trade {report.trade_id.value} of {report.last_qty} at {report.last_px} on "
                f"{report.instrument_id} could only be booked without the venue's statement of "
                f"order {report.venue_order_id!r}; if Gate.io has since reduced that order it "
                f"will stay open in the cache until it is cancelled",
            )
            self._send_fill_report(report)

    def _handle_late_fill(
        self,
        product: GateioProductType,
        order: Order,
        instrument: Instrument,
        payload: dict[str, Any],
        trade_id: TradeId,
    ) -> None:
        """Book a fill that arrived after the order was reported closed.

        Gate.io does not order ``*.orders`` against ``*.usertrades``, so the
        terminal order message can win the race against the fill that caused it —
        the ordinary IOC/FOK/STP case, which ``order_status_from_gateio`` maps to
        CANCELED. ``Order.apply`` refuses an ``OrderFilled`` on a closed order, so
        the only remaining route is reconciliation, and it works from CANCELED:
        the platform's state table holds ``(CANCELED, PARTIALLY_FILLED)`` and
        ``(CANCELED, FILLED)`` for exactly this ("Real world possibility",
        model/orders/base.pyx:132-133).

        It works from CANCELED and from nowhere else. There is no transition out
        of EXPIRED, REJECTED or DENIED for a fill, so the engine's
        ``_generate_order_filled`` raises ``InvalidStateTrigger``,
        ``_reconcile_fill_report`` (live/execution_engine.py:3379-3383) catches it,
        logs it and returns False, and the execution is gone. Handing those to
        reconciliation anyway would bury a real trade under a generic
        reconciliation error, so the loss is reported here instead, with
        everything needed to account for it.
        """
        try:
            report = self._parse_fill_report(product, payload, instrument)
        except ValueError as e:
            self._log.error(
                f"Fill {trade_id.value} arrived after {order.client_order_id!r} was reported "
                f"{order.status_string()} and cannot be read: {e}. The execution stands at "
                f"the venue and is recovered by the next reconciliation pass",
            )
            return
        if report is None:
            self._log.error(
                f"Fill {trade_id.value} arrived after {order.client_order_id!r} was reported "
                f"{order.status_string()} and cannot be expressed as a fill report",
            )
            return
        if order.status not in FILLABLE_TERMINAL_STATUSES:
            self._log.error(
                f"Trade {trade_id.value} of {report.last_qty} at {report.last_px} "
                f"(commission {report.commission}) is lost: it arrived after "
                f"{order.client_order_id!r} was reported {order.status_string()}, and "
                f"NautilusTrader has no state transition applying a fill to an order in that "
                f"status. The position on {order.instrument_id} is short of this trade until "
                f"the venue's own position is reconciled",
            )
            return
        self._log.warning(
            f"Fill {trade_id.value} arrived after {order.client_order_id!r} was reported "
            f"{order.status_string()}; routing it through the reconciliation path",
        )
        self._hand_over_fill(report)

    def _fill_is_booked(self, report: FillReport) -> bool:
        """Return whether this venue trade is already on the order it belongs to."""
        order = self._order_of_report(report)
        return order is not None and report.trade_id in order.trade_ids

    def _credentials(self) -> tuple[str, str]:
        from nautilus_gateio.common.credentials import resolve_credentials

        return resolve_credentials(
            self._config.api_key,
            self._config.api_secret,
            testnet=self._config.is_testnet,
        )

    async def _poll_account_state(self, interval_secs: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval_secs)
                await self._update_account_state()
            except asyncio.CancelledError:
                self._log.debug("Canceled task 'poll_account_state'")
                return
            except Exception as e:  # noqa: BLE001 - the poll must survive venue errors
                self._log.error(f"Error polling account state: {e}")

    # -- helpers -----------------------------------------------------------

    def _instrument(self, instrument_id: InstrumentId) -> Instrument | None:
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            instrument = self._instrument_provider.find(instrument_id)
        return instrument

    async def _instrument_or_load(self, instrument_id: InstrumentId) -> Instrument | None:
        """Resolve an instrument, loading it from the venue if it is not held yet.

        Reconciliation must never drop venue state just because the provider was
        started with a narrower filter than the account actually traded: an
        unknown instrument is a reason to fetch its definition, not a reason to
        forget an open order, a fill or a position. When the definition cannot be
        fetched either, the loss is logged as an error rather than passing
        silently.
        """
        instrument = self._instrument(instrument_id)
        if instrument is not None:
            return instrument

        loader = getattr(self._instrument_provider, "load_async", None)
        if loader is not None:
            try:
                await loader(instrument_id)
            except Exception as e:  # noqa: BLE001 - report, never abort the batch
                self._log.error(f"Cannot load the instrument definition for {instrument_id}: {e}")
            instrument = self._instrument(instrument_id)
            if instrument is not None:
                self._cache.add_instrument(instrument)
                return instrument

        self._log.error(
            f"Cannot resolve instrument {instrument_id}: it is held neither by the cache nor by "
            f"the instrument provider, and the venue definition could not be loaded. Venue state "
            f"for this instrument cannot be reconciled",
        )
        return None

    def _resolve(self, instrument_id: InstrumentId) -> tuple[GateioProductType, str] | None:
        """Return ``(product, venue symbol)`` when the product is configured."""
        try:
            product, raw_symbol = instrument_id_to_gateio(instrument_id)
        except ValueError as e:
            self._log.error(f"Cannot route {instrument_id}: {e}")
            return None
        if product not in self._products:
            self._log.error(
                f"Cannot handle {instrument_id}: product {product.value} is not configured "
                f"(configured: {', '.join(p.value for p in self._products)})",
            )
            return None
        return product, raw_symbol

    def _futures_api(self, product: GateioProductType) -> GateioFuturesHttpAPI:
        return self._futures_http[product]

    @staticmethod
    def _currency(code: str | None, default: str = "USDT") -> Currency:
        return Currency.from_str(str(code or default).upper())

    def _venue_text(self, client_order_id: ClientOrderId) -> str:
        """Return the Gate.io ``text`` value for a Nautilus client order id.

        The Nautilus id is embedded verbatim when it fits Gate.io's charset and
        length limit, which makes the mapping recoverable from the venue after a
        restart. Otherwise a compliant id is generated and the alias is kept in
        memory for the lifetime of the order.
        """
        existing = self._text_by_client_order_id.get(client_order_id)
        if existing is not None:
            return existing

        value = client_order_id.value
        if _TEXT_BODY_PATTERN.match(value):
            text = CLIENT_ORDER_ID_PREFIX + value
        else:
            text = generate_client_order_id(self._config.client_order_id_tag)
            self._log.debug(
                f"Client order id {value!r} does not fit the Gate.io `text` field; "
                f"submitting as {text!r}",
            )
        self._register_text(client_order_id, text)
        return text

    def _register_text(self, client_order_id: ClientOrderId, text: str) -> None:
        self._text_by_client_order_id[client_order_id] = text
        self._client_order_id_by_text[text] = client_order_id

    def _client_order_id_from_text(self, text: Any) -> ClientOrderId | None:
        """Recover the Nautilus client order id carried in a venue ``text`` field.

        Values that do not start with ``t-`` are Gate.io's own order-source
        markers (``api``, ``web``, ``liquidation``, ...) and never client ids.
        A value this client generated because the Nautilus id did not fit the
        field is only resolvable through the in-memory alias table, so after a
        restart it is reported as an unknown (external) order rather than
        decoded into an id that was never issued by Nautilus.
        """
        if not text:
            return None
        value = str(text)
        if not value.startswith(CLIENT_ORDER_ID_PREFIX):
            return None

        known = self._client_order_id_by_text.get(value)
        if known is not None:
            return known

        candidate = ClientOrderId(value[len(CLIENT_ORDER_ID_PREFIX) :])
        if self._cache.strategy_id_for_order(candidate) is not None:
            self._register_text(candidate, value)
            return candidate
        if self._generated_text.match(value):
            return None
        # The venue echoed an id this client embedded; it survives a restart
        # even when the local cache no longer holds the order.
        return candidate

    def _client_order_id_for(
        self,
        text: Any,
        venue_order_id: VenueOrderId | None,
    ) -> ClientOrderId | None:
        """Resolve a client order id from the venue text, the trigger links, then the cache.

        The trigger links come before the cache index because a fired
        conditional order is a *new* venue object: on spot it carries no client
        id at all, and the cache still indexes the client order id against the
        armed id until the rebasing ``OrderUpdated`` has been applied.
        """
        client_order_id = self._client_order_id_from_text(text)
        if client_order_id is not None:
            return client_order_id
        if venue_order_id is not None:
            link = self._trigger_link_for_venue_order_id(venue_order_id.value)
            if link is not None:
                return link.client_order_id
            return self._cache.client_order_id(venue_order_id)
        return None

    # -- price-triggered order identity ------------------------------------

    @property
    def trigger_links(self) -> dict[ClientOrderId, GateioTriggerLink]:
        """Return the armed/fired identity map of the live price-triggered orders."""
        return dict(self._trigger_links)

    def _register_trigger_link(
        self,
        product: GateioProductType,
        armed_id: str,
        client_order_id: ClientOrderId,
        fired_id: str | None = None,
    ) -> GateioTriggerLink:
        """Record (or update) the armed/fired identity of a price-triggered order."""
        link = self._trigger_links.get(client_order_id)
        if link is None or link.armed_id != armed_id:
            link = GateioTriggerLink(product, armed_id, client_order_id)
            self._trigger_links[client_order_id] = link
            self._trigger_by_armed_id[armed_id] = link
        if fired_id:
            self._attach_fired_order_id(link, fired_id)
        return link

    def _attach_fired_order_id(self, link: GateioTriggerLink, fired_id: str) -> None:
        """Attach the id of the order the trigger created, keeping the armed id."""
        if link.fired_id == fired_id:
            return
        if link.fired_id is not None:
            self._trigger_by_fired_id.pop(link.fired_id, None)
        link.fired_id = fired_id
        self._trigger_by_fired_id[fired_id] = link
        self._log.info(
            f"Price-triggered order {link.client_order_id.value!r} fired: armed id "
            f"{link.armed_id} -> order id {fired_id}",
        )

    def _trigger_link_for_venue_order_id(self, value: str) -> GateioTriggerLink | None:
        """Return the link a venue order id belongs to, armed or fired."""
        return self._trigger_by_fired_id.get(value) or self._trigger_by_armed_id.get(value)

    def _forget_order(self, client_order_id: ClientOrderId) -> None:
        """Drop the per-order bookkeeping once the order can no longer change."""
        text = self._text_by_client_order_id.pop(client_order_id, None)
        if text is not None:
            self._client_order_id_by_text.pop(text, None)
        self._applied_trade_ids.pop(client_order_id, None)
        self._cash_buy_bounds.pop(client_order_id, None)
        link = self._trigger_links.pop(client_order_id, None)
        if link is not None:
            self._trigger_by_armed_id.pop(link.armed_id, None)
            self._trigger_resolution_attempts.discard(link.armed_id)
            if link.fired_id is not None:
                self._trigger_by_fired_id.pop(link.fired_id, None)
                self._trigger_resolution_attempts.discard(link.fired_id)

    async def _last_price(
        self,
        product: GateioProductType,
        raw_symbol: str,
        instrument_id: InstrumentId,
    ) -> Decimal | None:
        """Return the most recent traded price, from the cache or the venue."""
        trade = self._cache.trade_tick(instrument_id)
        if trade is not None:
            return trade.price.as_decimal()
        quote = self._cache.quote_tick(instrument_id)
        if quote is not None:
            return (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2

        try:
            if product.is_spot:
                tickers = await self._spot_http.tickers(raw_symbol)
                value = tickers[0].get("last") if tickers else None
            elif product.is_option:
                underlying, _, _, _ = parse_option_symbol(raw_symbol)
                tickers = await self._options_http.tickers(underlying)
                value = next(
                    (t.get("last_price") for t in tickers if t.get("name") == raw_symbol),
                    None,
                )
            else:
                tickers = await self._futures_api(product).tickers(raw_symbol)
                value = tickers[0].get("last") if tickers else None
        except GateioError as e:
            self._log.warning(f"Cannot read the last price for {instrument_id}: {e}")
            return None

        price = to_decimal(value)
        return price if price > 0 else None

    async def _reference_price(
        self,
        product: GateioProductType,
        raw_symbol: str,
        instrument_id: InstrumentId,
        side: OrderSide,
    ) -> Decimal | None:
        """Return the best available price to cross for an aggressive order."""
        quote = self._cache.quote_tick(instrument_id)
        if quote is not None:
            price = quote.ask_price if side == OrderSide.BUY else quote.bid_price
            if price.as_decimal() > 0:
                return price.as_decimal()
        return await self._last_price(product, raw_symbol, instrument_id)

    # -- order submission --------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        order: Order = command.order
        prepared = await self._prepare_submission(order)
        if prepared is None:
            return
        product, request = prepared
        await self._announce_and_send(order, product, request)

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        """Submit an order list, batching the parts Gate.io can batch.

        **A contingent list is refused, every leg of it.** Gate.io does carry
        attached take-profit / stop-loss (spot ``stop_profit``/``stop_loss``,
        futures ``tpsl_tp_trigger_price``/``tpsl_sl_trigger_price``), and it is
        tempting to map a Nautilus bracket onto it. Neither shape carries a
        client-supplied identifier for the attached leg — that is the whole field
        list of both request models, not a gap in the reading — so the entry, the
        stop-loss and the take-profit would reach the venue as **one** order with
        one id. Nautilus needs three addressable orders: the acceptance spec's
        own criterion for a bracket is "three orders created and accepted".
        Announcing ``OrderSubmitted`` for two legs that can never acquire a venue
        order id does not merely leave them untidy, it destroys them —
        ``LiveExecutionEngine._resolve_inflight_order`` turns a ``SUBMITTED``
        order that the venue cannot identify into an ``OrderRejected`` with
        reason ``UNKNOWN`` once ``inflight_check_retries`` is spent, so within
        seconds the strategy is told its stop-loss was rejected while Gate.io is
        holding it live against the position. That is a silent, money-losing
        divergence behind a request body that looks perfectly correct, which is
        why the refusal is here and not a TODO. (OKX can do the mapping because
        OKX accepts ``attach_algo_cl_ord_id``; that one field is the whole
        difference.)

        The refusal is deliberately gated on ``linked_order_ids`` and
        ``contingency_type`` rather than on ``OrderList.is_bracket()``:
        ``is_bracket()`` requires both children to be ``OUO``
        (``model/orders/list.pyx``), so a list built with
        ``OrderFactory.bracket(contingency_type=ContingencyType.OCO)`` is not a
        bracket by that test and would slip through into the batch path, where
        both exits go live and neither is ever cancelled.

        Contingent orders remain available against this venue through the
        platform's own emulator: give any leg an ``emulation_trigger`` and
        ``Strategy.submit_order_list`` routes the whole list to ``OrderEmulator``
        (``trading/strategy.pyx``; ``execution/messages.pyx`` computes
        ``has_emulated_order`` at construction), which holds the contingent legs
        locally and sends this client only the plain orders it releases. This
        coroutine never sees such a list.

        **A list with no contingency is a set of independent orders**, and every
        one of them is submitted. The batch endpoints are used where the venue
        has one and the group fits it; everything else goes out one order at a
        time down the same path :meth:`_submit_order` uses. Falling back rather
        than denying is deliberate: nothing about a group being too large, or
        about delivery futures having no batch endpoint, makes an *order*
        invalid, and denying an order this client can submit perfectly well would
        be inventing a venue restriction. Chunking an oversized group across
        several batch requests is the option not taken — a half-applied chunk is
        a second ambiguity class this client would then have to model, while N
        single submissions have exactly the ambiguity profile ``_submit_order``
        already handles.

        Doing nothing is not an option, which is what makes this method a
        correctness fix rather than a feature. The inherited coroutine raises
        ``NotImplementedError`` (``live/execution_client.py``), ``create_task``'s
        done-callback logs the traceback and drops it, and the orders the
        execution engine already cached stay at ``INITIALIZED`` forever:
        ``INITIALIZED`` is neither in-flight nor open, so neither
        ``_check_inflight_orders`` nor ``_handle_missing_orders_at_venue`` will
        ever look at them. No event, no terminal state, no reconciliation.
        """
        orders: list[Order] = list(command.order_list.orders)
        if not orders:
            self._log.warning("Order list is empty, nothing to submit")
            return

        if any(
            order.linked_order_ids or order.contingency_type != ContingencyType.NO_CONTINGENCY
            for order in orders
        ):
            reason = (
                "UNSUPPORTED_CONTINGENT_ORDER_LIST: Gate.io attaches take-profit and stop-loss "
                "to the parent order and returns no order id for either leg, so the legs of a "
                "contingent list cannot be addressed, amended or cancelled individually. "
                "Resubmit with an `emulation_trigger` on the contingent legs and NautilusTrader "
                "will hold them locally"
            )
            for order in orders:
                if order.is_closed:
                    continue
                self._deny(order, reason)
            return

        prepared: list[tuple[Order, GateioProductType, GateioOrderRequest]] = []
        for order in orders:
            # Every order is validated and built before *any* of them is sent, so
            # a refusal this client makes is still an `OrderDenied` decided
            # before a submission is announced, exactly as on the single path.
            submission = await self._prepare_submission(order)
            if submission is not None:
                prepared.append((order, submission[0], submission[1]))

        for product, group, as_batch in self._plan_order_list(prepared):
            if as_batch:
                await self._send_batch(product, group)
                continue
            order, request = group[0]
            await self._announce_and_send(order, product, request)

    async def _prepare_submission(
        self,
        order: Order,
    ) -> tuple[GateioProductType, GateioOrderRequest] | None:
        """Validate one order and build its Gate.io request, or refuse it here.

        Returns ``None`` when nothing will be sent for this order, in which case
        the outcome has already been published: an ``OrderDenied`` for anything
        this client refuses, or a warning for an order that is already closed and
        therefore needs no event at all.
        """
        if order.is_closed:
            self._log.warning(f"Order {order.client_order_id!r} is already closed, skipping")
            return None

        instrument = self._instrument(order.instrument_id)
        if instrument is None:
            self._deny(order, f"instrument {order.instrument_id} not found")
            return None

        resolved = self._resolve(order.instrument_id)
        if resolved is None:
            self._deny(order, f"{order.instrument_id} is not a configured Gate.io product")
            return None
        product, raw_symbol = resolved

        if order.order_type not in SUPPORTED_ORDER_TYPES:
            self._deny(
                order,
                f"{order_type_to_str(order.order_type)} orders are not supported by Gate.io",
            )
            return None

        # Everything this client refuses on its own is decided here, before any
        # event claims a request reached Gate.io. `OrderDenied` is the event the
        # platform defines for exactly that ("denied by Nautilus for being
        # invalid, unprocessable, or exceeding a risk limit",
        # concepts/orders/index.md; `INITIALIZED -> DENIED`,
        # concepts/events/order_denied.md), and `OrderRejected` is reserved for
        # the venue's own refusal ("rejected by the trading venue",
        # `SUBMITTED -> REJECTED`). The in-tree adapters draw the same line:
        # Kraken denies an unsupported time in force, FOK on a non-limit spot
        # order and reduce-only on a cash account before its own
        # `generate_order_submitted` (adapters/kraken/execution.py:899-940 vs
        # :951), and OKX denies market-on-options the same way.
        try:
            request = await self._prepare_order(order, instrument, product, raw_symbol)
        except (UnsupportedOrderError, OrderValidationError, ValueError) as e:
            self._deny(order, str(e))
            return None
        except Exception as e:  # noqa: BLE001 - a denial is the only honest outcome here
            # Nothing has been sent, so there is no in-flight order for the
            # engine to resolve and `_outcome_unresolved` would strand this one
            # in INITIALIZED for the life of the process: the in-flight check
            # only queries orders in SUBMITTED, PENDING_UPDATE or PENDING_CANCEL
            # (live/execution_engine.py `_check_inflight_orders`). Denying is
            # both terminal and true — the order never left this client.
            self._deny(order, f"the Gate.io request could not be built: {e}")
            return None

        return product, request

    async def _announce_and_send(
        self,
        order: Order,
        product: GateioProductType,
        request: GateioOrderRequest,
    ) -> None:
        """Announce one submission and send it, resolving whatever comes back."""
        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        try:
            await self._send_order(order, product, request)
        except GateioError as e:
            reason = f"{e.label or 'ERROR'}: {e.message}"
            if is_ambiguous_outcome(e):
                self._outcome_unresolved("Submission", order.client_order_id, reason)
                return
            self._reject(order, reason, due_post_only=e.label in POST_ONLY_LABELS)
        except Exception as e:  # noqa: BLE001 - never leave an order in limbo
            # Nothing this client refuses can reach here any more: the whole
            # request was built above. What is left happened around a request
            # Gate.io may already have accepted — a response this client could
            # not read, a task cancelled mid-call — including the `ValueError`
            # and `OrderValidationError` a malformed success payload can raise
            # while it is being parsed. Rejecting on that is unrecoverable, not
            # merely pessimistic; see `_outcome_unresolved`.
            self._outcome_unresolved("Submission", order.client_order_id, f"submit failed: {e}")

    def _plan_order_list(
        self,
        prepared: list[tuple[Order, GateioProductType, GateioOrderRequest]],
    ) -> list[tuple[GateioProductType, list[tuple[Order, GateioOrderRequest]], bool]]:
        """Decide which requests Gate.io will actually receive for a prepared list.

        Returns the sends in the caller's own order, each flagged as a batch or a
        single. A group of one is always sent as a single: the single-order
        endpoint answers with the order object itself, which is a stronger answer
        than a one-element batch result, and it keeps the trivial case off the
        batch parsing entirely.

        Price-triggered orders never join a batch. They address a different
        endpoint (``/*/price_orders``), and their reply is an armed-order id that
        :meth:`_send_trigger_order` has to register in the trigger link table —
        a batch result carries none of that.
        """
        groups: list[list[tuple[Order, GateioOrderRequest]]] = []
        products: list[GateioProductType] = []
        slot_by_product: dict[GateioProductType, int] = {}

        for order, product, request in prepared:
            if request.is_trigger or product not in BATCHABLE_PRODUCTS:
                products.append(product)
                groups.append([(order, request)])
                continue
            slot = slot_by_product.get(product)
            if slot is None:
                slot = len(groups)
                slot_by_product[product] = slot
                products.append(product)
                groups.append([])
            groups[slot].append((order, request))

        plan: list[tuple[GateioProductType, list[tuple[Order, GateioOrderRequest]], bool]] = []
        for product, group in zip(products, groups, strict=True):
            if len(group) > 1 and self._batch_fits(product, group):
                plan.append((product, group, True))
                continue
            plan.extend((product, [item], False) for item in group)
        return plan

    @staticmethod
    def _batch_fits(
        product: GateioProductType,
        group: list[tuple[Order, GateioOrderRequest]],
    ) -> bool:
        """Return whether one batch request can carry the whole group."""
        if not product.is_spot:
            return len(group) <= FUTURES_BATCH_MAX_ORDERS

        # Spot counts two limits, and the per-pair one is not the total: four
        # pairs of ten orders is a legal request, eleven orders on one pair is
        # not. The `account` limit ("all items must share one value") is met by
        # construction — `_build_spot_order` writes `self.spot_account`, which is
        # one configured value for the whole client.
        per_pair: dict[str, int] = {}
        for _, request in group:
            pair = str(request.body.get("currency_pair"))
            per_pair[pair] = per_pair.get(pair, 0) + 1
        return (
            len(per_pair) <= SPOT_BATCH_MAX_PAIRS
            and max(per_pair.values()) <= SPOT_BATCH_MAX_ORDERS_PER_PAIR
        )

    async def _send_batch(
        self,
        product: GateioProductType,
        group: list[tuple[Order, GateioOrderRequest]],
    ) -> None:
        """Send one batch-order request and resolve every order it carried."""
        bodies = [request.body for _, request in group]
        for order, _ in group:
            self.generate_order_submitted(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                ts_event=self._clock.timestamp_ns(),
            )

        try:
            if product.is_spot:
                results = await self._spot_http.create_batch_orders(bodies)
            else:
                results = await self._futures_api(product).create_batch_orders(bodies)
        except GateioError as e:
            # A whole-request failure carries no per-order result, so it speaks
            # for every order in the batch or for none of them. A refusal the
            # venue answered with proves nothing was placed; anything else leaves
            # the whole group in flight for the engine to resolve. Both batch
            # endpoints are `expiring=True` and are never replayed by the
            # transport, and that must stay true: a replay of a partially applied
            # batch doubles the orders that did succeed.
            reason = f"{e.label or 'ERROR'}: {e.message}"
            ambiguous = is_ambiguous_outcome(e)
            for order, _ in group:
                if ambiguous:
                    self._outcome_unresolved("Submission", order.client_order_id, reason)
                else:
                    self._reject(order, reason, due_post_only=e.label in POST_ONLY_LABELS)
            return
        except Exception as e:  # noqa: BLE001 - never leave an order in limbo
            for order, _ in group:
                self._outcome_unresolved(
                    "Submission",
                    order.client_order_id,
                    f"batch submit failed: {e}",
                )
            return

        self._apply_batch_results(product, group, results)

    def _apply_batch_results(
        self,
        product: GateioProductType,
        group: list[tuple[Order, GateioOrderRequest]],
        results: Any,
    ) -> None:
        """Fan a batch response back out over the orders that were sent.

        HTTP 200 does not mean the orders were accepted: both endpoints report
        per-item success in the body (``succeeded`` with ``label``/``message`` on
        spot, ``label``/``detail`` on futures), so a caller that reads only the
        status code publishes acceptances Gate.io never gave.

        Attribution goes by ``text`` first and by position second. Gate.io
        documents the results as index-aligned with the request, but ``text`` is
        the client order id *this client chose and sent*, so matching on it makes
        a misalignment harmless instead of catastrophic — under index-only
        matching one shifted row rejects an order the venue accepted and reports
        an accepted order as live. A row naming an order outside this batch, and
        any order the response never mentioned, are left in flight rather than
        guessed at.
        """
        rows = list(results) if isinstance(results, list) else []
        order_by_text: dict[str, Order] = {}
        for order, request in group:
            text = request.body.get("text")
            if isinstance(text, str) and text:
                order_by_text[text] = order

        aligned = len(rows) == len(group)
        if not aligned:
            # Position is no longer evidence of anything; only `text` is.
            self._log.error(
                f"Gate.io answered a batch of {len(group)} {product.value} orders with "
                f"{len(rows)} result(s); only results carrying a `text` can be attributed",
            )

        answered: set[ClientOrderId] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                self._log.error(f"Discarding an unreadable batch result at index {index}: {row!r}")
                continue
            order = self._batch_result_order(row, index, group, order_by_text, aligned)
            if order is None:
                continue
            answered.add(order.client_order_id)
            self._resolve_batch_result(product, order, row)

        for order, _ in group:
            if order.client_order_id not in answered:
                self._outcome_unresolved(
                    "Submission",
                    order.client_order_id,
                    "the batch response carried no result for it",
                )

    def _batch_result_order(
        self,
        row: dict[str, Any],
        index: int,
        group: list[tuple[Order, GateioOrderRequest]],
        order_by_text: dict[str, Order],
        aligned: bool,
    ) -> Order | None:
        """Return the order one batch result speaks for, or ``None`` if unknowable."""
        text = row.get("text")
        if isinstance(text, str) and text:
            matched = order_by_text.get(text)
            if matched is not None:
                return matched
            self._log.error(
                f"A batch result names {text!r}, which was not sent in this batch; it cannot be "
                f"attributed to any of these orders",
            )
            return None
        if not aligned or index >= len(group):
            self._log.error(
                f"A batch result at index {index} carries no `text` and the response is not "
                f"index-aligned, so it cannot be attributed to an order",
            )
            return None
        return group[index][0]

    def _resolve_batch_result(
        self,
        product: GateioProductType,
        order: Order,
        row: dict[str, Any],
    ) -> None:
        """Publish the outcome one batch result states for one order.

        A successful row is the venue's order object and goes through
        :meth:`_handle_order_payload`, the same handler the single-order ack and
        the private order stream use, so the three cannot drift.

        ``succeeded`` is read strictly. A missing field is *not* a failure: the
        only rows that mean "refused" are those the venue marked ``false`` or
        gave a failure ``label``. Reading an absent flag as a refusal would emit
        ``OrderRejected`` — a terminal event — for an order Gate.io is holding
        live, which no later event can undo.
        """
        succeeded = row.get("succeeded")
        label = row.get("label")
        if succeeded is False or (succeeded is not True and label):
            detail = row.get("message") or row.get("detail") or ""
            self._reject(
                order,
                f"{label or 'BATCH_ORDER_FAILED'}: {detail}",
                due_post_only=label in POST_ONLY_LABELS,
            )
            return

        if row.get("id_string") or row.get("id"):
            self._handle_order_payload(product, row)
            return

        self._outcome_unresolved(
            "Submission",
            order.client_order_id,
            "the batch result states neither an order id nor a failure",
        )

    async def _prepare_order(
        self,
        order: Order,
        instrument: Instrument,
        product: GateioProductType,
        raw_symbol: str,
    ) -> GateioOrderRequest:
        """Build the Gate.io request for ``order``, refusing what it cannot express.

        Every refusal this client makes on its own is raised from here, so the
        caller can turn all of them into one ``OrderDenied`` before a submission
        is announced. Nothing here mutates client state or sends an order; the
        only I/O is a *read* of the current price, which two encodings need (an
        aggressive spot buy has to be priced, and a trigger rule has to know
        which side of the market the trigger sits on).
        """
        # Both prices are checked here rather than in each builder: the grid is a
        # property of the instrument, and every order type that carries a price
        # reaches the venue through one of the two calls below.
        self._assert_on_tick_grid(instrument, "price", getattr(order, "price", None))
        self._assert_on_tick_grid(
            instrument,
            "trigger price",
            getattr(order, "trigger_price", None),
        )
        if order.order_type in CONDITIONAL_ORDER_TYPES:
            return await self._prepare_trigger_order(order, product, raw_symbol)
        return await self._prepare_regular_order(order, instrument, product, raw_symbol)

    async def _prepare_regular_order(
        self,
        order: Order,
        instrument: Instrument,
        product: GateioProductType,
        raw_symbol: str,
    ) -> GateioOrderRequest:
        if product.is_spot:
            body = await self._build_spot_order(order, instrument, raw_symbol)
        elif product.is_option:
            body = self._build_options_order(order, raw_symbol)
        else:
            body = self._build_futures_order(order, raw_symbol)
        return GateioOrderRequest(body)

    async def _send_order(
        self,
        order: Order,
        product: GateioProductType,
        request: GateioOrderRequest,
    ) -> None:
        """Send a built request and publish whatever the venue answers with."""
        if request.is_trigger:
            await self._send_trigger_order(order, product, request)
            return

        response: Any
        if product.is_spot:
            response = await self._spot_http.create_order(request.body)
        elif product.is_option:
            response = await self._options_http.create_order(request.body)
        else:
            response = await self._futures_api(product).create_order(request.body)

        if isinstance(response, dict):
            self._handle_order_payload(product, response)

    def _deny(self, order: Order, reason: str) -> None:
        """Refuse an order this client will not submit. Nothing was sent.

        This is one half of a boundary the platform draws and this client keeps.
        ``OrderDenied`` is "denied by Nautilus for being invalid, unprocessable,
        or exceeding a risk limit" (concepts/orders/index.md, "Order status
        definitions"), it transitions ``INITIALIZED -> DENIED``, and it carries
        neither a ``venue_order_id`` nor an ``account_id``
        (concepts/events/order_denied.md) — because no venue was involved. Its
        counterpart, :meth:`_reject`, means Gate.io refused a submission.

        The distinction is not bookkeeping. Emitting ``OrderSubmitted`` asserts
        a network fact, so a refusal announced that way puts the order through
        the engine's in-flight set for nothing, writes a submission Gate.io
        never received into the persisted event stream an audit reads back, and
        charges the venue's rejection rate for this client's own validation.

        The platform also makes the boundary one-way: the installed 1.230.0
        state table reaches ``DENIED`` from ``INITIALIZED`` and ``RELEASED``
        only, so once ``OrderSubmitted`` has been emitted a denial can no longer
        be applied at all. Everything this client refuses is therefore decided
        while the request is built, before the submission is announced — see
        :class:`GateioOrderRequest`.
        """
        self._log.error(f"Denying {order.client_order_id!r}: {reason}")
        self.generate_order_denied(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
        )

    def _reject(self, order: Order, reason: str, due_post_only: bool = False) -> None:
        """Report that **Gate.io** refused a submitted order.

        Reserved for a refusal the venue itself made and proved:
        ``OrderRejected`` is "rejected by the trading venue"
        (concepts/events/order_rejected.md, ``SUBMITTED -> REJECTED``). A
        refusal this client decided is :meth:`_deny`; an outcome nobody can
        prove either way is :meth:`_outcome_unresolved`.
        """
        self._log.error(f"Rejecting {order.client_order_id!r}: {reason}")
        self.generate_order_rejected(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
            due_post_only=due_post_only,
        )

    def _outcome_unresolved(
        self,
        action: str,
        client_order_id: ClientOrderId,
        reason: str,
    ) -> None:
        """Record a command whose venue outcome is unknown, emitting no event.

        This is the whole handling NautilusTrader prescribes for an ambiguous
        outcome: "Nautilus logs the failure, keeps the order in its current
        in-flight state, and waits for WebSocket updates, open-order polling,
        in-flight checks, or startup reconciliation to resolve the state"
        (concepts/live.md, "Ambiguous outcomes"). The order stays ``SUBMITTED``,
        ``PENDING_UPDATE`` or ``PENDING_CANCEL``, which is exactly what the
        engine's in-flight check looks for.

        Emitting a rejection instead is not a conservative approximation, it is
        unrecoverable. ``OrderRejected`` is terminal: verified against installed
        1.230.0, ``Order.apply`` then raises ``InvalidStateTrigger`` on the
        ``OrderAccepted`` that reconciliation would have to emit, so an order
        Gate.io is holding could never be represented locally again and the
        position would drift for the life of the process. ``OrderCancelRejected``
        and ``OrderModifyRejected`` are not terminal but are just as wrong: they
        put the order back to ``ACCEPTED`` carrying state the venue no longer
        has. Resolving those two is the engine's call, not this client's — and
        the two sources disagree about what it decides, so the installed one is
        what this comment describes: live.md's "In-flight order timeout
        resolution" table leaves ``PENDING_CANCEL`` and ``PENDING_UPDATE``
        unresolved, while 1.230.0's ``_resolve_inflight_order`` generates
        ``OrderCanceled`` for both once the retries are spent. Either way the
        engine decides it after querying the venue, which is more than this
        client knows at the point of failure.

        Querying the venue from here instead would duplicate
        ``LiveExecutionEngine._check_inflight_orders``, which already re-queries
        every in-flight order through ``generate_order_status_report``, bounded
        by ``inflight_check_retries`` and sharing its retry counter with the
        open-order loop. A second, uncoordinated poll would race it.
        """
        self._log.warning(
            f"{action} of {client_order_id!r} is unresolved: {reason}. The venue may or may "
            f"not have applied it, so the order is left in flight for the execution engine "
            f"to resolve",
        )

    async def _build_spot_order(
        self,
        order: Order,
        instrument: Instrument,
        raw_symbol: str,
    ) -> dict[str, Any]:
        """Translate a Nautilus order into a ``POST /spot/orders`` body."""
        if order.is_reduce_only:
            raise UnsupportedOrderError(
                "Gate.io spot orders have no reduce-only flag; reduce-only is a derivatives "
                "concept and the order would change meaning if it were dropped",
            )

        side = order_side_to_gateio(order.side)
        text = self._venue_text(order.client_order_id)
        body: dict[str, Any] = {
            "currency_pair": raw_symbol,
            "side": side,
            "account": self.spot_account,
            "text": text,
        }

        if order.order_type == OrderType.MARKET:
            if order.is_post_only:
                raise UnsupportedOrderError("a market order cannot be post-only")
            # Spot goes through the same mapping as futures, delivery and
            # options rather than deciding its own. The shortcut this replaced
            # ("FOK stays FOK, everything else becomes ioc") silently accepted
            # AT_THE_OPEN and AT_THE_CLOSE, which the other three products
            # reject: one client answering the same instruction two ways is a
            # trap for a strategy that switches products, and a session
            # instruction on a 24/7 venue is a porting mistake worth reporting
            # rather than absorbing.
            tif = self._market_time_in_force(order, allow_fok=True)
            if order.side == OrderSide.SELL:
                if order.is_quote_quantity:
                    raise UnsupportedOrderError(
                        "Gate.io spot market sells take a base quantity; resubmit without "
                        "`quote_quantity=True`",
                    )
                body["type"] = "market"
                body["amount"] = str(order.quantity)
                body["time_in_force"] = tif.value
                return body

            if order.is_quote_quantity:
                # Gate.io's native market buy spends a quote amount.
                body["type"] = "market"
                body["amount"] = str(order.quantity)
                body["time_in_force"] = tif.value
                return body

            return await self._build_aggressive_spot_buy(order, instrument, raw_symbol, body, tif)

        if order.is_quote_quantity:
            raise UnsupportedOrderError(
                "Gate.io only accepts a quote-denominated quantity on a spot market buy",
            )

        price = getattr(order, "price", None)
        if price is None:
            raise UnsupportedOrderError(
                f"{order_type_to_str(order.order_type)} orders require a price",
            )

        body["type"] = "limit"
        body["amount"] = str(order.quantity)
        body["price"] = str(price)
        body["time_in_force"] = self._time_in_force(order, allow_fok=True).value

        display_qty = self._display_quantity(order, whole_contracts=False)
        if display_qty is not None:
            body["iceberg"] = str(display_qty)
        return body

    async def _build_aggressive_spot_buy(
        self,
        order: Order,
        instrument: Instrument,
        raw_symbol: str,
        body: dict[str, Any],
        tif: GateioTimeInForce,
    ) -> dict[str, Any]:
        """Express a base-denominated spot market buy as an aggressive limit.

        Gate.io's native spot market buy spends a **quote** amount, so it cannot
        express "buy exactly this many base units". Rather than convert the
        quantity behind the caller's back, the order is sent as a limit order
        priced through the book by the pair's own published slippage cap; the
        venue then fills at or better than that bound.

        The substitution is in the *price*, and it stops there. The time in
        force is carried through unchanged, which is why it is a parameter
        rather than a constant: a spot limit order takes ``fok`` as readily as
        ``ioc``, so a ``MARKET``/``FOK`` buy stays all-or-nothing at the bound
        instead of being downgraded to "fill whatever is available" — a
        different execution guarantee, and the one thing FOK exists to rule out.
        """
        reference = await self._reference_price(
            GateioProductType.SPOT,
            raw_symbol,
            order.instrument_id,
            OrderSide.BUY,
        )
        if reference is None:
            raise OrderValidationError(
                f"cannot price a base-denominated spot market buy on {raw_symbol}: no quote, "
                f"trade or ticker price is available. Resubmit with `quote_quantity=True` to "
                f"use Gate.io's native quote-denominated market buy",
            )

        info = getattr(instrument, "info", None) or {}
        slippage = to_decimal(info.get("slippage"))
        if slippage <= 0:
            slippage = DEFAULT_SPOT_SLIPPAGE
        limit_price = instrument.make_price(reference * (Decimal(1) + slippage))

        body["type"] = "limit"
        body["amount"] = str(order.quantity)
        body["price"] = str(limit_price)
        body["time_in_force"] = tif.value
        self._log.info(
            f"Base-denominated spot market buy on {raw_symbol} sent as a {tif.value} limit at "
            f"{limit_price} (reference {reference}, slippage cap {slippage})",
        )
        return body

    def _build_futures_order(self, order: Order, raw_symbol: str) -> dict[str, Any]:
        """Translate a Nautilus order into a futures/delivery order body."""
        size = self._contract_size(order)
        body: dict[str, Any] = {
            "contract": raw_symbol,
            "size": size,
            "text": self._venue_text(order.client_order_id),
        }
        if order.is_reduce_only:
            body["reduce_only"] = True

        if order.order_type == OrderType.MARKET:
            if order.is_post_only:
                raise UnsupportedOrderError("a market order cannot be post-only")
            body["price"] = "0"
            body["tif"] = self._market_time_in_force(order, allow_fok=True).value
            return body

        price = getattr(order, "price", None)
        if price is None:
            raise UnsupportedOrderError(
                f"{order_type_to_str(order.order_type)} orders require a price",
            )
        body["price"] = str(price)
        body["tif"] = self._time_in_force(order, allow_fok=True).value

        display_qty = self._display_quantity(order, whole_contracts=True)
        if display_qty is not None:
            body["iceberg"] = int(display_qty.as_decimal())
        return body

    def _build_options_order(self, order: Order, raw_symbol: str) -> dict[str, Any]:
        """Translate a Nautilus order into a ``POST /options/orders`` body."""
        size = self._contract_size(order)
        body: dict[str, Any] = {
            "contract": raw_symbol,
            "size": size,
            "text": self._venue_text(order.client_order_id),
        }
        if order.is_reduce_only:
            body["reduce_only"] = True

        if order.order_type == OrderType.MARKET:
            if order.is_post_only:
                raise UnsupportedOrderError("a market order cannot be post-only")
            body["price"] = "0"
            body["tif"] = self._market_time_in_force(order, allow_fok=False).value
            return body

        price = getattr(order, "price", None)
        if price is None:
            raise UnsupportedOrderError(
                f"{order_type_to_str(order.order_type)} orders require a price",
            )
        body["price"] = str(price)
        body["tif"] = self._time_in_force(order, allow_fok=False).value

        display_qty = self._display_quantity(order, whole_contracts=True)
        if display_qty is not None:
            body["iceberg"] = int(display_qty.as_decimal())
        return body

    @staticmethod
    def _display_quantity(order: Order, whole_contracts: bool) -> Quantity | None:
        """Return the order's ``display_qty``, refusing what ``iceberg`` cannot carry.

        Zero is not a no-op here, it is the *opposite* instruction on each side.
        NautilusTrader reads ``display_qty=0`` as a fully hidden order
        (concepts/orders/index.md, "Display quantity"), while Gate.io documents
        ``iceberg`` as "Null or 0 for normal orders. Hiding all amount is not
        supported" — so passing the zero through would rest the whole size
        visibly on the book. There is no other encoding for a hidden order on
        this venue, so the order is refused rather than inverted.

        The same inversion is what makes ``int()`` the wrong way to fit a display
        quantity into a contract count: ``int(Decimal("0.5"))`` is ``0``, which
        the venue again reads as "display everything". A display quantity that is
        not a whole number of contracts is therefore refused too, exactly as
        :meth:`_contract_size` refuses a fractional order quantity.
        """
        display_qty = getattr(order, "display_qty", None)
        if display_qty is None:
            return None

        value = display_qty.as_decimal()
        if value == 0:
            raise UnsupportedOrderError(
                "Gate.io reads `iceberg=0` as a normal (fully displayed) order and does not "
                "support hiding the whole amount, so `display_qty=0` cannot be submitted; "
                "omit `display_qty`, or name the portion to show",
            )
        if whole_contracts and value != int(value):
            raise OrderValidationError(
                f"Gate.io displays whole contracts on a derivatives iceberg order, "
                f"`display_qty` was {value}",
            )
        return display_qty

    def _contract_size(self, order: Order) -> int:
        """Return the signed contract count for a derivatives order."""
        if order.is_quote_quantity:
            raise UnsupportedOrderError(
                "contract quantities are counts of contracts; `quote_quantity=True` has no "
                "meaning on Gate.io futures, delivery or options orders",
            )
        quantity = order.quantity.as_decimal()
        contracts = int(quantity)
        if Decimal(contracts) != quantity:
            raise OrderValidationError(
                f"Gate.io contract quantities are whole contracts, was {quantity}",
            )
        if contracts <= 0:
            raise OrderValidationError(f"order quantity must be positive, was {quantity}")
        return contracts if order.side == OrderSide.BUY else -contracts

    @staticmethod
    def _assert_on_tick_grid(instrument: Instrument, label: str, price: Price | None) -> None:
        """Refuse a price that is not on the instrument's tick grid.

        On Gate.io the grid is not implied by the precision: ``BNB_USDT``
        perpetuals and the longer-dated ``ETH_USDT`` delivery contracts quote two
        decimals but tick in ``0.05``, so four of every five two-decimal prices
        are invalid at the venue. Nothing upstream catches that —
        ``Instrument.make_price`` rounds to the precision only
        (model/instruments/base.pyx:565) and the ``RiskEngine`` compares
        precisions, not increments (risk/engine.pyx:1041) — so a price built the
        documented way reaches this client off-grid and comes back as an opaque
        venue parameter error.

        Snapping it here would be worse than refusing: moving a limit price by up
        to a tick is a different order from the one the strategy sent, and this
        client's rule is that nothing is silently altered. Instruments carry a
        tick scheme for this (``next_bid_price`` / ``next_ask_price``), which the
        message points at. For the ~3,100 instruments whose tick *is* a power of
        ten, the check can never fire, since any price of the right precision is
        already a multiple of the increment.
        """
        if price is None:
            return
        increment = instrument.price_increment.as_decimal()
        if price.as_decimal() % increment != 0:
            raise OrderValidationError(
                f"{label} {price} is not a multiple of the {instrument.id} tick size "
                f"{increment}, and Gate.io accepts on-tick prices only. `make_price()` rounds "
                f"to the price precision, not to the tick; use the instrument's tick scheme "
                f"(`next_bid_price()` / `next_ask_price()`) to price on the grid",
            )

    @staticmethod
    def _market_time_in_force(order: Order, allow_fok: bool) -> GateioTimeInForce:
        """Time in force for a native market order.

        Gate.io only accepts ``ioc`` (and, on futures and delivery, ``fok``) for
        ``type=market``. GTC and DAY carry no meaning for an order that cannot
        rest, so they map to ``ioc``; ``FOK`` is honoured where the venue
        supports it and rejected where it does not, rather than being quietly
        downgraded into a different execution guarantee.
        """
        tif = order.time_in_force
        if tif == TimeInForce.FOK:
            if not allow_fok:
                raise UnsupportedOrderError(
                    "Gate.io options market orders support immediate-or-cancel only; "
                    "fill-or-kill is not available on this product",
                )
            return GateioTimeInForce.FOK
        if tif in (TimeInForce.IOC, TimeInForce.GTC, TimeInForce.DAY):
            return GateioTimeInForce.IOC
        raise UnsupportedOrderError(
            f"time in force {time_in_force_to_str(tif)} cannot be applied to a "
            f"Gate.io market order (supported: IOC, FOK on futures and delivery)",
        )

    @staticmethod
    def _time_in_force(order: Order, allow_fok: bool) -> GateioTimeInForce:
        """Map the order's time in force, rejecting anything Gate.io cannot express."""
        if not allow_fok and order.time_in_force == TimeInForce.FOK:
            raise UnsupportedOrderError(
                "Gate.io options orders accept gtc, ioc and poc only; there is no fill-or-kill",
            )
        try:
            return time_in_force_to_gateio(order.time_in_force, post_only=order.is_post_only)
        except ValueError as e:
            # The mapping's own message is passed through rather than wrapped:
            # it distinguishes an unsupported time in force from a post-only
            # combination that Gate.io cannot express, and re-prefixing it with
            # "time in force IOC is not supported" would misreport the second as
            # the first (IOC on its own is supported).
            raise UnsupportedOrderError(str(e)) from e

    # -- price-triggered orders --------------------------------------------

    async def _prepare_trigger_order(
        self,
        order: Order,
        product: GateioProductType,
        raw_symbol: str,
    ) -> GateioOrderRequest:
        if product.is_option:
            raise UnsupportedOrderError(
                "Gate.io options have no price-triggered order endpoint; "
                f"{order_type_to_str(order.order_type)} cannot be submitted for options",
            )

        trigger_price = getattr(order, "trigger_price", None)
        if trigger_price is None:
            raise UnsupportedOrderError(
                f"{order_type_to_str(order.order_type)} orders require a trigger price",
            )

        last_price = await self._last_price(product, raw_symbol, order.instrument_id)
        rule = trigger_rule(
            order.side,
            order.order_type,
            trigger_price.as_decimal(),
            last_price,
        )

        if product.is_spot:
            body = self._build_spot_price_order(order, raw_symbol, trigger_price, rule)
        else:
            body = self._build_futures_price_order(order, raw_symbol, trigger_price, rule)
        return GateioOrderRequest(
            body,
            is_trigger=True,
            trigger_price=trigger_price,
            trigger_rule=rule,
        )

    async def _send_trigger_order(
        self,
        order: Order,
        product: GateioProductType,
        request: GateioOrderRequest,
    ) -> None:
        if product.is_spot:
            response = await self._spot_http.create_price_order(request.body)
        else:
            response = await self._futures_api(product).create_price_order(request.body)

        trigger_id = self._trigger_order_id(response)
        if trigger_id is None:
            # The venue answered without an error, so it armed the order and
            # this client merely cannot read back the handle. That is a
            # post-submit parse failure, which the platform classifies as an
            # ambiguous outcome rather than a rejection: the order exists, and
            # only reconciliation can recover its id.
            raise GateioRequestAmbiguousError(
                0,
                "TRIGGER_ORDER_ID_MISSING",
                "the venue accepted the price-triggered order without returning its id",
            )

        self._register_trigger_link(product, trigger_id, order.client_order_id)
        self.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId(trigger_id),
            ts_event=self._clock.timestamp_ns(),
        )
        self._log.info(
            f"Armed price-triggered order {order.client_order_id!r} as {trigger_id} "
            f"(rule {request.trigger_rule}, trigger {request.trigger_price})",
        )

    @staticmethod
    def _trigger_order_id(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        value = response.get("id_string") or response.get("id")
        return str(value) if value not in (None, "") else None

    def _build_spot_price_order(
        self,
        order: Order,
        raw_symbol: str,
        trigger_price: Price,
        rule: int,
    ) -> dict[str, Any]:
        account = PRICE_ORDER_ACCOUNTS.get(self._spot_mode)
        if account is None:
            # The modes are sorted by their venue value, not as enum members:
            # `GateioSpotAccountMode` defines no ordering, so `sorted()` over the
            # keys raises `TypeError` while building this very message and the
            # refusal never reaches the strategy as a refusal.
            supported = ", ".join(sorted(mode.value for mode in PRICE_ORDER_ACCOUNTS))
            raise UnsupportedOrderError(
                f"Gate.io price-triggered spot orders accept the {supported} ledgers only, "
                f"configured mode is {self._spot_mode.value}",
            )
        if order.is_quote_quantity:
            raise UnsupportedOrderError(
                "the quantity of a price-triggered spot order is always base-denominated",
            )
        if order.is_reduce_only:
            raise UnsupportedOrderError(
                "Gate.io spot orders have no reduce-only flag; reduce-only is a derivatives "
                "concept and the price-triggered order would change meaning if it were dropped",
            )
        if order.trigger_type not in SPOT_TRIGGER_TYPES:
            # The trigger type is the instruction that says *which* price arms
            # the order, so dropping it does not lose a decoration, it arms the
            # order against a different price than the one that was named — and
            # usually against the one the caller chose the trigger type to
            # avoid, since MARK_PRICE and MID_POINT are picked precisely to be
            # immune to a thin-book last-trade wick. Gate.io's spot trigger has
            # no field to carry it, so it is refused here exactly as the futures
            # path refuses a price type it cannot encode.
            raise UnsupportedOrderError(
                f"trigger type {trigger_type_to_str(order.trigger_type)} cannot be expressed on "
                f"a Gate.io spot conditional order: the spot price-order endpoint takes a bare "
                f"comparison against the market price and has no price-type field, so the order "
                f"would silently arm on a different price. Use DEFAULT or LAST_PRICE; for a mark "
                f"or index trigger, trade the futures contract, whose price-order endpoint does "
                f"carry a price type",
            )
        self._assert_trigger_execution_flags(order)

        is_limit = order.order_type in (OrderType.STOP_LIMIT, OrderType.LIMIT_IF_TOUCHED)
        price = getattr(order, "price", None)
        if is_limit and price is None:
            raise UnsupportedOrderError(
                f"{order_type_to_str(order.order_type)} orders require a limit price",
            )

        put: dict[str, Any] = {
            "type": "limit" if is_limit else "market",
            "side": order_side_to_gateio(order.side),
            "amount": str(order.quantity),
            "account": account,
            "time_in_force": self._trigger_time_in_force(order, is_limit=is_limit).value,
            "price": str(price) if is_limit else str(trigger_price),
        }
        trigger: dict[str, Any] = {
            "price": str(trigger_price),
            "rule": SPOT_TRIGGER_RULES[rule],
        }
        expiration = self._trigger_expiration_secs(order)
        if expiration is not None:
            trigger["expiration"] = expiration
        return {"trigger": trigger, "put": put, "market": raw_symbol}

    def _build_futures_price_order(
        self,
        order: Order,
        raw_symbol: str,
        trigger_price: Price,
        rule: int,
    ) -> dict[str, Any]:
        price_type = FUTURES_TRIGGER_PRICE_TYPES.get(order.trigger_type)
        if price_type is None:
            raise UnsupportedOrderError(
                f"trigger type {trigger_type_to_str(order.trigger_type)} is not supported by "
                f"Gate.io; use LAST_PRICE, MARK_PRICE or INDEX_PRICE",
            )

        self._assert_trigger_execution_flags(order)

        is_limit = order.order_type in (OrderType.STOP_LIMIT, OrderType.LIMIT_IF_TOUCHED)
        price = getattr(order, "price", None)
        if is_limit and price is None:
            raise UnsupportedOrderError(
                f"{order_type_to_str(order.order_type)} orders require a limit price",
            )

        initial: dict[str, Any] = {
            "contract": raw_symbol,
            "size": self._contract_size(order),
            # A zero price with `ioc` is the venue's encoding of a market order.
            "price": str(price) if is_limit else "0",
            "tif": self._trigger_time_in_force(order, is_limit=is_limit).value,
            "text": self._venue_text(order.client_order_id),
        }
        if order.is_reduce_only:
            initial["reduce_only"] = True

        trigger: dict[str, Any] = {
            "strategy_type": 0,
            "price_type": price_type,
            "price": str(trigger_price),
            "rule": rule,
        }
        expiration = self._trigger_expiration_secs(order)
        if expiration is not None:
            trigger["expiration"] = expiration
        # `order_type` is omitted deliberately: its four submittable values all
        # describe closing an existing position, which is not what a plain
        # conditional entry does in one-way mode.
        return {"initial": initial, "trigger": trigger}

    @staticmethod
    def _assert_trigger_execution_flags(order: Order) -> None:
        """Reject execution flags Gate.io's price-triggered endpoints cannot express.

        The fired order Gate.io creates accepts neither a post-only constraint
        (``put``/``initial`` take ``gtc`` and ``ioc`` only) nor an iceberg
        display quantity. The regular order paths reject exactly these cases
        explicitly, so the conditional paths must too: dropping the flag would
        submit a materially different order from the one that was requested.
        """
        if order.is_post_only:
            raise UnsupportedOrderError(
                "Gate.io price-triggered orders accept gtc and ioc on the fired order only; "
                "post-only (poc) is not available, so the order cannot be submitted without "
                "losing its maker-only constraint",
            )
        if getattr(order, "display_qty", None) is not None:
            raise UnsupportedOrderError(
                "Gate.io price-triggered orders have no iceberg field; `display_qty` cannot be "
                "honoured on a conditional order",
            )

    @staticmethod
    def _trigger_time_in_force(order: Order, is_limit: bool) -> GateioTimeInForce:
        """Map the time in force of a conditional order onto the fired order.

        On a price-triggered order the Nautilus time in force describes how long
        the *trigger* stays armed, and Gate.io expresses that with
        ``trigger.expiration`` rather than with the fired order's own validity.
        GTC and GTD therefore leave the fired order at the venue's only sensible
        default (``gtc`` for a limit, ``ioc`` for a market), while IOC asks for
        an immediate-or-cancel fired order. FOK and the session-scoped values
        have no representation at all and are rejected rather than downgraded.
        """
        tif = order.time_in_force
        if tif == TimeInForce.IOC:
            return GateioTimeInForce.IOC
        if tif in (TimeInForce.GTC, TimeInForce.GTD):
            return GateioTimeInForce.GTC if is_limit else GateioTimeInForce.IOC
        raise UnsupportedOrderError(
            f"time in force {time_in_force_to_str(tif)} cannot be applied to a Gate.io "
            f"price-triggered order (supported: GTC, GTD via the trigger expiration, and IOC)",
        )

    def _trigger_expiration_secs(self, order: Order) -> int | None:
        """Return the trigger validity window in seconds, if the order names one."""
        expire_time_ns = getattr(order, "expire_time_ns", 0)
        if not expire_time_ns:
            return None
        remaining_ns = int(expire_time_ns) - self._clock.timestamp_ns()
        if remaining_ns <= 0:
            raise OrderValidationError("the order's expire time is already in the past")
        return max(1, remaining_ns // 1_000_000_000)

    # -- cancels -----------------------------------------------------------

    async def _cancel_order(self, command: CancelOrder) -> None:
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            self._cancel_rejected(command, "instrument is not a configured Gate.io product")
            return
        product, raw_symbol = resolved

        try:
            await self._cancel_one(product, raw_symbol, command.client_order_id, command)
        except GateioError as e:
            reason = f"{e.label or 'ERROR'}: {e.message}"
            if is_ambiguous_outcome(e):
                # The cancel endpoints are idempotent and replayed, so the usual
                # shape of this failure is a cancel Gate.io did apply and could
                # not report back. OrderCancelRejected would put the order back
                # to ACCEPTED, and a strategy that re-quotes on that event would
                # replace an order that no longer exists - double exposure.
                self._outcome_unresolved("Cancellation", command.client_order_id, reason)
                return
            if str(e.label or "").upper() in CANCEL_ALREADY_DONE_LABELS:
                await self._resolve_cancel_of_a_vanished_order(
                    product,
                    raw_symbol,
                    command,
                    reason,
                )
                return
            self._cancel_rejected(command, reason)
        except Exception as e:  # noqa: BLE001 - report, never propagate
            if is_ambiguous_outcome(e):
                self._outcome_unresolved(
                    "Cancellation",
                    command.client_order_id,
                    f"cancel failed: {e}",
                )
                return
            self._cancel_rejected(command, f"cancel failed: {e}")

    async def _resolve_cancel_of_a_vanished_order(
        self,
        product: GateioProductType,
        raw_symbol: str,
        command: CancelOrder,
        reason: str,
    ) -> None:
        """Resolve a cancel Gate.io answered with "there is no such live order".

        That answer is not a refusal, and reporting it as one misstates money:
        ``Order.apply(OrderCancelRejected)`` reverts the order to its previous
        status (model/orders/base.pyx:1065-1067), so the order stands open here
        while the venue holds nothing — and a strategy that re-quotes on the
        rejection replaces an order that no longer exists.

        The classification in :func:`is_ambiguous_outcome` is by exception type,
        because only the transport knows whether a request reached the venue.
        This is a label-level exception to it, and it is one for a reason the
        type cannot express: the cancel endpoints are idempotent and this
        client's transport replays them, so a 4xx naming a missing order is the
        expected answer to a cancellation that *worked*, not evidence of one
        that was refused.

        What the venue holds is a question with an answer, so it is asked
        rather than inferred: the order is re-read and its own statement goes
        through the same translation as every other order frame. Only when the
        re-read cannot answer at all is the outcome taken from the label, and
        then it is ``OrderCanceled`` — the venue said it holds no live order,
        ``PARTIALLY_FILLED -> CANCELED`` preserves the filled quantity, and a
        fill still in flight still reaches the order through
        ``_handle_late_fill``. This is the resolution the platform itself
        reaches for the same situation (``_resolve_order_not_found_at_venue``,
        live/execution_engine.py), arrived at here without waiting for a check
        that is not scheduled by default.
        """
        order = self._cache.order(command.client_order_id)
        venue_order_id = (order.venue_order_id if order is not None else None) or (
            self._cache.venue_order_id(command.client_order_id)
        )

        if venue_order_id is not None:
            try:
                payload = await self._get_order(product, venue_order_id.value, raw_symbol)
            except Exception as e:  # noqa: BLE001 - the label is the fallback, not a raise
                self._log.warning(
                    f"Gate.io answered the cancellation of {command.client_order_id!r} with "
                    f"{reason}, and the order could not be re-read ({e}); reporting the "
                    f"termination the label states",
                )
            else:
                if isinstance(payload, dict) and payload:
                    self._log.info(
                        f"Gate.io answered the cancellation of {command.client_order_id!r} with "
                        f"{reason}, which is not a refusal; the order's own statement decides",
                    )
                    self._handle_order_payload(product, payload)
                    return
                self._log.warning(
                    f"Gate.io answered the cancellation of {command.client_order_id!r} with "
                    f"{reason}, and the re-read returned no order object; reporting the "
                    f"termination the label states",
                )

        if order is not None and order.is_closed:
            return
        self.generate_order_canceled(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        self._forget_order(command.client_order_id)

    async def _cancel_one(
        self,
        product: GateioProductType,
        raw_symbol: str,
        client_order_id: ClientOrderId,
        command: CancelOrder,
    ) -> None:
        link = self._trigger_links.get(client_order_id)
        if link is not None and link.is_armed:
            # Still armed: only the auto-order id can disarm it.
            await self._cancel_armed_order(link)
            self.generate_order_canceled(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=VenueOrderId(link.armed_id),
                ts_event=self._clock.timestamp_ns(),
            )
            self._forget_order(client_order_id)
            return

        venue_order_id = command.venue_order_id or self._cache.venue_order_id(client_order_id)
        if link is not None and link.fired_id is not None:
            # The trigger fired: the armed id no longer identifies anything
            # cancellable, the order Gate.io created does.
            venue_order_id = VenueOrderId(link.fired_id)
        if venue_order_id is None:
            raise OrderValidationError(
                f"no venue order id known for {client_order_id!r}; Gate.io accepts a client "
                f"order id only while the order is resting",
            )

        response: Any
        if product.is_spot:
            response = await self._spot_http.cancel_order(
                venue_order_id.value,
                raw_symbol,
                account=self.spot_account,
            )
        elif product.is_option:
            response = await self._options_http.cancel_order(venue_order_id.value)
        else:
            response = await self._futures_api(product).cancel_order(venue_order_id.value)

        if isinstance(response, dict):
            self._handle_order_payload(product, response)

    def _cancel_rejected(self, command: CancelOrder, reason: str) -> None:
        self._log.error(f"Cannot cancel {command.client_order_id!r}: {reason}")
        self.generate_order_cancel_rejected(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
        )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            # A cancel-all that fails a local check produces no event — the
            # platform is explicit that "cancel, modify, cancel-all, and
            # batch-cancel commands that fail local checks log warnings and do
            # not produce rejection events" (concepts/live.md) — but it must not
            # produce silence either. Without this line a strategy waiting for
            # its cancels waits forever with nothing in the log to explain it.
            self._log.warning(
                f"Cannot cancel all orders on {command.instrument_id}: the instrument is not a "
                f"configured Gate.io product, so no cancel was sent and no order was affected",
            )
            return
        product, raw_symbol = resolved

        try:
            responses = await self._cancel_all_for(product, raw_symbol, command.order_side)
        except GateioError as e:
            self._log.error(f"Cannot cancel all orders on {command.instrument_id}: {e}")
            return

        for payload in responses:
            if isinstance(payload, dict):
                self._handle_order_payload(product, payload)

    async def _cancel_all_for(
        self,
        product: GateioProductType,
        raw_symbol: str,
        order_side: OrderSide,
    ) -> list[Any]:
        """Cancel the resting and armed orders a ``CancelAllOrders`` command covers.

        Neither Gate.io price-order endpoint accepts a side filter, so a bulk
        disarm would cancel both sides of the book whenever the command names
        one. A side-scoped command therefore disarms the matching price orders
        individually, by id, and leaves the other side alone.
        """
        responses: list[Any] = []
        side_scoped = order_side != OrderSide.NO_ORDER_SIDE

        if product.is_spot:
            side: str | None = order_side_to_gateio(order_side) if side_scoped else None
            responses += list(
                await self._spot_http.cancel_all(
                    raw_symbol,
                    side=side,
                    account=self.spot_account,
                )
                or [],
            )
            account = PRICE_ORDER_ACCOUNTS.get(self._spot_mode)
            if account is None:
                return responses
            if side_scoped:
                await self._cancel_armed_orders_by_side(product, raw_symbol, order_side)
            else:
                canceled = await self._spot_http.cancel_price_orders(
                    market=raw_symbol,
                    account=account,
                )
                self._handle_price_order_cancels(product, canceled)
        elif product.is_option:
            responses += list(await self._options_http.cancel_all(contract=raw_symbol) or [])
        else:
            # Gate.io futures name the book side, not the order side.
            book_side: str | None = None
            if order_side == OrderSide.BUY:
                book_side = "bid"
            elif order_side == OrderSide.SELL:
                book_side = "ask"
            api = self._futures_api(product)
            responses += list(await api.cancel_all(raw_symbol, side=book_side) or [])
            if side_scoped:
                await self._cancel_armed_orders_by_side(product, raw_symbol, order_side)
            else:
                canceled = await api.cancel_price_orders(contract=raw_symbol)
                self._handle_price_order_cancels(product, canceled)
        return responses

    async def _cancel_armed_order(self, link: GateioTriggerLink) -> Any:
        """Disarm one price-triggered order through its product's endpoint."""
        if link.product.is_spot:
            return await self._spot_http.cancel_price_order(link.armed_id)
        return await self._futures_api(link.product).cancel_price_order(link.armed_id)

    async def _cancel_armed_orders_by_side(
        self,
        product: GateioProductType,
        raw_symbol: str,
        order_side: OrderSide,
    ) -> None:
        """Disarm the armed price orders on one side of one instrument, by id."""
        instrument_id = gateio_to_instrument_id(product, raw_symbol)
        for link in list(self._trigger_links.values()):
            if link.product is not product or not link.is_armed:
                continue
            order = self._cache.order(link.client_order_id)
            if order is None or order.instrument_id != instrument_id or order.side != order_side:
                continue
            try:
                await self._cancel_armed_order(link)
            except GateioError as e:
                self._log.error(
                    f"Cannot disarm price-triggered order {link.client_order_id.value!r} "
                    f"({link.armed_id}): {e}",
                )
                continue
            self.generate_order_canceled(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=link.client_order_id,
                venue_order_id=VenueOrderId(link.armed_id),
                ts_event=self._clock.timestamp_ns(),
            )
            self._forget_order(link.client_order_id)

    def _handle_price_order_cancels(self, product: GateioProductType, payloads: Any) -> None:
        """Report the orders a bulk price-order cancel disarmed.

        The venue answers with price-order objects, which are not regular orders
        and must not go through the order-payload path; the ones this client
        armed are closed explicitly instead of being left resting locally.
        """
        for payload in payloads or []:
            if not isinstance(payload, dict):
                continue
            armed_id = payload.get("id_string") or payload.get("id")
            if armed_id in (None, ""):
                continue
            link = self._trigger_by_armed_id.get(str(armed_id))
            if link is None or link.product is not product:
                continue
            order = self._cache.order(link.client_order_id)
            if order is None or order.is_closed:
                self._forget_order(link.client_order_id)
                continue
            self.generate_order_canceled(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=link.client_order_id,
                venue_order_id=VenueOrderId(link.armed_id),
                ts_event=self._clock.timestamp_ns(),
            )
            self._forget_order(link.client_order_id)

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        cancels: list[CancelOrder] = list(command.cancels)
        if not cancels:
            return

        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            for cancel in cancels:
                self._cancel_rejected(cancel, "instrument is not a configured Gate.io product")
            return
        product, raw_symbol = resolved

        if not product.is_spot:
            # Only the spot API has a batch cancel; the others cancel one by one.
            for cancel in cancels:
                await self._cancel_order(cancel)
            return

        def _is_armed(cancel: CancelOrder) -> bool:
            link = self._trigger_links.get(cancel.client_order_id)
            return link is not None and link.is_armed

        direct = [c for c in cancels if not _is_armed(c)]
        for cancel in cancels:
            if _is_armed(cancel):
                await self._cancel_order(cancel)

        for chunk_start in range(0, len(direct), SPOT_CANCEL_BATCH_SIZE):
            chunk = direct[chunk_start : chunk_start + SPOT_CANCEL_BATCH_SIZE]
            items: list[dict[str, Any]] = []
            by_venue_id: dict[str, CancelOrder] = {}
            for cancel in chunk:
                venue_order_id = cancel.venue_order_id or self._cache.venue_order_id(
                    cancel.client_order_id,
                )
                if venue_order_id is None:
                    self._cancel_rejected(
                        cancel,
                        f"no venue order id known for {cancel.client_order_id!r}",
                    )
                    continue
                items.append(
                    {
                        "currency_pair": raw_symbol,
                        "id": venue_order_id.value,
                        "account": self.spot_account,
                    },
                )
                by_venue_id[venue_order_id.value] = cancel
            if not items:
                continue

            try:
                results = await self._spot_http.cancel_batch(items)
            except GateioError as e:
                # A whole-request failure carries no per-order result, so it says
                # nothing about the individual cancels unless the venue refused
                # the request outright (live.md: "A whole-request failure without
                # per-order results remains unresolved unless the target command
                # is proven refused"). Fanning a timeout or a 502 out as one
                # OrderCancelRejected per order is the single-cancel defect
                # multiplied by the batch size.
                reason = f"{e.label or 'ERROR'}: {e.message}"
                ambiguous = is_ambiguous_outcome(e)
                for cancel in by_venue_id.values():
                    if ambiguous:
                        self._outcome_unresolved("Cancellation", cancel.client_order_id, reason)
                    else:
                        self._cancel_rejected(cancel, reason)
                continue

            for result in results or []:
                if not isinstance(result, dict):
                    continue
                cancel = by_venue_id.get(str(result.get("id")))
                if result.get("succeeded"):
                    continue
                reason = f"{result.get('label', 'CANCEL_FAILED')}: {result.get('message', '')}"
                if cancel is not None:
                    self._cancel_rejected(cancel, reason)
                else:
                    self._log.error(f"Batch cancel failed for {result.get('id')}: {reason}")

    # -- modification ------------------------------------------------------

    async def _modify_order(self, command: ModifyOrder) -> None:
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            self._modify_rejected(command, "instrument is not a configured Gate.io product")
            return
        product, raw_symbol = resolved

        link = self._trigger_links.get(command.client_order_id)
        if link is not None and link.is_armed:
            self._modify_rejected(
                command,
                "Gate.io price-triggered orders cannot be amended through this client; "
                "cancel the order and submit a new one",
            )
            return
        if product.is_option:
            self._modify_rejected(
                command,
                "Gate.io options orders cannot be amended; cancel and resubmit instead",
            )
            return
        if product.is_delivery:
            self._modify_rejected(
                command,
                "Gate.io delivery futures orders cannot be amended; cancel and resubmit instead",
            )
            return
        if command.quantity is None and command.price is None:
            self._modify_rejected(command, "either a new quantity or a new price is required")
            return
        if command.trigger_price is not None:
            self._modify_rejected(
                command,
                "Gate.io cannot amend the trigger price of a working order",
            )
            return

        # An amended price must sit on the venue's grid exactly as a submitted
        # one does, and perpetuals are the one amendable product that includes an
        # off-decimal grid (`BNB_USDT` ticks in 0.05).
        instrument = self._instrument(command.instrument_id)
        if instrument is not None:
            try:
                self._assert_on_tick_grid(instrument, "price", command.price)
            except OrderValidationError as e:
                self._modify_rejected(command, str(e))
                return

        venue_order_id = command.venue_order_id or self._cache.venue_order_id(
            command.client_order_id,
        )
        if link is not None and link.fired_id is not None:
            venue_order_id = VenueOrderId(link.fired_id)
        if venue_order_id is None:
            self._modify_rejected(
                command,
                f"no venue order id known for {command.client_order_id!r}",
            )
            return

        contract_size: int | None = None
        if not product.is_spot and command.quantity is not None:
            order = self._cache.order(command.client_order_id)
            if order is None:
                # The side decides the sign of `size`, and Gate.io reads the sign
                # as the direction of the amended order. Guessing it would let an
                # amend flip a short into a long.
                self._modify_rejected(
                    command,
                    f"cannot amend {command.client_order_id!r}: the order is not in the cache, "
                    f"so its side is unknown and the signed contract size cannot be built",
                )
                return
            quantity = command.quantity.as_decimal()
            contracts = int(quantity)
            if Decimal(contracts) != quantity:
                self._modify_rejected(
                    command,
                    f"Gate.io contract quantities are whole contracts, was {quantity}",
                )
                return
            if contracts <= 0:
                self._modify_rejected(
                    command,
                    f"amended order quantity must be positive, was {quantity}",
                )
                return
            contract_size = contracts if order.side == OrderSide.BUY else -contracts

        try:
            if product.is_spot:
                body: dict[str, Any] = {}
                if command.quantity is not None:
                    body["amount"] = str(command.quantity)
                if command.price is not None:
                    body["price"] = str(command.price)
                response = await self._spot_http.amend_order(
                    venue_order_id.value,
                    raw_symbol,
                    body,
                )
            else:
                body = {}
                if contract_size is not None:
                    body["size"] = contract_size
                if command.price is not None:
                    body["price"] = str(command.price)
                response = await self._futures_api(product).amend_order(
                    venue_order_id.value,
                    body,
                )
        except UnsupportedOrderError as e:
            self._modify_rejected(command, str(e))
            return
        except GateioError as e:
            reason = f"{e.label or 'ERROR'}: {e.message}"
            if is_ambiguous_outcome(e):
                # Silently the worst of the three. OrderModifyRejected returns
                # the order to ACCEPTED with the OLD price and quantity while
                # the venue may be holding the new ones, and in 1.230.0 nothing
                # repairs that: the open-order check sets `should_reconcile`
                # only on an is_open or filled_qty mismatch, so `_should_update`
                # - the one comparison that looks at price and quantity - is
                # never reached from that loop, and the loop is off by default.
                # Left in PENDING_UPDATE, the in-flight check queries the order
                # and that same comparison does run.
                self._outcome_unresolved("Amendment", command.client_order_id, reason)
                return
            self._modify_rejected(command, reason)
            return
        except Exception as e:  # noqa: BLE001 - report, never propagate
            if is_ambiguous_outcome(e):
                self._outcome_unresolved(
                    "Amendment",
                    command.client_order_id,
                    f"amend failed: {e}",
                )
                return
            self._modify_rejected(command, f"amend failed: {e}")
            return

        if isinstance(response, dict):
            self._handle_order_payload(product, response)

    def _modify_rejected(self, command: ModifyOrder, reason: str) -> None:
        self._log.error(f"Cannot modify {command.client_order_id!r}: {reason}")
        self.generate_order_modify_rejected(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
        )

    # -- WebSocket handling ------------------------------------------------

    def _handle_ws_message(self, product: GateioProductType, message: dict[str, Any]) -> None:
        """Route one private WebSocket message to its handler."""
        if message.get("event") not in (None, "update", "all"):
            return  # Acknowledgements are consumed by the transport

        channel = str(message.get("channel", ""))
        result = message.get("result")
        if result is None:
            return
        payloads = result if isinstance(result, list) else [result]
        kind = channel.rpartition(".")[2]

        # Anchor for the reconnect lookback window: everything up to here has
        # been delivered, so a reconnect only has to re-query from this point.
        self._last_stream_event_ns[product] = (
            timestamp_to_nanos(message.get("time_ms") or message.get("time"))
            or self._clock.timestamp_ns()
        )

        try:
            for payload in payloads:
                if not isinstance(payload, dict):
                    continue
                if kind == "orders":
                    self._handle_order_payload(product, payload)
                elif kind == "usertrades":
                    self._handle_fill_payload(product, payload)
                elif kind == "balances":
                    self._handle_balance_payload(product, payload)
                elif kind == "positions":
                    self._handle_position_payload(product, payload)
                else:
                    self._log.debug(f"Unhandled private channel {channel}")
        except Exception as e:  # noqa: BLE001 - a bad payload must not kill the stream
            self._log.exception(f"Error handling {channel} message", e)

    def _handle_order_payload(self, product: GateioProductType, payload: dict[str, Any]) -> None:
        """Translate one Gate.io order object into Nautilus order events.

        Used for both private WebSocket order updates and the order objects
        returned by the REST submit, cancel and amend calls, so the two paths
        cannot drift. Fills are never generated here; they come from the trade
        stream, where the venue trade id is available.
        """
        raw_symbol = venue_symbol_of(payload)
        if not raw_symbol:
            self._log.debug("Discarding an order payload without a symbol")
            return
        instrument_id = gateio_to_instrument_id(product, raw_symbol)
        instrument = self._instrument(instrument_id)

        venue_order_id_value = payload.get("id_string") or payload.get("id")
        if venue_order_id_value in (None, ""):
            self._log.debug(f"Discarding an order payload for {raw_symbol} without an id")
            return
        venue_order_id = VenueOrderId(str(venue_order_id_value))

        client_order_id = self._client_order_id_for(payload.get("text"), venue_order_id)
        order = self._cache.order(client_order_id) if client_order_id is not None else None
        if order is None:
            if self._schedule_fired_order_resolution(product, raw_symbol, venue_order_id):
                return
            try:
                report = self._parse_order_status_report(product, payload, instrument)
            except ValueError as e:
                self._log.error(
                    f"Dropping an order frame for {venue_order_id!r} on {raw_symbol}: {e}. "
                    f"The order's state is recovered by reconciliation",
                )
                return
            if report is not None:
                self._send_order_status_report(report)
            return

        try:
            status = self._order_status(product, payload)
        except ValueError as e:
            # An unreadable deciding field on a live frame or an ack: acting
            # on a guessed status can close an order the venue holds open.
            # The order keeps its last known state — for an unacknowledged
            # submit that is SUBMITTED, which the engine's inflight check
            # resolves through the single-order query.
            self._log.error(
                f"Dropping an order frame for {order.client_order_id!r}: {e}. The order "
                f"keeps its last known state until the next frame or reconciliation",
            )
            return
        ts_event = (
            first_timestamp_ns(
                payload,
                "update_time_ms",
                "finish_time_ms",
                "update_time",
                "finish_time",
                "create_time_ms",
                "create_time",
                "time_ms",
                "time",
            )
            or self._clock.timestamp_ns()
        )
        finish_as = str(payload.get("finish_as") or "").lower()

        self._maybe_swap_trigger_venue_order_id(order, venue_order_id, ts_event)

        if status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED):
            if order.status == OrderStatus.SUBMITTED:
                self.generate_order_accepted(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=venue_order_id,
                    ts_event=ts_event,
                )
            else:
                self._maybe_generate_order_updated(
                    product,
                    order,
                    payload,
                    instrument,
                    venue_order_id,
                    ts_event,
                )
            return

        if finish_as == POST_ONLY_FINISH_AS:
            if order.status in REJECTABLE_ORDER_STATUSES:
                self.generate_order_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    reason="post-only order would have taken liquidity (finish_as=poc)",
                    ts_event=ts_event,
                    due_post_only=True,
                )
            elif not order.is_closed:
                # The venue says this post-only order finished without resting,
                # yet a fill has already been booked against it here — the shape
                # a REST response for a cancel or an amend takes when it arrives
                # after the trade stream. The platform has no
                # `PARTIALLY_FILLED -> REJECTED` transition, so a rejection would
                # raise `InvalidStateTrigger` inside the execution engine and the
                # order would stay open locally while it is finished at Gate.io.
                # What both sides agree on is that it is finished, and
                # `PARTIALLY_FILLED -> CANCELED` is legal, so the termination is
                # reported as the cancellation it is.
                self._log.warning(
                    f"{order.client_order_id!r} is {order.status_string()} here but Gate.io "
                    f"reports it finished as post-only; reporting the termination as a "
                    f"cancellation, which is the transition the platform accepts",
                )
                self.generate_order_canceled(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=venue_order_id,
                    ts_event=ts_event,
                )
            self._forget_order(order.client_order_id)
            return

        if product.is_spot and is_cash_buy_payload(payload):
            # The venue has finished a spot cash buy. Every branch below decides
            # an order's terminal state from a quantity this client holds, and
            # for this order that quantity is only a bound built from the fills
            # so far (`_raise_cash_buy_bound`) — the venue states the base total
            # here and nowhere else, so the close is taken from it.
            self._close_finished_cash_buy(order, payload, instrument, venue_order_id, ts_event)
            return

        if status == OrderStatus.EXPIRED:
            if not order.is_closed:
                self.generate_order_expired(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=venue_order_id,
                    ts_event=ts_event,
                )
            self._forget_order(order.client_order_id)
            return

        if status == OrderStatus.CANCELED:
            if not order.is_closed:
                self.generate_order_canceled(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=venue_order_id,
                    ts_event=ts_event,
                )
            self._forget_order(order.client_order_id)
            return

        if status == OrderStatus.FILLED:
            # The closing event is the fill itself, which arrives on the trade
            # stream with the venue trade id required for de-duplication.
            self._log.debug(f"{order.client_order_id!r} reported filled by the order stream")
            return

        self._log.debug(f"Unhandled order status {status} for {order.client_order_id!r}")

    def _maybe_swap_trigger_venue_order_id(
        self,
        order: Order,
        venue_order_id: VenueOrderId,
        ts_event: int,
    ) -> None:
        """Follow a price-triggered order onto the real order the venue fired.

        Gate.io arms a price-triggered order under one id and, when the trigger
        fires, creates a *new* order with a different id. Rebasing that id has
        to go through ``OrderUpdated``: it is the only event
        ``Order.apply`` accepts with a venue order id different from the one
        already on the order, so emitting anything else first would make every
        subsequent event for this order (including its fills) be rejected.

        ``OrderTriggered`` is emitted afterwards, and only for the order types
        NautilusTrader considers triggerable; for stop-market style orders the
        rebasing update is the whole transition.

        The armed id is **kept**, not discarded: it remains the only handle that
        identifies this order in the venue's price-order listings, which is what
        makes the identity rebuildable after a restart.

        A conditional order is the common case, not the only one: any event this
        client resolved to ``order`` — through the ``text`` alias it registered
        for it, or through the cache's own index — while carrying a venue order
        id the order does not hold is the venue speaking about a replacement
        object it created. Rebasing is the only way to take delivery of it. The
        tempting alternative, handing such an event to reconciliation, cannot
        work on any path: ``create_order_filled_event``
        (live/reconciliation.py:381) stamps the fill with
        ``report.venue_order_id``, so ``Order.apply`` raises the same
        ``ValueError`` there, and ``_reconcile_fill_report`` logs it away.
        """
        if order.venue_order_id is not None and order.venue_order_id == venue_order_id:
            return  # Already rebased onto this order
        if self._cache.venue_order_id(order.client_order_id) == venue_order_id:
            # The rebase was already emitted. `order.venue_order_id` may still
            # read as the previous id here, because the order object is only
            # updated once the execution engine applies the `OrderUpdated` — and
            # several fills for a freshly fired order can be handled before that
            # happens. The cache mapping is written synchronously by the rebase
            # itself, so it is the one signal that does not depend on event
            # delivery.
            return
        link = self._trigger_links.get(order.client_order_id)
        if link is not None and venue_order_id.value == link.armed_id:
            return  # The event carries the armed id the order already holds
        if link is None and order.venue_order_id is None:
            # Nothing to rebase: `Order.apply` adopts the id an event carries
            # when the order holds none, which the framework permits.
            return
        if link is not None:
            self._attach_fired_order_id(link, venue_order_id.value)
        else:
            self._log.warning(
                f"{order.client_order_id!r} holds venue order id {order.venue_order_id}, but "
                f"Gate.io reports it under {venue_order_id.value}; rebasing onto the id the "
                f"venue is using, since every later event and cancel is addressed by it",
            )
        self._cache.add_venue_order_id(
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            overwrite=True,
        )
        # Rebase the venue order id first (OrderUpdated is the only event allowed
        # to carry a different venue order id).
        self.generate_order_updated(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            quantity=order.quantity,
            price=getattr(order, "price", None),
            trigger_price=getattr(order, "trigger_price", None),
            ts_event=ts_event,
            venue_order_id_modified=True,
        )
        if link is not None and order.order_type in TRIGGERABLE_ORDER_TYPES:
            self.generate_order_triggered(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                ts_event=ts_event,
            )

    def _schedule_fired_order_resolution(
        self,
        product: GateioProductType,
        raw_symbol: str,
        venue_order_id: VenueOrderId,
    ) -> bool:
        """Try to attribute an unknown venue order id to an armed conditional order.

        A spot price order carries no client id (``put.text`` is an order-source
        marker), so the order Gate.io creates when the trigger fires arrives on
        the stream as a completely unrecognisable object. Rather than report it
        as an external order — which would strand the Nautilus order that is in
        fact its parent — the armed orders on the same instrument are re-read
        from the venue and matched on their fired order id.

        Returns whether a resolution attempt was scheduled, in which case the
        caller must not fall back to the external-order report path.
        """
        if venue_order_id.value in self._trigger_resolution_attempts:
            return False
        instrument_id = gateio_to_instrument_id(product, raw_symbol)
        candidates = [
            link
            for link in self._trigger_links.values()
            if link.product is product
            and link.is_armed
            and self._link_instrument_id(link) in (None, instrument_id)
        ]
        if not candidates:
            return False
        self._trigger_resolution_attempts.add(venue_order_id.value)
        self.create_task(
            self._resolve_fired_order(product, venue_order_id, candidates),
            log_msg="resolve_fired_trigger_order",
        )
        return True

    def _link_instrument_id(self, link: GateioTriggerLink) -> InstrumentId | None:
        order = self._cache.order(link.client_order_id)
        return order.instrument_id if order is not None else None

    async def _resolve_fired_order(
        self,
        product: GateioProductType,
        venue_order_id: VenueOrderId,
        candidates: list[GateioTriggerLink],
    ) -> None:
        """Re-read armed price orders and bind the one that fired ``venue_order_id``."""
        for link in candidates:
            try:
                if product.is_spot:
                    payload = await self._spot_http.get_price_order(link.armed_id)
                else:
                    payload = await self._futures_api(product).get_price_order(link.armed_id)
            except GateioError as e:
                self._log.warning(
                    f"Cannot re-read the armed price order {link.armed_id}: {e}",
                )
                continue
            if not isinstance(payload, dict):
                continue
            fired_id = _fired_order_id(payload)
            if fired_id != venue_order_id.value:
                continue

            self._attach_fired_order_id(link, fired_id)
            order = self._cache.order(link.client_order_id)
            if order is None:
                return
            self._maybe_swap_trigger_venue_order_id(
                order,
                venue_order_id,
                self._clock.timestamp_ns(),
            )
            return

        self._log.warning(
            f"Order {venue_order_id.value} on {product.value} matches no armed price order of "
            f"this client; treating it as an external order",
        )

    def _maybe_generate_order_updated(
        self,
        product: GateioProductType,
        order: Order,
        payload: dict[str, Any],
        instrument: Instrument | None,
        venue_order_id: VenueOrderId,
        ts_event: int,
    ) -> None:
        """Emit ``OrderUpdated`` when the venue reports an amended order.

        The payload's quantity is the venue's own order size, which is what a
        Gate.io amend restates and what another session's amend restates too —
        nothing in the payload distinguishes the two, and nothing should. It is
        passed on unaltered, spot included: a fill states the quantity the venue
        matched, gross of any fee withheld from it
        (:meth:`_fill_quantity_and_commission`), so the order's own quantity has
        to stay on that footing or its fills could never add up to it.
        """
        if order.order_type == OrderType.MARKET:
            # A market order has no resting price or quantity to amend on
            # Gate.io, and its payload quantity is denominated differently
            # depending on side, so there is nothing meaningful to restate.
            return
        if instrument is None or order.status not in (
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_UPDATE,
        ):
            return

        try:
            quantity = self._payload_quantity(order.instrument_id, payload, instrument)
            price_value = payload.get("price")
            price = (
                instrument.make_price(to_exact_decimal(price_value))
                if price_value not in (None, "", "0")
                else getattr(order, "price", None)
            )
        except ValueError as e:
            # Restating a live order from a value this client cannot read
            # would put a quantity or price the venue never stated on it
            # (REC-06); skipping the restatement keeps the venue's last known
            # statement, which reconciliation re-reads.
            self._log.warning(
                f"Not restating {order.client_order_id!r} from an unreadable frame: {e}",
            )
            return
        if quantity is None:
            return

        order_price = getattr(order, "price", None)
        if quantity == order.quantity and (price is None or price == order_price):
            return

        self.generate_order_updated(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            quantity=quantity,
            price=price,
            trigger_price=getattr(order, "trigger_price", None),
            ts_event=ts_event,
            is_quote_quantity=False if product.is_spot else None,
        )

    @staticmethod
    def _payload_quantity(
        instrument_id: InstrumentId,
        payload: dict[str, Any],
        instrument: Instrument,
    ) -> Quantity | None:
        """Return the order quantity a payload reports, in Nautilus units.

        Careful with spot market BUY orders: Gate.io denominates their ``amount``
        in the QUOTE currency, so it is a cash amount rather than a quantity.
        The base-denominated figure for those orders is ``filled_amount``; until
        something has filled there is no base quantity to report at all.
        """
        if "size" in payload:
            value = payload["size"]
            if value in (None, ""):
                return None  # No statement, so nothing to restate from
            size = abs(to_lot_count(value))
            return Quantity(size, instrument.size_precision) if size > 0 else None

        if is_cash_buy_payload(payload):
            value = payload.get("filled_amount")
            if value in (None, ""):
                return None
            filled_base = to_exact_decimal(value)
            return instrument.make_qty(filled_base) if filled_base > 0 else None

        value = payload.get("amount")
        if value in (None, ""):
            return None
        amount = to_exact_decimal(value)
        if amount <= 0:
            return None
        return instrument.make_qty(amount)

    @staticmethod
    def _order_status(product: GateioProductType, payload: dict[str, Any]) -> OrderStatus:
        """Map one Gate.io order object onto a Nautilus order status.

        The quantities read here decide the order's open/closed state
        (completion is ``filled >= amount``), so they are read strictly with
        one distinction (REC-06): a field the payload does not state makes no
        claim — the status falls back to the venue's own words (state and
        ``finish_as``) with no quantity asserted — while a field the payload
        states and this client cannot read raises. The old readers defaulted
        both cases to 0, and a defaulted ``left`` is not 0 of anything: it is
        a confident claim that everything filled, which reported a
        venue-canceled order FILLED 10 of 10 (CZ-3). Shared by the REST report
        path (which has already validated these fields strictly) and the live
        stream/ack path, where a raise drops the one frame loudly and leaves
        the order state to the next frame or to reconciliation.
        """
        if product.is_spot:
            status = payload.get("status")
            if status is None:
                # The spot stream discriminates with `event`, not `status`.
                event = str(payload.get("event") or "").lower()
                status = "open" if event in ("put", "update") else "finished"
            what = f"spot order {payload.get('id')!r}"
            amount = float(optional_exact_decimal(payload, "amount", what=what))
            if is_cash_buy_payload(payload):
                # `amount` on a spot cash buy is quote cash, so the comparable
                # filled figure is `filled_total` (quote) — NOT `filled_amount`,
                # which is the base bought. Comparing those two mixes
                # denominations, and the answer then depends on the price of the
                # pair rather than on the order: on a cheap pair the base number
                # is the larger one and a half-spent order reads FILLED, on an
                # expensive pair the same order reads CANCELED. Gate.io's own
                # documentation warns about this pairing. `left` below is quote
                # cash for this order too, so the fallback stays in one unit.
                filled = float(optional_exact_decimal(payload, "filled_total", what=what))
            else:
                filled = float(optional_exact_decimal(payload, "filled_amount", what=what))
            if not filled and payload.get("left") not in (None, "") and "amount" in payload:
                left = float(optional_exact_decimal(payload, "left", what=what))
                filled = max(0.0, amount - left)
        else:
            what = f"{payload.get('contract') or 'contract'} order {payload.get('id')!r}"
            reason = GateioFinishAs.parse(payload.get("finish_as"))
            status = payload.get("status")
            if status is None:
                # An absent state leaves the reason as the only statement: a
                # terminal reason means the order is finished, anything else —
                # including the open marker the stream uses — means it rests.
                # The old default read every stateless payload as open, which
                # reported a finished order live (REC-06).
                status = "open" if reason is GateioFinishAs.UNKNOWN else "finished"

            def lots(key: str) -> float:
                try:
                    return float(abs(to_lot_count(payload.get(key))))
                except ValueError as e:
                    raise ValueError(
                        f"the {what}'s '{key}' field decides the order's state: {e}",
                    ) from None

            has_quantities = payload.get("size") not in (None, "") and payload.get("left") not in (
                None,
                "",
            )
            amount = lots("size") if payload.get("size") not in (None, "") else 0.0
            # `filled` is only computable when both quantities are stated:
            # subtracting an unstated remainder from a stated size is the
            # confident-full-fill defect, not a smaller claim.
            filled = max(0.0, amount - lots("left")) if has_quantities else 0.0
        return order_status_from_gateio(
            str(status),
            payload.get("finish_as"),
            filled=filled,
            amount=amount,
        )

    def _handle_fill_payload(self, product: GateioProductType, payload: dict[str, Any]) -> None:
        """Translate one Gate.io fill into an ``OrderFilled`` event."""
        raw_symbol = venue_symbol_of(payload)
        if not raw_symbol:
            return
        instrument_id = gateio_to_instrument_id(product, raw_symbol)
        instrument = self._instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Cannot process a fill on {instrument_id}: instrument not found")
            return

        trade_id_value = payload.get("id")
        if trade_id_value in (None, ""):
            self._log.error(f"Discarding a fill on {instrument_id} without a venue trade id")
            return
        trade_id = TradeId(str(trade_id_value))

        order_id_value = payload.get("order_id") or payload.get("order")
        venue_order_id = (
            VenueOrderId(str(order_id_value)) if order_id_value not in (None, "") else None
        )
        client_order_id = self._client_order_id_for(payload.get("text"), venue_order_id)
        order = self._cache.order(client_order_id) if client_order_id is not None else None
        if order is None or venue_order_id is None:
            if venue_order_id is not None and self._schedule_fired_order_resolution(
                product,
                raw_symbol,
                venue_order_id,
            ):
                # The fill is re-delivered by the reconnect/reconciliation query
                # once the identity is known; reporting it now would attribute it
                # to an external order.
                return
            try:
                report = self._parse_fill_report(product, payload, instrument)
            except ValueError as e:
                self._log.error(
                    f"Dropping an external fill frame on {instrument_id}: {e}. The "
                    f"execution stands at the venue and is recovered by the next "
                    f"reconciliation pass",
                )
                return
            if report is not None:
                self._hand_over_fill(report)
            return

        applied = self._applied_trade_ids.setdefault(order.client_order_id, set())
        if trade_id.value in applied or trade_id in order.trade_ids:
            self._log.debug(
                f"Ignoring duplicate fill {trade_id.value} for {order.client_order_id!r}",
            )
            return

        if order.is_closed:
            # `_forget_order` already dropped this order's applied-trade set when
            # the terminal message arrived; re-seeding it deliberately keeps a
            # replay of the same late fill from being reported a second time.
            applied.add(trade_id.value)
            self._handle_late_fill(product, order, instrument, payload, trade_id)
            return

        try:
            last_px = instrument.make_price(
                exact_decimal_field(
                    payload,
                    "price",
                    what=f"{product.value} fill {trade_id.value}",
                ),
            )
            last_qty, commission = self._fill_quantity_and_commission(
                product,
                payload,
                instrument,
            )
        except ValueError as e:
            # A deciding field the venue stated and this client cannot read: a
            # booked guess would misstate money, so the frame is dropped
            # loudly. The execution stands at the venue, and every recovery
            # path (reconnect, restart, position check) re-reads the trade
            # listing, where an unreadable row fails the listing rather than
            # vanishing (REC-06).
            self._log.error(
                f"Dropping the stream fill {trade_id.value} for {order.client_order_id!r}: "
                f"{e}. The execution stands at the venue and is recovered by the next "
                f"reconciliation pass",
            )
            return
        if last_qty.as_decimal() <= 0:
            self._log.warning(f"Ignoring a zero-quantity fill {trade_id.value}")
            return

        ts_event = (
            first_timestamp_ns(
                payload,
                "create_time_ms",
                "create_time",
                "time_ms",
                "time",
            )
            or self._clock.timestamp_ns()
        )

        # Gate.io orders `*.orders` and `*.usertrades` independently, so this
        # fill can be the first message that mentions a venue-created
        # replacement order at all. The identity is rebased here rather than
        # handed to reconciliation: `Order.apply` refuses an `OrderFilled` whose
        # venue order id differs from the one already on the order, and it
        # refuses it just the same when the engine builds that fill —
        # `create_order_filled_event` (live/reconciliation.py:381) stamps it with
        # `report.venue_order_id`, so a mismatched fill raises `ValueError`
        # inside `_reconcile_fill_report` and is logged away. `OrderUpdated` is
        # the one carrier the platform lets change a venue order id.
        self._maybe_swap_trigger_venue_order_id(order, venue_order_id, ts_event)

        self._raise_cash_buy_bound(order, instrument, venue_order_id, last_qty, ts_event)

        applied.add(trade_id.value)
        self.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=trade_id,
            order_side=order.side,
            order_type=order.order_type,
            last_qty=last_qty,
            last_px=last_px,
            quote_currency=instrument.quote_currency,
            commission=commission,
            liquidity_side=liquidity_side_from_gateio(payload.get("role")),
            ts_event=ts_event,
        )

    def _fill_quantity_and_commission(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        instrument: Instrument,
    ) -> tuple[Quantity, Money]:
        """Return the quantity the venue matched and the fee it charged, unnetted.

        ``OrderFilled`` states the two as independent facts — ``last_qty`` is
        "the fill quantity for this execution" and ``commission`` "the fill
        commission" (docs/concepts/events/order_filled.md) — so both are reported
        exactly as the venue published them.

        Netting a spot base-currency fee off the quantity looks right, because
        Gate.io deducts its fee from the currency being bought and credits
        ``amount - fee`` base units for a match of ``amount``. The platform
        already does that, once and for itself: ``Position.apply``
        (model/position.pyx:591-612) raises a
        ``PositionAdjusted(COMMISSION, -commission)`` for every fill on a
        ``CurrencyPair`` whose commission is charged in the base currency, and
        ``apply_adjustment`` subtracts it from ``signed_qty``. Netting here as
        well subtracts the same fee twice and leaves every spot BUY position
        short by the cumulative fee — concepts/positions.md, "Base currency
        commissions": a buy of 1.0 BTC with a 0.001 BTC commission is a net long
        position of 0.999 BTC, *from a fill of 1.0*.
        """
        what = f"{product.value} fill {payload.get('id')!r}"
        # An absent fee is the venue making no fee statement — options REST
        # fills carry none at all — which is a commission of zero; a fee the
        # venue stated and this client cannot read raises (REC-06), because a
        # defaulted commission is money silently missing from realized PnL.
        fee = optional_exact_decimal(payload, "fee", what=what)
        if product.is_spot:
            quantity = exact_decimal_field(payload, "amount", what=what)
            fee_currency_value = payload.get("fee_currency")
            if fee_currency_value in (None, ""):
                # Gate.io states `fee_currency` on every documented spot trade
                # row (REST `my_trades` and the `spot.usertrades` stream
                # alike), and it is the base currency for the ordinary buy and
                # the quote for the ordinary sell — there is no correct guess.
                # The old default booked a base-currency fee as quote (a BTC
                # fee as USDT, R8A-03), misstating the commission's
                # denomination. A zero fee needs only a denomination, so it
                # keeps the quote; a nonzero fee without a stated currency is
                # an unknown payload shape and refuses like any other
                # unreadable deciding field.
                if fee != 0:
                    raise ValueError(
                        f"the {what} states a fee of {fee} but no 'fee_currency'; "
                        f"booking it in a guessed currency would misstate the "
                        f"commission",
                    )
                return instrument.make_qty(quantity), Money(0, instrument.quote_currency)
            return instrument.make_qty(quantity), Money(
                fee,
                self._currency(fee_currency_value),
            )

        settlement = getattr(instrument, "settlement_currency", instrument.quote_currency)
        size = abs(exact_lots(payload, "size", what=what))
        # Options fills carry no fee on the REST endpoint; the stream does.
        return Quantity(size, instrument.size_precision), Money(fee, settlement)

    def _raise_cash_buy_bound(
        self,
        order: Order,
        instrument: Instrument,
        venue_order_id: VenueOrderId,
        last_qty: Quantity,
        ts_event: int,
    ) -> None:
        """Hold a spot cash buy's quantity above the base the venue has credited.

        A Gate.io spot market buy is submitted as a cash amount, so the order's
        quantity is denominated in the quote currency while its fills are
        denominated in the base currency. The platform compares the two with no
        unit check at all — ``Order._filled`` (model/orders/base.pyx:1176-1180)
        is a raw ``filled_qty + last_qty < quantity`` — so the order needs a
        base-denominated quantity before its first fill is applied, and
        ``OrderUpdated`` carrying ``is_quote_quantity=False`` is the only
        carrier the platform offers for it.

        The venue states this order's base total exactly once, when the order
        finishes. Until then the quantity here is a **bound**, and it is built
        from the venue's own fill amounts rather than computed from a price.

        The predecessor divided the cash by the *first* fill's price. That
        estimate is an arithmetic the venue never took part in, and it fails in
        both directions: one increment low and the engine's overfill check
        discards the venue's fill outright (``allow_overfills`` defaults False),
        losing an execution; one increment high and the order can never reach
        FILLED and stays open for ever, which is what the mainnet step found. On
        a fill that sweeps two price levels it cannot come out right at all —
        every later fill of a buy is at a worse price than the one divided by.

        Holding the quantity one size increment above what the venue has
        credited removes the low case by construction: no fill is ever
        discarded, and no fill can complete the order before the venue says it
        is finished. ``_close_finished_cash_buy`` then replaces the bound with
        the venue's own base total and closes the order on it.
        """
        entry = self._cash_buy_bounds.get(order.client_order_id)
        if entry is None:
            if not order.is_quote_quantity:
                return
            credited, stated = Decimal(0), None
        else:
            credited, stated = entry

        credited += last_qty.as_decimal()
        if stated is not None and stated > credited:
            # The standing bound still covers this fill, so the order needs no
            # restatement — only the running total moves.
            self._cash_buy_bounds[order.client_order_id] = (credited, stated)
            return

        increment = Decimal(1).scaleb(-instrument.size_precision)
        quantity = instrument.make_qty(credited + increment)
        self._cash_buy_bounds[order.client_order_id] = (credited, quantity.as_decimal())
        self.generate_order_updated(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            quantity=quantity,
            price=getattr(order, "price", None),
            trigger_price=None,
            ts_event=ts_event,
            is_quote_quantity=False,
        )

    def _close_finished_cash_buy(
        self,
        order: Order,
        payload: dict[str, Any],
        instrument: Instrument | None,
        venue_order_id: VenueOrderId,
        ts_event: int,
    ) -> None:
        """Close a spot cash buy the venue finished, on the venue's own base total.

        ``filled_amount`` is the one base-denominated figure Gate.io states for
        this order, and it states it when the order finishes. Restating the
        quantity to it squares the bound the fills were applied against, so the
        order records no remainder the venue never had.

        The close itself has to be ``OrderCanceled``. ``OrderUpdated`` triggers
        no state transition (``Order.apply``, model/orders/base.pyx:1069-1073),
        so a restatement on its own leaves the order PARTIALLY_FILLED with
        nothing left to fill — open for ever, and unreachable by reconciliation,
        which returns "reconciled" for exactly this shape. ``PARTIALLY_FILLED ->
        CANCELED`` is the transition the platform holds for the real-world case
        (base.pyx:132-133), it preserves the filled quantity, and it states what
        the venue did: a Gate.io spot market buy is IOC or FOK, so whatever the
        cash did not buy was canceled rather than left working.

        A fill still in flight on the trade stream is already covered. Gate.io
        does not order ``spot.orders`` against ``spot.usertrades``, and
        ``_handle_late_fill`` routes a fill arriving after a CANCELED close
        through reconciliation — the ordinary IOC case for this venue.
        """
        if order.is_closed:
            self._forget_order(order.client_order_id)
            return

        filled_base: Decimal | None = None
        try:
            filled_base = optional_exact_decimal(
                payload,
                "filled_amount",
                what=f"spot order {payload.get('id')!r}",
            )
        except ValueError as e:
            # The venue stated its base total and this client cannot read it.
            # Restating on a guess would misstate the quantity, so the order is
            # closed on what is certain — that the venue finished it — and the
            # quantity is left to reconciliation.
            self._log.error(
                f"Cannot read the base total Gate.io states for {order.client_order_id!r}: "
                f"{e}. The order is closed on the venue's word without restating its "
                f"quantity, which the next reconciliation pass recovers",
            )

        stated = (self._cash_buy_bounds.get(order.client_order_id) or (None, None))[1]
        if filled_base and filled_base > 0 and instrument is not None:
            quantity = instrument.make_qty(filled_base)
            if quantity.as_decimal() != stated:
                self.generate_order_updated(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=venue_order_id,
                    quantity=quantity,
                    price=getattr(order, "price", None),
                    trigger_price=None,
                    ts_event=ts_event,
                    is_quote_quantity=False,
                )

        self.generate_order_canceled(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=ts_event,
        )
        self._forget_order(order.client_order_id)

    def _handle_balance_payload(self, product: GateioProductType, payload: dict[str, Any]) -> None:
        """Fold a balance change into the account state and publish it."""
        wallet = self._wallet_balances.setdefault(product, {})
        if product.is_spot:
            currency = str(payload.get("currency") or "").upper()
            if not currency:
                return
            total = to_decimal(payload.get("total"))
            free = to_decimal(payload.get("available"))
            if total <= 0 and free <= 0 and currency not in wallet:
                return
            wallet[currency] = (total, free)
        else:
            currency = str(payload.get("currency") or "").upper()
            if not currency:
                # Futures and options report the wallet of their settlement
                # currency, which the payload may leave implicit.
                currency = "BTC" if product.is_inverse else "USDT"
            total = to_decimal(payload.get("balance"))
            # Keep this wallet's previously known locked portion; the stream
            # reports the wallet total only.
            previous_total, previous_free = wallet.get(currency, (Decimal(0), Decimal(0)))
            locked = max(Decimal(0), previous_total - previous_free)
            wallet[currency] = (total, max(Decimal(0), total - locked))

        self._rebuild_aggregate_balances()
        # `ts_event` is "when the event occurred" and `ts_init` "when the object
        # was initialized" (concepts/events/account_state.md); the two parameters
        # exist on `generate_account_state` (execution/client.pyx:329-364) so an
        # adapter can report venue time. The balance stream carries it, so a
        # burst replayed after a reconnect keeps the order in which the balances
        # actually changed instead of collapsing onto the moment we parsed them.
        self._publish_account_state(
            reported=True,
            ts_event=first_timestamp_ns(payload, "time_ms", "timestamp_ms", "time", "timestamp"),
        )

    def _rebuild_aggregate_balances(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Recompute the per-currency totals from the individual wallets.

        Gate.io segregates a wallet per product, so the aggregate is normally the
        sum of the wallets. A **Unified Account** breaks that assumption: it
        reports one cross-product balance per currency that already contains the
        spot and derivative wallets, and every one of those wallets keeps
        answering its own endpoint with the same funds. Summing them would
        multiply the account's equity by the number of enabled products, so a
        currency the unified wallet reports replaces the per-product wallets
        instead of being added to them.
        """
        balances: dict[str, tuple[Decimal, Decimal]] = {}
        for wallet in self._wallet_balances.values():
            for currency, (total, free) in wallet.items():
                if currency in self._unified_balances:
                    continue
                _accumulate(balances, currency, total, free)
        for currency, (total, free) in self._unified_balances.items():
            balances[currency] = (total, max(Decimal(0), free))
        if balances:
            self._balances = balances
        return balances

    def _handle_position_payload(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
    ) -> None:
        """Log a position update without publishing it.

        REST is the single source of truth for positions during reconciliation;
        forwarding stream updates as well would let two views of the same
        position compete after every fill.
        """
        raw_symbol = venue_symbol_of(payload)
        if not raw_symbol:
            return
        size = to_int(payload.get("size"))
        self._log.debug(
            f"Position update {gateio_to_instrument_id(product, raw_symbol)}: size {size}, "
            f"entry {payload.get('entry_price')}, margin {payload.get('margin')}",
        )

    # -- account state -----------------------------------------------------

    async def _query_account(self, command: QueryAccount) -> None:
        """Answer a ``QueryAccount`` command with a fresh reading of every wallet.

        The platform leaves this coroutine to the adapter entirely.
        ``LiveExecutionClient`` overrides the public ``query_account`` and calls
        ``self._query_account(command)`` from it, but — unlike ``_query_order``,
        which it implements generically on top of ``generate_order_status_report``
        — it defines no ``_query_account`` and does not list one among the
        coroutines to implement. Without this method the attribute lookup fails,
        so a ``QueryAccount`` raises ``AttributeError`` *synchronously* inside
        ``query_account``, before any task exists, and propagates out through
        ``ExecutionEngine._handle_query_account``, which does not guard it. The
        state this replaces is not "the command is ignored"; it is an exception
        escaping the execution engine's command path.

        ``command.account_id`` is not used to route. This client owns exactly one
        Gate.io account, and the engine has already resolved the client by the
        account's issuer (``ExecutionEngine._find_client_for_command``), so there
        is nothing left to select. ``command.params`` is not read either: Bybit
        overloads it with venue margin actions, and this venue has no equivalent
        to hang off it.

        The error line is the whole difference between this and a bare
        ``await self._update_account_state()``. A wallet the sweep could not read
        keeps its previous figures, and the state is then published with
        ``reported=True`` — "the balances are reported directly from the
        exchange". The background poll can live with that, and
        :meth:`_update_account_state` explains at length why publishing the stale
        figure beats dropping a product's margins. A user command cannot: the
        caller asked *now* and will read the next ``AccountState`` as the answer,
        and a restatement of last week's numbers is indistinguishable from
        "nothing changed". So the publication is unchanged and the caller is told
        what it is looking at.
        """
        unread = await self._update_account_state()
        if unread:
            self._log.error(
                f"The account state just published for {command.account_id!r} is incomplete: the "
                f"{', '.join(sorted(product.value for product in unread))} wallet(s) could not be "
                f"read, so their balances and margins restate what those wallets last reported "
                f"rather than a fresh reading",
            )

    async def _update_account_state(self) -> frozenset[GateioProductType]:
        """Refresh balances and margins from REST across every enabled product.

        Returns the products whose wallet could **not** be read, which is empty
        on a complete sweep. The figures of an unread product still stand in the
        published state (see below), so a caller that has to distinguish "nothing
        changed" from "nothing was read" — :meth:`_query_account` does — cannot
        get that from the event and gets it from here.

        A poll that could not read every wallet is a *partial* answer, and the
        two things this method feeds treat a partial answer very differently.
        ``MarginAccount.apply`` **replaces** both margin stores from the incoming
        event rather than merging (accounting/accounts/margin.pyx:505-521, and
        concepts/accounting.md, "Margin scopes": "Adapters that emit partial
        snapshots must include every live margin entry on each update or those
        entries will be dropped"), so rebuilding the margin set from only the
        products that answered silently deletes the margin of the ones that did
        not. And under a Unified Account the aggregate is only correct because
        the unified ledger names the currencies whose per-product wallets are
        echoes of the same funds; without that list, summing the wallets
        multiplies the account's equity by the number of enabled products.

        So a product that could not be read keeps its previous wallet *and* its
        previous margins, and the unified ledger is replaced only when it was
        actually read.
        """
        margins: dict[GateioProductType, dict[InstrumentId | Currency, MarginBalance]] = {}
        unified: dict[str, tuple[Decimal, Decimal]] | None = None
        unread: set[GateioProductType] = set()

        for product in self._products:
            wallet: dict[str, tuple[Decimal, Decimal]] = {}
            product_margins: dict[InstrumentId | Currency, MarginBalance] = {}
            try:
                if product.is_spot:
                    unified = await self._collect_spot_balances(wallet)
                elif product.is_option:
                    await self._collect_options_balances(wallet, product_margins)
                else:
                    await self._collect_futures_balances(product, wallet, product_margins)
            except WalletQueryRefusedError as e:
                # Caught before its base class on purpose. Gate.io rejected the
                # *question* (a permission or account-mode label), so nothing at
                # all is known about this ledger — it may hold any balance and
                # any margin. That is an unread wallet, and the wallet that
                # merely does not exist below is not.
                self._log.warning(f"Skipping the {product.value} wallet: {e}")
                unread.add(product)
                continue
            except WalletNotProvisionedError as e:
                # `USER_NOT_FOUND`: the venue created no such wallet yet, which
                # is a complete answer of "it holds nothing". Counting it as
                # unread would make every single-product account report an
                # incomplete sweep on every query.
                self._log.warning(f"Skipping the {product.value} wallet: {e}")
                continue
            except GateioError as e:
                self._log.error(f"Cannot read the {product.value} wallet: {e}")
                unread.add(product)
                continue
            self._wallet_balances[product] = wallet
            margins[product] = product_margins

        if unified is not None:
            self._unified_balances = unified
        elif self._spot_mode is GateioSpotAccountMode.UNIFIED and not self._unified_balances:
            # Never state an aggregate already known to be inflated. Without the
            # unified ledger there is no way to tell which currencies the
            # per-product wallets are merely echoing, and summing them reports
            # the same funds once per enabled product. Publishing nothing leaves
            # the last state that could be stated; on the first poll it leaves
            # the account unregistered, which `_await_account_registered` turns
            # into an explicit connect failure (live/execution_client.py:534-567).
            self._log.error(
                "Cannot state the Gate.io account: the unified ledger could not be read, and "
                "the per-product wallets echo the same funds, so their sum would overstate the "
                "account by a factor of the number of enabled products",
            )
            # Nothing at all was published, so nothing at all was read as far as
            # any caller of this method is concerned.
            return frozenset(self._products)

        self._margins_by_product.update(margins)
        balances = self._rebuild_aggregate_balances()

        if not balances:
            # An account that has never been funded returns no rows at all.
            # Reporting the settlement currencies as zero states exactly that,
            # and lets the account register so the client can start.
            self._log.warning(
                "Gate.io reported no balances for the enabled products; the account holds no "
                "funds, or the API key lacks read permission for those wallets",
            )
            for product in self._products:
                balances.setdefault(product.settle.upper(), (Decimal(0), Decimal(0)))

        self._balances = balances
        self._publish_account_state(reported=True)
        return frozenset(unread)

    async def _collect_spot_balances(
        self,
        balances: dict[str, tuple[Decimal, Decimal]],
    ) -> dict[str, tuple[Decimal, Decimal]] | None:
        """Read the spot ledger, returning the unified snapshot when there is one.

        The return value is what tells the caller a Unified Account was read
        successfully: ``None`` means this client is not in unified mode, and a
        dict (possibly empty) means the unified ledger answered.
        """
        accounts = await self._spot_http.accounts()
        for entry in accounts or []:
            currency = str(entry.get("currency") or "").upper()
            if not currency:
                continue
            free = to_decimal(entry.get("available"))
            locked = to_decimal(entry.get("locked"))
            _accumulate(balances, currency, free + locked, free)

        if self._spot_mode is GateioSpotAccountMode.MARGIN:
            await self._collect_isolated_margin_balances(balances)
        elif self._spot_mode is GateioSpotAccountMode.CROSS_MARGIN:
            await self._collect_cross_margin_balances(balances)
        elif self._spot_mode is GateioSpotAccountMode.UNIFIED:
            # Collected apart from the spot wallet: the unified balance already
            # contains it, and every other product wallet as well.
            unified: dict[str, tuple[Decimal, Decimal]] = {}
            await self._collect_unified_balances(unified)
            return unified
        return None

    async def _collect_isolated_margin_balances(
        self,
        balances: dict[str, tuple[Decimal, Decimal]],
    ) -> None:
        accounts = await require_wallet(
            self._margin_http.accounts(),
            "the isolated margin ledger",
        )
        for pair in accounts or []:
            for side in ("base", "quote"):
                entry = pair.get(side) or {}
                currency = str(entry.get("currency") or "").upper()
                if not currency:
                    continue
                free = to_decimal(entry.get("available"))
                locked = to_decimal(entry.get("locked"))
                borrowed = to_decimal(entry.get("borrowed")) + to_decimal(entry.get("interest"))
                _accumulate(balances, currency, free + locked - borrowed, free)

    async def _collect_cross_margin_balances(
        self,
        balances: dict[str, tuple[Decimal, Decimal]],
    ) -> None:
        account = await require_wallet(
            self._margin_http.cross_accounts(),
            "the cross margin ledger",
        )
        for currency, entry in (account.get("balances") or {}).items():
            code = str(currency).upper()
            free = to_decimal(entry.get("available"))
            locked = to_decimal(entry.get("freeze"))
            borrowed = to_decimal(entry.get("borrowed")) + to_decimal(entry.get("interest"))
            _accumulate(balances, code, free + locked - borrowed, free)

    async def _collect_unified_balances(
        self,
        balances: dict[str, tuple[Decimal, Decimal]],
    ) -> None:
        """Read the Unified Account ledger, which subsumes every product wallet."""
        account = await require_wallet(
            self._margin_http.unified_accounts(),
            "the unified account",
        )
        for currency, entry in (account.get("balances") or {}).items():
            code = str(currency).upper()
            free = to_decimal(entry.get("available"))
            locked = to_decimal(entry.get("freeze"))
            borrowed = to_decimal(entry.get("borrowed")) + to_decimal(entry.get("interest"))
            balances[code] = (free + locked - borrowed, free)

    async def _collect_futures_balances(
        self,
        product: GateioProductType,
        balances: dict[str, tuple[Decimal, Decimal]],
        margins: dict[InstrumentId | Currency, MarginBalance],
    ) -> None:
        api = self._futures_api(product)
        account = await require_wallet(api.accounts(), f"the {product.value} wallet")
        currency = str(account.get("currency") or product.settle).upper()
        # The wallet balance, deliberately *without* the venue's unrealised PnL.
        # Gate.io says of `total`: "does not include upl of positions", and that
        # is exactly the figure the platform wants: `Portfolio.equity()` for a
        # margin account is `balances_total + sum(unrealized_pnl(open positions))`
        # (portfolio/portfolio.pyx:1176-1243; concepts/portfolio.md, "Equity
        # formula"), so folding the venue's unrealised PnL into `total` makes the
        # platform count it a second time. In-tree Binance reports the same
        # figure for the same reason — `walletBalance`, not `marginBalance`
        # (adapters/binance/futures/schemas/account.py:75-88). It also makes the
        # REST poll agree with the `futures.balances` stream, which carries the
        # wallet balance alone and would otherwise contradict it every tick.
        total = to_decimal(account.get("total"))
        free = to_decimal(account.get("available"))
        # `available` can exceed the wallet balance when unrealised profit is
        # spendable as collateral; clamping keeps `locked` non-negative, which is
        # what the reference adapter does at this same point.
        _accumulate(balances, currency, total, min(free, total))

        positions = await require_wallet(
            api.positions(holding=True),
            f"the {product.value} positions",
        )
        currency_obj = self._currency(currency)
        for position in positions or []:
            margin = self._position_margin(product, position, currency_obj)
            if margin is not None:
                _merge_margin(margins, margin)

    def _position_margin(
        self,
        product: GateioProductType,
        position: dict[str, Any],
        currency: Currency,
    ) -> MarginBalance | None:
        """Build the margin one venue position requires, in the scope it is held under.

        The scope is not cosmetic. concepts/accounting.md, "Margin scopes",
        defines the per-instrument scope (``instrument_id`` set) as *isolated*
        collateral — segregated to one position — and the account-wide scope
        (``instrument_id=None``, keyed by the collateral currency) as what a
        cross-margin venue reports, because there the collateral is shared and
        closing one position frees it for every other. Every in-tree crypto
        adapter that runs cross margin reports account-wide, and
        ``MarginAccount`` keeps the two in separate stores
        (accounting/accounts/margin.pyx:511-521), so the choice decides which
        query answers at all: ``margin_init_for_currency`` sees only the
        account-wide store, ``margin_init(instrument_id)`` only the other.

        Gate.io states which one a position uses in one field: ``leverage == "0"``
        means cross margin (the cap then lives in ``cross_leverage_limit``), and
        any positive ``leverage`` is isolated at that leverage. Reporting a cross
        position per instrument would tell a strategy that each instrument has
        its own collateral, which on this venue is not true.
        """
        raw_symbol = venue_symbol_of(position)
        if not raw_symbol:
            return None
        initial = to_decimal(position.get("initial_margin"))
        if initial <= 0:
            initial = to_decimal(position.get("margin"))
        maintenance = to_decimal(position.get("maintenance_margin"))
        if maintenance <= 0:
            rate = to_decimal(position.get("average_maintenance_rate"))
            if rate <= 0:
                rate = to_decimal(position.get("maintenance_rate"))
            maintenance = to_decimal(position.get("value")) * rate
        if initial <= 0 and maintenance <= 0:
            return None
        return MarginBalance(
            initial=Money(max(Decimal(0), initial), currency),
            maintenance=Money(max(Decimal(0), maintenance), currency),
            instrument_id=(
                None if _is_cross_margin(position) else gateio_to_instrument_id(product, raw_symbol)
            ),
        )

    async def _collect_options_balances(
        self,
        balances: dict[str, tuple[Decimal, Decimal]],
        margins: dict[InstrumentId | Currency, MarginBalance],
    ) -> None:
        account = await require_wallet(self._options_http.account(), "the options wallet")
        currency = str(account.get("currency") or "USDT").upper()
        # `total` is the options account balance; `equity` is "balance + position
        # value" and therefore already carries the unrealised PnL the Portfolio
        # adds itself (see `_collect_futures_balances`). Use the balance, and
        # recover it from `equity` only when the venue omitted it.
        total = to_decimal(account.get("total"))
        if total <= 0:
            total = to_decimal(account.get("equity")) - to_decimal(account.get("unrealised_pnl"))
        free = to_decimal(account.get("available"))
        _accumulate(balances, currency, total, min(free, total))

        initial = to_decimal(account.get("init_margin")) + to_decimal(account.get("order_margin"))
        maintenance = to_decimal(account.get("maint_margin"))
        if initial > 0 or maintenance > 0:
            # The options wallet reports one figure for the whole account, which
            # is the account-wide scope by definition.
            currency_obj = self._currency(currency)
            _merge_margin(
                margins,
                MarginBalance(
                    initial=Money(max(Decimal(0), initial), currency_obj),
                    maintenance=Money(max(Decimal(0), maintenance), currency_obj),
                ),
            )

    def _publish_account_state(self, reported: bool, ts_event: int = 0) -> None:
        """Publish the aggregated wallet state as a Nautilus ``AccountState``.

        ``ts_event`` is the venue's own timestamp for the change, when there is
        one. A REST snapshot has none — it is a reading taken now — so it passes
        ``0`` and the local clock stands in.
        """
        balances: list[AccountBalance] = []
        for code, (total, free) in sorted(self._balances.items()):
            currency = self._currency(code)
            total_money = Money(total, currency)
            free_money = Money(min(free, total), currency)
            locked_money = Money(total_money.as_decimal() - free_money.as_decimal(), currency)
            balances.append(
                AccountBalance(total=total_money, locked=locked_money, free=free_money),
            )
        if not balances:
            return

        margins: list[MarginBalance] = []
        if self._account_type == AccountType.MARGIN:
            # Every live entry, every time: `MarginAccount.apply` replaces both
            # stores from the event, so anything left out here is deleted. The
            # merge across products matters as much as the completeness: two
            # products can back onto one collateral — a USDT-settled perpetual
            # and the USDT options wallet — and the platform keys account-wide
            # margin by currency (accounting/accounts/margin.pyx:511-521), so
            # two entries for the same currency would not add up, the second
            # would simply replace the first.
            merged: dict[InstrumentId | Currency, MarginBalance] = {}
            for product_margins in self._margins_by_product.values():
                for margin in product_margins.values():
                    _merge_margin(merged, margin)
            margins = list(merged.values())

        self.generate_account_state(
            balances=balances,
            margins=margins,
            reported=reported,
            ts_event=ts_event or self._clock.timestamp_ns(),
        )

    # -- wallet transfers --------------------------------------------------

    async def transfer(
        self,
        currency: str,
        from_: str,
        to: str,
        amount: str,
        settle: str | None = None,
        currency_pair: str | None = None,
    ) -> dict[str, Any]:
        """Move funds between this account's own Gate.io trading wallets.

        Gate.io segregates balances per product, and a derivative wallet is
        created by the first transfer into it, so this is both how funds are
        rebalanced and how the futures, delivery and options wallets come into
        existence.

        Parameters
        ----------
        currency : str
            The asset to move, for example ``"USDT"``.
        from_ : str
            The source wallet (``spot``, ``margin``, ``futures``, ``delivery``,
            ``options``, ``cross_margin``, ``unified``).
        to : str
            The destination wallet.
        amount : str
            The decimal amount as a string.
        settle : str, optional
            The settlement currency, required when either end is a contract
            wallet (``"usdt"`` or ``"btc"``).
        currency_pair : str, optional
            The isolated margin pair, required when either end is ``margin``.

        Returns
        -------
        dict[str, Any]
            The venue response, ``{"tx_id": ...}``.

        Raises
        ------
        ValueError
            If either end is not an internal trading wallet, or a required
            companion parameter is missing.

        Notes
        -----
        Every internal transfer is routed through the spot wallet by Gate.io, so
        moving between two derivative wallets takes two calls. This method
        cannot send funds outside the account: the request carries no address
        and no recipient.

        """
        response = await self._wallet_http.transfer(
            currency=currency,
            from_=from_,
            to=to,
            amount=amount,
            settle=settle,
            currency_pair=currency_pair,
        )
        self._log.info(f"Transferred {amount} {currency} from {from_} to {to}")
        await self._update_account_state()
        return response

    # -- reconciliation: startup mass status --------------------------------

    async def generate_mass_status(
        self,
        lookback_mins: int | None = None,
    ) -> ExecutionMassStatus | None:
        """Assemble the startup mass status, surviving an unanswerable position query.

        The inherited implementation gathers the three report sets together and
        returns ``None`` — no orders, no fills, no reconciliation at all — if any
        one of them raises. That is the wrong trade here, because
        :meth:`generate_position_status_reports` raises *by design*: it is the
        only way this client can tell the engine that a position query went
        unanswered, and the engine's position paths depend on hearing it.
        Inheriting the base behaviour would let one 502 on the position endpoint
        throw away the order and fill recovery a restart exists to perform.

        Positions lose nothing by being left out of the mass status when they
        could not be read: ``reconcile_execution_state`` follows the mass status
        by querying, per instrument, every open position the mass status did not
        cover, and that path handles both "the venue says flat" and "the venue
        did not answer" correctly.

        A failed order or trade listing is the opposite case, and takes the
        platform's own posture (the base ``generate_mass_status``, installed
        live/execution_client.py:440-514, where any report exception nukes the
        whole mass status): this method returns ``None`` and the kernel
        refuses to start. Round eight restated this from "work from the
        partial answer" after the refutation drove the partial path through
        the engine: order reports state filled quantities, the trades backing
        them are missing from the partial listing, and
        ``_handle_fill_quantity_mismatch`` mints commission-less inferred
        stand-ins for the difference while reconciliation reports success — a
        fabricated execution in place of a venue trade. Positions can be left
        out safely because the engine re-queries them per instrument with
        correct failure semantics; orders and fills have no such second
        chance inside one startup.

        The recovered executions are booked **inside this method, before it
        returns** — see the sweep below. This is the one moment on the restart
        route with the three properties a correct booking needs, all verified
        against the installed engine:

        * the engine has reconciled nothing yet: ``reconcile_execution_state``
          awaits this method and only then calls
          ``_reconcile_execution_mass_status``, so nothing can have squared a
          position report that already contains these trades against a cache
          that does not carry them yet;
        * the cached orders carry their venue ids (they survive a restart in
          the cache index, and the rebase above restores the trigger-fired
          ones), so ``_reconcile_fill_report_single`` can attribute every fill;
        * a fill booked here updates the cache before the engine's duplicate
          filter runs, so ``_deduplicate_mass_status_orders`` deletes a
          now-matching order report harmlessly and a position report
          reconciles against a cache that already carries the trade.

        Checking *afterwards* which reports the engine booked — the reconnect
        route's shape — has no correct trigger here, and the attempt is
        recorded so it is not repeated (docs/roadmap.md, Stage 0): a sweep
        staged off the engine's publication of the reconciled mass status ran
        after the engine had already reconciled the startup position reports,
        so it booked the venue's real trade on top of the inferred fill the
        engine had just minted for the same trade — a doubled position that
        nothing corrects, because the periodic position check is off by
        default.
        """
        self._log.info("Generating ExecutionMassStatus...")
        self.reconciliation_active = True

        since = (
            self._clock.utc_now() - timedelta(minutes=lookback_mins)
            if lookback_mins is not None
            else None
        )
        ts_init = self._clock.timestamp_ns()

        mass_status = ExecutionMassStatus(
            client_id=self.id,
            account_id=self.account_id,
            venue=self.venue,
            report_id=UUID4(),
            ts_init=ts_init,
        )

        # Concurrent, as in the base class: the order listing and the trade
        # listing are two separate venue answers, and every moment between them
        # is a window in which a match can land in one and not the other.
        orders, fills, positions = await asyncio.gather(
            self.generate_order_status_reports(
                GenerateOrderStatusReports(
                    instrument_id=None,
                    start=since,
                    end=None,
                    open_only=False,
                    command_id=UUID4(),
                    ts_init=ts_init,
                ),
            ),
            self.generate_fill_reports(
                GenerateFillReports(
                    instrument_id=None,
                    venue_order_id=None,
                    start=since,
                    end=None,
                    command_id=UUID4(),
                    ts_init=ts_init,
                ),
            ),
            self.generate_position_status_reports(
                GeneratePositionStatusReports(
                    instrument_id=None,
                    start=since,
                    end=None,
                    command_id=UUID4(),
                    ts_init=ts_init,
                ),
            ),
            return_exceptions=True,
        )

        for result in (orders, fills, positions):
            if isinstance(result, asyncio.CancelledError):
                raise result

        if isinstance(orders, BaseException) or isinstance(fills, BaseException):
            # The platform posture (see the docstring): an order or trade
            # listing that did not answer in full - a failed endpoint, or a
            # row whose deciding field could not be read - fails the whole
            # mass status. Booking a partial account and letting the engine
            # square the gaps fabricates executions; refusing to start does
            # not.
            failure = orders if isinstance(orders, BaseException) else fills
            self._log.exception("Cannot reconcile execution state", failure)
            return None

        # A restart destroys the armed/fired identity map, so the cached order
        # still holds the id it was placed under while the venue reports it — and
        # its trades — under the id the trigger created. The engine reconciles a
        # mass status inline, so the rebase has to land before it is handed over.
        await self._adopt_reported_venue_order_ids(orders)

        # Book every recovered execution not yet on its order, now, before the
        # engine sees anything. Waiting for the engine's grouped pass loses
        # them: its duplicate filter deletes an order report matching the
        # cached order on status and filled quantity together with the trades
        # grouped under it, and its ACCEPTED short-circuit returns before the
        # trade loop — both of which are exactly the shape of a match landing
        # between the order-listing read and the trade-listing read. The
        # engine then squares the resulting position gap with a reconciliation
        # order and an inferred fill carrying no venue trade id and no
        # commission, which on spot overstates the position by the withheld
        # base-currency fee.
        listed_orders = {
            report.venue_order_id: report for report in orders if report.venue_order_id is not None
        }
        unbooked_before = [r for r in fills if not self._fill_is_booked(r)]
        if unbooked_before:
            # The orders and the instruments with open positions held before
            # the sweep books anything: the prior knowledge that decides which
            # of the trades being booked arm the stale-answer memory
            # (`_record_recovery_bookings`). Snapshotted — and recorded —
            # before the sweep, so a position it opens can never count as
            # pre-existing (that would re-freeze the fresh-cache restart,
            # R7C-01), and a trade the sweep fails to book is still guarded:
            # the engine books it from this very mass status moments after
            # this method returns, which is after any post-sweep arming would
            # have run (REC-07).
            known_before = {order.client_order_id for order in self._cache.orders()}
            positions_before = {
                position.instrument_id for position in self._cache.positions_open(venue=None)
            }
            self._record_recovery_bookings(unbooked_before, known_before, positions_before)
            try:
                await self._hand_over_unapplied_fills(unbooked_before, listed_orders)
            except FillReportsUnavailable as e:
                # A venue-named execution whose order statement was asked for
                # and not delivered readably: booking without it fabricates,
                # and returning a mass status that silently lacks it reports
                # success over a book that is missing money. The platform
                # posture again (REC-08): refuse the whole mass status and let
                # the kernel refuse to start; the next attempt re-reads the
                # order and heals.
                self._log.exception("Cannot reconcile execution state", e)
                return None
            booked_now = [r for r in unbooked_before if self._fill_is_booked(r)]
            orders = self._prune_reports_the_sweep_outran(orders, booked_now)

        mass_status.add_order_reports(reports=orders)
        mass_status.add_fill_reports(reports=fills)

        if isinstance(positions, PositionStatusUnavailable):
            self._log.warning(
                f"Startup mass status carries no position reports: {positions}. "
                f"Open positions are reconciled per instrument straight after this",
            )
        elif isinstance(positions, BaseException):
            self._log.exception(
                "Cannot read position status for the startup mass status",
                positions,
            )
        else:
            # The account-wide position answer was read concurrently with the
            # listings above, so it can predate the trades just booked; a row
            # the booked trades refute is withheld rather than handed to an
            # engine that would take it as current truth.
            mass_status.add_position_reports(
                reports=self._withhold_stale_position_reports(positions),
            )

        self.reconciliation_active = False
        return mass_status

    # -- reconciliation: order status --------------------------------------

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        """Generate order status reports across every enabled product.

        A product whose listing failed — the endpoint, or a row whose deciding
        field could not be read — makes this method raise
        :class:`OrderReportsUnavailable` carrying what did parse. Swallowing
        the failure and returning the rest is indistinguishable from "the
        venue holds no such orders": a cached order the missing row would have
        closed then stays open locally with only a debug line downstream, an
        open/closed disagreement with the venue (REC-06). Where the raise
        lands: the startup path refuses the mass status (kernel refuses to
        start), the single-order path answers ``None``, and the open-order
        check swallows it per client and proceeds on an empty answer — see
        :class:`OrderReportsUnavailable` for the non-default configuration in
        which that empty answer can still fabricate.

        A wallet Gate.io has not created is not a failure: it holds no orders,
        which is a definite answer of none.
        """
        reports: list[OrderStatusReport] = []
        failures: list[str] = []
        start_secs, end_secs = self._window(command.start, command.end)

        for product in self._products:
            try:
                reports += await self._order_reports_for_product(
                    product,
                    command.instrument_id,
                    open_only=command.open_only,
                    start_secs=start_secs,
                    end_secs=end_secs,
                )
            except WalletNotProvisionedError as e:
                self._log.warning(f"Skipping {product.value} order reports: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reported below, after every product
                self._log_report_error(e, f"{product.value} OrderStatusReports")
                failures.append(f"{product.value}: {e}")

        self._log_report_receipt(len(reports), "OrderStatusReport", command.log_receipt_level)

        if failures:
            raise OrderReportsUnavailable(
                f"Gate.io did not answer the order listing"
                f"{f' for {command.instrument_id}' if command.instrument_id is not None else ''}: "
                f"{'; '.join(failures)}",
                reports,
            )
        return reports

    async def _order_reports_for_product(
        self,
        product: GateioProductType,
        instrument_id: InstrumentId | None,
        open_only: bool,
        start_secs: int,
        end_secs: int,
    ) -> list[OrderStatusReport]:
        if instrument_id is not None:
            resolved = self._resolve(instrument_id)
            if resolved is None or resolved[0] is not product:
                return []
            symbols: list[str] = [resolved[1]]
        else:
            symbols = []

        if product.is_spot:
            return await self._spot_order_reports(symbols, open_only, start_secs, end_secs)
        if product.is_option:
            return await self._options_order_reports(symbols, open_only, start_secs, end_secs)
        return await self._futures_order_reports(product, symbols, open_only, start_secs, end_secs)

    async def _collect_pages(
        self,
        fetch: Callable[[int], Awaitable[Any]],
        *,
        description: str,
        wallet: str | None = None,
        stop_after: Callable[[list[dict[str, Any]]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Read every page of a listing endpoint, not just the first.

        Gate.io caps a listing at 100 rows, so a single call silently truncates
        reconciliation to the newest 100 orders or fills. ``fetch`` receives the
        cursor (a page number or a row offset, whichever the endpoint uses) and
        returns one page. Paging stops on a short page, when ``stop_after`` says
        the window is exhausted, or at :data:`MAX_REPORT_PAGES` so a
        misbehaving endpoint cannot produce an unbounded request loop.
        """
        collected: list[dict[str, Any]] = []
        for page_index in range(MAX_REPORT_PAGES):
            call = fetch(page_index)
            payloads = await (require_wallet(call, wallet) if wallet is not None else call)
            rows = [row for row in (payloads or []) if isinstance(row, dict)]
            collected += rows
            if len(rows) < REPORT_PAGE_LIMIT:
                return collected
            if stop_after is not None and stop_after(rows):
                return collected
        self._log.warning(
            f"Stopped paging {description} at {MAX_REPORT_PAGES} pages "
            f"({len(collected)} rows); the window may be incompletely reconciled",
        )
        return collected

    async def _spot_order_reports(
        self,
        symbols: list[str],
        open_only: bool,
        start_secs: int,
        end_secs: int,
    ) -> list[OrderStatusReport]:
        product = GateioProductType.SPOT
        market = symbols[0] if symbols else None
        reports: list[OrderStatusReport] = []
        seen_symbols: set[str] = set(symbols)

        # Price orders are read FIRST: they carry the armed -> fired identity a
        # fired conditional order needs before it can be attributed to its
        # Nautilus order (a spot price order has no client-id field at all).
        armed = await self._collect_pages(
            lambda page: self._spot_http.list_price_orders(
                status="open",
                market=market,
                limit=REPORT_PAGE_LIMIT,
                offset=page * REPORT_PAGE_LIMIT,
            ),
            description="spot price orders",
        )
        reports += await self._trigger_reports(product, armed)

        if not open_only:
            fired = await self._collect_pages(
                lambda page: self._spot_http.list_price_orders(
                    status="finished",
                    market=market,
                    limit=REPORT_PAGE_LIMIT,
                    offset=page * REPORT_PAGE_LIMIT,
                ),
                description="finished spot price orders",
            )
            # Finished price orders produce no report of their own (the order
            # they fired is reported as a normal order), but they restore the
            # identity map after a restart.
            reports += await self._trigger_reports(product, fired)

        if symbols:
            for symbol in symbols:
                payloads = await self._collect_pages(
                    lambda page, symbol=symbol: self._spot_http.list_orders(
                        symbol,
                        status="open",
                        limit=REPORT_PAGE_LIMIT,
                        page=page + 1,
                        account=self.spot_account,
                    ),
                    description=f"open spot orders on {symbol}",
                )
                reports += await self._reports_from(product, payloads)
        else:
            grouped = await self._collect_pages(
                lambda page: self._spot_http.open_orders(
                    limit=REPORT_PAGE_LIMIT,
                    page=page + 1,
                ),
                description="open spot orders",
            )
            for group in grouped:
                symbol = str(group.get("currency_pair") or "")
                if symbol:
                    seen_symbols.add(symbol)
                reports += await self._reports_from(product, group.get("orders"))

        if open_only:
            return reports

        for symbol in sorted(seen_symbols | self._active_symbols(product)):
            payloads = await self._collect_pages(
                lambda page, symbol=symbol: self._spot_http.list_orders(
                    symbol,
                    status="finished",
                    limit=REPORT_PAGE_LIMIT,
                    page=page + 1,
                    frm=start_secs,
                    to=end_secs,
                    account=self.spot_account,
                ),
                description=f"finished spot orders on {symbol}",
            )
            reports += await self._reports_from(product, payloads)
        return reports

    async def _futures_order_reports(
        self,
        product: GateioProductType,
        symbols: list[str],
        open_only: bool,
        start_secs: int,
        end_secs: int,
    ) -> list[OrderStatusReport]:
        api = self._futures_api(product)
        contract = symbols[0] if symbols else None
        reports: list[OrderStatusReport] = []

        armed = await self._collect_pages(
            lambda page: api.list_price_orders(
                status="open",
                contract=contract,
                limit=REPORT_PAGE_LIMIT,
                offset=page * REPORT_PAGE_LIMIT,
            ),
            description=f"{product.value} price orders",
            wallet=f"the {product.value} price-triggered orders",
        )
        reports += await self._trigger_reports(product, armed)

        payloads = await self._collect_pages(
            lambda page: api.list_orders(
                status="open",
                contract=contract,
                limit=REPORT_PAGE_LIMIT,
                offset=page * REPORT_PAGE_LIMIT,
            ),
            description=f"open {product.value} orders",
            wallet=f"the {product.value} open orders",
        )
        reports += await self._reports_from(product, payloads)

        if open_only:
            return reports

        fired = await self._collect_pages(
            lambda page: api.list_price_orders(
                status="finished",
                contract=contract,
                limit=REPORT_PAGE_LIMIT,
                offset=page * REPORT_PAGE_LIMIT,
            ),
            description=f"finished {product.value} price orders",
            wallet=f"the {product.value} finished price-triggered orders",
        )
        reports += await self._trigger_reports(product, fired)

        finished = await self._collect_pages(
            lambda page: api.list_orders(
                status="finished",
                contract=contract,
                limit=REPORT_PAGE_LIMIT,
                offset=page * REPORT_PAGE_LIMIT,
            ),
            description=f"finished {product.value} orders",
            wallet=f"the {product.value} finished orders",
            stop_after=lambda rows: _oldest_ts_before(rows, start_secs),
        )
        reports += [
            report
            for report in await self._reports_from(product, finished)
            if _within(report.ts_last, start_secs, end_secs)
        ]
        return reports

    async def _options_order_reports(
        self,
        symbols: list[str],
        open_only: bool,
        start_secs: int,
        end_secs: int,
    ) -> list[OrderStatusReport]:
        product = GateioProductType.OPT
        contract = symbols[0] if symbols else None
        reports: list[OrderStatusReport] = []

        payloads = await self._collect_pages(
            lambda page: self._options_http.list_orders(
                status="open",
                contract=contract,
                limit=REPORT_PAGE_LIMIT,
                offset=page * REPORT_PAGE_LIMIT,
            ),
            description="open options orders",
            wallet="the options open orders",
        )
        reports += await self._reports_from(product, payloads)

        if open_only:
            return reports

        finished = await self._collect_pages(
            lambda page: self._options_http.list_orders(
                status="finished",
                contract=contract,
                limit=REPORT_PAGE_LIMIT,
                offset=page * REPORT_PAGE_LIMIT,
                frm=start_secs,
                to=end_secs,
            ),
            description="finished options orders",
            wallet="the options finished orders",
        )
        reports += await self._reports_from(product, finished)
        return reports

    def _active_symbols(self, product: GateioProductType) -> set[str]:
        """Venue symbols with local open orders or positions for ``product``."""
        symbols: set[str] = set()
        for order in self._cache.orders_open(venue=GATEIO_VENUE):
            resolved = self._safe_resolve(order.instrument_id)
            if resolved is not None and resolved[0] is product:
                symbols.add(resolved[1])
        for position in self._cache.positions_open(venue=GATEIO_VENUE):
            resolved = self._safe_resolve(position.instrument_id)
            if resolved is not None and resolved[0] is product:
                symbols.add(resolved[1])
        return symbols

    @staticmethod
    def _safe_resolve(instrument_id: InstrumentId) -> tuple[GateioProductType, str] | None:
        try:
            return instrument_id_to_gateio(instrument_id)
        except ValueError:
            return None

    async def _reports_from(
        self,
        product: GateioProductType,
        payloads: Any,
    ) -> list[OrderStatusReport]:
        reports: list[OrderStatusReport] = []
        for payload in payloads or []:
            if not isinstance(payload, dict):
                continue
            raw_symbol = venue_symbol_of(payload)
            if not raw_symbol:
                self._log.error(
                    f"Discarding a {product.value} order report without a symbol: "
                    f"id {payload.get('id')!r}",
                )
                continue
            instrument = await self._instrument_or_load(
                gateio_to_instrument_id(product, raw_symbol),
            )
            if instrument is None:
                continue  # `_instrument_or_load` has already logged the loss
            report = self._parse_order_status_report(product, payload, instrument)
            if report is not None:
                reports.append(report)
        return reports

    async def _trigger_reports(
        self,
        product: GateioProductType,
        payloads: Any,
    ) -> list[OrderStatusReport]:
        """Rebuild the trigger identity map and report the orders still armed.

        Every price order restores its armed/fired link, including the ones that
        have already fired: that link is the only way a restarted client can
        attribute the fired order, and its fills, back to the Nautilus order.
        Only orders still waiting for their trigger produce a report, since a
        fired one is reported through the regular order listing.
        """
        reports: list[OrderStatusReport] = []
        for payload in payloads or []:
            if not isinstance(payload, dict):
                continue
            link = self._link_from_trigger_payload(product, payload)
            status = str(payload.get("status") or "").lower()
            if status not in ARMED_TRIGGER_STATUSES:
                continue
            report = await self._parse_trigger_order_report(product, payload, link)
            if report is not None:
                reports.append(report)
        return reports

    def _link_from_trigger_payload(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
    ) -> GateioTriggerLink | None:
        """Recover the armed/fired identity of one price-order payload.

        Resolution order, most reliable first: an id this client already knows;
        the ``t-`` client id embedded in ``initial.text`` (futures only — a spot
        ``put`` has no such field); the cache index against the armed id, which
        is what the pre-restart ``OrderAccepted`` recorded; and finally the cache
        index against the fired id.
        """
        armed_value = payload.get("id_string") or payload.get("id")
        if armed_value in (None, ""):
            return None
        armed_id = str(armed_value)
        fired_id = _fired_order_id(payload)

        link = self._trigger_by_armed_id.get(armed_id)
        if link is not None:
            if fired_id:
                self._attach_fired_order_id(link, fired_id)
            return link

        initial = payload.get("initial") or payload.get("put") or {}
        client_order_id = self._client_order_id_from_text(initial.get("text"))
        if client_order_id is None:
            client_order_id = self._cache.client_order_id(VenueOrderId(armed_id))
        if client_order_id is None and fired_id:
            client_order_id = self._cache.client_order_id(VenueOrderId(fired_id))
        if client_order_id is None:
            return None
        return self._register_trigger_link(product, armed_id, client_order_id, fired_id)

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        """Generate a single order status report by venue or client order id.

        Either identifier is enough, and answering to the client order id alone
        is not optional. ``LiveExecutionEngine._check_inflight_orders`` (installed
        live/execution_engine.py:701-765) queries an order that is still
        ``SUBMITTED``, and a ``SUBMITTED`` order has no venue order id — the
        engine passes ``order.venue_order_id``, which is ``None`` until
        ``OrderAccepted``. That query is the whole resolution path for a submit
        whose outcome Gate.io never confirmed: ``open_check_interval_secs``
        defaults to ``None`` (live/config.py:188), so the open-order sweep is not
        even running, and after ``inflight_check_retries`` unanswered queries
        ``_resolve_inflight_order`` (:767-795) emits
        ``OrderRejected(reason="UNKNOWN")`` — terminal, and on 1.230.0 no later
        ``OrderAccepted`` can undo it. Returning ``None`` for want of a venue
        order id therefore discards exactly the handling
        :meth:`_outcome_unresolved` exists to provide.
        """
        instrument_id = command.instrument_id
        client_order_id = command.client_order_id
        venue_order_id = command.venue_order_id

        if client_order_id is None and venue_order_id is None:
            # The platform states this as the method's own contract
            # (LiveExecutionClient.generate_order_status_report, installed
            # live/execution_client.py:359-362: "Raises ValueError if both the
            # `client_order_id` and `venue_order_id` are None"), and the
            # reference adapter asserts it the same way
            # (adapters/binance/execution.py:381-384). It is a caller error, not
            # an order that could not be found, so it must not be logged as one.
            raise ValueError("both `client_order_id` and `venue_order_id` were `None`")

        if venue_order_id is None and client_order_id is not None:
            venue_order_id = self._cache.venue_order_id(client_order_id)

        link = self._trigger_links.get(client_order_id) if client_order_id is not None else None
        if link is not None and link.fired_id is not None:
            # The trigger fired: the live object is the order it created.
            venue_order_id = VenueOrderId(link.fired_id)

        try:
            if link is not None and link.is_armed:
                product = link.product
                if product.is_spot:
                    payload = await self._spot_http.get_price_order(link.armed_id)
                else:
                    payload = await self._futures_api(product).get_price_order(link.armed_id)
                if not isinstance(payload, dict):
                    return None
                refreshed = self._link_from_trigger_payload(product, payload) or link
                return await self._parse_trigger_order_report(product, payload, refreshed)

            if instrument_id is None:
                self._log.warning(
                    "Cannot generate an order status report without an instrument id: "
                    "Gate.io scopes order lookups by product",
                )
                return None

            resolved = self._resolve(instrument_id)
            if resolved is None:
                return None
            product, raw_symbol = resolved

            if venue_order_id is None:
                return await self._report_by_client_order_id(
                    product,
                    raw_symbol,
                    instrument_id,
                    client_order_id,  # type: ignore[arg-type]  # guarded above
                )

            payload = await self._get_order(product, venue_order_id.value, raw_symbol)
        except WalletNotProvisionedError as e:
            self._log.warning(f"Cannot generate an order status report: {e}")
            return None
        except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
            self._log_report_error(e, "OrderStatusReport")
            return None

        try:
            return await self._report_from_payload(product, payload, raw_symbol)
        except ValueError as e:
            # An unreadable deciding field on the one-order read. ``None`` is
            # the honest single-order answer — the engine treats it as an
            # unanswered query — and the failure is loud enough to name the
            # field and the value.
            self._log_report_error(e, "OrderStatusReport")
            return None

    async def _get_order(
        self,
        product: GateioProductType,
        order_id: str,
        raw_symbol: str,
    ) -> Any:
        """Read one order from the product's single-order endpoint."""
        if product.is_spot:
            return await self._spot_http.get_order(
                order_id,
                raw_symbol,
                account=self.spot_account,
            )
        if product.is_option:
            return await self._options_http.get_order(order_id)
        return await self._futures_api(product).get_order(order_id)

    async def _report_from_payload(
        self,
        product: GateioProductType,
        payload: Any,
        raw_symbol: str,
    ) -> OrderStatusReport | None:
        """Turn one order payload into a report, loading its instrument if needed.

        This is the single-order path (the direct read behind
        ``generate_order_status_report``), which is what ``single_order``
        declares: it changes only the still-open spot cash market buy, which
        gets the honest quote-denominated ACCEPTED answer here and silence in
        the listings (R7C-03; see ``_open_cash_buy_as_quote``).
        """
        if not isinstance(payload, dict):
            return None
        instrument = await self._instrument_or_load(
            gateio_to_instrument_id(product, venue_symbol_of(payload) or raw_symbol),
        )
        if instrument is None:
            return None
        return self._parse_order_status_report(product, payload, instrument, single_order=True)

    def _known_venue_text(self, client_order_id: ClientOrderId) -> str | None:
        """Return the ``text`` this order was submitted under, without minting one.

        :meth:`_venue_text` is the wrong call for a lookup: it *creates* an alias
        for an id it has not seen, which would have this client ask Gate.io about
        a text it never sent. The alias table answers for the life of the
        process; past a restart the ``t-``-prefixed form is still reconstructible
        for any id that fits the field, which is the ordinary case and the reason
        the id is embedded verbatim in the first place. An id that did not fit
        was submitted under a generated text, and that mapping does not survive
        the process — the caller falls back to the listing scan, which reports
        the order as external, which is what it has become.
        """
        known = self._text_by_client_order_id.get(client_order_id)
        if known is not None:
            return known
        if _TEXT_BODY_PATTERN.match(client_order_id.value):
            return CLIENT_ORDER_ID_PREFIX + client_order_id.value
        return None

    async def _report_by_client_order_id(
        self,
        product: GateioProductType,
        raw_symbol: str,
        instrument_id: InstrumentId,
        client_order_id: ClientOrderId,
    ) -> OrderStatusReport | None:
        """Report an order whose venue order id this client never learned.

        This is the ambiguous-submit case: the create request went out, the
        answer did not come back, and the only handle on the order is the client
        id embedded in its ``text``. Gate.io takes that text in place of the
        venue id on the spot and perpetual single-order endpoints
        (:data:`CLIENT_ID_ADDRESSABLE_PRODUCTS`) but not on delivery or options,
        and even where it is taken it stops resolving once the order has been
        finished for a minute. So the direct read is an optimisation, not the
        mechanism: what works on every product and at every age is the order
        listing, which carries ``text`` on every row and is already parsed into
        reports by :meth:`_order_reports_for_product`. Resting orders are listed
        first because an order queried without a venue id has almost always just
        been submitted; the finished listing is only walked when that missed.
        """
        text = self._known_venue_text(client_order_id)
        if text is not None and product in CLIENT_ID_ADDRESSABLE_PRODUCTS:
            try:
                payload = await self._get_order(product, text, raw_symbol)
            except GateioError as e:
                # "Not found" is an ordinary answer here — the custom-id window
                # has closed, or the venue never accepted the order — and it is
                # not the end of the search, so it must not surface as a report
                # failure.
                self._log.debug(f"Gate.io did not resolve {text!r} directly: {e}")
            else:
                report = await self._report_from_payload(product, payload, raw_symbol)
                # The identity is checked rather than assumed: this report is
                # about to be adopted as the venue's statement on an in-flight
                # order, and adopting the wrong venue order id for it would
                # address every later cancel and amend to somebody else's order.
                if report is not None and report.client_order_id == client_order_id:
                    return report

        start_secs, end_secs = self._window(None, None)
        for open_only in (True, False):
            reports = await self._order_reports_for_product(
                product,
                instrument_id,
                open_only=open_only,
                start_secs=start_secs,
                end_secs=end_secs,
            )
            for report in reports:
                if report.client_order_id == client_order_id:
                    return report

        self._log.warning(
            f"Gate.io holds no {product.value} order for {client_order_id!r} on {raw_symbol}: "
            f"neither the resting nor the finished listing carries its client id",
        )
        return None

    def _parse_order_status_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        instrument: Instrument | None,
        *,
        single_order: bool = False,
    ) -> OrderStatusReport | None:
        """Build an :class:`OrderStatusReport`, or refuse a payload it cannot read.

        ``ValueError`` — an unreadable deciding field, or a report the platform
        itself refuses to construct — propagates, enriched with the order id,
        because a batch that silently omits an order the venue reported is
        answering a different question than it was asked (REC-06): the listing
        callers turn it into :class:`OrderReportsUnavailable`. Anything else is
        an internal error, logged and skipped so one bug cannot fail a batch.

        ``single_order`` marks the one-order query path (the engine's inflight
        check), where the still-open spot cash market buy gets the honest
        quote-denominated answer the listings must not give — see
        :meth:`_parse_spot_order_fields`.
        """
        try:
            return self._build_order_status_report(
                product,
                payload,
                instrument,
                single_order=single_order,
            )
        except ValueError as e:
            raise ValueError(
                f"the {product.value} order {payload.get('id')!r} cannot be read: {e}",
            ) from e
        except Exception as e:  # noqa: BLE001 - an internal bug must not fail the batch
            self._log.warning(f"Cannot parse the order {payload.get('id')!r}: {e}")
            return None

    def _build_order_status_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        instrument: Instrument | None,
        *,
        single_order: bool = False,
    ) -> OrderStatusReport | None:
        raw_symbol = venue_symbol_of(payload)
        if not raw_symbol or instrument is None:
            self._log.error(
                f"Cannot build an order status report for {product.value} order "
                f"{payload.get('id')!r} on {raw_symbol or '<no symbol>'}: the instrument is "
                f"unknown, so this venue order cannot be reconciled",
            )
            return None

        venue_order_id_value = payload.get("id_string") or payload.get("id")
        if venue_order_id_value in (None, ""):
            return None
        venue_order_id = VenueOrderId(str(venue_order_id_value))
        client_order_id = self._client_order_id_for(payload.get("text"), venue_order_id)

        if product.is_spot:
            parsed = self._parse_spot_order_fields(
                payload,
                instrument,
                client_order_id,
                single_order=single_order,
            )
        else:
            parsed = self._parse_contract_order_fields(payload, instrument)
        if parsed is None:
            return None
        side, order_type, quantity, filled_qty, price = parsed

        time_in_force_value = str(payload.get("time_in_force") or payload.get("tif") or "gtc")
        post_only = time_in_force_value.lower() == GateioTimeInForce.POC.value
        # The average price decides money on a filled row: it is what the
        # engine puts on the inferred stand-in fill it mints for a
        # filled-quantity difference (installed live/execution_engine.py,
        # `_handle_fill_quantity_mismatch`), so a stated-and-unreadable value
        # may not collapse to the forgiving default — that priced a fabricated
        # execution from a value nobody stated (R8A-02). Absence stays the
        # smaller claim (an unfilled order states no average), and a readable
        # zero is reported as no average because zero is not a price.
        avg_px_value = payload.get("avg_deal_price") or payload.get("fill_price")
        avg_px: Decimal | None = None
        if filled_qty.as_decimal() > 0 and avg_px_value not in (None, ""):
            try:
                parsed_avg = to_exact_decimal(avg_px_value)
            except ValueError as e:
                raise ValueError(
                    f"the 'avg_deal_price'/'fill_price' field decides the answer: {e}",
                ) from None
            if parsed_avg > 0:
                avg_px = parsed_avg

        display_qty = None
        iceberg = payload.get("iceberg")
        if iceberg not in (None, "", 0, "0"):
            display_qty = instrument.make_qty(to_decimal(iceberg))

        ts_accepted = first_timestamp_ns(payload, "create_time_ms", "create_time")
        ts_last = (
            first_timestamp_ns(
                payload,
                "update_time_ms",
                "finish_time_ms",
                "update_time",
                "finish_time",
            )
            or ts_accepted
        )
        ts_init = self._clock.timestamp_ns()

        order_status, trigger_price, trigger_type, restate_trigger = self._fired_trigger_fields(
            venue_order_id,
            self._order_status(product, payload),
        )

        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument.id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=order_type,
            time_in_force=time_in_force_from_gateio(time_in_force_value),
            order_status=order_status,
            quantity=quantity,
            filled_qty=filled_qty,
            price=price,
            trigger_price=trigger_price,
            trigger_type=trigger_type,
            avg_px=avg_px,
            display_qty=display_qty,
            post_only=post_only,
            reduce_only=bool(payload.get("is_reduce_only") or payload.get("reduce_only")),
            report_id=UUID4(),
            ts_accepted=ts_accepted or ts_init,
            ts_last=ts_last or ts_init,
            ts_triggered=(ts_accepted or ts_init) if restate_trigger else None,
            ts_init=ts_init,
        )

    def _fired_trigger_fields(
        self,
        venue_order_id: VenueOrderId,
        order_status: OrderStatus,
    ) -> tuple[OrderStatus, Price | None, TriggerType, bool]:
        """Restate the report of an order one of this client's triggers fired.

        Gate.io splits a conditional order into two venue objects: the armed
        price order, which holds the trigger, and the ordinary order it creates
        when the trigger fires, which holds none of it. Reported exactly as the
        venue states it, that second object is an ACCEPTED limit order with no
        trigger price — while the local order is a STOP_LIMIT that has already
        been TRIGGERED. Both halves of the comparison then misfire on installed
        1.230.0, on every reconciliation pass, for as long as the order rests:

        * ``_handle_order_status_transitions`` (live/execution_engine.py:3253)
          calls ``_generate_order_accepted`` because the report says ACCEPTED and
          the order does not, and ``TRIGGERED -> ACCEPTED`` is absent from the
          state table (model/orders/base.pyx:110-157), so the event is dropped
          with a warning and the pass reconciles nothing;
        * ``_should_update`` (:3307-3318) compares ``report.trigger_price``
          against the order's for every stop type, and ``None`` never equals a
          price, so a reconciliation ``OrderUpdated`` claiming an amendment the
          venue never made is published to the message bus each time.

        The trigger fields are taken from the local order because the venue does
        not repeat them on the fired object, and the armed/fired link is this
        client's own record of which pair of venue ids is one Nautilus order —
        the same reading :meth:`_build_trigger_order_report` already does for the
        order type.

        ``TRIGGERED`` is reported only for the types the platform has that state
        for: ``_generate_order_triggered`` (:3644-3654) skips market-style stops
        outright, and reporting TRIGGERED for one of those would have the engine
        treat the report as reconciled while emitting nothing, so those keep the
        venue's own status and gain only the trigger price.

        ``ts_triggered`` is restated only while the local order has not recorded
        the trigger itself. The engine re-emits ``OrderTriggered`` from that field
        on the CANCELED and EXPIRED paths (:3281, :3294) without checking the
        order's current state, so repeating a trigger the order already applied
        buys a dropped event and a warning and nothing else; the field exists so
        that a trigger which fired while this client was down is not lost.
        """
        link = self._trigger_link_for_venue_order_id(venue_order_id.value)
        if link is None or link.fired_id != venue_order_id.value:
            return order_status, None, TriggerType.NO_TRIGGER, False

        order = self._cache.order(link.client_order_id)
        if order is None or not order.has_trigger_price:
            return order_status, None, TriggerType.NO_TRIGGER, False

        trigger_type = getattr(order, "trigger_type", TriggerType.NO_TRIGGER)
        if trigger_type == TriggerType.NO_TRIGGER:
            # `OrderStatusReport` refuses a trigger price without a trigger type,
            # so an order that somehow carries one without the other is reported
            # without either rather than not at all.
            return order_status, None, TriggerType.NO_TRIGGER, False

        if order_status == OrderStatus.ACCEPTED and order.order_type in TRIGGERABLE_ORDER_TYPES:
            order_status = OrderStatus.TRIGGERED

        return (
            order_status,
            order.trigger_price,
            trigger_type,
            getattr(order, "ts_triggered", 0) == 0,
        )

    def _parse_spot_order_fields(
        self,
        payload: dict[str, Any],
        instrument: Instrument,
        client_order_id: ClientOrderId | None = None,
        *,
        single_order: bool = False,
    ) -> tuple[OrderSide, OrderType, Quantity, Quantity, Price | None] | None:
        """Read the deciding fields of one spot or margin order row, strictly.

        Every field read here decides side, type, quantity or filled quantity,
        and the forgiving readers turned unreadable bytes into confident
        claims: a mangled ``side`` flipped BUY to SELL, a mangled ``type``
        routed a cash amount past the market-buy protection, and an unreadable
        ``amount`` silently erased the venue's order from the answer (REC-06).
        Unreadable raises; the listing callers turn that into a loud
        :class:`OrderReportsUnavailable`. An explicit readable zero stays
        believed.
        """
        what = f"spot order {payload.get('id')!r}"
        side = order_side_from_gateio(require_field(payload, "side", what=what))
        order_type = order_type_from_gateio(require_field(payload, "type", what=what))
        amount = exact_decimal_field(payload, "amount", what=what)
        filled = exact_decimal_field(payload, "filled_amount", what=what)

        if order_type == OrderType.MARKET and side == OrderSide.BUY:
            # `amount` is a quote-currency cash amount here, so the only
            # base-denominated quantity the venue ever publishes for this order
            # is the final filled amount — and that figure exists only once the
            # venue has finished working the order. While the row still reads
            # "open", `filled_amount` is a running partial: a report built from
            # it would state a quantity the venue never set, the engine would
            # restate the order to that partial figure, and every further match
            # would then be refused as an overfill (REC-04). So an unfinished
            # cash buy yields no order status report in a listing. Nothing is
            # lost by that: its executions are recovered from the trade
            # listing, and the order's own statement is taken from a re-read
            # once the venue has finished it (`_hand_over_fills_with_their_order`).
            # The status deciding all of that must itself be readable: a
            # mangled status falling through to the `filled` branch is the
            # restate-to-partial defect REC-04 closed.
            status = str(require_field(payload, "status", what=what) or "").lower()
            if status not in SPOT_ORDER_STATUSES:
                raise ValueError(
                    f"the {what}'s 'status' field decides whether a cash market buy has a "
                    f"base quantity yet, and {payload.get('status')!r} is not a spot order "
                    f"status",
                )
            if status == "open":
                if single_order:
                    quote_report = self._open_cash_buy_as_quote(
                        payload,
                        instrument,
                        client_order_id,
                        amount,
                        filled,
                    )
                    if quote_report is not None:
                        return quote_report
                self._log.debug(
                    "Skipping the report of a spot market buy Gate.io is still working: "
                    "no base-denominated quantity exists for it until it finishes",
                )
                return None
            if filled <= 0:
                self._log.debug(
                    "Skipping an unfilled spot market buy report: Gate.io publishes only a "
                    "quote-denominated amount for it",
                )
                return None
            amount = filled

        # `filled_amount` and `amount` are passed on as the venue states them,
        # base-currency fee and all. The report and the fills have to describe
        # the same order in the same units: `_should_update` (installed
        # live/execution_engine.py:3307) restates the order to `report.quantity`
        # whenever it differs, and `_handle_fill_quantity_mismatch` (:3164)
        # compares `report.filled_qty` against the sum of the fills, so a report
        # netted of a fee the fills are not would have the engine restating the
        # quantity on one pass and inferring a phantom fill on the next.
        if amount <= 0:
            # The venue affirmatively states a non-positive quantity, which no
            # OrderStatusReport can carry; believed, skipped, and never silent.
            self._log.warning(
                f"Skipping the {what} report: the venue states a non-positive amount {amount}",
            )
            return None
        price_value = payload.get("price")
        price = (
            instrument.make_price(exact_decimal_field(payload, "price", what=what))
            if order_type == OrderType.LIMIT and price_value not in (None, "")
            else None
        )
        return (
            side,
            order_type,
            instrument.make_qty(amount),
            instrument.make_qty(max(Decimal(0), min(filled, amount))),
            price,
        )

    def _open_cash_buy_as_quote(
        self,
        payload: dict[str, Any],
        instrument: Instrument,
        client_order_id: ClientOrderId | None,
        amount: Decimal,
        filled: Decimal,
    ) -> tuple[OrderSide, OrderType, Quantity, Quantity, Price | None] | None:
        """The one honest report for a cash buy the venue is still working.

        The engine's inflight check resolves an unacknowledged SUBMITTED order
        through ``generate_order_status_report``, and five consecutive ``None``
        answers fabricate ``OrderRejected(reason="UNKNOWN")`` for an order the
        venue holds (installed live/execution_engine.py:701-797) — so the
        single-order path may not stay silent just because no base quantity
        exists yet (R7C-03).

        The answer that cannot lie states the order in the venue's own units:
        ACCEPTED, quantity = the quote cash amount, filled 0. That is only
        honest while the local order is still quote-denominated with nothing
        filled on either side — then ``report.quantity == order.quantity`` and
        the engine's ``_should_update`` finds nothing to restate, which is the
        REC-04 constraint (never state the quote amount as base). Once either
        side has a fill the units diverge and the answer is silence again; by
        then the order has left the inflight check's SUBMITTED state anyway.
        """
        if client_order_id is None or filled > 0:
            return None
        order = self._cache.order(client_order_id)
        if (
            order is None
            or not order.is_quote_quantity
            or order.filled_qty.as_decimal() > 0
            or order.quantity.as_decimal() != amount
        ):
            return None
        return (
            OrderSide.BUY,
            OrderType.MARKET,
            instrument.make_qty(amount),
            instrument.make_qty(Decimal(0)),
            None,
        )

    def _parse_contract_order_fields(
        self,
        payload: dict[str, Any],
        instrument: Instrument,
    ) -> tuple[OrderSide, OrderType, Quantity, Quantity, Price | None] | None:
        """Read the deciding fields of one futures/delivery/options order row, strictly.

        ``size`` decides the side and the quantity, ``left`` decides the
        filled quantity, ``price`` decides both the price and the order type.
        The forgiving readers defaulted every one of them to 0, and through
        the installed engine those defaults crossed the bar (REC-06): an
        unreadable ``left`` became a confident full fill the engine closed
        with a fabricated execution while the venue holds the order open
        (CZ-1), an unreadable ``left`` on a venue-canceled order reported it
        FILLED (CZ-3), and an unreadable ``size`` collapsed into the genuine
        close-position zero so the report vanished behind the same debug line
        (CZ-6). Unreadable raises; the listing callers answer
        :class:`OrderReportsUnavailable`. The one believed zero is the value
        the payload explicitly states as 0 — the close-position order, which
        genuinely has no quantity of its own. Fractional sizes raise too:
        decimal-sized (`enable_decimal`) contracts are not supported by this
        client, and truncating them misstates the quantity silently.
        """
        what = f"{payload.get('contract') or 'contract'} order {payload.get('id')!r}"
        size = exact_lots(payload, "size", what=what)
        if size == 0:
            # A close-position order carries no quantity of its own; this 0 is
            # the venue's affirmative statement, read exactly, not a default.
            self._log.debug("Skipping a close-position order report with size 0")
            return None
        side = OrderSide.BUY if size > 0 else OrderSide.SELL
        quantity = abs(size)
        left = abs(exact_lots(payload, "left", what=what))
        filled = max(0, quantity - left)

        price_value = optional_exact_decimal(payload, "price", what=what)
        is_market = price_value <= 0
        price = None if is_market else instrument.make_price(price_value)
        order_type = OrderType.MARKET if is_market else OrderType.LIMIT
        return (
            side,
            order_type,
            Quantity(quantity, instrument.size_precision),
            Quantity(filled, instrument.size_precision),
            price,
        )

    async def _parse_trigger_order_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        link: GateioTriggerLink | None,
    ) -> OrderStatusReport | None:
        """Build a report for an armed price-triggered order.

        Only orders still waiting for their trigger are reported: once one fires
        the order it created is a normal order and is reported as such.
        """
        try:
            return await self._build_trigger_order_report(product, payload, link)
        except ValueError as e:
            # Same discipline as the regular order rows (REC-06): a deciding
            # field the venue stated and this client cannot read fails the
            # listing. An armed order adopted with a defaulted side or size
            # would be a wrong order in the cache, and one silently omitted is
            # invisible to reconciliation — and, with the open-order check's
            # missing-at-venue resolution enabled, resolvable into a
            # fabricated rejection.
            raise ValueError(
                f"the {product.value} price-triggered order {payload.get('id')!r} cannot "
                f"be read: {e}",
            ) from e
        except Exception as e:  # noqa: BLE001 - an internal bug must not fail the batch
            self._log.warning(f"Cannot parse the price-triggered order {payload.get('id')!r}: {e}")
            return None

    async def _build_trigger_order_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        link: GateioTriggerLink | None,
    ) -> OrderStatusReport | None:
        status = str(payload.get("status") or "").lower()
        if status not in ARMED_TRIGGER_STATUSES:
            return None

        trigger = payload.get("trigger") or {}
        initial = payload.get("initial") or payload.get("put") or {}
        raw_symbol = str(
            payload.get("market") or initial.get("contract") or venue_symbol_of(payload) or "",
        )
        if not raw_symbol:
            self._log.error(
                f"Discarding a {product.value} price-order report without a symbol: "
                f"id {payload.get('id')!r}",
            )
            return None
        instrument = await self._instrument_or_load(
            gateio_to_instrument_id(product, raw_symbol),
        )
        if instrument is None:
            return None

        trigger_id_value = payload.get("id_string") or payload.get("id")
        if trigger_id_value in (None, ""):
            return None
        trigger_id = str(trigger_id_value)
        venue_order_id = VenueOrderId(trigger_id)
        client_order_id = link.client_order_id if link is not None else None

        what = f"price order {trigger_id}"
        if product.is_spot:
            side = order_side_from_gateio(require_field(initial, "side", what=what))
            quantity = instrument.make_qty(exact_decimal_field(initial, "amount", what=what))
            is_limit = str(initial.get("type") or "limit").lower() == "limit"
            price_value = optional_exact_decimal(initial, "price", what=what)
            time_in_force_value = str(initial.get("time_in_force") or "gtc")
        else:
            size = exact_lots(initial, "size", what=what)
            if size == 0:
                self._log.warning(
                    f"Skipping the {what} report: the venue states a size of 0",
                )
                return None
            side = OrderSide.BUY if size > 0 else OrderSide.SELL
            quantity = Quantity(abs(size), instrument.size_precision)
            price_value = optional_exact_decimal(initial, "price", what=what)
            is_limit = price_value > 0
            time_in_force_value = str(initial.get("tif") or "gtc")

        order = self._cache.order(client_order_id) if client_order_id is not None else None
        if order is not None and order.order_type in CONDITIONAL_ORDER_TYPES:
            order_type = order.order_type
        elif is_limit:
            order_type = OrderType.STOP_LIMIT
        else:
            order_type = OrderType.STOP_MARKET

        trigger_price_value = exact_decimal_field(trigger, "price", what=what)
        if trigger_price_value <= 0:
            self._log.warning(
                f"Skipping the {what} report: the venue states a trigger price of "
                f"{trigger_price_value}, which no report can carry",
            )
            return None

        ts_accepted = first_timestamp_ns(payload, "create_time", "ctime")
        ts_init = self._clock.timestamp_ns()
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument.id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=order_type,
            time_in_force=time_in_force_from_gateio(time_in_force_value),
            order_status=OrderStatus.ACCEPTED,
            quantity=quantity,
            filled_qty=Quantity(0, instrument.size_precision),
            price=instrument.make_price(price_value) if is_limit and price_value > 0 else None,
            trigger_price=instrument.make_price(trigger_price_value),
            trigger_type=_trigger_type(trigger.get("price_type")),
            reduce_only=bool(initial.get("reduce_only")),
            report_id=UUID4(),
            ts_accepted=ts_accepted or ts_init,
            ts_last=ts_accepted or ts_init,
            ts_init=ts_init,
        )

    # -- reconciliation: fills ---------------------------------------------

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        """Generate fill reports across every enabled product.

        ``command.venue_order_id`` narrows the answer to one order's executions.
        It is a filter the platform declares on the command
        (``GenerateFillReports``, installed execution/messages.pyx:338-382) and
        that in-tree adapters set when they re-read the trades of a single order,
        so a client that ignored it would answer a question about one order with
        every fill in the window — and a caller grouping those under that order's
        status report would attach executions the venue booked against other
        orders. Gate.io takes the same narrowing server-side on spot
        (``order_id``) and on futures and delivery (``order``); options has no
        such parameter, so the filter is applied to the parsed reports as well,
        which is what makes the guarantee hold on every product.

        A product whose trade listing failed makes this method raise
        :class:`FillReportsUnavailable`, carrying everything the other products
        did answer. Returning the partial list instead — which is what logging
        and continuing amounts to — is indistinguishable from "the venue reports
        no such trades", and the engine's only defence against squaring a
        position to flat on a failed query is that this call raises: it sets
        ``had_fill_query_errors`` in ``_query_and_find_missing_fills`` from the
        exception and from nothing else. Swallowing the failure there costs a
        real closing trade, permanently, because the squared position is no
        longer open and is never queried again.

        A wallet Gate.io has not created is not a failure and does not raise:
        ``USER_NOT_FOUND`` means the ledger does not exist, so it holds no
        trades, which is a definite answer of none.
        """
        reports: list[FillReport] = []
        failures: list[str] = []
        start_secs, end_secs = self._window(command.start, command.end)

        for product in self._products:
            try:
                reports += await self._fill_reports_for_product(
                    product,
                    command.instrument_id,
                    start_secs,
                    end_secs,
                    command.venue_order_id,
                )
            except WalletNotProvisionedError as e:
                self._log.warning(f"Skipping {product.value} fill reports: {e}")
            except asyncio.CancelledError:
                raise
            except FillReportsUnavailable as e:
                # A product whose listing held unreadable rows: keep what it
                # did answer readably, and keep the failure.
                self._log_report_error(e, f"{product.value} FillReports")
                reports += e.reports
                failures.append(f"{product.value}: {e}")
            except Exception as e:  # noqa: BLE001 - reported below, after every product
                self._log_report_error(e, f"{product.value} FillReports")
                failures.append(f"{product.value}: {e}")

        if command.venue_order_id is not None:
            reports = [
                report for report in reports if report.venue_order_id == command.venue_order_id
            ]

        # Reconciliation applies fills in list order, so ordering is load-bearing.
        reports.sort(key=lambda report: (report.ts_event, report.trade_id.value))
        self._log_report_receipt(len(reports), "FillReport", LogLevel.INFO)

        if failures:
            raise FillReportsUnavailable(
                f"Gate.io did not answer the trade listing"
                f"{f' for {command.instrument_id}' if command.instrument_id is not None else ''}: "
                f"{'; '.join(failures)}",
                reports,
            )
        return reports

    async def _fill_reports_for_product(
        self,
        product: GateioProductType,
        instrument_id: InstrumentId | None,
        start_secs: int,
        end_secs: int,
        venue_order_id: VenueOrderId | None = None,
    ) -> list[FillReport]:
        symbol: str | None = None
        if instrument_id is not None:
            resolved = self._resolve(instrument_id)
            if resolved is None or resolved[0] is not product:
                return []
            symbol = resolved[1]

        order_id = venue_order_id.value if venue_order_id is not None else None

        payloads: list[dict[str, Any]] = []
        if product.is_spot:
            symbols = [symbol] if symbol else sorted(self._active_symbols(product)) or [None]
            for pair in symbols:
                payloads += await self._collect_pages(
                    # Gate.io refuses `order_id` without `currency_pair`, so an
                    # unscoped sweep keeps asking broadly and narrows afterwards.
                    lambda page, pair=pair: self._spot_http.my_trades(
                        pair=pair,
                        order_id=order_id if pair else None,
                        limit=REPORT_PAGE_LIMIT,
                        page=page + 1,
                        frm=start_secs,
                        to=end_secs,
                    ),
                    description=f"spot fills on {pair or 'all pairs'}",
                )
        elif product.is_option:
            for underlying in self._option_underlyings(symbol):
                payloads += await self._collect_pages(
                    lambda page, underlying=underlying: self._options_http.my_trades(
                        underlying=underlying,
                        contract=symbol,
                        limit=REPORT_PAGE_LIMIT,
                        offset=page * REPORT_PAGE_LIMIT,
                        frm=start_secs,
                        to=end_secs,
                    ),
                    description=f"options fills on {underlying}",
                    wallet="the options fills",
                )
        else:
            # The futures and delivery fill endpoints accept no time range at
            # all, so the window has to be walked with the row offset until a
            # page reaches back past its start.
            payloads += await self._collect_pages(
                lambda page: self._futures_api(product).my_trades(
                    contract=symbol,
                    order=order_id,
                    limit=REPORT_PAGE_LIMIT,
                    offset=page * REPORT_PAGE_LIMIT,
                ),
                description=f"{product.value} fills",
                wallet=f"the {product.value} fills",
                stop_after=lambda rows: _oldest_ts_before(rows, start_secs),
            )

        reports: list[FillReport] = []
        row_failures: list[str] = []
        for payload in payloads:
            raw_symbol = venue_symbol_of(payload)
            if not raw_symbol:
                self._log.error(
                    f"Discarding a {product.value} fill report without a symbol: "
                    f"trade id {payload.get('id')!r}",
                )
                continue
            instrument = await self._instrument_or_load(
                gateio_to_instrument_id(product, raw_symbol),
            )
            if instrument is None:
                continue  # `_instrument_or_load` has already logged the loss
            try:
                report = self._parse_fill_report(product, payload, instrument)
            except ValueError as e:
                # A row the venue stated and this client cannot read: the rest
                # of the listing is still parsed — a caller that can use a
                # loud partial answer gets it off the exception — but the
                # listing as a whole has failed (REC-06).
                row_failures.append(str(e))
                continue
            if report is None:
                continue
            if not _within(report.ts_event, start_secs, end_secs):
                continue
            reports.append(report)
        if row_failures:
            raise FillReportsUnavailable(
                f"Gate.io answered the {product.value} trade listing with rows this client "
                f"cannot read: {'; '.join(row_failures)}",
                reports,
            )
        return reports

    def _option_underlyings(self, symbol: str | None) -> list[str]:
        """Return the option underlyings to query, since Gate.io requires one."""
        if symbol:
            try:
                underlying, _, _, _ = parse_option_symbol(symbol)
                return [underlying]
            except ValueError:
                return []
        configured = self._config.options_underlyings
        if configured:
            return [u.upper() for u in configured]

        underlyings: set[str] = set()
        for raw_symbol in self._active_symbols(GateioProductType.OPT):
            try:
                underlying, _, _, _ = parse_option_symbol(raw_symbol)
            except ValueError:
                continue
            underlyings.add(underlying)
        if not underlyings:
            self._log.warning(
                "Cannot query option fills: Gate.io requires an underlying and none is "
                "configured (`options_underlyings`) or implied by an open position",
            )
        return sorted(underlyings)

    def _parse_fill_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        instrument: Instrument,
    ) -> FillReport | None:
        """Build a :class:`FillReport` from one Gate.io fill object, strictly.

        Every deciding field of an execution — identity, side, quantity, price,
        fee, time — raises when the payload states it and this client cannot
        read it (REC-06). A silently dropped row is a lost execution: the
        engine believes the listing was complete, replaces the missing
        quantity with a commission-less inferred fill, and the venue's trade
        id — the only key by which a later replay could be recognised — is
        gone (CZ-2, CZ-4, CZ-5). The listing caller turns the raise into
        :class:`FillReportsUnavailable`, which is the one signal that arms the
        engine's brake against squaring positions on an incomplete answer.

        ``None`` is returned only for a row whose readable content states no
        execution — a zero size or amount the venue affirmatively published —
        and never silently.
        """
        what = f"{product.value} fill {payload.get('id')!r}"
        trade_id_value = require_field(payload, "id", what=what)
        order_id_value = payload.get("order_id") or payload.get("order")
        if trade_id_value in (None, ""):
            raise ValueError(f"the {what} states no venue trade id")
        if order_id_value in (None, ""):
            raise ValueError(f"the {what} states no venue order id")

        last_px = instrument.make_price(exact_decimal_field(payload, "price", what=what))
        if product.is_spot:
            side = order_side_from_gateio(require_field(payload, "side", what=what))
        else:
            size = exact_lots(payload, "size", what=what)
            if size == 0:
                self._log.warning(
                    f"Skipping the {what}: the venue states a size of 0, which is not an execution",
                )
                return None
            side = OrderSide.BUY if size > 0 else OrderSide.SELL

        last_qty, commission = self._fill_quantity_and_commission(product, payload, instrument)
        if last_qty.as_decimal() <= 0:
            self._log.warning(
                f"Skipping the {what}: the venue states a quantity of {last_qty}, which is "
                f"not an execution",
            )
            return None

        venue_order_id = VenueOrderId(str(order_id_value))
        # The execution time orders the fills the engine applies and feeds the
        # staleness memory; stated-but-unreadable raises, and a row stating no
        # time at all cannot be ordered honestly, so it fails the same way.
        ts_event = exact_first_timestamp_ns(
            payload,
            "create_time_ms",
            "create_time",
            "time_ms",
            "time",
        )
        if not ts_event:
            raise ValueError(f"the {what} states no readable execution time")
        return FillReport(
            account_id=self.account_id,
            instrument_id=instrument.id,
            venue_order_id=venue_order_id,
            client_order_id=self._client_order_id_for(payload.get("text"), venue_order_id),
            trade_id=TradeId(str(trade_id_value)),
            order_side=side,
            last_qty=last_qty,
            last_px=last_px,
            commission=commission,
            liquidity_side=liquidity_side_from_gateio(payload.get("role")),
            report_id=UUID4(),
            ts_event=ts_event,
            ts_init=self._clock.timestamp_ns(),
        )

    # -- reconciliation: positions -----------------------------------------

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        """Report the venue's positions, or say the venue did not answer.

        Every branch below turns on one distinction: *did the venue make a
        statement about this instrument?* NautilusTrader has exactly three ways
        to hear an answer, and they are not interchangeable:

        ``PositionStatusReport``
            The venue says the position is this. The engine will square the local
            book against it, minting a RECONCILIATION order and an inferred fill
            if they disagree.
        ``[]`` (this instrument omitted)
            Nothing is claimed. On the per-instrument query the engine simply has
            no report and leaves the position alone; in an *account-wide* answer,
            though, the engine reads omission as flatness, which is why the
            account-wide branch below refuses to omit silently.
        raising
            The venue was asked and did not answer. This is the only signal the
            engine has for it — ``LiveExecutionEngine._did_position_status_query_fail``
            skips the venue's cached positions entirely, and the startup path
            counts the raise as a failed reconciliation rather than reconciling
            against nothing.

        Swallowing a query failure collapses the third case into the second, and
        the second into the first wherever the FLAT fallback or the engine's own
        ``_create_flat_position_report`` is reached — which is how "the venue was
        unreachable" ends up closing a position that is still open, through an
        execution that never happened.
        """
        reports: list[PositionStatusReport] = []
        requested: InstrumentId | None = command.instrument_id
        failures: list[BaseException] = []

        for product in self._products:
            if product.is_spot:
                continue  # Spot balances are not positions; nothing to query
            try:
                reports += await self._position_reports_for_product(product, requested)
            except WalletQueryRefusedError as e:
                # The venue was asked and would not answer: the key lacks the
                # permission, or the account is not in the mode the endpoint
                # needs. Nothing follows about what the ledger holds, so this
                # client declines to speak for the product at all — the same
                # refusal `_refuse_incomplete_account_sweep` makes below for the
                # products it structurally cannot route. Reading it as "no
                # position here" is how it would reach the FLAT fallback and
                # close a position that is still open.
                self._log_report_error(e, f"{product.value} PositionStatusReports")
                failures.append(e)
            except WalletNotProvisionedError as e:
                # A wallet Gate.io has not created yet holds no position: the
                # venue creates it on the first transfer in and says
                # USER_NOT_FOUND until then, which is a definite absence rather
                # than an unanswered question. This clause must stay *below* the
                # refusal clause, which is a subclass of it.
                self._log.warning(f"Skipping {product.value} position reports: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - re-raised below, after every product
                self._log_report_error(e, f"{product.value} PositionStatusReports")
                failures.append(e)

        if failures:
            raise PositionStatusUnavailable(
                f"Gate.io did not answer the position query"
                f"{f' for {requested}' if requested is not None else ''}: "
                f"{'; '.join(str(e) for e in failures)}",
            ) from failures[0]

        if requested is not None:
            if not self._has_venue_positions(requested):
                # Startup reconciliation asks this client, one instrument at a
                # time, about every open position the cache holds — and Nautilus
                # opens a position from a spot fill like any other. Answering
                # FLAT would be a statement about the venue, and for spot there
                # is no venue-side statement to make: Gate.io has no spot
                # position endpoint, a spot balance is a balance and not a
                # position, and the loop above queried nothing for it. Answering
                # with nothing says "not applicable here" and leaves the position
                # the venue's own fills produced alone.
                self._log.debug(
                    f"Not reporting a position for {requested}: Gate.io keeps no venue-side "
                    f"position for this product, so this client claims no authority over it",
                )
            elif not reports:
                # The venue really was asked and really did say "no position", so
                # an explicit FLAT report is a statement it made. Without it a
                # derivative position closed at the venue could never be closed
                # locally. The one exception: an absent row cannot contain venue
                # trades this recovery just booked, and it carries no timestamp
                # that could show it postdates them — so while the answer is
                # exactly the pre-trade book, it is a stale read, and asserting
                # FLAT from it would have the engine square away the very trades
                # the trade listing named.
                if self._position_answer_is_stale(requested, Decimal(0), 0):
                    raise PositionStatusUnavailable(
                        f"Gate.io lists no {requested} position, but this recovery pass "
                        f"booked venue trades there that an absent row cannot contain; "
                        f"the answer was read before those trades and is stale, not flat",
                    )
                instrument = self._instrument(requested)
                if instrument is not None:
                    reports.append(
                        PositionStatusReport.create_flat(
                            account_id=self.account_id,
                            instrument_id=requested,
                            size_precision=instrument.size_precision,
                            ts_init=self._clock.timestamp_ns(),
                        ),
                    )
        else:
            self._refuse_incomplete_account_sweep()

        for report in reports:
            # The same stale-read test, for a row the venue did send: a row that
            # neither contains the trades this recovery just booked nor is
            # stamped after them is a read from before those trades, and the
            # only honest answer built on it is "the query is not answered yet".
            # The stamp judged is the venue's own (0 when the row stated none);
            # `report.ts_last` falls back to local now, which would read as
            # fresher than any booked trade (R7C-02).
            if self._position_answer_is_stale(
                report.instrument_id,
                report.signed_decimal_qty,
                self._position_row_venue_ts.get(report.instrument_id, 0),
            ):
                raise PositionStatusUnavailable(
                    f"The {report.instrument_id} position row does not contain venue "
                    f"trades this recovery just booked and is not stamped after them; "
                    f"it is a stale read, not a statement that those trades did not "
                    f"happen",
                )

        self._log_report_receipt(len(reports), "PositionStatusReport", command.log_receipt_level)
        return reports

    def _refuse_incomplete_account_sweep(self) -> None:
        """Raise rather than let omission be read as flatness on an account-wide query.

        The engine's periodic position check
        (``LiveExecEngineConfig.position_check_interval_secs``) asks account-wide
        and then walks *its own* cached open positions: for any that the answer
        did not mention it builds the FLAT report itself
        (``LiveExecutionEngine._process_cached_position_discrepancies`` ->
        ``_create_flat_position_report``) and squares the book against it. To
        that caller "I returned no report for it" and "the venue says it is flat"
        are the same answer, so returning a list that omits an instrument this
        client cannot speak for hands the engine a claim nobody made.

        A list cannot express partial authority, so the only honest answer is the
        one Nautilus already understands: the query failed. It is deliberately
        scoped to *open* positions this client cannot report on, so a
        derivatives-only account — or a mixed one that holds no spot position at
        the time — keeps a fully working periodic check. While a spot position is
        open, the check pauses for this venue instead of destroying it; startup
        reconciliation is unaffected, because it asks per instrument, where
        omission means nothing.
        """
        unanswerable = sorted(
            {
                position.instrument_id.value
                for position in self._cache.positions_open(venue=GATEIO_VENUE)
                if not self._has_venue_positions(position.instrument_id)
            },
        )
        if not unanswerable:
            return
        raise PositionStatusUnavailable(
            f"This client cannot state an account-wide position for {', '.join(unanswerable)}: "
            f"Gate.io publishes no venue-side position for them, and an answer that merely "
            f"omitted them would be read as a claim that they are flat",
        )

    def _routable_product(self, instrument_id: InstrumentId) -> GateioProductType | None:
        """Return the configured product that routes ``instrument_id``, if any."""
        resolved = self._safe_resolve(instrument_id)
        if resolved is None or resolved[0] not in self._products:
            return None
        return resolved[0]

    def _has_venue_positions(self, instrument_id: InstrumentId) -> bool:
        """Return whether Gate.io keeps a venue-side position for this instrument.

        Only the derivative products do, and only when this client is configured
        to route them. Spot is a cash ledger: buying credits base currency to a
        wallet, and no endpoint will ever answer "your spot position in BTC_USDT
        is X". An instrument this client does not route is treated the same way —
        the product loop never queried anything for it, so a FLAT answer would be
        a claim built on no question at all.
        """
        product = self._routable_product(instrument_id)
        return product is not None and not product.is_spot

    async def _position_reports_for_product(
        self,
        product: GateioProductType,
        instrument_id: InstrumentId | None,
    ) -> list[PositionStatusReport]:
        symbol: str | None = None
        if instrument_id is not None:
            resolved = self._resolve(instrument_id)
            if resolved is None or resolved[0] is not product:
                return []
            symbol = resolved[1]

        if product.is_option:
            payloads = await require_wallet(
                self._options_http.positions(contract=symbol),
                "the options positions",
            )
        elif symbol is not None:
            payloads = await require_wallet(
                self._futures_api(product).position(symbol),
                f"the {product.value} position",
            )
        else:
            payloads = await require_wallet(
                self._futures_api(product).positions(holding=True),
                f"the {product.value} positions",
            )

        entries = payloads if isinstance(payloads, list) else [payloads]
        reports: list[PositionStatusReport] = []
        for index, payload in enumerate(entries):
            # A row that cannot be read is not a row that says nothing. Dropping
            # it leaves this method returning fewer reports than the venue sent —
            # and one caller above reads "no report for this instrument" as an
            # explicit FLAT while the other lets the engine build the same flat
            # report itself, so a row the parser choked on ends up squaring a
            # live position with an inferred fill. What the venue said is
            # unknown; that is a failed query, and the only thing this client can
            # honestly do with it is say so.
            if not isinstance(payload, dict):
                raise PositionStatusUnavailable(
                    f"Gate.io answered the {product.value} position query with a row "
                    f"({index + 1} of {len(entries)}) this client cannot read: "
                    f"{type(payload).__name__} where a position object was expected. "
                    f"Nothing follows about what the ledger holds",
                )
            try:
                report = await self._parse_position_report(product, payload)
            except ValueError as e:
                # A field that decides the answer could not be read. This is
                # kept apart from the `None` branch below so the raise can name
                # the field and the value: "size was the string '-0.5'" sends
                # an operator to the right place, "cannot read" does not.
                raise PositionStatusUnavailable(
                    f"Gate.io answered the {product.value} position query with a row "
                    f"({index + 1} of {len(entries)}) this client cannot read: {e}. "
                    f"Nothing follows about what the ledger holds",
                ) from e
            if report is None:
                raise PositionStatusUnavailable(
                    f"Gate.io answered the {product.value} position query with a row "
                    f"({index + 1} of {len(entries)}) this client cannot read. Nothing "
                    f"follows about what the ledger holds",
                )
            reports.append(report)
        return reports

    async def _parse_position_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
    ) -> PositionStatusReport | None:
        """Build a :class:`PositionStatusReport` from one Gate.io position.

        ``None`` means the row could not be read, never "there is no position
        here": a position of zero is a report like any other, with
        ``PositionSide.FLAT``. An unreadable *deciding field* raises
        ``ValueError`` naming the field and the value instead, so the caller
        can say which row failed and why. Either way the caller turns the
        outcome into a failed query, because a row that was not read supports
        no claim at all.
        """
        raw_symbol = venue_symbol_of(payload)
        if not raw_symbol:
            self._log.error(
                f"Discarding a {product.value} position report without a symbol",
            )
            return None
        instrument = await self._instrument_or_load(
            gateio_to_instrument_id(product, raw_symbol),
        )
        if instrument is None:
            return None  # `_instrument_or_load` has already logged the loss

        # `size` is the one field that decides both the side and the quantity,
        # so it is read strictly. The forgiving `to_int` used elsewhere answers
        # 0 for a missing key, null, an empty string, a non-numeric string and
        # any magnitude truncating below one lot — and 0 here is not a default,
        # it is the affirmative claim FLAT, which the engine acts on by closing
        # the local position with an invented execution. Gate.io moved every
        # futures size field from integer to string in v4.106.0, so a shape
        # this client cannot read is a live possibility, not a hypothesis.
        if "size" not in payload:
            raise ValueError(f"the {raw_symbol} row has no 'size' field")
        try:
            size = to_lot_count(payload["size"])
        except ValueError as e:
            raise ValueError(
                f"the {raw_symbol} row's 'size' field decides the answer: {e}"
            ) from None
        if size > 0:
            side = PositionSide.LONG
        elif size < 0:
            side = PositionSide.SHORT
        else:
            side = PositionSide.FLAT

        entry_price = to_decimal(payload.get("entry_price"))
        ts_last = first_timestamp_ns(payload, "update_time", "time_ms", "time")
        # What the staleness rule judges is the venue's own stamp, recorded
        # exactly as stated — 0 when the row stated none. The report's
        # `ts_last` below falls back to local now because the platform needs a
        # real timestamp on the report, but local now is a stamp this client
        # fabricated: it postdates any booked trade by construction, so
        # feeding it to the rule would silently bypass the stale-answer
        # protection (R7C-02).
        self._position_row_venue_ts[instrument.id] = ts_last
        ts_init = self._clock.timestamp_ns()
        return PositionStatusReport(
            account_id=self.account_id,
            instrument_id=instrument.id,
            position_side=side,
            quantity=Quantity(abs(size), instrument.size_precision),
            avg_px_open=entry_price if entry_price > 0 else None,
            report_id=UUID4(),
            ts_last=ts_last or ts_init,
            ts_init=ts_init,
        )

    # -- window helper -----------------------------------------------------

    def _window(self, start: Any, end: Any) -> tuple[int, int]:
        """Return the ``(from, to)`` Unix-second window for a report command."""
        now_secs = int(self._clock.timestamp_ns() // 1_000_000_000)
        end_secs = int(end.timestamp()) if end is not None else now_secs
        start_secs = (
            int(start.timestamp()) if start is not None else end_secs - DEFAULT_LOOKBACK_SECS
        )
        return start_secs, max(end_secs, start_secs)


def _accumulate(
    balances: dict[str, tuple[Decimal, Decimal]],
    currency: str,
    total: Decimal,
    free: Decimal,
) -> None:
    """Add a wallet's contribution to the aggregated per-currency balance."""
    previous_total, previous_free = balances.get(currency, (Decimal(0), Decimal(0)))
    balances[currency] = (previous_total + total, previous_free + max(Decimal(0), free))


def _is_cross_margin(position: dict[str, Any]) -> bool:
    """Return whether a Gate.io futures position is held under cross margin.

    ``leverage`` carries the isolated leverage, and Gate.io documents ``"0"`` as
    the cross-margin marker: "leverage for isolated margin. 0 means cross margin.
    For leverage of cross margin, please refer to `cross_leverage_limit`." A
    payload with no ``leverage`` at all says nothing about the mode, and isolated
    is the safer reading of silence: it keeps the margin attributed to the one
    instrument it was observed on rather than pooling it across the account.
    """
    value = position.get("leverage")
    if value in (None, ""):
        return False
    return to_decimal(value) == 0


def _merge_margin(
    margins: dict[InstrumentId | Currency, MarginBalance],
    margin: MarginBalance,
) -> None:
    """Fold one margin entry into the set, keyed by the scope it belongs to.

    Account-wide entries are keyed by collateral currency, exactly as
    ``MarginAccount`` stores them (accounting/accounts/margin.pyx:511-521), so
    several of them can coexist — a USDT-settled perpetual, a BTC-settled
    inverse and the options wallet are three separate collaterals on one Gate.io
    account. Keying them all under ``None`` would silently keep only the last.
    """
    key: InstrumentId | Currency = (
        margin.currency if margin.instrument_id is None else margin.instrument_id
    )
    existing = margins.get(key)
    if existing is None:
        margins[key] = margin
        return
    margins[key] = MarginBalance(
        initial=existing.initial + margin.initial,
        maintenance=existing.maintenance + margin.maintenance,
        instrument_id=margin.instrument_id,
    )


def _within(ts_ns: int, start_secs: int, end_secs: int) -> bool:
    """Return whether a nanosecond timestamp falls inside a second-resolution window."""
    if not ts_ns:
        return True
    seconds = ts_ns / 1_000_000_000
    return start_secs <= seconds <= end_secs + 1


def _oldest_ts_before(rows: list[dict[str, Any]], start_secs: int) -> bool:
    """Return whether a page already reaches back past the start of the window.

    Used to stop paging endpoints that accept no time range: once a page holds a
    row older than the requested window, every further page is older still.
    """
    for row in rows:
        ts_ns = first_timestamp_ns(row, "create_time_ms", "create_time", "time_ms", "time")
        if ts_ns and ts_ns / 1_000_000_000 < start_secs:
            return True
    return False


def _fired_order_id(payload: dict[str, Any]) -> str | None:
    """Return the id of the order a price-triggered order created, if it fired.

    Spot publishes it as ``fired_order_id`` on the price order; futures and
    delivery publish it as ``trade_id``, which is ``0`` or absent until the
    trigger fires. ``me_order_id`` is deliberately not consulted: on the REST
    model it identifies the order a take-profit/stop-loss is attached to, which
    is a different relationship.
    """
    for key in ("fired_order_id", "trade_id"):
        value = payload.get(key)
        if value in (None, "", 0, "0"):
            continue
        return str(value)
    return None


def _trigger_type(price_type: Any) -> TriggerType:
    """Map a Gate.io futures ``trigger.price_type`` onto a Nautilus trigger type."""
    value = to_int(price_type, default=0)
    if value == 1:
        return TriggerType.MARK_PRICE
    if value == 2:
        return TriggerType.INDEX_PRICE
    return TriggerType.LAST_PRICE


__all__ = [
    "CONDITIONAL_ORDER_TYPES",
    "FILLABLE_TERMINAL_STATUSES",
    "SUPPORTED_ORDER_TYPES",
    "TRIGGERABLE_ORDER_TYPES",
    "GateioExecutionClient",
    "GateioTriggerLink",
    "trigger_rule",
]
