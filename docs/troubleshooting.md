# Troubleshooting

Real failure modes seen with this adapter, and how to fix them.

## `INVALID_SIGNATURE` on private requests

Gate.io rejects the request signature. The two usual causes:

* **Clock drift.** The signature includes a Unix-second timestamp; if your
  local clock is off, signatures are rejected. Call
  `GateioHttpClient.sync_time()` once after constructing the client — it
  measures the offset against the exchange's `/spot/time` and applies it to
  all subsequent signed requests. Also consider enabling NTP on the host.
* **Whitespace in keys.** A key or secret pasted with a trailing newline or
  spaces produces a wrong HMAC. `resolve_credentials()` strips surrounding
  whitespace from environment-sourced values, but explicit config values are
  used as-is — check for stray whitespace if you pass keys directly.

Also verify you are using the key pair for the right environment: testnet
keys are not valid on mainnet and vice versa.

## `TOO_MANY_REQUESTS` (HTTP 429)

You are hitting Gate.io rate limits. The built-in `RateLimiter` paces
requests (default 8 requests/second) and backs off exponentially on 429; the
private request path retries up to 3 times before raising
`GateioError(429, "TOO_MANY_REQUESTS", ...)`. If you still see this:

* lower `max_per_sec` when constructing `GateioHttpClient`;
* increase `account_poll_interval_secs` (each cycle polls every pending
  order plus balances);
* reduce the number of concurrent clients sharing one API key — the limiter
  is per-client-instance, not global.

## Balances come back empty

`balances()` returns `{}` (or the node shows no account state) even though
the account is funded. Almost always a host mismatch: you are asking the
**testnet** host with a mainnet-funded account, or the **mainnet** host with
testnet credentials. Check `GateioExecClientConfig.environment` (default is
`"testnet"`!) and which key pair the environment variables hold. Testnet
balances exist only on `https://api-testnet.gateapi.io` and mainnet balances
only on `https://api.gateio.ws`.

## `label: INVALID_CURRENCY_PAIR`

The symbol form is wrong. Gate.io pairs use an underscore: `BTC_USDT`, not
`BTCUSDT` or `BTC/USDT`. In Nautilus terms, use the canonical instrument id
`BTC_USDT.GATEIO`. The compatibility form `BTCUSDT.GATEIO` is accepted by
`instrument_id_to_gate_pair()` for known quote suffixes (USDT, USDC, BTC,
ETH) but the underscore form is recommended — it is lossless and is what
`GateioInstrumentProvider` produces. Also confirm the pair is actually
listed and tradable (`GateioHttpClient.currency_pair("BTC_USDT")`,
`trade_status == "tradable"`).

## WebSocket connects but no bars arrive

Not a bug — the adapter emits **closed bars only**. After subscribing, the
first bar arrives when the current interval *closes*: up to 1 minute for a
`1-MINUTE` spec, up to a full day for `1-DAY`. If you need fast feedback
while wiring things up, subscribe to a `1-MINUTE` bar type first — it is the
fastest supported interval. To verify liveness at the transport level, check
`GateioDataClient.metrics()["last_event_ms"]` or the WebSocket client's
`messages` counter, which counts every received message including
in-progress candle updates that are filtered out.

## MARKET order rejected: below minimum notional

Gate.io enforces a minimum order value per pair (`min_quote_amount`, e.g.
around 1-3 USDT on major pairs) plus a minimum base amount and precision
constraints. An order below the minimum is rejected by the exchange and
surfaced as `OrderRejected`. Check the instrument's constraints via
`GateioHttpClient.currency_pair(pair)` and size orders above
`min_quote_amount` at the current price. For direct REST usage,
`place_order_validated()` checks minimums and precision locally
(raising `OrderValidationError`) before the request leaves the process.

## Orders rejected with `LiveOrdersDisabledError`

The `live_orders` safety switch is off — the deliberate default for any
directly constructed `GateioHttpClient`. Credentials alone never enable
order flow. Construct the client with `live_orders=True` to allow mutating
calls. (The Nautilus execution client sets this itself; wiring it into a
`TradingNode` is the opt-in.) Note that `emergency_stop()` flips the switch
back off permanently for that client instance.

## `instrument not found` when submitting orders or subscribing

The instrument is not in the cache because the provider never loaded it.
`GateioInstrumentProvider` loads on demand — configure your node's
instrument provider with the ids you trade, or load them explicitly:

```python
await provider.load_ids_async([InstrumentId.from_str("BTC_USDT.GATEIO")])
```

Prefer `load_ids_async` with an explicit list over `load_all_async`, which
fetches thousands of pairs.

## Installation fails: Python or NautilusTrader version mismatch

The package requires **Python >= 3.12** (and < 3.15) and
**`nautilus_trader >= 1.230.0, < 2`**. On older Pythons, `pip` either
refuses to install or resolves an ancient version; with an out-of-range
NautilusTrader, imports can fail on moved modules (e.g. the live client base
classes). Check with:

```bash
python --version
python -c "import nautilus_trader; print(nautilus_trader.__version__)"
```

and upgrade inside a fresh virtual environment if either is out of range.
