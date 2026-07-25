"""Loading Nautilus instruments via ``GateioInstrumentProvider``.

Fetches the Gate.io specifications for BTC_USDT and ETH_USDT and builds
Nautilus ``CurrencyPair`` instruments from them, then prints the key trading
constraints (precisions, increments, minimum quantity and minimum notional)
that Nautilus uses for order validation.

Credentials: NOT required. Instrument specifications are public data.

Run:
    python examples/03_instruments.py
"""

from __future__ import annotations

import asyncio

from nautilus_trader.model.identifiers import InstrumentId

from nautilus_gateio import GateioInstrumentProvider

INSTRUMENT_IDS = [
    InstrumentId.from_str("BTC_USDT.GATEIO"),
    InstrumentId.from_str("ETH_USDT.GATEIO"),
]


async def main() -> None:
    provider = GateioInstrumentProvider()
    await provider.load_ids_async(INSTRUMENT_IDS)

    for instrument_id in INSTRUMENT_IDS:
        instrument = provider.find(instrument_id)
        if instrument is None:
            print(f"{instrument_id}: not found")
            continue
        print(f"{instrument.id}")
        print(f"  base / quote   : {instrument.base_currency} / {instrument.quote_currency}")
        print(f"  price precision: {instrument.price_precision}")
        print(f"  size precision : {instrument.size_precision}")
        print(f"  price increment: {instrument.price_increment}")
        print(f"  size increment : {instrument.size_increment}")
        print(f"  min quantity   : {instrument.min_quantity}")
        print(f"  min notional   : {instrument.min_notional}")
        print(f"  maker / taker  : {instrument.maker_fee} / {instrument.taker_fee}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
