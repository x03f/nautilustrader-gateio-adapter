"""``SubmitOrderList``: what Gate.io can carry, and what it must refuse.

Two independent failures live behind this command, and neither is visible in a
request body.

**Contingency.** Gate.io does carry attached take-profit / stop-loss, and a
Nautilus bracket looks like it maps straight onto it. Neither the spot shape
(``stop_profit``/``stop_loss``) nor the futures shape (``tpsl_*_trigger_price``)
carries a client-supplied identifier for the attached leg, so the three Nautilus
orders would reach the venue as one order with one id. Announcing
``OrderSubmitted`` for legs that can never acquire a venue order id is not
untidy, it is destructive: ``LiveExecutionEngine._resolve_inflight_order`` turns
a ``SUBMITTED`` order the venue cannot identify into ``OrderRejected`` once the
in-flight retries are spent, and the strategy is told its stop-loss was rejected
while Gate.io holds it live against the position. A test that asserted
``body["tpsl_sl_trigger_price"] == ...`` would pass on exactly that code, which
is why nothing here asserts a body shape for a bracket.

**Silence.** Before this method existed the inherited coroutine raised, the
task's done-callback logged the traceback and dropped it, and the orders the
execution engine had already cached stayed at ``INITIALIZED``: not in-flight, so
``_check_inflight_orders`` never queried them, and not open, so
``_handle_missing_orders_at_venue`` never reconciled them. No event, no terminal
state, nothing to wait on. The assertions below are therefore about *events and
order status*, never about documentation.

The batch assertions all turn on one venue fact this repository already records:
HTTP 200 does not mean the orders were accepted. Per-item outcome lives in the
body, so a mixed response must reject exactly the failed order and leave the
others alone.
"""

from __future__ import annotations

from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.model.enums import (
    ContingencyType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.events import (
    OrderDenied,
    OrderRejected,
    OrderSubmitted,
)
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import LimitOrder

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioClientError, GateioServerError
from nautilus_gateio.http.client import GateioRequestAmbiguousError
from nautilus_gateio.instruments import parse_spot_instrument
from tests.test_execution_orders import (
    FUT_BTC_USDT,
    PERP_BTC_USDT,
    SPOT_BTC_USDT,
    SPOT_PAIR_PAYLOAD,
    ExecHarness,
)

# -- fixtures and helpers -----------------------------------------------------


@pytest.fixture
def harness():
    env = ExecHarness()
    yield env
    env.close()


@pytest.fixture
def multi_harness():
    env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
    yield env
    env.close()


def _submit_list(env: ExecHarness, orders: list[Any]) -> None:
    order_list = env.order_factory.create_list(orders)
    for order in order_list.orders:
        env.add_order(order)
    env.run(
        env.client._submit_order_list(
            SubmitOrderList(
                trader_id=env.trader_id,
                strategy_id=env.strategy_id,
                order_list=order_list,
                command_id=UUID4(),
                ts_init=env.clock.timestamp_ns(),
            ),
        ),
    )


def _submit_order_list(env: ExecHarness, order_list: Any) -> None:
    for order in order_list.orders:
        env.add_order(order)
    env.run(
        env.client._submit_order_list(
            SubmitOrderList(
                trader_id=env.trader_id,
                strategy_id=env.strategy_id,
                order_list=order_list,
                command_id=UUID4(),
                ts_init=env.clock.timestamp_ns(),
            ),
        ),
    )


def _spot_limit(env: ExecHarness, side: OrderSide, quantity: str, price: str) -> Any:
    return env.order_factory.limit(
        SPOT_BTC_USDT,
        side,
        Quantity.from_str(quantity),
        Price.from_str(price),
    )


def _perp_limit(env: ExecHarness, side: OrderSide, contracts: int, price: str) -> Any:
    return env.order_factory.limit(
        PERP_BTC_USDT,
        side,
        Quantity.from_int(contracts),
        Price.from_str(price),
    )


def _spot_instruments(count: int) -> list[Any]:
    """Build ``count`` distinct spot pairs, so a batch can span more than one."""
    bases = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOT")
    return [
        parse_spot_instrument(dict(SPOT_PAIR_PAYLOAD, id=f"{base}_USDT", base=base))
        for base in bases[:count]
    ]


def _text_of(env: ExecHarness, order: Any) -> str:
    return f"t-{order.client_order_id.value}"


def _ack(env: ExecHarness, order: Any, venue_order_id: str, **extra: Any) -> dict[str, Any]:
    """A successful spot batch row, as Gate.io answers one."""
    row = {
        "succeeded": True,
        "id": venue_order_id,
        "text": _text_of(env, order),
        "currency_pair": "BTC_USDT",
        "status": "open",
        "amount": str(order.quantity),
        "left": str(order.quantity),
        "price": str(order.price),
        "create_time_ms": 1785000000000,
    }
    row.update(extra)
    return row


def _refusal(env: ExecHarness, order: Any, label: str, message: str) -> dict[str, Any]:
    return {
        "succeeded": False,
        "label": label,
        "message": message,
        "text": _text_of(env, order),
        "currency_pair": "BTC_USDT",
    }


def _statuses(env: ExecHarness, orders: list[Any]) -> list[OrderStatus]:
    """Apply every generated event to the orders it names, then read them back.

    Applying through the real ``Order.apply`` is the point: an event sequence the
    venue could not produce, or one this client emits in the wrong order, raises
    out of the finite state machine instead of passing quietly.
    """
    by_id = {order.client_order_id: order for order in orders}
    for event in env.drain():
        order = by_id.get(event.client_order_id)
        if order is not None:
            order.apply(event)
            env.cache.update_order(order)
    return [order.status for order in orders]


# -- contingency: the refusal that protects a position ------------------------


class TestAContingentListIsRefusedWholesale:
    @staticmethod
    def _bracket(env: ExecHarness, contingency_type: ContingencyType) -> Any:
        return env.order_factory.bracket(
            instrument_id=SPOT_BTC_USDT,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str("0.010000"),
            contingency_type=contingency_type,
            entry_order_type=OrderType.LIMIT,
            entry_price=Price.from_str("59000.00"),
            sl_trigger_price=Price.from_str("58000.00"),
            tp_price=Price.from_str("61000.00"),
        )

    def test_every_leg_of_a_bracket_is_denied_and_nothing_is_sent(self, harness):
        bracket = self._bracket(harness, ContingencyType.OUO)
        assert bracket.is_bracket()

        _submit_order_list(harness, bracket)

        denied = harness.events_of(OrderDenied)
        assert len(denied) == 3, harness.events
        assert {event.client_order_id for event in denied} == {
            order.client_order_id for order in bracket.orders
        }
        assert harness.events_of(OrderSubmitted) == [], "nothing reached Gate.io"
        assert not harness.spot.called("create_order")
        assert not harness.spot.called("create_batch_orders")
        assert not harness.spot.called("create_price_order")

    def test_the_denial_names_the_venue_fact_and_the_way_round_it(self, harness):
        bracket = self._bracket(harness, ContingencyType.OUO)

        _submit_order_list(harness, bracket)

        reason = harness.events_of(OrderDenied)[0].reason
        assert "no order id for either leg" in reason
        assert "emulation_trigger" in reason

    def test_no_leg_is_left_without_a_terminal_state(self, harness):
        """The regression: three orders used to sit at ``INITIALIZED`` forever.

        ``INITIALIZED`` is neither in-flight nor open, so neither reconciliation
        loop in ``LiveExecutionEngine`` would ever look at them again.
        """
        bracket = self._bracket(harness, ContingencyType.OUO)

        _submit_order_list(harness, bracket)

        assert _statuses(harness, list(bracket.orders)) == [OrderStatus.DENIED] * 3
        assert all(order.is_closed for order in bracket.orders)

    def test_an_oco_bracket_is_denied_although_is_bracket_says_no(self, harness):
        """``is_bracket()`` requires both children to be ``OUO``.

        Gating the refusal on it would let an ``OCO`` list through to the batch
        path, where both exits go live at the venue and nothing ever cancels the
        losing one — the platform only manages contingencies locally, and only
        when the strategy opts into ``manage_contingent_orders`` (default False).
        """
        bracket = self._bracket(harness, ContingencyType.OCO)
        assert not bracket.is_bracket()

        _submit_order_list(harness, bracket)

        assert len(harness.events_of(OrderDenied)) == 3
        assert not harness.spot.called("create_batch_orders")

    def test_a_single_linked_leg_refuses_the_whole_list(self, harness):
        """Linkage is a property of the list, so the refusal has to be as well.

        Submitting the unlinked members and denying the linked ones would leave
        a strategy holding an entry with no exits and no event saying so.
        """
        first = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        second = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        # Only this leg carries a link, which is enough to refuse all three.
        third = LimitOrder(
            trader_id=harness.trader_id,
            strategy_id=harness.strategy_id,
            instrument_id=SPOT_BTC_USDT,
            client_order_id=ClientOrderId("O-LINKED-1"),
            order_side=OrderSide.SELL,
            quantity=Quantity.from_str("0.030000"),
            price=Price.from_str("62000.00"),
            init_id=UUID4(),
            ts_init=harness.clock.timestamp_ns(),
            contingency_type=ContingencyType.OCO,
            linked_order_ids=[second.client_order_id],
        )
        order_list = harness.order_factory.create_list([first, second, third])

        _submit_order_list(harness, order_list)

        assert len(harness.events_of(OrderDenied)) == 3
        assert not harness.spot.called("create_batch_orders")


# -- the plain list: every order is submitted ---------------------------------


class TestAPlainListIsBatched:
    def test_an_empty_list_warns_and_does_nothing(self, harness):
        """``OrderList`` cannot be constructed empty, so the command is built directly."""

        class _EmptyList:
            orders: list[Any] = []

        class _Command:
            order_list = _EmptyList()

        harness.run(harness.client._submit_order_list(_Command()))

        assert harness.events == []
        assert not harness.spot.called("create_batch_orders")

    def test_two_spot_orders_go_out_as_one_batch_request(self, harness):
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = [
            _ack(harness, buy, "1001"),
            _ack(harness, sell, "1002"),
        ]

        _submit_list(harness, [buy, sell])

        calls = harness.spot.calls_named("create_batch_orders")
        assert len(calls) == 1
        assert not harness.spot.called("create_order")
        bodies = calls[0].body
        assert [body["text"] for body in bodies] == [
            _text_of(harness, buy),
            _text_of(harness, sell),
        ]
        assert [body["side"] for body in bodies] == ["buy", "sell"]
        assert [body["amount"] for body in bodies] == ["0.010000", "0.020000"]

    def test_every_order_is_announced_before_the_request_leaves(self, harness):
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = [
            _ack(harness, buy, "1001"),
            _ack(harness, sell, "1002"),
        ]

        _submit_list(harness, [buy, sell])

        submitted = harness.events_of(OrderSubmitted)
        assert {event.client_order_id for event in submitted} == {
            buy.client_order_id,
            sell.client_order_id,
        }
        assert _statuses(harness, [buy, sell]) == [OrderStatus.SUBMITTED] * 2

    def test_a_single_order_list_uses_the_single_order_endpoint(self, harness):
        """One order is not a batch: the single endpoint answers with the order itself."""
        only = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        harness.spot.responses["create_order"] = _ack(harness, only, "1001")

        _submit_list(harness, [only])

        assert harness.spot.called("create_order")
        assert not harness.spot.called("create_batch_orders")

    def test_a_leg_this_client_refuses_is_denied_and_the_others_still_go(self, harness):
        """A local refusal is still an ``OrderDenied``, decided before any submission."""
        good_one = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        good_two = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        refused = harness.order_factory.limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.030000"),
            Price.from_str("62000.00"),
            reduce_only=True,  # spot has no reduce-only flag
        )
        harness.spot.responses["create_batch_orders"] = [
            _ack(harness, good_one, "1001"),
            _ack(harness, good_two, "1002"),
        ]

        _submit_list(harness, [good_one, refused, good_two])

        denied = harness.events_of(OrderDenied)
        assert [event.client_order_id for event in denied] == [refused.client_order_id]
        bodies = harness.spot.calls_named("create_batch_orders")[0].body
        assert [body["text"] for body in bodies] == [
            _text_of(harness, good_one),
            _text_of(harness, good_two),
        ]

    def test_a_conditional_order_never_joins_a_batch(self, harness):
        """A price order addresses another endpoint and answers with an armed id."""
        plain_one = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        plain_two = _spot_limit(harness, OrderSide.BUY, "0.020000", "58000.00")
        conditional = harness.order_factory.limit_if_touched(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("0.030000"),
            Price.from_str("57000.00"),
            Price.from_str("57500.00"),
        )
        harness.spot.responses["tickers"] = [{"last": "60000"}]
        harness.spot.responses["create_price_order"] = {"id": 9001}
        harness.spot.responses["create_batch_orders"] = [
            _ack(harness, plain_one, "1001"),
            _ack(harness, plain_two, "1002"),
        ]

        _submit_list(harness, [plain_one, conditional, plain_two])

        assert len(harness.spot.calls_named("create_price_order")) == 1
        bodies = harness.spot.calls_named("create_batch_orders")[0].body
        assert len(bodies) == 2
        assert harness.events_of(OrderDenied) == []


class TestProductsWithoutABatchEndpoint:
    def test_delivery_futures_are_submitted_one_at_a_time(self):
        """The wrapper raises ``ValueError`` for delivery; it is never called.

        A ``ValueError`` escaping here would land in ``create_task``'s exception
        log and strand both orders at ``INITIALIZED`` — the exact defect this
        method exists to remove. Denying them instead would be inventing a venue
        restriction: each order is perfectly submittable on its own.
        """
        env = ExecHarness(products=(GateioProductType.FUT,))
        try:
            first = env.order_factory.limit(
                FUT_BTC_USDT,
                OrderSide.BUY,
                Quantity.from_int(3),
                Price.from_str("59000.0"),
            )
            second = env.order_factory.limit(
                FUT_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(5),
                Price.from_str("61000.0"),
            )

            _submit_list(env, [first, second])

            assert not env.delivery.called("create_batch_orders")
            assert len(env.delivery.calls_named("create_order")) == 2
            assert env.events_of(OrderDenied) == []
            assert _statuses(env, [first, second]) == [OrderStatus.SUBMITTED] * 2
        finally:
            env.close()

    def test_a_group_over_the_venue_cap_falls_back_to_single_submissions(self, harness):
        """Eleven orders on one spot pair exceed the ten-per-pair cap.

        Chunking is the option not taken: a half-applied chunk is an ambiguity
        class this client would then have to model, while eleven single
        submissions behave exactly like eleven ``submit_order`` commands.
        """
        orders = [
            _spot_limit(harness, OrderSide.BUY, "0.010000", f"{59000 + index}.00")
            for index in range(11)
        ]

        _submit_list(harness, orders)

        assert not harness.spot.called("create_batch_orders")
        assert len(harness.spot.calls_named("create_order")) == 11
        assert harness.events_of(OrderDenied) == []

    @pytest.mark.parametrize(
        ("pairs", "batched"),
        [(4, True), (5, False)],
        ids=["four-pairs", "five-pairs"],
    )
    def test_the_spot_pair_cap_is_counted_separately_from_the_order_cap(
        self,
        pairs: int,
        batched: bool,
    ):
        """Four pairs of two orders is a legal request; five pairs is not.

        Eight orders are under every per-pair limit, so a client that only
        counted orders would send the five-pair list as one batch and Gate.io
        would refuse the whole request.
        """
        instruments = _spot_instruments(pairs)
        env = ExecHarness(instruments=instruments)
        try:
            orders = [
                env.order_factory.limit(
                    instrument.id,
                    OrderSide.BUY,
                    Quantity.from_str("0.010000"),
                    Price.from_str(f"{59000 + index}.00"),
                )
                for index, instrument in enumerate(instruments)
                for _ in range(2)
            ]

            _submit_list(env, orders)

            assert env.spot.called("create_batch_orders") is batched
            assert env.spot.called("create_order") is not batched
            assert env.events_of(OrderDenied) == []
        finally:
            env.close()

    def test_ten_orders_on_one_pair_still_batch(self, harness):
        orders = [
            _spot_limit(harness, OrderSide.BUY, "0.010000", f"{59000 + index}.00")
            for index in range(10)
        ]

        _submit_list(harness, orders)

        assert len(harness.spot.calls_named("create_batch_orders")) == 1
        assert not harness.spot.called("create_order")

    def test_a_list_spanning_two_products_batches_each_one_separately(self, multi_harness):
        spot_one = _spot_limit(multi_harness, OrderSide.BUY, "0.010000", "59000.00")
        perp_one = _perp_limit(multi_harness, OrderSide.BUY, 3, "59000.0")
        spot_two = _spot_limit(multi_harness, OrderSide.SELL, "0.020000", "61000.00")
        perp_two = _perp_limit(multi_harness, OrderSide.SELL, 5, "61000.0")

        _submit_list(multi_harness, [spot_one, perp_one, spot_two, perp_two])

        spot_bodies = multi_harness.spot.calls_named("create_batch_orders")[0].body
        perp_bodies = multi_harness.perp.calls_named("create_batch_orders")[0].body
        assert [body["currency_pair"] for body in spot_bodies] == ["BTC_USDT"] * 2
        assert [body["contract"] for body in perp_bodies] == ["BTC_USDT"] * 2
        assert [body["size"] for body in perp_bodies] == [3, -5]


# -- the batch response: HTTP 200 is not acceptance ---------------------------


class TestABatchResponseIsReadPerItem:
    def test_only_the_failed_order_is_rejected(self, harness):
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = [
            _ack(harness, buy, "1001"),
            _refusal(harness, sell, "BALANCE_NOT_ENOUGH", "not enough BTC"),
        ]

        _submit_list(harness, [buy, sell])

        rejected = harness.events_of(OrderRejected)
        assert [event.client_order_id for event in rejected] == [sell.client_order_id]
        assert "BALANCE_NOT_ENOUGH" in rejected[0].reason
        assert _statuses(harness, [buy, sell]) == [OrderStatus.SUBMITTED, OrderStatus.REJECTED]

    def test_attribution_follows_the_client_id_not_the_row_position(self, harness):
        """The failure must land on the order it names, whatever order they arrive in.

        Gate.io documents the results as index-aligned, and this client sends its
        own client id in ``text``, so a shifted response is recoverable. Under
        index-only matching this response would reject the buy — an order the
        venue accepted — and report the refused sell as live.
        """
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = [
            _refusal(harness, sell, "BALANCE_NOT_ENOUGH", "not enough BTC"),
            _ack(harness, buy, "1001"),
        ]

        _submit_list(harness, [buy, sell])

        rejected = harness.events_of(OrderRejected)
        assert [event.client_order_id for event in rejected] == [sell.client_order_id]

    def test_a_row_with_no_succeeded_flag_is_not_read_as_a_refusal(self, harness):
        """An absent flag is not a refusal, and ``OrderRejected`` is terminal.

        Reading a missing field as failure would reject an order Gate.io is
        holding live, and no later event could undo it: ``Order.apply`` raises
        ``InvalidStateTrigger`` on the ``OrderAccepted`` reconciliation would
        then have to emit.
        """
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        rows = [_ack(harness, buy, "1001"), _ack(harness, sell, "1002")]
        for row in rows:
            del row["succeeded"]
        harness.spot.responses["create_batch_orders"] = rows

        _submit_list(harness, [buy, sell])

        assert harness.events_of(OrderRejected) == []
        assert _statuses(harness, [buy, sell]) == [OrderStatus.SUBMITTED] * 2

    def test_an_order_the_response_never_mentioned_stays_in_flight(
        self,
        harness,
        log_capture,
    ):
        """Silence about an order is not acceptance and not refusal.

        ``SUBMITTED`` is what ``_check_inflight_orders`` looks for, so leaving it
        there hands the question to the engine, which queries the venue. The
        status alone does not distinguish "left in flight deliberately" from
        "never noticed", and the platform's own answer to an ambiguous outcome is
        a log line and no event, so the line is what is asserted.
        """
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = [_ack(harness, buy, "1001")]

        log_capture.mark()
        _submit_list(harness, [buy, sell])

        assert harness.events_of(OrderRejected) == []
        assert harness.events_of(OrderDenied) == []
        assert _statuses(harness, [buy, sell]) == [OrderStatus.SUBMITTED] * 2
        lines = log_capture.wait_for("carried no result for it")
        assert any(
            "[WARN]" in line
            and sell.client_order_id.value in line
            and "carried no result for it" in line
            for line in lines
        ), lines
        assert not any(buy.client_order_id.value in line for line in lines if "unresolved" in line)

    def test_a_row_naming_an_order_from_elsewhere_is_not_applied(self, harness):
        """A ``text`` this batch never sent cannot be attributed to anything here."""
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        stranger = _refusal(harness, buy, "BALANCE_NOT_ENOUGH", "not enough")
        stranger["text"] = "t-SOMEONE-ELSE-1"
        harness.spot.responses["create_batch_orders"] = [
            _ack(harness, buy, "1001"),
            stranger,
        ]

        _submit_list(harness, [buy, sell])

        assert harness.events_of(OrderRejected) == []
        assert _statuses(harness, [buy, sell]) == [OrderStatus.SUBMITTED] * 2

    def test_a_post_only_refusal_keeps_its_flag(self, harness):
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = [
            _ack(harness, buy, "1001"),
            _refusal(harness, sell, "ORDER_POC_IMMEDIATE", "would take liquidity"),
        ]

        _submit_list(harness, [buy, sell])

        rejected = harness.events_of(OrderRejected)
        assert len(rejected) == 1
        assert rejected[0].due_post_only

    def test_a_futures_refusal_reads_the_detail_field(self, multi_harness):
        """Spot reports ``message``; futures reports ``detail``."""
        first = _perp_limit(multi_harness, OrderSide.BUY, 3, "59000.0")
        second = _perp_limit(multi_harness, OrderSide.SELL, 5, "61000.0")
        multi_harness.perp.responses["create_batch_orders"] = [
            {"succeeded": True, "id": 1001, "text": _text_of(multi_harness, first)},
            {
                "succeeded": False,
                "label": "INVALID_PARAM_VALUE",
                "detail": "size too small",
                "text": _text_of(multi_harness, second),
            },
        ]

        _submit_list(multi_harness, [first, second])

        rejected = multi_harness.events_of(OrderRejected)
        assert [event.client_order_id for event in rejected] == [second.client_order_id]
        assert "size too small" in rejected[0].reason


class TestABatchThatFailedAsAWhole:
    def test_an_ambiguous_batch_leaves_every_order_in_flight(self, harness):
        """Nobody knows what the venue did, and both endpoints are never replayed.

        Rejecting here would be unrecoverable: ``OrderRejected`` is terminal, so
        an order Gate.io actually placed could never be represented locally
        again. Retrying would be worse — a replay of a partially applied batch
        doubles the orders that did succeed.
        """
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = GateioRequestAmbiguousError(
            0,
            "REQUEST_AMBIGUOUS",
            "POST /spot/batch_orders failed after the request was sent",
        )

        _submit_list(harness, [buy, sell])

        assert harness.events_of(OrderRejected) == []
        assert len(harness.spot.calls_named("create_batch_orders")) == 1, "never replayed"
        assert not harness.spot.called("create_order"), "and never retried one by one"
        assert _statuses(harness, [buy, sell]) == [OrderStatus.SUBMITTED] * 2

    def test_a_server_failure_is_ambiguous_too(self, harness):
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = GateioServerError(
            502,
            "SERVER_ERROR",
            "bad gateway",
        )

        _submit_list(harness, [buy, sell])

        assert harness.events_of(OrderRejected) == []
        assert _statuses(harness, [buy, sell]) == [OrderStatus.SUBMITTED] * 2

    def test_a_proven_refusal_of_the_request_rejects_every_order(self, harness):
        """A malformed item rejects the whole futures request with HTTP 400.

        The venue answered "I placed nothing", which is a proof about every item,
        so leaving them in flight would strand orders that do not exist.
        """
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        harness.spot.responses["create_batch_orders"] = GateioClientError(
            400,
            "INVALID_PARAM_VALUE",
            "malformed batch item",
        )

        _submit_list(harness, [buy, sell])

        rejected = harness.events_of(OrderRejected)
        assert {event.client_order_id for event in rejected} == {
            buy.client_order_id,
            sell.client_order_id,
        }
        assert _statuses(harness, [buy, sell]) == [OrderStatus.REJECTED] * 2


class TestNoOrderIsEverLeftAtInitialized:
    """The property the whole method exists for, asserted directly.

    ``INITIALIZED`` is invisible to both reconciliation loops, so an order left
    there is lost for the life of the process. After the command every order must
    be either in flight (``SUBMITTED``, which ``_check_inflight_orders`` queries)
    or closed.
    """

    @pytest.mark.parametrize(
        "batch_response",
        [
            None,
            GateioRequestAmbiguousError(0, "REQUEST_AMBIGUOUS", "unknown"),
            GateioClientError(400, "INVALID_PARAM_VALUE", "refused"),
            [],
        ],
        ids=["accepted", "ambiguous", "refused", "empty-response"],
    )
    def test_every_order_leaves_initialized(self, harness, batch_response: Any):
        buy = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        sell = _spot_limit(harness, OrderSide.SELL, "0.020000", "61000.00")
        if batch_response is None:
            batch_response = [_ack(harness, buy, "1001"), _ack(harness, sell, "1002")]
        harness.spot.responses["create_batch_orders"] = batch_response

        _submit_list(harness, [buy, sell])

        for status in _statuses(harness, [buy, sell]):
            assert status != OrderStatus.INITIALIZED

    def test_a_time_in_force_the_venue_cannot_express_is_denied_not_dropped(self, harness):
        """The whole list is refused order by order, never abandoned mid-way."""
        good = _spot_limit(harness, OrderSide.BUY, "0.010000", "59000.00")
        bad = harness.order_factory.limit(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("0.020000"),
            Price.from_str("58000.00"),
            time_in_force=TimeInForce.AT_THE_OPEN,
        )

        _submit_list(harness, [good, bad])

        assert [event.client_order_id for event in harness.events_of(OrderDenied)] == [
            bad.client_order_id,
        ]
        assert _statuses(harness, [good, bad]) == [OrderStatus.SUBMITTED, OrderStatus.DENIED]
