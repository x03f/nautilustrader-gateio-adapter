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
Nautech Systems. Current maturity is **alpha (v0.2.0)**, developed against
`nautilus_trader` 1.230.0 on Python 3.13.

Upgrading from 0.1.0? Read the
[migration guide](docs/migration-0.1-to-0.2.md) first — the venue string, the
instrument ids and the execution environment default all changed.

## What it does

* **Real market data only.** Trades, best bid/offer quotes, sequence-validated
  order book deltas, closed bars, mark and index prices and funding rates — all
  from the venue's own streams. Nothing is synthesised or interpolated.
* **Every tradable product.** One data client and one execution client
  multiplex spot, USDT perpetuals, BTC-settled perpetuals, USDT delivery futures
  and USDT-settled options.
* **Execution that never lies about your order.** Six order types, four
  time-in-force values, post-only, reduce-only and iceberg. Anything Gate.io
  cannot express is denied or rejected with a stated reason — never silently
  converted into a different order.
* **Real reconciliation.** All four NautilusTrader report generators are
  implemented against REST, so a restart with resting orders and open positions
  is a supported path.
* **Usable standalone.** The async REST transport with its typed per-product
  namespaces, and the self-healing WebSocket clients, work without a Nautilus
  node.

## Status

**Alpha — `0.2.0a1`.** An external community adapter, written in pure Python,
built against NautilusTrader 1.230.0. Not an official NautilusTrader
integration, and not affiliated with Gate.io.

The package is complete and covered by an extensive offline test suite, but a
passing suite is evidence about the code, not about the exchange. **No mainnet
validation has been recorded yet**, so nothing in the matrix below is marked
*Stable* — the project reserves that label for behaviour that has been both
unit-tested and exercised against the real venue, with the result written down
in [docs/validation.md](docs/validation.md).

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

Status vocabulary:

| Status | Meaning |
|---|---|
| **Stable** | Unit-tested **and** exercised on mainnet, with the result recorded in [docs/validation.md](docs/validation.md) |
| **Experimental** | Implemented; the API or behaviour may still change |
| **Partial** | Implemented for some cases only, as stated in the row |
| **Implemented — not mainnet-validated** | Complete and unit-tested, never run against the real venue |
| **Unsupported** | Not implemented |

No feature currently qualifies as *Stable*. That is a statement about validation
coverage, not about test coverage.

### Market data

| Feature | Spot | Perpetual | Inverse | Delivery | Options | Status | Notes |
|---|---|---|---|---|---|---|---|
| Trade ticks | yes | yes | yes | yes | yes | Implemented — not mainnet-validated | `*.trades`; venue trade id preserved |
| Quote ticks (real BBO) | yes | yes | yes | yes | yes | Implemented — not mainnet-validated | `*.book_ticker`; no synthesised quotes anywhere |
| Order book deltas | yes | yes | yes | yes | yes | Implemented — not mainnet-validated | REST snapshot + incremental stream, sequence-validated, resync on gap |
| Order book snapshot request | yes | yes | yes | yes | yes | Implemented — not mainnet-validated | Depth clamped to what the product accepts |
| Bars (closed only) | yes | yes | yes | yes | yes | Implemented — not mainnet-validated | 1s to 7d; delivery and options infer the close |
| Historical bars / trades | yes | yes | yes | yes | yes | Implemented — not mainnet-validated | Paginated REST, 1000 rows per call |
| Mark price | n/a | yes | yes | yes | no | Implemented — not mainnet-validated | From `futures.tickers` |
| Index price | n/a | yes | yes | yes | no | Implemented — not mainnet-validated | From `futures.tickers` |
| Funding rate | n/a | yes | yes | n/a | n/a | Implemented — not mainnet-validated | From `futures.tickers` |
| Instrument updates | yes | yes | yes | yes | yes | Implemented — not mainnet-validated | Polled; Gate.io has no instrument channel |
| Options underlying streams | n/a | n/a | n/a | n/a | yes | Partial | Reachable through the raw WebSocket client, not wired into the data engine |

### Instruments

| Feature | Status | Notes |
|---|---|---|
| Spot `CurrencyPair` | Implemented — not mainnet-validated | Precision, minimums, account fee tier |
| `CryptoPerpetual` (linear and inverse) | Implemented — not mainnet-validated | Contract-count quantities, `quanto_multiplier` |
| `CryptoFuture` (delivery) | Implemented — not mainnet-validated | Activation and expiration from the contract |
| `CryptoOption` | Implemented — not mainnet-validated | Strike and kind from the symbol |
| Multi-product provider with filters | Implemented — not mainnet-validated | Per-product degradation on an unprovisioned wallet |
| Rejection of unrepresentable price scales | Implemented — not mainnet-validated | Never publishes a quantised zero as a venue price |

### Execution

| Feature | Spot | Perpetual | Inverse | Delivery | Options | Status |
|---|---|---|---|---|---|---|
| MARKET | yes | yes | yes | yes | yes | Implemented — not mainnet-validated |
| LIMIT (GTC / IOC / FOK) | yes | yes | yes | yes | GTC/IOC | Implemented — not mainnet-validated |
| Post-only (`poc`) | yes | yes | yes | yes | yes | Implemented — not mainnet-validated |
| STOP_MARKET / STOP_LIMIT | yes | yes | yes | yes | no | Implemented — not mainnet-validated |
| MARKET_IF_TOUCHED / LIMIT_IF_TOUCHED | yes | yes | yes | yes | no | Implemented — not mainnet-validated |
| Reduce-only | n/a | yes | yes | yes | yes | Implemented — not mainnet-validated |
| Iceberg (`display_qty`) | yes | yes | yes | yes | yes | Implemented — not mainnet-validated |
| Quote-denominated quantity | market buy | no | no | no | no | Implemented — not mainnet-validated |
| Cancel / cancel-all / batch cancel | yes | yes | yes | yes | yes | Implemented — not mainnet-validated |
| Modify (amend) | yes | yes | yes | no | no | Partial (delivery and options reject explicitly) |
| Private WebSocket lifecycle | yes | yes | yes | yes | yes | Implemented — not mainnet-validated |
| Order status / fill / position reports | yes | yes | yes | yes | yes | Implemented — not mainnet-validated |
| Internal wallet transfers | yes | yes | yes | yes | yes | Implemented — not mainnet-validated |
| Hedge (dual) position mode | n/a | refused | refused | refused | n/a | Unsupported (detected and refused, never switched) |

### Accounts and margin

| Feature | Status | Notes |
|---|---|---|
| Cash account (spot only, plain ledger) | Implemented — not mainnet-validated | |
| Margin account (any other product combination) | Implemented — not mainnet-validated | One Nautilus account, wallets aggregated per currency |
| Isolated margin ledger | Implemented — not mainnet-validated | `spot_account_mode=MARGIN` |
| Cross margin ledger | Implemented — not mainnet-validated | Requires a unified account on the venue |
| Unified account | Implemented — not mainnet-validated | `single_currency` has no balance minimum; per Gate.io's documentation `multi_currency` needs > 500 USDT and `portfolio` > 1000 USDT, which this adapter neither enforces nor checks |
| Borrow / repay endpoints | Implemented — not mainnet-validated | Exposed because isolated and cross margin need them; every liability-creating method says so |
| Withdrawals, sub-accounts, Earn, Gate Pay, P2P, Copy Trading, Bots | Unsupported | Out of scope: unrelated to trading, no code exists for them |

### Mainnet validation results

*Placeholder — nothing recorded yet.* See
[docs/validation.md](docs/validation.md) for the table that gates the *Stable*
label and for the paths that cannot be validated without specific account
states.

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
* **Nothing is silently altered.** An order Gate.io cannot express is denied or
  rejected with a reason, never converted into a different order.
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

* **No mainnet validation yet.** Nothing is marked *Stable*; see
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
