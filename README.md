# Gate.io Adapter for NautilusTrader

[![CI](https://github.com/x03f/gateio-nt-community/actions/workflows/ci.yml/badge.svg)](https://github.com/x03f/gateio-nt-community/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![NautilusTrader 1.230+](https://img.shields.io/badge/nautilus__trader-1.230%2B-orange)](https://github.com/nautechsystems/nautilus_trader)

A community-maintained adapter connecting
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) to
[Gate.io](https://www.gate.io/): market data and order execution across spot, margin, perpetual
futures (linear and inverse), delivery futures and options, over the Gate.io v4 REST and WebSocket
API. It is an external pip-installable package rather than part of the NautilusTrader repository,
This is an independent community project: it is not affiliated with, endorsed by, or supported
by Nautech Systems Pty Ltd or the official NautilusTrader project, nor by Gate.io.

- Market data comes from the venue's own streams and listings: trades, best bid and offer,
  sequence-validated book deltas, ten-level depth from the periodic snapshot channel, closed bars,
  mark and index prices, funding rates, instrument status and settlement closes. Nothing is
  synthesized or interpolated: where Gate.io publishes no history, the request is refused rather
  than answered from a current row.
- One data client and one execution client multiplex every product family.
- An order Gate.io cannot express is denied with a stated reason before any request is sent. It is
  never converted into a different order, and never reported as a venue rejection the venue did not
  make.
- All four NautilusTrader report generators are implemented against REST and asserted by the offline
  suite against recorded venue payloads. What a live restart has shown is narrower: one recorded run
  read an open perpetual position back into a node that had never seen it, and no run has adopted a
  resting order or restarted a node onto its own live state. Restart recovery therefore sits below
  the top rung of the [evidence ladder](docs/validation.md#the-evidence-ladder), which is the one
  vocabulary these pages grade evidence in.

## Quickstart

Public market data needs no account, so the shortest path from install to data involves no
credentials at all.

```bash
pip install "gateio-nt-community @ git+https://github.com/x03f/gateio-nt-community"
```

That line installs the default branch, which is ahead of the published release; pinning the release
tag instead, and everything else about versions, is under
[Requirements and installation](#requirements-and-installation).

```python
import asyncio

from gateio_nt import GateioFuturesHttpAPI, GateioHttpClient, GateioSpotHttpAPI


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

```text
spot precision  : 1 / 6
spot top of book: 62811.4 / 62811.5
perp multiplier : 0.0001
```

The figures are whatever the venue is publishing when you run it; what matters is that three public
endpoints answered without a key and without a Nautilus node.

Where to go from there:

| Next                                | Where                                                                                             | Needs                             |
|-------------------------------------|---------------------------------------------------------------------------------------------------|-----------------------------------|
| The same data inside a node         | [A live `TradingNode`](#a-live-tradingnode)                                                       | Nothing                           |
| Read a real account, place no order | [Credentials](#credentials), [`examples/05_account_readonly.py`](examples/05_account_readonly.py) | A testnet key and secret, or `GATEIO_ENVIRONMENT=mainnet` with a mainnet pair |
| One order, on the testnet           | [One order on the testnet](#one-order-on-the-testnet)                                             | A testnet key and secret          |
| What the venue has actually seen    | [Status](#status), [docs/validation.md](docs/validation.md)                                       | Nothing                           |
| What surprises people first         | [What will bite you](#what-will-bite-you)                                                         | Nothing                           |

This is alpha software that places real orders with real money by default: `environment` is
`"mainnet"` on both clients unless you say otherwise. Read [Status](#status) and
[Before your first order](#before-your-first-order) before the first order, not after it.

## Status

**Alpha.** A build of this branch reports `0.2.0a2.dev0`; the published release reports `0.2.0a1`.
Both require `nautilus_trader>=1.230.0,<2` and Python `>=3.12,<3.15`, and CI runs the suite on 3.12,
3.13 and 3.14. Where a page names an interpreter — the platform behaviors verified in
[docs/configuration.md](docs/configuration.md) — it is 3.13, because that is the version those
readings were taken on, not a version this package requires.

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
keeps the two apart, and so does the version: `0.2.0a2.dev0` is a build of this branch, `0.2.0a1` is
the release.

Interfaces may change between 0.x releases. Upgrading from 0.1.0 is a port rather than a version
bump: read [docs/migration-0.1-to-0.2.md](docs/migration-0.1-to-0.2.md) first, because the venue
string, the instrument ids and the execution environment default all changed.

## Requirements and installation

| Adapter                       | nautilus_trader | Python         | Gate.io API |
|-------------------------------|-----------------|----------------|-------------|
| `main` (`0.2.0a2.dev0`)       | `>=1.230.0,<2`  | `>=3.12,<3.15` | v4          |
| `v0.2.0a1` (the released tag) | `>=1.230.0,<2`  | `>=3.12,<3.15` | v4          |

```bash
pip install "gateio-nt-community @ git+https://github.com/x03f/gateio-nt-community"
```

That line installs the **default branch**, which is ahead of the published release, reports
`0.2.0a2.dev0`, and is what the pages here describe. To install the release instead, pin the tag:

```bash
pip install "gateio-nt-community @ git+https://github.com/x03f/gateio-nt-community@v0.2.0a1"
```

The package is not on PyPI, so a bare `pip install gateio-nt-community` finds nothing, and
neither does `gateio-nt-community==0.2.0a1`: the name is an install name rather than a
PyPI name, and that version is a git tag rather than a PyPI release. The two builds report different
versions — `0.2.0a2.dev0` from the branch, `0.2.0a1` from the tag. A development release sorts after
the release it followed and before the release it is working toward
([PEP 440](https://peps.python.org/pep-0440/)), which is what the branch is, so `__version__` says
which side of the release a build sits on. It does not say which commit: the branch moves, and every
build of it reports the same `0.2.0a2.dev0`. For that, `pip freeze` appends the commit it installed,
and the release commit is `0e0814f`:

```bash
pip freeze | grep gateio
# gateio-nt-community @ git+https://github.com/x03f/...@0e0814f5818011...
```

`nautilus_trader` is a declared dependency, so pip will pull it in. It is a large wheel, and on a
platform with no wheel it is a long source build; installing it into the target environment first
makes any failure there easier to read.

Check that the two versions line up before anything else:

```bash
python -c "import gateio_nt, nautilus_trader; print(gateio_nt.__version__, nautilus_trader.__version__)"
# 0.2.0a2.dev0 1.230.0        (0.2.0a1 if you pinned the tag)
```

For development:

```bash
git clone https://github.com/x03f/gateio-nt-community
cd gateio-nt-community
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
(`GATEIO_ENVIRONMENT`, `GATEIO_ALLOW_ORDERS`), the package is `gateio_nt`, the venue string is
`GATE_IO`, and every constant in the code is `GATEIO_*`. `GATEIO_API_KEY` is read by nothing.

Nothing checks at startup that credentials are present, because their absence is a valid state:
public market data needs none. A data client without a key runs and looks healthy. An execution
client without a key also builds and reaches `READY`, and then fails at its first signed request
with `MISSING_CREDENTIALS` (`gateio_nt/http/client.py`). A misspelled variable name produces
exactly that failure, and what you see is a node that waits a minute and then reports
`Timed out (60.0s) waiting for engines to connect and initialize`, with the real cause one `[ERROR]`
line above it ([troubleshooting.md](docs/troubleshooting.md#the-node-hangs-for-a-minute-and-then-reports-a-timeout)).

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

| What you configure                           | REST sections called            |
|----------------------------------------------|---------------------------------|
| any execution client, at startup             | `/wallet/fee`, then `/spot/fee` |
| `SPOT`                                       | `/spot/*`                       |
| `spot_account_mode=MARGIN` or `CROSS_MARGIN` | `/margin/*`                     |
| `spot_account_mode=UNIFIED`                  | `/unified/*`                    |
| `PERP`, `INVERSE`                            | `/futures/{settle}/*`           |
| `FUT`                                        | `/delivery/{settle}/*`          |
| `OPT`                                        | `/options/*`                    |
| `transfer()`                                 | `/wallet/*` transfers           |

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

The [quickstart](#quickstart) above is this path: one typed namespace per product family over one
shared transport, no credentials anywhere. The REST transport and the WebSocket clients work without
a Nautilus node, which is what [`examples/01_public_rest.py`](examples/01_public_rest.py) and
[`examples/02_public_websocket.py`](examples/02_public_websocket.py) show.

### A live `TradingNode`

```python
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from gateio_nt import (
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

This type is on the branch only. A build pinned to `v0.2.0a1` — anything whose `__version__` reads
`0.2.0a1` — has no `GateioTicker`, and the import below raises `ImportError` there.

```python
from nautilus_trader.model.data import DataType

from gateio_nt import GATEIO_CLIENT_ID, GateioTicker

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
  a separate testnet account and are not the mainnet ones: the testnet is served at
  `https://testnet.gate.com`, and the account and its key are created there. The API host the
  adapter talks to, `https://api-testnet.gateapi.io`, is not a page you can register on.
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
  the account owner can do. The adapter never changes an account setting for you. `UNIFIED` also
  needs `SPOT` among `products` — the unified ledger is read only while sweeping the spot wallet, so
  the execution client refuses to construct without it.
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
  with an explanatory error, never switched off for you. So is an answer that does not establish the
  mode — a blank `position_mode`, or a wallet the key may not read (`FORBIDDEN`) — because an unread
  mode is not a one-way mode. The one exception is a futures wallet Gate.io has not created yet
  (`USER_NOT_FOUND`): it holds no positions, so that product is skipped with a warning and the rest
  are still checked. See [docs/execution.md](docs/execution.md#account-routing).

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

from gateio_nt import (
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
from gateio_nt import GATEIO, GateioLiveDataClientFactory, GateioLiveExecClientFactory

node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
```

A declarative node config can carry the client configuration instead, which is how a pip-installed
adapter is wired into a node without being imported in your own code. The registration above does
not go away on that path: `nautilus_trader` 1.230.0 cannot build the execution client from a
declared factory, so `node.add_exec_client_factory(...)` stays in Python while the rest of the entry
is data. State `environment` on **both** entries — they are separate configurations and each
defaults to mainnet, which is how a node ends up trading in one environment while watching prices
from another ([configuration.md](docs/configuration.md#the-environment-default-is-mainnet)).

[docs/configuration.md](docs/configuration.md) is the reference for that path, under *Registering
from a declarative config*: a full example, why the execution factory cannot be declared, the casing
each of the two enums takes, and the one field a declarative config cannot express at all.

The factories share one HTTP transport and one instrument provider between clients configured alike,
so a data client and an execution client in the same node use one connection pool and one instrument
load. Each cache holds exactly one entry — `functools.lru_cache(1)` in
[`gateio_nt/factories.py`](gateio_nt/factories.py), matching the adapters bundled with
NautilusTrader. The transport is keyed by credentials, base URL, timeout and retry count; `products`
is not part of that key, and the instrument provider is keyed separately, by transport, products,
option underlyings and provider config. A second, differently configured transport therefore evicts
the first rather than joining it: invisible within one node, visible to a process that builds nodes
for two environments
([architecture.md](docs/architecture.md#shared-reference-counted-rest-transport)).

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
including the 60-second startup timeout a missing credential produces,
`INVALID_SIGNATURE`, `USER_NOT_FOUND`, `FORBIDDEN`, orders stuck in flight and books that keep
resynchronizing.

## Feature support matrix

These tables grade capability; how well a capability is *proven* is graded in one place only, the
[evidence ladder](docs/validation.md#the-evidence-ladder) in
[docs/validation.md](docs/validation.md), and this page defines no scale of its own.

`✓` means the capability is implemented and the offline suite asserts it against recorded venue
payloads — the ladder's *unit-tested* rung at least, higher where that page says so, and the Notes
cell says where offline coverage stops short of the client. `-` means it is not available on that
product. The **Mainnet** column is the ladder's top rung, per product: it names the products for
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
| Hedge (dual) position mode                 | -    | -    | -       | -        | -       | -                    | *Not supported.* Only a one-way answer starts the client; hedge mode and an unreadable mode are both refused at connect, never switched. |

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

This is an independent community project. It is not affiliated with, endorsed by, or supported by
Nautech Systems Pty Ltd or the official NautilusTrader project, and it is not affiliated with,
endorsed by, or supported by Gate.io. All trademarks belong to their respective owners.

Trading cryptocurrencies involves substantial risk of loss and is not suitable for everyone. This
software is provided "as is", without warranty of any kind; see the [LICENSE](LICENSE) for the full
terms. You are solely responsible for safeguarding your API credentials, for every order the
software places on your accounts, and for your compliance with the Gate.io Terms of Service and any
regulations that apply to you.
