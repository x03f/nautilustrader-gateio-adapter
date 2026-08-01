"""Cancellation that *works*: the request sent and the event that comes back.

The rest of this suite is rich in cancellation failures — ambiguous transports,
whole-batch errors, orders the venue no longer holds — and had nothing on the
ordinary success. That is the wrong way round: cancelling a resting order is the
most-used command in the client, and its failure mode is silence. An order whose
cancel produced no ``OrderCanceled`` stays ``PENDING_CANCEL`` in the cache; the
strategy that is waiting to re-quote never does, and the engine's in-flight check
is the only thing left that can free it.

Every test here therefore asserts both halves of the round trip:

* **the request** — which namespace was called, which method on it, and the
  arguments Gate.io needs to identify the order. Spot, futures and delivery use
  three different endpoints with three different signatures, so "a cancel was
  sent" is not a claim; "``spot.cancel_order('1001', 'BTC_USDT', account='spot')``
  was sent, and no bulk or batch endpoint was touched" is.
* **the event** — through the real ``Order`` finite state machine, so an event
  this client emits in the wrong order, or a second event for an order already
  closed, raises out of ``Order.apply`` instead of passing quietly.

Coverage: TC-E40 (cancel a single limit), TC-E41/TC-E81 (cancel-all reaches the
plain resting orders, not only the armed triggers) and TC-E43 (a batch every row
of which succeeded).
"""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import BatchCancelOrders, CancelAllOrders, CancelOrder
from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.events import OrderCanceled, OrderCancelRejected
from nautilus_trader.model.identifiers import InstrumentId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity

from gateio_nt.common.enums import GateioProductType

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        FUT_BTC_USDT,
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
        FUT_BTC_USDT,
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
    )


# -- products -----------------------------------------------------------------


class Product(NamedTuple):
    """One tradable product, with everything a cancel round trip needs on it."""

    product: GateioProductType
    instrument_id: InstrumentId
    api: str  #: the attribute on ``ExecHarness`` holding that product's stub namespace
    raw_symbol: str
    quantity: Quantity
    price: Price


SPOT = Product(
    GateioProductType.SPOT,
    SPOT_BTC_USDT,
    "spot",
    "BTC_USDT",
    Quantity.from_str("0.010000"),
    Price.from_str("60000.00"),
)
PERP = Product(
    GateioProductType.PERP,
    PERP_BTC_USDT,
    "perp",
    "BTC_USDT",
    Quantity.from_int(10),
    Price.from_str("61000.0"),
)
DELIVERY = Product(
    GateioProductType.FUT,
    FUT_BTC_USDT,
    "delivery",
    "BTC_USDT_20260807",
    Quantity.from_int(10),
    Price.from_str("61000.0"),
)

PRODUCTS = {"spot": SPOT, "perp": PERP, "delivery": DELIVERY}

#: Every cancellation endpoint the client owns. A cancel of one resting order
#: must reach exactly one of them; the others being untouched is what makes the
#: assertion about the first one mean something.
CANCEL_ENDPOINTS = (
    "cancel_order",
    "cancel_all",
    "cancel_batch",
    "cancel_price_order",
    "cancel_price_orders",
)


def _harness(case: Product) -> ExecHarness:
    return ExecHarness(products=(case.product,))


def _api(env: ExecHarness, case: Product) -> Any:
    return getattr(env, case.api)


def _resting_limit(env: ExecHarness, case: Product, side: OrderSide, price: Price | None = None):
    return env.order_factory.limit(
        case.instrument_id,
        side,
        case.quantity,
        price or case.price,
    )


def _canceled_payload(case: Product, venue_order_id: str, order: Any) -> dict[str, Any]:
    """The order object Gate.io answers a successful cancel with.

    Shapes copied from the live venue: spot states ``status``/``amount``/``left``
    in base units, the contract products state a *signed* ``size`` and report the
    termination through ``finish_as``.
    """
    text = f"t-{order.client_order_id.value}"
    if case.product.is_spot:
        return {
            "id": venue_order_id,
            "currency_pair": case.raw_symbol,
            "text": text,
            "status": "cancelled",
            "amount": str(case.quantity),
            "left": str(case.quantity),
            "filled_amount": "0",
            "update_time_ms": 1_700_000_000_000,
        }
    signed = int(case.quantity.as_decimal()) * (1 if order.side == OrderSide.BUY else -1)
    return {
        "id": int(venue_order_id),
        "contract": case.raw_symbol,
        "text": text,
        "status": "finished",
        "finish_as": "cancelled",
        "size": signed,
        "left": signed,
        "finish_time_ms": 1_700_000_000_000,
    }


def _cancel(env: ExecHarness, order: Any) -> None:
    env.run(
        env.client._cancel_order(
            CancelOrder(
                trader_id=env.trader_id,
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                command_id=UUID4(),
                ts_init=env.clock.timestamp_ns(),
            ),
        ),
    )


def _cancel_command(env: ExecHarness, order: Any) -> CancelOrder:
    return CancelOrder(
        trader_id=env.trader_id,
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=order.venue_order_id,
        command_id=UUID4(),
        ts_init=env.clock.timestamp_ns(),
    )


def _drain_all(env: ExecHarness, orders: list[Any]) -> list[Any]:
    """Apply the new events to whichever of ``orders`` each one names.

    ``ExecHarness.drain`` takes a single order and advances a shared cursor, so
    calling it once per order would hand the first call every event and apply
    only the ones that happen to name its order — the rest would be silently
    dropped and every later assertion about their status would read the
    pre-command state.
    """
    events = env.drain()
    by_id = {order.client_order_id: order for order in orders}
    for event in events:
        order = by_id.get(event.client_order_id)
        if order is not None:
            order.apply(event)
            env.cache.update_order(order)
    return events


def _endpoints_called(env: ExecHarness) -> set[tuple[str, str]]:
    """Every (namespace, cancellation endpoint) pair the client actually called."""
    called = set()
    for name in ("spot", "margin", "options", "perp", "inverse", "delivery"):
        api = getattr(env, name)
        for endpoint in CANCEL_ENDPOINTS:
            if api.called(endpoint):
                called.add((name, endpoint))
    return called


# -- TC-E40: cancel one resting limit ----------------------------------------


class TestCancelSucceeds:
    @pytest.mark.parametrize("case", PRODUCTS.values(), ids=PRODUCTS)
    @pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL], ids=["buy", "sell"])
    def test_a_plain_limit_cancel_emits_exactly_one_order_canceled(
        self,
        case: Product,
        side: OrderSide,
    ):
        """TC-E40: the venue accepts the cancel and the engine is told, once.

        The order is a plain resting limit — not an armed conditional, which is
        the only cancel path the suite covered before and which goes through a
        different endpoint (``cancel_price_order``) with a different id.
        """
        env = _harness(case)
        try:
            order = env.accepted(_resting_limit(env, case, side), "1001")
            _api(env, case).responses["cancel_order"] = _canceled_payload(case, "1001", order)

            _cancel(env, order)
            events = env.drain(order)

            # -- the request -------------------------------------------------
            calls = _api(env, case).calls_named("cancel_order")
            assert len(calls) == 1, f"expected one cancel request, got {_api(env, case).calls}"
            assert calls[0].args[0] == "1001", (
                "Gate.io identifies the order by the venue order id it issued; sending "
                "anything else cancels nothing and the order rests on forever"
            )
            if case.product.is_spot:
                # Spot needs the pair and the account or the venue 400s.
                assert calls[0].args[1] == case.raw_symbol
                assert calls[0].kwargs["account"] == "spot"
            else:
                # The futures and delivery endpoints take the id alone.
                assert calls[0].args[1:] == ()

            assert _endpoints_called(env) == {(case.api, "cancel_order")}, (
                "a single cancel goes to the single-order endpoint of its own product: a "
                "bulk or batch endpoint would cancel orders nobody asked to cancel, and the "
                "wrong product's namespace signs against the wrong wallet"
            )

            # -- the event ---------------------------------------------------
            canceled = [event for event in events if isinstance(event, OrderCanceled)]
            assert len(canceled) == 1, f"expected exactly one OrderCanceled, got {events}"
            assert canceled[0].client_order_id == order.client_order_id
            assert canceled[0].venue_order_id == VenueOrderId("1001")
            assert env.events_of(OrderCancelRejected) == []
            assert order.status == OrderStatus.CANCELED
            assert order.filled_qty == Quantity.zero(order.quantity.precision)
        finally:
            env.close()

    @pytest.mark.parametrize("case", PRODUCTS.values(), ids=PRODUCTS)
    def test_a_repeated_confirmation_frame_produces_no_second_event(self, case: Product):
        """Gate.io re-states a finished order on the stream after the REST answer.

        The REST response to the cancel and the ``*.orders`` update describing the
        same cancellation are the same object arriving twice. A second
        ``OrderCanceled`` is not a duplicate log line: ``Order.apply`` has no
        ``CANCELED -> CANCELED`` transition, so the execution engine raises
        ``InvalidStateTrigger`` on it and the strategy sees an error where a
        clean cancellation happened.
        """
        env = _harness(case)
        try:
            order = env.accepted(_resting_limit(env, case, OrderSide.BUY), "1001")
            payload = _canceled_payload(case, "1001", order)
            _api(env, case).responses["cancel_order"] = payload

            _cancel(env, order)
            env.drain(order)
            assert order.status == OrderStatus.CANCELED

            channel = "spot.orders" if case.product.is_spot else "futures.orders"
            env.client._handle_ws_message(
                case.product,
                {"channel": channel, "event": "update", "result": [payload]},
            )
            second = env.drain(order)

            assert second == [], f"the repeated frame produced {second}"
            assert len(env.events_of(OrderCanceled)) == 1
        finally:
            env.close()

    def test_three_individual_cancels_produce_three_events_and_no_cross_talk(self):
        """TC-E42: N cancels, N events, each naming its own order.

        The failure this rules out is attribution, not counting: a client that
        resolved the confirmation through the last command it sent, or through
        the pair rather than the id, would still emit three events — all three
        about one order, leaving the other two resting locally forever.
        """
        env = _harness(SPOT)
        try:
            orders = []
            for index, venue_order_id in enumerate(("1001", "1002", "1003")):
                order = env.accepted(
                    _resting_limit(
                        env,
                        SPOT,
                        OrderSide.BUY,
                        Price.from_str(f"6000{index}.00"),
                    ),
                    venue_order_id,
                )
                orders.append((order, venue_order_id))

            for order, venue_order_id in orders:
                env.spot.responses["cancel_order"] = _canceled_payload(SPOT, venue_order_id, order)
                _cancel(env, order)
                env.drain(order)

            assert [call.args[0] for call in env.spot.calls_named("cancel_order")] == [
                "1001",
                "1002",
                "1003",
            ]
            canceled = env.events_of(OrderCanceled)
            assert [(event.client_order_id, event.venue_order_id) for event in canceled] == [
                (order.client_order_id, VenueOrderId(vid)) for order, vid in orders
            ]
            assert all(order.status == OrderStatus.CANCELED for order, _ in orders)
        finally:
            env.close()

    def test_a_partly_filled_order_keeps_its_fill_through_the_cancel(self):
        """A cancelled order states what it filled before it was pulled.

        ``PARTIALLY_FILLED -> CANCELED`` keeps ``filled_qty``, and the number is
        a position: reporting the cancellation with the quantity reset would
        make the local position disagree with the venue's by the filled amount.
        """
        env = _harness(SPOT)
        try:
            order = env.accepted(_resting_limit(env, SPOT, OrderSide.BUY), "1001")
            env.client._handle_fill_payload(
                GateioProductType.SPOT,
                {
                    "id": "t-1",
                    "currency_pair": "BTC_USDT",
                    "order_id": "1001",
                    "text": f"t-{order.client_order_id.value}",
                    "side": "buy",
                    "amount": "0.004000",
                    "price": "60000.00",
                    "fee": "0",
                    "fee_currency": "USDT",
                    "create_time_ms": 1_700_000_000_000,
                },
            )
            env.drain(order)
            assert order.status == OrderStatus.PARTIALLY_FILLED

            env.spot.responses["cancel_order"] = {
                "id": "1001",
                "currency_pair": "BTC_USDT",
                "text": f"t-{order.client_order_id.value}",
                "status": "cancelled",
                "amount": "0.010000",
                "left": "0.006000",
                "filled_amount": "0.004000",
                "update_time_ms": 1_700_000_000_001,
            }
            _cancel(env, order)
            env.drain(order)

            assert order.status == OrderStatus.CANCELED
            assert order.filled_qty == Quantity.from_str("0.004000")
            assert len(env.events_of(OrderCanceled)) == 1
        finally:
            env.close()


# -- TC-E41 / TC-E81: cancel-all covers the plain orders too ------------------


class TestCancelAllReachesPlainOrders:
    def test_cancel_all_cancels_the_resting_limits_as_well_as_the_armed_trigger(self):
        """TC-E41/TC-E81: both halves of "cancel everything on this pair".

        Gate.io keeps resting orders and armed price-triggered orders in two
        separate books with two separate bulk endpoints. Every existing test of
        this path arms conditional orders only, so a client that called just the
        price-order endpoint would pass them all while leaving two live limit
        orders on the book of a strategy that has shut down.
        """
        env = _harness(SPOT)
        try:
            first = env.accepted(
                _resting_limit(env, SPOT, OrderSide.BUY, Price.from_str("59000.00")),
                "1001",
            )
            second = env.accepted(
                _resting_limit(env, SPOT, OrderSide.BUY, Price.from_str("58000.00")),
                "1002",
            )
            armed = env.order_factory.stop_market(
                SPOT_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_str("0.010000"),
                Price.from_str("57000.00"),
            )
            env.accepted(armed, "A-1")
            env.client._register_trigger_link(GateioProductType.SPOT, "A-1", armed.client_order_id)

            env.spot.responses["cancel_all"] = [
                _canceled_payload(SPOT, "1001", first),
                _canceled_payload(SPOT, "1002", second),
            ]
            env.spot.responses["cancel_price_orders"] = [{"id": "A-1"}]

            env.run(
                env.client._cancel_all_orders(
                    CancelAllOrders(
                        trader_id=env.trader_id,
                        strategy_id=env.strategy_id,
                        instrument_id=SPOT_BTC_USDT,
                        order_side=OrderSide.NO_ORDER_SIDE,
                        command_id=UUID4(),
                        ts_init=env.clock.timestamp_ns(),
                    ),
                ),
            )
            _drain_all(env, [first, second, armed])

            bulk = env.spot.calls_named("cancel_all")
            assert len(bulk) == 1
            assert bulk[0].args[0] == "BTC_USDT"
            assert bulk[0].kwargs["side"] is None, "an unscoped command names no side"
            assert env.spot.called("cancel_price_orders"), (
                "the armed order lives in the price-order book and the regular bulk cancel "
                "does not reach it"
            )

            canceled = env.events_of(OrderCanceled)
            reported = [(event.client_order_id, event.venue_order_id) for event in canceled]
            # The armed order is disarmed before the bulk responses are read, so
            # the order of the three events is the client's business; that there
            # are exactly three, one per order, each carrying its own id, is not.
            assert sorted(reported) == sorted(
                [
                    (first.client_order_id, VenueOrderId("1001")),
                    (second.client_order_id, VenueOrderId("1002")),
                    (armed.client_order_id, VenueOrderId("A-1")),
                ],
            ), f"cancel-all reported {reported}"
            assert all(order.status == OrderStatus.CANCELED for order in (first, second, armed))
        finally:
            env.close()


# -- TC-E43: a batch every row of which succeeded -----------------------------


class TestBatchCancel:
    """The request a fully successful batch sends.

    The event half of TC-E43 is *not* asserted here, and that is a statement
    about the adapter rather than about the test: Gate.io's
    ``POST /spot/cancel_batch_orders`` answers with per-row
    ``{id, succeeded, label, message}`` results and no order object, and
    :meth:`GateioExecutionClient._batch_cancel_orders` skips every succeeded row
    without generating anything — so a batch whose rows all succeeded emits no
    event at all and leaves every order ``PENDING_CANCEL`` until the execution
    engine's in-flight check resolves it. Asserting the current silence would
    freeze the defect into the suite, so what is pinned here is the half that is
    right — that every order reaches the venue, in one request, addressed by its
    own venue order id — plus the definitive claim that a succeeded row is never
    reported as a refusal.
    """

    def _batch(self, env: ExecHarness, orders: list[Any]) -> None:
        env.run(
            env.client._batch_cancel_orders(
                BatchCancelOrders(
                    trader_id=env.trader_id,
                    strategy_id=env.strategy_id,
                    instrument_id=orders[0].instrument_id,
                    cancels=[_cancel_command(env, order) for order in orders],
                    command_id=UUID4(),
                    ts_init=env.clock.timestamp_ns(),
                ),
            ),
        )

    def test_a_fully_successful_batch_sends_every_order_in_one_request(self):
        """TC-E43: three orders in, three rows out — none dropped, none doubled.

        Dropping a row is invisible from the venue's answer: the venue reports
        on what it received, so a client that built two items from three cancels
        gets two successes back and nothing anywhere says the third order is
        still live.
        """
        env = _harness(SPOT)
        try:
            orders = [
                env.accepted(
                    _resting_limit(env, SPOT, OrderSide.BUY, Price.from_str(f"6000{index}.00")),
                    venue_order_id,
                )
                for index, venue_order_id in enumerate(("1001", "1002", "1003"))
            ]
            env.spot.responses["cancel_batch"] = [
                {
                    "currency_pair": "BTC_USDT",
                    "id": venue_order_id,
                    "succeeded": True,
                    "label": "",
                    "message": "",
                    "account": "spot",
                }
                for venue_order_id in ("1001", "1002", "1003")
            ]

            self._batch(env, orders)
            _drain_all(env, orders)

            calls = env.spot.calls_named("cancel_batch")
            assert len(calls) == 1, "three orders fit one request; Gate.io chunks at 20"
            assert calls[0].args[0] == [
                {"currency_pair": "BTC_USDT", "id": "1001", "account": "spot"},
                {"currency_pair": "BTC_USDT", "id": "1002", "account": "spot"},
                {"currency_pair": "BTC_USDT", "id": "1003", "account": "spot"},
            ]
            assert env.events_of(OrderCancelRejected) == [], (
                "every row succeeded; a refusal event would put an order the venue no longer "
                "holds back to ACCEPTED, and a strategy that re-quotes on it would double its "
                "exposure"
            )
            assert not env.spot.called("cancel_order"), (
                "the spot batch endpoint exists so that N cancels cost one request"
            )
        finally:
            env.close()

    def test_a_batch_of_twenty_five_is_split_and_still_carries_every_order(self):
        """The chunking must not lose the tail.

        Gate.io caps ``cancel_batch_orders`` at 20 ids. An off-by-one in the
        chunk arithmetic silently leaves the last orders uncancelled — the same
        damage as dropping a row, only harder to see.
        """
        env = _harness(SPOT)
        try:
            orders = []
            for index in range(25):
                order = env.accepted(
                    _resting_limit(
                        env,
                        SPOT,
                        OrderSide.BUY,
                        Price.from_str(f"{50000 + index}.00"),
                    ),
                    str(2000 + index),
                )
                orders.append(order)
            env.spot.responses["cancel_batch"] = lambda items: [
                {"currency_pair": "BTC_USDT", "id": item["id"], "succeeded": True} for item in items
            ]

            self._batch(env, orders)

            calls = env.spot.calls_named("cancel_batch")
            assert [len(call.args[0]) for call in calls] == [20, 5]
            sent = [item["id"] for call in calls for item in call.args[0]]
            assert sent == [str(2000 + index) for index in range(25)]
            assert env.events_of(OrderCancelRejected) == []
        finally:
            env.close()

    def test_a_batch_cancel_on_a_contract_product_falls_back_to_one_request_each(self):
        """Only the spot API has a batch cancel; futures cancel one at a time.

        The event half is reachable here, because each fallback cancel is an
        ordinary single cancel and answers with the order object.
        """
        env = _harness(PERP)
        try:
            orders = [
                env.accepted(
                    _resting_limit(env, PERP, OrderSide.SELL, Price.from_str(f"6100{index}.0")),
                    venue_order_id,
                )
                for index, venue_order_id in enumerate(("5001", "5002"))
            ]
            answers = {
                "5001": _canceled_payload(PERP, "5001", orders[0]),
                "5002": _canceled_payload(PERP, "5002", orders[1]),
            }
            env.perp.responses["cancel_order"] = lambda order_id: answers[order_id]

            self._batch(env, orders)
            _drain_all(env, orders)

            assert [call.args[0] for call in env.perp.calls_named("cancel_order")] == [
                "5001",
                "5002",
            ]
            assert not env.spot.called("cancel_batch"), (
                "there is no futures batch-cancel endpoint; signing a spot request for a "
                "futures order cancels nothing"
            )
            canceled = env.events_of(OrderCanceled)
            assert [event.venue_order_id for event in canceled] == [
                VenueOrderId("5001"),
                VenueOrderId("5002"),
            ]
            assert all(order.status == OrderStatus.CANCELED for order in orders)
            assert env.events_of(OrderCancelRejected) == []
        finally:
            env.close()
