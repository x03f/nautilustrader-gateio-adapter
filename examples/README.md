# Examples

Standalone scripts demonstrating the adapter from raw REST calls up to a live
Nautilus `TradingNode`. Run them from the repository root:

```bash
python examples/01_public_rest.py
```

| Example | What it shows | Needs |
|---|---|---|
| [`01_public_rest.py`](01_public_rest.py) | `GateioHttpClient` public REST: ping, pair spec, candles, top of book | Nothing (public data) |
| [`02_public_websocket.py`](02_public_websocket.py) | `GateioWebSocketClient`: live 1m candles + trades stream, transport metrics | Nothing (public data) |
| [`03_instruments.py`](03_instruments.py) | `GateioInstrumentProvider`: building Nautilus `CurrencyPair` instruments with precisions and minimums | Nothing (public data) |
| [`04_trading_node_data.py`](04_trading_node_data.py) | Minimal live `TradingNode` with the Gate.io data client and a bar-logging strategy | Nothing (public data) |
| [`05_account_readonly.py`](05_account_readonly.py) | Authenticated read-only access: balances and open orders; proves `live_orders=False` blocks order calls | Testnet API keys (`GATE_TESTNET_API_KEY`, `GATE_TESTNET_API_SECRET`) |
| [`06_testnet_orders.py`](06_testnet_orders.py) | Full order round-trip on the spot **testnet** behind four safety gates: place a far-off limit buy, list it, cancel it | Testnet API keys **and** `GATEIO_ALLOW_ORDERS=YES` |

## Safety model

The examples follow the same layered safety model as the adapter itself:

1. **Credentials never imply trading.** `GateioHttpClient` defaults to
   `live_orders=False`; with it, any order-mutating call raises
   `LiveOrdersDisabledError` locally, before any network request. Example 05
   demonstrates this with valid credentials in place.
2. **Explicit opt-in for orders.** Example 06 refuses to run unless
   `GATEIO_ALLOW_ORDERS=YES` is set for that run — keys sitting in the
   environment can never place an order on their own.
3. **Testnet only, hard-coded.** The order example targets the Gate.io
   testnet host via a constant defined in the script; no environment variable
   or flag can redirect it to mainnet.
4. **Bounded and validated orders.** Order notional is hard-capped
   (5 USDT), the limit price sits 30% below the market so it cannot fill, and
   every order goes through `place_order_validated`, which enforces the
   exchange's precision and minimum constraints.

Scripts that require credentials exit with a clear explanation (and without a
traceback) when the environment variables are missing.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` | 05, 06 | Gate.io **testnet** API key pair |
| `GATEIO_ALLOW_ORDERS` | 06 | Must be exactly `YES` to allow order placement |

Never use mainnet API keys with these examples.
