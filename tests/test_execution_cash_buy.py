"""A Gate.io spot cash buy is closed on the venue's own base total.

Gate.io denominates a spot MARKET BUY in the quote currency: ``amount`` is cash
to spend, ``left`` is cash unspent, ``filled_total`` is cash spent, and
``filled_amount`` is the base quantity bought. NautilusTrader has no unit
awareness anywhere in the order lifecycle — ``Order._filled``
(model/orders/base.pyx:1176-1180) is a raw ``filled_qty + last_qty < quantity``
— so the order needs a base-denominated quantity before its first fill, and the
venue states that quantity exactly once, when the order finishes.

Everything here is a regression for the mainnet finding: the client used to
divide the cash by the *first* fill's price and restate the order to the
result. Against a venue that publishes its own arithmetic that estimate is
wrong in both directions, and both are money-state defects:

* one increment high and the order can never reach a terminal status — it stays
  in ``cache.orders_open()`` for the life of the process while the venue has
  finished it, and neither reconciliation nor a cancel can close it;
* one increment low and the engine's overfill check discards the venue's fill
  outright, so an execution that happened is not booked.

The tests parametrise the fill price on purpose. The suite's original case used
600 USDT at exactly 60000.00, the one arrangement where the estimate and the
venue agree, which is why it stayed green while the venue did not.
"""

from __future__ import annotations

from typing import Any

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.events import OrderCanceled, OrderFilled, OrderUpdated
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity

from gateio_nt.common.enums import GateioProductType
from gateio_nt.execution import is_cash_buy_payload
from gateio_nt.instruments import parse_spot_instrument
from tests.test_execution_orders import SPOT_BTC_USDT, ExecHarness, build_instruments

# A cheap pair, where the base number is the larger of the two denominations.
# The units mix this closes only shows up on one side of that comparison.
TRX_PAIR_PAYLOAD: dict[str, Any] = {
    "id": "TRX_USDT",
    "base": "TRX",
    "quote": "USDT",
    "fee": "0.2",
    "min_base_amount": "1",
    "min_quote_amount": "3",
    "amount_precision": 4,
    "precision": 5,
    "trade_status": "tradable",
    "slippage": "0.05",
}
SPOT_TRX_USDT = InstrumentId.from_str("TRX_USDT.GATE_IO")


@pytest.fixture()
def env():
    harness = ExecHarness(
        instruments=[*build_instruments(), parse_spot_instrument(TRX_PAIR_PAYLOAD)]
    )
    yield harness
    harness.close()


def _cash_buy(env: ExecHarness, instrument_id: InstrumentId, cash: str) -> Any:
    """Submit and accept a quote-denominated spot market buy."""
    order = env.order_factory.market(
        instrument_id,
        OrderSide.BUY,
        Quantity.from_str(cash),
        quote_quantity=True,
    )
    env.accepted(order, "778899")
    env.client._register_text(order.client_order_id, f"t-{order.client_order_id.value}")
    return order


def _fill(order: Any, trade_id: str, *, pair: str, amount: str, price: str, fee: str) -> dict:
    """One ``spot.usertrades`` row."""
    return {
        "id": trade_id,
        "currency_pair": pair,
        "order_id": "778899",
        "side": "buy",
        "amount": amount,
        "price": price,
        "fee": fee,
        "fee_currency": pair.split("_")[0],
        "role": "taker",
        "create_time_ms": "1785000000000",
        "text": f"t-{order.client_order_id.value}",
    }


def _finished(
    order: Any,
    *,
    pair: str,
    cash: str,
    filled_base: str,
    filled_total: str,
    finish_as: str = "filled",
) -> dict:
    """One terminal ``spot.orders`` frame, in the shape the venue publishes it."""
    return {
        "id": "778899",
        "id_string": "778899",
        "currency_pair": pair,
        "type": "market",
        "side": "buy",
        "account": "spot",
        "event": "finish",
        "amount": cash,
        "filled_amount": filled_base,
        "filled_total": filled_total,
        "left": str(round(float(cash) - float(filled_total), 8)),
        "finish_as": finish_as,
        "text": f"t-{order.client_order_id.value}",
        "update_time_ms": "1785000000001",
    }


class TestTheInFlightQuantityIsABound:
    """While the order works, its quantity is built from the venue's own fills."""

    @pytest.mark.parametrize(
        ("price", "credited"),
        [
            ("60000.00", "0.010000"),  # the original fixture: an exact divisor
            ("60321.70", "0.009946"),  # the estimate used to land one tick high
            ("59873.10", "0.010021"),
            ("61111.11", "0.009818"),
            ("58999.99", "0.010169"),
        ],
    )
    def test_the_bound_sits_one_increment_above_the_venues_credit(self, env, price, credited):
        """No price divides the cash for this: the fill amount is the only input.

        The predecessor's ``cash / first_fill_price`` is what made this
        price-sensitive. Nothing here reads the price at all, so every case
        lands the same way — which is the point of parametrising it.
        """
        order = _cash_buy(env, SPOT_BTC_USDT, "600.00")
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _fill(order, "T-1", pair="BTC_USDT", amount=credited, price=price, fee="0.000010"),
        )
        env.drain(order)

        assert order.filled_qty == Quantity.from_str(credited)
        assert order.quantity == Quantity.from_str(credited) + Quantity.from_str("0.000001")
        assert order.leaves_qty == Quantity.from_str("0.000001")
        # Still working: the venue has not said it is finished.
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert [event.is_quote_quantity for event in env.events_of(OrderUpdated)] == [False]

    def test_a_sweep_across_two_prices_never_discards_a_fill(self, env):
        """2 USDT at 0.32 then 2 USDT at 0.40 buys 11.25 TRX, not 12.5.

        Dividing the whole cash by the first price assumes every later match is
        at that price. For a BUY every later match is *worse*, so the estimate
        is above the venue's total by construction — and the engine, handed a
        restatement below what it has already booked, discards the fill instead
        (``allow_overfills`` defaults False, execution/config.py:98).
        """
        order = _cash_buy(env, SPOT_TRX_USDT, "4.0000")
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _fill(order, "T-1", pair="TRX_USDT", amount="6.2500", price="0.32000", fee="0.0125"),
        )
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _fill(order, "T-2", pair="TRX_USDT", amount="5.0000", price="0.40000", fee="0.0100"),
        )
        env.drain(order)

        # Both fills were booked. The old estimate of 12.5000 TRX was a number
        # the venue never stated for an order that bought 11.2500.
        assert [event.last_qty for event in env.events_of(OrderFilled)] == [
            Quantity.from_str("6.2500"),
            Quantity.from_str("5.0000"),
        ]
        assert order.filled_qty == Quantity.from_str("11.2500")
        assert order.quantity == Quantity.from_str("11.2501")


class TestTheVenuesFinishFrameClosesTheOrder:
    """The terminal frame is where Gate.io states the base total, so it closes."""

    @pytest.mark.parametrize("price", ["0.32690", "0.31000", "0.34117"])
    def test_a_fully_spent_cash_buy_closes_on_the_venues_base_total(self, env, price):
        order = _cash_buy(env, SPOT_TRX_USDT, "4.0000")
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _fill(order, "T-1", pair="TRX_USDT", amount="12.2361", price=price, fee="0.0245"),
        )
        env.drain(order)
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _finished(
                order,
                pair="TRX_USDT",
                cash="4",
                filled_base="12.2361",
                filled_total="4",
            ),
        )
        env.drain(order)

        assert order.is_closed
        assert order.status == OrderStatus.CANCELED
        assert order.filled_qty == Quantity.from_str("12.2361")
        # The bound is squared against the venue's own figure, so the order
        # records no remainder Gate.io never had.
        assert order.quantity == Quantity.from_str("12.2361")
        assert order.leaves_qty == Quantity.from_str("0.0000")
        assert len(env.events_of(OrderCanceled)) == 1

    def test_a_partly_spent_cash_buy_closes_on_what_it_actually_bought(self, env):
        """Gate.io cancels the unspent remainder of a market buy (``finish_as=ioc``).

        Half the cash bought 6.1180 TRX. On a cheap pair the base number is the
        larger of the two denominations, so comparing it against the quote
        ``amount`` used to read as a completed order — the same order on an
        expensive pair read as canceled. The answer must not depend on the
        price of the pair.
        """
        order = _cash_buy(env, SPOT_TRX_USDT, "4.0000")
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _fill(order, "T-1", pair="TRX_USDT", amount="6.1180", price="0.32690", fee="0.0122"),
        )
        env.drain(order)
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _finished(
                order,
                pair="TRX_USDT",
                cash="4",
                filled_base="6.1180",
                filled_total="2",
                finish_as="ioc",
            ),
        )
        env.drain(order)

        assert order.is_closed
        assert order.status == OrderStatus.CANCELED
        assert order.filled_qty == Quantity.from_str("6.1180")
        assert order.quantity == Quantity.from_str("6.1180")

    def test_an_expensive_pair_closes_the_same_way(self, env):
        """The mirror of the case above, where the base number is the smaller one."""
        order = _cash_buy(env, SPOT_BTC_USDT, "600.00")
        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _fill(
                order, "T-1", pair="BTC_USDT", amount="0.005000", price="60000.00", fee="0.000005"
            ),
        )
        env.drain(order)
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _finished(
                order,
                pair="BTC_USDT",
                cash="600",
                filled_base="0.005000",
                filled_total="300",
                finish_as="ioc",
            ),
        )
        env.drain(order)

        assert order.status == OrderStatus.CANCELED
        assert order.filled_qty == Quantity.from_str("0.005000")
        assert order.quantity == Quantity.from_str("0.005000")

    def test_a_cash_buy_that_bought_nothing_is_closed_without_a_restatement(self, env):
        """Nothing filled, so there is no base figure to restate to."""
        order = _cash_buy(env, SPOT_TRX_USDT, "4.0000")
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _finished(
                order,
                pair="TRX_USDT",
                cash="4",
                filled_base="0",
                filled_total="0",
                finish_as="ioc",
            ),
        )
        env.drain(order)

        assert order.status == OrderStatus.CANCELED
        assert order.filled_qty == Quantity.from_str("0.0000")
        assert env.events_of(OrderUpdated) == []

    def test_the_close_survives_the_venue_finishing_before_its_own_fill(self, env):
        """Gate.io does not order ``spot.orders`` against ``spot.usertrades``.

        The terminal frame can win the race against the fill that caused it.
        The order closes on the venue's base total, and the fill that arrives
        afterwards is routed through reconciliation by ``_handle_late_fill`` —
        the platform holds ``(CANCELED, PARTIALLY_FILLED)`` and
        ``(CANCELED, FILLED)`` for exactly this (base.pyx:132-133).
        """
        order = _cash_buy(env, SPOT_TRX_USDT, "4.0000")
        env.client._handle_order_payload(
            GateioProductType.SPOT,
            _finished(
                order,
                pair="TRX_USDT",
                cash="4",
                filled_base="12.2361",
                filled_total="4",
            ),
        )
        env.drain(order)
        assert order.status == OrderStatus.CANCELED
        assert order.quantity == Quantity.from_str("12.2361")

        env.client._handle_fill_payload(
            GateioProductType.SPOT,
            _fill(order, "T-1", pair="TRX_USDT", amount="12.2361", price="0.32690", fee="0.0245"),
        )

        # The execution is not dropped: it goes to reconciliation, which is the
        # one route that can apply a fill to a closed order.
        assert [report.trade_id.value for report in env.reports] == ["T-1"]


class TestTheUnitsAreNeverMixed:
    """``filled_amount`` is base and ``amount`` is quote; only one can be compared."""

    def test_the_status_of_a_cash_buy_is_decided_in_quote_units(self, env):
        payload = _finished(
            _cash_buy(env, SPOT_TRX_USDT, "4.0000"),
            pair="TRX_USDT",
            cash="4",
            filled_base="6.1180",  # base: larger than the cash on a cheap pair
            filled_total="2",  # quote: half the cash actually spent
            finish_as="ioc",
        )
        assert env.client._order_status(GateioProductType.SPOT, payload) == OrderStatus.CANCELED

    def test_a_cash_buy_that_spent_all_its_cash_reads_as_filled(self, env):
        payload = _finished(
            _cash_buy(env, SPOT_TRX_USDT, "4.0000"),
            pair="TRX_USDT",
            cash="4",
            filled_base="12.2361",
            filled_total="4",
        )
        assert env.client._order_status(GateioProductType.SPOT, payload) == OrderStatus.FILLED

    def test_the_predicate_names_only_the_quote_denominated_buy(self):
        """A base-denominated spot market buy never reaches the venue as one.

        This client submits it as an aggressive limit order instead, so
        ``type=market`` with ``side=buy`` identifies the cash buy exactly.
        """
        assert is_cash_buy_payload({"type": "market", "side": "buy"})
        assert not is_cash_buy_payload({"type": "market", "side": "sell"})
        assert not is_cash_buy_payload({"type": "limit", "side": "buy"})
        assert not is_cash_buy_payload({})
