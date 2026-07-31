# Examples

Standalone scripts, from raw REST calls up to a live NautilusTrader
`TradingNode`. Run them from the repository root:

```bash
python examples/01_public_rest.py
```

| Example                                                  | What it shows                                                                                                                                                               | Needs                                                 |
|----------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------|
| [`01_public_rest.py`](01_public_rest.py)                 | The async REST transport with one typed namespace per product: spot spec and book, perpetual contract, delivery contracts, option chain                                     | Nothing (public data)                                 |
| [`02_public_websocket.py`](02_public_websocket.py)       | `GateioPublicWebSocket` on two products at once: real best bid/offer and public trades, plus transport counters                                                             | Nothing (public data)                                 |
| [`03_instruments.py`](03_instruments.py)                 | `GateioInstrumentProvider` building `CurrencyPair`, `CryptoPerpetual` (linear and inverse), `CryptoFuture` and `CryptoOption`, and what a `Quantity` means on each          | Nothing (public data)                                 |
| [`04_trading_node_data.py`](04_trading_node_data.py)     | A live `TradingNode` with the Gate.io data client: quotes, trades and closed bars for a spot pair and a perpetual simultaneously                                            | Nothing (public data)                                 |
| [`05_account_readonly.py`](05_account_readonly.py)       | Authenticated read-only inspection: fee tier, per-product wallets (including one that does not exist yet), resting orders                                                   | API credentials                                       |
| [`06_testnet_orders.py`](06_testnet_orders.py)           | A spot **testnet** order round-trip through the REST namespace rather than a `TradingNode`: place a far-from-market limit buy, confirm it rests, cancel it                  | Testnet credentials **and** `GATEIO_ALLOW_ORDERS=YES` |
| [`07_trading_node_orders.py`](07_trading_node_orders.py) | One spot **testnet** order through a live `TradingNode`: both clients, both factories, a post-only limit far from the market, canceled on stop. Never run against the venue | Testnet credentials **and** `GATEIO_ALLOW_ORDERS=YES` |

Examples 01 to 04 need no credentials and touch only public endpoints.

These are the examples on the default branch. A checkout of `v0.2.0a1` has six of
them — `07_trading_node_orders.py` is newer than that release.

## Symbology in the examples

Instrument ids follow the adapter's rule: the exact venue symbol, with `-PERP`
on perpetuals only.

```text
BTC_USDT.GATE_IO                     spot
BTC_USDT-PERP.GATE_IO                USDT-margined perpetual
BTC_USD-PERP.GATE_IO                 BTC-margined (inverse) perpetual
SOL_USDT_YYYYMMDD.GATE_IO            delivery future (an expiration the venue lists today)
BTC_USDT-YYYYMMDD-STRIKE-P.GATE_IO   option
```

Note the venue string is `GATE_IO`, not `GATEIO` — that changed in 0.2.0.

## Safety in the order examples

The adapter has **no order kill switch**, and `environment` defaults to
`"mainnet"`. The gates below live in `06_testnet_orders.py` and
`07_trading_node_orders.py` themselves, and are how an example script should
behave — they are not adapter features:

1. **Explicit opt-in.** The script refuses to run unless `GATEIO_ALLOW_ORDERS=YES`
   is set for that run, so keys sitting in the environment cannot place an order
   on their own.
2. **Testnet, stated.** 06 takes its base URL from the `GATEIO_HTTP_TESTNET`
   constant, so nothing can redirect it to mainnet. 07 sets
   `environment="testnet"` on both clients, and the execution client prints
   `Environment: testnet` as it starts.
3. **Bounded notional.** The order value is capped in the script and the limit
   price sits far below the market, so it cannot fill.
4. **Cancel whatever happens.** 06 cancels in `finally` and says so loudly if the
   cancel itself fails; 07 cancels in `on_stop`. Check the venue afterwards
   anyway — two of four recorded mainnet shutdowns ended with an order still on
   the book.

07 has never been run against Gate.io, on either environment. Run it once on the
testnet before you rely on it.

For real deployments the controls that bind are the API key's permissions and
IP allowlist, plus an explicitly chosen `environment` — see
[docs/configuration.md](../docs/configuration.md#safety-model).

Scripts that need credentials exit with a clear message, and no traceback, when
the environment variables are missing.

## Environment variables

| Variable                                           | Used by    | Purpose                                                 |
|----------------------------------------------------|------------|---------------------------------------------------------|
| `GATE_API_KEY` / `GATE_API_SECRET`                 | 05         | Mainnet credentials (and the testnet fallback)          |
| `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` | 05, 06, 07 | Gate.io **testnet** credentials. 07 takes no other pair |
| `GATEIO_ENVIRONMENT`                               | 05         | `testnet` (default) or `mainnet`                        |
| `GATEIO_ALLOW_ORDERS`                              | 06, 07     | Must be exactly `YES` for the script to place an order  |
