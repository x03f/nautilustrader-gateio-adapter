"""Public WebSocket streaming via ``GateioWebSocketClient``.

Subscribes to 1-minute candlesticks and public trades for BTC_USDT and prints
every *closed* bar and each trade as it arrives. After the run window expires,
transport reliability metrics (reconnects, gap count, message count) are
printed.

Note: only closed candles are emitted, so with the default 90-second window
you should see one or two bars; trades usually arrive within seconds.

Credentials: NOT required. Public market-data channels only.

Run:
    python examples/02_public_websocket.py [max_seconds]

``max_seconds`` defaults to 90; pass a smaller value for a quicker look.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from nautilus_gateio import GateioWebSocketClient

PAIR = "BTC_USDT"
DEFAULT_MAX_SECONDS = 90.0


def on_bar(bar: dict[str, Any]) -> None:
    when = datetime.fromtimestamp(bar["ts"] / 1000, tz=UTC)
    gap_note = "  [GAP detected before this bar]" if bar["gap"] else ""
    print(
        f"closed bar  {bar['pair']} {bar['interval']}  {when:%H:%M} UTC  "
        f"O={bar['open']:.2f} H={bar['high']:.2f} L={bar['low']:.2f} "
        f"C={bar['close']:.2f} V={bar['volume']:.4f}{gap_note}",
        flush=True,
    )


def on_trade(result: Any) -> None:
    trades = result if isinstance(result, list) else [result]
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        print(
            f"trade       {trade.get('currency_pair', PAIR)}  "
            f"{trade.get('side', '?'):4s} {trade.get('amount', '?')} @ {trade.get('price', '?')}",
            flush=True,
        )


async def main(max_seconds: float) -> None:
    client = GateioWebSocketClient(on_bar=on_bar, on_trade=on_trade)
    client.subscribe_candles(PAIR, "1m")
    client.subscribe_trades(PAIR)

    print(f"streaming {PAIR} 1m candles + trades for ~{max_seconds:.0f}s ...", flush=True)
    try:
        # The client also accepts run(max_seconds=...), but that deadline is
        # only checked between messages; wait_for enforces it during quiet
        # periods as well, so the script always ends on time.
        await asyncio.wait_for(client.run(max_seconds=max_seconds), timeout=max_seconds + 5)
    except TimeoutError:
        client.stop()

    print(f"\ntransport metrics: {client.metrics()}")


if __name__ == "__main__":
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MAX_SECONDS
    try:
        asyncio.run(main(seconds))
    except KeyboardInterrupt:
        print("\ninterrupted")
