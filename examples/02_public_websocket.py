"""Public WebSocket streams for two products at once.

``GateioPublicWebSocket`` wraps one connection to one product's endpoint,
because Gate.io serves each product from its own host and a message is only
interpretable together with the host it arrived on. This script opens a spot
connection and a perpetual connection, subscribes each to the real best
bid/offer stream and the public trade stream, and prints what arrives.

Nothing here is synthesized: quotes come from ``*.book_ticker`` and trades from
``*.trades``, exactly as the venue publishes them.

Credentials: NOT required.

Run:
    python examples/02_public_websocket.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from nautilus_gateio import GateioProductType, GateioPublicWebSocket

SYMBOL = "BTC_USDT"
RUN_SECONDS = 15.0

PRODUCTS = (GateioProductType.SPOT, GateioProductType.PERP)


class Counter:
    """Counts and prints a sample of what one product's stream delivers."""

    def __init__(self, product: GateioProductType) -> None:
        self.product = product
        self.quotes = 0
        self.trades = 0

    def __call__(self, message: dict[str, Any]) -> None:
        channel = message.get("channel", "")
        result = message.get("result")
        if channel.endswith(".book_ticker"):
            self.quotes += 1
            if self.quotes == 1 and isinstance(result, dict):
                print(
                    f"[{self.product.value}] first quote: "
                    f"bid {result.get('b')} x {result.get('B')} / "
                    f"ask {result.get('a')} x {result.get('A')}"
                )
        elif channel.endswith(".trades"):
            self.trades += 1
            if self.trades == 1:
                # Spot sends one object; futures send an array of objects.
                first = result[0] if isinstance(result, list) and result else result
                if isinstance(first, dict):
                    size = first.get("amount", first.get("size"))
                    print(f"[{self.product.value}] first trade: {size} @ {first.get('price')}")


async def stream(product: GateioProductType) -> Counter:
    counter = Counter(product)
    ws = GateioPublicWebSocket(product=product, handler=counter)
    await ws.connect()
    try:
        await ws.subscribe_book_ticker(SYMBOL)
        await ws.subscribe_trades(SYMBOL)
        print(f"[{product.value}] subscribed on {ws.client.url}")
        await asyncio.sleep(RUN_SECONDS)
        print(
            f"[{product.value}] reconnects={ws.client.reconnects} "
            f"messages={ws.client.messages_received} "
            f"subscribe_failures={ws.client.subscribe_failures}"
        )
    finally:
        await ws.disconnect()
    return counter


async def main() -> None:
    print(f"Streaming {SYMBOL} for {RUN_SECONDS:.0f}s on: {', '.join(p.value for p in PRODUCTS)}")
    counters = await asyncio.gather(*(stream(product) for product in PRODUCTS))

    print("\nreceived:")
    for counter in counters:
        print(f"  {counter.product.value:<8} quotes={counter.quotes} trades={counter.trades}")
    print("\nQuotes are the venue's own best bid/offer; nothing was derived or simulated.")


if __name__ == "__main__":
    asyncio.run(main())
