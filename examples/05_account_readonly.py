"""Authenticated READ-ONLY account inspection.

Reads the account's fee tier, the balances of each product wallet, and any
resting spot orders. Nothing here mutates state: every call is a GET.

Two things this example makes concrete:

* Gate.io keeps a **separate wallet per product**, and the derivative wallets do
  not exist until the first internal transfer into them. The venue reports a
  missing wallet as an ordinary error, which the adapter turns into
  ``WalletNotProvisionedError`` — a configuration state, not a failure.
* ``environment`` defaults to ``"mainnet"``. This script asks for the
  environment explicitly through ``GATEIO_ENVIRONMENT`` and defaults it to
  ``testnet``, because an example that reads a live account should say which
  account it means.

Credentials: REQUIRED.

    export GATEIO_ENVIRONMENT=testnet             # or "mainnet"
    export GATE_TESTNET_API_KEY=...               # testnet credentials
    export GATE_TESTNET_API_SECRET=...
    # for mainnet: GATE_API_KEY / GATE_API_SECRET

Run:
    python examples/05_account_readonly.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from nautilus_gateio import (
    GateioFuturesHttpAPI,
    GateioHttpClient,
    GateioOptionsHttpAPI,
    GateioSpotHttpAPI,
    GateioWalletHttpAPI,
    WalletNotProvisionedError,
    resolve_credentials,
)
from nautilus_gateio.config import resolve_http_url
from nautilus_gateio.http.margin import require_wallet

ENVIRONMENT = os.environ.get("GATEIO_ENVIRONMENT", "testnet").strip().lower()


async def show_fees(wallet: GateioWalletHttpAPI) -> None:
    fees = await wallet.fee()
    print("\nfee tier (GET /wallet/fee)")
    print(f"  user id             : {fees.get('user_id')}")
    print(f"  spot maker / taker  : {fees.get('maker_fee')} / {fees.get('taker_fee')}")
    print(
        f"  perp maker / taker  : {fees.get('futures_maker_fee')} / {fees.get('futures_taker_fee')}"
    )


async def show_spot(spot: GateioSpotHttpAPI) -> None:
    accounts = await spot.accounts()
    funded = [row for row in accounts if float(row.get("available", 0) or 0) > 0]
    print("\nspot wallet (GET /spot/accounts)")
    if not funded:
        print("  no non-zero balances")
    for row in funded[:10]:
        print(f"  {row['currency']:<8} available={row['available']} locked={row['locked']}")

    grouped = await spot.open_orders()
    total = sum(int(group.get("total", 0) or 0) for group in grouped)
    print(f"\nresting spot orders : {total}")
    for group in grouped:
        for order in group.get("orders", [])[:5]:
            print(
                f"  {group['currency_pair']:<12} id={order['id']} {order['side']}"
                f" {order['amount']} @ {order.get('price')} left={order.get('left')}"
            )


async def show_wallet(name: str, coroutine: object) -> None:
    """Report one derivative wallet, tolerating one that does not exist yet."""
    try:
        account = await require_wallet(coroutine, f"the {name} wallet")  # type: ignore[arg-type]
    except WalletNotProvisionedError as exc:
        print(f"\n{name} wallet: {exc}")
        return
    if isinstance(account, dict):
        print(f"\n{name} wallet: total={account.get('total')} available={account.get('available')}")


async def main() -> int:
    api_key, api_secret = resolve_credentials(None, None, testnet=ENVIRONMENT == "testnet")
    if not api_key or not api_secret:
        print(
            "No credentials found. Set GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET "
            "(or GATE_API_KEY / GATE_API_SECRET for mainnet) and try again.",
            file=sys.stderr,
        )
        return 1

    base_url = resolve_http_url(ENVIRONMENT)
    print(f"environment: {ENVIRONMENT}  ->  {base_url}")

    async with GateioHttpClient(api_key, api_secret, base_url=base_url) as client:
        # Align the signature timestamp with the venue clock before signing.
        await client.sync_time()

        await show_fees(GateioWalletHttpAPI(client))
        await show_spot(GateioSpotHttpAPI(client))
        await show_wallet(
            "perpetual",
            GateioFuturesHttpAPI(client, settle="usdt").accounts(),
        )
        await show_wallet(
            "delivery",
            GateioFuturesHttpAPI(client, settle="usdt", delivery=True).accounts(),
        )
        await show_wallet("options", GateioOptionsHttpAPI(client).account())

    print("\nRead-only: this script issued no mutating request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
