"""Spot TESTNET order round-trip: place, list, cancel.

Submits one limit buy far below the market, confirms it is resting, and cancels
it. The order is priced so that it cannot fill.

Safety gates in this script (they live here, not in the adapter):

1. **Explicit opt-in.** Refuses to run unless ``GATEIO_ALLOW_ORDERS=YES`` is set
   for this run, so credentials sitting in the environment cannot place an order
   on their own.
2. **Testnet host, hard-coded.** The base URL is the ``GATEIO_HTTP_TESTNET``
   constant. No environment variable or flag redirects this script to mainnet.
3. **Bounded notional.** The order value is capped at ``MAX_NOTIONAL_USDT`` and
   the limit price sits far below the market.
4. **Cancel in ``finally``.** The order is cancelled even if an assertion or an
   exception intervenes.

The adapter itself has no order kill switch: ``environment`` defaults to
mainnet, and the controls that bind are the API key's permissions and IP
allow-list. See docs/configuration.md.

Credentials: REQUIRED, testnet only.

    export GATE_TESTNET_API_KEY=...
    export GATE_TESTNET_API_SECRET=...
    export GATEIO_ALLOW_ORDERS=YES

Run:
    python examples/06_testnet_orders.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from nautilus_gateio import (
    GATEIO_HTTP_TESTNET,
    GateioHttpClient,
    GateioSpotHttpAPI,
    generate_client_order_id,
    resolve_credentials,
)

PAIR = "BTC_USDT"
#: Hard cap on the value of the order this script may submit.
MAX_NOTIONAL_USDT = Decimal("5")
#: The limit price is placed this far below the last trade, so it cannot fill.
PRICE_DISCOUNT = Decimal("0.5")


def _quantize(value: Decimal, decimals: int, rounding: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=rounding)


def _build_order(spec: dict[str, Any], last: Decimal) -> dict[str, Any]:
    """Size and price a resting limit buy inside the venue's constraints."""
    price_decimals = int(spec["precision"])
    amount_decimals = int(spec["amount_precision"])
    min_notional = Decimal(str(spec.get("min_quote_amount") or "1"))
    min_amount = Decimal(str(spec.get("min_base_amount") or "0"))

    if min_notional > MAX_NOTIONAL_USDT:
        raise SystemExit(
            f"{PAIR} requires a minimum notional of {min_notional} USDT, "
            f"above this script's cap of {MAX_NOTIONAL_USDT}"
        )

    price = _quantize(last * (Decimal(1) - PRICE_DISCOUNT), price_decimals, ROUND_DOWN)
    # Round the amount UP so the notional clears the venue minimum, then verify
    # the result is still inside the cap.
    amount = _quantize(min_notional / price, amount_decimals, ROUND_UP)
    amount = max(amount, _quantize(min_amount, amount_decimals, ROUND_UP))
    notional = amount * price

    if notional > MAX_NOTIONAL_USDT:
        raise SystemExit(
            f"computed notional {notional} USDT exceeds the cap of {MAX_NOTIONAL_USDT}"
        )

    print(f"order: BUY {amount} {PAIR} @ {price} (notional {notional} USDT, last {last})")
    return {
        "currency_pair": PAIR,
        "side": "buy",
        "type": "limit",
        "account": "spot",
        "amount": str(amount),
        "price": str(price),
        "time_in_force": "gtc",
        "text": generate_client_order_id("ex"),
    }


async def main() -> int:
    if os.environ.get("GATEIO_ALLOW_ORDERS") != "YES":
        print(
            "Refusing to run: set GATEIO_ALLOW_ORDERS=YES for this run to allow "
            "this script to place a testnet order.",
            file=sys.stderr,
        )
        return 1

    api_key, api_secret = resolve_credentials(None, None, testnet=True)
    if not api_key or not api_secret:
        print(
            "No testnet credentials found. Set GATE_TESTNET_API_KEY and "
            "GATE_TESTNET_API_SECRET and try again.",
            file=sys.stderr,
        )
        return 1

    # Gate 2: the host is a constant, not configuration.
    print(f"host: {GATEIO_HTTP_TESTNET} (testnet, hard-coded)")

    async with GateioHttpClient(api_key, api_secret, base_url=GATEIO_HTTP_TESTNET) as client:
        await client.sync_time()
        spot = GateioSpotHttpAPI(client)

        spec = await spot.currency_pair(PAIR)
        tickers = await spot.tickers(PAIR)
        last = Decimal(str(tickers[0]["last"]))
        body = _build_order(spec, last)

        placed = await spot.create_order(body)
        order_id = str(placed["id"])
        print(f"placed: id={order_id} status={placed.get('status')} text={placed.get('text')}")

        try:
            grouped = await spot.open_orders()
            resting = [
                order
                for group in grouped
                if group.get("currency_pair") == PAIR
                for order in group.get("orders", [])
                if str(order.get("id")) == order_id
            ]
            print(f"resting: {'yes' if resting else 'no'}")
        finally:
            # Gate 4: cancel no matter what happened above.
            cancelled = await spot.cancel_order(order_id, PAIR)
            print(f"cancelled: id={cancelled['id']} status={cancelled.get('status')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
