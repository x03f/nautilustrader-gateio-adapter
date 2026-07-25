# Market data

`GateioDataClient` provides Gate.io spot market data to a Nautilus live node.
Public data requires no credentials.

## Supported

### Live bars (EXTERNAL aggregation)

Subscribe with a standard Nautilus bar type using `EXTERNAL` aggregation, e.g.
`BTC_USDT.GATEIO-1-MINUTE-LAST-EXTERNAL`. Supported intervals:

| Bar spec | Gate.io interval |
|---|---|
| `1-MINUTE` | `1m` |
| `5-MINUTE` | `5m` |
| `15-MINUTE` | `15m` |
| `30-MINUTE` | `30m` |
| `1-HOUR` | `1h` |
| `4-HOUR` | `4h` |
| `8-HOUR` | `8h` |
| `1-DAY` | `1d` |

Delivery is via the WebSocket `spot.candlesticks` channel by default, with
automatic REST-polling fallback (`/spot/candlesticks`) if the WebSocket
transport fails, or exclusively via REST when `use_websocket=False`.

**Only closed bars are emitted.** The WebSocket stream is filtered on the
window-close flag and the REST poller emits only the last completed candle.
A bar therefore arrives shortly *after* its interval ends — expect no data
for up to a full interval after subscribing (see
[troubleshooting.md](troubleshooting.md)).

### Historical bars

`request_bars` serves historical bars over REST from `/spot/candlesticks`.
The `limit` is capped at 1000 rows per request (Gate.io API limit); a request
without a limit defaults to 500. Optional `start` / `end` bounds are passed
through as Unix-second `from` / `to` parameters. Rows are sorted oldest-first
before delivery.

## Reliability behavior

The WebSocket transport (`GateioWebSocketClient`) implements, and unit-tests,
the following:

* **Reconnect with backoff.** Connection failures reconnect with exponential
  backoff (1 s doubling up to `max_backoff`, default 30 s). A successful
  connect resets the backoff, and all subscriptions are replayed on
  reconnect.
* **Deduplication.** A bar with the same timestamp as the last emitted bar
  for that (pair, interval) is dropped.
* **Out-of-order drop.** A bar older than the last emitted bar is dropped.
* **Gap detection.** If the spacing between consecutive bars exceeds 1.5x
  the interval, the gap counter increments and the affected bar is emitted
  with `gap=True`. The REST poller applies the same 1.5x rule.

### Metrics

Both the data client and the WebSocket client expose a `metrics()` method
with transport reliability counters:

* `GateioDataClient.metrics()` — `reconnect_count`, `gaps_detected`,
  `last_event_ms`, aggregated across all active streams.
* `GateioWebSocketClient.metrics()` — `reconnect_count`, `gaps_detected`,
  `messages`, `last_event_ms` for one connection.

Use `last_event_ms` for staleness alerts and `gaps_detected` to decide
whether to re-request historical bars over REST.

## Synthetic quotes

**Read this section before consuming `QuoteTick` data from this adapter.**

When `emit_synthetic_quotes` is enabled (the **default**), the data client
emits one synthetic `QuoteTick` alongside each closed bar:

* bid = bar close - 0.5 basis point, ask = bar close + 0.5 basis point;
* bid/ask sizes are fixed at 1.0;
* timestamps match the bar.

These ticks exist for exactly one purpose: quote-driven execution
simulations (sandbox/backtest-style fill models) need top-of-book updates to
produce fills, and Gate.io bars alone would leave them starved.

They are **not real market quotes**:

* the spread is a fixed constant, not the live spread;
* the sizes are meaningless placeholders;
* there is at most one quote per bar interval — no intra-bar movement.

Any strategy or component that treats quote ticks as market truth (spread
analysis, microstructure signals, quote-based risk checks) must not run with
synthetic quotes enabled. **To disable:** set `emit_synthetic_quotes=False`
in `GateioDataClientConfig`.

## Not supported

* **Real quote subscriptions** — no `spot.book_ticker` integration; the only
  `QuoteTick`s are the synthetic ones described above.
* **Trade tick subscriptions into Nautilus** — `GateioWebSocketClient` can
  subscribe to `spot.trades` at the transport level (`subscribe_trades`),
  but the data client does not convert them into Nautilus `TradeTick`
  objects.
* **Order-book streams** — no depth/order-book deltas or snapshots into
  Nautilus. (REST snapshots are available on `GateioHttpClient.order_book`
  for direct use.)
* **Funding-rate streams** — futures support is an experimental REST-only
  module, not a data-client feature.
