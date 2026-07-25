# Gate.io Adapter for NautilusTrader

[![CI](https://github.com/x03f/nautilustrader-gateio-adapter/actions/workflows/ci.yml/badge.svg)](https://github.com/x03f/nautilustrader-gateio-adapter/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![NautilusTrader 1.230+](https://img.shields.io/badge/nautilus__trader-1.230%2B-orange)](https://github.com/nautechsystems/nautilus_trader)

A community-maintained, **unofficial** exchange adapter that connects [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) to [Gate.io](https://www.gate.io/): spot market data and order execution over the Gate.io API v4 (REST and WebSocket). This project is not affiliated with, maintained by, or endorsed by Gate.io or Nautech Systems. Current maturity is **alpha (v0.1.0)**, tested against nautilus_trader 1.230.0 on Python 3.13 with Gate.io API v4.

## Overview

NautilusTrader is a high-performance algorithmic trading platform with a growing set of exchange integrations — but, to our knowledge, no Gate.io adapter existed before this one. This package fills that gap for spot markets:

- **Market data** — live bars over WebSocket (`spot.candlesticks`) with an automatic REST-polling fallback, plus historical bars, delivered into a live `TradingNode` through the standard `LiveMarketDataClient` interface.
- **Instruments** — a dynamic `InstrumentProvider` that builds Nautilus `CurrencyPair` instruments (price/size precision, minimum amount, minimum notional) from live Gate.io specifications.
- **Execution** — a `LiveExecutionClient` for spot trading: limit and (emulated) market orders, cancels, partial-fill tracking, and account balance updates.
- **Standalone clients** — the underlying REST (`GateioHttpClient`) and WebSocket (`GateioWebSocketClient`) clients are usable on their own, without a Nautilus node.
- **Safety first** — a layered guard model ensures API credentials alone can never place an order; execution defaults to the Gate.io testnet.

## Status

**Alpha.** The adapter has been exercised extensively against the Gate.io spot **testnet**, including real testnet order flow: submissions, fills, partial fills, cancels, and IOC remainders surfaced back into the Nautilus portfolio. The public-data path runs against mainnet (Gate.io has no public spot testnet market-data feed).

Interfaces may change between 0.x releases. Correct operation is a goal; economic results are never guaranteed — see the [Disclaimer](#disclaimer).

## Feature support matrix

Statuses: **Supported** · **Partial** · **Experimental** · **Not supported** · **N/A**

### Market data

| Feature | Status | Notes |
|---|---|---|
| Live bars (WebSocket primary, REST fallback) | Supported | Closed bars only |
| Historical bars (`request_bars`) | Supported | REST `spot/candlesticks` |
| Reconnect / dedup / gap detection | Supported | Exponential backoff, duplicate drop, gap counters |
| Out-of-order bar drop | Supported | Bars older than the last emitted bar are discarded |
| Synthetic quotes (derived from bars) | Partial | By design: `QuoteTick`s synthesized around each bar close (±0.5 bp, unit sizes) so quote-driven fill simulations work. **These are NOT real market quotes** — disable with `emit_synthetic_quotes=False` if your strategy treats quotes as market truth |
| Real quote / trade streams into Nautilus | Not supported | `GateioWebSocketClient` can stream raw public trades, but they are not wired into the Nautilus data engine |
| Order-book streams | Not supported | REST snapshots only (standalone client) |
| Funding / open interest | Not supported | See experimental futures module for raw funding data |

### Instruments

| Feature | Status | Notes |
|---|---|---|
| Spot discovery and metadata (precision, min amount, min notional) | Supported | From live Gate.io pair specifications |
| Dynamic instrument provider | Supported | `load_ids_async` / `load_all_async` |
| Perpetual futures contract specs (REST) | Experimental | Raw client only, not Nautilus-integrated |
| Delivery futures | Not supported | |

### Execution (spot)

| Feature | Status | Notes |
|---|---|---|
| LIMIT GTC | Supported | |
| MARKET | Supported | With caveat: emulated as an aggressive IOC limit crossing the spread by 1%, because Gate.io spot market-buy orders interpret `amount` as *quote* currency. Mechanics documented in the module docs |
| Cancel / cancel-all | Supported | |
| Partial fills | Supported | Detected via REST delta polling |
| Exchange-side cancel (e.g. unfilled IOC remainder) surfaced | Supported | Emitted as `OrderCanceled` |
| Client order id propagation | Supported | Via the Gate.io `text` field (28-char limit, `t-` prefix, `[0-9A-Za-z_.-]`); ids that do not fit are mapped in memory |
| Stop orders / post-only / modify / TIF beyond GTC + IOC | Not supported | `ModifyOrder` logs a warning; cancel and re-submit instead |
| Private WebSocket | Not supported | Fills and balances are detected by REST polling |
| Start-up reconciliation | Not supported | `generate_*_reports` return empty (fresh-start semantics); a diagnostic `reconcile()` helper compares local vs. exchange state without mutating anything |
| Balances → account state | Supported | Pushed on connect, after fills, and on each poll |
| Positions | N/A | Spot `CASH` account — no positions |

### Safety

| Feature | Status | Notes |
|---|---|---|
| Layered order guards | Supported | `live_orders=True` hard switch on the HTTP client; execution config defaults to testnet; futures private client is testnet-only; order examples require an explicit `GATEIO_ALLOW_ORDERS=YES` opt-in |

### Futures (experimental)

| Feature | Status | Notes |
|---|---|---|
| Public data (contracts, tickers, book, candles, funding) | Experimental | Raw REST client |
| Private REST | Experimental | Testnet hosts only, enforced with `PermissionError`; mutating calls additionally require `live_orders=True` |
| Nautilus integration | Not supported | No futures instrument provider, data client, or execution client |

### Paper trading

| Feature | Status | Notes |
|---|---|---|
| Local fill simulator on live public data | Experimental | Pure simulation — orders never leave the process; market orders walk the real live order book for realistic slippage and partial fills |

## Supported markets

- **Spot market data**: mainnet (public, no credentials required; Gate.io has no public spot testnet market-data feed).
- **Spot trading**: verified against the Gate.io spot **testnet** (`api-testnet.gateapi.io`), including real order round-trips. Mainnet trading is possible via an explicit opt-in (`environment="mainnet"` in `GateioExecClientConfig`) but, to be plain about it, **has not been extensively exercised** — treat it accordingly and start small.
- **Futures**: experimental raw REST clients only, private side restricted to testnet. Not integrated with Nautilus.

## Compatibility

| Adapter | nautilus_trader | Python | Gate.io API |
|---|---|---|---|
| 0.1.x | >=1.230.0,<2 | >=3.12,<3.15 | v4 |

Tested combination: Python 3.13.5 + nautilus_trader 1.230.0.

## Installation

Not yet published on PyPI — install from GitHub:

```bash
pip install "nautilustrader-gateio-adapter @ git+https://github.com/x03f/nautilustrader-gateio-adapter"
```

Or for development:

```bash
git clone https://github.com/x03f/nautilustrader-gateio-adapter
cd nautilustrader-gateio-adapter
pip install -e '.[dev]'
```

## Quick start

### 1. Public market data (no credentials)

```python
from nautilus_gateio import GateioHttpClient

with GateioHttpClient() as client:
    candles = client.candles("BTC_USDT", interval="1m", limit=5)
    book = client.order_book("BTC_USDT", limit=5)
    print(f"last close: {candles[-1]['close']}")
    print(f"best bid/ask: {book['bids'][0][0]} / {book['asks'][0][0]}")
```

### 2. Nautilus instruments

```python
import asyncio

from nautilus_trader.model.identifiers import InstrumentId

from nautilus_gateio import GateioInstrumentProvider


async def main() -> None:
    instrument_id = InstrumentId.from_str("BTC_USDT.GATEIO")
    provider = GateioInstrumentProvider()
    await provider.load_ids_async([instrument_id])
    instrument = provider.find(instrument_id)
    print(instrument.id, instrument.price_precision, instrument.min_notional)


asyncio.run(main())
```

### 3. Live TradingNode with Gate.io market data

```python
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode

from nautilus_gateio import GATEIO, GateioDataClientConfig, GateioLiveDataClientFactory

config = TradingNodeConfig(
    trader_id="EXAMPLE-001",
    data_clients={
        GATEIO: GateioDataClientConfig(
            instrument_provider=InstrumentProviderConfig(
                load_ids=frozenset(["BTC_USDT.GATEIO"]),
            ),
        ),
    },
)
node = TradingNode(config=config)
node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
node.build()
# node.run()  # add a strategy first — see the full runnable example below
```

See [`examples/04_trading_node_data.py`](examples/04_trading_node_data.py) for the complete runnable version with a bar-logging strategy.

### 4. Authenticated, read-only account access

```python
import os

from nautilus_gateio import GATEIO_HTTP_TESTNET, GateioHttpClient

client = GateioHttpClient(
    api_key=os.environ["GATE_TESTNET_API_KEY"],
    api_secret=os.environ["GATE_TESTNET_API_SECRET"],
    live_orders=False,  # default — order calls raise LiveOrdersDisabledError locally
    base_url=GATEIO_HTTP_TESTNET,
)
with client:
    client.sync_time()
    print(client.balances())
    print(client.open_orders())
```

With `live_orders=False` (the default), every order-mutating call raises `LiveOrdersDisabledError` before any network request — valid credentials alone can never place an order.

### 5. Testnet order flow

Order-placing code is deliberately **not** inlined here. Run [`examples/06_testnet_orders.py`](examples/06_testnet_orders.py), which performs a full place → list → cancel round-trip on the spot testnet behind four safety gates: an explicit `GATEIO_ALLOW_ORDERS=YES` opt-in, a hard-coded testnet host, testnet-only credentials, and a hard notional cap with exchange-constraint validation. The [examples README](examples/README.md) explains the safety model.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `GATE_API_KEY` | mainnet clients | Gate.io API key (also the fallback when testnet variables are unset) |
| `GATE_API_SECRET` | mainnet clients | Gate.io API secret |
| `GATE_TESTNET_API_KEY` | testnet clients (execution default) | Gate.io testnet API key |
| `GATE_TESTNET_API_SECRET` | testnet clients (execution default) | Gate.io testnet API secret |
| `GATEIO_ALLOW_ORDERS` | example scripts only | Must be `YES` for the order-placing example to run |

Credentials are resolved at client-creation time when `api_key` / `api_secret` are not passed explicitly; empty values keep the client in public-data-only mode.

## Architecture overview

The package is layered: pure functions at the bottom (signing, symbol mapping, response parsing), standalone REST/WebSocket clients above them, and the Nautilus live clients (data, execution, instrument provider, factories) on top. The WebSocket transport handles reconnects with capped exponential backoff, drops duplicate and out-of-order candles, and counts gaps; execution fills are detected by REST delta polling against order state. See [docs/architecture.md](docs/architecture.md) for the full picture.

## Testing

```bash
pip install -e '.[dev]'
pytest
```

The default run executes unit tests only — they use fakes and recorded shapes, need no credentials, and hit no network. Tests that talk to Gate.io are marked `integration` and are deselected by default (`addopts = "-m 'not integration'"` in `pyproject.toml`); run them explicitly with:

```bash
pytest -m integration
```

## Known limitations

- **No real quote or trade streams into Nautilus** — bars are the only market data type delivered to the data engine; order-book streams and funding/open-interest data are not supported.
- **Synthetic quotes are not market data** — the optional `QuoteTick`s are derived from bar closes for the benefit of quote-driven fill simulations; disable them (`emit_synthetic_quotes=False`) if quotes matter to your strategy.
- **Fill detection is REST polling** — there is no private WebSocket, so fills arrive with polling latency (default 5 s cadence, plus an immediate post-submit check). Not suitable for latency-sensitive execution.
- **MARKET orders are emulated** — implemented as aggressive IOC limit orders crossing the spread by 1% (a deliberate workaround for Gate.io's quote-amount semantics on spot market buys).
- **No stop orders, post-only, or order modification**; time-in-force support is GTC and IOC only.
- **No start-up reconciliation** — the execution client starts with fresh-state semantics; the standalone `reconcile()` helper only diagnoses discrepancies.
- **Futures are experimental** — raw REST clients only, private side testnet-only, no Nautilus integration.
- **Mainnet trading is not extensively exercised** — testnet is the verified path.
- **Not yet on PyPI** — install from GitHub.

## Troubleshooting

1. **`LiveOrdersDisabledError` when placing an order** — intentional: construct `GateioHttpClient` with `live_orders=True`. Credentials alone never enable order flow.
2. **Signature errors (`INVALID_SIGNATURE` / 401)** — call `client.sync_time()` first to align signature timestamps with the exchange clock, and check for whitespace in your key/secret (the config resolver strips it; direct construction does not).
3. **No bars arriving** — only *closed* candles are emitted, so a 1-minute subscription produces its first bar up to a minute after connect; longer intervals take correspondingly longer.
4. **`ValueError: cannot infer quote currency`** — use the canonical underscore symbol form (`BTC_USDT.GATEIO`); the no-underscore form is a best-effort heuristic for common quote currencies only.
5. **Testnet authentication fails** — testnet keys are separate from mainnet keys; create them on the Gate.io testnet and set `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET`.

More in [docs/troubleshooting.md](docs/troubleshooting.md).

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability disclosure policy. Practical rules:

- **Never commit API keys** — use environment variables; `.gitignore` covers common secret-file patterns, but the responsibility is yours.
- **Least privilege** — create API keys with spot-trade permission only; never grant withdrawal permission to a trading key.
- **IP allowlist** — restrict keys to your trading host's address in the Gate.io API settings.
- **Testnet first** — verify your full setup against the testnet before pointing anything at mainnet.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, coding standards, and test requirements. Release history lives in [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).

NautilusTrader is a dependency of this package and is licensed under [LGPL-3.0](https://github.com/nautechsystems/nautilus_trader/blob/master/LICENSE). This package contains no NautilusTrader source code; it only imports the library through its public API.

## Disclaimer

This is an unofficial, community-maintained project. It is not affiliated with, maintained by, or endorsed by Gate.io or Nautech Systems. All trademarks belong to their respective owners.

Trading cryptocurrencies involves substantial risk of loss and is not suitable for everyone. This software is provided "as is", without warranty of any kind — see the [LICENSE](LICENSE) for the full terms. You are solely responsible for safeguarding your API credentials, for every order the software places on your accounts, and for your compliance with the Gate.io Terms of Service and any regulations that apply to you.
