# Examples

Standalone scripts, from raw REST calls up to a live NautilusTrader
`TradingNode`. Run them from the repository root:

```bash
python examples/01_public_rest.py
```

| Example | What it shows | Needs |
|---|---|---|
| [`01_public_rest.py`](01_public_rest.py) | The async REST transport with one typed namespace per product: spot spec and book, perpetual contract, delivery contracts, option chain | Nothing (public data) |
| [`02_public_websocket.py`](02_public_websocket.py) | `GateioPublicWebSocket` on two products at once: real best bid/offer and public trades, plus transport counters | Nothing (public data) |
| [`03_instruments.py`](03_instruments.py) | `GateioInstrumentProvider` building `CurrencyPair`, `CryptoPerpetual` (linear and inverse), `CryptoFuture` and `CryptoOption`, and what a `Quantity` means on each | Nothing (public data) |
| [`04_trading_node_data.py`](04_trading_node_data.py) | A live `TradingNode` with the Gate.io data client: quotes, trades and closed bars for a spot pair and a perpetual simultaneously | Nothing (public data) |
| [`05_account_readonly.py`](05_account_readonly.py) | Authenticated read-only inspection: fee tier, per-product wallets (including one that does not exist yet), resting orders | API credentials |
| [`06_testnet_orders.py`](06_testnet_orders.py) | A spot **testnet** order round-trip: place a far-from-market limit buy, confirm it rests, cancel it | Testnet credentials **and** `GATEIO_ALLOW_ORDERS=YES` |

Examples 01 to 04 need no credentials and touch only public endpoints.

## Symbology in the examples

Instrument ids follow the adapter's rule: the exact venue symbol, with `-PERP`
on perpetuals only.

```text
BTC_USDT.GATE_IO                     spot
BTC_USDT-PERP.GATE_IO                USDT-margined perpetual
BTC_USD-PERP.GATE_IO                 BTC-margined (inverse) perpetual
SOL_USDT_20260731.GATE_IO            delivery future
BTC_USDT-20260726-62500-P.GATE_IO    option
```

Note the venue string is `GATE_IO`, not `GATEIO` — that changed in 0.2.0.

## Safety in the order example

The adapter has **no order kill switch**, and `environment` defaults to
`"mainnet"`. The gates below live in `06_testnet_orders.py` itself, and are how
an example script should behave — they are not adapter features:

1. **Explicit opt-in.** The script refuses to run unless `GATEIO_ALLOW_ORDERS=YES`
   is set for that run, so keys sitting in the environment cannot place an order
   on their own.
2. **Testnet host, hard-coded.** The base URL comes from the
   `GATEIO_HTTP_TESTNET` constant; nothing can redirect the script to mainnet.
3. **Bounded notional.** The order value is capped in the script and the limit
   price sits far below the market, so it cannot fill.
4. **Cancel in `finally`.** The order is cancelled even if something above it
   raises.

For real deployments the controls that bind are the API key's permissions and
IP allow-list, plus an explicitly chosen `environment` — see
[docs/configuration.md](../docs/configuration.md#safety-model).

Scripts that need credentials exit with a clear message, and no traceback, when
the environment variables are missing.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `GATE_API_KEY` / `GATE_API_SECRET` | 05 | Mainnet credentials (and the testnet fallback) |
| `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` | 05, 06 | Gate.io **testnet** credentials |
| `GATEIO_ENVIRONMENT` | 05 | `testnet` (default) or `mainnet` |
| `GATEIO_ALLOW_ORDERS` | 06 | Must be exactly `YES` for the script to place an order |
