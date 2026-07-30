"""Event-stream tests for ``GateioExecutionClient``.

Order events, fills and balance updates are driven through the client exactly as
the private WebSocket would deliver them, and every generated event is applied to
a real NautilusTrader ``Order`` so the finite state machine has to accept the
sequence.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model.enums import (
    OrderSide,
    OrderStatus,
    PositionAdjustmentType,
    TimeInForce,
)
from nautilus_trader.model.events import (
    OrderCanceled,
    OrderFilled,
    OrderTriggered,
    OrderUpdated,
)
from nautilus_trader.model.identifiers import TradeId, VenueOrderId
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.model.position import Position

from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode
from nautilus_gateio.common.errors import GateioClientError

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
    )


@pytest.fixture()
def perp_harness():
    env = ExecHarness(products=(GateioProductType.PERP,))
    yield env
    env.close()


@pytest.fixture()
def spot_harness():
    env = ExecHarness()
    yield env
    env.close()


async def _drain_tasks(env: ExecHarness) -> None:
    """Run every task the client scheduled to completion.

    Hand-overs that have to ask the venue something are scheduled rather than
    awaited, because the private WebSocket callback that starts them is
    synchronous.
    """
    del env  # the tasks belong to the running loop, not to the harness
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


#: A venue timestamp the reconnect re-query window can still reach.
#:
#: `_reconcile_after_reconnect` anchors its window on the wall clock and looks
#: back `DEFAULT_LOOKBACK_SECS`, so a fixed epoch second in these payloads ages
#: out of the window on its own and the recovery tests quietly stop exercising
#: anything.
RECENT_SECS = int(time.time()) - 60


def _futures_order_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 900001,
        "id_string": "900001",
        "contract": "BTC_USDT",
        "size": -10,
        "left": -10,
        "price": "59000.0",
        "tif": "gtc",
        "status": "open",
        "create_time": RECENT_SECS,
        "update_time": RECENT_SECS + 1,
    }
    payload.update(overrides)
    return payload


def _futures_fill_payload(trade_id: str, size: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": trade_id,
        "contract": "BTC_USDT",
        "order_id": "900001",
        "size": size,
        "price": "59000.0",
        "role": "taker",
        "fee": "0.05",
        "create_time": RECENT_SECS + 2,
    }
    payload.update(overrides)
    return payload


def _accepted_spot_buy(env: ExecHarness, venue_order_id: str = "778899") -> Any:
    """A resting spot BUY of 0.010000, submitted and accepted as a live one is."""
    order = env.order_factory.limit(
        SPOT_BTC_USDT,
        OrderSide.BUY,
        Quantity.from_str("0.010000"),
        Price.from_str("60000.00"),
        time_in_force=TimeInForce.GTC,
    )
    env.accepted(order, venue_order_id)
    env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
    return order


def _spot_order_payload(**overrides: Any) -> dict[str, Any]:
    """One ``GET /spot/orders`` row for the resting buy the spot fixtures use."""
    payload: dict[str, Any] = {
        "id": 778899,
        "id_string": "778899",
        "currency_pair": "BTC_USDT",
        "type": "limit",
        "side": "buy",
        "account": "spot",
        "amount": "0.010000",
        "left": "0.010000",
        "filled_amount": "0",
        "price": "60000.00",
        "status": "open",
        "time_in_force": "gtc",
        "create_time": RECENT_SECS,
        "update_time": RECENT_SECS + 1,
    }
    payload.update(overrides)
    return payload


def _spot_fill_payload(
    env: ExecHarness,
    order: Any,
    trade_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    """One ``spot.usertrades`` row, in the shape the venue publishes it."""
    payload: dict[str, Any] = {
        "id": trade_id,
        "currency_pair": "BTC_USDT",
        "order_id": "778899",
        "side": "buy",
        "amount": "0.010000",
        "price": "60000.00",
        "fee": "0.000010",
        "fee_currency": "BTC",
        "role": "taker",
        "create_time_ms": "1785000000000",
        "text": f"t-{order.client_order_id.value}",
    }
    payload.update(overrides)
    return payload


def _arm_futures_stop_limit(env: ExecHarness, armed_id: str = "AUTO-77") -> Any:
    """Arm a STOP_LIMIT conditional order under a venue auto-order id."""
    order = env.order_factory.stop_limit(
        PERP_BTC_USDT,
        OrderSide.SELL,
        Quantity.from_int(10),
        Price.from_str("59000.0"),
        Price.from_str("59500.0"),
    )
    env.accepted(order, armed_id)
    env.client._register_trigger_link(
        GateioProductType.PERP,
        armed_id,
        order.client_order_id,
    )
    # The venue text this client would have sent with `initial.text`.
    env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
    return order


# -- MANDATORY TEST 1: the venue-order-id rebase on trigger -------------------


class TestTriggerVenueOrderIdRebase:
    """Regression for EXEC-1: a fired conditional order gets a NEW venue id.

    ``OrderUpdated`` is the only event ``Order.apply`` accepts carrying a venue
    order id different from the one already on the order, so it has to come
    first. Emitting ``OrderTriggered`` (or a fill) first makes the FSM reject
    every subsequent event for the order, which silently loses all of its fills.
    """

    def test_updated_first_then_triggered_then_fill_applies(self, perp_harness):
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        assert order.venue_order_id == VenueOrderId("AUTO-77")

        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(text=f"t-{order.client_order_id.value}"),
        )
        events = env.drain(order)

        assert [type(event).__name__ for event in events] == ["OrderUpdated", "OrderTriggered"]
        updated, triggered = events
        assert isinstance(updated, OrderUpdated)
        assert updated.venue_order_id == VenueOrderId("900001")
        assert isinstance(triggered, OrderTriggered)
        assert order.venue_order_id == VenueOrderId("900001")
        assert order.status == OrderStatus.TRIGGERED

        # The FSM must now accept a fill carrying the NEW id.
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-1", -10),
        )
        fills = env.drain(order)
        assert len(fills) == 1
        assert isinstance(fills[0], OrderFilled)
        assert fills[0].venue_order_id == VenueOrderId("900001")
        assert order.status == OrderStatus.FILLED
        assert order.filled_qty == Quantity.from_int(10)

    def test_venue_order_id_modified_flag_is_required_and_used(self, perp_harness):
        """``generate_order_updated`` refuses a new venue id without the flag.

        The flag is not decorative: NautilusTrader validates the event's venue
        order id against the cached one unless it is set, so a rebase emitted
        without it raises instead of rebasing.
        """
        env = perp_harness
        order = _arm_futures_stop_limit(env)

        with pytest.raises(ValueError):
            env.client.generate_order_updated(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=VenueOrderId("900001"),
                quantity=order.quantity,
                price=order.price,
                trigger_price=order.trigger_price,
                ts_event=env.clock.timestamp_ns(),
            )

        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(text=f"t-{order.client_order_id.value}"),
        )
        updated = env.drain(order)[0]
        assert isinstance(updated, OrderUpdated)
        assert updated.venue_order_id == VenueOrderId("900001")
        assert env.cache.venue_order_id(order.client_order_id) == VenueOrderId("900001")

    def test_stop_market_gets_no_order_triggered(self, perp_harness):
        """Nautilus accepts ``OrderTriggered`` only for triggerable order types."""
        env = perp_harness
        order = env.order_factory.stop_market(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "AUTO-78")
        env.client._register_trigger_link(
            GateioProductType.PERP,
            "AUTO-78",
            order.client_order_id,
        )

        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(price="0", text=f"t-{order.client_order_id.value}"),
        )
        events = env.drain(order)

        assert [type(event).__name__ for event in events] == ["OrderUpdated"]
        assert order.venue_order_id == VenueOrderId("900001")

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-9", -10),
        )
        fills = env.drain(order)
        assert len(fills) == 1
        assert order.status == OrderStatus.FILLED

    def test_armed_id_is_preserved_across_the_transition(self, perp_harness):
        """Both identities stay reachable: the armed id is the venue's listing key."""
        env = perp_harness
        order = _arm_futures_stop_limit(env)

        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(text=f"t-{order.client_order_id.value}"),
        )
        env.drain(order)

        link = env.client.trigger_links[order.client_order_id]
        assert link.armed_id == "AUTO-77"
        assert link.fired_id == "900001"
        assert link.is_armed is False
        assert env.client._trigger_link_for_venue_order_id("AUTO-77") is link
        assert env.client._trigger_link_for_venue_order_id("900001") is link

    def test_repeated_payload_does_not_rebase_twice(self, perp_harness):
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        payload = _futures_order_payload(text=f"t-{order.client_order_id.value}")

        env.client._handle_order_payload(GateioProductType.PERP, payload)
        env.drain(order)
        env.client._handle_order_payload(GateioProductType.PERP, payload)
        env.drain(order)

        assert len(env.events_of(OrderUpdated)) == 1
        assert len(env.events_of(OrderTriggered)) == 1

    def test_cancel_after_trigger_uses_the_fired_id(self, perp_harness):
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.messages import CancelOrder

        env = perp_harness
        order = _arm_futures_stop_limit(env)
        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(text=f"t-{order.client_order_id.value}"),
        )
        env.drain(order)

        env.run(
            env.client._cancel_order(
                CancelOrder(
                    trader_id=env.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=env.clock.timestamp_ns(),
                ),
            ),
        )

        # The armed id no longer identifies anything cancellable.
        assert not env.perp.called("cancel_price_order")
        assert [call.args[0] for call in env.perp.calls_named("cancel_order")] == ["900001"]

    def test_cancel_while_armed_uses_the_armed_id(self, perp_harness):
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.messages import CancelOrder

        env = perp_harness
        order = _arm_futures_stop_limit(env)

        env.run(
            env.client._cancel_order(
                CancelOrder(
                    trader_id=env.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=env.clock.timestamp_ns(),
                ),
            ),
        )

        assert [call.args[0] for call in env.perp.calls_named("cancel_price_order")] == ["AUTO-77"]
        assert not env.perp.called("cancel_order")
        canceled = env.events_of(OrderCanceled)
        assert len(canceled) == 1
        assert canceled[0].venue_order_id == VenueOrderId("AUTO-77")


# -- MANDATORY TEST 2: partial and multiple fills after triggering ------------


class TestFillsAfterTrigger:
    def test_two_partial_fills_both_apply(self, perp_harness):
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(text=f"t-{order.client_order_id.value}"),
        )
        env.drain(order)

        env.client._handle_fill_payload(GateioProductType.PERP, _futures_fill_payload("T-1", -4))
        env.drain(order)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.filled_qty == Quantity.from_int(4)

        env.client._handle_fill_payload(GateioProductType.PERP, _futures_fill_payload("T-2", -6))
        env.drain(order)
        assert order.status == OrderStatus.FILLED
        assert order.filled_qty == Quantity.from_int(10)

        fills = env.events_of(OrderFilled)
        assert [fill.trade_id for fill in fills] == [TradeId("T-1"), TradeId("T-2")]
        assert all(fill.venue_order_id == VenueOrderId("900001") for fill in fills)

    def test_duplicate_trade_id_produces_no_second_fill(self, perp_harness):
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(text=f"t-{order.client_order_id.value}"),
        )
        env.drain(order)

        env.client._handle_fill_payload(GateioProductType.PERP, _futures_fill_payload("T-1", -4))
        env.drain(order)
        env.client._handle_fill_payload(GateioProductType.PERP, _futures_fill_payload("T-1", -4))
        env.drain(order)

        assert len(env.events_of(OrderFilled)) == 1
        assert order.filled_qty == Quantity.from_int(4)

    def test_fill_without_a_venue_trade_id_is_discarded(self, perp_harness):
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(text=f"t-{order.client_order_id.value}"),
        )
        env.drain(order)

        payload = _futures_fill_payload("T-1", -4)
        payload["id"] = ""
        env.client._handle_fill_payload(GateioProductType.PERP, payload)

        assert env.events_of(OrderFilled) == []


# -- EXEC-9: a fill that loses the race against the terminal order message ----


class TestFillOnClosedOrder:
    def test_fill_after_cancel_goes_to_the_reconciliation_path(self, perp_harness):
        from nautilus_trader.execution.reports import FillReport

        env = perp_harness
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")

        # The terminal order message wins the race against the fill.
        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(
                status="finished",
                finish_as="cancelled",
                text=f"t-{order.client_order_id.value}",
            ),
        )
        env.drain(order)
        assert order.is_closed

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-LATE", -4, text=f"t-{order.client_order_id.value}"),
        )

        # No OrderFilled: `Order.apply` would raise on a closed order. The fill is
        # handed to reconciliation, which knows how to apply it.
        assert env.events_of(OrderFilled) == []
        reports = [report for report in env.reports if isinstance(report, FillReport)]
        assert len(reports) == 1
        assert reports[0].trade_id == TradeId("T-LATE")
        assert reports[0].venue_order_id == VenueOrderId("900001")

    def test_a_fill_after_an_expired_order_is_not_routed_where_it_dies(self, perp_harness):
        """CANCELED can take a late fill; EXPIRED cannot, and pretending hides it.

        The platform's order state table has ``(CANCELED, PARTIALLY_FILLED)`` and
        ``(CANCELED, FILLED)`` and nothing at all out of EXPIRED
        (model/orders/base.pyx:132-163). A fill handed to reconciliation for an
        EXPIRED order therefore raises ``InvalidStateTrigger`` inside
        ``_reconcile_fill_report``, which catches it, logs it and returns False:
        the execution is discarded, and the only trace is a generic
        reconciliation error against a report nobody is looking at. The loss is
        reported here instead, where the trade, its quantity and its commission
        are still to hand.
        """
        from nautilus_trader.execution.reports import FillReport

        env = perp_harness
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        text = f"t-{order.client_order_id.value}"
        env.client._register_text(order.client_order_id, text)

        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(status="finished", finish_as="expired", text=text),
        )
        env.drain(order)
        assert order.status == OrderStatus.EXPIRED

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-LATE", -4, text=text),
        )

        assert env.events_of(OrderFilled) == []
        assert [report for report in env.reports if isinstance(report, FillReport)] == []

    def test_the_late_fill_routing_matches_the_platforms_state_table(self, perp_harness):
        """`FILLABLE_TERMINAL_STATUSES` is a claim about the FSM; hold it to it.

        The set decides whether a late fill is handed over or written off, so it
        is checked against the platform rather than against this client's idea of
        it: each terminal status is reached with the client's own events, and the
        same fill is offered to ``Order.apply``.
        """
        from nautilus_trader.core.fsm import InvalidStateTrigger

        from nautilus_gateio.execution import FILLABLE_TERMINAL_STATUSES

        accepted_from = set()
        for finish_as, status in (
            ("cancelled", OrderStatus.CANCELED),
            ("expired", OrderStatus.EXPIRED),
        ):
            env = ExecHarness(products=(GateioProductType.PERP,))
            try:
                order = env.order_factory.limit(
                    PERP_BTC_USDT,
                    OrderSide.SELL,
                    Quantity.from_int(10),
                    Price.from_str("59000.0"),
                )
                env.accepted(order, "900001")
                text = f"t-{order.client_order_id.value}"
                env.client._register_text(order.client_order_id, text)

                # Take the fill event first, while the order can still produce one.
                env.client._handle_fill_payload(
                    GateioProductType.PERP,
                    _futures_fill_payload("T-LATE", -4, text=text),
                )
                fill = env.events_of(OrderFilled)[0]

                env.client._handle_order_payload(
                    GateioProductType.PERP,
                    _futures_order_payload(status="finished", finish_as=finish_as, text=text),
                )
                for event in env.drain():
                    if not isinstance(event, OrderFilled):
                        order.apply(event)
                assert order.status == status

                try:
                    order.apply(fill)
                except InvalidStateTrigger:
                    continue
                accepted_from.add(status)
            finally:
                env.close()

        assert accepted_from == set(FILLABLE_TERMINAL_STATUSES)

    def test_a_duplicate_late_fill_is_not_reported_twice(self, perp_harness):
        from nautilus_trader.execution.reports import FillReport

        env = perp_harness
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(
                status="finished",
                finish_as="cancelled",
                text=f"t-{order.client_order_id.value}",
            ),
        )
        env.drain(order)

        payload = _futures_fill_payload("T-LATE", -4, text=f"t-{order.client_order_id.value}")
        env.client._handle_fill_payload(GateioProductType.PERP, payload)
        env.client._handle_fill_payload(GateioProductType.PERP, payload)

        reports = [report for report in env.reports if isinstance(report, FillReport)]
        assert len(reports) == 1


# -- a fill for an order this client has never seen ---------------------------


class TestUnattributedFill:
    """A trade whose order is unknown must reach the engine by a route it implements.

    ``LiveExecutionEngine._reconcile_fill_report_single`` resolves the order a
    lone ``FillReport`` belongs to only through
    ``cache.client_order_id(report.venue_order_id)`` (1.230.0,
    live/execution_engine.py:2183-2200). An id that index has never seen is
    logged as "deferring reconciliation" and dropped — there is no deferral queue
    on this version, and the one retry loop that exists is driven by
    ``position_check_interval_secs``, which defaults to ``None``. So the order is
    re-read from the venue and handed over with its trade, which is the path
    ``_reconcile_execution_mass_status`` does implement.
    """

    @staticmethod
    def _capture_mass_status(env: ExecHarness) -> list[Any]:
        captured: list[Any] = []
        env.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_mass_status",
            handler=captured.append,
        )
        return captured

    def test_an_external_fill_is_handed_over_with_the_order_it_belongs_to(self, perp_harness):
        from nautilus_trader.execution.reports import FillReport

        env = perp_harness
        mass_statuses = self._capture_mass_status(env)
        env.perp.responses["get_order"] = _futures_order_payload(left=-6, status="open")

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-EXT", -4),
        )
        env.run(_drain_tasks(env))

        # Nothing was offered on the single-report endpoint, where it would have
        # been dropped for want of an indexed venue order id.
        assert [report for report in env.reports if isinstance(report, FillReport)] == []
        assert [call.args[0] for call in env.perp.calls_named("get_order")] == ["900001"]
        assert len(mass_statuses) == 1
        assert VenueOrderId("900001") in mass_statuses[0].order_reports
        assert [
            fill.trade_id for fill in mass_statuses[0].fill_reports[VenueOrderId("900001")]
        ] == [TradeId("T-EXT")]

    def test_a_venue_that_cannot_name_the_order_loses_nothing_silently(self, perp_harness):
        """The venue not answering is the one case with nowhere left to go.

        Handing the trade over on its own would have the engine drop it with a
        log line about a deferral that never happens, so it is reported here
        instead, with the quantity, price and commission needed to account for
        it.
        """
        from nautilus_trader.execution.reports import FillReport

        env = perp_harness
        mass_statuses = self._capture_mass_status(env)
        env.perp.responses["get_order"] = GateioClientError(404, "ORDER_NOT_FOUND", "not found")

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-EXT", -4),
        )
        env.run(_drain_tasks(env))

        assert mass_statuses == []
        assert [report for report in env.reports if isinstance(report, FillReport)] == []


# -- MANDATORY TEST 4: per-wallet balances aggregate without corruption -------


class TestWalletBalanceAggregation:
    """Regression for EXEC-2: one stream tick used to replace the whole aggregate."""

    def test_spot_tick_keeps_the_futures_wallet_contribution(self):
        env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
        try:
            env.client._handle_balance_payload(
                GateioProductType.PERP,
                {"currency": "USDT", "balance": "500"},
            )
            env.client._handle_balance_payload(
                GateioProductType.SPOT,
                {"currency": "USDT", "total": "1000", "available": "900"},
            )

            total, free = env.client._balances["USDT"]
            assert total == Decimal("1500")
            assert free == Decimal("1400")

            state = env.account_states[-1]
            usdt = next(b for b in state.balances if b.currency.code == "USDT")
            assert usdt.total.as_decimal() == Decimal("1500")
        finally:
            env.close()

    def test_futures_tick_keeps_the_spot_wallet_contribution(self):
        env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
        try:
            env.client._handle_balance_payload(
                GateioProductType.SPOT,
                {"currency": "USDT", "total": "1000", "available": "900"},
            )
            env.client._handle_balance_payload(
                GateioProductType.PERP,
                {"currency": "USDT", "balance": "500"},
            )

            total, _ = env.client._balances["USDT"]
            assert total == Decimal("1500")
        finally:
            env.close()

    def test_aggregate_equals_the_sum_of_the_wallets(self):
        env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
        try:
            env.client._handle_balance_payload(
                GateioProductType.SPOT,
                {"currency": "USDT", "total": "1000", "available": "900"},
            )
            env.client._handle_balance_payload(
                GateioProductType.SPOT,
                {"currency": "BTC", "total": "2", "available": "2"},
            )
            env.client._handle_balance_payload(
                GateioProductType.PERP,
                {"currency": "USDT", "balance": "500"},
            )

            wallets = env.client._wallet_balances
            expected = sum(
                (wallet.get("USDT", (Decimal(0), Decimal(0)))[0] for wallet in wallets.values()),
                Decimal(0),
            )
            assert env.client._balances["USDT"][0] == expected
            assert env.client._balances["BTC"][0] == Decimal("2")
        finally:
            env.close()


# -- MANDATORY TEST 5: no double counting under a Unified Account -------------


class TestUnifiedAccountDoubleCounting:
    """A unified wallet already contains every product wallet.

    Gate.io keeps answering ``/spot/accounts`` and ``/futures/usdt/accounts``
    with the *same* funds once the Unified Account is active, so summing the
    wallets multiplies the account's equity by the number of enabled products.
    """

    @staticmethod
    def _build() -> ExecHarness:
        env = ExecHarness(
            products=(GateioProductType.SPOT, GateioProductType.PERP),
            spot_account_mode=GateioSpotAccountMode.UNIFIED,
        )
        env.spot.responses["accounts"] = [
            {"currency": "USDT", "available": "1000", "locked": "0"},
        ]
        env.margin.responses["unified_accounts"] = {
            "balances": {
                "USDT": {
                    "available": "1000",
                    "freeze": "0",
                    "borrowed": "0",
                    "interest": "0",
                },
            },
        }
        env.perp.responses["accounts"] = {
            "currency": "USDT",
            "total": "1000",
            "available": "1000",
            "unrealised_pnl": "0",
        }
        env.perp.responses["positions"] = []
        return env

    def test_unified_balance_replaces_the_product_wallets(self):
        env = self._build()
        try:
            env.run(env.client._update_account_state())

            total, _ = env.client._balances["USDT"]
            assert total == Decimal("1000"), "the same 1000 USDT must not be counted three times"
        finally:
            env.close()

    def test_published_account_state_is_not_inflated(self):
        env = self._build()
        try:
            env.run(env.client._update_account_state())

            state = env.account_states[-1]
            usdt = next(b for b in state.balances if b.currency.code == "USDT")
            assert usdt.total.as_decimal() == Decimal("1000")
        finally:
            env.close()

    def test_a_wallet_stream_tick_cannot_reinflate_the_aggregate(self):
        env = self._build()
        try:
            env.run(env.client._update_account_state())
            env.client._handle_balance_payload(
                GateioProductType.PERP,
                {"currency": "USDT", "balance": "1000"},
            )

            assert env.client._balances["USDT"][0] == Decimal("1000")
        finally:
            env.close()

    def test_a_currency_outside_the_unified_wallet_still_aggregates(self):
        env = self._build()
        try:
            env.run(env.client._update_account_state())
            env.client._handle_balance_payload(
                GateioProductType.SPOT,
                {"currency": "BTC", "total": "3", "available": "3"},
            )

            assert env.client._balances["BTC"][0] == Decimal("3")
            assert env.client._balances["USDT"][0] == Decimal("1000")
        finally:
            env.close()

    def test_classic_account_still_sums_every_wallet(self):
        """Without a unified wallet the aggregate must remain the plain sum."""
        env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
        try:
            env.spot.responses["accounts"] = [
                {"currency": "USDT", "available": "1000", "locked": "0"},
            ]
            env.perp.responses["accounts"] = {
                "currency": "USDT",
                "total": "500",
                "available": "500",
                "unrealised_pnl": "0",
            }
            env.perp.responses["positions"] = []
            env.run(env.client._update_account_state())

            assert env.client._balances["USDT"][0] == Decimal("1500")
        finally:
            env.close()


# -- spot base-currency fees on the event stream -----------------------------


class TestSpotBaseFeeReporting:
    """A spot fill states what the venue matched; the fee is a separate fact.

    Gate.io deducts a spot BUY fee from the currency being bought, so the wallet
    is credited ``amount - fee`` base units for a match of ``amount``. It is the
    platform, not the adapter, that reflects that in the position:
    ``Position.apply`` raises a ``PositionAdjusted(COMMISSION, -commission)`` for
    every fill on a ``CurrencyPair`` commissioned in its base currency
    (model/position.pyx:591-612), and ``apply_adjustment`` takes it off
    ``signed_qty``. An adapter that also nets the fee off ``last_qty`` has the
    same fee subtracted twice.
    """

    def test_the_fill_states_the_matched_quantity_and_the_fee_apart(self, spot_harness):
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TS-1"),
        )
        env.drain(order)

        fill = env.events_of(OrderFilled)[0]
        assert fill.last_qty == Quantity.from_str("0.010000")
        assert fill.commission == Money(Decimal("0.000010"), Currency.from_str("BTC"))

    @staticmethod
    def _position(env: ExecHarness, *fills: Any) -> Position:
        """Build the position these fills produce, as the execution engine would.

        The engine stamps a position id onto every fill before opening a position
        with it (`ExecutionEngine._determine_position_id`), and `Position`
        refuses a fill without one. The attribute is not writable from Python, so
        the events are round-tripped through the platform's own serialisation
        rather than rebuilt field by field here.
        """
        identified = []
        for fill in fills:
            values = OrderFilled.to_dict(fill)
            values["position_id"] = "P-1"
            identified.append(OrderFilled.from_dict(values))
        position = Position(instrument=env.instruments[0], fill=identified[0])
        for fill in identified[1:]:
            position.apply(fill)
        return position

    def test_the_position_is_short_by_the_fee_exactly_once(self, spot_harness):
        """The double count is only visible on a `Position`, so build one.

        The venue matched 0.010000 BTC and kept 0.000010 as its fee, so the
        wallet was credited 0.009990. With the fee netted off `last_qty` as well,
        the position came out at 0.009980 — a full fee short, compounding over
        every fill of every spot buy.
        """
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TS-1"),
        )
        env.drain(order)

        position = self._position(env, env.events_of(OrderFilled)[0])

        assert position.quantity == Quantity.from_str("0.009990")
        assert [adjustment.adjustment_type for adjustment in position.adjustments] == [
            PositionAdjustmentType.COMMISSION,
        ]
        assert position.adjustments[0].quantity_change == Decimal("-0.000010")

    def test_a_quote_currency_fee_leaves_the_position_whole(self, spot_harness):
        """A sell is commissioned in quote, so nothing is taken off the quantity."""
        env = spot_harness
        order = env.order_factory.limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("60000.00"),
        )
        env.accepted(order, "778900")
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(
                env,
                order,
                "TS-2",
                side="sell",
                fee="0.6",
                fee_currency="USDT",
                order_id="778900",
            ),
        )
        env.drain(order)

        fill = env.events_of(OrderFilled)[0]
        assert fill.last_qty == Quantity.from_str("0.010000")
        position = self._position(env, fill)
        assert position.quantity == Quantity.from_str("0.010000")
        assert position.adjustments == []

    def test_a_fully_matched_buy_closes_without_restating_the_order(self, spot_harness):
        """Gross fills add up to the order's own quantity, so it closes by itself.

        This is what makes the second netting unnecessary rather than merely
        redundant: while the fills were published net of the fee they could never
        reach the quantity the order was submitted with, the order came to rest
        at PARTIALLY_FILLED with nothing left to fill, and it stayed in
        ``cache.orders_open()`` for the rest of the session — which is why a
        second netting was applied to the order's quantity in the first place.
        """
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TS-3", amount="0.010000", fee="0.000010"),
        )
        env.drain(order)

        assert env.events_of(OrderUpdated) == []
        assert order.quantity == Quantity.from_str("0.010000")
        assert order.filled_qty == Quantity.from_str("0.010000")
        assert order.leaves_qty == Quantity.from_str("0.000000")
        assert order.status == OrderStatus.FILLED
        assert env.cache.orders_open() == []

    def test_a_partial_fill_leaves_the_unmatched_quantity_working(self, spot_harness):
        """What is left is what the venue is still working, fees notwithstanding."""
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TS-4", amount="0.006000", fee="0.000006"),
        )
        env.drain(order)

        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.quantity == Quantity.from_str("0.010000")
        assert order.filled_qty == Quantity.from_str("0.006000")
        assert order.leaves_qty == Quantity.from_str("0.004000")

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TS-5", amount="0.004000", fee="0.000004"),
        )
        env.drain(order)

        assert order.status == OrderStatus.FILLED
        assert order.leaves_qty == Quantity.from_str("0.000000")

    def test_two_fills_in_one_frame_close_the_order(self, spot_harness):
        """Gate.io delivers several trades in one frame; both must land whole."""
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_ws_message(
            GateioProductType.SPOT,
            {
                "time": 1785000000,
                "channel": "spot.usertrades",
                "event": "update",
                "result": [
                    _spot_fill_payload(env, order, "TS-6", amount="0.006000", fee="0.000006"),
                    _spot_fill_payload(env, order, "TS-7", amount="0.004000", fee="0.000004"),
                ],
            },
        )
        env.drain(order)

        assert order.quantity == Quantity.from_str("0.010000")
        assert order.filled_qty == Quantity.from_str("0.010000")
        assert order.status == OrderStatus.FILLED

    def test_the_cumulative_fee_is_taken_off_the_position_once_per_fill(self, spot_harness):
        """Two fills, two adjustments, and the position is short exactly both fees."""
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TS-6", amount="0.006000", fee="0.000006"),
        )
        env.drain(order)
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TS-7", amount="0.004000", fee="0.000004"),
        )
        env.drain(order)

        position = self._position(env, *env.events_of(OrderFilled))

        assert position.quantity == Quantity.from_str("0.009990")


def _spot_amend_payload(order: Any, amount: str, left: str, filled: str) -> dict[str, Any]:
    """The venue's own account of an order whose quantity was amended.

    Gate.io reports an amend as an ordinary ``spot.orders`` update carrying the
    new ``amount``, whether this session asked for it (``_modify_order`` accepts
    spot amends) or another session did. Nothing in the payload distinguishes the
    two, and nothing should: it is the venue restating what it is working.
    """
    return {
        "id": 778899,
        "id_string": "778899",
        "currency_pair": "BTC_USDT",
        "type": "limit",
        "side": "buy",
        "event": "update",
        "amount": amount,
        "left": left,
        "filled_amount": filled,
        "price": "60000.00",
        "time_in_force": "gtc",
        "create_time": 1785000000,
        "update_time": 1785000002,
        "text": f"t-{order.client_order_id.value}",
    }


class TestSpotQuantityAmends:
    """A venue amend is the venue's own size, and it is passed on untouched.

    What is still working at the venue is ``amended amount - matched amount``,
    and no arithmetic over fees may change that. The fee belongs to the position,
    not to the order: netting it off the order's quantity gets this wrong in both
    directions once the venue moves that quantity for its own reasons — amended
    down the order stays open after the venue has matched it in full, amended up
    it closes while the venue is still working the balance. The second is the
    more dangerous of the two: the strategy is told the order is done and stops
    tracking exposure that is still live.
    """

    def test_an_amend_down_still_closes_when_the_venue_has_matched_it_all(self, spot_harness):
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TA-1", amount="0.004000", fee="0.000004"),
        )
        env.drain(order)
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _spot_amend_payload(order, "0.008000", "0.004000", "0.004000"),
        )
        env.drain(order)
        assert order.quantity == Quantity.from_str("0.008000")

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TA-2", amount="0.004000", fee="0.000004"),
        )
        env.drain(order)

        assert order.filled_qty == Quantity.from_str("0.008000")
        assert order.leaves_qty == Quantity.from_str("0.000000")
        assert order.status == OrderStatus.FILLED
        assert env.cache.orders_open() == []

    def test_an_amend_up_leaves_the_venues_own_remainder_working(self, spot_harness):
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TA-1", amount="0.004000", fee="0.000004"),
        )
        env.drain(order)
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _spot_amend_payload(order, "0.020000", "0.016000", "0.004000"),
        )
        env.drain(order)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TA-2", amount="0.006000", fee="0.000006"),
        )
        env.drain(order)

        assert order.filled_qty == Quantity.from_str("0.010000")
        # The venue amended to 0.020 and has matched 0.010 of it.
        assert order.leaves_qty == Quantity.from_str("0.010000")
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order in env.cache.orders_open()

    def test_a_venue_update_restating_the_same_size_emits_nothing(self, spot_harness):
        """Every ``spot.orders`` frame carries the order's size, amend or not.

        The order already holds it, so there is nothing to restate and no
        ``OrderUpdated`` to publish — a strategy is told about quantity changes
        the venue made, not about every frame it sent.
        """
        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TA-1", amount="0.006000", fee="0.000006"),
        )
        env.drain(order)
        assert order.quantity == Quantity.from_str("0.010000")

        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _spot_amend_payload(order, "0.010000", "0.004000", "0.006000"),
        )
        env.drain(order)

        assert env.events_of(OrderUpdated) == []
        assert order.quantity == Quantity.from_str("0.010000")

    def test_a_converted_quote_quantity_stays_in_base_units(self, spot_harness):
        """Regression: ``is_quote_quantity`` must be stated, not inherited.

        ``generate_order_updated`` resolves ``is_quote_quantity=None`` from the
        cached order, which still reads ``True`` while the conversion's own
        ``OrderUpdated`` sits in the engine's queue. Inheriting it re-flags a
        base-denominated quantity as quote, undoing the conversion.
        """
        env = spot_harness
        order = env.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("600.000000"),
            quote_quantity=True,
        )
        env.accepted(order, "778899")
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "TQ-1", amount="0.010000", fee="0.000010"),
        )
        env.drain(order)

        assert [event.is_quote_quantity for event in env.events_of(OrderUpdated)] == [False]
        assert order.is_quote_quantity is False
        # The quantity is a bound while the order is in flight: one size
        # increment above the base Gate.io has credited, so no further fill can
        # be discarded as an overfill and none can close the order before the
        # venue says it is finished.
        assert order.quantity == Quantity.from_str("0.010001")
        assert order.filled_qty == Quantity.from_str("0.010000")
        assert order.status == OrderStatus.PARTIALLY_FILLED


# -- reconnect must reconcile, not just refresh balances ---------------------


class TestReconnectReconciliation:
    """Regression for EXEC-8 / seam-03: Gate.io replays nothing on a reconnect."""

    @staticmethod
    def _capture_mass_status(env) -> list[Any]:
        """Observe the endpoint the execution engine owns in production.

        ``ExecHarness`` cannot register this one itself: the release-gate
        harnesses put a real ``LiveExecutionEngine`` behind the same bus, and
        ``MessageBus.register`` refuses a second handler for an endpoint.
        """
        captured: list[Any] = []
        env.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_mass_status",
            handler=captured.append,
        )
        return captured

    @staticmethod
    def _wire_gap(env, fill_order_id: str = "900001") -> None:
        """The venue state a 3-lot fill during the outage leaves behind."""
        env.perp.responses["accounts"] = {
            "currency": "USDT",
            "total": "1000",
            "available": "1000",
            "unrealised_pnl": "0",
        }
        env.perp.responses["positions"] = []
        env.perp.responses["list_orders"] = lambda **kwargs: (
            [_futures_order_payload(left=-7)] if kwargs.get("status") == "open" else []
        )
        env.perp.responses["my_trades"] = lambda **kwargs: (
            [_futures_fill_payload("T-GAP", -3, order_id=fill_order_id)]
            if kwargs.get("offset", 0) == 0
            else []
        )

    def test_reconnect_requeries_orders_and_fills(self, perp_harness):
        env = perp_harness
        mass_statuses = self._capture_mass_status(env)
        self._wire_gap(env)

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert env.perp.called("list_orders")
        assert env.perp.called("my_trades")

        assert len(mass_statuses) == 1
        status = mass_statuses[0]
        assert VenueOrderId("900001") in status.order_reports
        fills = status.fill_reports[VenueOrderId("900001")]
        assert [report.trade_id for report in fills] == [TradeId("T-GAP")]

    def test_the_gap_fill_is_handed_over_with_the_order_it_belongs_to(self, perp_harness):
        """Regression: an order report must never reach the engine on its own.

        ``ExecEngine.reconcile_execution_report`` reconciles one report against
        the local order in isolation. An order status report arriving alone
        states a filled quantity with no trade to account for it, so the engine
        closes the gap with an inferred fill under a synthetic trade id; the
        venue's own trade then arrives with the real id and is booked a second
        time, or is dropped as predating the inferred one and leaves the venue
        trade id unrecorded for the next replay to fill again. The grouped
        hand-over is what lets the engine apply the venue's own fill instead of
        inventing one, and it has to go first, because it is the pass that
        restates the order's quantity before any fill lands on it.
        """
        env = perp_harness
        order_of_hand_over: list[str] = []
        mass_statuses: list[Any] = []
        env.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_mass_status",
            handler=lambda report: (
                mass_statuses.append(report),
                order_of_hand_over.append("grouped"),
            ),
        )
        env.msgbus.deregister(
            endpoint="ExecEngine.reconcile_execution_report",
            handler=env.reports.append,
        )
        env.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_report",
            handler=lambda report: (
                env.reports.append(report),
                order_of_hand_over.append("single"),
            ),
        )
        self._wire_gap(env)

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert order_of_hand_over[0] == "grouped"
        assert len(mass_statuses) == 1
        report = mass_statuses[0].order_reports[VenueOrderId("900001")]
        assert report.filled_qty == Quantity.from_int(3)
        assert [
            fill.trade_id for fill in mass_statuses[0].fill_reports[VenueOrderId("900001")]
        ] == [TradeId("T-GAP")]

    def test_a_trade_the_grouped_pass_did_not_book_is_reported_on_its_own(self, perp_harness):
        """Regression: grouping makes trade delivery conditional on the order's status.

        ``LiveExecutionEngine._reconcile_order_report`` asks
        ``_handle_order_status_transitions`` about the order report before it
        walks the trades, and that returns "reconciled" for an ACCEPTED, REJECTED
        or TRIGGERED report — and for a CANCELED or EXPIRED one when the local
        order already holds that status — without ever reaching the
        ``for trade in trades`` loop. The venue trade grouped under such a report
        is discarded, and discarded without a log line. The reconnect reaches
        that pairing on its own: it queries the order listing and the trade
        listing as two sequential REST sweeps, so a match landing between them
        appears in the trades while the order snapshot still reads fully open.

        The trade belongs to an order the listing *does* cover, so it is not an
        orphan and the orphan fallback deliberately does not carry it. What
        catches it is checking the outcome instead of predicting it: whatever is
        not on its order after the grouped hand-over goes through the
        single-report path, which has no status gate.

        The grouped pass runs first and synchronously, so by the time the sweep
        looks the engine has already created the external order and indexed its
        venue order id — which is the only thing that makes a lone `FillReport`
        resolvable at all (`_reconcile_fill_report_single`). The index is seeded
        here because this harness carries no execution engine to do it.
        """
        env = perp_harness
        self._capture_mass_status(env)
        self._wire_gap(env)
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert [report.trade_id for report in env.reports] == [TradeId("T-GAP")]
        assert [report.venue_order_id for report in env.reports] == [VenueOrderId("900001")]
        assert not env.perp.called("get_order")

    def test_the_order_is_restated_before_its_recovered_trade_is_re_offered(self, perp_harness):
        """Regression: a lone ``FillReport`` cannot restate what it is applied to.

        The venue reduced this order to 6 lots during the outage and matched all
        6. The order listing, read first, carries the reduced size and has not
        caught up with the match; the trade listing, read second, has. The
        grouped pass therefore short-circuits on the ACCEPTED snapshot (and the
        engine's own ``_deduplicate_mass_status_orders`` drops the report and its
        trades outright, because its exact-match test compares status, filled
        quantity, instrument and side and never the quantity), so the sweep is
        what delivers the trade.

        Delivered on its own, the trade is applied against the 10 lots this
        client still believes are working, leaving 4 lots that the venue will
        never fill. ``Order.apply(OrderUpdated)`` triggers no state transition
        (installed model/orders/base.pyx), so no later reconnect and no restart
        can ever close that order — every one of them restates the quantity
        *after* the fill. The venue's own snapshot therefore has to go over
        first, and it has to go as a report: ``generate_order_updated`` publishes
        to ``ExecEngine.process``, which ``LiveExecutionEngine`` enqueues, while
        the report endpoints run inline.
        """
        env = perp_harness
        self._capture_mass_status(env)
        env.perp.responses["accounts"] = {
            "currency": "USDT",
            "total": "1000",
            "available": "1000",
            "unrealised_pnl": "0",
        }
        env.perp.responses["positions"] = []
        env.perp.responses["list_orders"] = lambda **kwargs: (
            [_futures_order_payload(size=-6, left=-6)] if kwargs.get("status") == "open" else []
        )
        env.perp.responses["my_trades"] = lambda **kwargs: (
            [_futures_fill_payload("T-GAP", -6)] if kwargs.get("offset", 0) == 0 else []
        )
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert [type(report).__name__ for report in env.reports] == [
            "OrderStatusReport",
            "FillReport",
        ]
        assert env.reports[0].quantity == Quantity.from_int(6)
        assert env.reports[1].trade_id == TradeId("T-GAP")

    def test_a_recovered_trade_is_regrouped_when_its_order_cannot_take_it(self, spot_harness):
        """Regression: a quote-denominated order cannot hold a base-denominated fill.

        Gate.io's own spot market buy is submitted as a quote-currency cash
        amount and filled in base units, and the venue publishes no
        base-denominated quantity for one until it has filled — so the order
        listing carries no statement of it at all while it is still resting, and
        the sweep has nothing to restate from. Re-offering the trade on its own
        would leave 590 USDT 'working' against an order the venue has finished,
        for ever. The order is re-read instead and the two are handed over
        together, which is the one route that restates before it applies.
        """
        env = spot_harness
        mass_statuses = self._capture_mass_status(env)
        order = env.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("590.00"),
            quote_quantity=True,
        )
        env.accepted(order, "778899")
        # The order has to be open in the cache: the spot fill sweep asks the
        # venue only about the pairs this client is actually working.
        env.drain(order)
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
        text = f"t-{order.client_order_id.value}"
        resting = _spot_order_payload(
            type="market",
            side="buy",
            amount="590.00",
            left="590.00",
            filled_amount="0",
            price="0",
            status="open",
            text=text,
        )
        env.spot.responses["accounts"] = [{"currency": "USDT", "available": "1000", "locked": "0"}]
        env.spot.responses["open_orders"] = lambda **kwargs: (
            [{"currency_pair": "BTC_USDT", "orders": [resting]}]
            if kwargs.get("page", 1) == 1
            else []
        )
        env.spot.responses["list_orders"] = lambda *args, **kwargs: []
        env.spot.responses["my_trades"] = lambda **kwargs: (
            [
                _spot_fill_payload(
                    env,
                    order,
                    "TS-GAP",
                    create_time_ms=str(RECENT_SECS * 1000),
                ),
            ]
            if kwargs.get("page", 1) == 1
            else []
        )
        env.spot.responses["get_order"] = _spot_order_payload(
            type="market",
            side="buy",
            amount="590.00",
            left="0",
            filled_amount="0.010000",
            price="0",
            status="closed",
            finish_as="filled",
            text=text,
        )

        env.run(env.client._reconcile_after_reconnect(GateioProductType.SPOT))

        assert env.spot.called("get_order")
        assert len(mass_statuses) == 2
        regrouped = mass_statuses[1]
        assert regrouped.order_reports[VenueOrderId("778899")].quantity == Quantity.from_str(
            "0.010000",
        )
        assert [fill.trade_id for fill in regrouped.fill_reports[VenueOrderId("778899")]] == [
            TradeId("TS-GAP")
        ]
        # The last-resort single report follows only because this harness has no
        # execution engine behind the bus to book the grouped hand-over, so the
        # sweep's outcome check still reads "not booked". What that route leaves
        # behind with a real engine is settled by the release-gate scenario
        # spot_market_buy_recovered_after_a_reconnect_is_not_left_open.
        assert [report.trade_id for report in env.reports] == [TradeId("TS-GAP")]

    def test_a_trade_is_still_booked_when_the_venue_will_not_state_its_order(self, spot_harness):
        """An execution that happened is worse lost than recorded against a stale order.

        The re-read is the preferred route because it restates before it applies,
        but it depends on the venue answering. When it does not, the trade still
        belongs on the order the cache already indexes: booking it leaves the
        order possibly open, not booking it leaves the position wrong.
        """
        env = spot_harness
        self._capture_mass_status(env)
        order = env.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("590.00"),
            quote_quantity=True,
        )
        env.accepted(order, "778899")
        env.drain(order)
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
        env.spot.responses["accounts"] = [{"currency": "USDT", "available": "1000", "locked": "0"}]
        env.spot.responses["my_trades"] = lambda **kwargs: (
            [
                _spot_fill_payload(
                    env,
                    order,
                    "TS-GAP",
                    create_time_ms=str(RECENT_SECS * 1000),
                ),
            ]
            if kwargs.get("page", 1) == 1
            else []
        )
        env.spot.responses["get_order"] = GateioClientError(400, "INVALID_ORDER_ID", "unknown")

        env.run(env.client._reconcile_after_reconnect(GateioProductType.SPOT))

        assert [report.trade_id for report in env.reports] == [TradeId("TS-GAP")]

    def test_the_restatement_set_matches_the_platforms_state_table(self):
        """``RESTATABLE_ORDER_STATUSES`` is a claim about the FSM; hold it to it.

        Handing an ACCEPTED order status report over on its own is safe only
        where the engine's ACCEPTED branch generates nothing the FSM would
        refuse. It generates an ``OrderAccepted`` for every order not already
        accepted, so the set is exactly: ACCEPTED, where it generates none, plus
        every status from which ``Order.apply(OrderAccepted)`` is a legal
        trigger. That is read off the platform here rather than off this
        client's reading of it, and the statuses it excludes are exercised too,
        so the set cannot quietly become "everything".
        """
        from nautilus_trader.core.fsm import InvalidStateTrigger
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.model.events import OrderAccepted

        from nautilus_gateio.execution import RESTATABLE_ORDER_STATUSES

        def accepts_an_accepted_event(status: OrderStatus) -> bool:
            env = ExecHarness(products=(GateioProductType.PERP,))
            try:
                order = env.order_factory.limit(
                    PERP_BTC_USDT,
                    OrderSide.SELL,
                    Quantity.from_int(10),
                    Price.from_str("59000.0"),
                )
                env.cache.add_order(order)
                text = f"t-{order.client_order_id.value}"
                env.client._register_text(order.client_order_id, text)
                if status is not OrderStatus.INITIALIZED:
                    env.client.generate_order_submitted(
                        strategy_id=order.strategy_id,
                        instrument_id=order.instrument_id,
                        client_order_id=order.client_order_id,
                        ts_event=env.clock.timestamp_ns(),
                    )
                if status in (
                    OrderStatus.ACCEPTED,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.CANCELED,
                ):
                    env.client.generate_order_accepted(
                        strategy_id=order.strategy_id,
                        instrument_id=order.instrument_id,
                        client_order_id=order.client_order_id,
                        venue_order_id=VenueOrderId("900001"),
                        ts_event=env.clock.timestamp_ns(),
                    )
                env.drain(order)
                if status is OrderStatus.PARTIALLY_FILLED:
                    env.client._handle_fill_payload(
                        GateioProductType.PERP,
                        _futures_fill_payload("T-PART", -4, text=text),
                    )
                    env.drain(order)
                if status is OrderStatus.CANCELED:
                    env.client._handle_order_payload(
                        GateioProductType.PERP,
                        _futures_order_payload(
                            status="finished",
                            finish_as="cancelled",
                            text=text,
                        ),
                    )
                    env.drain(order)
                assert order.status == status, f"could not reach {status}"

                accepted = OrderAccepted(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=VenueOrderId("900001"),
                    account_id=env.client.account_id,
                    event_id=UUID4(),
                    ts_event=env.clock.timestamp_ns(),
                    ts_init=env.clock.timestamp_ns(),
                    reconciliation=True,
                )
                try:
                    order.apply(accepted)
                except InvalidStateTrigger:
                    return False
                return True
            finally:
                env.close()

        probed = (
            OrderStatus.INITIALIZED,
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCELED,
        )
        safe = {
            status
            for status in probed
            # ACCEPTED is safe because the engine generates nothing there, not
            # because the FSM would take the event.
            if status is OrderStatus.ACCEPTED or accepts_an_accepted_event(status)
        }

        assert safe == set(RESTATABLE_ORDER_STATUSES)
        assert OrderStatus.PARTIALLY_FILLED not in safe
        assert OrderStatus.CANCELED not in safe

    @staticmethod
    def _partly_filled_perp(env) -> Any:
        """A local order of 10 lots with 4 already booked against it."""
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        text = f"t-{order.client_order_id.value}"
        env.client._register_text(order.client_order_id, text)
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-BOOKED", -4, text=text),
        )
        env.drain(order)
        assert order.filled_qty == Quantity.from_int(4)
        return order

    @staticmethod
    def _listing_report(env, order, *, status, quantity, filled) -> Any:
        """The venue's own statement of ``order``, as the order listing gives it."""
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.reports import OrderStatusReport

        ts_now = env.clock.timestamp_ns()
        return OrderStatusReport(
            account_id=env.client.account_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            order_side=order.side,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            order_status=status,
            price=order.price,
            quantity=quantity,
            filled_qty=filled,
            report_id=UUID4(),
            ts_accepted=ts_now,
            ts_last=ts_now,
            ts_init=ts_now,
        )

    def test_a_partly_filled_order_the_venue_reduced_is_restated(self, perp_harness):
        """The restatement guard is about inferred fills, so it must follow them.

        The old guard admitted an ACCEPTED report only, which never fires for an
        order that already carries a fill. That leaves the case the platform's own
        duplicate filter creates: ``_deduplicate_mass_status_orders`` (installed
        live/execution_engine.py:2028-2120) compares status, filled quantity,
        instrument and side and **not** quantity, so a snapshot that has reduced
        the order while still reporting the filled quantity the cache holds is
        deleted as an exact duplicate, along with the trades grouped under it. The
        sweep is then the only thing left, and without a restatement it applies the
        surviving trade to a quantity the venue has already cut — leaving the order
        open for ever against a remainder nobody will fill.

        Nothing can be inferred here: ``_handle_fill_quantity_mismatch`` (:3164)
        generates a fill only on ``report.filled_qty > order.filled_qty`` with the
        order still open, and these two are equal.
        """
        env = perp_harness
        order = self._partly_filled_perp(env)
        env.reports.clear()

        env.client._restate_from_listing(
            self._listing_report(
                env,
                order,
                status=OrderStatus.PARTIALLY_FILLED,
                quantity=Quantity.from_int(7),
                filled=Quantity.from_int(4),
            ),
        )

        assert [report.quantity for report in env.reports] == [Quantity.from_int(7)]

    def test_a_report_claiming_more_fills_than_the_order_holds_is_not_restated(
        self,
        perp_harness,
    ):
        """This is the one shape that reaches the inferred-fill branch.

        ``report.filled_qty > order.filled_qty`` on an open order is exactly where
        ``_handle_fill_quantity_mismatch`` mints a fill with a synthetic trade id —
        an execution the venue never reported, which the real trade this sweep is
        about to re-offer would then duplicate.
        """
        env = perp_harness
        order = self._partly_filled_perp(env)
        env.reports.clear()

        env.client._restate_from_listing(
            self._listing_report(
                env,
                order,
                status=OrderStatus.PARTIALLY_FILLED,
                quantity=Quantity.from_int(7),
                filled=Quantity.from_int(6),
            ),
        )

        assert env.reports == []

    @pytest.mark.parametrize(
        "status",
        [OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED, OrderStatus.TRIGGERED],
    )
    def test_a_terminal_report_is_left_to_the_grouped_hand_over(self, perp_harness, status):
        """These cannot infer a fill either, and are still not sent alone.

        Their branches in ``_handle_order_status_transitions`` do not restate the
        order, they *act* on it — reject, trigger, cancel, expire. Sent on their
        own, ahead of the trade the sweep is about to re-offer, they close the
        order before its execution is booked, which is the reverse of the order
        ``_reconcile_execution_mass_status`` uses: there the trades grouped under
        the report are reconciled first.
        """
        env = perp_harness
        order = self._partly_filled_perp(env)
        env.reports.clear()

        env.client._restate_from_listing(
            self._listing_report(
                env,
                order,
                status=status,
                quantity=Quantity.from_int(7),
                filled=Quantity.from_int(4),
            ),
        )

        assert env.reports == []

    def test_every_unbooked_trade_of_one_order_goes_over_in_one_hand_over(self, perp_harness):
        """Regression: one trade at a time makes the engine invent the rest.

        Gate.io matches an aggressive order across price levels, so several trades
        against one order id is the ordinary answer. ``_reconcile_execution_mass_status``
        applies every trade grouped under an order report *before*
        ``_handle_fill_quantity_mismatch`` compares the totals; hand it a subset
        and the comparison disagrees by the trades left out, so the engine closes
        the difference with an inferred fill carrying a synthetic id and no
        commission. The real trade that was left out is then rejected on top of it
        as a duplicate or an overfill: a venue trade id and the fee it paid, both
        gone.
        """
        env = perp_harness
        mass_statuses = self._capture_mass_status(env)
        self._wire_gap(env, fill_order_id="900777")
        env.perp.responses["my_trades"] = lambda **kwargs: (
            [
                _futures_fill_payload("T-GAP-1", -2, order_id="900777"),
                _futures_fill_payload("T-GAP-2", -1, order_id="900777"),
            ]
            if kwargs.get("offset", 0) == 0
            else []
        )
        env.perp.responses["get_order"] = _futures_order_payload(
            id=900777,
            id_string="900777",
            left=-7,
            status="open",
        )

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))
        env.run(_drain_tasks(env))

        # One re-read and one hand-over for the order, not one per trade.
        assert [call.args[0] for call in env.perp.calls_named("get_order")] == ["900777"]
        assert len(mass_statuses) == 2
        recovered = mass_statuses[1]
        assert [fill.trade_id for fill in recovered.fill_reports[VenueOrderId("900777")]] == [
            TradeId("T-GAP-1"),
            TradeId("T-GAP-2"),
        ]

    def test_an_order_the_venue_reports_under_another_id_is_rebased_first(self, perp_harness):
        """A conditional that fired during the outage speaks under a new id.

        Gate.io creates a brand-new order when a trigger fires, so the cached
        order still holds the armed id while the venue's listing — and every trade
        it produced — names the fired one. Nothing in reconciliation bridges that:
        ``create_order_filled_event`` (live/reconciliation.py:381) stamps the fill
        with ``report.venue_order_id``, ``Order.apply`` refuses an ``OrderFilled``
        whose venue order id differs from the order's, and the engine logs the
        ``ValueError`` away. ``_should_update`` (live/execution_engine.py:3307)
        compares quantity, price and trigger price and never the venue order id,
        so the rebase has to come from this client before the reports go over.
        """
        env = perp_harness
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
        env.client._register_trigger_link(
            GateioProductType.PERP,
            "900001",
            order.client_order_id,
        )
        self._wire_gap(env, fill_order_id="900777")
        env.perp.responses["list_orders"] = lambda **kwargs: (
            [
                _futures_order_payload(
                    id=900777,
                    id_string="900777",
                    left=-7,
                    text=f"t-{order.client_order_id.value}",
                ),
            ]
            if kwargs.get("status") == "open"
            else []
        )

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert env.cache.venue_order_id(order.client_order_id) == VenueOrderId("900777")
        rebases = [
            event
            for event in env.events_of(OrderUpdated)
            if event.venue_order_id == VenueOrderId("900777")
        ]
        assert len(rebases) == 1
        assert env.client.trigger_links[order.client_order_id].fired_id == "900777"

    def test_a_trade_already_on_its_order_is_not_reported_a_second_time(self, perp_harness):
        """The sweep is a repair, not a second delivery.

        A trade the grouped pass booked — or one the private stream applied
        before the socket ever dropped — is already on the order, so re-offering
        it would only lean on the engine's de-duplication. It is skipped, which
        is what keeps a reconnect over unchanged venue answers a no-op.
        """
        env = perp_harness
        self._capture_mass_status(env)
        self._wire_gap(env)

        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("60000.0"),
        )
        env.accepted(order, "900001")
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-GAP", -3, order_id="900001"),
        )
        env.drain(order)
        assert TradeId("T-GAP") in order.trade_ids

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert env.reports == []

    def test_a_fill_with_no_order_of_its_own_is_requeried_and_handed_over(self, perp_harness):
        """A trade the order listing does not cover must not be dropped silently.

        The grouped hand-over walks the order reports, so a fill whose venue
        order id is not among them has nobody to be grouped with — an order that
        finished outside the window its trade falls inside, or one the venue
        answered for on only one of the two endpoints.

        Reporting it on its own does not work: nothing has ever put that venue
        order id in the cache, and `_reconcile_fill_report_single` resolves the
        order through that index alone, so the engine logs "deferring
        reconciliation" and drops it with no retry. The order it belongs to is
        re-read from the venue instead, and the two are handed over together.
        """
        env = perp_harness
        mass_statuses = self._capture_mass_status(env)
        self._wire_gap(env, fill_order_id="900777")
        env.perp.responses["get_order"] = _futures_order_payload(
            id=900777,
            id_string="900777",
            left=-7,
            status="open",
        )

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))
        env.run(_drain_tasks(env))

        assert env.reports == []
        assert [call.args[0] for call in env.perp.calls_named("get_order")] == ["900777"]
        assert len(mass_statuses) == 2
        recovered = mass_statuses[1]
        assert VenueOrderId("900777") in recovered.order_reports
        assert [fill.trade_id for fill in recovered.fill_reports[VenueOrderId("900777")]] == [
            TradeId("T-GAP")
        ]

    def test_reconnect_still_refreshes_the_account_state(self, perp_harness):
        env = perp_harness
        env.perp.responses["accounts"] = {
            "currency": "USDT",
            "total": "1000",
            "available": "1000",
            "unrealised_pnl": "0",
        }
        env.perp.responses["positions"] = []

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert env.account_states
        assert env.client._balances["USDT"][0] == Decimal("1000")

    def test_reconnect_uses_the_last_stream_event_as_the_window_anchor(self, perp_harness):
        env = perp_harness
        env.client._handle_ws_message(
            GateioProductType.PERP,
            {
                "time_ms": 1785000000000,
                "channel": "futures.orders",
                "event": "update",
                "result": [],
            },
        )
        assert env.client._last_stream_event_ns[GateioProductType.PERP] == 1785000000000000000

    def test_reconnect_schedules_reconciliation_not_only_balances(self, perp_harness):
        env = perp_harness
        env.perp.responses["accounts"] = {
            "currency": "USDT",
            "total": "1",
            "available": "1",
            "unrealised_pnl": "0",
        }
        env.perp.responses["positions"] = []

        env.client._handle_ws_reconnect(GateioProductType.PERP)
        env.run(asyncio.sleep(0))
        env.run(asyncio.sleep(0))

        pending = [task for task in asyncio.all_tasks(env.loop) if not task.done()]
        env.run(asyncio.gather(*pending)) if pending else None

        assert env.perp.called("list_orders")


# -- EXEC-1: the fill-before-order race --------------------------------------


class TestFillBeforeOrderUpdate:
    """Regression for EXEC-1: a fill can arrive before the order that explains it.

    Gate.io publishes ``*.orders`` and ``*.usertrades`` on independent channels
    with no ordering between them, so the first message mentioning a fired
    conditional order is frequently its fill. Until this was fixed the fill was
    emitted against the armed venue order id, ``Order.apply`` refused it, and the
    execution engine swallowed the resulting ``ValueError`` into a log line — the
    fill was simply gone, and the position silently disagreed with the venue.

    Every test here drives the client through ``ExecHarness.drain``, which applies
    each generated event to a real ``Order``. A sequence the FSM rejects therefore
    fails the test rather than being quietly accepted.
    """

    def test_fill_before_any_order_message_is_not_lost(self, perp_harness):
        """The whole point: no order payload has been seen, only the fill."""
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        text = f"t-{order.client_order_id.value}"

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-1", -4, text=text),
        )
        events = env.drain(order)

        assert [type(event).__name__ for event in events] == [
            "OrderUpdated",
            "OrderTriggered",
            "OrderFilled",
        ]
        assert order.venue_order_id == VenueOrderId("900001")
        assert order.filled_qty == Quantity.from_int(4)
        assert order.status == OrderStatus.PARTIALLY_FILLED

    def test_the_rebase_precedes_the_fill(self, perp_harness):
        """Ordering is the fix. `OrderUpdated` must carry the new identity first."""
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-1", -4, text=f"t-{order.client_order_id.value}"),
        )
        events = env.drain(order)

        updated = next(event for event in events if isinstance(event, OrderUpdated))
        filled = next(event for event in events if isinstance(event, OrderFilled))
        assert updated.venue_order_id == VenueOrderId("900001")
        assert filled.venue_order_id == VenueOrderId("900001")
        assert events.index(updated) < events.index(filled)

    def test_the_armed_identity_is_kept_after_the_rebase(self, perp_harness):
        """Both identities survive: the armed id still addresses the price order."""
        env = perp_harness
        order = _arm_futures_stop_limit(env, "AUTO-77")
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-1", -4, text=f"t-{order.client_order_id.value}"),
        )
        env.drain(order)

        link = env.client._trigger_links[order.client_order_id]
        assert link.armed_id == "AUTO-77"
        assert link.fired_id == "900001"

    def test_several_fills_before_the_order_message(self, perp_harness):
        """The rebase happens once; every subsequent fill applies normally."""
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        text = f"t-{order.client_order_id.value}"

        for index, size in enumerate((-3, -3, -4), start=1):
            env.client._handle_fill_payload(
                GateioProductType.PERP,
                _futures_fill_payload(f"T-{index}", size, text=text),
            )
        events = env.drain(order)

        assert len([e for e in events if isinstance(e, OrderUpdated)]) == 1
        assert len([e for e in events if isinstance(e, OrderFilled)]) == 3
        assert order.filled_qty == Quantity.from_int(10)
        assert order.status == OrderStatus.FILLED

    def test_the_order_message_after_the_fill_is_idempotent(self, perp_harness):
        """The late order payload must not rebase or re-trigger a second time."""
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        text = f"t-{order.client_order_id.value}"
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-1", -4, text=text),
        )
        env.drain(order)

        env.client._handle_order_payload(
            GateioProductType.PERP,
            _futures_order_payload(left=-6, status="open", text=text),
        )
        events = env.drain(order)

        assert not [e for e in events if isinstance(e, OrderTriggered)]
        assert order.venue_order_id == VenueOrderId("900001")
        assert order.filled_qty == Quantity.from_int(4)

    def test_a_duplicate_fill_after_the_rebase_is_ignored(self, perp_harness):
        """Replay after a recovery must not double-count."""
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        text = f"t-{order.client_order_id.value}"
        payload = _futures_fill_payload("T-1", -4, text=text)

        env.client._handle_fill_payload(GateioProductType.PERP, payload)
        env.drain(order)
        env.client._handle_fill_payload(GateioProductType.PERP, dict(payload))
        replayed = env.drain(order)

        assert replayed == []
        assert order.filled_qty == Quantity.from_int(4)

    def test_a_mismatched_identity_without_a_trigger_link_is_rebased(self, perp_harness):
        """An identity this client does not model is still the venue's own.

        The fill names one of this client's orders through the `text` alias it
        registered for it, and carries a venue order id that order does not hold:
        the venue has replaced the object it is working under. Handing that to
        reconciliation cannot help — `create_order_filled_event` stamps the fill
        with `report.venue_order_id`, so `Order.apply` refuses it there for
        exactly the same reason and `_reconcile_fill_report` logs it away.
        `OrderUpdated` is the one event allowed to move a venue order id, so the
        identity is rebased and the fill applies.
        """
        env = perp_harness
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "555000")
        text = f"t-{order.client_order_id.value}"
        env.client._register_text(order.client_order_id, text)

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-9", -4, text=text),
        )
        events = env.drain(order)

        assert [type(event).__name__ for event in events] == ["OrderUpdated", "OrderFilled"]
        assert events[0].venue_order_id == VenueOrderId("900001")
        assert env.reports == []
        assert order.venue_order_id == VenueOrderId("900001")
        assert order.filled_qty == Quantity.from_int(4)

    def test_the_rebased_fill_is_not_applied_twice(self, perp_harness):
        """A replay of it must not rebase again or fill again."""
        env = perp_harness
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "555000")
        text = f"t-{order.client_order_id.value}"
        env.client._register_text(order.client_order_id, text)
        payload = _futures_fill_payload("T-9", -4, text=text)

        env.client._handle_fill_payload(GateioProductType.PERP, payload)
        env.drain(order)
        env.client._handle_fill_payload(GateioProductType.PERP, dict(payload))
        replayed = env.drain(order)

        assert replayed == []
        assert env.reports == []
        assert order.filled_qty == Quantity.from_int(4)

    def test_an_unknown_order_id_is_scheduled_for_resolution(self, perp_harness):
        """A fill with no text at all cannot name its order; resolve it from REST."""
        env = perp_harness
        _arm_futures_stop_limit(env)

        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-1", -4),
        )

        assert "900001" in env.client._trigger_resolution_attempts
        pending = [task for task in asyncio.all_tasks(env.loop) if not task.done()]
        assert pending, "a resolution task must have been scheduled"
        for task in pending:
            task.cancel()
        # Let the cancellation reach the coroutine: a task cancelled but never
        # awaited leaves the loop holding a coroutine that never started.
        env.run(asyncio.gather(*pending, return_exceptions=True))

    def test_reconnect_between_the_fill_and_the_order_message(self, perp_harness):
        """A reconnect must not undo the identity the fill established."""
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        text = f"t-{order.client_order_id.value}"
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-1", -4, text=text),
        )
        env.drain(order)

        env.perp.responses["list_orders"] = lambda **kwargs: []
        env.perp.responses["my_trades"] = lambda **kwargs: []
        env.perp.responses["accounts"] = {
            "currency": "USDT",
            "total": "1000",
            "available": "1000",
            "unrealised_pnl": "0",
        }
        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))
        env.drain(order)

        assert order.venue_order_id == VenueOrderId("900001")
        assert order.filled_qty == Quantity.from_int(4)
        link = env.client._trigger_links[order.client_order_id]
        assert (link.armed_id, link.fired_id) == ("AUTO-77", "900001")

    def test_restart_between_the_fill_and_the_order_message(self, perp_harness):
        """After a restart the link is rebuilt from the venue, not from memory.

        A restart loses `_trigger_links` and `_applied_trade_ids`. The order is
        restored from the Nautilus cache already rebased onto the fired id, so a
        replayed fill must be recognised as belonging to it and must not be
        applied twice.
        """
        env = perp_harness
        order = _arm_futures_stop_limit(env)
        text = f"t-{order.client_order_id.value}"
        payload = _futures_fill_payload("T-1", -4, text=text)
        env.client._handle_fill_payload(GateioProductType.PERP, payload)
        env.drain(order)

        # Restart: in-memory state is gone, the cached order survives.
        env.client._trigger_links.clear()
        env.client._applied_trade_ids.clear()
        env.client._trigger_resolution_attempts.clear()

        env.client._handle_fill_payload(GateioProductType.PERP, dict(payload))
        replayed = env.drain(order)

        assert replayed == []
        assert order.filled_qty == Quantity.from_int(4)
        assert order.venue_order_id == VenueOrderId("900001")


# -- the post-only termination must respect the platform's state table --------


class TestPostOnlyTermination:
    """``finish_as=poc`` is a rejection only where the FSM can accept one.

    Regression: the branch used to call ``generate_order_rejected``
    unconditionally, while the three branches beside it all guarded on the
    order's state. The installed state table has no
    ``PARTIALLY_FILLED -> REJECTED`` entry (model/orders/base.pyx), so the
    unconditional call raises ``InvalidStateTrigger`` inside the execution
    engine for an order this client has already booked a fill against — and the
    order then stays open locally while Gate.io has finished it.

    The payload path is not hypothetical: ``_handle_order_payload`` serves the
    REST responses to submit, cancel and amend as well as the order stream, and
    Gate.io does not order ``spot.orders`` against ``spot.usertrades``.
    """

    def _terminate_as_post_only(self, env: ExecHarness, order: Any) -> list[Any]:
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _spot_order_payload(
                status="closed",
                finish_as="poc",
                left="0.005000",
                filled_amount="0.005000",
                text=f"t-{order.client_order_id.value}",
            ),
        )
        return env.drain(order)

    def test_an_untouched_order_is_rejected_with_the_post_only_flag(self, spot_harness):
        from nautilus_trader.model.events import OrderRejected

        env = spot_harness
        order = _accepted_spot_buy(env)

        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _spot_order_payload(
                status="closed",
                finish_as="poc",
                left="0.010000",
                filled_amount="0",
                text=f"t-{order.client_order_id.value}",
            ),
        )
        events = env.drain(order)

        assert [type(event).__name__ for event in events] == ["OrderRejected"]
        assert isinstance(events[0], OrderRejected)
        assert events[0].due_post_only is True
        assert order.status == OrderStatus.REJECTED

    def test_a_partly_filled_order_is_canceled_not_rejected(self, spot_harness):
        """The order is finished either way; only one transition is legal."""
        env = spot_harness
        order = _accepted_spot_buy(env)
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _spot_fill_payload(env, order, "T-1", amount="0.005000"),
        )
        env.drain(order)
        assert order.status == OrderStatus.PARTIALLY_FILLED

        events = self._terminate_as_post_only(env, order)

        # Applying through the real FSM is the assertion: an OrderRejected here
        # would raise InvalidStateTrigger out of `env.drain`.
        assert [type(event).__name__ for event in events] == ["OrderCanceled"]
        assert isinstance(events[0], OrderCanceled)
        assert order.status == OrderStatus.CANCELED

    def test_an_already_closed_order_produces_nothing(self, spot_harness):
        """A replayed payload for a finished order is not a second termination."""
        env = spot_harness
        order = _accepted_spot_buy(env)
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _spot_order_payload(
                status="cancelled",
                finish_as="cancelled",
                text=f"t-{order.client_order_id.value}",
            ),
        )
        env.drain(order)
        assert order.status == OrderStatus.CANCELED
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")

        assert self._terminate_as_post_only(env, order) == []
        assert order.status == OrderStatus.CANCELED


class TestStartupRecoverySweep:
    """Regression (REC-01): the restart route books recovered trades itself.

    ``LiveExecutionEngine.reconcile_execution_state`` awaits
    ``generate_mass_status`` and reconciles the result after it returns, so the
    inside of that call is the last moment at which nothing has been reconciled
    yet. A trade booked there reaches the cache before the engine's duplicate
    filter compares snapshots against it and before any position report is
    reconciled — the ordering that round six's withdrawn repair (a sweep staged
    off the engine's publication of the reconciled mass status) got wrong, and
    that docs/roadmap.md Stage 0 records so it is not tried again.
    """

    @staticmethod
    def _wire_startup_gap(env: ExecHarness, text: str) -> None:
        """The venue after a 3-lot match the order listing has not caught up with."""
        env.perp.responses["positions"] = []
        env.perp.responses["position"] = []
        env.perp.responses["list_orders"] = lambda **kwargs: (
            [_futures_order_payload(text=text)] if kwargs.get("status") == "open" else []
        )
        env.perp.responses["my_trades"] = lambda **kwargs: (
            [_futures_fill_payload("T-GAP", -3, text=text)] if kwargs.get("offset", 0) == 0 else []
        )

    def test_the_gap_trade_is_offered_before_the_mass_status_is_returned(self, perp_harness):
        env = perp_harness
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        self._wire_startup_gap(env, f"t-{order.client_order_id.value}")

        mass_status = env.run(env.client.generate_mass_status(None))

        # The venue trade went to the engine DURING the call, over the
        # single-report channel that has no status gate. On the old tree
        # nothing was sent here: the engine's grouped pass then deleted the
        # ACCEPTED snapshot as an exact duplicate of the cached order and the
        # trade grouped under it in the same breath, and the position gap was
        # closed with an inferred fill instead of the venue's execution.
        assert [report.trade_id for report in env.reports] == [TradeId("T-GAP")]
        assert [report.venue_order_id for report in env.reports] == [VenueOrderId("900001")]

        # The mass status still carries the venue's own answers: the ACCEPTED
        # snapshot is kept (the engine short-circuits on it without reaching
        # the trade loop, and a re-ACCEPT of an advanced order is swallowed as
        # an idempotent transition), and the trade stays grouped under it.
        assert mass_status is not None
        assert VenueOrderId("900001") in mass_status.order_reports
        assert [fill.trade_id for fill in mass_status.fill_reports[VenueOrderId("900001")]] == [
            TradeId("T-GAP")
        ]

    def test_a_snapshot_the_sweep_outran_is_withheld_from_the_mass_status(self, perp_harness):
        """A fall-through snapshot claiming fewer fills than the order now holds
        would fail the whole startup reconciliation.

        ``_handle_fill_quantity_mismatch`` (installed live/execution_engine.py)
        reads ``report.filled_qty < order.filled_qty`` as duplicate fills or
        corrupted cache, logs at ERROR and returns False — and a False from
        ``reconcile_execution_state`` aborts node start (system/kernel.py). The
        snapshot is merely older than the trades the sweep just booked, so it
        is withheld. An equally stale ACCEPTED snapshot stays: the engine
        short-circuits on it harmlessly, and the mass status remains the
        venue's answer rather than this client's edit of it.
        """
        env = perp_harness
        instrument = env.cache.instrument(PERP_BTC_USDT)
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        text = f"t-{order.client_order_id.value}"

        # The order already carries the six lots the sweep just booked.
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-NEW", -6, text=text),
        )
        env.drain(order)
        assert order.filled_qty == Quantity.from_int(6)

        booked_fill = env.client._parse_fill_report(
            GateioProductType.PERP,
            _futures_fill_payload("T-NEW", -6, text=text),
            instrument,
        )
        stale = env.client._parse_order_status_report(
            GateioProductType.PERP,
            _futures_order_payload(left=-8, text=text),  # PARTIALLY_FILLED, filled 2 of 10
            instrument,
        )
        untouched = env.client._parse_order_status_report(
            GateioProductType.PERP,
            _futures_order_payload(left=-10, text=text),  # ACCEPTED, filled 0
            instrument,
        )

        kept = env.client._prune_reports_the_sweep_outran([stale, untouched], [booked_fill])

        assert kept == [untouched]


class TestRecoveredTradeWithoutAdoptedOrder:
    """Regression (REC-08): a dangling venue-order-id index is not an order.

    Live validation caught the pairing every offline round had missed: the
    engine, configured with ``filter_unclaimed_external_orders=True``, drops an
    unclaimed external order inside ``_generate_order`` and *still* indexes its
    client/venue order ids afterwards (``_reconcile_execution_mass_status``,
    installed live/execution_engine.py:1915-1925). The recovery sweep then read
    that bare index entry as proof the single-report channel could book, and
    the lone ``FillReport`` it sent crashed the engine instead of deferring:
    ``_reconcile_fill_report_single`` resolves the index, misses the order and
    calls ``_find_order_by_venue_order_id(order_side=None)``, whose
    ``Cache.orders(side=None)`` the Cython signature refuses with
    ``TypeError("an integer is required")``. The TypeError escaped
    ``generate_mass_status``, the engine logged "Failed to generate mass
    status", and the node started without a reconciled execution state.
    """

    FOREIGN_TEXT = "t-O-20260729-043212-002-002-1"
    FOREIGN_ORDER_ID = "910001"

    @classmethod
    def _foreign_settled_order_payload(cls) -> dict[str, Any]:
        """The venue's statement of a spot cash market buy another run settled."""
        return {
            "id": cls.FOREIGN_ORDER_ID,
            "text": cls.FOREIGN_TEXT,
            "currency_pair": "BTC_USDT",
            "type": "market",
            "account": "spot",
            "side": "buy",
            "amount": "590",  # quote cash, as Gate.io states a market buy
            "price": "0",
            "status": "closed",
            "time_in_force": "ioc",
            "left": "0.01",
            "filled_amount": "0.010000",
            "avg_deal_price": "59000.00",
            "fee": "0.0000098",
            "fee_currency": "BTC",
            "create_time": RECENT_SECS - 3600,
            "update_time": RECENT_SECS - 3599,
        }

    @classmethod
    def _foreign_settled_trade_payload(cls) -> dict[str, Any]:
        return {
            "id": "T-FOREIGN",
            "currency_pair": "BTC_USDT",
            "order_id": cls.FOREIGN_ORDER_ID,
            "side": "buy",
            "amount": "0.010000",
            "price": "59000.00",
            "fee": "0.0000098",
            "fee_currency": "BTC",
            "role": "taker",
            "create_time_ms": str((RECENT_SECS - 3599) * 1000),
            "text": cls.FOREIGN_TEXT,
        }

    @classmethod
    def _wire_foreign_history(cls, env: ExecHarness) -> None:
        """A fresh account whose trade listing names one settled foreign trade."""
        env.spot.responses["open_orders"] = []
        env.spot.responses["list_price_orders"] = []
        env.spot.responses["list_orders"] = []
        env.spot.responses["my_trades"] = lambda **kwargs: (
            [cls._foreign_settled_trade_payload()] if kwargs.get("page", 1) == 1 else []
        )
        env.spot.responses["get_order"] = cls._foreign_settled_order_payload()

    @classmethod
    def _foreign_fill_report(cls, env: ExecHarness) -> Any:
        instrument = env.cache.instrument(SPOT_BTC_USDT)
        return env.client._parse_fill_report(
            GateioProductType.SPOT,
            cls._foreign_settled_trade_payload(),
            instrument,
        )

    @staticmethod
    def _seed_dangling_index(env: ExecHarness, client_order_id_value: str, venue_id: str) -> None:
        """The cache state the engine's filtered-external-order path leaves behind."""
        from nautilus_trader.model.identifiers import ClientOrderId

        env.cache.add_venue_order_id(
            client_order_id=ClientOrderId(client_order_id_value),
            venue_order_id=VenueOrderId(venue_id),
        )

    def test_the_live_crash_a_filtered_external_order_no_longer_kills_the_mass_status(
        self,
        spot_harness,
    ):
        """The exact live pairing, against the real installed engine.

        Pre-fix this raised ``TypeError: an integer is required`` out of
        ``generate_mass_status`` — the crash the live node logged as "Failed
        to generate mass status" before starting unusable.
        """
        from nautilus_trader.live.config import LiveExecEngineConfig
        from nautilus_trader.live.execution_engine import LiveExecutionEngine
        from nautilus_trader.model.identifiers import ClientOrderId

        env = spot_harness
        # The harness endpoints make way for the real engine's own.
        env.msgbus.deregister(endpoint="ExecEngine.process", handler=env.events.append)
        env.msgbus.deregister(
            endpoint="ExecEngine.reconcile_execution_report",
            handler=env.reports.append,
        )
        engine = LiveExecutionEngine(
            loop=env.loop,
            msgbus=env.msgbus,
            cache=env.cache,
            clock=env.clock,
            config=LiveExecEngineConfig(filter_unclaimed_external_orders=True),
        )
        engine.register_client(env.client)
        self._wire_foreign_history(env)

        mass_status = env.run(env.client.generate_mass_status(None))

        # The mass status survives, carrying the venue's own answers.
        assert mass_status is not None
        assert [
            fill.trade_id for fill in mass_status.fill_reports[VenueOrderId(self.FOREIGN_ORDER_ID)]
        ] == [TradeId("T-FOREIGN")]

        # The engine's filtering left exactly the state the old sweep misread:
        # the ids indexed, the order never created. Documented here because it
        # is the whole mechanism of the defect.
        indexed = env.cache.client_order_id(VenueOrderId(self.FOREIGN_ORDER_ID))
        assert indexed == ClientOrderId("O-20260729-043212-002-002-1")
        assert env.cache.order(indexed) is None

        # And the engine's own front-door reconciliation of the returned mass
        # status must not crash on the excluded trade either.
        assert engine._reconcile_execution_mass_status(mass_status) is True

    def test_a_dangling_index_entry_is_not_read_as_a_bookable_order(self, spot_harness):
        """No lone ``FillReport`` may be sent where only the index answers.

        Pre-fix the sweep sent one ("could only be booked without the venue's
        statement"), which is precisely the send that crashed live: the
        single-report channel cannot resolve an order the engine refused to
        create.
        """
        env = spot_harness
        mass_statuses: list[Any] = []
        env.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_mass_status",
            handler=mass_statuses.append,
        )
        self._wire_foreign_history(env)
        self._seed_dangling_index(env, "O-20260729-043212-002-002-1", self.FOREIGN_ORDER_ID)
        fill = self._foreign_fill_report(env)

        env.run(env.client._hand_over_fills_with_their_order([fill]))

        # The grouped offer went out with the venue's statement; nothing was
        # sent over the channel that cannot book it.
        assert len(mass_statuses) == 1
        assert env.reports == []

    def test_an_unobtainable_order_statement_refuses_the_startup_mass_status(
        self,
        spot_harness,
    ):
        """Asked-and-unanswered is a refusal, not a silently lighter book.

        Pre-fix the sweep logged the loss and returned a mass status that
        simply lacked the venue's execution: reconciliation reported success
        over a book missing money. The platform posture for an unanswerable
        listing — refuse the whole mass status, let the kernel decline to
        start, heal on the next attempt — applies to the one-order statement
        a recovered trade cannot be booked without.
        """
        env = spot_harness
        self._wire_foreign_history(env)
        env.spot.responses["get_order"] = GateioClientError(404, "ORDER_NOT_FOUND", "not found")

        mass_status = env.run(env.client.generate_mass_status(None))

        assert mass_status is None
        assert env.reports == []  # nothing half-booked on the way down

    def test_the_reconnect_sweep_keeps_state_when_the_statement_is_unobtainable(
        self,
        spot_harness,
    ):
        """The reconnect route has no mass status to refuse: it keeps its state.

        The grouped hand-over has already applied what it could; the raise from
        the sweep is absorbed into the same stale-but-honest posture as a
        failed trade listing, and the next reconnect or restart re-reads the
        order.
        """
        env = spot_harness
        mass_statuses: list[Any] = []
        env.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_mass_status",
            handler=mass_statuses.append,
        )
        env.spot.responses["accounts"] = []
        self._wire_foreign_history(env)
        env.spot.responses["get_order"] = GateioClientError(404, "ORDER_NOT_FOUND", "not found")

        env.run(env.client._reconcile_after_reconnect(GateioProductType.SPOT))

        # The grouped pass delivered the listings; the unanswerable statement
        # neither crashed the pass nor produced a lone fill report.
        assert len(mass_statuses) == 1
        assert [type(r).__name__ for r in env.reports] == []

    def test_a_stream_fill_at_a_dangling_index_goes_the_grouped_route(self, spot_harness):
        """The runtime twin of the startup crash, closed by the same gate.

        ``_hand_over_fill`` used to send a lone ``FillReport`` whenever the
        index answered — the same dangling entry would crash the engine from
        the WebSocket path at runtime, after startup had already survived.
        """
        env = spot_harness
        mass_statuses: list[Any] = []
        env.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_mass_status",
            handler=mass_statuses.append,
        )
        self._wire_foreign_history(env)
        self._seed_dangling_index(env, "O-20260729-043212-002-002-1", self.FOREIGN_ORDER_ID)
        fill = self._foreign_fill_report(env)

        env.client._hand_over_fill(fill)
        env.run(_drain_tasks(env))

        # The grouped route asked the venue for the order; the lone-report
        # channel that cannot resolve it was never used.
        assert env.spot.called("get_order")
        assert len(mass_statuses) == 1
        assert env.reports == []
