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
spot market buy) is rejected with an explicit reason rather than silently
altered.

Sizes on futures, delivery and options are **contract counts**: the Nautilus
``Quantity`` is the number of contracts and is sent as a signed integer, positive
for a buy and negative for a sell.

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, Final

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor, LogLevel
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
    SubmitOrder,
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
    OrderValidationError,
    UnsupportedOrderError,
    WalletNotProvisionedError,
)
from nautilus_gateio.common.parsing import timestamp_to_nanos, to_decimal, to_float, to_int
from nautilus_gateio.common.signing import generate_client_order_id
from nautilus_gateio.common.symbols import (
    gateio_to_instrument_id,
    instrument_id_to_gateio,
    parse_option_symbol,
)
from nautilus_gateio.config import GateioExecClientConfig, validate_products
from nautilus_gateio.http.client import GateioHttpClient
from nautilus_gateio.http.futures import GateioFuturesHttpAPI
from nautilus_gateio.http.margin import GateioMarginHttpAPI, require_wallet
from nautilus_gateio.http.options import GateioOptionsHttpAPI
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.http.wallet import GateioWalletHttpAPI
from nautilus_gateio.websocket.private import ALL, GateioPrivateWebSocket


@dataclass
class SpotQuantity:
    """What a live spot order's quantity is made of, tracked apart from the order.

    A Gate.io spot BUY is charged its fee in the currency being bought, so the
    venue credits ``amount - fee`` base units for a match of ``amount``. The
    fills this client publishes are netted of that fee (EXEC-4), so a fully
    matched order's fills add up to less than the quantity it was submitted with,
    and the order has to be restated or it never leaves ``cache.orders_open()``.

    The restatement is *not* a second opinion about the order's quantity: it is
    ``gross - withheld``, and the two move for entirely unrelated reasons.
    ``gross`` is the venue's own order size and changes only when the venue says
    so — a spot amend, which ``_modify_order`` supports and another session can
    make at any time. ``withheld`` only ever grows, by whatever the venue kept on
    each fill. Keeping them apart is what lets an amend and the fee compose:
    what is still working at the venue is ``gross - matched``, and no arithmetic
    over fees may change that.

    They are held here rather than read back off the order because
    ``OrderUpdated`` reaches the order through the execution engine's event
    queue, so ``order.quantity`` still reads its pre-update value for as long as
    the queue is undrained — and Gate.io delivers several fills in one frame,
    which the fill handler walks synchronously. A shadow of the *result*, though,
    is what refuted the first attempt at this: it went stale the moment anyone
    else restated the order, and silently discarded the amend. These three
    numbers cannot go stale that way, because every source that changes the
    order's quantity updates the one it owns.
    """

    #: The order size as the venue states it, in base units.
    gross: Decimal
    #: Cumulative base currency the venue has kept as its fee.
    withheld: Decimal = Decimal(0)
    #: Cumulative base currency actually credited by fills (net of the fee).
    matched: Decimal = Decimal(0)


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

#: Fallback slippage cap for a base-denominated spot market buy, used when the
#: pair definition carries no ``slippage`` field.
DEFAULT_SPOT_SLIPPAGE: Final[Decimal] = Decimal("0.05")

#: Window used by the report builders when the command carries no start time.
DEFAULT_LOOKBACK_SECS: Final[int] = 24 * 60 * 60

#: Largest number of orders Gate.io cancels in one spot batch request.
SPOT_CANCEL_BATCH_SIZE: Final[int] = 20

#: Page size used when listing orders and fills.
REPORT_PAGE_LIMIT: Final[int] = 100

#: Hard cap on pages fetched for one report query, so a pathological venue
#: response can never turn reconciliation into an unbounded request loop.
MAX_REPORT_PAGES: Final[int] = 20

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


def venue_symbol_of(payload: dict[str, Any]) -> str:
    """Return the Gate.io symbol a private payload refers to."""
    for key in ("currency_pair", "contract", "market"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def trigger_rule(
    order_side: OrderSide,
    order_type: OrderType,
    trigger_price: Decimal,
    last_price: Decimal | None,
) -> int:
    """Return the Gate.io trigger rule for a conditional order.

    Gate.io does not model "stop" and "if touched" separately: it takes a
    comparison rule and validates it against the current last price (rule ``1``
    requires a trigger above the market, rule ``2`` one below it). When a last
    price is known the rule follows directly from the comparison, which is what
    the venue itself validates; without one it follows the semantics of the
    Nautilus order type — a stop is placed away from the market in the direction
    of the trade, an if-touched order towards it.
    """
    if last_price is not None and last_price > 0:
        return TRIGGER_RULE_ABOVE if trigger_price >= last_price else TRIGGER_RULE_BELOW
    if order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
        return TRIGGER_RULE_ABOVE if order_side == OrderSide.BUY else TRIGGER_RULE_BELOW
    return TRIGGER_RULE_BELOW if order_side == OrderSide.BUY else TRIGGER_RULE_ABOVE


#: Order types NautilusTrader accepts an ``OrderTriggered`` event for. Gate.io
#: also arms stop-market style orders, but for those the venue-order-id rebase
#: is the whole transition (see ``_maybe_swap_trigger_venue_order_id``).
TRIGGERABLE_ORDER_TYPES: Final[frozenset[OrderType]] = frozenset(
    {OrderType.STOP_LIMIT, OrderType.TRAILING_STOP_LIMIT, OrderType.LIMIT_IF_TOUCHED}
)

#: ``status`` values a price-triggered order reports while it is still armed.
ARMED_TRIGGER_STATUSES: Final[frozenset[str]] = frozenset({"", "open"})


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
        #: What each live spot order's quantity is made of (see `SpotQuantity`).
        self._spot_quantities: dict[ClientOrderId, SpotQuantity] = {}

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
        self._margins: dict[InstrumentId | None, MarginBalance] = {}

        self._account_poll_task: asyncio.Task | None = None
        #: Last private-stream event per product, used as the reconciliation
        #: lookback anchor after a reconnect.
        self._last_stream_event_ns: dict[GateioProductType, int] = {}

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
        * a fill report alone cannot restate the order's quantity, so a spot buy
          whose base-currency fee shrinks the quantity the venue can ever credit
          (EXEC-4) is applied as a fill against a quantity it can never reach.
          ``Order.apply(OrderUpdated)`` never triggers a terminal status, so the
          restatement that follows leaves the order at zero remaining quantity
          and permanently PARTIALLY_FILLED.

        Merely sending the fills before the orders fixes the first half and
        creates the second. ``_reconcile_execution_mass_status`` takes an order
        report *together with* its trades: it restates the quantity first,
        applies the venue's own trades under their own ids next, and only then
        infers anything for a residual difference. That is exactly the order
        these two failure modes require, and it is the same path startup
        reconciliation already uses.

        Grouping alone is not enough, though, because it makes delivery of a
        trade *conditional on the status of the order report it was grouped
        under* — see :meth:`_hand_over_unapplied_fills` for what that costs and
        how the sweep afterwards repairs it.
        """
        await self._update_account_state()

        anchor_ns = self._last_stream_event_ns.get(product, 0)
        now_ns = self._clock.timestamp_ns()
        lookback_ns = DEFAULT_LOOKBACK_SECS * 1_000_000_000
        start_ns = max(anchor_ns, now_ns - lookback_ns) if anchor_ns else now_ns - lookback_ns
        start = datetime.fromtimestamp(start_ns / 1_000_000_000, tz=UTC)

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

        self._send_mass_status_report(mass_status)
        self._hand_over_unapplied_fills(fill_reports)

        self._log.info(
            f"Reconciled {len(order_reports)} order and {len(fill_reports)} fill reports after "
            f"the {product.value} reconnect",
            LogColor.GREEN,
        )

    def _hand_over_unapplied_fills(self, fill_reports: list[FillReport]) -> None:
        """Re-offer, one by one, every recovered trade the grouped pass did not book.

        Grouping a trade under its order report is what lets the engine restate
        the order's quantity before applying the trade, but it also routes the
        trade through ``LiveExecutionEngine._reconcile_order_report``, which asks
        ``_handle_order_status_transitions`` about the *order report* first. That
        returns "reconciled" — before the ``for trade in trades`` loop is ever
        reached — for every one of:

        * ``ACCEPTED``: the ordinary reconnect race. This method queries the
          order listing and the trade listing as two sequential paginated REST
          sweeps, so a match landing between them appears in the trade listing
          while the order snapshot still reads ``left == size``, which parses to
          ACCEPTED with ``filled_qty=0``;
        * ``TRIGGERED``: a conditional order that fired and matched before its
          own listing caught up;
        * ``REJECTED``;
        * ``CANCELED`` and ``EXPIRED`` *when the local order already holds that
          status* — the stream delivered the terminal message and the fill that
          preceded it went missing, which is the exact gap this recovery exists
          to close.

        The mass-status loop drops trades in two further places that have
        nothing to do with status: a report whose reconciliation raises
        ``InvalidStateTrigger`` (caught per order, remaining trades abandoned),
        and a fill whose venue order id has no order report at all (an order that
        finished outside the window its trade falls inside, or one the venue
        answered for on only one of the two endpoints).

        Rather than predict which of those applies — the CANCELED and EXPIRED
        branches depend on local order state, so no static rule can — this
        checks the outcome. ``_send_mass_status_report`` dispatches synchronously
        over the message bus and the engine applies reconciliation fills
        immediately, so by the time it returns the cache already shows which
        trade ids were booked. Anything missing goes through
        ``_reconcile_fill_report_single``, which has no status gate. A trade the
        grouped pass *did* book is skipped, and a replay of one is refused by the
        engine's own trade-id de-duplication, so no execution can be counted
        twice.
        """
        for report in fill_reports:
            if self._fill_is_booked(report):
                continue
            self._log.warning(
                f"Recovered trade {report.trade_id.value} was not booked by the grouped "
                f"hand-over for {report.venue_order_id!r}; reporting it on its own",
            )
            self._send_fill_report(report)

    def _fill_is_booked(self, report: FillReport) -> bool:
        """Return whether this venue trade is already on the order it belongs to."""
        client_order_id = report.client_order_id
        if client_order_id is None and report.venue_order_id is not None:
            client_order_id = self._cache.client_order_id(report.venue_order_id)
        if client_order_id is None:
            return False
        order = self._cache.order(client_order_id)
        if order is None:
            return False
        return report.trade_id in order.trade_ids

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
        self._spot_quantities.pop(client_order_id, None)
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
        if order.is_closed:
            self._log.warning(f"Order {order.client_order_id!r} is already closed, skipping")
            return

        instrument = self._instrument(order.instrument_id)
        if instrument is None:
            self._deny(order, f"instrument {order.instrument_id} not found")
            return

        resolved = self._resolve(order.instrument_id)
        if resolved is None:
            self._deny(order, f"{order.instrument_id} is not a configured Gate.io product")
            return
        product, raw_symbol = resolved

        if order.order_type not in SUPPORTED_ORDER_TYPES:
            self._deny(
                order,
                f"{order_type_to_str(order.order_type)} orders are not supported by Gate.io",
            )
            return

        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        try:
            if order.order_type in CONDITIONAL_ORDER_TYPES:
                await self._submit_trigger_order(order, instrument, product, raw_symbol)
            else:
                await self._submit_regular_order(order, instrument, product, raw_symbol)
        except (UnsupportedOrderError, OrderValidationError, ValueError) as e:
            self._reject(order, str(e))
        except GateioError as e:
            self._reject(
                order,
                f"{e.label or 'ERROR'}: {e.message}",
                due_post_only=e.label in POST_ONLY_LABELS,
            )
        except Exception as e:  # noqa: BLE001 - never leave an order in limbo
            self._reject(order, f"submit failed: {e}")

    def _deny(self, order: Order, reason: str) -> None:
        self._log.error(f"Denying {order.client_order_id!r}: {reason}")
        self.generate_order_denied(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
        )

    def _reject(self, order: Order, reason: str, due_post_only: bool = False) -> None:
        self._log.error(f"Rejecting {order.client_order_id!r}: {reason}")
        self.generate_order_rejected(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
            due_post_only=due_post_only,
        )

    async def _submit_regular_order(
        self,
        order: Order,
        instrument: Instrument,
        product: GateioProductType,
        raw_symbol: str,
    ) -> None:
        if product.is_spot:
            body = await self._build_spot_order(order, instrument, raw_symbol)
            response = await self._spot_http.create_order(body)
        elif product.is_option:
            body = self._build_options_order(order, raw_symbol)
            response = await self._options_http.create_order(body)
        else:
            body = self._build_futures_order(order, raw_symbol)
            response = await self._futures_api(product).create_order(body)

        if isinstance(response, dict):
            self._handle_order_payload(product, response)

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
            tif = (
                GateioTimeInForce.FOK
                if order.time_in_force == TimeInForce.FOK
                else GateioTimeInForce.IOC
            )
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

            return await self._build_aggressive_spot_buy(order, instrument, raw_symbol, body)

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

        display_qty = getattr(order, "display_qty", None)
        if display_qty is not None:
            body["iceberg"] = str(display_qty)
        return body

    async def _build_aggressive_spot_buy(
        self,
        order: Order,
        instrument: Instrument,
        raw_symbol: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Express a base-denominated spot market buy as an aggressive IOC limit.

        Gate.io's native spot market buy spends a **quote** amount, so it cannot
        express "buy exactly this many base units". Rather than convert the
        quantity behind the caller's back, the order is sent as an
        immediate-or-cancel limit order priced through the book by the pair's own
        published slippage cap; the venue then fills at or better than that bound
        and cancels any remainder.
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
        body["time_in_force"] = GateioTimeInForce.IOC.value
        self._log.info(
            f"Base-denominated spot market buy on {raw_symbol} sent as an IOC limit at "
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

        display_qty = getattr(order, "display_qty", None)
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

        display_qty = getattr(order, "display_qty", None)
        if display_qty is not None:
            body["iceberg"] = int(display_qty.as_decimal())
        return body

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
            raise UnsupportedOrderError(
                f"time in force {time_in_force_to_str(order.time_in_force)} is not supported "
                f"by Gate.io ({e})",
            ) from e

    # -- price-triggered orders --------------------------------------------

    async def _submit_trigger_order(
        self,
        order: Order,
        instrument: Instrument,
        product: GateioProductType,
        raw_symbol: str,
    ) -> None:
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
            response = await self._spot_http.create_price_order(body)
        else:
            body = self._build_futures_price_order(order, raw_symbol, trigger_price, rule)
            response = await self._futures_api(product).create_price_order(body)

        trigger_id = self._trigger_order_id(response)
        if trigger_id is None:
            raise GateioError(
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
            f"(rule {rule}, trigger {trigger_price})",
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
            raise UnsupportedOrderError(
                f"Gate.io price-triggered spot orders accept the "
                f"{', '.join(sorted(PRICE_ORDER_ACCOUNTS))} ledgers only, "
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
            self._cancel_rejected(command, f"{e.label or 'ERROR'}: {e.message}")
        except Exception as e:  # noqa: BLE001 - report, never propagate
            self._cancel_rejected(command, f"cancel failed: {e}")

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
                for cancel in by_venue_id.values():
                    self._cancel_rejected(cancel, f"{e.label or 'ERROR'}: {e.message}")
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
            self._modify_rejected(command, f"{e.label or 'ERROR'}: {e.message}")
            return
        except Exception as e:  # noqa: BLE001 - report, never propagate
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
            report = self._parse_order_status_report(product, payload, instrument)
            if report is not None:
                self._send_order_status_report(report)
            return

        status = self._order_status(product, payload)
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
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason="post-only order would have taken liquidity (finish_as=poc)",
                ts_event=ts_event,
                due_post_only=True,
            )
            self._forget_order(order.client_order_id)
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
    ) -> bool:
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

        Returns whether an event carrying ``venue_order_id`` can now be applied
        to ``order``. Callers on the fill path need that answer: Gate.io orders
        ``*.orders`` and ``*.usertrades`` independently, so a fill can arrive
        before the order message that would have rebased the identity, and
        emitting it against a stale identity gets it rejected and dropped.
        """
        if order.venue_order_id is not None and order.venue_order_id == venue_order_id:
            return True  # Already rebased onto this order
        link = self._trigger_links.get(order.client_order_id)
        if link is None:
            # Not a conditional order. The identity is reconcilable only while
            # the order carries none of its own, which the framework permits.
            return order.venue_order_id is None
        if venue_order_id.value == link.armed_id:
            return True  # The event carries the armed id the order already holds
        if self._cache.venue_order_id(order.client_order_id) == venue_order_id:
            # The rebase was already emitted. `order.venue_order_id` may still
            # read as the armed id here, because the order object is only updated
            # once the execution engine applies the `OrderUpdated` — and several
            # fills for a freshly fired order can be handled before that happens.
            # The cache mapping is written synchronously by the rebase itself, so
            # it is the one signal that does not depend on event delivery.
            return True
        self._attach_fired_order_id(link, venue_order_id.value)
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
        if order.order_type in TRIGGERABLE_ORDER_TYPES:
            self.generate_order_triggered(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                ts_event=ts_event,
            )
        return True

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

        The payload's quantity is the venue's *gross* order size, which is what
        a Gate.io amend restates and what another session's amend restates too —
        nothing in the payload distinguishes the two, and nothing should. On spot
        it is recorded as ``SpotQuantity.gross`` and the order is asked for
        ``gross - withheld``, so an amend and the base-currency fee compose
        instead of overwriting each other. Without that, every venue update
        during a partially filled spot buy would restate the order back to gross
        and undo the netting — which the next fill would redo, leaving the final
        state decided by whichever message happened to arrive last.
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

        quantity = self._payload_quantity(order.instrument_id, payload, instrument)
        price_value = payload.get("price")
        price = (
            instrument.make_price(to_decimal(price_value))
            if price_value not in (None, "", "0")
            else getattr(order, "price", None)
        )
        if quantity is None:
            return

        if product.is_spot:
            state = self._spot_quantities.get(order.client_order_id)
            if state is None:
                state = SpotQuantity(
                    gross=quantity.as_decimal(),
                    matched=order.filled_qty.as_decimal(),
                )
                self._spot_quantities[order.client_order_id] = state
            else:
                state.gross = quantity.as_decimal()
            restated = instrument.make_qty(max(state.gross - state.withheld, state.matched))
        else:
            restated = quantity

        order_price = getattr(order, "price", None)
        if restated == order.quantity and (price is None or price == order_price):
            return

        self.generate_order_updated(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            quantity=restated,
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
            size = abs(to_int(payload.get("size")))
            return Quantity(size, instrument.size_precision) if size > 0 else None

        is_spot_market_buy = (
            str(payload.get("type") or "").lower() == "market"
            and str(payload.get("side") or "").lower() == "buy"
        )
        if is_spot_market_buy:
            filled_base = to_decimal(payload.get("filled_amount"))
            return instrument.make_qty(filled_base) if filled_base > 0 else None

        amount = to_decimal(payload.get("amount"))
        if amount <= 0:
            return None
        return instrument.make_qty(amount)

    @staticmethod
    def _order_status(product: GateioProductType, payload: dict[str, Any]) -> OrderStatus:
        """Map one Gate.io order object onto a Nautilus order status."""
        if product.is_spot:
            status = payload.get("status")
            if status is None:
                # The spot stream discriminates with `event`, not `status`.
                event = str(payload.get("event") or "").lower()
                status = "open" if event in ("put", "update") else "finished"
            amount = to_float(payload.get("amount"))
            filled = to_float(payload.get("filled_amount"))
            if not filled:
                filled = max(0.0, amount - to_float(payload.get("left")))
        else:
            status = payload.get("status") or "open"
            amount = float(abs(to_int(payload.get("size"))))
            filled = max(0.0, amount - float(abs(to_int(payload.get("left")))))
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
            report = self._parse_fill_report(product, payload, instrument)
            if report is not None:
                self._send_fill_report(report)
            return

        applied = self._applied_trade_ids.setdefault(order.client_order_id, set())
        if trade_id.value in applied or trade_id in order.trade_ids:
            self._log.debug(
                f"Ignoring duplicate fill {trade_id.value} for {order.client_order_id!r}",
            )
            return

        if order.is_closed:
            # Gate.io does not order `*.orders` against `*.usertrades`, so a
            # terminal order message can win the race against the fill that
            # caused it. `Order.apply` refuses an `OrderFilled` on a closed
            # order, so the fill goes through the reconciliation path, which
            # knows how to apply it to an order that has already finished.
            self._log.warning(
                f"Fill {trade_id.value} arrived after {order.client_order_id!r} was reported "
                f"{order.status_string()}; routing it through the reconciliation path",
            )
            # `_forget_order` already dropped this order's applied-trade set when
            # the terminal message arrived; re-seeding it deliberately keeps a
            # replay of the same late fill from being reported a second time.
            applied.add(trade_id.value)
            report = self._parse_fill_report(product, payload, instrument)
            if report is not None:
                self._send_fill_report(report)
            return

        last_px = instrument.make_price(to_decimal(payload.get("price")))
        last_qty, commission = self._fill_quantity_and_commission(product, payload, instrument)
        if last_qty.as_decimal() <= 0:
            self._log.debug(f"Ignoring a zero-quantity fill {trade_id.value}")
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

        if not self._maybe_swap_trigger_venue_order_id(order, venue_order_id, ts_event):
            # Gate.io orders `*.orders` and `*.usertrades` independently, so this
            # fill can be the first message that mentions the fired order at all.
            # The identity could not be reconciled inline, and `Order.apply`
            # refuses an `OrderFilled` whose venue order id differs from the one
            # already on the order — the exception is swallowed by the execution
            # engine, which is how a fill goes missing silently. Report it
            # through reconciliation instead, where an id mismatch is expected
            # and resolved against the venue, and say so loudly enough to act on.
            self._log.error(
                f"Fill {trade_id.value} carries venue order id {venue_order_id.value}, but "
                f"{order.client_order_id!r} holds {order.venue_order_id}; the identity could not "
                f"be reconciled from this event. Routing the fill through reconciliation.",
            )
            applied.add(trade_id.value)
            report = self._parse_fill_report(product, payload, instrument)
            if report is not None:
                self._send_fill_report(report)
            return

        self._maybe_convert_quote_quantity(order, instrument, venue_order_id, last_px, ts_event)
        self._net_withheld_base(
            product,
            order,
            instrument,
            payload,
            venue_order_id,
            last_qty,
            ts_event,
        )

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
        """Return the fill quantity and commission, netting a base-currency fee.

        On spot Gate.io deducts the fee from the currency received, so a buy
        credits slightly less base currency than the fill quantity states.
        Netting the fee off ``last_qty`` when it is charged in the base currency
        keeps the Nautilus position equal to the balance the venue actually
        credited.
        """
        fee = to_decimal(payload.get("fee"))
        if product.is_spot:
            fee_currency = self._currency(
                payload.get("fee_currency"),
                default=instrument.quote_currency.code,
            )
            quantity = to_decimal(payload.get("amount"))
            base_currency = getattr(instrument, "base_currency", None)
            if base_currency is not None and fee_currency == base_currency and fee > 0:
                quantity = max(Decimal(0), quantity - fee)
            return instrument.make_qty(quantity), Money(fee, fee_currency)

        settlement = getattr(instrument, "settlement_currency", instrument.quote_currency)
        size = abs(to_int(payload.get("size")))
        # Options fills carry no fee on the REST endpoint; the stream does.
        return Quantity(size, instrument.size_precision), Money(fee, settlement)

    def _maybe_convert_quote_quantity(
        self,
        order: Order,
        instrument: Instrument,
        venue_order_id: VenueOrderId,
        last_px: Price,
        ts_event: int,
    ) -> None:
        """Convert a quote-denominated spot market buy to base units on first fill.

        A Gate.io spot market buy is submitted as a cash amount, so the order's
        quantity is denominated in the quote currency while its fills are
        denominated in the base currency. The order is restated in base units at
        the venue's own fill price before the first fill is applied, so that
        position and PnL arithmetic downstream works in one unit.
        """
        if order.client_order_id in self._spot_quantities:
            # This order's quantity is already tracked in base units, so the
            # conversion has either happened or is not needed. Both guards below
            # read the order, which still carries its pre-update state while the
            # engine's queue is undrained, so a second fill in the same frame
            # would otherwise convert a second time.
            return
        if not order.is_quote_quantity or order.filled_qty.as_decimal() > 0:
            return
        price = last_px.as_decimal()
        if price <= 0:
            return
        base_quantity = instrument.make_qty(order.quantity.as_decimal() / price)
        self._spot_quantities[order.client_order_id] = SpotQuantity(
            gross=base_quantity.as_decimal(),
        )
        self.generate_order_updated(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            quantity=base_quantity,
            price=getattr(order, "price", None),
            trigger_price=None,
            ts_event=ts_event,
            is_quote_quantity=False,
        )

    def _net_withheld_base(
        self,
        product: GateioProductType,
        order: Order,
        instrument: Instrument,
        payload: dict[str, Any],
        venue_order_id: VenueOrderId,
        last_qty: Quantity,
        ts_event: int,
    ) -> None:
        """Fold one spot fill into the order's quantity bookkeeping and restate it.

        Gate.io charges a spot BUY fee in the currency being bought, so it
        credits ``amount - fee`` base units for a match of ``amount``.
        ``_fill_quantity_and_commission`` nets that off ``last_qty`` (EXEC-4), so
        the order's fills can never add up to the quantity it was submitted with:
        the shortfall is exactly the cumulative fee, and it is not a quantity
        still working at the venue. Left alone, a fully matched buy ends with
        zero remaining quantity and status PARTIALLY_FILLED, and stays in
        ``cache.orders_open()`` for the rest of the session.

        Restating the order is the only way out, and it has to happen *before*
        the closing fill: ``Order.apply(OrderUpdated)`` never triggers a terminal
        status, so an update that arrives after the last fill lands leaves the
        order at zero remaining quantity and still open — which is precisely what
        the REST report path does when the stream applied the fill first. The
        same reasoning already governs ``_maybe_convert_quote_quantity``.

        What is recorded here is the fee itself, not the quantity it implies:
        ``SpotQuantity.withheld`` grows by this fill's withheld base and
        ``gross`` is left alone, because only the venue moves ``gross``. The
        target quantity is then always ``gross - withheld``, whoever last
        amended the order — which is the difference between this and a shadow
        copy of the order's quantity, and the reason an amend between two fills
        now survives instead of being discarded.

        The withheld amount is derived from ``last_qty`` rather than from the
        payload's ``fee`` so that both sides carry the same rounding: netting the
        raw fee here and a rounded fee into the fill would reintroduce the gap
        this method exists to close, one dust unit at a time.
        """
        if not product.is_spot:
            return  # Derivative fees are charged in the settlement currency

        state = self._spot_quantities.get(order.client_order_id)
        if state is None:
            if order.is_quote_quantity:
                # A quote-denominated spot market buy whose conversion could not
                # run (the venue published no usable fill price). Its quantity is
                # a cash amount, and netting base units off cash means nothing.
                self._log.warning(
                    f"Not restating {order.client_order_id!r}: its quantity is still "
                    f"denominated in {instrument.quote_currency}, so the base-currency fee "
                    f"withheld from fill {payload.get('id')!r} cannot be netted off it",
                )
                return
            state = SpotQuantity(
                gross=order.quantity.as_decimal(),
                matched=order.filled_qty.as_decimal(),
            )
            self._spot_quantities[order.client_order_id] = state

        state.matched += last_qty.as_decimal()
        state.withheld += max(
            Decimal(0),
            to_decimal(payload.get("amount")) - last_qty.as_decimal(),
        )
        self._restate_spot_quantity(order, instrument, venue_order_id, ts_event, state)

    def _restate_spot_quantity(
        self,
        order: Order,
        instrument: Instrument,
        venue_order_id: VenueOrderId,
        ts_event: int,
        state: SpotQuantity,
    ) -> None:
        """Ask the engine for ``gross - withheld``, the only quantity that composes.

        ``gross - matched`` is what the venue is still working, and netting the
        fee off the order's quantity has to leave that untouched: with the order
        at ``gross - withheld`` and its fills summing to ``matched - withheld``,
        the remaining quantity reads ``gross - matched`` exactly. It reaches zero
        when the venue has matched the order in full and never before, whether or
        not the venue amended the order along the way.

        ``is_quote_quantity`` is stated rather than inherited: ``generate_order_updated``
        resolves ``None`` from the *cached* order, which still reads ``True``
        while the conversion's own ``OrderUpdated`` sits in the engine's queue, so
        inheriting it would re-flag a base-denominated quantity as quote.
        """
        target = state.gross - state.withheld
        if target < state.matched:
            # The venue would have to have amended the order below what it has
            # already matched. Nothing this client can express is below its own
            # fills, so clamp and say so rather than emit a quantity that makes
            # `leaves_qty` negative.
            self._log.warning(
                f"Clamping the restated quantity of {order.client_order_id!r} from {target} "
                f"to {state.matched}: the venue's order size ({state.gross}) less the "
                f"base-currency fees it withheld ({state.withheld}) is below what it has "
                f"already credited",
            )
            target = state.matched

        quantity = instrument.make_qty(target)
        if quantity == order.quantity:
            return

        self.generate_order_updated(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            quantity=quantity,
            price=getattr(order, "price", None),
            trigger_price=getattr(order, "trigger_price", None),
            ts_event=ts_event,
            is_quote_quantity=False,
        )

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
        self._publish_account_state(reported=True)

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

    async def _update_account_state(self) -> None:
        """Refresh balances and margins from REST across every enabled product."""
        margins: dict[InstrumentId | None, MarginBalance] = {}
        self._unified_balances = {}

        for product in self._products:
            wallet: dict[str, tuple[Decimal, Decimal]] = {}
            try:
                if product.is_spot:
                    await self._collect_spot_balances(wallet)
                elif product.is_option:
                    await self._collect_options_balances(wallet, margins)
                else:
                    await self._collect_futures_balances(product, wallet, margins)
            except WalletNotProvisionedError as e:
                self._log.warning(f"Skipping the {product.value} wallet: {e}")
                continue
            except GateioError as e:
                self._log.error(f"Cannot read the {product.value} wallet: {e}")
                continue
            self._wallet_balances[product] = wallet

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
        self._margins = margins
        self._publish_account_state(reported=True)

    async def _collect_spot_balances(self, balances: dict[str, tuple[Decimal, Decimal]]) -> None:
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
            await self._collect_unified_balances(self._unified_balances)

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
        margins: dict[InstrumentId | None, MarginBalance],
    ) -> None:
        api = self._futures_api(product)
        account = await require_wallet(api.accounts(), f"the {product.value} wallet")
        currency = str(account.get("currency") or product.settle).upper()
        total = to_decimal(account.get("total")) + to_decimal(account.get("unrealised_pnl"))
        free = to_decimal(account.get("available"))
        _accumulate(balances, currency, total, min(free, total))

        positions = await require_wallet(
            api.positions(holding=True),
            f"the {product.value} positions",
        )
        currency_obj = self._currency(currency)
        for position in positions or []:
            margin = self._position_margin(product, position, currency_obj)
            if margin is not None:
                margins[margin.instrument_id] = margin

    def _position_margin(
        self,
        product: GateioProductType,
        position: dict[str, Any],
        currency: Currency,
    ) -> MarginBalance | None:
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
            instrument_id=gateio_to_instrument_id(product, raw_symbol),
        )

    async def _collect_options_balances(
        self,
        balances: dict[str, tuple[Decimal, Decimal]],
        margins: dict[InstrumentId | None, MarginBalance],
    ) -> None:
        account = await require_wallet(self._options_http.account(), "the options wallet")
        currency = str(account.get("currency") or "USDT").upper()
        total = to_decimal(account.get("equity"))
        if total <= 0:
            total = to_decimal(account.get("total"))
        free = to_decimal(account.get("available"))
        _accumulate(balances, currency, total, min(free, total))

        initial = to_decimal(account.get("init_margin")) + to_decimal(account.get("order_margin"))
        maintenance = to_decimal(account.get("maint_margin"))
        if initial > 0 or maintenance > 0:
            currency_obj = self._currency(currency)
            margins[None] = MarginBalance(
                initial=Money(max(Decimal(0), initial), currency_obj),
                maintenance=Money(max(Decimal(0), maintenance), currency_obj),
            )

    def _publish_account_state(self, reported: bool) -> None:
        """Publish the aggregated wallet state as a Nautilus ``AccountState``."""
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
            margins = list(self._margins.values())

        self.generate_account_state(
            balances=balances,
            margins=margins,
            reported=reported,
            ts_event=self._clock.timestamp_ns(),
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
            failure = orders if isinstance(orders, BaseException) else fills
            self._log.exception("Cannot reconcile execution state", failure)
            return None

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
            mass_status.add_position_reports(reports=positions)

        self.reconciliation_active = False
        return mass_status

    # -- reconciliation: order status --------------------------------------

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        """Generate order status reports across every enabled product."""
        reports: list[OrderStatusReport] = []
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
            except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
                self._log_report_error(e, f"{product.value} OrderStatusReports")

        self._log_report_receipt(len(reports), "OrderStatusReport", command.log_receipt_level)
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
        """Generate a single order status report by venue or client order id."""
        instrument_id = command.instrument_id
        client_order_id = command.client_order_id
        venue_order_id = command.venue_order_id

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
                if not product.is_spot:
                    self._log.warning(
                        f"Cannot look up {client_order_id!r} on {product.value}: Gate.io "
                        f"resolves a client order id only while the order is resting",
                    )
                    return None
                text = (
                    self._text_by_client_order_id.get(client_order_id)
                    if client_order_id is not None
                    else None
                )
                if text is None:
                    self._log.warning(f"No venue order id known for {client_order_id!r}")
                    return None
                lookup_id = text
            else:
                lookup_id = venue_order_id.value

            if product.is_spot:
                payload = await self._spot_http.get_order(
                    lookup_id,
                    raw_symbol,
                    account=self.spot_account,
                )
            elif product.is_option:
                payload = await self._options_http.get_order(lookup_id)
            else:
                payload = await self._futures_api(product).get_order(lookup_id)
        except WalletNotProvisionedError as e:
            self._log.warning(f"Cannot generate an order status report: {e}")
            return None
        except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
            self._log_report_error(e, "OrderStatusReport")
            return None

        if not isinstance(payload, dict):
            return None
        instrument = await self._instrument_or_load(
            gateio_to_instrument_id(product, venue_symbol_of(payload) or raw_symbol),
        )
        if instrument is None:
            return None
        return self._parse_order_status_report(product, payload, instrument)

    def _parse_order_status_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        instrument: Instrument | None,
    ) -> OrderStatusReport | None:
        """Build an :class:`OrderStatusReport`, skipping a payload it cannot express."""
        try:
            return self._build_order_status_report(product, payload, instrument)
        except Exception as e:  # noqa: BLE001 - one bad order must not fail the batch
            self._log.warning(f"Cannot parse the order {payload.get('id')!r}: {e}")
            return None

    def _build_order_status_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
        instrument: Instrument | None,
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
            parsed = self._parse_spot_order_fields(payload, instrument)
        else:
            parsed = self._parse_contract_order_fields(payload, instrument)
        if parsed is None:
            return None
        side, order_type, quantity, filled_qty, price = parsed

        time_in_force_value = str(payload.get("time_in_force") or payload.get("tif") or "gtc")
        post_only = time_in_force_value.lower() == GateioTimeInForce.POC.value
        avg_px_value = payload.get("avg_deal_price") or payload.get("fill_price")
        avg_px = to_decimal(avg_px_value) if filled_qty.as_decimal() > 0 else None

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

        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument.id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            order_side=side,
            order_type=order_type,
            time_in_force=time_in_force_from_gateio(time_in_force_value),
            order_status=self._order_status(product, payload),
            quantity=quantity,
            filled_qty=filled_qty,
            price=price,
            avg_px=avg_px if avg_px and avg_px > 0 else None,
            display_qty=display_qty,
            post_only=post_only,
            reduce_only=bool(payload.get("is_reduce_only") or payload.get("reduce_only")),
            report_id=UUID4(),
            ts_accepted=ts_accepted or ts_init,
            ts_last=ts_last or ts_init,
            ts_init=ts_init,
        )

    @staticmethod
    def _base_currency_fee(payload: dict[str, Any], instrument: Instrument) -> Decimal:
        """Return the payload's fee when it is charged in the base currency, else zero.

        Gate.io deducts a spot fee from the currency received, so a buy credits
        slightly less base currency than the raw ``filled_amount`` states.
        """
        base_currency = getattr(instrument, "base_currency", None)
        if base_currency is None:
            return Decimal(0)
        fee_currency = str(payload.get("fee_currency") or "").upper()
        if fee_currency != base_currency.code:
            return Decimal(0)
        return max(Decimal(0), to_decimal(payload.get("fee")))

    def _parse_spot_order_fields(
        self,
        payload: dict[str, Any],
        instrument: Instrument,
    ) -> tuple[OrderSide, OrderType, Quantity, Quantity, Price | None] | None:
        side = order_side_from_gateio(str(payload.get("side")))
        order_type = order_type_from_gateio(payload.get("type"))
        amount = to_decimal(payload.get("amount"))
        filled = to_decimal(payload.get("filled_amount"))

        if order_type == OrderType.MARKET and side == OrderSide.BUY:
            # `amount` is a quote-currency cash amount here, so the only
            # base-denominated quantity the venue publishes is what was filled.
            if filled <= 0:
                self._log.debug(
                    "Skipping an unfilled spot market buy report: Gate.io publishes only a "
                    "quote-denominated amount for it",
                )
                return None
            amount = filled

        # The event stream nets a base-currency fee off every fill quantity, so
        # the report has to net the cumulative fee off the filled quantity too.
        # Without this the report claims a larger filled quantity than the fills
        # that produced it: reconciliation then sees the order as permanently
        # short of its own report, never closes it, and synthesises a fill.
        fee = self._base_currency_fee(payload, instrument)
        if fee > 0:
            fully_filled = filled > 0 and (to_decimal(payload.get("left")) <= 0 or filled >= amount)
            filled = max(Decimal(0), filled - fee)
            if fully_filled:
                # A fully filled order can never report a quantity its own fills
                # cannot reach, or it would stay open forever.
                amount = filled

        if amount <= 0:
            return None
        price_value = payload.get("price")
        price = (
            instrument.make_price(to_decimal(price_value))
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

    def _parse_contract_order_fields(
        self,
        payload: dict[str, Any],
        instrument: Instrument,
    ) -> tuple[OrderSide, OrderType, Quantity, Quantity, Price | None] | None:
        size = to_int(payload.get("size"))
        if size == 0:
            # A close-position order carries no quantity of its own.
            self._log.debug("Skipping a close-position order report with size 0")
            return None
        side = OrderSide.BUY if size > 0 else OrderSide.SELL
        quantity = abs(size)
        left = abs(to_int(payload.get("left")))
        filled = max(0, quantity - left)

        price_value = to_decimal(payload.get("price"))
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
        except Exception as e:  # noqa: BLE001 - one bad order must not fail the batch
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

        if product.is_spot:
            side = order_side_from_gateio(str(initial.get("side")))
            quantity = instrument.make_qty(to_decimal(initial.get("amount")))
            is_limit = str(initial.get("type") or "limit").lower() == "limit"
            price_value = to_decimal(initial.get("price"))
            time_in_force_value = str(initial.get("time_in_force") or "gtc")
        else:
            size = to_int(initial.get("size"))
            if size == 0:
                return None
            side = OrderSide.BUY if size > 0 else OrderSide.SELL
            quantity = Quantity(abs(size), instrument.size_precision)
            price_value = to_decimal(initial.get("price"))
            is_limit = price_value > 0
            time_in_force_value = str(initial.get("tif") or "gtc")

        order = self._cache.order(client_order_id) if client_order_id is not None else None
        if order is not None and order.order_type in CONDITIONAL_ORDER_TYPES:
            order_type = order.order_type
        elif is_limit:
            order_type = OrderType.STOP_LIMIT
        else:
            order_type = OrderType.STOP_MARKET

        trigger_price_value = to_decimal(trigger.get("price"))
        if trigger_price_value <= 0:
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
        """Generate fill reports across every enabled product."""
        reports: list[FillReport] = []
        start_secs, end_secs = self._window(command.start, command.end)

        for product in self._products:
            try:
                reports += await self._fill_reports_for_product(
                    product,
                    command.instrument_id,
                    start_secs,
                    end_secs,
                )
            except WalletNotProvisionedError as e:
                self._log.warning(f"Skipping {product.value} fill reports: {e}")
            except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
                self._log_report_error(e, f"{product.value} FillReports")

        # Reconciliation applies fills in list order, so ordering is load-bearing.
        reports.sort(key=lambda report: (report.ts_event, report.trade_id.value))
        self._log_report_receipt(len(reports), "FillReport", LogLevel.INFO)
        return reports

    async def _fill_reports_for_product(
        self,
        product: GateioProductType,
        instrument_id: InstrumentId | None,
        start_secs: int,
        end_secs: int,
    ) -> list[FillReport]:
        symbol: str | None = None
        if instrument_id is not None:
            resolved = self._resolve(instrument_id)
            if resolved is None or resolved[0] is not product:
                return []
            symbol = resolved[1]

        payloads: list[dict[str, Any]] = []
        if product.is_spot:
            symbols = [symbol] if symbol else sorted(self._active_symbols(product)) or [None]
            for pair in symbols:
                payloads += await self._collect_pages(
                    lambda page, pair=pair: self._spot_http.my_trades(
                        pair=pair,
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
                    limit=REPORT_PAGE_LIMIT,
                    offset=page * REPORT_PAGE_LIMIT,
                ),
                description=f"{product.value} fills",
                wallet=f"the {product.value} fills",
                stop_after=lambda rows: _oldest_ts_before(rows, start_secs),
            )

        reports: list[FillReport] = []
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
            report = self._parse_fill_report(product, payload, instrument)
            if report is None:
                continue
            if not _within(report.ts_event, start_secs, end_secs):
                continue
            reports.append(report)
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
        """Build a :class:`FillReport` from one Gate.io fill object."""
        trade_id_value = payload.get("id")
        order_id_value = payload.get("order_id") or payload.get("order")
        if trade_id_value in (None, "") or order_id_value in (None, ""):
            return None

        last_px = instrument.make_price(to_decimal(payload.get("price")))
        if product.is_spot:
            side = order_side_from_gateio(str(payload.get("side")))
        else:
            size = to_int(payload.get("size"))
            if size == 0:
                return None
            side = OrderSide.BUY if size > 0 else OrderSide.SELL

        last_qty, commission = self._fill_quantity_and_commission(product, payload, instrument)
        if last_qty.as_decimal() <= 0:
            return None

        venue_order_id = VenueOrderId(str(order_id_value))
        ts_event = first_timestamp_ns(payload, "create_time_ms", "create_time", "time_ms", "time")
        ts_init = self._clock.timestamp_ns()
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
            ts_event=ts_event or ts_init,
            ts_init=ts_init,
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
            except WalletNotProvisionedError as e:
                # Not a failure: Gate.io creates a product wallet on first use
                # and says USER_NOT_FOUND until then, which is a definite "there
                # is no position here" rather than an unanswered question.
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
                # locally.
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
        for payload in entries:
            if not isinstance(payload, dict):
                continue
            report = await self._parse_position_report(product, payload)
            if report is not None:
                reports.append(report)
        return reports

    async def _parse_position_report(
        self,
        product: GateioProductType,
        payload: dict[str, Any],
    ) -> PositionStatusReport | None:
        """Build a :class:`PositionStatusReport` from one Gate.io position."""
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

        size = to_int(payload.get("size"))
        if size > 0:
            side = PositionSide.LONG
        elif size < 0:
            side = PositionSide.SHORT
        else:
            side = PositionSide.FLAT

        entry_price = to_decimal(payload.get("entry_price"))
        ts_last = first_timestamp_ns(payload, "update_time", "time_ms", "time")
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
    "SUPPORTED_ORDER_TYPES",
    "TRIGGERABLE_ORDER_TYPES",
    "GateioExecutionClient",
    "GateioTriggerLink",
    "trigger_rule",
]
