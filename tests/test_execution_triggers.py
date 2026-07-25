"""Conditional-order identity tests for ``GateioExecutionClient``.

Gate.io arms a price-triggered order under one id and creates a **different**
order when the trigger fires. Both identities have to survive, in both
directions, and both have to be rebuildable from the venue alone after a
restart — on spot that is the only way the order can be recognised at all,
because a spot price order has no client-id field (its ``put.text`` is an
order-source marker such as ``api``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
)
from nautilus_trader.model.enums import OrderSide, OrderStatus, OrderType
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import TradeId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity

from nautilus_gateio.common.enums import GateioProductType

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

WINDOW_START = datetime(2026, 7, 25, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 27, tzinfo=UTC)
INSIDE_SECS = int(datetime(2026, 7, 26, tzinfo=UTC).timestamp())

ARMED_ID = "A-1"
FIRED_ID = "5555"


@pytest.fixture()
def spot_env():
    env = ExecHarness()
    yield env
    env.close()


@pytest.fixture()
def perp_env():
    env = ExecHarness(products=(GateioProductType.PERP,))
    yield env
    env.close()


def _spot_price_order(status: str, fired_order_id: Any = None) -> dict[str, Any]:
    """A ``GET /spot/price_orders`` entry. Note ``put`` carries no client id."""
    payload: dict[str, Any] = {
        "id": int(ARMED_ID.split("-")[1]),
        "id_string": ARMED_ID,
        "market": "BTC_USDT",
        "user": 1000,
        "status": status,
        "ctime": INSIDE_SECS,
        "trigger": {"price": "59500.00", "rule": "<=", "expiration": 0},
        "put": {
            "type": "limit",
            "side": "sell",
            "price": "59000.00",
            "amount": "0.010000",
            "account": "normal",
            "time_in_force": "gtc",
            "text": "api",
        },
    }
    if fired_order_id is not None:
        payload["fired_order_id"] = fired_order_id
    return payload


def _fired_spot_order(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": FIRED_ID,
        "currency_pair": "BTC_USDT",
        "type": "limit",
        "side": "sell",
        "account": "spot",
        "amount": "0.010000",
        "left": "0.010000",
        "filled_amount": "0",
        "price": "59000.00",
        "status": "open",
        "time_in_force": "gtc",
        "text": "api",  # the venue's own source marker, never a client id
        "create_time": INSIDE_SECS,
        "update_time": INSIDE_SECS,
    }
    payload.update(overrides)
    return payload


def _spot_fill(trade_id: str, amount: str = "0.010000") -> dict[str, Any]:
    return {
        "id": trade_id,
        "currency_pair": "BTC_USDT",
        "order_id": FIRED_ID,
        "side": "sell",
        "amount": amount,
        "price": "59000.00",
        "fee": "0.6",
        "fee_currency": "USDT",
        "role": "taker",
        "create_time": INSIDE_SECS,
        "text": "api",
    }


def _restarted_client_state(env: ExecHarness) -> Any:
    """The state a restart leaves behind: a cached order, no in-memory maps.

    The order was armed before the restart, so the cache still indexes it
    against the **armed** id and nothing else is known.
    """
    order = env.order_factory.stop_limit(
        SPOT_BTC_USDT,
        OrderSide.SELL,
        Quantity.from_str("0.010000"),
        Price.from_str("59000.00"),
        Price.from_str("59500.00"),
    )
    env.accepted(order, ARMED_ID)
    assert env.client.trigger_links == {}, "an in-memory map must not survive a restart"
    return order


# -- MANDATORY TEST 3: restart between the trigger and the fill ---------------


class TestRestartAcrossTheTriggerTransition:
    @staticmethod
    def _wire_reconciliation(env: ExecHarness) -> None:
        env.spot.responses["list_price_orders"] = lambda **kwargs: (
            [_spot_price_order("finish", fired_order_id=int(FIRED_ID))]
            if kwargs.get("status") == "finished" and kwargs.get("offset", 0) == 0
            else []
        )
        env.spot.responses["open_orders"] = lambda **kwargs: (
            [{"currency_pair": "BTC_USDT", "total": 1, "orders": [_fired_spot_order()]}]
            if kwargs.get("page", 1) == 1
            else []
        )
        env.spot.responses["list_orders"] = []
        env.spot.responses["my_trades"] = lambda **kwargs: (
            [_spot_fill("TR-1")] if kwargs.get("page", 1) == 1 else []
        )

    def _reconcile(self, env: ExecHarness) -> list[Any]:
        return env.run(
            env.client.generate_order_status_reports(
                GenerateOrderStatusReports(
                    instrument_id=None,
                    start=WINDOW_START,
                    end=WINDOW_END,
                    open_only=False,
                    command_id=UUID4(),
                    ts_init=0,
                ),
            ),
        )

    def test_identity_is_rebuilt_from_the_price_order_listing(self, spot_env):
        env = spot_env
        order = _restarted_client_state(env)
        self._wire_reconciliation(env)

        self._reconcile(env)

        link = env.client.trigger_links[order.client_order_id]
        assert link.armed_id == ARMED_ID
        assert link.fired_id == FIRED_ID
        assert env.client._trigger_link_for_venue_order_id(FIRED_ID) is link
        assert env.client._trigger_link_for_venue_order_id(ARMED_ID) is link

    def test_the_fired_order_is_reported_against_the_right_client_order_id(self, spot_env):
        env = spot_env
        order = _restarted_client_state(env)
        self._wire_reconciliation(env)

        reports = self._reconcile(env)

        fired = [report for report in reports if report.venue_order_id == VenueOrderId(FIRED_ID)]
        assert len(fired) == 1
        assert fired[0].client_order_id == order.client_order_id
        assert fired[0].instrument_id == SPOT_BTC_USDT

    def test_the_fill_resolves_to_the_same_order(self, spot_env):
        env = spot_env
        order = _restarted_client_state(env)
        self._wire_reconciliation(env)
        self._reconcile(env)

        fills = env.run(
            env.client.generate_fill_reports(
                GenerateFillReports(
                    instrument_id=SPOT_BTC_USDT,
                    venue_order_id=None,
                    start=WINDOW_START,
                    end=WINDOW_END,
                    command_id=UUID4(),
                    ts_init=0,
                ),
            ),
        )

        assert len(fills) == 1
        assert fills[0].client_order_id == order.client_order_id
        assert fills[0].venue_order_id == VenueOrderId(FIRED_ID)
        assert fills[0].trade_id == TradeId("TR-1")

    def test_the_fill_applies_with_no_duplicate_and_no_unknown_order(self, spot_env):
        env = spot_env
        order = _restarted_client_state(env)
        self._wire_reconciliation(env)
        self._reconcile(env)

        # Reconciliation rebases the venue order id, exactly as the live path does.
        env.client._maybe_swap_trigger_venue_order_id(
            order,
            VenueOrderId(FIRED_ID),
            env.clock.timestamp_ns(),
        )
        env.drain(order)
        assert order.venue_order_id == VenueOrderId(FIRED_ID)

        env.client._handle_fill_payload(GateioProductType.SPOT, _spot_fill("TR-1"))
        env.drain(order)

        applied = env.events_of(OrderFilled)
        assert len(applied) == 1
        assert applied[0].client_order_id == order.client_order_id
        assert order.status == OrderStatus.FILLED

        # The same trade id again must change nothing, and must not be reported
        # as an external (unknown) fill either.
        from nautilus_trader.execution.reports import FillReport

        before = len([report for report in env.reports if isinstance(report, FillReport)])
        env.client._handle_fill_payload(GateioProductType.SPOT, _spot_fill("TR-1"))
        env.drain(order)

        assert len(env.events_of(OrderFilled)) == 1
        assert len([r for r in env.reports if isinstance(r, FillReport)]) == before

    def test_a_still_armed_order_is_reported_as_armed_after_a_restart(self, spot_env):
        env = spot_env
        order = _restarted_client_state(env)
        env.spot.responses["list_price_orders"] = lambda **kwargs: (
            [_spot_price_order("open")]
            if kwargs.get("status") == "open" and kwargs.get("offset", 0) == 0
            else []
        )

        reports = env.run(
            env.client.generate_order_status_reports(
                GenerateOrderStatusReports(
                    instrument_id=None,
                    start=WINDOW_START,
                    end=WINDOW_END,
                    open_only=True,
                    command_id=UUID4(),
                    ts_init=0,
                ),
            ),
        )

        assert len(reports) == 1
        assert reports[0].client_order_id == order.client_order_id
        assert reports[0].venue_order_id == VenueOrderId(ARMED_ID)
        assert reports[0].order_status == OrderStatus.ACCEPTED
        assert reports[0].trigger_price == Price.from_str("59500.00")
        assert reports[0].order_type == OrderType.STOP_LIMIT

    def test_cancel_after_a_restart_still_disarms_by_the_armed_id(self, spot_env):
        from nautilus_trader.execution.messages import CancelOrder

        env = spot_env
        order = _restarted_client_state(env)
        env.spot.responses["list_price_orders"] = lambda **kwargs: (
            [_spot_price_order("open")]
            if kwargs.get("status") == "open" and kwargs.get("offset", 0) == 0
            else []
        )
        env.run(
            env.client.generate_order_status_reports(
                GenerateOrderStatusReports(
                    instrument_id=None,
                    start=WINDOW_START,
                    end=WINDOW_END,
                    open_only=True,
                    command_id=UUID4(),
                    ts_init=0,
                ),
            ),
        )

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

        assert [call.args[0] for call in env.spot.calls_named("cancel_price_order")] == [ARMED_ID]


# -- live resolution of a fired spot order -----------------------------------


class TestFiredOrderResolution:
    """A fired spot order arrives with no recoverable client id at all."""

    def test_unresolvable_payload_schedules_a_resolution(self, spot_env):
        env = spot_env
        order = env.order_factory.stop_limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
        )
        env.accepted(order, ARMED_ID)
        env.client._register_trigger_link(
            GateioProductType.SPOT,
            ARMED_ID,
            order.client_order_id,
        )

        scheduled = env.client._schedule_fired_order_resolution(
            GateioProductType.SPOT,
            "BTC_USDT",
            VenueOrderId(FIRED_ID),
        )
        assert scheduled is True
        # Drain the task the client scheduled so the loop closes cleanly.
        env.spot.responses["get_price_order"] = _spot_price_order(
            "finish",
            fired_order_id=int(FIRED_ID),
        )
        env.run(_drain_tasks(env))

        link = env.client.trigger_links[order.client_order_id]
        assert link.fired_id == FIRED_ID

    def test_resolution_binds_the_armed_order_and_rebases_the_id(self, spot_env):
        env = spot_env
        order = env.order_factory.stop_limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
        )
        env.accepted(order, ARMED_ID)
        link = env.client._register_trigger_link(
            GateioProductType.SPOT,
            ARMED_ID,
            order.client_order_id,
        )
        env.spot.responses["get_price_order"] = _spot_price_order(
            "finish",
            fired_order_id=int(FIRED_ID),
        )

        env.run(
            env.client._resolve_fired_order(
                GateioProductType.SPOT,
                VenueOrderId(FIRED_ID),
                [link],
            ),
        )
        env.drain(order)

        assert link.fired_id == FIRED_ID
        assert order.venue_order_id == VenueOrderId(FIRED_ID)
        assert order.status == OrderStatus.TRIGGERED

    def test_resolution_leaves_a_genuinely_external_order_alone(self, spot_env):
        env = spot_env
        order = env.order_factory.stop_limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
        )
        env.accepted(order, ARMED_ID)
        link = env.client._register_trigger_link(
            GateioProductType.SPOT,
            ARMED_ID,
            order.client_order_id,
        )
        env.spot.responses["get_price_order"] = _spot_price_order("open")

        env.run(
            env.client._resolve_fired_order(
                GateioProductType.SPOT,
                VenueOrderId("9999"),
                [link],
            ),
        )

        assert link.fired_id is None
        assert order.venue_order_id == VenueOrderId(ARMED_ID)

    def test_no_armed_orders_means_no_resolution_attempt(self, spot_env):
        env = spot_env
        assert (
            env.client._schedule_fired_order_resolution(
                GateioProductType.SPOT,
                "BTC_USDT",
                VenueOrderId(FIRED_ID),
            )
            is False
        )


# -- futures identity: `initial.text` and `trade_id` -------------------------


class TestFuturesTriggerIdentity:
    @staticmethod
    def _futures_price_order(status: str, trade_id: int, text: str) -> dict[str, Any]:
        return {
            "id": 777,
            "id_string": "777",
            "status": status,
            "trade_id": trade_id,
            "create_time": INSIDE_SECS,
            "trigger": {
                "strategy_type": 0,
                "price_type": 1,
                "price": "59500.0",
                "rule": 2,
            },
            "initial": {
                "contract": "BTC_USDT",
                "size": -10,
                "price": "59000.0",
                "tif": "gtc",
                "text": text,
            },
        }

    def test_text_recovers_the_client_order_id(self, perp_env):
        env = perp_env
        order = env.order_factory.stop_limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
            Price.from_str("59500.0"),
        )
        env.accepted(order, "777")

        link = env.client._link_from_trigger_payload(
            GateioProductType.PERP,
            self._futures_price_order("open", 0, f"t-{order.client_order_id.value}"),
        )

        assert link is not None
        assert link.client_order_id == order.client_order_id
        assert link.armed_id == "777"
        assert link.is_armed is True

    def test_trade_id_supplies_the_fired_order_id(self, perp_env):
        env = perp_env
        order = env.order_factory.stop_limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
            Price.from_str("59500.0"),
        )
        env.accepted(order, "777")

        link = env.client._link_from_trigger_payload(
            GateioProductType.PERP,
            self._futures_price_order("finish", 900001, f"t-{order.client_order_id.value}"),
        )

        assert link is not None
        assert link.fired_id == "900001"
        assert link.is_armed is False

    def test_zero_trade_id_is_not_a_fired_order(self, perp_env):
        env = perp_env
        order = env.order_factory.stop_limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
            Price.from_str("59500.0"),
        )
        env.accepted(order, "777")

        link = env.client._link_from_trigger_payload(
            GateioProductType.PERP,
            self._futures_price_order("open", 0, f"t-{order.client_order_id.value}"),
        )
        assert link is not None
        assert link.fired_id is None

    def test_single_report_lookup_follows_the_armed_order(self, perp_env):
        env = perp_env
        order = env.order_factory.stop_limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
            Price.from_str("59500.0"),
        )
        env.accepted(order, "777")
        env.client._register_trigger_link(GateioProductType.PERP, "777", order.client_order_id)
        env.perp.responses["get_price_order"] = self._futures_price_order(
            "open",
            0,
            f"t-{order.client_order_id.value}",
        )

        report = env.run(
            env.client.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=PERP_BTC_USDT,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=0,
                ),
            ),
        )

        assert report is not None
        assert report.venue_order_id == VenueOrderId("777")
        assert report.client_order_id == order.client_order_id
        assert report.trigger_price == Price.from_str("59500.0")
        assert not env.perp.called("get_order")

    def test_single_report_lookup_follows_the_fired_order(self, perp_env):
        env = perp_env
        order = env.order_factory.stop_limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
            Price.from_str("59500.0"),
        )
        env.accepted(order, "777")
        env.client._register_trigger_link(
            GateioProductType.PERP,
            "777",
            order.client_order_id,
            fired_id="900001",
        )
        env.perp.responses["get_order"] = {
            "id": 900001,
            "id_string": "900001",
            "contract": "BTC_USDT",
            "size": -10,
            "left": 0,
            "price": "59000.0",
            "tif": "gtc",
            "status": "finished",
            "finish_as": "filled",
            "create_time": INSIDE_SECS,
        }

        report = env.run(
            env.client.generate_order_status_report(
                GenerateOrderStatusReport(
                    instrument_id=PERP_BTC_USDT,
                    client_order_id=order.client_order_id,
                    venue_order_id=None,
                    command_id=UUID4(),
                    ts_init=0,
                ),
            ),
        )

        assert report is not None
        assert report.venue_order_id == VenueOrderId("900001")
        assert [call.args[0] for call in env.perp.calls_named("get_order")] == ["900001"]
        assert not env.perp.called("get_price_order")


# -- helpers ------------------------------------------------------------------


async def _drain_tasks(env: ExecHarness) -> None:
    import asyncio

    pending = [task for task in asyncio.all_tasks(env.loop) if task is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
