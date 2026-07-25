"""Local paper-fill simulator driven by live public Gate.io data.

**This is a SIMULATION.** Orders never leave the local process — there is no
exchange sandbox involved (Gate.io provides no public spot testnet market
data). Fills are modeled against the real live order book, fees and
precision/minimum constraints come from the real instrument specs:

* market orders walk the opposite side of the live book, so insufficient
  depth produces realistic slippage and partial fills;
* taker/maker fees are applied per fill;
* minimum amount / minimum notional / precision are enforced.

Useful for exercising strategy logic against live data with zero risk, and
for tests (inject a fake data source via the ``data`` parameter).
"""

from __future__ import annotations

import time
from typing import Any

from nautilus_gateio.http import GateioHttpClient


class PaperFill:
    """One simulated fill."""

    __slots__ = (
        "order_id",
        "client_id",
        "pair",
        "side",
        "price",
        "amount",
        "fee",
        "fee_currency",
        "role",
        "ts",
        "partial",
    )

    def __init__(self, **kwargs: Any) -> None:
        for key in self.__slots__:
            setattr(self, key, kwargs.get(key))

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__slots__}


class PaperExecution:
    """Simulated spot execution with virtual balances, orders and fills."""

    SIMULATION = True

    def __init__(
        self,
        starting_balances: dict[str, float] | None = None,
        taker_fee: float = 0.0016,
        maker_fee: float = 0.00075,
        data: Any | None = None,
    ) -> None:
        # ``data`` needs .order_book(pair, limit), .candles(pair, interval, limit)
        # and .currency_pair(pair) — the real GateioHttpClient or a test fake.
        self.data = data or GateioHttpClient()
        self.balances: dict[str, float] = dict(starting_balances or {"USDT": 10_000.0})
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.orders: dict[str, dict[str, Any]] = {}
        self.fills: list[PaperFill] = []
        self._sequence = 0
        self._spec_cache: dict[str, dict[str, Any]] = {}

    # -- instrument helpers ---------------------------------------------------

    def _spec(self, pair: str) -> dict[str, Any]:
        if pair not in self._spec_cache:
            try:
                self._spec_cache[pair] = self.data.currency_pair(pair) or {}
            except Exception:
                self._spec_cache[pair] = {}
        return self._spec_cache[pair]

    def _round_price(self, pair: str, price: float) -> float:
        precision = self._spec(pair).get("price_precision")
        return round(price, int(precision)) if precision is not None else price

    def _round_amount(self, pair: str, amount: float) -> float:
        precision = self._spec(pair).get("amount_precision")
        return round(amount, int(precision)) if precision is not None else amount

    def check_min(self, pair: str, amount: float, price: float) -> tuple[bool, str]:
        """Check Gate.io minimum size and notional. Returns ``(ok, reason)``."""
        spec = self._spec(pair)
        notional = amount * price
        min_quote = float(spec.get("min_quote_amount") or 0)
        min_base = float(spec.get("min_base_amount") or 0)
        if min_quote and notional < min_quote:
            return False, f"notional {notional:.2f} < min {min_quote}"
        if min_base and amount < min_base:
            return False, f"amount {amount} < min_base {min_base}"
        return True, ""

    def _next_id(self) -> str:
        self._sequence += 1
        return f"paper-{self._sequence}"

    # -- order flow (simulated) ----------------------------------------------

    def submit_market(
        self,
        pair: str,
        side: str,
        amount: float,
        client_id: str | None = None,
        ts: int | None = None,
    ) -> dict[str, Any]:
        """Simulate a market order filled against the live order book.

        Returns a dict with ``status`` (``closed`` / ``partial`` / ``rejected``),
        the average price and the list of fills (walking the book models
        slippage; thin books produce partial fills).
        """
        ts = ts or int(time.time() * 1000)
        amount = self._round_amount(pair, amount)
        book = self.data.order_book(pair, limit=20)
        ladder = book["asks"] if side == "buy" else book["bids"]
        if not ladder:
            return {"status": "rejected", "reason": "empty order book", "fills": []}
        ok, reason = self.check_min(pair, amount, ladder[0][0])
        if not ok:
            return {"status": "rejected", "reason": reason, "fills": []}
        order_id = self._next_id()
        fills: list[PaperFill] = []
        remaining = amount
        cost = 0.0
        for price, quantity in ladder:
            take = min(remaining, quantity)
            if take <= 0:
                break
            price = self._round_price(pair, price)
            fee = take * price * self.taker_fee
            fills.append(
                PaperFill(
                    order_id=order_id,
                    client_id=client_id,
                    pair=pair,
                    side=side,
                    price=price,
                    amount=take,
                    fee=fee,
                    fee_currency="USDT",
                    role="taker",
                    ts=ts,
                    partial=(take < remaining),
                )
            )
            cost += take * price
            remaining -= take
            if remaining <= 1e-12:
                break
        filled = amount - remaining
        self._apply_fills(pair, side, fills)
        self.fills.extend(fills)
        status = "closed" if remaining <= 1e-9 else ("partial" if filled > 0 else "rejected")
        average = (cost / filled) if filled else 0.0
        self.orders[order_id] = {
            "id": order_id,
            "client_id": client_id,
            "pair": pair,
            "side": side,
            "type": "market",
            "amount": amount,
            "filled": filled,
            "avg_price": average,
            "status": status,
        }
        return {
            "status": status,
            "order_id": order_id,
            "filled": filled,
            "avg_price": average,
            "fills": [f.as_dict() for f in fills],
        }

    def _apply_fills(self, pair: str, side: str, fills: list[PaperFill]) -> None:
        base = pair.split("_")[0]
        for fill in fills:
            notional = fill.amount * fill.price
            if side == "buy":
                self.balances["USDT"] = self.balances.get("USDT", 0) - notional - fill.fee
                self.balances[base] = self.balances.get(base, 0) + fill.amount
            else:
                self.balances["USDT"] = self.balances.get("USDT", 0) + notional - fill.fee
                self.balances[base] = self.balances.get(base, 0) - fill.amount

    # -- reporting -----------------------------------------------------------

    def equity_usdt(self) -> float:
        """Mark-to-market equity in USDT at current prices."""
        equity = self.balances.get("USDT", 0.0)
        for currency, amount in self.balances.items():
            if currency == "USDT" or abs(amount) < 1e-9:
                continue
            try:
                candles = self.data.candles(f"{currency}_USDT", "1m", limit=1)
                if candles:
                    equity += amount * candles[-1]["close"]
            except Exception:
                pass
        return equity

    def snapshot(self) -> dict[str, Any]:
        return {
            "simulation": True,
            "balances": dict(self.balances),
            "orders": len(self.orders),
            "fills": len(self.fills),
            "equity_usdt": round(self.equity_usdt(), 2),
        }
