"""Public REST across every Gate.io product family.

Shows the shared async transport (``GateioHttpClient``) plus one typed namespace
per product: spot pair specifications and top of book, a perpetual contract
definition, delivery contracts, and an option chain.

Credentials: NOT required. Every call here is a public read.

Run:
    python examples/01_public_rest.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from nautilus_gateio import (
    GateioFuturesHttpAPI,
    GateioHttpClient,
    GateioOptionsHttpAPI,
    GateioSpotHttpAPI,
)

SPOT_PAIR = "BTC_USDT"
PERP_CONTRACT = "BTC_USDT"
OPTION_UNDERLYING = "BTC_USDT"


def _top_of_contract_book(book: dict[str, Any], side: str) -> str:
    """Render the top level of a futures/options book (``{"p": ..., "s": ...}``)."""
    levels = book.get(side) or []
    if not levels:
        return "(empty)"
    top = levels[0]
    return f"{top['p']} x {top['s']}"


async def show_spot(spot: GateioSpotHttpAPI) -> None:
    spec = await spot.currency_pair(SPOT_PAIR)
    book = await spot.order_book(SPOT_PAIR, limit=5)
    candles = await spot.candlesticks(SPOT_PAIR, interval="1m", limit=3)

    print(f"\nSPOT {SPOT_PAIR}  ->  instrument id {SPOT_PAIR}.GATE_IO")
    print(f"  base / quote        : {spec['base']} / {spec['quote']}")
    print(f"  price precision     : {spec['precision']} decimals")
    print(f"  amount precision    : {spec['amount_precision']} decimals")
    print(f"  min base amount     : {spec.get('min_base_amount')}")
    print(f"  min quote (notional): {spec.get('min_quote_amount')}")
    print(f"  trade status        : {spec['trade_status']}")
    print(f"  top of book         : {book['bids'][0][0]} / {book['asks'][0][0]}")
    print(f"  last 1m closes      : {[row[2] for row in candles]}")


async def show_perpetual(perp: GateioFuturesHttpAPI) -> None:
    contract = await perp.contract(PERP_CONTRACT)
    book = await perp.order_book(PERP_CONTRACT, limit=5)

    print(f"\nPERPETUAL {PERP_CONTRACT}  ->  instrument id {PERP_CONTRACT}-PERP.GATE_IO")
    print(f"  quanto multiplier   : {contract['quanto_multiplier']} (base units per contract)")
    print(f"  price tick          : {contract['order_price_round']}")
    print(f"  min / max size      : {contract['order_size_min']} / {contract['order_size_max']}")
    print(f"  maintenance rate    : {contract['maintenance_rate']}")
    print(f"  funding rate        : {contract.get('funding_rate')}")
    print(
        f"  best bid / ask      : {_top_of_contract_book(book, 'bids')}"
        f" / {_top_of_contract_book(book, 'asks')}"
    )


async def show_delivery(delivery: GateioFuturesHttpAPI) -> None:
    contracts = await delivery.contracts()
    print("\nDELIVERY (dated futures)")
    if not contracts:
        print("  no contracts listed right now")
        return
    for contract in contracts[:3]:
        print(
            f"  {contract['name']:<28} cycle={contract.get('cycle')}"
            f" multiplier={contract.get('quanto_multiplier')}"
        )
    print(f"  instrument id form  : {contracts[0]['name']}.GATE_IO (no suffix needed)")


async def show_options(options: GateioOptionsHttpAPI) -> None:
    expirations = await options.expirations(OPTION_UNDERLYING)
    print(f"\nOPTIONS on {OPTION_UNDERLYING}")
    if not expirations:
        print("  no expirations listed right now")
        return
    nearest = min(expirations)
    contracts = await options.contracts(OPTION_UNDERLYING, expiration=nearest)
    print(f"  expirations listed  : {len(expirations)} (nearest unix {nearest})")
    for contract in contracts[:3]:
        print(
            f"  {contract['name']:<32} multiplier={contract.get('multiplier')}"
            f" tick={contract.get('order_price_round')}"
        )


async def main() -> None:
    # No credentials: every endpoint used here is public.
    async with GateioHttpClient() as client:
        await show_spot(GateioSpotHttpAPI(client))
        await show_perpetual(GateioFuturesHttpAPI(client, settle="usdt"))
        await show_delivery(GateioFuturesHttpAPI(client, settle="usdt", delivery=True))
        await show_options(GateioOptionsHttpAPI(client))

    print("\nQuantity semantics: spot quantities are base-currency amounts;")
    print("contract quantities (perpetual, delivery, option) are numbers of contracts.")


if __name__ == "__main__":
    asyncio.run(main())
