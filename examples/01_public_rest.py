"""Public REST market data via ``GateioHttpClient``.

Demonstrates the credential-free endpoints of the Gate.io API v4 spot REST
client: server ping, a single currency-pair specification, recent candles,
and the top of the order book for BTC_USDT.

Credentials: NOT required. This script performs read-only public requests.

Run:
    python examples/01_public_rest.py
"""

from __future__ import annotations

from datetime import UTC, datetime

from nautilus_gateio import GateioHttpClient

PAIR = "BTC_USDT"


def main() -> None:
    # No api_key/api_secret: public market data needs no credentials.
    with GateioHttpClient() as client:
        # 1. Ping: round-trip latency and exchange server time.
        info = client.ping()
        print(f"ping ok: server_time_ms={info['server_time_ms']} latency_ms={info['latency_ms']}")

        # 2. Trading constraints for a single pair.
        spec = client.currency_pair(PAIR)
        print(f"\n{PAIR} specification:")
        print(f"  base / quote        : {spec['base']} / {spec['quote']}")
        print(f"  price precision     : {spec['price_precision']} decimals")
        print(f"  amount precision    : {spec['amount_precision']} decimals")
        print(f"  min base amount     : {spec['min_base_amount']}")
        print(f"  min quote (notional): {spec['min_quote_amount']}")
        print(f"  trade status        : {spec['trade_status']}")

        # 3. Last 5 one-minute candles (oldest first).
        candles = client.candles(PAIR, interval="1m", limit=5)
        print(f"\nlast {len(candles)} 1m candles for {PAIR}:")
        for candle in candles:
            when = datetime.fromtimestamp(candle["ts"] / 1000, tz=UTC)
            print(
                f"  {when:%Y-%m-%d %H:%M} UTC  "
                f"O={candle['open']:.2f} H={candle['high']:.2f} "
                f"L={candle['low']:.2f} C={candle['close']:.2f} "
                f"V={candle['volume']:.4f}"
            )

        # 4. Top of the order book.
        book = client.order_book(PAIR, limit=1)
        best_bid = book["bids"][0] if book["bids"] else None
        best_ask = book["asks"][0] if book["asks"] else None
        print(f"\ntop of book for {PAIR}:")
        if best_bid:
            print(f"  best bid: {best_bid[0]:.2f} (size {best_bid[1]})")
        if best_ask:
            print(f"  best ask: {best_ask[0]:.2f} (size {best_ask[1]})")
        if best_bid and best_ask:
            print(f"  spread  : {best_ask[0] - best_bid[0]:.2f}")


if __name__ == "__main__":
    main()
