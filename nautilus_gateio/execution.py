"""Live execution client for Gate.io spot.

Order lifecycle
---------------
``Strategy.submit_order`` -> this client -> Gate.io REST (``/spot/orders``) ->
``OrderSubmitted`` / ``OrderAccepted`` / ``OrderFilled`` / ``OrderRejected`` /
``OrderCanceled`` events -> Nautilus portfolio. Fills are detected by REST
polling (an immediate post-submit check plus a periodic loop) — the adapter
has no private WebSocket yet.

Known behaviors (documented, tested where practical):

* ``MARKET`` orders are emulated as aggressive LIMIT IOC orders crossing the
  spread by 1%, because Gate.io spot market-buy orders interpret ``amount``
  as quote currency (see ``docs/execution.md``).
* The Nautilus ``ClientOrderId`` is propagated to the exchange through the
  Gate.io ``text`` field when it fits the field's constraints; otherwise a
  generated id is used and the mapping is kept in memory.
* ``generate_*_reports`` return empty results: reconciliation of pre-existing
  exchange state on start-up is not implemented (fresh-start semantics). Use
  :func:`nautilus_gateio.reconcile.reconcile` for a diagnostic comparison.
* The default environment is Gate.io **testnet**; mainnet requires an
  explicit ``environment="mainnet"`` in the config.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.execution.messages import (
    CancelAllOrders,
    CancelOrder,
    ModifyOrder,
    SubmitOrder,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderType,
)
from nautilus_trader.model.identifiers import AccountId, ClientId, TradeId, Venue, VenueOrderId
from nautilus_trader.model.objects import AccountBalance, Money, Price, Quantity

from nautilus_gateio.config import GateioExecClientConfig, resolve_credentials
from nautilus_gateio.http import GateioHttpClient
from nautilus_gateio.providers import get_currency
from nautilus_gateio.signing import generate_client_order_id, sanitize_client_order_id
from nautilus_gateio.symbols import instrument_id_to_gate_pair


class _PendingOrder:
    """In-memory tracking for one live venue order."""

    __slots__ = (
        "instrument_id",
        "strategy_id",
        "client_order_id",
        "pair",
        "venue_order_id",
        "reported_fill",
    )

    def __init__(self, instrument_id, strategy_id, client_order_id, pair, venue_order_id) -> None:
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.client_order_id = client_order_id
        self.pair = pair
        self.venue_order_id = venue_order_id
        self.reported_fill = 0.0


class GateioExecutionClient(LiveExecutionClient):
    """Gate.io spot execution client (``AccountType.CASH``, netting OMS)."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: GateioExecClientConfig | None = None,
        http_client: GateioHttpClient | None = None,
    ) -> None:
        config = config or GateioExecClientConfig()
        venue = config.venue
        super().__init__(
            loop=loop,
            client_id=ClientId(venue),
            venue=Venue(venue),
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=None,
            instrument_provider=InstrumentProvider(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._set_account_id(AccountId(f"{venue}-SPOT"))
        self._config_local = config
        if http_client is not None:
            self._http = http_client
        else:
            api_key, api_secret = resolve_credentials(
                config.api_key, config.api_secret, config.is_testnet
            )
            # live_orders=True: this client's purpose is order routing; the
            # explicit opt-in is wiring it into a TradingNode. The environment
            # still defaults to testnet — mainnet is a further explicit choice.
            self._http = GateioHttpClient(
                api_key=api_key,
                api_secret=api_secret,
                live_orders=True,
                base_url=config.resolve_base_url(),
            )
        self._pending: dict[str, _PendingOrder] = {}
        self._cancelling: set[str] = set()
        self._poll_task: asyncio.Task | None = None

    # -- helpers -----------------------------------------------------------

    async def _run(self, fn, *args, **kwargs):
        return await self._loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def _exchange_text(self, client_order_id) -> str:
        """Map the Nautilus ``ClientOrderId`` onto Gate.io's ``text`` field."""
        candidate = sanitize_client_order_id(f"t-{client_order_id.value}")
        if len(candidate) > 2:
            return candidate
        return generate_client_order_id(self._config_local.client_order_id_tag)

    # -- lifecycle ---------------------------------------------------------

    async def _connect(self) -> None:
        await self._push_account_state()
        self._poll_task = self.create_task(self._poll_loop())
        env = "testnet" if self._config_local.is_testnet else "MAINNET"
        self._log.info(f"Gate.io spot execution client connected ({env})")

    async def _disconnect(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        await self._run(self._http.close)

    # -- account state -----------------------------------------------------

    async def _push_account_state(self) -> None:
        try:
            raw = await self._run(self._http.balances)
        except Exception as exc:
            self._log.warning(f"Balance fetch failed: {str(exc)[:100]}")
            return
        balances: list[AccountBalance] = []
        for code, entry in raw.items():
            free = float(entry.get("available", 0))
            locked = float(entry.get("locked", 0))
            total = free + locked
            if total <= 0:
                continue
            currency = get_currency(code)
            balances.append(
                AccountBalance(
                    Money(total, currency), Money(locked, currency), Money(free, currency)
                )
            )
        if balances:
            self.generate_account_state(
                balances=balances,
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )

    # -- order submission --------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        self.generate_order_submitted(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            self._clock.timestamp_ns(),
        )
        pair = instrument_id_to_gate_pair(order.instrument_id)
        side = "buy" if order.side == OrderSide.BUY else "sell"
        amount = str(order.quantity)
        text = self._exchange_text(order.client_order_id)
        try:
            if order.order_type == OrderType.MARKET:
                # Emulate MARKET with an aggressive LIMIT IOC crossing the spread,
                # avoiding Gate.io's quote-amount semantics for spot market buys.
                last = await self._run(self._http.ticker_last, pair)
                price = last * (1.01 if side == "buy" else 0.99)
                spec = await self._run(self._http.currency_pair, pair)
                price = round(price, int(spec.get("price_precision", 2)))
                raw = await self._run(
                    self._http.place_order, pair, side, amount, str(price), "limit", text, "ioc"
                )
            elif order.order_type == OrderType.LIMIT:
                raw = await self._run(
                    self._http.place_order,
                    pair,
                    side,
                    amount,
                    str(order.price),
                    "limit",
                    text,
                    "gtc",
                )
            else:
                self.generate_order_rejected(
                    order.strategy_id,
                    order.instrument_id,
                    order.client_order_id,
                    f"order type not supported by Gate.io spot adapter: {order.order_type}",
                    self._clock.timestamp_ns(),
                )
                return
        except Exception as exc:
            self.generate_order_rejected(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                str(exc)[:150],
                self._clock.timestamp_ns(),
            )
            return
        venue_order_id = VenueOrderId(str(raw.get("id")))
        self.generate_order_accepted(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            venue_order_id,
            self._clock.timestamp_ns(),
        )
        self._pending[str(raw.get("id"))] = _PendingOrder(
            order.instrument_id,
            order.strategy_id,
            order.client_order_id,
            pair,
            venue_order_id,
        )
        # Aggressive orders usually fill immediately — check shortly after submit.
        await asyncio.sleep(0.6)
        await self._check_fills(str(raw.get("id")))

    # -- fill detection ----------------------------------------------------

    async def _check_fills(self, order_id: str) -> None:
        pending = self._pending.get(order_id)
        if pending is None:
            return
        try:
            raw: dict[str, Any] = await self._run(self._http.order, order_id, pending.pair)
        except Exception:
            return  # transient — the poll loop retries
        amount = float(raw.get("amount", 0) or 0)
        left = float(raw.get("left", 0) or 0)
        base_filled = round(amount - left, 12)
        status = raw.get("status")
        new_fill = round(base_filled - pending.reported_fill, 12)
        if new_fill > 1e-12:
            price = float(raw.get("avg_price") or raw.get("price") or 0)
            if not price:
                price = await self._run(self._http.ticker_last, pending.pair)
            side = raw.get("side")
            fee = float(raw.get("fee", 0) or 0)
            fee_currency = raw.get("fee_currency") or (
                pending.pair.split("_")[0] if side == "buy" else pending.pair.split("_")[1]
            )
            instrument = self._cache.instrument(pending.instrument_id)
            quantity = instrument.make_qty(new_fill) if instrument else Quantity(new_fill, 8)
            last_price = instrument.make_price(price) if instrument else Price(price, 8)
            quote_currency = instrument.quote_currency if instrument else USDT
            try:
                self.generate_order_filled(
                    pending.strategy_id,
                    pending.instrument_id,
                    pending.client_order_id,
                    pending.venue_order_id,
                    None,
                    TradeId(f"{order_id}-{int(round(pending.reported_fill * 1e8))}"),
                    OrderSide.BUY if side == "buy" else OrderSide.SELL,
                    OrderType.LIMIT,
                    quantity,
                    last_price,
                    quote_currency,
                    Money(fee, get_currency(fee_currency)),
                    LiquiditySide.TAKER,
                    self._clock.timestamp_ns(),
                )
                self._log.info(f"Fill {order_id}: qty={quantity} px={last_price}")
            except Exception as exc:
                self._log.error(f"generate_order_filled failed: {str(exc)[:150]}")
            pending.reported_fill = base_filled
            await self._push_account_state()
        if status in ("closed", "cancelled"):
            if status == "cancelled" and order_id not in self._cancelling:
                # Cancelled by the exchange (e.g. unfilled IOC remainder) —
                # surface the terminal state to Nautilus.
                self.generate_order_canceled(
                    pending.strategy_id,
                    pending.instrument_id,
                    pending.client_order_id,
                    pending.venue_order_id,
                    self._clock.timestamp_ns(),
                )
            self._cancelling.discard(order_id)
            self._pending.pop(order_id, None)

    async def _poll_loop(self) -> None:
        interval = self._config_local.account_poll_interval_secs
        while True:
            try:
                for order_id in list(self._pending.keys()):
                    await self._check_fills(order_id)
                await self._push_account_state()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._log.warning(f"Poll loop error: {str(exc)[:100]}")
            await asyncio.sleep(interval)

    # -- cancels / modify ---------------------------------------------------

    async def _cancel_order(self, command: CancelOrder) -> None:
        pair = instrument_id_to_gate_pair(command.instrument_id)
        venue_order_id = command.venue_order_id
        if venue_order_id is None:
            self._log.warning(f"Cannot cancel {command.client_order_id}: no venue order id yet")
            return
        self._cancelling.add(str(venue_order_id))
        try:
            await self._run(self._http.cancel_order, str(venue_order_id), pair)
            self.generate_order_canceled(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                venue_order_id,
                self._clock.timestamp_ns(),
            )
            self._pending.pop(str(venue_order_id), None)
        except Exception as exc:
            self._cancelling.discard(str(venue_order_id))
            self._log.warning(f"Cancel failed: {str(exc)[:100]}")

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        pair = instrument_id_to_gate_pair(command.instrument_id)
        try:
            for order in await self._run(self._http.open_orders, pair):
                try:
                    self._cancelling.add(str(order["id"]))
                    await self._run(self._http.cancel_order, str(order["id"]), pair)
                except Exception:
                    self._cancelling.discard(str(order["id"]))
        except Exception as exc:
            self._log.warning(f"Cancel-all failed: {str(exc)[:100]}")

    async def _modify_order(self, command: ModifyOrder) -> None:
        self._log.warning(
            "Order modification is not supported by the Gate.io spot adapter; "
            "cancel and re-submit at the strategy level"
        )

    async def _query_account(self, command) -> None:
        await self._push_account_state()

    # -- reconciliation reports (fresh-start semantics) ----------------------

    async def generate_order_status_report(self, command) -> None:
        return None

    async def generate_order_status_reports(self, command) -> list:
        return []

    async def generate_fill_reports(self, command) -> list:
        return []

    async def generate_position_status_reports(self, command) -> list:
        return []  # spot CASH account — no positions
