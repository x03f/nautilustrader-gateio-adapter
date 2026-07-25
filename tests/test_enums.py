"""Tests for the Gate.io venue enums and their mapping onto NautilusTrader enums.

Covers the order status machine (``status`` plus the terminal ``finish_as``
reason and the filled/total amounts), time in force, order side, order type and
liquidity side. Values follow ``scratchpad/spec/websocket.md``: futures and
options report ``status in {open, finished}`` plus ``finish_as``, spot reports
``open|closed|cancelled`` plus ``finish_as``.
"""

from __future__ import annotations

import pytest
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)

from nautilus_gateio.common.enums import (
    GATEIO_ALL_PRODUCTS,
    GateioFinishAs,
    GateioOrderStatus,
    GateioProductType,
    GateioSpotAccountMode,
    GateioTimeInForce,
    GateioTriggerRule,
    liquidity_side_from_gateio,
    order_side_from_gateio,
    order_side_to_gateio,
    order_status_from_gateio,
    order_type_from_gateio,
    time_in_force_from_gateio,
    time_in_force_to_gateio,
)


class TestProductType:
    def test_all_products_are_enumerated(self):
        assert set(GATEIO_ALL_PRODUCTS) == set(GateioProductType)
        assert len(GATEIO_ALL_PRODUCTS) == 5

    @pytest.mark.parametrize(
        ("product", "spot", "futures", "perpetual", "delivery", "option", "inverse"),
        [
            (GateioProductType.SPOT, True, False, False, False, False, False),
            (GateioProductType.PERP, False, True, True, False, False, False),
            (GateioProductType.INVERSE, False, True, True, False, False, True),
            (GateioProductType.FUT, False, True, False, True, False, False),
            (GateioProductType.OPT, False, False, False, False, True, False),
        ],
    )
    def test_predicates(self, product, spot, futures, perpetual, delivery, option, inverse):
        assert product.is_spot is spot
        assert product.is_futures is futures
        assert product.is_perpetual is perpetual
        assert product.is_delivery is delivery
        assert product.is_option is option
        assert product.is_inverse is inverse

    @pytest.mark.parametrize(
        ("product", "settle"),
        [
            (GateioProductType.PERP, "usdt"),
            (GateioProductType.INVERSE, "btc"),
            (GateioProductType.FUT, "usdt"),
            (GateioProductType.OPT, "usdt"),
        ],
    )
    def test_settle_path_parameter(self, product, settle):
        assert product.settle == settle

    def test_exactly_one_product_is_inverse(self):
        assert [p for p in GateioProductType if p.is_inverse] == [GateioProductType.INVERSE]


class TestSpotAccountMode:
    @pytest.mark.parametrize(
        ("mode", "wire", "is_margin"),
        [
            (GateioSpotAccountMode.SPOT, "spot", False),
            (GateioSpotAccountMode.MARGIN, "margin", True),
            (GateioSpotAccountMode.CROSS_MARGIN, "cross_margin", True),
            (GateioSpotAccountMode.UNIFIED, "unified", True),
        ],
    )
    def test_wire_value_and_margin_flag(self, mode, wire, is_margin):
        assert mode.value == wire
        assert mode.is_margin is is_margin


class TestOrderSide:
    @pytest.mark.parametrize(
        ("side", "wire"),
        [(OrderSide.BUY, "buy"), (OrderSide.SELL, "sell")],
    )
    def test_to_gateio(self, side, wire):
        assert order_side_to_gateio(side) == wire

    @pytest.mark.parametrize(
        ("wire", "side"),
        [
            ("buy", OrderSide.BUY),
            ("BUY", OrderSide.BUY),
            ("sell", OrderSide.SELL),
            ("SELL", OrderSide.SELL),
        ],
    )
    def test_from_gateio(self, wire, side):
        assert order_side_from_gateio(wire) == side

    @pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
    def test_round_trip(self, side):
        assert order_side_from_gateio(order_side_to_gateio(side)) == side


class TestTimeInForce:
    @pytest.mark.parametrize(
        ("nautilus_tif", "expected"),
        [
            (TimeInForce.GTC, GateioTimeInForce.GTC),
            (TimeInForce.IOC, GateioTimeInForce.IOC),
            (TimeInForce.FOK, GateioTimeInForce.FOK),
        ],
    )
    def test_supported_values_map_to_the_venue_vocabulary(self, nautilus_tif, expected):
        assert time_in_force_to_gateio(nautilus_tif) is expected

    @pytest.mark.parametrize("nautilus_tif", [TimeInForce.GTC, TimeInForce.IOC, TimeInForce.FOK])
    def test_post_only_takes_precedence_and_maps_to_poc(self, nautilus_tif):
        assert time_in_force_to_gateio(nautilus_tif, post_only=True) is GateioTimeInForce.POC

    @pytest.mark.parametrize(
        "nautilus_tif",
        [TimeInForce.GTD, TimeInForce.DAY, TimeInForce.AT_THE_OPEN, TimeInForce.AT_THE_CLOSE],
    )
    def test_unsupported_values_are_rejected_never_downgraded(self, nautilus_tif):
        """Regression: an unrepresentable TIF must raise, not silently become GTC."""
        with pytest.raises(ValueError, match="not supported by Gate.io"):
            time_in_force_to_gateio(nautilus_tif)

    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            ("gtc", TimeInForce.GTC),
            ("GTC", TimeInForce.GTC),
            ("ioc", TimeInForce.IOC),
            ("fok", TimeInForce.FOK),
            ("poc", TimeInForce.GTC),  # post-only is GTC plus a maker-only constraint
            (None, TimeInForce.GTC),
            ("unknown_value", TimeInForce.GTC),
        ],
    )
    def test_from_gateio(self, wire, expected):
        assert time_in_force_from_gateio(wire) == expected

    @pytest.mark.parametrize("tif", [TimeInForce.GTC, TimeInForce.IOC, TimeInForce.FOK])
    def test_round_trip_for_the_natively_supported_values(self, tif):
        assert time_in_force_from_gateio(time_in_force_to_gateio(tif).value) == tif

    def test_wire_values_match_the_venue_strings(self):
        assert {t.value for t in GateioTimeInForce} == {"gtc", "ioc", "poc", "fok"}


class TestLiquiditySide:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("maker", LiquiditySide.MAKER),
            ("MAKER", LiquiditySide.MAKER),
            ("taker", LiquiditySide.TAKER),
            ("TAKER", LiquiditySide.TAKER),
            (None, LiquiditySide.NO_LIQUIDITY_SIDE),
        ],
    )
    def test_from_gateio(self, role, expected):
        assert liquidity_side_from_gateio(role) == expected


class TestOrderType:
    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            ("market", OrderType.MARKET),
            ("MARKET", OrderType.MARKET),
            ("limit", OrderType.LIMIT),
            (None, OrderType.LIMIT),
        ],
    )
    def test_from_gateio(self, wire, expected):
        assert order_type_from_gateio(wire) == expected


class TestFinishAs:
    @pytest.mark.parametrize(
        "wire",
        [
            "filled",
            "cancelled",
            "liquidated",
            "ioc",
            "fok",
            "auto_deleveraged",
            "reduce_only",
            "position_closed",
            "reduce_out",
            "stp",
            "expired",
        ],
    )
    def test_documented_reasons_parse(self, wire):
        assert GateioFinishAs.parse(wire).value == wire

    @pytest.mark.parametrize("wire", [None, "", "something_new_from_the_venue"])
    def test_unknown_reason_degrades_to_unknown(self, wire):
        assert GateioFinishAs.parse(wire) is GateioFinishAs.UNKNOWN

    def test_venue_status_values(self):
        assert {s.value for s in GateioOrderStatus} == {"open", "closed", "cancelled", "finished"}


class TestOrderStatusMapping:
    def test_open_with_nothing_filled_is_accepted(self):
        assert (
            order_status_from_gateio("open", None, filled=0.0, amount=10.0) is OrderStatus.ACCEPTED
        )

    def test_open_with_a_partial_fill_is_partially_filled(self):
        status = order_status_from_gateio("open", None, filled=3.0, amount=10.0)
        assert status is OrderStatus.PARTIALLY_FILLED

    @pytest.mark.parametrize("state", ["open", "OPEN"])
    def test_open_wins_over_a_stale_finish_reason(self, state):
        """A resting order is never terminal, whatever ``finish_as`` says."""
        assert order_status_from_gateio(state, "filled", filled=0.0, amount=1.0) is (
            OrderStatus.ACCEPTED
        )

    @pytest.mark.parametrize("state", ["closed", "finished", "cancelled"])
    def test_finish_as_filled_is_filled(self, state):
        assert order_status_from_gateio(state, "filled", filled=10.0, amount=10.0) is (
            OrderStatus.FILLED
        )

    @pytest.mark.parametrize("state", ["closed", "finished", "cancelled"])
    def test_finish_as_cancelled_is_canceled(self, state):
        assert order_status_from_gateio(state, "cancelled", filled=0.0, amount=10.0) is (
            OrderStatus.CANCELED
        )

    def test_finish_as_expired_is_expired(self):
        assert order_status_from_gateio("finished", "expired", filled=0.0, amount=10.0) is (
            OrderStatus.EXPIRED
        )

    @pytest.mark.parametrize("reason", ["ioc", "fok", "stp"])
    def test_unfilled_immediate_order_is_canceled(self, reason):
        assert order_status_from_gateio("finished", reason, filled=0.0, amount=10.0) is (
            OrderStatus.CANCELED
        )

    @pytest.mark.parametrize("reason", ["ioc", "fok", "stp"])
    def test_partially_filled_immediate_order_is_canceled(self, reason):
        """The remainder is gone; Nautilus tracks the fills separately."""
        assert order_status_from_gateio("finished", reason, filled=4.0, amount=10.0) is (
            OrderStatus.CANCELED
        )

    @pytest.mark.parametrize("reason", ["ioc", "fok", "stp"])
    def test_fully_filled_immediate_order_is_filled(self, reason):
        assert order_status_from_gateio("finished", reason, filled=10.0, amount=10.0) is (
            OrderStatus.FILLED
        )

    @pytest.mark.parametrize("reason", ["reduce_only", "reduce_out", "position_closed"])
    def test_reduce_style_terminations_are_canceled(self, reason):
        assert order_status_from_gateio("finished", reason, filled=0.0, amount=10.0) is (
            OrderStatus.CANCELED
        )

    @pytest.mark.parametrize("reason", ["liquidated", "auto_deleveraged"])
    def test_forced_closures_are_filled(self, reason):
        """A liquidated or auto-deleveraged order was executed by the venue."""
        assert order_status_from_gateio("finished", reason, filled=10.0, amount=10.0) is (
            OrderStatus.FILLED
        )

    def test_missing_reason_falls_back_to_the_amounts(self):
        assert order_status_from_gateio("finished", None, filled=10.0, amount=10.0) is (
            OrderStatus.FILLED
        )
        assert order_status_from_gateio("finished", None, filled=0.0, amount=10.0) is (
            OrderStatus.CANCELED
        )

    def test_unknown_reason_falls_back_to_the_amounts(self):
        assert order_status_from_gateio(
            "finished", "brand_new_reason", filled=10.0, amount=10.0
        ) is (OrderStatus.FILLED)
        assert order_status_from_gateio(
            "finished", "brand_new_reason", filled=1.0, amount=10.0
        ) is (OrderStatus.CANCELED)

    def test_overfilled_amounts_are_still_filled(self):
        assert order_status_from_gateio("finished", None, filled=10.5, amount=10.0) is (
            OrderStatus.FILLED
        )

    def test_unknown_state_without_amounts_is_accepted(self):
        assert order_status_from_gateio(None, None, filled=0.0, amount=0.0) is OrderStatus.ACCEPTED

    @pytest.mark.parametrize("reason", [r.value for r in GateioFinishAs if r.value != "_unknown"])
    def test_every_documented_reason_maps_to_a_terminal_status(self, reason):
        """No documented terminal reason may leave an order looking live."""
        status = order_status_from_gateio("finished", reason, filled=10.0, amount=10.0)
        assert status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED)


class TestTriggerRule:
    def test_wire_values(self):
        assert GateioTriggerRule.GREATER_OR_EQUAL.value == 1
        assert GateioTriggerRule.LESS_OR_EQUAL.value == 2
