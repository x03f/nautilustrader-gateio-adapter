"""Spot TESTNET order through a live ``TradingNode``.

The whole path in one file: both clients configured, both factories registered,
a strategy that submits one post-only limit buy far below the market and cancels
it on stop. Nothing here can fill at the price it uses.

Example 06 places its order by calling ``GateioSpotHttpAPI`` directly, which
exercises the transport. This one goes through the platform: the order is built
by ``OrderFactory``, routed by the execution engine, and reported back through
the client, which is what a strategy actually meets.

**This script has never been run against Gate.io.** It builds, and its gates and
its order construction were checked against the live spot specification, but no
order has been submitted from it on either environment. Run it once on the
testnet before trusting it.

Safety gates in this script (they live here, not in the adapter):

1. **Explicit opt-in.** Refuses to run unless ``GATEIO_ALLOW_ORDERS=YES`` is set
   for this run, so credentials sitting in the environment cannot place an order
   on their own.
2. **Testnet, stated.** Both clients carry ``environment="testnet"``. Confirm it
   in the log: the execution client prints ``Environment: testnet`` and
   ``Account type: CASH`` while it starts.
3. **Bounded notional.** ``NOTIONAL_USDT`` caps what the order is worth, and the
   limit price is half the best bid.
4. **Cancel on stop.** ``on_stop`` cancels what the strategy left resting. Check
   the venue afterwards anyway: two of four recorded mainnet shutdowns ended
   with an order still on the book (docs/validation.md).

Credentials: REQUIRED, testnet only. The testnet is a separate Gate.io account
with its own keys and its own balances. Unlike example 05 and example 06, this
script does not accept the mainnet fallback: a testnet run signed with a mainnet
key fails at the venue with ``INVALID_SIGNATURE``, which reads like a signing bug
and is a missing account.

    export GATE_TESTNET_API_KEY=...
    export GATE_TESTNET_API_SECRET=...
    export GATEIO_ALLOW_ORDERS=YES

Run (stop with Ctrl-C):
    python examples/07_trading_node_orders.py
"""

from __future__ import annotations

import os
import sys

from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from gateio_nt import (
    GATEIO,
    GATEIO_CLIENT_ID,
    GATEIO_VENUE,
    GateioDataClientConfig,
    GateioExecClientConfig,
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
    GateioProductType,
)

SPOT_ID = InstrumentId.from_str("BTC_USDT.GATE_IO")
#: Value of the single order this script may submit. The pair's own minimum
#: notional still applies and is read off the instrument at run time.
NOTIONAL_USDT = 6.0


class OneRestingBid(Strategy):
    """Submits one post-only limit buy at half the market, then holds it."""

    def __init__(self) -> None:
        super().__init__()
        self._submitted = False

    def on_start(self) -> None:
        # Read-only, and it costs one REST call: the client re-reads every
        # enabled wallet and logs an error naming any it could not read, which
        # says whether the credentials work before an order is ever built.
        account = self.cache.account_for_venue(GATEIO_VENUE)
        if account is not None:
            self.query_account(account.id, client_id=GATEIO_CLIENT_ID)
        self.subscribe_quote_ticks(SPOT_ID)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self._submitted:
            return
        self._submitted = True

        instrument = self.cache.instrument(SPOT_ID)
        price = instrument.make_price(tick.bid_price.as_double() * 0.5)
        quantity = instrument.make_qty(NOTIONAL_USDT / price.as_double())

        order = self.order_factory.limit(
            instrument_id=SPOT_ID,
            order_side=OrderSide.BUY,
            quantity=quantity,
            price=price,
            time_in_force=TimeInForce.GTC,
            post_only=True,  # Gate.io `poc`: maker-only, rests until canceled
        )
        self.log.info(f"Submitting {order.quantity} @ {order.price}")
        self.submit_order(order)

    def on_stop(self) -> None:
        self.cancel_all_orders(SPOT_ID)
        self.unsubscribe_quote_ticks(SPOT_ID)


def main() -> int:
    if os.environ.get("GATEIO_ALLOW_ORDERS") != "YES":
        print(
            "Refusing to run: set GATEIO_ALLOW_ORDERS=YES for this run to allow "
            "this script to place a testnet order.",
            file=sys.stderr,
        )
        return 1
    if not os.environ.get("GATE_TESTNET_API_KEY"):
        print(
            "No testnet credentials found. Set GATE_TESTNET_API_KEY and "
            "GATE_TESTNET_API_SECRET and try again. The mainnet pair is not "
            "accepted here: it would sign a testnet request and the venue would "
            "answer INVALID_SIGNATURE.",
            file=sys.stderr,
        )
        return 1

    # Both clients take the same provider config, so the factories hand them one
    # transport and one instrument load rather than two.
    instrument_provider = InstrumentProviderConfig(load_ids=frozenset([str(SPOT_ID)]))

    config = TradingNodeConfig(
        trader_id="GATEIO-001",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={
            GATEIO: GateioDataClientConfig(
                environment="testnet",
                products=(GateioProductType.SPOT,),
                instrument_provider=instrument_provider,
            ),
        },
        exec_clients={
            GATEIO: GateioExecClientConfig(
                environment="testnet",
                products=(GateioProductType.SPOT,),
                instrument_provider=instrument_provider,
            ),
        },
    )

    node = TradingNode(config=config)
    node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
    node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
    node.build()
    node.trader.add_strategy(OneRestingBid())

    try:
        node.run()
    finally:
        node.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
