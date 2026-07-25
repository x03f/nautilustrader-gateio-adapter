"""Minimal Nautilus ``TradingNode`` streaming Gate.io bars to a strategy.

Wires the Gate.io data client into a live ``TradingNode`` and runs the
simplest possible strategy: subscribe to 1-minute BTC_USDT bars and log each
one as it closes. No execution client is configured — this node consumes
market data only.

Important: instruments must be pre-loaded via the instrument provider before
a strategy can subscribe to them. Here this is done declaratively with
``InstrumentProviderConfig(load_ids=...)`` inside the data client config; the
provider then fetches the specification and registers the ``CurrencyPair``
during node startup.

Credentials: NOT required. Market data is public.

Run (stop with Ctrl-C):
    python examples/04_trading_node_data.py
"""

from __future__ import annotations

from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.trading.strategy import Strategy

from nautilus_gateio import GATEIO, GateioDataClientConfig, GateioLiveDataClientFactory

BAR_TYPE = BarType.from_str("BTC_USDT.GATEIO-1-MINUTE-LAST-EXTERNAL")


class BarLogger(Strategy):
    """Logs every bar it receives; the smallest useful strategy."""

    def on_start(self) -> None:
        self.subscribe_bars(BAR_TYPE)
        self.log.info(f"Subscribed to {BAR_TYPE}")

    def on_bar(self, bar: Bar) -> None:
        self.log.info(f"Received {bar}")

    def on_stop(self) -> None:
        self.unsubscribe_bars(BAR_TYPE)


def main() -> None:
    config = TradingNodeConfig(
        trader_id="EXAMPLE-001",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={
            GATEIO: GateioDataClientConfig(
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset([str(BAR_TYPE.instrument_id)]),
                ),
            ),
        },
        timeout_connection=30.0,
    )

    node = TradingNode(config=config)
    node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
    node.build()
    node.trader.add_strategy(BarLogger())
    print("node built - starting (Ctrl-C to stop)", flush=True)

    try:
        node.run()
    except KeyboardInterrupt:
        print("interrupt received - shutting down", flush=True)
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
