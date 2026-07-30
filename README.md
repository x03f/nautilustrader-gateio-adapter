# Gate.io Adapter for NautilusTrader

[![CI](https://github.com/x03f/nautilustrader-gateio-adapter/actions/workflows/ci.yml/badge.svg)](https://github.com/x03f/nautilustrader-gateio-adapter/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![NautilusTrader 1.230+](https://img.shields.io/badge/nautilus__trader-1.230%2B-orange)](https://github.com/nautechsystems/nautilus_trader)

A community-maintained, **unofficial** exchange adapter connecting
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) to
[Gate.io](https://www.gate.io/): market data and order execution across spot,
margin, perpetual futures (linear and inverse), delivery futures and options,
over the Gate.io API v4 REST and WebSocket interfaces.

This project is not affiliated with, maintained by, or endorsed by Gate.io or
Nautech Systems. Current maturity is **alpha (v0.2.0a1)**, developed against
`nautilus_trader` 1.230.0 on Python 3.13.

Upgrading from 0.1.0? Read the
[migration guide](docs/migration-0.1-to-0.2.md) first — the venue string, the
instrument ids and the execution environment default all changed.

## What it does

* **Real market data only.** Trades, best bid/offer quotes, sequence-validated
  order book deltas, ten-level depth from the venue's periodic snapshot channel,
  closed bars, mark and index prices, funding rates, instrument status and
  settlement closes — all from the venue's own streams and listings. Nothing is
  synthesised or interpolated: where Gate.io publishes no history, the request is
  refused rather than answered from a current row.
* **Every tradable product.** One data client and one execution client
  multiplex spot, USDT perpetuals, BTC-settled perpetuals, USDT delivery futures
  and USDT-settled options.
* **Execution that never lies about your order.** Six order types, four
  time-in-force values, post-only, reduce-only and iceberg. Anything Gate.io
  cannot express is denied with a stated reason before anything is sent — never
  silently converted into a different order, and never reported as a venue
  rejection the venue did not make.
* **Real reconciliation.** All four NautilusTrader report generators are
  implemented against REST, so a restart with resting orders and open positions
  is a supported path. Nine recovery defects have been found and closed over
  the rework, the last two by live runs rather than by review: the
  unapplied-fill sweep now runs on the restart route as well as the reconnect
  route, a position row this client cannot parse fails the query instead of
  reporting flat, a failed fill query is raised to the engine, a stale position
  answer can no longer erase a position the venue still holds, and a trade
  whose order the engine declined to adopt no longer crashes startup
  reconciliation. [execution.md](docs/execution.md) states each mechanism, the
  residual risks, and the repair that was tried first and withdrawn;
  [review-matrix.md](docs/review-matrix.md) carries all nine. A node that had
  never seen the account has read an open perpetual position back out of the
  venue and traded it flat; the rest is offline-proven only.
* **A cash buy ends `CANCELED`, not `FILLED`.** Gate.io denominates a spot
  market buy in the quote currency and states the base quantity it bought only
  when the order finishes, and NautilusTrader offers no way to move an order to
  `FILLED` against a quantity restated after its fills. The order is therefore
  closed on the venue's own figure with `OrderCanceled`, which preserves the
  filled quantity — **read the outcome from `filled_qty` and the resulting
  position, never from the terminal status.** The alternative was an estimated
  quantity that could leave the order open for ever or make the engine discard
  a fill; see [execution.md](docs/execution.md#fills).
* **Usable standalone.** The async REST transport with its typed per-product
  namespaces, and the self-healing WebSocket clients, work without a Nautilus
  node.

## Status

**Alpha — `0.2.0a1`.** An external community adapter, written in pure Python,
built against NautilusTrader 1.230.0. Not an official NautilusTrader
integration, and not affiliated with Gate.io.

The package is complete and covered by an extensive offline test suite, but a
passing suite is evidence about the code, not about the exchange. **Mainnet
validation covers market data and spot execution**: the public streams and
requests, the whole spot order lifecycle, spot time in force including the
quote-denominated market buy, cancel-replace, cancel-all and a conditional buy
armed and cancelled. Several recorded steps failed and are written down beside
the successes in [docs/validation.md](docs/validation.md) — including two runs
that ended with an order still resting at the venue, after cancelling everything
that had been resting when they began to stop.

On the derivative side the venue has seen orders on one USDT perpetual and one
option contract. The perpetual carried both position sides, the reduce-only flag
and its refusal, conditional orders armed and re-armed without firing, and a
long that a second node read back out of the venue and flattened. The option
took a resting limit buy, an aggressive one that filled, and a limit sell
covered by the resulting long. **Everything else on the derivatives is offline
evidence only** — inverse perpetuals and delivery futures have never had an
order sent, and neither has any margin, cross-margin or unified spot ledger.
Nothing is marked *Stable*: one recorded run shows that a path works, not that
it keeps working, and one shutdown path here came out two ways in four runs of
the same code.

Use it for evaluation and controlled use. Start on the testnet, then start
small, and verify anything you are about to trust with money.

NautilusTrader prefers a Rust core with a thin PyO3 layer for adapters it ships
in-tree. This one is Python throughout. That is a deliberate, stated deviation
for an external package rather than an oversight, and it does not exempt the
adapter from any behavioural requirement — see
[docs/architecture.md](docs/architecture.md). A Rust migration is a possible
future project and is not being promised.

`0.1.0` was the previous experimental, primarily spot-oriented implementation.
`0.2.0a1` is a substantial redesign rather than an incremental patch; the
upgrade path is in
[docs/migration-0.1-to-0.2.md](docs/migration-0.1-to-0.2.md). The old release
remains available: its tag is untouched and its code is preserved on the
`legacy/v0.1.0` branch.

What was audited during the rework, and what remains, is published in
[docs/review-matrix.md](docs/review-matrix.md).

Interfaces may change between 0.x releases. Correct operation is the goal;
economic results are never guaranteed — see the [Disclaimer](#disclaimer).

## Feature support matrix

Two columns, two different questions. **Status** grades the code and the offline
test suite. **Mainnet** names the products for which a run against the real
exchange is recorded in [docs/validation.md](docs/validation.md); `—` means the
venue has never seen that path.

| Status | Meaning |
|---|---|
| **Stable** | Unit-tested, exercised on mainnet, and shown to keep working there |
| **Experimental** | Implemented; the API or behaviour may still change |
| **Partial** | Implemented, or confirmed, for some cases only — the row says which |
| **Implemented — mock-tested** | Complete and covered by the offline suite, which says nothing about what the venue will do; read the **Mainnet** column for that |
| **Implemented — mainnet validation pending** | Implemented, but the offline suite does not assert this end to end, and no live run covers it |
| **Unsupported** | Not implemented |

No feature is marked *Stable*. Many rows below have now been confirmed on
mainnet, but a single recorded run is not evidence of stability — see
[why nothing is marked Stable](docs/validation.md#why-nothing-is-marked-stable).

### Market data

| Feature | Spot | Perpetual | Inverse | Delivery | Options | Status | Mainnet | Notes |
|---|---|---|---|---|---|---|---|---|
| Trade ticks | yes | yes | yes | yes | yes | Implemented — mock-tested | spot | `*.trades`; venue trade id preserved |
| Quote ticks (real BBO) | yes | yes | yes | yes | yes | Implemented — mock-tested | spot | `*.book_ticker`; no synthesised quotes anywhere |
| Order book deltas | yes | yes | yes | yes | yes | Implemented — mock-tested | spot | REST snapshot + incremental stream, sequence-validated, resync on gap. Interval snapshots and the managed book were confirmed in the same run |
| Order book depth (`OrderBookDepth10`) | yes | yes | yes | yes | yes | Implemented — mock-tested | — | The venue's periodic `*.order_book` snapshot channel, ten levels per side |
| Order book snapshot request | yes | yes | yes | yes | yes | Implemented — mock-tested | spot | Depth clamped to what the product accepts |
| Bars (closed only) | yes | yes | yes | yes | yes | Implemented — mock-tested | spot | 1s to 7d; delivery and options infer the close |
| Historical bars / trades | yes | yes | yes | yes | yes | Partial (offline coverage reaches the HTTP layer only) | spot | Paginated REST, 1000 rows per call |
| Mark price | n/a | yes | yes | yes | yes | Implemented — mock-tested | USDT perpetual | `futures.tickers`; `options.contract_tickers` on options |
| Index price | n/a | yes | yes | yes | yes | Implemented — mock-tested | USDT perpetual | `futures.tickers`; `options.contract_tickers` on options |
| Funding rate | n/a | yes | yes | n/a | n/a | Implemented — mock-tested | USDT perpetual | From `futures.tickers` |
| Historical funding rates | n/a | yes | yes | n/a | n/a | Partial (offline coverage reaches the HTTP layer only) | USDT perpetual | REST `/futures/{settle}/funding_rate` |
| Instrument updates | yes | yes | yes | yes | yes | Implemented — mock-tested | — | Polled; Gate.io has no instrument channel. The initial load is confirmed on mainnet (see Instruments below); the periodic reload is not |
| Instrument status | yes | yes | yes | yes | yes | Implemented — mock-tested | — | Polled from the instrument listings; Gate.io publishes no status channel, so a halt shorter than the poll interval is invisible |
| Instrument close | n/a | n/a | n/a | yes | yes | Implemented — mock-tested | — | Settlement after expiry; the three continuous products never settle and the subscription is refused |
| `GateioTicker` custom data | yes | yes | yes | yes | yes | Implemented — mock-tested | — | The venue ticker fields the platform has no type for: 24h statistics, greeks, implied volatilities, delivery basis |
| Historical quotes | n/a | n/a | n/a | n/a | n/a | Unsupported | n/a | Gate.io publishes no quote history; the request is refused rather than answered from the current ticker row |
| Options underlying streams | n/a | n/a | n/a | n/a | yes | Partial | — | Reachable through the raw WebSocket client, not wired into the data engine |

### Instruments

| Feature | Status | Mainnet | Notes |
|---|---|---|---|
| Spot `CurrencyPair` | Implemented — mock-tested | spot | Precision, minimums, account fee tier |
| `CryptoPerpetual` (linear and inverse) | Implemented — mock-tested | USDT perpetual | Contract-count quantities, `quanto_multiplier`. The inverse variant has no live run |
| `CryptoFuture` (delivery) | Implemented — mock-tested | delivery | Activation and expiration from the contract |
| `CryptoOption` | Implemented — mock-tested | options | Strike and kind from the symbol |
| Multi-product provider with filters | Implemented — mock-tested | spot, perpetual, delivery, options (load only) | Per-product degradation on an unprovisioned wallet, which has no live run |
| Rejection of unrepresentable price scales | Implemented — mock-tested | — | Never publishes a quantised zero as a venue price |

### Execution

| Feature | Spot | Perpetual | Inverse | Delivery | Options | Status | Mainnet |
|---|---|---|---|---|---|---|---|
| MARKET | yes | yes | yes | yes | yes | Implemented — mock-tested | spot (buy, closing sell, both time-in-force families, quote-denominated buy); USDT perpetual (a sell opening a short, a buy opening a long, and a two-contract sell flipping one to the other) |
| LIMIT (GTC / IOC / FOK) | yes | yes | yes | yes | GTC/IOC | Implemented — mock-tested | spot (both sides accepted; aggressive IOC and FOK filled, passive IOC cancelled, passive FOK rejected); options (a passive buy cancelled, an aggressive IOC buy filled, a covered passive sell cancelled) |
| Post-only (`poc`, GTC only) | yes | yes | yes | yes | yes | Implemented — mock-tested | spot (accepted when passive; rejected by the venue when it would cross) |
| STOP_MARKET / STOP_LIMIT | yes | yes | yes | yes | no | Implemented — mock-tested | spot (STOP_LIMIT, buy side only) and USDT perpetual (STOP_MARKET, both sides, plus cancel-replace of the armed order); nothing triggered |
| MARKET_IF_TOUCHED / LIMIT_IF_TOUCHED | yes | yes | yes | yes | no | Implemented — mock-tested | spot (LIMIT_IF_TOUCHED, buy side only) and USDT perpetual (MARKET_IF_TOUCHED, both sides), armed and cancelled; nothing triggered |
| Reduce-only | n/a | yes | yes | yes | yes | Implemented — mock-tested | USDT perpetual (closed a short, and refused by the venue with no position open) |
| Iceberg (`display_qty`, non-zero) | yes | yes | yes | yes | yes | Implemented — mock-tested | spot (accepted carrying its display quantity) |
| Quote-denominated quantity | market buy | no | no | no | no | Implemented — mock-tested | spot (the venue filled it and reported the quantity in base units; the order itself closes `CANCELED` on the venue's own base total, never `FILLED`) |
| Order lists, no contingency | yes | yes | yes | yes | yes | Implemented — mock-tested | — | Batched where the venue has a batch endpoint and the group fits, otherwise one order at a time |
| Order lists with a contingency (bracket, OCO, OTO) | no | no | no | no | no | Unsupported (denied in full, with the reason) | n/a | Gate.io's attached TP/SL carries no per-leg id, so the legs could never be identified; use order emulation |
| Cancel / cancel-all / batch cancel | yes | yes | yes | yes | yes | Implemented — mock-tested | spot (single cancel, repeated cancel, cancel-replace and cancel-all, each clearing what was resting); options (a resting buy and a resting sell cancelled); the batch endpoint has no live run, and two shutdowns left behind an order submitted after the sweep |
| Modify (amend) | yes | yes | yes | no | no | Partial (delivery and options reject explicitly) | spot (price amendment acknowledged) |
| Private WebSocket lifecycle | yes | yes | yes | yes | yes | Implemented — mock-tested | — |
| Order status / fill / position reports | yes | yes | yes | yes | yes | Implemented — mock-tested | spot and USDT perpetual (a fresh node's mass status answered from the venue: order, fill and position reports for state it had never seen). The perpetual's position was adopted into that node's cache; the orders were filtered by the platform as unclaimed, so order adoption has no live run |
| Internal wallet transfers | yes | yes | yes | yes | yes | Implemented — mock-tested | — |
| Hedge (dual) position mode | n/a | refused | refused | refused | n/a | Unsupported (detected and refused, never switched) | n/a |

### Accounts and margin

| Feature | Status | Mainnet | Notes |
|---|---|---|---|
| Cash account (spot only, plain ledger) | Implemented — mock-tested | spot | Every recorded live order ran on this account type |
| Margin account (any other product combination) | Implemented — mock-tested | USDT perpetual and options (every derivative order listed below ran on a `MARGIN` account) | One Nautilus account, wallets aggregated per currency |
| Isolated margin ledger | Implemented — mock-tested | — | `spot_account_mode=MARGIN` |
| Cross margin ledger | Implemented — mock-tested | — | Requires a unified account on the venue |
| Unified account | Implemented — mock-tested | — | `single_currency` has no balance minimum; per Gate.io's documentation `multi_currency` needs > 500 USDT and `portfolio` > 1000 USDT, which this adapter neither enforces nor checks |
| `Strategy.query_account()` | Implemented — mock-tested | — | Re-reads every enabled product's wallet over REST; names the wallets it could not read |
| Borrow / repay endpoints | Implemented — mock-tested | — | Exposed because isolated and cross margin need them; every liability-creating method says so |
| Withdrawals, sub-accounts, Earn, Gate Pay, P2P, Copy Trading, Bots | Unsupported | n/a | Out of scope: unrelated to trading, no code exists for them |

### Mainnet validation results

Recorded on 2026-07-29 against Gate.io mainnet, at the smallest size each
instrument permits — the spot rows on a cash account, the derivative rows on a
margin account:

* **Public market data.** Instruments loaded for spot, USDT perpetual, delivery
  and options; quotes, trades, closed bars, incremental book deltas and interval
  snapshots arrived, and the managed book was populated and correctly ordered;
  the book snapshot request and the historical trade, bar and funding-rate
  requests returned rows. The mark, index and funding streams delivered updates
  on the USDT perpetual, the only instrument subscribed for them.
* **Spot order lifecycle.** Market buy filled and opened a position; both limit
  sides accepted; post-only accepted; an iceberg accepted carrying its display
  quantity; a price amendment acknowledged; every open order cancelled on stop;
  the position closed by a sell that traded on the venue.
* **Spot time in force.** Market IOC and market FOK filled; the
  quote-denominated market buy filled and reported in base units; aggressive IOC
  and FOK limits filled, the FOK in a single fill event; the passive IOC was
  finished immediately without a fill and the passive FOK was rejected.
* **Spot cancels.** Cancel-replace passed: orders cancelled, replacements
  accepted, none rejected, and the cancel-all on stop cleared what was still
  resting. Cancelling an already-cancelled order passed: no fabricated
  rejection, no reopened order. Clearing resting orders on the way down was run
  four times and **failed twice**: every run cancelled everything that was
  resting when it began to stop, and two of them then submitted one more order
  that was still there when the run ended. No run has reached the batch
  endpoint, which therefore has no live evidence at all. A post-only order
  priced to cross was rejected by the venue with its own reason and nothing
  filled, but that step is recorded as failed, on the check about how the reason
  is worded.
* **Spot conditional orders.** Stop-limit and limit-if-touched armed at the
  venue and cancelled on stop — on the buy side only: a resting conditional sell
  needs base currency, and on a cash account holding none the platform denied
  every one before it could be sent. Nothing triggered, so the fire path is
  unconfirmed.
* **USDT perpetual execution — on a margin account.** A market sell filled and
  opened a short; a reduce-only order closed it and was not rejected; a
  reduce-only order sent with no position was refused by the venue, filled
  nothing and created nothing; stop-market and market-if-touched orders on both
  sides were armed and cancelled without firing; and one armed stop was
  cancelled and re-placed at a new trigger ten times in three minutes, every
  replacement accepted.
* **Options execution — one contract.** A resting limit buy was accepted, did
  not fill, and was cancelled; an aggressive IOC limit buy filled and opened a
  long; a limit sell was accepted against that long — covered, never naked —
  and cancelled on stop with the long intact. Options take no market order here
  and this adapter refuses conditional orders on them.
* **Reading state back from the venue.** Nodes that had never seen the account
  asked for its execution state and got it — order, fill and position reports,
  and a mass status the engine reconciled. On the perpetual an open long was
  adopted into the fresh cache with the venue's own quantity and entry price,
  and a later step flipped and flattened it. The spot orders left resting for
  the same test did not enter the cache: those runs kept the platform's default
  filter for unclaimed external orders, and one looked back over a window
  shorter than the age of the fill. Position adoption is confirmed; order
  adoption is not.

Nothing else has been run against the exchange — no order on an inverse
perpetual or a delivery contract, nothing on a margin, cross-margin or unified
spot ledger, and no restart of a node onto its own live state. The full record,
including what each run checked, what it did not, the steps that failed and the
three recorded checks that do not check what they claim, is in
[docs/validation.md](docs/validation.md).

## Symbology in one table

The venue string is `GATE_IO`. Gate.io symbols are used verbatim; only
perpetuals take a suffix, because 527 of them share a symbol with a spot pair.

| Product | Instrument id |
|---|---|
| Spot | `BTC_USDT.GATE_IO` |
| Perpetual (linear) | `BTC_USDT-PERP.GATE_IO` |
| Perpetual (inverse) | `BTC_USD-PERP.GATE_IO` |
| Delivery future | `BTC_USDT_20260807.GATE_IO` |
| Option | `BTC_USDT-20260729-70000-C.GATE_IO` |

Full reasoning, including why delivery and options need no suffix, is in
[docs/symbology.md](docs/symbology.md).

**Quantity semantics:** on spot a `Quantity` is base-currency amount; on every
contract product it is a **number of contracts**, with the face value in the
instrument's `multiplier`.

## Compatibility

| Adapter | nautilus_trader | Python | Gate.io API |
|---|---|---|---|
| 0.2.0a1 | >=1.230.0,<2 | >=3.12,<3.15 | v4 |

Developed against Python 3.13 and `nautilus_trader` 1.230.0.

## Installation

Not yet published on PyPI — install from GitHub:

```bash
pip install "nautilustrader-gateio-adapter @ git+https://github.com/x03f/nautilustrader-gateio-adapter"
```

For development:

```bash
git clone https://github.com/x03f/nautilustrader-gateio-adapter
cd nautilustrader-gateio-adapter
pip install -e '.[dev]'
```

## Quick start

### 1. Public REST (no credentials)

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

        print(f"spot precision : {pair['precision']} / {pair['amount_precision']}")
        print(f"spot top of book: {book['bids'][0][0]} / {book['asks'][0][0]}")
        print(f"perp multiplier : {contract['quanto_multiplier']}")


asyncio.run(main())
```

### 2. Public WebSocket (no credentials)

```python
import asyncio

from nautilus_gateio import GateioProductType, GateioPublicWebSocket


async def main() -> None:
    ws = GateioPublicWebSocket(
        product=GateioProductType.PERP,
        handler=lambda msg: print(msg["channel"], msg.get("result")),
    )
    await ws.connect()
    await ws.subscribe_book_ticker("BTC_USDT")
    await asyncio.sleep(10)
    await ws.disconnect()


asyncio.run(main())
```

### 3. Instruments

```python
import asyncio

from nautilus_trader.model.identifiers import InstrumentId

from nautilus_gateio import GateioHttpClient, GateioInstrumentProvider, GateioProductType


async def main() -> None:
    async with GateioHttpClient() as client:
        provider = GateioInstrumentProvider(
            client,
            products=(GateioProductType.SPOT, GateioProductType.PERP),
        )
        ids = [
            InstrumentId.from_str("BTC_USDT.GATE_IO"),
            InstrumentId.from_str("BTC_USDT-PERP.GATE_IO"),
        ]
        await provider.load_ids_async(ids)
        for instrument_id in ids:
            instrument = provider.find(instrument_id)
            print(instrument.id, type(instrument).__name__, instrument.price_increment)


asyncio.run(main())
```

### 4. Live `TradingNode` with market data

```python
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode

from nautilus_gateio import (
    GATEIO,
    GateioDataClientConfig,
    GateioLiveDataClientFactory,
    GateioProductType,
)

config = TradingNodeConfig(
    trader_id="EXAMPLE-001",
    data_clients={
        GATEIO: GateioDataClientConfig(
            products=(GateioProductType.SPOT, GateioProductType.PERP),
            instrument_provider=InstrumentProviderConfig(
                load_ids=frozenset(["BTC_USDT.GATE_IO", "BTC_USDT-PERP.GATE_IO"]),
            ),
        ),
    },
)
node = TradingNode(config=config)
node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
node.build()
# node.trader.add_strategy(...)  # then node.run()
```

See [`examples/04_trading_node_data.py`](examples/04_trading_node_data.py) for a
complete runnable version.

### 5. Adding execution

```python
from nautilus_gateio import GateioExecClientConfig, GateioLiveExecClientFactory, GateioProductType

exec_config = GateioExecClientConfig(
    environment="testnet",  # the default is "mainnet" — state which you mean
    products=(GateioProductType.SPOT,),
)
# node_config = TradingNodeConfig(..., exec_clients={GATEIO: exec_config})
# node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
```

## Safety model

Read this before configuring execution.

* **`environment` defaults to `"mainnet"` on both clients.** In 0.1.0 execution
  defaulted to the testnet. It no longer does: an execution client that silently
  points at a different exchange environment than the operator believes is more
  dangerous than one that requires the venue to be stated.
* **There is no local order kill switch.** The 0.1.0 `live_orders` flag is gone.
  A boolean inside the process is not a security boundary — the process holds
  the key either way.
* The controls that do bind, strongest first:
  1. **API key permissions** on the Gate.io side — grant only what the strategy
     uses, and never grant withdrawal permission to a trading key;
  2. **IP allow-listing** on the key;
  3. `environment="testnet"` for rehearsal (spot and USDT perpetuals only);
  4. NautilusTrader's own sandbox and backtest execution for simulation.
* **Nothing is silently altered.** An order Gate.io cannot express is denied
  with a reason before any request is sent, never converted into a different
  order.
* **No venue-side setting is changed for you.** Hedge mode is refused, not
  switched; a unified account is never upgraded automatically.
* **The adapter cannot move funds out of the account.** `transfer()` only
  addresses the account's own trading wallets; there is no withdrawal code.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `GATE_API_KEY` / `GATE_API_SECRET` | `environment="mainnet"`, and as the testnet fallback | Gate.io API credentials |
| `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` | `environment="testnet"` | Gate.io testnet credentials |
| `GATEIO_ALLOW_ORDERS` | the order example script only | Must be `YES` for [`examples/06_testnet_orders.py`](examples/06_testnet_orders.py) to place anything. A guard in that script, not an adapter feature |

Credentials are resolved when a client is created; empty values keep the client
in public-data-only mode. Values are stripped of surrounding whitespace.

## Documentation

| Page | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | Package layout, dataflows, design decisions |
| [symbology.md](docs/symbology.md) | Venue string, instrument ids, why only perpetuals get a suffix |
| [products.md](docs/products.md) | Spot, margin (isolated/cross/unified), perpetual, inverse, delivery, options |
| [configuration.md](docs/configuration.md) | Complete field reference for both config classes |
| [market-data.md](docs/market-data.md) | Subscriptions, requests, order book synchronisation |
| [execution.md](docs/execution.md) | Order translation, every rejection path, reconciliation |
| [errors.md](docs/errors.md) | Every exception raised on purpose, and who handles it — you or the engine |
| [migration-0.1-to-0.2.md](docs/migration-0.1-to-0.2.md) | Every breaking change from 0.1.0 |
| [validation.md](docs/validation.md) | What has actually been exercised, and where |
| [testing.md](docs/testing.md) | Running the suite, coverage areas, credentialed-test rules |
| [troubleshooting.md](docs/troubleshooting.md) | Real failure modes and their fixes |
| [releasing.md](docs/releasing.md) | Release checklist |
| [examples/README.md](examples/README.md) | The example scripts and what each needs |

Where the project is going, stage by stage, and how work is done here: [docs/roadmap.md](docs/roadmap.md).

## Testing

```bash
pip install -e '.[dev]'
pytest
```

The default run is unit tests only: no network, no credentials. Tests that talk
to Gate.io are marked `integration` and deselected by default
(`addopts = "-m 'not integration'"`); run them with `pytest -m integration`.

## Known limitations

* **Mainnet validation covers market data, spot execution, one USDT perpetual
  and one option contract.** No inverse or delivery order, nothing on a margin
  or unified spot ledger and no restart of a node onto its own live state has
  been run against the exchange; two shutdowns out of four left an order resting
  at the venue; and nothing is marked *Stable*. See
  [docs/validation.md](docs/validation.md).
* **Testnet covers spot and USDT perpetuals only.** Inverse perpetuals, delivery
  futures and options have no testnet endpoint, and configuring them with
  `environment="testnet"` is rejected up front.
* **Delivery and options orders cannot be amended** — the venue has no amend
  endpoint there, so modification is rejected explicitly.
* **Options have no price-trigger endpoint**, so conditional order types are
  rejected on that product.
* **Cross margin and unified accounts need venue-side activation**, and the
  richer unified modes need balances above the venue's own thresholds
  (500 / 1000 USDT).
* **Derivative wallets are created by the first transfer into them**; until then
  Gate.io reports them as missing and the adapter skips that product.
* **Not yet on PyPI** — install from GitHub.

### Behaviour limits carried into the alpha

These survived review deliberately, rather than being unknown. Each is stated in
full under [residual risks](docs/review-matrix.md#residual-risks).

* **Report paging stops at 20 pages (2000 rows)** and logs a warning naming the
  cap. An account holding more open orders or fills than that in one listing
  window reports a truncated view.
* **Contracts with decimal sizes (`enable_decimal`) are refused loudly** rather
  than truncated to a whole lot count.
* **Run with the default `open_check_open_only=True`.** With it set to `False`,
  an order whose listing row cannot be read is counted missing on every cycle
  and, once `open_check_missing_retries` is exhausted, is resolved by the engine
  with a fabricated rejection or cancellation.
* **A fired spot conditional order is resolved by re-reading the venue's armed
  price orders**, because Gate.io carries no client order id on that path.
* **The position staleness memory is one restart deep**, and a compensating
  trade stamped in the same second as the answer is withheld until the venue
  produces a distinguishable row.
* **A spot order stream payload that states neither a status nor an event is
  inferred to be finished.** No documented Gate.io payload reaches that branch,
  but the inference contradicts the rule that absence makes no claim.

## Security

See [SECURITY.md](SECURITY.md) for the disclosure policy. Practical rules:

* **Never commit API keys.** Use environment variables; `.gitignore` covers
  common secret-file patterns, but the responsibility is yours.
* **Least privilege.** Create keys with only the permissions the strategy needs;
  never grant withdrawal permission to a trading key.
* **IP allowlist.** Restrict keys to the host that will use them.
* **Rehearse first.** Testnet, then mainnet with small sizes.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
workflow, coding standards and test requirements. Release history lives in
[CHANGELOG.md](CHANGELOG.md). Validation results are especially welcome: see
[docs/validation.md](docs/validation.md).

## Support the project

This adapter is developed and maintained in free time. The best support is always a ⭐ star,
a well-written issue, or a pull request. If the adapter saves you time and you would like to
support its continued development financially, donations are welcome — entirely optional and
without any additional benefits or obligations.

<details>
<summary>Donation addresses</summary>

| Asset | Network | Address |
|---|---|---|
| BTC | Bitcoin Mainnet | `bc1qrzw790us8sen3wh7kl07yntvvkaa6upxk6jke9` |
| ETH | Ethereum Mainnet | `0xcC3b21D33abA753dcbEA96AB823fD22b8B5C444D` |
| USDT | TRON (TRC20) | `TAiVKz7LveKsqxeG8jnnang6fmpt8SX8Fq` |
| USDT | TON Network | `UQA5cxIn0YIkezeOFqFQa0t7pIzQl1svhhm8w09Q9DUPtPRm` |
| USDC | Base Network | `0xcC3b21D33abA753dcbEA96AB823fD22b8B5C444D` |

Gate.io internal transfer (zero-fee within Gate.io) — UID: `4415345`

Always send only the listed asset on the exactly matching network; double-check the address
before sending. Donations are non-refundable.

</details>

## License

MIT — see [LICENSE](LICENSE).

NautilusTrader is a dependency of this package and is licensed under [LGPL-3.0](https://github.com/nautechsystems/nautilus_trader/blob/master/LICENSE). This package contains no NautilusTrader source code; it only imports the library through its public API.

## Disclaimer

This is an unofficial, community-maintained project. It is not affiliated with, maintained by, or endorsed by Gate.io or Nautech Systems. All trademarks belong to their respective owners.

Trading cryptocurrencies involves substantial risk of loss and is not suitable for everyone. This software is provided "as is", without warranty of any kind — see the [LICENSE](LICENSE) for the full terms. You are solely responsible for safeguarding your API credentials, for every order the software places on your accounts, and for your compliance with the Gate.io Terms of Service and any regulations that apply to you.
