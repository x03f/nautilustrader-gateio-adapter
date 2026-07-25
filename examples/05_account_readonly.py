"""Authenticated READ-ONLY account access against the Gate.io testnet.

Signs in with testnet credentials and prints spot balances and open orders —
and nothing else. The client is constructed with ``live_orders=False`` (the
default), which makes every order-mutating call impossible: the adapter
raises ``LiveOrdersDisabledError`` before any network request is made, even
though valid credentials are present. This script demonstrates that guarantee
by attempting a (blocked) order placement at the end.

Credentials: REQUIRED (testnet). Set these environment variables:
    GATE_TESTNET_API_KEY
    GATE_TESTNET_API_SECRET

The script exits with a clear message if they are missing. It never places,
modifies, or cancels orders.

Run:
    python examples/05_account_readonly.py
"""

from __future__ import annotations

import os
import sys

from nautilus_gateio import (
    GATEIO_HTTP_TESTNET,
    GateioHttpClient,
    LiveOrdersDisabledError,
)


def main() -> None:
    api_key = os.getenv("GATE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("GATE_TESTNET_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print(
            "Missing testnet credentials.\n"
            "Set GATE_TESTNET_API_KEY and GATE_TESTNET_API_SECRET to run this "
            "example (a Gate.io testnet account API key pair).\n"
            "Nothing was queried; exiting."
        )
        sys.exit(0)

    # live_orders=False (the default) is a hard switch: with it, order
    # placement/cancellation is impossible regardless of credentials.
    client = GateioHttpClient(
        api_key=api_key,
        api_secret=api_secret,
        live_orders=False,
        base_url=GATEIO_HTTP_TESTNET,
    )
    with client:
        client.sync_time()  # align signature timestamps with the exchange clock

        print("spot balances (non-zero):")
        balances = client.balances()
        shown = 0
        for currency, amounts in sorted(balances.items()):
            if amounts["available"] or amounts["locked"]:
                print(
                    f"  {currency:8s} available={amounts['available']} locked={amounts['locked']}"
                )
                shown += 1
        if not shown:
            print("  (none)")

        print("\nopen orders:")
        orders = client.open_orders()
        for order in orders:
            print(
                f"  {order['pair']} {order['side']} {order['amount']} @ {order['price']} "
                f"(id={order['id']}, status={order['status']})"
            )
        if not orders:
            print("  (none)")

        # Demonstrate the read-only guarantee: even with valid credentials,
        # order placement is rejected locally because live_orders=False.
        print("\nattempting an order placement to demonstrate the guard ...")
        try:
            client.place_order("BTC_USDT", "buy", amount="0.001", price="1.0")
            print("ERROR: order was accepted - this should be impossible")
            sys.exit(1)
        except LiveOrdersDisabledError as exc:
            print(f"blocked as expected: {exc}")
            print("(no request was sent to the exchange)")


if __name__ == "__main__":
    main()
