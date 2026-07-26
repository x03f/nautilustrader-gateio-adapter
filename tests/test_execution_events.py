"""Event-stream tests for ``GateioExecutionClient``.

Order events, fills and balance updates are driven through the client exactly as
the private WebSocket would deliver them, and every generated event is applied to
a real NautilusTrader ``Order`` so the finite state machine has to accept the
sequence.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.events import (
    OrderCanceled,
    OrderFilled,
    OrderTriggered,
    OrderUpdated,
)
from nautilus_trader.model.identifiers import TradeId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity

from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode

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
        "create_time": 1785000000,
        "update_time": 1785000001,
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
        "create_time": 1785000002,
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


# -- spot fee netting on the event stream ------------------------------------


class TestSpotBaseFeeNetting:
    def test_base_currency_fee_is_netted_off_the_fill_quantity(self, spot_harness):
        env = spot_harness
        order = env.order_factory.limit(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("0.010000"),
            Price.from_str("60000.00"),
            time_in_force=TimeInForce.GTC,
        )
        env.accepted(order, "778899")
        env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            {
                "id": "TS-1",
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
            },
        )
        env.drain(order)

        fill = env.events_of(OrderFilled)[0]
        assert fill.last_qty == Quantity.from_str("0.009990")
        assert fill.commission.currency.code == "BTC"

    def test_quote_currency_fee_is_not_netted(self, spot_harness):
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
            {
                "id": "TS-2",
                "currency_pair": "BTC_USDT",
                "order_id": "778900",
                "side": "sell",
                "amount": "0.010000",
                "price": "60000.00",
                "fee": "0.6",
                "fee_currency": "USDT",
                "role": "taker",
                "create_time_ms": "1785000000000",
                "text": f"t-{order.client_order_id.value}",
            },
        )
        env.drain(order)

        fill = env.events_of(OrderFilled)[0]
        assert fill.last_qty == Quantity.from_str("0.010000")


# -- reconnect must reconcile, not just refresh balances ---------------------


class TestReconnectReconciliation:
    """Regression for EXEC-8 / seam-03: Gate.io replays nothing on a reconnect."""

    def test_reconnect_requeries_orders_and_fills(self, perp_harness):
        env = perp_harness
        env.perp.responses["accounts"] = {
            "currency": "USDT",
            "total": "1000",
            "available": "1000",
            "unrealised_pnl": "0",
        }
        env.perp.responses["positions"] = []
        env.perp.responses["list_orders"] = lambda **kwargs: (
            [_futures_order_payload()] if kwargs.get("status") == "open" else []
        )
        env.perp.responses["my_trades"] = lambda **kwargs: (
            [_futures_fill_payload("T-GAP", -3)] if kwargs.get("offset", 0) == 0 else []
        )

        env.run(env.client._reconcile_after_reconnect(GateioProductType.PERP))

        assert env.perp.called("list_orders")
        assert env.perp.called("my_trades")

        from nautilus_trader.execution.reports import FillReport, OrderStatusReport

        assert any(isinstance(report, OrderStatusReport) for report in env.reports)
        fills = [report for report in env.reports if isinstance(report, FillReport)]
        assert [report.trade_id for report in fills] == [TradeId("T-GAP")]

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

    def test_a_mismatched_identity_without_a_trigger_link_reconciles(self, perp_harness):
        """An id that cannot be reconciled inline goes to reconciliation, not to /dev/null.

        This is the safety net for identity transitions this client does not
        model. Emitting the fill would have it rejected and dropped; reporting it
        keeps it recoverable and says why.
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

        before = len(env.reports)
        env.client._handle_fill_payload(
            GateioProductType.PERP,
            _futures_fill_payload("T-9", -4, text=text),
        )
        events = env.drain(order)

        assert events == []
        assert len(env.reports) == before + 1
        assert order.venue_order_id == VenueOrderId("555000")
        assert order.filled_qty == Quantity.from_int(0)

    def test_that_reconciled_fill_is_not_reported_twice(self, perp_harness):
        """A replay of the unreconcilable fill must not produce a second report."""
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
        after_first = len(env.reports)
        env.client._handle_fill_payload(GateioProductType.PERP, dict(payload))

        assert len(env.reports) == after_first

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
