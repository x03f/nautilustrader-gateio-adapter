# Gate.io Adapter for NautilusTrader

[![CI](https://github.com/x03f/nautilustrader-gateio-adapter/actions/workflows/ci.yml/badge.svg)](https://github.com/x03f/nautilustrader-gateio-adapter/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![NautilusTrader 1.230+](https://img.shields.io/badge/nautilus__trader-1.230%2B-orange)](https://github.com/nautechsystems/nautilus_trader)

A community-maintained adapter connecting
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) to
[Gate.io](https://www.gate.io/): market data and order execution across spot, margin, perpetual
futures (linear and inverse), delivery futures and options, over the Gate.io v4 REST and WebSocket
API. It is an external pip-installable package rather than part of the NautilusTrader repository,
and it is not affiliated with, maintained by or endorsed by Gate.io or Nautech Systems.

- Market data comes from the venue's own streams and listings: trades, best bid and offer,
  sequence-validated book deltas, ten-level depth from the periodic snapshot channel, closed bars,
  mark and index prices, funding rates, instrument status and settlement closes. Nothing is
  synthesized or interpolated: where Gate.io publishes no history, the request is refused rather
  than answered from a current row.
- One data client and one execution client multiplex every product family.
- An order Gate.io cannot express is denied with a stated reason before any request is sent. It is
  never converted into a different order, and never reported as a venue rejection the venue did not
  make.
- All four NautilusTrader report generators are implemented against REST, so a restart holding
  resting orders and open positions is a supported path.

## Status

**Alpha, `0.2.0a1`**, built against `nautilus_trader` 1.230.0 on Python 3.13.

Gate.io mainnet has answered the public market-data paths, the whole spot order lifecycle, one
USDT perpetual and one option contract. No order has been sent on an inverse perpetual or a
delivery contract, or on any margin, cross-margin or unified spot ledger, and two of four recorded
shutdowns ended with an order still resting at the venue. Nothing is marked stable, because a
recorded run shows that a path works rather than that it keeps working:
[docs/validation.md](docs/validation.md) carries the record per capability, including the runs that
failed and three recorded checks that do not check what they claim.

The capability matrices below describe the branch, which is what the install line resolves to. The
published `0.2.0a1` release predates several of the client hooks they list — order book depth,
instrument status and settlement closes, the venue ticker type, order lists and `query_account` —
so a build pinned to that version has less than the matrices show. [CHANGELOG.md](CHANGELOG.md)
keeps the two apart.

Interfaces may change between 0.x releases. Upgrading from 0.1.0 is a port rather than a version
bump: read [docs/migration-0.1-to-0.2.md](docs/migration-0.1-to-0.2.md) first, because the venue
string, the instrument ids and the execution environment default all changed.

## Requirements and installation

| Adapter   | nautilus_trader | Python         | Gate.io API |
|-----------|-----------------|----------------|-------------|
| `0.2.0a1` | `>=1.230.0,<2`  | `>=3.12,<3.15` | v4          |

```bash
pip install "nautilustrader-gateio-adapter @ git+https://github.com/x03f/nautilustrader-gateio-adapter"
```

`nautilus_trader` is a declared dependency, so pip will pull it in. It is a large wheel, and on a
platform with no wheel it is a long source build; installing it into the target environment first
makes any failure there easier to read.

Check that the two versions line up before anything else:

```bash
python -c "import nautilus_gateio, nautilus_trader; print(nautilus_gateio.__version__, nautilus_trader.__version__)"
# 0.2.0a1 1.230.0
```

For development:

```bash
git clone https://github.com/x03f/nautilustrader-gateio-adapter
cd nautilustrader-gateio-adapter
pip install -e '.[dev]'
```

## Credentials

Four environment variables, and the package reads no others anywhere:

```bash
export GATE_API_KEY=...             # mainnet
export GATE_API_SECRET=...
export GATE_TESTNET_API_KEY=...     # testnet
export GATE_TESTNET_API_SECRET=...
```

| Variable                                           | Read when                                                 |
|----------------------------------------------------|-----------------------------------------------------------|
| `GATE_API_KEY` / `GATE_API_SECRET`                 | `environment="mainnet"`, and as the testnet fallback.     |
| `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` | `environment="testnet"`, in preference to the pair above. |

The credential names drop the "IO". The switches in the example scripts keep it
(`GATEIO_ENVIRONMENT`, `GATEIO_ALLOW_ORDERS`), the package is `nautilus_gateio`, the venue string is
`GATE_IO`, and every constant in the code is `GATEIO_*`. `GATEIO_API_KEY` is read by nothing.

Nothing checks at startup that credentials are present, because their absence is a valid state:
public market data needs none. A data client without a key runs and looks healthy. An execution
client without a key also builds and reaches `READY`, and then fails at its first signed request
with `MISSING_CREDENTIALS` (`nautilus_gateio/http/client.py`). A misspelled variable name produces
exactly that failure.

On `environment="testnet"` the testnet variables take precedence and fall back to the mainnet pair.
With only the mainnet pair exported, a testnet run signs with the mainnet key against the testnet
host, and the venue answers `INVALID_SIGNATURE`. That reads like a signing bug and is a missing
account.

Both config classes also accept `api_key=` and `api_secret=` directly, which is how a secret manager
feeds them in. Values sourced from the environment are stripped of surrounding whitespace, because a
key pasted with a trailing newline otherwise produces a signature Gate.io rejects without
explanation.

### Key permissions

Grant the key the sections the configuration actually calls, and no withdrawal permission: no module
in this package implements a withdrawal, and `transfer()` validates both ends of a move against a
fixed set of the account's own trading wallets, so an external destination cannot be expressed at
all. An IP allowlist on the key is the other control that binds at the venue.

Every execution client reads `/wallet/fee` once at startup, falling back to `/spot/fee`, for the
numeric account id that Gate.io's private derivative channels require. A spot-only client does not
need it: if both reads fail it logs two warnings and carries on. A client configured with any
derivative product refuses to start, raising a `RuntimeError` that names the missing read
permission. Beyond that, what is called follows the configuration:

| What you configure                           | REST sections called   |
|----------------------------------------------|------------------------|
| `SPOT`                                       | `/spot/*`              |
| `spot_account_mode=MARGIN` or `CROSS_MARGIN` | `/margin/*`            |
| `spot_account_mode=UNIFIED`                  | `/unified/*`           |
| `PERP`, `INVERSE`                            | `/futures/{settle}/*`  |
| `FUT`                                        | `/delivery/{settle}/*` |
| `OPT`                                        | `/options/*`           |
| `transfer()`                                 | `/wallet/*` transfers  |

[SECURITY.md](SECURITY.md) states what the adapter does with credentials, module by module, and what
identifies you in a log even though it is not a credential.

## Symbology and quantities

The venue string is `GATE_IO`. Gate.io symbols are used verbatim, and only perpetuals take a suffix,
because 527 of them shared a symbol with a spot pair when the listings were surveyed.

| Product             | Instrument id                                        |
|---------------------|------------------------------------------------------|
| Spot                | `BTC_USDT.GATE_IO`                                   |
| Perpetual (linear)  | `BTC_USDT-PERP.GATE_IO`                              |
| Perpetual (inverse) | `BTC_USD-PERP.GATE_IO`                               |
| Delivery future     | `BTC_USDT_YYYYMMDD.GATE_IO`                          |
| Option              | `BTC_USDT-YYYYMMDD-STRIKE-C.GATE_IO`, `-P` for a put |

Dated contracts are listed a few weeks at a time, so an expiration copied off a page is usually
already dead, and a subscription to a dead id fails silently rather than loudly.
[`examples/01_public_rest.py`](examples/01_public_rest.py) prints the delivery contracts and the
option chain the venue lists right now, and
[`examples/03_instruments.py`](examples/03_instruments.py) builds instruments from what it finds.

On spot a `Quantity` is an amount of the base currency. On every contract product it is a **number
of contracts**, with the face value in the instrument's `multiplier`. The full reasoning, including
why delivery and options need no suffix, is in [docs/symbology.md](docs/symbology.md).

## Getting market data

### Public REST, no credentials

```python
import asyncio

from nautilus_gateio import GateioFuturesHttpAPI, GateioHttpClient, GateioSpotHttpAPI


async def main() -> None:
    async with GateioHttpClient() as client:
        spot = GateioSpotHttpAPI(client)
        perp = GateioFuturesHttpAPI(client, settle="usdt")

        pair = await spot.currency_pair("BTC_USDT")
        book = await spot.order_book("BTC_USDT", limit=5)
        contract = await perp.contract("BTC_USDT")

        print(f"spot precision  : {pair['precision']} / {pair['amount_precision']}")
        print(f"spot top of book: {book['bids'][0][0]} / {book['asks'][0][0]}")
        print(f"perp multiplier : {contract['quanto_multiplier']}")


asyncio.run(main())
```

The REST transport and the WebSocket clients work without a Nautilus node, which is what
[`examples/01_public_rest.py`](examples/01_public_rest.py) and
[`examples/02_public_websocket.py`](examples/02_public_websocket.py) show.

### A live `TradingNode`

```python
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from nautilus_gateio import (
    GATEIO,
    GateioDataClientConfig,
    GateioLiveDataClientFactory,
    GateioProductType,
)

SPOT_ID = InstrumentId.from_str("BTC_USDT.GATE_IO")


class QuoteLogger(Strategy):
    def on_start(self) -> None:
        self.subscribe_quote_ticks(SPOT_ID)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.log.info(f"{tick.instrument_id}: {tick.bid_price} / {tick.ask_price}")

    def on_stop(self) -> None:
        self.unsubscribe_quote_ticks(SPOT_ID)


config = TradingNodeConfig(
    trader_id="EXAMPLE-001",
    logging=LoggingConfig(log_level="INFO"),
    data_clients={
        GATEIO: GateioDataClientConfig(
            products=(GateioProductType.SPOT,),
            instrument_provider=InstrumentProviderConfig(
                load_ids=frozenset([str(SPOT_ID)]),
            ),
        ),
    },
)

node = TradingNode(config=config)
node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
node.build()
node.trader.add_strategy(QuoteLogger())

try:
    node.run()
finally:
    node.dispose()
```

Within a couple of seconds:

```text
[INFO] EXAMPLE-001.DataClient-GATE_IO: RUNNING
[INFO] EXAMPLE-001.DataClient-GATE_IO: Connecting...
[INFO] EXAMPLE-001.GateioInstrumentProvider: Loading instruments: BTC_USDT.GATE_IO...
[INFO] EXAMPLE-001.GateioInstrumentProvider: Loaded 1 instruments
[INFO] EXAMPLE-001.DataClient-GATE_IO: Connected public WebSocket for SPOT: wss://api.gateio.ws/ws/v4/
[INFO] EXAMPLE-001.DataClient-GATE_IO: Subscribed BTC_USDT.GATE_IO quotes
[INFO] EXAMPLE-001.QuoteLogger: BTC_USDT.GATE_IO: 64854.0 / 64854.1
```

A strategy can only subscribe to instruments the provider has loaded, which is what `load_ids` does
during startup. Only closed bars are published, so a `1-MINUTE` subscription stays silent for up to
a minute after it starts, and a `1-DAY` subscription for up to a day. Use a short interval while
wiring things up.

[`examples/04_trading_node_data.py`](examples/04_trading_node_data.py) runs the same shape for a
spot pair and a perpetual at once, with quotes, trades and bars on both.

### Venue ticker rows

Gate.io's ticker message carries figures NautilusTrader has no type for: the 24-hour statistics, the
greeks and implied volatilities on options, the delivery basis. They reach a strategy as custom data
rather than being dropped.

```python
from nautilus_trader.model.data import DataType

from nautilus_gateio import GATEIO_CLIENT_ID, GateioTicker

self.subscribe_data(
    DataType(GateioTicker, metadata={"instrument_id": instrument_id}),
    client_id=GATEIO_CLIENT_ID,
)
```

The metadata is not optional. NautilusTrader addresses a custom type by the whole `DataType`, so a
bare `DataType(GateioTicker)` with the instrument id passed alongside it subscribes to a different
topic from the one rows are published on: the client reports the subscription held, and the
subscriber receives nothing. Every field is the venue's own string, unconverted. Mark price, index
price and funding rate are deliberately absent, because the platform has types of its own for those
three and this client publishes them from the same venue message. No recorded run against Gate.io
covers this path.

## Before your first order

Gate.io is five venues behind one API key, and most first-order failures are venue-side state rather
than code.

- **Keys.** Permissions as above, no withdrawal permission, IP allowlist. Testnet keys are issued by
  a separate testnet account and are not the mainnet ones.
- **Wallets are segregated per product.** USDT sitting in the spot wallet cannot open a perpetual
  position. The futures, delivery and options wallets do not exist until the first internal transfer
  into them; until then Gate.io answers `USER_NOT_FOUND`, and the adapter logs a warning and skips
  that product rather than refusing to start. Any transfer creates the wallet, whether it is made in
  the Gate.io interface, through `GateioWalletHttpAPI.transfer` or through
  `GateioExecutionClient.transfer`. [`examples/05_account_readonly.py`](examples/05_account_readonly.py)
  prints which of your wallets exist today.
- **`spot_account_mode` selects the ledger** spot orders trade against: `SPOT`, `MARGIN` (isolated),
  `CROSS_MARGIN` or `UNIFIED`. The margin ledgers need the corresponding account type provisioned at
  the venue, and cross margin and unified need the account upgraded out of classic mode, which only
  the account owner can do. The adapter never changes an account setting for you.
- **The Nautilus account type follows the configuration.** Spot alone on the plain spot ledger is a
  `CASH` account. Any derivative product, or any margin ledger, makes it `MARGIN`, which changes what
  the platform's risk engine will let a strategy do. The execution client logs which one it built at
  startup.
- **Sizes are per instrument.** Read `min_quantity` and `min_notional` off the instrument rather than
  assuming a floor; the smallest legal order on one pair is refused on another.
- **A spot market buy ends `CANCELED`, not `FILLED`.** Gate.io denominates it in the quote currency
  and states the base quantity only once the order finishes, and NautilusTrader has no way to move an
  order to `FILLED` against a quantity restated after its fills. The order is closed on the venue's
  own figure with `OrderCanceled`, which preserves the filled quantity. Read the outcome from
  `filled_qty` and the resulting position, never from the terminal status. The alternative was an
  estimated quantity that could leave the order open forever or make the engine discard a fill; see
  [docs/execution.md](docs/execution.md#fills).
- **Position mode is one-way** (`OmsType.NETTING`). Hedge mode is detected at connect and refused
  with an explanatory error, never switched off for you.

## One order on the testnet

Gate.io's testnet is a separate account with its own keys and its own balances, and it serves spot
and USDT perpetuals only. Configuring `INVERSE`, `FUT` or `OPT` together with
`environment="testnet"` raises before any network activity.

No testnet run is recorded in [docs/validation.md](docs/validation.md). A testnet success is
evidence that your wiring is right, not that this path has been exercised.

```python
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from nautilus_gateio import (
    GATEIO,
    GateioDataClientConfig,
    GateioExecClientConfig,
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
    GateioProductType,
)

SPOT_ID = InstrumentId.from_str("BTC_USDT.GATE_IO")
NOTIONAL_USDT = 6.0


class OneRestingBid(Strategy):
    """Submits one post-only limit buy at half the market, then holds it."""

    def __init__(self) -> None:
        super().__init__()
        self._submitted = False

    def on_start(self) -> None:
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
            environment="testnet",  # the default is "mainnet" on both clients
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
```

Export `GATE_TESTNET_API_KEY` and `GATE_TESTNET_API_SECRET` before running it. The order is
post-only at half the best bid, so it rests and cannot fill, and the pair's own minimum notional
still applies. `on_stop` cancels what is left; check the venue afterwards anyway, because two of four
recorded mainnet shutdowns ended with an order still on the book.

The same thing as a runnable script, behind an opt-in gate:
[`examples/07_trading_node_orders.py`](examples/07_trading_node_orders.py).

Nothing in the adapter gates any of this. There is no local kill switch, and `environment`
defaults to `"mainnet"` on both clients. What binds is the key's permissions, its IP allowlist, and
the environment you state. [`examples/06_testnet_orders.py`](examples/06_testnet_orders.py) places
the same kind of order through the REST namespace instead of a node.

## Registering the clients

Both client dictionaries and both factory registrations are keyed by the venue string `GATE_IO`,
which the package exports as `GATEIO`:

```python
from nautilus_gateio import GATEIO, GateioLiveDataClientFactory, GateioLiveExecClientFactory

node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
```

A declarative node config can carry the client configuration instead, which is how a pip-installed
adapter is wired into a node without being imported in your own code:

```python
from nautilus_trader.common.config import ImportableConfig, ImportableFactoryConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode

from nautilus_gateio import GATEIO, GateioLiveExecClientFactory

config = TradingNodeConfig(
    trader_id="GATEIO-001",
    data_clients={
        "GATE_IO": ImportableConfig(
            path="nautilus_gateio.config:GateioDataClientConfig",
            config={"products": ["SPOT", "PERP"], "instrument_provider": {"load_all": True}},
            factory=ImportableFactoryConfig(
                path="nautilus_gateio.factories:GateioLiveDataClientFactory",
            ),
        ),
    },
    exec_clients={
        "GATE_IO": ImportableConfig(
            path="nautilus_gateio.config:GateioExecClientConfig",
            config={"environment": "testnet", "products": ["SPOT"], "spot_account_mode": "spot"},
        ),
    },
)

node = TradingNode(config=config)
node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)  # not declarative, see below
node.build()
```

What that path requires, verified against `nautilus_trader` 1.230.0:

- **The execution factory has to be registered in Python.** Give the exec entry a
  `factory=ImportableFactoryConfig(...)` and `node.build()` raises
  `AttributeError: 'GateioLiveExecClientFactory' object has no attribute '__name__'`.
  `nautilus_trader/live/node_builder.py::TradingNodeBuilder.build_exec_clients` reads `__name__` off
  the factory object to recognize the sandbox factory, and `ImportableFactoryConfig.create()` hands
  it an instance rather than the class. Registering the factory beforehand makes the builder skip
  that construction, and the rest of the entry stays declarative. The data-client path carries no
  such check and works fully declaratively.
- `products` takes the enum *names*: `"SPOT"`, `"PERP"`, `"INVERSE"`, `"FUT"`, `"OPT"`, uppercase.
- `spot_account_mode` takes the venue's own strings: `"spot"`, `"margin"`, `"cross_margin"`,
  `"unified"`, lowercase. `"SPOT"` raises
  ``msgspec.ValidationError: Invalid enum value 'SPOT' - at `$.spot_account_mode` ``. The two enums
  live in the same config class and take opposite casing.
- `instrument_provider.load_ids` cannot be given here at all. `ImportableConfig.create()` decodes
  without a hook, so string ids raise ``msgspec.ValidationError: Expected
  `nautilus_trader.model.identifiers.InstrumentId`, got `str` - at
  `$.instrument_provider.load_ids[0]` ``. That is a platform limitation rather than an adapter one.
  Use `load_all` with `filters`, or build the config in Python.

The factories cache one HTTP transport and one instrument provider per set of credentials,
environment, products and provider config, so two clients configured identically share one
connection pool and one instrument load.

## Configuration

[docs/configuration.md](docs/configuration.md) is the field reference for both classes. Four
defaults decide behavior:

| Setting                            | Default     | Consequence                                                                                                          |
|------------------------------------|-------------|----------------------------------------------------------------------------------------------------------------------|
| `environment` (both clients)       | `"mainnet"` | A node holding valid credentials trades for real unless it says otherwise. 0.1.0 defaulted execution to the testnet. |
| `products`                         | `(SPOT,)`   | Adding any derivative also changes the account type to `MARGIN`.                                                     |
| `spot_account_mode`                | `SPOT`      | The margin ledgers need a venue-side account type.                                                                   |
| `update_instruments_interval_mins` | `60`        | Instruments are polled, since Gate.io publishes no instrument channel.                                               |

One platform-side setting matters as much as those: keep NautilusTrader's own
`LiveExecEngineConfig.open_check_open_only=True` (its default). With it set to `False`, an order
whose listing row cannot be read is counted missing on every cycle and, once
`open_check_missing_retries` is exhausted, is resolved by the engine with a fabricated rejection or
cancellation. It is set on the node, not on this adapter:

```python
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.config import LiveExecEngineConfig

config = TradingNodeConfig(
    trader_id="GATEIO-001",
    exec_engine=LiveExecEngineConfig(open_check_open_only=True),
    # data_clients and exec_clients as above
)
```

## What will bite you

Ordered by how soon you are likely to meet it.

- **Cancel-all on stop is not proven.** Four recorded shutdown runs canceled everything resting when
  the wind-down began; two of them then submitted one more order that was still at the venue when
  the run ended. Verify the account after a stop rather than trusting the sweep
  ([validation.md](docs/validation.md)).
- **A spot market buy closes `CANCELED`.** Read `filled_qty`, not the terminal status
  ([execution.md](docs/execution.md#fills)).
- **Deltas and depth on the same instrument overwrite each other.** NautilusTrader caches one
  `OrderBook` per instrument and `apply_depth` replaces it, so a depth message discards every delta
  level below the tenth. The client warns when it sees both held and subscribes anyway: the book
  belongs to the engine, and the adapter cannot repair it from here. Take one of the two per
  instrument.
- **A refused request never completes.** Gate.io publishes no quote history, so `request_quote_ticks`
  is refused with a log line naming the venue fact, and so is a request for any venue-native data
  type. The platform closes a request group on a response, so the awaiting historical-data callback
  never fires either way. Read the log line, and use the live quote subscription or an order book
  snapshot instead ([market-data.md](docs/market-data.md#requests)).
- **There is no local order kill switch.** The 0.1.0 `live_orders` flag is gone: a boolean inside the
  process is not a security boundary, since the process holds the key either way. What binds is the
  key's permissions, the IP allowlist, `environment`, and NautilusTrader's own sandbox and backtest
  execution ([configuration.md](docs/configuration.md)).
- **Report paging stops at 20 pages, 2000 rows**, and logs a warning naming the cap. An account with
  more open orders or fills than that in one listing window reports a truncated view
  ([execution.md](docs/execution.md#the-paging-bound-stated-honestly)).
- **The testnet covers spot and USDT perpetuals only.** Inverse perpetuals, delivery futures and
  options have no testnet endpoint, so their first real run is on mainnet.
- **Brackets, OCO and OTO are denied here, every leg.** Gate.io's attached take-profit and stop-loss
  carry no client-supplied id for the attached leg, so those legs could never be addressed
  afterwards. Give any leg an `emulation_trigger` and NautilusTrader's own emulator holds the
  contingency and sends this client the plain orders it releases
  ([products.md](docs/products.md#order-emulation)).
- **Delivery and options orders cannot be amended.** The venue has no amend endpoint there, so
  modification is refused explicitly rather than emulated by cancel and replace.
- **Options take no conditional orders.** Gate.io publishes no price-trigger endpoint for them.
- **Contracts with decimal sizes (`enable_decimal`) are refused loudly** rather than truncated to a
  whole lot count.
- **A halt shorter than `update_instruments_interval_mins` is invisible.** Gate.io publishes no
  instrument-status channel, so status is polled from the listings on that interval, 60 minutes by
  default.
- **A fired spot conditional order is resolved by re-reading the venue's armed price orders**,
  because Gate.io carries no client order id on that path.
- **The position staleness memory is one restart deep**, and a compensating trade stamped in the same
  second as the venue's answer is withheld until the venue produces a distinguishable row
  ([review-matrix.md](docs/review-matrix.md#residual-risks)).

[docs/troubleshooting.md](docs/troubleshooting.md) covers the failures that have actually happened,
including `INVALID_SIGNATURE`, `USER_NOT_FOUND`, `FORBIDDEN`, orders stuck in flight and books that
keep resynchronizing.

## Feature support matrix

`✓` means the capability is implemented and covered by the offline test suite. `-` means it is not
available on that product. The **Mainnet** column is a different question: it names the products for
which a run against the real exchange is recorded in [docs/validation.md](docs/validation.md), and
`-` there means the venue has never seen that path. No row is marked stable, because a single
recorded run is not evidence of stability; see
[why nothing is marked Stable](docs/validation.md#why-nothing-is-marked-stable).

### Market data

| Feature                               | Spot | Perp | Inverse | Delivery | Options | Mainnet        | Notes                                                                                                                                                           |
|---------------------------------------|------|------|---------|----------|---------|----------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Trade ticks                           | ✓    | ✓    | ✓       | ✓        | ✓       | Spot           | `*.trades`. The venue trade id is preserved.                                                                                                                    |
| Quote ticks (real BBO)                | ✓    | ✓    | ✓       | ✓        | ✓       | Spot           | `*.book_ticker`. No quote is synthesized anywhere.                                                                                                              |
| Order book deltas                     | ✓    | ✓    | ✓       | ✓        | ✓       | Spot           | REST snapshot plus incremental stream, sequence-validated, resynchronized on a gap.                                                                             |
| Order book depth (`OrderBookDepth10`) | ✓    | ✓    | ✓       | ✓        | ✓       | -              | The venue's periodic `*.order_book` snapshot channel, ten levels per side.                                                                                      |
| Order book snapshot request           | ✓    | ✓    | ✓       | ✓        | ✓       | Spot           | Depth is clamped to what the product accepts.                                                                                                                   |
| Bars (closed only)                    | ✓    | ✓    | ✓       | ✓        | ✓       | Spot           | 1s to 7d. Delivery and options infer the close.                                                                                                                 |
| Historical bars and trades            | ✓    | ✓    | ✓       | ✓        | ✓       | Spot           | Paginated REST, 1000 rows per call. Offline coverage reaches the HTTP layer only.                                                                               |
| Mark price                            | -    | ✓    | ✓       | ✓        | ✓       | USDT perpetual | `futures.tickers`; `options.contract_tickers` on options.                                                                                                       |
| Index price                           | -    | ✓    | ✓       | ✓        | ✓       | USDT perpetual | Sources as for the mark price.                                                                                                                                  |
| Funding rate                          | -    | ✓    | ✓       | -        | -       | USDT perpetual | Perpetuals only. From `futures.tickers`.                                                                                                                        |
| Historical funding rates              | -    | ✓    | ✓       | -        | -       | USDT perpetual | REST `/futures/{settle}/funding_rate`. Offline coverage reaches the HTTP layer only.                                                                            |
| Instrument updates                    | ✓    | ✓    | ✓       | ✓        | ✓       | -              | Polled. The initial load is mainnet-confirmed; the periodic reload is not.                                                                                      |
| Instrument status                     | ✓    | ✓    | ✓       | ✓        | ✓       | -              | Polled from the instrument listings. Gate.io publishes no status channel.                                                                                       |
| Instrument close                      | -    | -    | -       | ✓        | ✓       | -              | Settlement after expiry. The three continuous products never settle.                                                                                            |
| `GateioTicker` custom data            | ✓    | ✓    | ✓       | ✓        | ✓       | -              | The ticker fields the platform has no type for: 24-hour statistics, greeks, implied volatilities, basis.                                                        |
| Historical quotes                     | -    | -    | -       | -        | -       | -              | *Not supported.* Gate.io publishes no quote history; the request is refused.                                                                                    |
| Options underlying streams            | -    | -    | -       | -        | ✓       | -              | Reachable through the raw WebSocket client, not wired into the data engine. The greeks reach a strategy as `GateioTicker` fields rather than as `OptionGreeks`. |

### Instruments

| Feature                                   | Mainnet                                        | Notes                                                                                |
|-------------------------------------------|------------------------------------------------|--------------------------------------------------------------------------------------|
| Spot `CurrencyPair`                       | Spot                                           | Precision, minimums, account fee tier.                                               |
| `CryptoPerpetual`, linear and inverse     | USDT perpetual                                 | Contract-count quantities, `quanto_multiplier`. The inverse variant has no live run. |
| `CryptoFuture` (delivery)                 | Delivery                                       | Activation and expiration read from the contract.                                    |
| `CryptoOption`                            | Options                                        | Strike and kind parsed from the symbol.                                              |
| Multi-product provider with filters       | Spot, perpetual, delivery, options (load only) | Per-product degradation on an unprovisioned wallet has no live run.                  |
| Rejection of unrepresentable price scales | -                                              | A quantized zero is never published as a venue price.                                |

### Execution

| Feature                                    | Spot | Perp | Inverse | Delivery | Options | Mainnet              | Notes                                                                                         |
|--------------------------------------------|------|------|---------|----------|---------|----------------------|-----------------------------------------------------------------------------------------------|
| `MARKET`                                   | ✓    | ✓    | ✓       | ✓        | ✓       | Spot, USDT perpetual | On derivatives an immediate order at price 0. FOK is refused on options.                      |
| `LIMIT` (GTC, IOC, FOK)                    | ✓    | ✓    | ✓       | ✓        | ✓       | Spot, options        | Options have no `fok`, so FOK is refused there.                                               |
| Post-only, GTC only                        | ✓    | ✓    | ✓       | ✓        | ✓       | Spot                 | Gate.io `poc`. Post-only with IOC or FOK is refused rather than downgraded.                   |
| `STOP_MARKET`, `STOP_LIMIT`                | ✓    | ✓    | ✓       | ✓        | -       | Spot, USDT perpetual | Arming and canceling are confirmed; nothing has fired in a recorded run.                      |
| `MARKET_IF_TOUCHED`, `LIMIT_IF_TOUCHED`    | ✓    | ✓    | ✓       | ✓        | -       | Spot, USDT perpetual | As above. Spot is confirmed on the buy side only.                                             |
| Reduce-only                                | -    | ✓    | ✓       | ✓        | ✓       | USDT perpetual       | Refused on spot rather than dropped; it is a derivatives concept.                             |
| Iceberg (`display_qty`)                    | ✓    | ✓    | ✓       | ✓        | ✓       | Spot                 | A fully hidden order (`display_qty=0`) is refused.                                            |
| Quote-denominated quantity                 | ✓    | -    | -       | -        | -       | Spot                 | Spot market buy only. The order closes `CANCELED` on the venue's base total.                  |
| Order lists, no contingency                | ✓    | ✓    | ✓       | ✓        | ✓       | -                    | Batched where Gate.io has a batch endpoint and the group fits; otherwise one order at a time. |
| Order lists with a contingency             | -    | -    | -       | -        | -       | -                    | *Not supported.* Bracket, OCO and OTO are denied in full, every leg. Use order emulation.     |
| Cancel, cancel-all, batch cancel           | ✓    | ✓    | ✓       | ✓        | ✓       | Spot, options        | The batch endpoint has no live run; every recorded cancel went out per order.                 |
| Modify (amend)                             | ✓    | ✓    | ✓       | -        | -       | Spot                 | Delivery and options have no amend endpoint. *Not supported* there.                           |
| Private WebSocket order and fill lifecycle | ✓    | ✓    | ✓       | ✓        | ✓       | -                    | Recorded runs confirm the outcomes, not this transport in isolation.                          |
| Order, fill and position reports           | ✓    | ✓    | ✓       | ✓        | ✓       | Spot, USDT perpetual | A fresh node read an open perpetual position back. Order adoption has no live run.            |
| Internal wallet transfers                  | ✓    | ✓    | ✓       | ✓        | ✓       | -                    | Between the account's own trading wallets only.                                               |
| Hedge (dual) position mode                 | -    | -    | -       | -        | -       | -                    | *Not supported.* Detected at connect and refused, never switched.                             |

### Accounts and margin

| Feature                                                            | Mainnet                 | Notes                                                                                     |
|--------------------------------------------------------------------|-------------------------|-------------------------------------------------------------------------------------------|
| Cash account (spot only, plain ledger)                             | Spot                    | Every recorded spot order ran on this account type.                                       |
| Margin account (any other combination)                             | USDT perpetual, options | One Nautilus account, wallets aggregated per currency.                                    |
| Isolated margin ledger                                             | -                       | `spot_account_mode=MARGIN`.                                                               |
| Cross margin ledger                                                | -                       | Requires a unified account at the venue.                                                  |
| Unified account                                                    | -                       | Venue thresholds: 500 USDT for `multi_currency`, 1000 for `portfolio`. Not enforced here. |
| `Strategy.query_account()`                                         | -                       | Re-reads every enabled product's wallet over REST. Names the wallets it could not read.   |
| Borrow and repay endpoints                                         | -                       | Exposed because isolated and cross margin need them. Every one says so in its docstring.  |
| Withdrawals, sub-accounts, Earn, Gate Pay, P2P, Copy Trading, Bots | -                       | *Not supported.* Out of scope, and no code exists for them.                               |

## Validation

Recorded against Gate.io mainnet on 2026-07-29, at the smallest size each instrument permits: the
public market-data paths on four product families, the whole spot order lifecycle including both
time-in-force families and the quote-denominated market buy, cancel-replace and cancel-all, spot
conditional orders on the buy side, one USDT perpetual (both position sides, reduce-only and its
refusal, armed conditional orders, and a position read back out of the venue by a node that had
never seen it), and three orders on one option contract.

Not recorded: any order on an inverse perpetual or a delivery contract, anything on a margin,
cross-margin or unified spot ledger, a restart of a node onto its own live state, the batch-cancel
endpoint, any conditional order actually firing, and anything at all on the testnet. Two of four
shutdown runs left an order resting at the venue. Nothing added to the clients since the `0.2.0a1`
release has been run against the venue at all.

[docs/validation.md](docs/validation.md) has every run, what each one checked and did not check, the
steps that failed, and the three checks that do not check what they claim.

## Examples

Run them from the repository root. Each is standalone and prints what it did.

| Example                                                           | What it shows                                                                                                     | Needs                                              |
|-------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|----------------------------------------------------|
| [`01_public_rest.py`](examples/01_public_rest.py)                 | The REST transport, one typed namespace per product, with the live delivery and option listings.                  | Nothing.                                           |
| [`02_public_websocket.py`](examples/02_public_websocket.py)       | `GateioPublicWebSocket` on two products at once, with transport counters.                                         | Nothing.                                           |
| [`03_instruments.py`](examples/03_instruments.py)                 | All five instrument classes built from the venue, and what a `Quantity` means on each.                            | Nothing.                                           |
| [`04_trading_node_data.py`](examples/04_trading_node_data.py)     | A `TradingNode` with quotes, trades and bars for a spot pair and a perpetual.                                     | Nothing.                                           |
| [`05_account_readonly.py`](examples/05_account_readonly.py)       | Fee tier, per-product wallets including one that does not exist yet, resting orders. All GETs.                    | API credentials.                                   |
| [`06_testnet_orders.py`](examples/06_testnet_orders.py)           | A spot testnet order round trip through the REST namespace rather than a node.                                    | Testnet credentials and `GATEIO_ALLOW_ORDERS=YES`. |
| [`07_trading_node_orders.py`](examples/07_trading_node_orders.py) | One spot testnet order through a `TradingNode`: both clients, both factories, a post-only limit canceled on stop. | Testnet credentials and `GATEIO_ALLOW_ORDERS=YES`. |

The switches those scripts read are their own, not adapter features: `GATEIO_ENVIRONMENT` selects
the environment in example 05 and defaults to `testnet` there, the opposite of the adapter's mainnet
default, because an example that reads a live account should say which account it means.
`GATEIO_ALLOW_ORDERS` must be exactly `YES` for examples 06 and 07 to place anything.

## Documentation

| Page                                                    | Contents                                                                        |
|---------------------------------------------------------|---------------------------------------------------------------------------------|
| [architecture.md](docs/architecture.md)                 | Package layout, dataflows, and why this adapter is pure Python.                 |
| [symbology.md](docs/symbology.md)                       | Venue string, instrument ids, why only perpetuals get a suffix.                 |
| [products.md](docs/products.md)                         | Spot, margin (isolated, cross, unified), perpetual, inverse, delivery, options. |
| [configuration.md](docs/configuration.md)               | Complete field reference for both config classes.                               |
| [market-data.md](docs/market-data.md)                   | Subscriptions, requests, order book synchronization.                            |
| [execution.md](docs/execution.md)                       | Order translation, every rejection path, reconciliation.                        |
| [errors.md](docs/errors.md)                             | Every exception raised on purpose, and who handles it.                          |
| [validation.md](docs/validation.md)                     | What has been exercised against the venue, and where.                           |
| [review-matrix.md](docs/review-matrix.md)               | What was audited during the rework, and the residual risks.                     |
| [troubleshooting.md](docs/troubleshooting.md)           | Real failure modes and their fixes.                                             |
| [migration-0.1-to-0.2.md](docs/migration-0.1-to-0.2.md) | Every breaking change from 0.1.0.                                               |
| [testing.md](docs/testing.md)                           | Running the suite, coverage areas, rules for credentialed tests.                |
| [roadmap.md](docs/roadmap.md)                           | Where the project is going, stage by stage.                                     |
| [releasing.md](docs/releasing.md)                       | Release checklist.                                                              |
| [examples/README.md](examples/README.md)                | The example scripts and what each one needs.                                    |

## Testing

```bash
pip install -e '.[dev]'
pytest
```

The suite is offline in full: no network, no credentials. The `integration` marker is registered and
deselected by default (`addopts = "-m 'not integration'"`), and no test carries it yet, so live
behavior is recorded by hand in [docs/validation.md](docs/validation.md) rather than asserted by the
suite. The rules a credentialed test must follow are in [docs/testing.md](docs/testing.md).

## Contributing

Bug reports, corrections, tests and features are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) for
the workflow and the test requirements. Release history is in [CHANGELOG.md](CHANGELOG.md).
Validation results are the most valuable contribution this project can receive: a pull request
adding a row to [docs/validation.md](docs/validation.md) for a path the venue has never confirmed
here moves it further than most code changes.

## Security

[SECURITY.md](SECURITY.md) carries the disclosure policy, what the adapter does with credentials,
and what identifies you in a log. Report a vulnerability privately through GitHub's advisory form
rather than in a public issue.

## Support the project

This adapter is maintained in free time. A well-written issue or a pull request is worth more than a
star. Donations are optional and carry no benefits or obligations.

<details>
<summary>Donation addresses</summary>

| Asset | Network          | Address                                            |
|-------|------------------|----------------------------------------------------|
| BTC   | Bitcoin Mainnet  | `bc1qrzw790us8sen3wh7kl07yntvvkaa6upxk6jke9`       |
| ETH   | Ethereum Mainnet | `0xcC3b21D33abA753dcbEA96AB823fD22b8B5C444D`       |
| USDT  | TRON (TRC20)     | `TAiVKz7LveKsqxeG8jnnang6fmpt8SX8Fq`               |
| USDT  | TON Network      | `UQA5cxIn0YIkezeOFqFQa0t7pIzQl1svhhm8w09Q9DUPtPRm` |
| USDC  | Base Network     | `0xcC3b21D33abA753dcbEA96AB823fD22b8B5C444D`       |

</details>

## License

MIT, see [LICENSE](LICENSE).

NautilusTrader is a dependency of this package and is licensed under
[LGPL-3.0](https://github.com/nautechsystems/nautilus_trader/blob/master/LICENSE). This package
contains no NautilusTrader source code; it imports the library through its public API.

## Disclaimer

This is an unofficial, community-maintained project. It is not affiliated with, maintained by or
endorsed by Gate.io or Nautech Systems. All trademarks belong to their respective owners.

Trading cryptocurrencies involves substantial risk of loss and is not suitable for everyone. This
software is provided "as is", without warranty of any kind; see the [LICENSE](LICENSE) for the full
terms. You are solely responsible for safeguarding your API credentials, for every order the
software places on your accounts, and for your compliance with the Gate.io Terms of Service and any
regulations that apply to you.
