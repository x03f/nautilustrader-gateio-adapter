"""Spot TESTNET order round-trip with layered safety gates.

Places one deliberately unfillable limit buy (30% below the last price) on
the Gate.io spot testnet, shows it among the open orders, cancels it, and
shows the final state. Every step uses the validated order path
(``place_order_validated``), which rounds to instrument precision and checks
exchange minimums before submitting.

Why the gates exist
-------------------
Order-placing code must never run by accident — not from a copy-pasted
snippet, not from an environment that happens to contain credentials, and
never against mainnet. The presence of API keys alone must NEVER be enough to
place orders. This script therefore refuses to run unless ALL of the
following hold, and prints each check as it passes:

1. ``GATEIO_ALLOW_ORDERS=YES`` is set — an explicit, per-run human opt-in.
2. The REST host is the hard-coded testnet host (``api-testnet.gateapi.io``).
   The constant is defined in this file and is not overridable by any
   environment variable, so the script physically cannot target mainnet.
3. Testnet credentials are present (``GATE_TESTNET_API_KEY`` /
   ``GATE_TESTNET_API_SECRET``).
4. The order notional is hard-capped at 5 USDT; the computed order is
   validated against the exchange specification before submission.

These mirror the adapter's own layered safety model (``live_orders`` switch,
testnet-by-default execution config).

Credentials: REQUIRED (testnet only). Never use mainnet keys here.

Run:
    GATEIO_ALLOW_ORDERS=YES python examples/06_testnet_orders.py
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from nautilus_gateio import GateioHttpClient

# Hard-coded testnet endpoint. Deliberately NOT read from the environment or
# CLI: this script must be physically unable to reach the mainnet API.
TESTNET_BASE_URL = "https://api-testnet.gateapi.io"
EXPECTED_HOST = "api-testnet.gateapi.io"

PAIR = "BTC_USDT"
MAX_NOTIONAL_USDT = 5.0  # hard cap on order value
DISCOUNT = 0.70  # buy limit at 30% below last price -> deep out of the book


def refuse(reason: str) -> None:
    print(f"REFUSED: {reason}")
    print("No order was placed; exiting.")
    sys.exit(0)


def main() -> None:
    # -- gate 1: explicit opt-in -------------------------------------------
    if os.getenv("GATEIO_ALLOW_ORDERS", "") != "YES":
        refuse(
            "GATEIO_ALLOW_ORDERS is not set to YES. This example places real "
            "orders on the Gate.io testnet and requires an explicit opt-in: "
            "run it with GATEIO_ALLOW_ORDERS=YES. API keys alone never enable "
            "order placement."
        )
    print("[gate 1/4] explicit opt-in: GATEIO_ALLOW_ORDERS=YES")

    # -- gate 2: testnet host only -----------------------------------------
    host = urlparse(TESTNET_BASE_URL).hostname
    if host != EXPECTED_HOST:
        refuse(f"base URL host {host!r} is not the testnet host {EXPECTED_HOST!r}")
    print(f"[gate 2/4] endpoint is the hard-coded testnet host: {host}")

    # -- gate 3: testnet credentials ---------------------------------------
    api_key = os.getenv("GATE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("GATE_TESTNET_API_SECRET", "").strip()
    if not api_key or not api_secret:
        refuse(
            "missing GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET. Create a "
            "Gate.io testnet API key pair and export both variables."
        )
    print("[gate 3/4] testnet credentials present")

    client = GateioHttpClient(
        api_key=api_key,
        api_secret=api_secret,
        live_orders=True,  # explicit switch; without it orders raise locally
        base_url=TESTNET_BASE_URL,
    )
    with client:
        client.sync_time()

        # -- gate 4: notional hard cap -------------------------------------
        spec = client.currency_pair(PAIR)
        last = client.ticker_last(PAIR)
        price = round(last * DISCOUNT, spec["price_precision"])
        # Smallest amount satisfying the exchange minimums at our price...
        min_quote = float(spec["min_quote_amount"] or 0)
        min_base = float(spec["min_base_amount"] or 0)
        target_notional = max(min_quote * 1.05, min_base * price, 1.0)
        amount = round(target_notional / price, spec["amount_precision"])
        notional = amount * price
        if notional > MAX_NOTIONAL_USDT:
            refuse(
                f"computed notional {notional:.4f} USDT exceeds the hard cap of "
                f"{MAX_NOTIONAL_USDT} USDT (pair minimums too high for this demo)"
            )
        print(f"[gate 4/4] notional {notional:.4f} USDT is within the {MAX_NOTIONAL_USDT} USDT cap")

        print(f"\nlast price {last:.2f}; placing limit buy {amount} {PAIR} @ {price:.2f}")
        order = client.place_order_validated(
            PAIR,
            "buy",
            amount=amount,
            price=price,
            spec=spec,
        )
        print(f"placed: id={order['id']} status={order['status']} client_id={order['client_id']}")

        open_orders = client.open_orders(PAIR)
        print(f"\nopen orders for {PAIR}: {len(open_orders)}")
        for entry in open_orders:
            marker = "  <-- ours" if entry["id"] == order["id"] else ""
            print(
                f"  id={entry['id']} {entry['side']} {entry['amount']} @ {entry['price']}{marker}"
            )

        print("\ncancelling ...")
        cancelled = client.cancel_order(order["id"], PAIR)
        print(f"cancelled: id={cancelled['id']} status={cancelled['status']}")

        remaining = client.open_orders(PAIR)
        print(f"\nfinal state: {len(remaining)} open orders for {PAIR}")
        print("done - the order never rested near the market and was cancelled cleanly")


if __name__ == "__main__":
    main()
