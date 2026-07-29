"""Ambiguous command outcomes must not be reported as venue rejections.

NautilusTrader's live execution policy (concepts/live.md, "Order command outcome
policy") permits ``OrderRejected``, ``OrderCancelRejected`` and
``OrderModifyRejected`` only when the venue's answer proves the command was
refused. Transport failures, server errors, whole-batch failures without
per-order results and parse failures after a request may have reached the venue
are *ambiguous*: the adapter logs them, leaves the order in its in-flight state
and lets the execution engine resolve it.

Every test here drives a real ``Order`` through the real finite state machine, so
the consequence of the old behaviour is visible rather than asserted: an order
locally rejected on a read timeout can never accept the ``OrderAccepted`` that
reconciliation would have to emit for an order Gate.io is in fact holding.
"""

from __future__ import annotations

from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import BatchCancelOrders, CancelOrder, ModifyOrder
from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.events import (
    OrderCancelRejected,
    OrderModifyRejected,
    OrderPendingCancel,
    OrderPendingUpdate,
    OrderRejected,
)
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Price, Quantity

from nautilus_gateio.common.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
    OrderValidationError,
    UnsupportedOrderError,
)
from nautilus_gateio.execution import is_ambiguous_outcome
from nautilus_gateio.http.client import (
    GateioAmbiguousServerError,
    GateioRequestAmbiguousError,
)

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        SPOT_BTC_USDT,
        ExecHarness,
        _submit,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
        SPOT_BTC_USDT,
        ExecHarness,
        _submit,
    )


@pytest.fixture()
def harness():
    env = ExecHarness()
    yield env
    env.close()


# -- helpers ------------------------------------------------------------------


AMBIGUOUS_FAILURES: dict[str, BaseException] = {
    "read timeout after the request was sent": GateioRequestAmbiguousError(
        0,
        "REQUEST_AMBIGUOUS",
        "POST /spot/orders failed after the request was sent",
    ),
    "5xx on a mutating request": GateioAmbiguousServerError(502, "SERVER_ERROR", "bad gateway"),
    "5xx on a replayed request": GateioServerError(500, "SERVER_ERROR", "internal"),
    "unreadable response": KeyError("id"),
}

#: The subset raised by the transport itself. The batch-cancel path reports
#: venue failures only; anything else propagates to the client's task handler
#: without an event, which is already "unresolved".
AMBIGUOUS_VENUE_FAILURES: dict[str, GateioError] = {
    name: error for name, error in AMBIGUOUS_FAILURES.items() if isinstance(error, GateioError)
}

DEFINITIVE_REFUSALS: dict[str, GateioError] = {
    "bad parameter": GateioClientError(400, "INVALID_PARAM_VALUE", "bad amount"),
    "rate limited": GateioClientError(429, "TOO_MANY_REQUESTS", "slow down"),
    "unknown order": GateioClientError(404, "ORDER_NOT_FOUND", "no such order"),
    "never left the process": GateioError(0, "NETWORK_ERROR", "connection refused"),
}


def _limit_order(env: ExecHarness) -> Any:
    return env.order_factory.limit(
        SPOT_BTC_USDT,
        OrderSide.BUY,
        Quantity.from_str("0.010000"),
        Price.from_str("60000.00"),
    )


def _pending_cancel(env: ExecHarness, order: Any) -> None:
    """Apply the ``OrderPendingCancel`` the strategy emits before the command."""
    order.apply(
        OrderPendingCancel(
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


def _pending_update(env: ExecHarness, order: Any) -> None:
    """Apply the ``OrderPendingUpdate`` the strategy emits before the command."""
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


def _amend(env: ExecHarness, order: Any, price: Price) -> None:
    env.run(
        env.client._modify_order(
            ModifyOrder(
                trader_id=env.trader_id,
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                quantity=None,
                price=price,
                trigger_price=None,
                command_id=UUID4(),
                ts_init=env.clock.timestamp_ns(),
            ),
        ),
    )


# -- the classifier -----------------------------------------------------------


class TestOutcomeClassification:
    """The definitive/ambiguous split is by exception type, not by message."""

    @pytest.mark.parametrize("error", AMBIGUOUS_FAILURES.values(), ids=AMBIGUOUS_FAILURES)
    def test_unknown_outcomes_are_ambiguous(self, error: BaseException):
        assert is_ambiguous_outcome(error)

    @pytest.mark.parametrize("error", DEFINITIVE_REFUSALS.values(), ids=DEFINITIVE_REFUSALS)
    def test_venue_refusals_are_definitive(self, error: GateioError):
        assert not is_ambiguous_outcome(error)

    @pytest.mark.parametrize(
        "error",
        [
            OrderValidationError("min notional not met"),
            UnsupportedOrderError("Gate.io cannot express this order"),
        ],
        ids=["validation", "unsupported"],
    )
    def test_local_refusals_are_definitive(self, error: Exception):
        """Nothing was sent, so these prove non-acceptance however deep they occur."""
        assert not is_ambiguous_outcome(error)


# -- submit -------------------------------------------------------------------


class TestAmbiguousSubmit:
    @pytest.mark.parametrize("error", AMBIGUOUS_FAILURES.values(), ids=AMBIGUOUS_FAILURES)
    def test_ambiguous_submit_leaves_the_order_in_flight(self, harness, error: BaseException):
        harness.spot.responses["create_order"] = error
        order = _limit_order(harness)

        _submit(harness, order)
        harness.drain(order)

        assert harness.events_of(OrderRejected) == []
        assert order.status == OrderStatus.SUBMITTED

    def test_a_venue_that_kept_the_order_can_still_be_reconciled(self, harness):
        """The point of leaving it in flight: ``OrderRejected`` is terminal.

        Gate.io answered nothing, but it holds the order and reconciliation will
        find it. Applying the ``OrderAccepted`` that the in-flight check
        produces raises ``InvalidStateTrigger`` on a locally rejected order, and
        no later event can undo that.
        """
        harness.spot.responses["create_order"] = GateioRequestAmbiguousError(
            0,
            "REQUEST_AMBIGUOUS",
            "POST /spot/orders failed after the request was sent",
        )
        order = _limit_order(harness)

        _submit(harness, order)
        harness.drain(order)

        harness.client.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId("1001"),
            ts_event=harness.clock.timestamp_ns(),
        )
        harness.drain(order)

        assert order.status == OrderStatus.ACCEPTED
        assert order.venue_order_id == VenueOrderId("1001")

    @pytest.mark.parametrize("error", DEFINITIVE_REFUSALS.values(), ids=DEFINITIVE_REFUSALS)
    def test_a_proven_refusal_is_still_rejected(self, harness, error: GateioError):
        harness.spot.responses["create_order"] = error
        order = _limit_order(harness)

        _submit(harness, order)
        harness.drain(order)

        rejections = harness.events_of(OrderRejected)
        assert len(rejections) == 1
        assert error.label in rejections[0].reason
        assert order.status == OrderStatus.REJECTED

    def test_post_only_rejection_keeps_its_flag(self, harness):
        """A post-only reject is definitive and must keep ``due_post_only``."""
        harness.spot.responses["create_order"] = GateioClientError(
            400,
            "ORDER_POC_IMMEDIATE",
            "order would take liquidity",
        )
        order = _limit_order(harness)

        _submit(harness, order)
        harness.drain(order)

        rejections = harness.events_of(OrderRejected)
        assert len(rejections) == 1
        assert rejections[0].due_post_only

    def test_an_armed_order_whose_id_cannot_be_read_is_not_rejected(self, harness):
        """The venue armed the order; only its handle was lost."""
        harness.spot.responses["create_price_order"] = {"status": "ok"}
        harness.spot.responses["tickers"] = [{"last": "60000"}]
        order = harness.order_factory.limit_if_touched(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
        )

        _submit(harness, order)
        harness.drain(order)

        assert harness.events_of(OrderRejected) == []
        assert order.status == OrderStatus.SUBMITTED


# -- cancel -------------------------------------------------------------------


class TestAmbiguousCancel:
    @pytest.mark.parametrize("error", AMBIGUOUS_FAILURES.values(), ids=AMBIGUOUS_FAILURES)
    def test_ambiguous_cancel_stays_pending(self, harness, error: BaseException):
        harness.spot.responses["cancel_order"] = error
        order = harness.accepted(_limit_order(harness), "1001")
        _pending_cancel(harness, order)

        _cancel(harness, order)
        harness.drain(order)

        assert harness.events_of(OrderCancelRejected) == []
        assert order.status == OrderStatus.PENDING_CANCEL

    def test_a_proven_refusal_is_still_a_cancel_reject(self, harness):
        harness.spot.responses["cancel_order"] = GateioClientError(
            404,
            "ORDER_NOT_FOUND",
            "no such order",
        )
        order = harness.accepted(_limit_order(harness), "1001")
        _pending_cancel(harness, order)

        _cancel(harness, order)
        harness.drain(order)

        rejections = harness.events_of(OrderCancelRejected)
        assert len(rejections) == 1
        assert "ORDER_NOT_FOUND" in rejections[0].reason
        assert order.status == OrderStatus.ACCEPTED


class TestAmbiguousBatchCancel:
    def _batch(self, env: ExecHarness, orders: list[Any]) -> None:
        env.run(
            env.client._batch_cancel_orders(
                BatchCancelOrders(
                    trader_id=env.trader_id,
                    strategy_id=env.strategy_id,
                    instrument_id=SPOT_BTC_USDT,
                    cancels=[_cancel_command(env, order) for order in orders],
                    command_id=UUID4(),
                    ts_init=env.clock.timestamp_ns(),
                ),
            ),
        )

    def _two_pending_cancels(self, env: ExecHarness) -> list[Any]:
        orders = []
        for index, venue_order_id in enumerate(("1001", "1002")):
            order = env.accepted(
                env.order_factory.limit(
                    SPOT_BTC_USDT,
                    OrderSide.BUY,
                    Quantity.from_str("0.010000"),
                    Price.from_str(f"6000{index}.00"),
                ),
                venue_order_id,
            )
            _pending_cancel(env, order)
            orders.append(order)
        return orders

    @pytest.mark.parametrize(
        "error", AMBIGUOUS_VENUE_FAILURES.values(), ids=AMBIGUOUS_VENUE_FAILURES
    )
    def test_a_whole_batch_failure_resolves_nothing(self, harness, error: GateioError):
        """One 502 must not revert every order in the chunk to ACCEPTED."""
        harness.spot.responses["cancel_batch"] = error
        orders = self._two_pending_cancels(harness)

        self._batch(harness, orders)
        for order in orders:
            harness.drain(order)

        assert harness.events_of(OrderCancelRejected) == []
        assert all(order.status == OrderStatus.PENDING_CANCEL for order in orders)

    def test_per_order_failures_are_still_definitive(self, harness):
        """A 200 response carrying per-order results *is* the venue's answer."""
        harness.spot.responses["cancel_batch"] = [
            {"id": "1001", "succeeded": True},
            {"id": "1002", "succeeded": False, "label": "ORDER_NOT_FOUND", "message": "gone"},
        ]
        orders = self._two_pending_cancels(harness)

        self._batch(harness, orders)
        for order in orders:
            harness.drain(order)

        rejections = harness.events_of(OrderCancelRejected)
        assert len(rejections) == 1
        assert rejections[0].client_order_id == orders[1].client_order_id

    def test_a_refused_batch_request_is_definitive(self, harness):
        """A 4xx on the batch endpoint means the venue processed none of it."""
        harness.spot.responses["cancel_batch"] = GateioClientError(
            400,
            "INVALID_CURRENCY_PAIR",
            "unknown pair",
        )
        orders = self._two_pending_cancels(harness)

        self._batch(harness, orders)
        for order in orders:
            harness.drain(order)

        assert len(harness.events_of(OrderCancelRejected)) == len(orders)


# -- modify -------------------------------------------------------------------


class TestAmbiguousAmend:
    @pytest.mark.parametrize("error", AMBIGUOUS_FAILURES.values(), ids=AMBIGUOUS_FAILURES)
    def test_ambiguous_amend_stays_pending(self, harness, error: BaseException):
        """PENDING_UPDATE is the only state the in-flight check re-reads prices in."""
        harness.spot.responses["amend_order"] = error
        order = harness.accepted(_limit_order(harness), "1001")
        _pending_update(harness, order)

        _amend(harness, order, Price.from_str("59000.00"))
        harness.drain(order)

        assert harness.events_of(OrderModifyRejected) == []
        assert order.status == OrderStatus.PENDING_UPDATE

    def test_a_proven_refusal_is_still_a_modify_reject(self, harness):
        harness.spot.responses["amend_order"] = GateioClientError(
            400,
            "ORDER_NOT_FOUND",
            "no such order",
        )
        order = harness.accepted(_limit_order(harness), "1001")
        _pending_update(harness, order)

        _amend(harness, order, Price.from_str("59000.00"))
        harness.drain(order)

        rejections = harness.events_of(OrderModifyRejected)
        assert len(rejections) == 1
        assert "ORDER_NOT_FOUND" in rejections[0].reason
        assert order.status == OrderStatus.ACCEPTED
