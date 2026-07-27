"""Order-fidelity tests: the order Gate.io receives must be the order that was asked for.

Every case here is a place where the client used to answer an instruction it
could not carry with a *different* instruction the venue happens to accept — a
completed order relabelled expired, a mark-price trigger armed on the last
price, a fill-or-kill buy downgraded to immediate-or-cancel, a stop armed as an
if-touched order. NautilusTrader's rule for that is in concepts/orders/index.md
(lines 16-20): "If an order includes an instruction or option the target venue
does not support, the system does not submit it. Instead, it logs a clear,
explanatory error."

The tests assert the damage rather than the shape of the fix: a lost execution,
a trigger armed on the wrong price, a lost execution guarantee, an order type
turned into its opposite.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from nautilus_trader.model.enums import (
    OrderSide,
    OrderStatus,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.model.events import OrderFilled, OrderRejected
from nautilus_trader.model.objects import Price, Quantity

from nautilus_gateio.common.enums import (
    GateioFinishAs,
    GateioProductType,
    order_status_from_gateio,
)

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
        _assert_denied,
        _submit,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
        PERP_BTC_USDT,
        SPOT_BTC_USDT,
        ExecHarness,
        _assert_denied,
        _submit,
    )

RECENT_SECS = int(time.time()) - 60

#: Every `finish_as` value this adapter names, minus the sentinel.
DOCUMENTED_REASONS = [reason.value for reason in GateioFinishAs if reason.value != "_unknown"]


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


def _rejections(env: ExecHarness) -> list[Any]:
    return env.events_of(OrderRejected)


# -- the terminal status follows the quantities, not the reason ---------------


class TestTerminalStatusFollowsTheQuantities:
    """`finish_as` explains why an order stopped; it cannot say that it finished.

    Reading the reason first produced a wrong terminal state in both directions:
    a completed order reported `expired`, and an untouched order reported
    `filled` because a liquidation cancelled it.
    """

    @pytest.mark.parametrize("reason", DOCUMENTED_REASONS)
    def test_a_completed_order_is_filled_whatever_the_reason(self, reason):
        assert order_status_from_gateio("finished", reason, filled=10.0, amount=10.0) is (
            OrderStatus.FILLED
        )

    @pytest.mark.parametrize("reason", ["liquidated", "auto_deleveraged"])
    def test_an_untouched_forced_closure_is_a_cancellation(self, reason):
        """Gate.io's own words: "cancelled because of liquidation", "finished by ADL".

        Calling these FILLED with nothing filled leaves the order open forever:
        the client emits no closing event for an order it believes filled (the
        fill stream is supposed to close it), and reconciliation finds no fill to
        infer either.
        """
        assert order_status_from_gateio("finished", reason, filled=0.0, amount=10.0) is (
            OrderStatus.CANCELED
        )

    @pytest.mark.parametrize("reason", ["liquidated", "auto_deleveraged"])
    def test_a_partially_filled_forced_closure_is_a_cancellation(self, reason):
        assert order_status_from_gateio("finished", reason, filled=4.0, amount=10.0) is (
            OrderStatus.CANCELED
        )

    def test_an_unfilled_expiry_is_still_expired(self):
        """The correction must not swing the other way: a lapsed order did expire."""
        assert order_status_from_gateio("finished", "expired", filled=0.0, amount=10.0) is (
            OrderStatus.EXPIRED
        )

    def test_a_partially_filled_expiry_is_still_expired(self):
        assert order_status_from_gateio("finished", "expired", filled=4.0, amount=10.0) is (
            OrderStatus.EXPIRED
        )


class TestACompletedOrderKeepsItsFill:
    """The cost of the wrong terminal status is a lost execution, not a wrong label."""

    @staticmethod
    def _order_payload(text: str, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": 900001,
            "id_string": "900001",
            "contract": "BTC_USDT",
            "size": -10,
            "left": 0,  # nothing left: the venue filled the order in full
            "price": "59000.0",
            "tif": "gtc",
            "status": "finished",
            "finish_as": "expired",
            "text": text,
            "create_time": RECENT_SECS,
            "update_time": RECENT_SECS + 1,
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _fill_payload(text: str) -> dict[str, Any]:
        return {
            "id": "T-COMPLETING",
            "contract": "BTC_USDT",
            "order_id": "900001",
            "size": -10,
            "price": "59000.0",
            "role": "taker",
            "fee": "0.05",
            "text": text,
            "create_time": RECENT_SECS + 2,
        }

    def test_a_completed_order_reported_expired_still_books_its_fill(self, perp_env):
        """A full fill plus `finish_as=expired` used to discard the execution.

        Gate.io does not order ``*.orders`` against ``*.usertrades``, so the
        message closing an order routinely arrives before the fill that closed
        it. Closing the order as EXPIRED is terminal with no transition back for
        a fill (installed model/orders/base.pyx:132-133 allows a late fill out of
        CANCELED only), so the trade that completed the order was written off and
        the position was left short of it.
        """
        env = perp_env
        order = env.order_factory.limit(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
        )
        env.accepted(order, "900001")
        text = f"t-{order.client_order_id.value}"
        env.client._register_text(order.client_order_id, text)

        env.client._handle_order_payload(GateioProductType.PERP, self._order_payload(text))
        env.drain(order)
        assert order.status is not OrderStatus.EXPIRED

        env.client._handle_fill_payload(GateioProductType.PERP, self._fill_payload(text))
        env.drain(order)

        fills = env.events_of(OrderFilled)
        assert len(fills) == 1
        assert fills[0].last_qty == Quantity.from_int(10)
        assert order.status is OrderStatus.FILLED
        assert order.filled_qty == Quantity.from_int(10)


# -- a spot conditional order cannot silently change its trigger reference ----


class TestSpotConditionalTriggerType:
    """Gate.io's spot trigger is `{price, rule, expiration}` — there is no price type.

    Dropping `trigger_type` does not lose a decoration: it arms the order on the
    last traded price, which is exactly the price a caller reaching for
    MARK_PRICE or MID_POINT chose the trigger type to avoid.
    """

    @pytest.mark.parametrize(
        "trigger_type",
        [
            TriggerType.MARK_PRICE,
            TriggerType.INDEX_PRICE,
            TriggerType.BID_ASK,
            TriggerType.MID_POINT,
            TriggerType.DOUBLE_LAST,
            TriggerType.DOUBLE_BID_ASK,
            TriggerType.LAST_OR_BID_ASK,
        ],
        ids=lambda t: str(t),
    )
    def test_a_trigger_type_spot_cannot_carry_is_refused(self, spot_env, trigger_type):
        spot_env.spot.responses["tickers"] = [{"last": "60000"}]
        spot_env.spot.responses["create_price_order"] = {"id": 11, "id_string": "11"}
        order = spot_env.order_factory.stop_market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            trigger_type=trigger_type,
        )
        _submit(spot_env, order)

        _assert_denied(spot_env, order, "trigger type")
        assert not spot_env.spot.called("create_price_order")

    @pytest.mark.parametrize(
        "trigger_type",
        [TriggerType.DEFAULT, TriggerType.LAST_PRICE],
        ids=lambda t: str(t),
    )
    def test_the_two_types_spot_can_carry_are_still_armed(self, spot_env, trigger_type):
        """DEFAULT and LAST_PRICE name the same price, which is the only one spot has."""
        spot_env.spot.responses["tickers"] = [{"last": "60000"}]
        spot_env.spot.responses["create_price_order"] = {"id": 11, "id_string": "11"}
        order = spot_env.order_factory.stop_market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            trigger_type=trigger_type,
        )
        _submit(spot_env, order)

        assert _rejections(spot_env) == []
        body = spot_env.spot.calls_named("create_price_order")[0].body
        assert body["trigger"] == {"price": "59000.00", "rule": "<="}

    def test_futures_still_accepts_the_types_it_can_encode(self, perp_env):
        """The spot refusal must not leak onto the product that has the field."""
        perp_env.perp.responses["tickers"] = [{"last": "60000.0"}]
        perp_env.perp.responses["create_price_order"] = {"id": 12, "id_string": "12"}
        order = perp_env.order_factory.stop_market(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(10),
            Price.from_str("59000.0"),
            trigger_type=TriggerType.MARK_PRICE,
        )
        _submit(perp_env, order)

        assert _rejections(perp_env) == []
        body = perp_env.perp.calls_named("create_price_order")[0].body
        assert body["trigger"]["price_type"] == 1


# -- one market-order time-in-force mapping for every product -----------------


class TestSpotMarketTimeInForce:
    """Spot used to take a shortcut that accepted what the other products reject."""

    @pytest.mark.parametrize(
        "tif",
        [TimeInForce.AT_THE_OPEN, TimeInForce.AT_THE_CLOSE],
        ids=lambda t: str(t),
    )
    def test_a_session_time_in_force_is_refused_on_spot_too(self, spot_env, tif):
        """Gate.io has no sessions, and the other three products already say so.

        The same instruction answered with a rejection on a perpetual and a
        silent `ioc` on spot is a trap for a strategy that switches products,
        and on a 24/7 venue a session instruction is a porting mistake worth
        reporting.
        """
        order = spot_env.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            time_in_force=tif,
        )
        _submit(spot_env, order)

        _assert_denied(spot_env, order, "time in force")
        assert not spot_env.spot.called("create_order")

    @pytest.mark.parametrize(
        "tif",
        [TimeInForce.GTC, TimeInForce.IOC, TimeInForce.DAY],
        ids=lambda t: str(t),
    )
    def test_a_resting_instruction_on_a_market_order_still_collapses_to_ioc(self, spot_env, tif):
        """An order that cannot rest has no resting duration; that is not a downgrade."""
        spot_env.spot.responses["create_order"] = {}
        order = spot_env.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            time_in_force=tif,
        )
        _submit(spot_env, order)

        assert _rejections(spot_env) == []
        body = spot_env.spot.calls_named("create_order")[0].body
        assert body["type"] == "market"
        assert body["time_in_force"] == "ioc"

    def test_a_base_denominated_fill_or_kill_buy_stays_fill_or_kill(self, spot_env):
        """The price is approximated on this path; the execution guarantee is not.

        A base-denominated spot market buy is sent as an aggressive limit because
        Gate.io's native market buy spends a quote amount. That substitution is
        about the price. Sending `ioc` regardless turned "fill in full or not at
        all" into "fill whatever is available" — the one outcome FOK exists to
        rule out — even though a spot limit order takes `fok` perfectly well.
        """
        spot_env.spot.responses["create_order"] = {}
        spot_env.spot.responses["tickers"] = [{"last": "60000"}]
        order = spot_env.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("0.010000"),
            time_in_force=TimeInForce.FOK,
        )
        _submit(spot_env, order)

        assert _rejections(spot_env) == []
        body = spot_env.spot.calls_named("create_order")[0].body
        assert body["type"] == "limit"
        assert body["time_in_force"] == "fok"
        assert body["amount"] == "0.010000"


# -- a conditional order keeps its own order type -----------------------------


class TestTheTriggerRuleKeepsTheOrderType:
    """Gate.io takes a comparison rule, and only the one the market implies.

    A stop is placed away from the market in the direction of the trade; an
    if-touched order towards it (concepts/orders/stop_market.md:5-7,
    market_if_touched.md:13-14). When the trigger price contradicts that, the
    only rule Gate.io accepts encodes the *other* order type, so the order is
    refused instead of being armed as something else.
    """

    @staticmethod
    def _arm(env: ExecHarness, factory_name: str, side: OrderSide, trigger: str) -> Any:
        env.perp.responses["tickers"] = [{"last": "60000.0"}]
        env.perp.responses["create_price_order"] = {"id": 77, "id_string": "77"}
        order = getattr(env.order_factory, factory_name)(
            PERP_BTC_USDT,
            side,
            Quantity.from_int(10),
            Price.from_str(trigger),
        )
        _submit(env, order)
        return order

    def test_a_stop_buy_below_the_market_is_not_turned_into_a_dip_buy(self, perp_env):
        """A breakout entry silently became its opposite: buy when price *falls*."""
        order = self._arm(perp_env, "stop_market", OrderSide.BUY, "59000.0")

        _assert_denied(perp_env, order, "STOP_MARKET")
        assert not perp_env.perp.called("create_price_order")

    def test_an_if_touched_buy_above_the_market_is_not_turned_into_a_breakout(self, perp_env):
        order = self._arm(perp_env, "market_if_touched", OrderSide.BUY, "61000.0")

        _assert_denied(perp_env, order, "MARKET_IF_TOUCHED")
        assert not perp_env.perp.called("create_price_order")

    def test_a_breached_sell_stop_is_refused_rather_than_inverted(self, perp_env):
        """The market has already run through the stop; the venue cannot arm it."""
        order = self._arm(perp_env, "stop_market", OrderSide.SELL, "61000.0")

        _assert_denied(perp_env, order, "")
        assert not perp_env.perp.called("create_price_order")

    @pytest.mark.parametrize(
        ("factory_name", "side", "trigger", "rule"),
        [
            ("stop_market", OrderSide.BUY, "61000.0", 1),
            ("stop_market", OrderSide.SELL, "59000.0", 2),
            ("market_if_touched", OrderSide.BUY, "59000.0", 2),
            ("market_if_touched", OrderSide.SELL, "61000.0", 1),
        ],
    )
    def test_a_well_formed_conditional_order_still_arms(
        self,
        perp_env,
        factory_name,
        side,
        trigger,
        rule,
    ):
        self._arm(perp_env, factory_name, side, trigger)

        assert _rejections(perp_env) == []
        body = perp_env.perp.calls_named("create_price_order")[0].body
        assert body["trigger"]["rule"] == rule

    @pytest.mark.parametrize(
        ("factory_name", "side", "rule"),
        [
            ("stop_market", OrderSide.BUY, 1),
            ("stop_market", OrderSide.SELL, 2),
            ("market_if_touched", OrderSide.BUY, 2),
            ("market_if_touched", OrderSide.SELL, 1),
        ],
    )
    def test_without_a_last_price_the_order_type_decides(
        self,
        perp_env,
        factory_name,
        side,
        rule,
    ):
        """Nothing can contradict the order type, so it stands and the venue rules."""
        perp_env.perp.responses["create_price_order"] = {"id": 78, "id_string": "78"}
        order = getattr(perp_env.order_factory, factory_name)(
            PERP_BTC_USDT,
            side,
            Quantity.from_int(10),
            Price.from_str("60000.0"),
        )
        _submit(perp_env, order)

        assert _rejections(perp_env) == []
        body = perp_env.perp.calls_named("create_price_order")[0].body
        assert body["trigger"]["rule"] == rule
        assert order is not None
