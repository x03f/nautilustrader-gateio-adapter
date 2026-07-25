# Architecture

This document describes how the adapter is put together: the module map, the
runtime dataflows, and the design decisions behind the less obvious choices.

## Module map

All modules live in the flat `nautilus_gateio` package.

| Module | Responsibility |
|---|---|
| `config.py` | Config classes (`GateioDataClientConfig`, `GateioExecClientConfig`, `GateioPaperConfig`) and `resolve_credentials()` |
| `constants.py` | Venue identifiers, REST/WS endpoints, bar-interval maps, client-order-id constraints |
| `data.py` | `GateioDataClient` — live market-data client (bars over WS with REST fallback, historical bars, synthetic quotes) |
| `errors.py` | Typed error hierarchy (`GateioError`, `GateioClientError`, `GateioServerError`, `LiveOrdersDisabledError`, `OrderValidationError`) and retry classification (`should_retry`) |
| `execution.py` | `GateioExecutionClient` — live spot execution client (order routing, fill polling, account state) |
| `factories.py` | `GateioLiveDataClientFactory` / `GateioLiveExecClientFactory` for `TradingNode` registration |
| `futures.py` | Experimental raw REST clients for USDT-perpetual futures (`GateioFuturesPublicClient`, `GateioFuturesPrivateClient`) — not integrated with Nautilus |
| `http.py` | `GateioHttpClient` — synchronous Gate.io API v4 REST client with signing, rate limiting (`RateLimiter`), and the `live_orders` safety switch |
| `paper.py` | `PaperExecution` — local paper-fill simulator driven by live public data (orders never leave the process) |
| `providers.py` | `GateioInstrumentProvider` / `StaticInstrumentProvider`, `build_currency_pair()`, `get_currency()` |
| `reconcile.py` | `reconcile()` — standalone diagnostic comparison of local state vs. exchange state (read-only) |
| `schemas.py` | Pure parsing helpers for API v4 payloads (`parse_order`, `parse_candle`, `parse_balances`, ..., `validate_order`) |
| `signing.py` | HMAC-SHA512 request signing (`sign_request`) and client-order-id helpers (`generate_client_order_id`, `sanitize_client_order_id`) |
| `symbols.py` | Symbol conversion between Nautilus instrument ids and Gate.io currency pairs |
| `websocket.py` | `GateioWebSocketClient` — public spot WebSocket transport (candlesticks/trades) with reconnect, dedup, and gap detection |

## Dataflow

### Market data (inbound)

```text
Gate.io WS (spot.candlesticks)
        |
        v
GateioWebSocketClient          -- closed-bar filter, dedup, out-of-order drop,
        |                         gap detection, reconnect with backoff
        v  on_bar callback (plain dict OHLCV)
GateioDataClient._emit_bar
        |
        +--> Bar  ------------------> msgbus --> strategies / actors
        |
        +--> QuoteTick (synthetic, --> msgbus     [optional, see below]
              close +/- 0.5 bp)

Fallback path (WS failure or use_websocket=False):

Gate.io REST /spot/candlesticks
        |
        v
GateioDataClient._stream_poll  -- polls every poll_interval_secs, emits the
        |                         last CLOSED candle once per new timestamp
        v
GateioDataClient._emit_bar --> msgbus
```

Historical requests follow a separate, simpler path:

```text
Strategy.request_bars --> GateioDataClient._request_bars
        --> REST /spot/candlesticks (limit capped at 1000)
        --> list[Bar] --> msgbus response
```

### Execution (outbound + inbound events)

```text
Strategy.submit_order
        |
        v
GateioExecutionClient._submit_order
        |                         (MARKET is emulated as an aggressive
        v                          LIMIT IOC — see design decisions)
GateioHttpClient.place_order  --> REST POST /spot/orders
        |
        v
OrderSubmitted / OrderAccepted / OrderRejected events --> msgbus

Fill detection (no private WebSocket yet):

  post-submit check (~0.6 s)  \
                               >--> GateioHttpClient.order (REST GET)
  _poll_loop (every            /         |
   account_poll_interval_secs)           v
                              delta vs. reported_fill
                                         |
                                         v
              OrderFilled / OrderCanceled events --> msgbus
              AccountState push after each fill and each poll cycle
```

## Design decisions

### WebSocket primary, REST fallback

The WebSocket candlestick stream is the primary bar transport: it is pushed,
cheap, and carries an explicit window-close flag. If the WS transport fails
(exception escaping the reconnect loop), the same subscription transparently
degrades to REST polling of `/spot/candlesticks`, which emits the last closed
candle whenever its timestamp advances. Both transports feed the same
`_emit_bar` path, so downstream consumers cannot tell them apart.

### Closed bars only

Both transports emit only *closed* bars (WS: the `window_close` flag; REST:
the second-to-last row of the candle response). In-progress candles mutate on
every trade; emitting them would produce a stream of partial bars that most
strategies mishandle. The trade-off is latency: a bar arrives only after its
interval ends, so the 1-minute bar spec is the lowest-latency option.

### Synthetic quotes

When `emit_synthetic_quotes` is enabled (the default), each closed bar also
emits a `QuoteTick` centered on the bar close with a fixed +/- 0.5 basis-point
half-spread and unit sizes. The single purpose is to feed quote-driven
execution simulations (sandbox/backtest-style fill models that need
top-of-book updates). These ticks are **not** real market quotes — they carry
no real spread, depth, or intra-bar movement. Disable them if any component
treats quote ticks as market truth. See `docs/market-data.md`.

### MARKET emulated as an IOC limit

Gate.io spot market **buy** orders interpret the `amount` field as *quote*
currency (e.g. USDT to spend), while Nautilus quantities are in *base*
currency. Rather than converting at a guessed price, the execution client
emulates MARKET orders as aggressive LIMIT IOC orders that cross the spread
by 1% (last price * 1.01 for buys, * 0.99 for sells). This keeps quantity
semantics exact, bounds worst-case slippage at 1%, and lets the exchange
cancel any unfillable remainder (surfaced as `OrderCanceled`). See
`docs/execution.md`.

### REST polling for fills

The adapter has no private WebSocket integration yet. Fills are detected by
polling `GET /spot/orders/{id}`: an immediate check ~0.6 s after submission
(aggressive orders usually fill instantly) plus a periodic loop every
`account_poll_interval_secs`. Each poll computes the fill delta versus the
last reported amount, so partial fills generate incremental `OrderFilled`
events. The cost is fill-report latency up to one poll interval.

### Fresh-start reconciliation semantics

`generate_order_status_reports`, `generate_fill_reports`, and
`generate_position_status_reports` return empty results: the client does not
adopt pre-existing exchange state on start-up, and Nautilus starts from a
clean slate. Pre-existing open orders are *not* managed by the node. For a
read-only diagnostic comparison of local vs. exchange state, use the
standalone `nautilus_gateio.reconcile.reconcile()` helper — it reports
discrepancies and recommended actions but never mutates anything.

### Layered order-flow safety

Credentials alone never enable order placement. `GateioHttpClient` requires
an explicit `live_orders=True` before any mutating endpoint works
(`LiveOrdersDisabledError` otherwise), and `GateioExecClientConfig` defaults
to the Gate.io testnet host — mainnet requires `environment="mainnet"`
explicitly. The experimental futures private client goes further and refuses
to operate on any non-testnet host at all.

## Conventions

The package follows the structure conventions of the official NautilusTrader
adapters: a flat `config` / `constants` / `data` / `execution` / `factories`
/ `providers` module layout modeled on the Bybit and OKX adapters, with the
transport layer (HTTP and WebSocket clients separate from the Nautilus-facing
clients) modeled on the Binance adapter.

Deliberate deviations from the official adapters:

* **Plain-Python transports.** REST uses `httpx` and WebSocket uses
  `websockets` instead of the `nautilus_pyo3` Rust network clients. This
  keeps the package pure Python — installable anywhere, debuggable with
  standard tooling — and is acceptable at this adapter's throughput
  (closed-bar market data and low-frequency spot order flow).
* **Dict-based schemas.** Payload parsing returns plain dicts
  (`schemas.py`) rather than `msgspec` structs. Fewer dependencies, and
  honest about the package's maturity: field-level typing can be added once
  the payload surface stabilizes.
* **Single flat package.** No `spot/` and `futures/` subpackages: spot is
  the supported product, and futures support is an experimental REST-only
  module (`futures.py`) that would not justify a parallel tree yet.
