"""Pure parsing helpers for Gate.io API v4 payloads.

Every function takes decoded JSON (dicts/lists) and returns plain Python
structures. No network access, no Nautilus objects — this keeps the parsers
trivially unit-testable and reusable by both the HTTP client and the live
clients.
"""

from __future__ import annotations

from typing import Any


def parse_currency_pair(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one entry of ``GET /spot/currency_pairs``."""
    return {
        "pair": raw.get("id"),
        "base": raw.get("base"),
        "quote": raw.get("quote"),
        "amount_precision": int(raw.get("amount_precision", 8)),
        "price_precision": int(raw.get("precision", 8)),
        "min_base_amount": float(raw.get("min_base_amount", 0) or 0),
        "min_quote_amount": float(raw.get("min_quote_amount", 0) or 0),  # ~ min notional
        "fee": float(raw.get("fee", 0) or 0),
        "trade_status": raw.get("trade_status"),
    }


def parse_candle(raw: list[Any]) -> dict[str, Any]:
    """Parse one ``/spot/candlesticks`` row.

    Gate.io returns ``[ts, quote_volume, close, high, low, open, base_volume, ...]``
    with the timestamp in seconds.
    """
    return {
        "ts": int(float(raw[0])) * 1000,  # seconds -> ms
        "open": float(raw[5]),
        "high": float(raw[3]),
        "low": float(raw[4]),
        "close": float(raw[2]),
        "volume": float(raw[6]),  # base-currency volume
    }


def parse_public_trade(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one entry of ``GET /spot/trades``."""
    return {
        "id": raw.get("id"),
        "ts": int(float(raw.get("create_time_ms", 0) or 0)),
        "price": float(raw.get("price", 0) or 0),
        "amount": float(raw.get("amount", 0) or 0),
        "side": raw.get("side"),
    }


def parse_order_book(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse ``GET /spot/order_book`` into float bid/ask ladders."""
    return {
        "bids": [[float(p), float(a)] for p, a in raw.get("bids", [])],
        "asks": [[float(p), float(a)] for p, a in raw.get("asks", [])],
        "ts": raw.get("current"),
    }


def parse_order(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse a spot order payload (``/spot/orders`` family) into a flat model.

    ``partial`` is true while the order is open with a non-zero, non-complete
    fill. ``finish_as`` preserves the exchange's terminal reason (``filled``,
    ``cancelled``, ``ioc``, ...).
    """
    filled = float(raw.get("filled_total", 0) or 0)
    amount = float(raw.get("amount", 0) or 0)
    status = raw.get("status", "")
    return {
        "id": raw.get("id"),
        "client_id": raw.get("text"),
        "pair": raw.get("currency_pair"),
        "side": raw.get("side"),
        "type": raw.get("type"),
        "price": float(raw.get("price", 0) or 0),
        "avg_price": float(raw.get("avg_deal_price", 0) or 0),
        "amount": amount,
        "filled": filled,
        "left": float(raw.get("left", 0) or 0),
        "status": status,  # open | closed | cancelled
        "partial": status == "open" and 0 < filled < amount,
        "fee": float(raw.get("fee", 0) or 0),
        "fee_currency": raw.get("fee_currency"),
        "create_time_ms": int(raw.get("create_time_ms", 0) or 0),
        "finish_as": raw.get("finish_as"),
        "time_in_force": raw.get("time_in_force"),
    }


def parse_fill(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one entry of ``GET /spot/my_trades``."""
    return {
        "id": raw.get("id"),
        "order_id": raw.get("order_id"),
        "pair": raw.get("currency_pair"),
        "side": raw.get("side"),
        "price": float(raw.get("price", 0) or 0),
        "amount": float(raw.get("amount", 0) or 0),
        "fee": float(raw.get("fee", 0) or 0),
        "fee_currency": raw.get("fee_currency"),
        "role": raw.get("role"),  # maker | taker
        "create_time_ms": int(raw.get("create_time_ms", 0) or 0),
    }


def parse_balances(raw: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Parse ``GET /spot/accounts`` into ``{currency: {available, locked}}``."""
    return {
        b["currency"]: {
            "available": float(b.get("available", 0) or 0),
            "locked": float(b.get("locked", 0) or 0),
        }
        for b in raw
    }


def parse_futures_contract(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one entry of ``GET /futures/usdt/contracts``."""
    return {
        "name": raw.get("name"),
        "type": raw.get("type"),
        "quanto_multiplier": float(raw.get("quanto_multiplier", 0) or 0),
        "tick_size": raw.get("order_price_round"),
        "order_size_min": raw.get("order_size_min"),
        "leverage_min": raw.get("leverage_min"),
        "leverage_max": raw.get("leverage_max"),
        "maker_fee": float(raw.get("maker_fee_rate", 0) or 0),
        "taker_fee": float(raw.get("taker_fee_rate", 0) or 0),
        "mark_price": float(raw.get("mark_price", 0) or 0),
        "index_price": float(raw.get("index_price", 0) or 0),
        "funding_rate": float(raw.get("funding_rate", 0) or 0),
        "funding_interval": raw.get("funding_interval"),
        "maintenance_rate": raw.get("maintenance_rate"),
        "in_delisting": raw.get("in_delisting"),
    }


def validate_order(
    spec: dict[str, Any],
    amount: float,
    price: float | None,
) -> tuple[float, float | None]:
    """Validate and normalize an order against instrument constraints.

    Rounds ``amount``/``price`` to the instrument precision, then checks
    minimum base amount, minimum notional (only when a price is available)
    and tradability. Returns the normalized ``(amount, price)`` or raises
    :class:`~nautilus_gateio.errors.OrderValidationError`.
    """
    from nautilus_gateio.errors import OrderValidationError

    errors: list[str] = []
    amt = round(float(amount), int(spec["amount_precision"]))
    px = round(float(price), int(spec["price_precision"])) if price is not None else None
    min_base = float(spec.get("min_base_amount", 0) or 0)
    min_quote = float(spec.get("min_quote_amount", 0) or 0)
    if min_base > 0 and amt < min_base:
        errors.append(f"amount {amt} < min_base_amount {min_base}")
    if px is not None and min_quote > 0 and amt * px < min_quote:
        errors.append(f"notional {amt * px:.4f} < min_notional {min_quote}")
    trade_status = spec.get("trade_status")
    if trade_status and trade_status != "tradable":
        errors.append(f"pair is not tradable (trade_status={trade_status})")
    if errors:
        raise OrderValidationError("; ".join(errors))
    return amt, px
