"""Shared test fixtures. All data is synthetic — no network, no credentials."""

from __future__ import annotations

from typing import Any

import pytest


class FakeMarket:
    """Synthetic market-data source for PaperExecution and validation tests.

    Mimics the subset of ``GateioHttpClient`` the simulator needs:
    ``order_book`` / ``candles`` / ``currency_pair``. Depth levels and
    precision are configurable per test.
    """

    def __init__(
        self,
        mid: float = 100.0,
        depth: list[tuple[float, ...]] | None = None,
        precision: tuple[int, int] = (2, 4),
        mins: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self.mid = mid
        self.depth = depth or [(0.5,), (1.0,), (5.0,)]  # quantity per level
        self.price_precision, self.amount_precision = precision
        self.min_quote, self.min_base = mins

    def set_mid(self, mid: float) -> None:
        self.mid = mid

    def order_book(self, pair: str, limit: int = 20) -> dict[str, Any]:
        asks = [[self.mid * (1 + 0.001 * (i + 1)), q[0]] for i, q in enumerate(self.depth)]
        bids = [[self.mid * (1 - 0.001 * (i + 1)), q[0]] for i, q in enumerate(self.depth)]
        return {"asks": asks, "bids": bids}

    def candles(self, pair: str, interval: str = "1m", limit: int = 1) -> list[dict[str, Any]]:
        return [{"close": self.mid}]

    def currency_pair(self, pair: str) -> dict[str, Any]:
        return {
            "pair": pair,
            "price_precision": self.price_precision,
            "amount_precision": self.amount_precision,
            "min_quote_amount": self.min_quote,
            "min_base_amount": self.min_base,
            "trade_status": "tradable",
        }


@pytest.fixture
def fake_market() -> FakeMarket:
    return FakeMarket()


@pytest.fixture
def btc_spec() -> dict[str, Any]:
    """A realistic BTC_USDT spec (synthetic values, shaped like the real API)."""
    return {
        "pair": "BTC_USDT",
        "base": "BTC",
        "quote": "USDT",
        "amount_precision": 6,
        "price_precision": 2,
        "min_base_amount": 0.00001,
        "min_quote_amount": 3.0,
        "fee": 0.002,
        "trade_status": "tradable",
    }
