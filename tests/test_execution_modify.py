"""Modification that *works*, and the two body fields an amend must never rebuild.

The suite covered every way an amend can fail — off-tick prices, fractional
contract counts, an order the cache does not hold, ambiguous transports — and
nothing that drove a ``ModifyOrder`` through to an ``OrderUpdated``. The three
claims that were therefore unasserted are the ones a strategy depends on:

* the amend reaches Gate.io's **native** ``amend_order`` endpoint and never
  degrades into cancel-and-create, which would surrender queue priority and can
  double-fill;
* what came back is applied to the order — a modify that changes the venue and
  emits nothing leaves the order ``PENDING_UPDATE`` with the *old* price while
  the venue works the new one;
* the request states the amendment and nothing else.

The last point is why the sign of ``size`` and the ``reduce_only`` flag are
tested in this module rather than beside the submit-body tests. Gate.io's futures
amend body carries ``size`` and ``price`` only: the side is *re-derived* from the
cached order and re-encoded as the sign of ``size``, and ``reduce_only`` is not
restated at all because the venue keeps it on the order. Both facts hold only
while the amend is native. A cancel-and-create in disguise would have to rebuild
them from scratch, and each is a silent, expensive loss — an inverted sign turns
a reduction into a reversal, and a dropped reduce-only flag opens exposure on a
closing order.

Coverage: TC-E30/TC-E31 (amend a limit price, both sides, spot and perpetual),
TC-E32/TC-E33 (cancel-replace keeps a fresh identity), TC-E61 (reduce-only on a
regular, non-conditional contract order) and the sign-of-``size`` requirement.
"""

from __future__ import annotations

from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder, ModifyOrder, SubmitOrder
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType
from nautilus_trader.model.events import (
    OrderCanceled,
    OrderDenied,
    OrderFilled,
    OrderModifyRejected,
    OrderPendingUpdate,
    OrderUpdated,
)
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Price, Quantity

from gateio_nt.common.enums import GateioProductType, GateioSpotAccountMode
from gateio_nt.common.errors import UnsupportedOrderError

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        FUT_BTC_USDT,
        OPT_BTC_USDT,
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
        _submit,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
        FUT_BTC_USDT,
        OPT_BTC_USDT,
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
        _submit,
    )


# -- helpers ------------------------------------------------------------------


SPOT_QTY = Quantity.from_str("0.010000")
PERP_QTY = Quantity.from_int(10)


def _pending_update(env: ExecHarness, order: Any) -> None:
    """Apply the ``OrderPendingUpdate`` the execution engine emits before the command."""
    order.apply(
        OrderPendingUpdate(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            account_id=order.account_id,
            event_id=UUID4(),
            ts_event=env.clock.timestamp_ns(),
            ts_init=env.clock.timestamp_ns(),
        ),
    )
    env.cache.update_order(order)


def _modify(
    env: ExecHarness,
    order: Any,
    *,
    price: Price | None = None,
    quantity: Quantity | None = None,
) -> None:
    env.run(
        env.client._modify_order(
            ModifyOrder(
                trader_id=env.trader_id,
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                quantity=quantity,
                price=price,
                trigger_price=None,
                command_id=UUID4(),
                ts_init=env.clock.timestamp_ns(),
            ),
        ),
    )


def _amend_body(call: Any) -> dict[str, Any]:
    """The body an ``amend_order`` call carried, whichever way it was passed."""
    body = call.kwargs.get("body")
    if body is None:
        body = call.args[-1]
    assert isinstance(body, dict)
    return body


def _spot_frame(order: Any, price: str, amount: str, left: str) -> dict[str, Any]:
    return {
        "id": "1001",
        "currency_pair": "BTC_USDT",
        "text": f"t-{order.client_order_id.value}",
        "status": "open",
        "amount": amount,
        "left": left,
        "filled_amount": "0",
        "price": price,
        "update_time_ms": 1_700_000_000_000,
    }


def _perp_frame(order: Any, price: str, contracts: int) -> dict[str, Any]:
    """A futures order frame, with ``size`` signed the way the venue signs it."""
    signed = contracts if order.side == OrderSide.BUY else -contracts
    return {
        "id": 5001,
        "contract": "BTC_USDT",
        "text": f"t-{order.client_order_id.value}",
        "status": "open",
        "size": signed,
        "left": signed,
        "price": price,
        "update_time_ms": 1_700_000_000_000,
    }


# -- TC-E30 / TC-E31: an amend that succeeds ---------------------------------


class TestTheAmendNamesItsLedger:
    """An amendment has to address the ledger the order actually lives on.

    Every other spot command names it — submit puts it in the body, cancel,
    cancel-all and batch cancel put it in the query, and so do the report
    queries. The amend did not, and Gate.io documents the default set for
    ``PATCH /spot/orders/{id}`` as "spot, unified account and isolated margin
    account": a cross-margin order is not in it, so an amendment that omits the
    parameter does not address the order the client is holding.
    """

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (GateioSpotAccountMode.SPOT, "spot"),
            (GateioSpotAccountMode.MARGIN, "margin"),
            (GateioSpotAccountMode.CROSS_MARGIN, "cross_margin"),
        ],
        ids=["spot", "isolated-margin", "cross-margin"],
    )
    def test_the_spot_amend_states_the_configured_ledger(self, mode, expected: str):
        env = ExecHarness(spot_account_mode=mode)
        try:
            order = env.accepted(
                env.order_factory.limit(
                    SPOT_BTC_USDT, OrderSide.BUY, SPOT_QTY, Price.from_str("60000.00")
                ),
                "1001",
            )
            _pending_update(env, order)
            env.spot.responses["amend_order"] = _spot_frame(
                order, price="59000.00", amount="0.010000", left="0.010000"
            )

            _modify(env, order, price=Price.from_str("59000.00"))

            call = env.spot.calls_named("amend_order")[0]
            assert call.kwargs.get("account") == expected, (
                "the amendment went out without naming the ledger the order is on; "
                f"the client is configured for {expected!r} and Gate.io resolves an "
                "unqualified amend against its own default set"
            )
        finally:
            env.close()


class TestSuccessfulAmend:
    @pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL], ids=["buy", "sell"])
    def test_a_spot_price_amend_emits_order_updated_with_the_new_price(self, side: OrderSide):
        """TC-E30/TC-E31 on spot: the request states the price, the order takes it."""
        env = ExecHarness()
        try:
            order = env.accepted(
                env.order_factory.limit(SPOT_BTC_USDT, side, SPOT_QTY, Price.from_str("60000.00")),
                "1001",
            )
            _pending_update(env, order)
            env.spot.responses["amend_order"] = _spot_frame(
                order,
                price="59000.00",
                amount="0.010000",
                left="0.010000",
            )

            _modify(env, order, price=Price.from_str("59000.00"))
            events = env.drain(order)

            calls = env.spot.calls_named("amend_order")
            assert len(calls) == 1
            assert calls[0].args[0] == "1001"
            assert calls[0].args[1] == "BTC_USDT"
            assert _amend_body(calls[0]) == {"price": "59000.00"}, (
                "an amend states what changed; restating the quantity would re-queue an "
                "order that only moved in price"
            )

            updated = [event for event in events if isinstance(event, OrderUpdated)]
            assert len(updated) == 1, f"expected exactly one OrderUpdated, got {events}"
            assert updated[0].price == Price.from_str("59000.00")
            assert updated[0].quantity == SPOT_QTY
            assert order.price == Price.from_str("59000.00")
            assert order.quantity == SPOT_QTY
            assert order.status == OrderStatus.ACCEPTED, (
                "an order left in PENDING_UPDATE is in flight forever: the strategy's "
                "re-quote logic waits on the update it never gets"
            )
            assert env.events_of(OrderModifyRejected) == []
        finally:
            env.close()

    @pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL], ids=["buy", "sell"])
    def test_a_perpetual_price_amend_emits_order_updated_with_the_new_price(self, side: OrderSide):
        """TC-E30/TC-E31 on the contract product, where the body has no ``side`` field."""
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.accepted(
                env.order_factory.limit(PERP_BTC_USDT, side, PERP_QTY, Price.from_str("61000.0")),
                "5001",
            )
            _pending_update(env, order)
            env.perp.responses["amend_order"] = _perp_frame(order, price="60500.0", contracts=10)

            _modify(env, order, price=Price.from_str("60500.0"))
            events = env.drain(order)

            calls = env.perp.calls_named("amend_order")
            assert len(calls) == 1
            assert calls[0].args[0] == "5001"
            assert _amend_body(calls[0]) == {"price": "60500.0"}, (
                "a price-only amend must not restate `size`: on Gate.io futures the sign of "
                "`size` is the side, so an unrequested size in the body is an unrequested "
                "statement about direction"
            )

            updated = [event for event in events if isinstance(event, OrderUpdated)]
            assert len(updated) == 1, f"expected exactly one OrderUpdated, got {events}"
            assert updated[0].price == Price.from_str("60500.0")
            assert updated[0].quantity == PERP_QTY
            assert order.price == Price.from_str("60500.0")
            assert order.side == side, "an amend changes the order, not its direction"
            assert order.status == OrderStatus.ACCEPTED
        finally:
            env.close()

    def test_the_amend_is_native_and_never_becomes_cancel_and_create(self):
        """A modify that is really a cancel-and-create loses queue priority silently.

        It is also unsafe: between the cancel and the create the order is not on
        the book at all, and a venue that filled the original before the cancel
        landed leaves the strategy holding two executions where it asked for one.
        Nothing in the ``OrderUpdated`` the caller sees would say so, which is
        exactly why it is pinned here.
        """
        env = ExecHarness()
        try:
            order = env.accepted(
                env.order_factory.limit(
                    SPOT_BTC_USDT,
                    OrderSide.BUY,
                    SPOT_QTY,
                    Price.from_str("60000.00"),
                ),
                "1001",
            )
            _pending_update(env, order)
            env.spot.responses["amend_order"] = _spot_frame(
                order,
                price="59000.00",
                amount="0.010000",
                left="0.010000",
            )

            _modify(env, order, price=Price.from_str("59000.00"))
            env.drain(order)

            assert env.spot.called("amend_order")
            assert not env.spot.called("cancel_order")
            assert not env.spot.called("cancel_batch")
            assert not env.spot.called("create_order")
            assert env.events_of(OrderCanceled) == []
            assert order.venue_order_id == VenueOrderId("1001"), (
                "the amended order keeps the venue's handle; a new id means a new order"
            )
        finally:
            env.close()

    def test_a_spot_quantity_amend_states_the_size_and_leaves_the_price_alone(self):
        env = ExecHarness()
        try:
            order = env.accepted(
                env.order_factory.limit(
                    SPOT_BTC_USDT,
                    OrderSide.BUY,
                    SPOT_QTY,
                    Price.from_str("60000.00"),
                ),
                "1001",
            )
            _pending_update(env, order)
            env.spot.responses["amend_order"] = _spot_frame(
                order,
                price="60000.00",
                amount="0.004000",
                left="0.004000",
            )

            _modify(env, order, quantity=Quantity.from_str("0.004000"))
            events = env.drain(order)

            body = _amend_body(env.spot.calls_named("amend_order")[0])
            assert body == {"amount": "0.004000"}
            assert "price" not in body, (
                "a quantity-only amend that also restates the price would move an order "
                "the strategy asked only to shrink"
            )

            updated = [event for event in events if isinstance(event, OrderUpdated)]
            assert len(updated) == 1
            assert updated[0].quantity == Quantity.from_str("0.004000")
            assert updated[0].price == Price.from_str("60000.00")
            assert order.quantity == Quantity.from_str("0.004000")
            assert order.price == Price.from_str("60000.00")
        finally:
            env.close()

    def test_both_fields_can_move_in_one_request(self):
        env = ExecHarness()
        try:
            order = env.accepted(
                env.order_factory.limit(
                    SPOT_BTC_USDT,
                    OrderSide.SELL,
                    SPOT_QTY,
                    Price.from_str("60000.00"),
                ),
                "1001",
            )
            _pending_update(env, order)
            env.spot.responses["amend_order"] = _spot_frame(
                order,
                price="61000.00",
                amount="0.020000",
                left="0.020000",
            )

            _modify(
                env,
                order,
                price=Price.from_str("61000.00"),
                quantity=Quantity.from_str("0.020000"),
            )
            env.drain(order)

            assert _amend_body(env.spot.calls_named("amend_order")[0]) == {
                "amount": "0.020000",
                "price": "61000.00",
            }
            assert order.price == Price.from_str("61000.00")
            assert order.quantity == Quantity.from_str("0.020000")
            assert order.status == OrderStatus.ACCEPTED
        finally:
            env.close()


# -- the sign of `size` is the side ------------------------------------------


class TestTheSignOfSizeCarriesTheSide:
    """On Gate.io contract products nothing in an order body names a side.

    ``POST /futures/{settle}/orders`` and the amend that follows it carry
    ``size`` and nothing else about direction: a positive ``size`` is long, a
    negative one is short. An inverted sign is therefore not an off-by-one, it is
    the opposite trade — and it costs twice the position, because closing a long
    with a "sell" that arrives as a buy doubles the exposure instead of clearing
    it. The same convention runs backwards on the way in, so both directions are
    asserted.
    """

    @pytest.mark.parametrize(
        ("side", "expected"),
        [(OrderSide.BUY, 4), (OrderSide.SELL, -4)],
        ids=["buy", "sell"],
    )
    def test_an_amended_contract_size_takes_its_sign_from_the_order_side(
        self,
        side: OrderSide,
        expected: int,
    ):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.accepted(
                env.order_factory.limit(PERP_BTC_USDT, side, PERP_QTY, Price.from_str("61000.0")),
                "5001",
            )
            _pending_update(env, order)
            env.perp.responses["amend_order"] = _perp_frame(order, price="61000.0", contracts=4)

            _modify(env, order, quantity=Quantity.from_int(4))
            env.drain(order)

            body = _amend_body(env.perp.calls_named("amend_order")[0])
            assert body["size"] == expected
            assert order.side == side
            assert order.quantity == Quantity.from_int(4)
        finally:
            env.close()

    @pytest.mark.parametrize(
        ("side", "expected"),
        [(OrderSide.BUY, 10), (OrderSide.SELL, -10)],
        ids=["buy", "sell"],
    )
    @pytest.mark.parametrize(
        ("instrument_id", "api", "order_type"),
        [
            (PERP_BTC_USDT, "perp", OrderType.LIMIT),
            (PERP_BTC_USDT, "perp", OrderType.MARKET),
            (FUT_BTC_USDT, "delivery", OrderType.LIMIT),
            (FUT_BTC_USDT, "delivery", OrderType.MARKET),
        ],
        ids=["perp-limit", "perp-market", "delivery-limit", "delivery-market"],
    )
    def test_a_submitted_contract_order_states_its_side_as_the_sign_of_size(
        self,
        instrument_id: Any,
        api: str,
        order_type: OrderType,
        side: OrderSide,
        expected: int,
    ):
        env = ExecHarness(products=(GateioProductType.PERP, GateioProductType.FUT))
        try:
            if order_type == OrderType.MARKET:
                order = env.order_factory.market(instrument_id, side, Quantity.from_int(10))
            else:
                order = env.order_factory.limit(
                    instrument_id,
                    side,
                    Quantity.from_int(10),
                    Price.from_str("61000.0"),
                )
            _submit(env, order)

            calls = getattr(env, api).calls_named("create_order")
            assert len(calls) == 1, f"expected one submit, got {getattr(env, api).calls}"
            body = calls[0].args[0]
            assert body["size"] == expected
            assert "side" not in body, (
                "Gate.io contract orders have no `side` field; a client that added one "
                "would look right while the venue read only the sign"
            )
            assert env.events_of(OrderDenied) == []
        finally:
            env.close()

    @pytest.mark.parametrize(
        ("signed_size", "expected_side"),
        [(10, OrderSide.BUY), (-10, OrderSide.SELL)],
        ids=["long", "short"],
    )
    def test_a_signed_size_read_back_from_the_venue_decodes_to_the_same_side(
        self,
        signed_size: int,
        expected_side: OrderSide,
    ):
        """The other direction of the same convention.

        An order this client does not hold — another session's, or one that
        survived a restart — is recovered from the sign alone. Reading it as an
        unsigned count loses the direction, and reconciliation then rebuilds the
        position with the wrong sign.
        """
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.client._handle_order_payload(
                GateioProductType.PERP,
                {
                    "id": 7001,
                    "contract": "BTC_USDT",
                    "text": "someone-elses-order",
                    "status": "open",
                    "size": signed_size,
                    "left": signed_size,
                    "price": "61000.0",
                    "create_time_ms": 1_700_000_000_000,
                    "update_time_ms": 1_700_000_000_000,
                },
            )

            assert len(env.reports) == 1, f"expected one order status report, got {env.reports}"
            report = env.reports[0]
            assert report.order_side == expected_side
            assert report.quantity == Quantity.from_int(10), (
                "the quantity is the magnitude; a signed quantity is not representable and "
                "would raise or wrap"
            )
        finally:
            env.close()

    def test_a_negative_size_in_an_amend_answer_does_not_flip_the_quantity(self):
        """The venue restates a short amend as ``size: -4``; the order stays 4 short."""
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.accepted(
                env.order_factory.limit(
                    PERP_BTC_USDT,
                    OrderSide.SELL,
                    PERP_QTY,
                    Price.from_str("61000.0"),
                ),
                "5001",
            )
            _pending_update(env, order)
            env.perp.responses["amend_order"] = _perp_frame(order, price="61000.0", contracts=4)

            _modify(env, order, quantity=Quantity.from_int(4))
            events = env.drain(order)

            updated = [event for event in events if isinstance(event, OrderUpdated)]
            assert len(updated) == 1
            assert updated[0].quantity == Quantity.from_int(4)
            assert order.quantity == Quantity.from_int(4)
            assert order.side == OrderSide.SELL
        finally:
            env.close()


# -- TC-E61: reduce-only on a regular contract order --------------------------


class TestReduceOnlyOnRegularOrders:
    """The flag that decides whether a closing order closes or reverses.

    The conditional-order path has a test for this; the regular one — the path a
    plain "close my position" market or limit order takes — had none. Dropping
    the flag does not fail: the order is accepted, fills, and takes the position
    through zero to the same size the other way. The account then carries twice
    the intended exposure with no error anywhere.
    """

    @pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL], ids=["buy", "sell"])
    @pytest.mark.parametrize(
        ("instrument_id", "api", "raw_symbol"),
        [
            (PERP_BTC_USDT, "perp", "BTC_USDT"),
            (FUT_BTC_USDT, "delivery", "BTC_USDT_20260807"),
        ],
        ids=["perp", "delivery"],
    )
    @pytest.mark.parametrize(
        "order_type", [OrderType.MARKET, OrderType.LIMIT], ids=["market", "limit"]
    )
    def test_a_regular_contract_order_carries_reduce_only_to_the_venue(
        self,
        instrument_id: Any,
        api: str,
        raw_symbol: str,
        side: OrderSide,
        order_type: OrderType,
    ):
        env = ExecHarness(products=(GateioProductType.PERP, GateioProductType.FUT))
        try:
            if order_type == OrderType.MARKET:
                order = env.order_factory.market(
                    instrument_id,
                    side,
                    Quantity.from_int(10),
                    reduce_only=True,
                )
            else:
                order = env.order_factory.limit(
                    instrument_id,
                    side,
                    Quantity.from_int(10),
                    Price.from_str("61000.0"),
                    reduce_only=True,
                )
            _submit(env, order)

            calls = getattr(env, api).calls_named("create_order")
            assert len(calls) == 1, f"the order never reached Gate.io: {getattr(env, api).calls}"
            body = calls[0].args[0]
            assert body["reduce_only"] is True
            assert body["contract"] == raw_symbol
            assert body["size"] == (10 if side == OrderSide.BUY else -10), (
                "the flag must not cost the sign: reduce-only on the wrong direction is "
                "refused by the venue, and reduce-only with a flipped sign is refused for "
                "the wrong reason"
            )
            assert env.events_of(OrderDenied) == []
        finally:
            env.close()

    def test_an_options_order_carries_reduce_only_too(self):
        env = ExecHarness(products=(GateioProductType.OPT,))
        try:
            order = env.order_factory.limit(
                OPT_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(3),
                Price.from_str("100"),
                reduce_only=True,
            )
            _submit(env, order)

            calls = env.options.calls_named("create_order")
            assert len(calls) == 1
            assert calls[0].args[0]["reduce_only"] is True
            assert calls[0].args[0]["size"] == -3
        finally:
            env.close()

    @pytest.mark.parametrize(
        "order_type", [OrderType.MARKET, OrderType.LIMIT], ids=["market", "limit"]
    )
    def test_an_order_that_is_not_reduce_only_carries_no_such_key(self, order_type: OrderType):
        """Absent, not ``False``.

        Gate.io reads the key's presence, and this is the regression guard for
        the cheapest possible mistake in the opposite direction: a builder that
        always writes the field would mark every opening order reduce-only, and
        the venue would refuse every one of them on an account with no position.
        """
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            if order_type == OrderType.MARKET:
                order = env.order_factory.market(
                    PERP_BTC_USDT,
                    OrderSide.BUY,
                    Quantity.from_int(10),
                )
            else:
                order = env.order_factory.limit(
                    PERP_BTC_USDT,
                    OrderSide.BUY,
                    Quantity.from_int(10),
                    Price.from_str("61000.0"),
                )
            _submit(env, order)

            body = env.perp.calls_named("create_order")[0].args[0]
            assert "reduce_only" not in body, f"an opening order was marked reduce-only: {body}"
        finally:
            env.close()

    @pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL], ids=["buy", "sell"])
    def test_spot_has_no_reduce_only_so_the_order_is_refused_rather_than_stripped(
        self,
        side: OrderSide,
    ):
        """Where the flag does not apply, the order does not go.

        Gate.io spot has no reduce-only concept, so there is nothing to send and
        two ways to be wrong: sending the key (the venue refuses the order, or
        worse, ignores it) and dropping it (the order is submitted meaning
        something the strategy did not ask for). This client does neither — it
        refuses locally, before anything is announced to the venue.
        """
        env = ExecHarness()
        try:
            order = env.order_factory.limit(
                SPOT_BTC_USDT,
                side,
                SPOT_QTY,
                Price.from_str("60000.00"),
                reduce_only=True,
            )

            with pytest.raises(UnsupportedOrderError, match="reduce-only"):
                env.run(env.client._build_spot_order(order, env.instruments[0], "BTC_USDT"))

            _submit(env, order)
            assert not env.spot.called("create_order"), (
                "a spot order carrying a flag the venue cannot express must not be sent at all"
            )
            denied = env.events_of(OrderDenied)
            assert len(denied) == 1
            assert "reduce-only" in denied[0].reason
        finally:
            env.close()

    def test_the_amend_body_does_not_restate_reduce_only(self):
        """Which is only safe because the amend is native.

        Gate.io keeps ``reduce_only`` on the order it already holds, so an amend
        that carried the flag again would be redundant — and one that carried it
        as ``False`` would quietly clear it. The claim is asserted together with
        the endpoint, because a cancel-and-create implementation would have to
        re-send the flag and this assertion would then be actively wrong.
        """
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.accepted(
                env.order_factory.limit(
                    PERP_BTC_USDT,
                    OrderSide.SELL,
                    PERP_QTY,
                    Price.from_str("61000.0"),
                    reduce_only=True,
                ),
                "5001",
            )
            _pending_update(env, order)
            env.perp.responses["amend_order"] = _perp_frame(order, price="60500.0", contracts=10)

            _modify(env, order, price=Price.from_str("60500.0"))
            env.drain(order)

            body = _amend_body(env.perp.calls_named("amend_order")[0])
            assert body == {"price": "60500.0"}
            assert not env.perp.called("create_order")
            assert not env.perp.called("cancel_order")
            assert order.is_reduce_only
            assert order.status == OrderStatus.ACCEPTED
        finally:
            env.close()


# -- TC-E32 / TC-E33: cancel-replace keeps two identities --------------------


class TestCancelReplace:
    def test_a_cancel_then_resubmit_uses_a_fresh_identity(self):
        """Gate.io has no replace, so a strategy cancels and submits again.

        The replacement is a different order and must be a different identity
        end to end — its own client order id, its own ``text`` alias and its own
        venue order id. The damage from sharing one is late execution: Gate.io
        does not order ``*.orders`` against ``*.usertrades``, so a fill for the
        cancelled order routinely arrives after the replacement is live, and a
        client that resolved it by anything looser than the id would book it
        against the new order and report a position that was never taken.
        """
        env = ExecHarness()
        try:
            original = env.accepted(
                env.order_factory.limit(
                    SPOT_BTC_USDT,
                    OrderSide.BUY,
                    SPOT_QTY,
                    Price.from_str("60000.00"),
                ),
                "1001",
            )
            env.spot.responses["cancel_order"] = {
                "id": "1001",
                "currency_pair": "BTC_USDT",
                "text": f"t-{original.client_order_id.value}",
                "status": "cancelled",
                "amount": "0.010000",
                "left": "0.010000",
                "filled_amount": "0",
                "update_time_ms": 1_700_000_000_000,
            }
            env.run(
                env.client._cancel_order(
                    CancelOrder(
                        trader_id=env.trader_id,
                        strategy_id=original.strategy_id,
                        instrument_id=original.instrument_id,
                        client_order_id=original.client_order_id,
                        venue_order_id=original.venue_order_id,
                        command_id=UUID4(),
                        ts_init=env.clock.timestamp_ns(),
                    ),
                ),
            )
            env.drain(original)
            assert original.status == OrderStatus.CANCELED

            replacement = env.order_factory.limit(
                SPOT_BTC_USDT,
                OrderSide.BUY,
                SPOT_QTY,
                Price.from_str("59000.00"),
            )
            ack = _spot_frame(replacement, price="59000.00", amount="0.010000", left="0.010000")
            ack["id"] = "1002"
            env.spot.responses["create_order"] = ack
            env.run(
                env.client._submit_order(
                    SubmitOrder(
                        trader_id=env.trader_id,
                        strategy_id=replacement.strategy_id,
                        order=env.add_order(replacement),
                        command_id=UUID4(),
                        ts_init=env.clock.timestamp_ns(),
                    ),
                ),
            )
            env.drain(replacement)
            env.client._handle_ws_message(
                GateioProductType.SPOT,
                {"channel": "spot.orders", "event": "update", "result": [ack]},
            )
            env.drain(replacement)

            assert replacement.client_order_id != original.client_order_id
            submitted = env.spot.calls_named("create_order")[0].args[0]
            assert submitted["text"] == f"t-{replacement.client_order_id.value}"
            assert submitted["text"] != f"t-{original.client_order_id.value}"
            assert replacement.status == OrderStatus.ACCEPTED
            assert replacement.venue_order_id == VenueOrderId("1002")

            # The cancelled order's last fill arrives after the replacement is live.
            env.client._handle_fill_payload(
                GateioProductType.SPOT,
                {
                    "id": "t-99",
                    "currency_pair": "BTC_USDT",
                    "order_id": "1001",
                    "text": f"t-{original.client_order_id.value}",
                    "side": "buy",
                    "amount": "0.010000",
                    "price": "60000.00",
                    "fee": "0",
                    "fee_currency": "USDT",
                    "create_time_ms": 1_700_000_000_002,
                },
            )
            late = env.drain(replacement)

            assert [event for event in late if isinstance(event, OrderFilled)] == [], (
                f"a fill for the cancelled order reached the replacement: {late}"
            )
            assert replacement.filled_qty == Quantity.zero(SPOT_QTY.precision)
            assert replacement.venue_order_id == VenueOrderId("1002")
        finally:
            env.close()
