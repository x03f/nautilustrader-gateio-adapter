"""Live ``TradingNode`` consuming Gate.io market data for two products.

Wires the Gate.io data client into a live ``TradingNode`` and runs a strategy
that subscribes to real quotes, real trades and closed bars for one spot pair
and one perpetual contract at the same time — one client, two products, two
WebSocket endpoints.

Instruments must be loaded before a strategy can subscribe to them, which is
what ``InstrumentProviderConfig(load_ids=...)`` inside the data client config
does during node startup.

Credentials: NOT required. Market data is public.

Run (stop with Ctrl-C):
    python examples/04_trading_node_data.py
"""

from __future__ import annotations

from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from nautilus_gateio import (
    GATEIO,
    GateioDataClientConfig,
    GateioLiveDataClientFactory,
    GateioProductType,
)

SPOT_ID = InstrumentId.from_str("BTC_USDT.GATE_IO")
PERP_ID = InstrumentId.from_str("BTC_USDT-PERP.GATE_IO")
INSTRUMENT_IDS = (SPOT_ID, PERP_ID)


class MarketDataLogger(Strategy):
    """Subscribes to quotes, trades and bars, and logs a sample of each."""

    def __init__(self) -> None:
        super().__init__()
        self._quotes = 0
        self._trades = 0

    def on_start(self) -> None:
        for instrument_id in INSTRUMENT_IDS:
            self.subscribe_quote_ticks(instrument_id)
            self.subscribe_trade_ticks(instrument_id)
            self.subscribe_bars(BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"))
            self.log.info(f"Subscribed to quotes, trades and 1m bars for {instrument_id}")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self._quotes += 1
        if self._quotes % 25 == 1:
            self.log.info(f"Quote {tick.instrument_id}: {tick.bid_price} / {tick.ask_price}")

    def on_trade_tick(self, tick: TradeTick) -> None:
        self._trades += 1
        if self._trades % 25 == 1:
            self.log.info(f"Trade {tick.instrument_id}: {tick.size} @ {tick.price}")

    def on_bar(self, bar: Bar) -> None:
        # Only closed bars are ever published, so the first one arrives at the
        # end of the interval it covers.
        self.log.info(f"Bar {bar.bar_type}: close={bar.close} volume={bar.volume}")

    def on_stop(self) -> None:
        self.log.info(f"Received {self._quotes} quotes and {self._trades} trades")
        for instrument_id in INSTRUMENT_IDS:
            self.unsubscribe_quote_ticks(instrument_id)
            self.unsubscribe_trade_ticks(instrument_id)
            self.unsubscribe_bars(BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"))


def main() -> None:
    config = TradingNodeConfig(
        trader_id="EXAMPLE-001",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={
            GATEIO: GateioDataClientConfig(
                # environment defaults to "mainnet"; public data lives there.
                products=(GateioProductType.SPOT, GateioProductType.PERP),
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset(str(i) for i in INSTRUMENT_IDS),
                ),
                order_book_update_interval_ms=100,
                order_book_snapshot_limit=100,
            ),
        },
        timeout_connection=30.0,
    )

    node = TradingNode(config=config)
    node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
    node.build()
    node.trader.add_strategy(MarketDataLogger())
    print("node built - starting (Ctrl-C to stop)", flush=True)

    try:
        node.run()
    except KeyboardInterrupt:
        print("interrupt received - shutting down", flush=True)
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
