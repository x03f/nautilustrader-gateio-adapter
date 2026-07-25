"""Unit tests for :class:`nautilus_gateio.paper.PaperExecution`.

The simulator is driven by the ``FakeMarket`` fixture from ``conftest.py`` —
a synthetic order book / candle source. No network access, no credentials.
"""

from __future__ import annotations

import pytest

from nautilus_gateio.paper import PaperExecution
from tests.conftest import FakeMarket


def test_market_buy_walks_two_levels_with_fees_and_balances():
    fake = FakeMarket(mid=100.0)  # asks: 100.1 x0.5, 100.2 x1.0, 100.3 x5.0
    paper = PaperExecution(starting_balances={"USDT": 10_000.0}, data=fake)

    result = paper.submit_market("BTC_USDT", "buy", 1.0)

    assert result["status"] == "closed"
    assert result["filled"] == pytest.approx(1.0)
    assert len(result["fills"]) == 2

    p1 = round(100.0 * 1.001, 2)
    p2 = round(100.0 * 1.002, 2)
    cost = 0.5 * p1 + 0.5 * p2
    assert result["avg_price"] == pytest.approx(cost / 1.0)

    # Taker fee is charged per fill on that fill's notional.
    for fill, (price, qty) in zip(result["fills"], [(p1, 0.5), (p2, 0.5)], strict=True):
        assert fill["price"] == pytest.approx(price)
        assert fill["amount"] == pytest.approx(qty)
        assert fill["fee"] == pytest.approx(qty * price * paper.taker_fee)
        assert fill["role"] == "taker"

    fees = cost * paper.taker_fee
    assert paper.balances["USDT"] == pytest.approx(10_000.0 - cost - fees)
    assert paper.balances["BTC"] == pytest.approx(1.0)


def test_min_notional_rejection():
    fake = FakeMarket(mid=100.0, mins=(3.0, 0.0))  # min notional 3 USDT
    paper = PaperExecution(starting_balances={"USDT": 1_000.0}, data=fake)

    result = paper.submit_market("BTC_USDT", "buy", 0.01)  # notional ~1 USDT

    assert result["status"] == "rejected"
    assert "notional" in result["reason"]
    assert result["fills"] == []
    assert paper.balances["USDT"] == pytest.approx(1_000.0)  # untouched
    assert "BTC" not in paper.balances


def test_partial_fill_when_amount_exceeds_book_depth():
    fake = FakeMarket(mid=100.0)  # total ask depth = 0.5 + 1.0 + 5.0 = 6.5
    paper = PaperExecution(starting_balances={"USDT": 10_000.0}, data=fake)

    result = paper.submit_market("BTC_USDT", "buy", 10.0)

    assert result["status"] == "partial"
    assert result["filled"] == pytest.approx(6.5)
    assert result["filled"] < 10.0
    assert paper.balances["BTC"] == pytest.approx(6.5)


def test_price_and_amount_precision_rounding():
    fake = FakeMarket(mid=123.4567, precision=(2, 4))
    paper = PaperExecution(starting_balances={"USDT": 10_000.0}, data=fake)

    result = paper.submit_market("BTC_USDT", "buy", 0.123456)

    order = paper.orders[result["order_id"]]
    assert order["amount"] == pytest.approx(0.1235)  # rounded to 4 decimals
    fill = result["fills"][0]
    assert fill["price"] == round(123.4567 * 1.001, 2)  # rounded to 2 decimals


def test_market_sell_increases_usdt_minus_fee():
    fake = FakeMarket(mid=100.0)  # bids: 99.9 x0.5, 99.8 x1.0, 99.7 x5.0
    paper = PaperExecution(starting_balances={"USDT": 1_000.0, "BTC": 2.0}, data=fake)

    result = paper.submit_market("BTC_USDT", "sell", 1.0)

    assert result["status"] == "closed"
    p1 = round(100.0 * 0.999, 2)
    p2 = round(100.0 * 0.998, 2)
    proceeds = 0.5 * p1 + 0.5 * p2
    fees = proceeds * paper.taker_fee
    assert paper.balances["USDT"] == pytest.approx(1_000.0 + proceeds - fees)
    assert paper.balances["BTC"] == pytest.approx(1.0)


def test_snapshot_reports_simulation_and_equity():
    fake = FakeMarket(mid=100.0)
    paper = PaperExecution(starting_balances={"USDT": 10_000.0}, data=fake)
    paper.submit_market("BTC_USDT", "buy", 1.0)

    snap = paper.snapshot()

    assert snap["simulation"] is True
    assert snap["orders"] == 1
    assert snap["fills"] == 2
    expected_equity = paper.balances["USDT"] + paper.balances["BTC"] * fake.mid
    assert snap["equity_usdt"] == pytest.approx(round(expected_equity, 2))


def test_equity_usdt_marks_non_usdt_balances_at_candle_close():
    fake = FakeMarket(mid=100.0)
    paper = PaperExecution(starting_balances={"USDT": 50.0, "BTC": 2.0}, data=fake)

    assert paper.equity_usdt() == pytest.approx(50.0 + 2.0 * 100.0)

    fake.set_mid(150.0)
    assert paper.equity_usdt() == pytest.approx(50.0 + 2.0 * 150.0)
