"""Building NautilusTrader instruments for every Gate.io product.

``GateioInstrumentProvider`` loads one product family per REST namespace and
turns each payload into the matching NautilusTrader instrument: ``CurrencyPair``
for spot, ``CryptoPerpetual`` for perpetuals (linear and inverse),
``CryptoFuture`` for delivery contracts and ``CryptoOption`` for options.

The instrument ids show the symbology rule in practice: the venue symbol is used
verbatim, and only perpetuals take a ``-PERP`` suffix, because they are the only
product that shares a symbol with a spot pair.

Credentials: NOT required (instrument endpoints are public; with credentials the
account's real fee tier is used for spot instead of the published default).

Run:
    python examples/03_instruments.py
"""

from __future__ import annotations

import asyncio

from nautilus_trader.model.identifiers import InstrumentId

from gateio_nt import (
    GateioFuturesHttpAPI,
    GateioHttpClient,
    GateioInstrumentProvider,
    GateioOptionsHttpAPI,
    GateioProductType,
    product_of,
)

PRODUCTS = (
    GateioProductType.SPOT,
    GateioProductType.PERP,
    GateioProductType.INVERSE,
    GateioProductType.FUT,
    GateioProductType.OPT,
)


async def pick_instrument_ids(client: GateioHttpClient) -> list[InstrumentId]:
    """Choose one live instrument id per product, discovered from the venue."""
    ids = [
        InstrumentId.from_str("BTC_USDT.GATE_IO"),
        InstrumentId.from_str("BTC_USDT-PERP.GATE_IO"),
        InstrumentId.from_str("BTC_USD-PERP.GATE_IO"),
    ]

    delivery = await GateioFuturesHttpAPI(client, settle="usdt", delivery=True).contracts()
    if delivery:
        ids.append(InstrumentId.from_str(f"{delivery[0]['name']}.GATE_IO"))

    options = GateioOptionsHttpAPI(client)
    expirations = await options.expirations("BTC_USDT")
    if expirations:
        contracts = await options.contracts("BTC_USDT", expiration=min(expirations))
        if contracts:
            ids.append(InstrumentId.from_str(f"{contracts[0]['name']}.GATE_IO"))

    return ids


async def main() -> None:
    async with GateioHttpClient() as client:
        provider = GateioInstrumentProvider(client, products=PRODUCTS)
        instrument_ids = await pick_instrument_ids(client)

        print(f"loading {len(instrument_ids)} instruments...")
        await provider.load_ids_async(instrument_ids)

        for instrument_id in instrument_ids:
            instrument = provider.find(instrument_id)
            if instrument is None:
                print(f"\n{instrument_id}: not available")
                continue

            print(f"\n{instrument.id}  ({type(instrument).__name__})")
            print(f"  raw symbol      : {instrument.raw_symbol}")
            print(f"  price increment : {instrument.price_increment}")
            print(f"  size increment  : {instrument.size_increment}")
            print(f"  multiplier      : {instrument.multiplier}")
            print(f"  maker / taker   : {instrument.maker_fee} / {instrument.taker_fee}")
            product = product_of(instrument.id)
            meaning = "base-currency amount" if product.is_spot else "number of contracts"
            print(f"  product         : {product.value}")
            print(f"  quantity means  : {meaning}")


if __name__ == "__main__":
    asyncio.run(main())
