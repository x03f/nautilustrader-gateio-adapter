# Market data

`GateioDataClient` delivers Gate.io market data to a NautilusTrader live node.
Public market data requires no credentials.

**Everything this client publishes is data the venue actually sent.** There are
no synthesised quotes, no derived ticks and no interpolation. Version 0.1.0 had
an option that fabricated `QuoteTick`s around bar closes; it is gone (see the
[migration guide](migration-0.1-to-0.2.md)).

One client multiplexes every configured product. Each product has its own
WebSocket host, so the client opens one public socket per configured product and
routes each message by the endpoint it arrived on and the channel it names.

## Subscriptions

| Nautilus subscription | Gate.io source | Products |
|---|---|---|
| `subscribe_trade_ticks` | `{spot,futures,options}.trades` | all |
| `subscribe_quote_ticks` | `{spot,futures,options}.book_ticker` (real BBO) | all |
| `subscribe_order_book_deltas` | REST snapshot + `*.order_book_update`, sequence-validated | all |
| `subscribe_bars` | `*.candlesticks`, closed bars only | all |
| `subscribe_mark_prices` | `futures.tickers` `mark_price` field | perpetual, inverse, delivery |
| `subscribe_index_prices` | `futures.tickers` `index_price` field | perpetual, inverse, delivery |
| `subscribe_funding_rates` | `futures.tickers` `funding_rate` field | perpetual, inverse |
| `subscribe_instruments` / `subscribe_instrument` | REST, refreshed by the reload task | all |

## Requests (historical / on demand)

| Nautilus request | Gate.io source | Notes |
|---|---|---|
| `request_bars` | REST `*/candlesticks` | Paginated at 1000 rows per call; still-open buckets are dropped; results sorted oldest-first and trimmed to `limit` |
| `request_trade_ticks` | REST `*/trades` | Capped at 1000 rows; filtered to the requested `start`/`end` window |
| `request_order_book_snapshot` | REST `*/order_book` | Depth clamped to the nearest value the product accepts (options stop at 50) |
| `request_instrument` / `request_instruments` | Instrument provider | Loads on demand if not already cached |

## Bars

Subscribe with a standard bar type using `EXTERNAL` aggregation and the `LAST`
price type, for example `BTC_USDT.GATE_IO-1-MINUTE-LAST-EXTERNAL`. A bar
specification with no Gate.io equivalent raises `ValueError` naming the
supported set, rather than silently substituting an interval.

| Bar spec | Gate.io interval |
|---|---|
| `1-SECOND` | `1s` |
| `10-SECOND` | `10s` |
| `1-MINUTE` | `1m` |
| `5-MINUTE` | `5m` |
| `15-MINUTE` | `15m` |
| `30-MINUTE` | `30m` |
| `1-HOUR` | `1h` |
| `4-HOUR` | `4h` |
| `8-HOUR` | `8h` |
| `1-DAY` | `1d` |
| `7-DAY` | `7d` |

**Only closed bars are emitted.** Spot and perpetual candlestick messages carry
a window-close flag (`w`); delivery and options do not, so a bar there is held
until the next bucket opens plus a short grace period. Either way the first bar
of a subscription arrives after the interval it covers has ended — a 1-minute
subscription produces nothing for up to a minute.

`bars_timestamp_on_close` (default `True`) controls whether a bar is stamped at
the close of its interval, which is the Nautilus convention, or at the open,
which is what Gate.io's `t` field means.

## Order books

Gate.io publishes depth as a REST snapshot plus an incremental WebSocket stream.
`nautilus_gateio.books.GateioOrderBook` implements the venue's documented
synchronisation algorithm:

1. subscribe to `*.order_book_update` and buffer the notifications;
2. fetch the REST snapshot with `with_id=true` and keep its `id`;
3. discard buffered notifications whose `u` is older than the snapshot;
4. start applying at the notification that straddles the snapshot,
   `U <= id + 1 <= u`;
5. if the snapshot predates the whole buffer, fetch a newer one;
6. if a later notification has `U > previous u + 1`, updates were lost: the book
   raises `OrderBookSequenceError`, the gap counter increments, a warning is
   logged, the book is re-snapshotted over REST, and the deltas published carry
   the `F_SNAPSHOT` clear flag;
7. a snapshot that is not newer than the state the book already holds is
   discarded, so a slow REST response cannot roll a resynced book backwards.

Level amounts are absolute, not deltas: size `0` deletes the level. Messages
with empty `a`/`b` arrays still advance the update id and are not skipped.

Two configuration values must agree with the venue's rules:

* `order_book_update_interval_ms` — `20`, `100` or `1000`. Spot derives the
  depth from the interval (`20ms` gives 20 levels, `100ms` gives 100); perpetual
  `20ms` streams serve 20 levels only; delivery and options accept `100` and
  `1000` only.
* `order_book_snapshot_limit` — one of `1, 5, 10, 20, 50, 100`. The snapshot
  must match the depth the stream serves, otherwise the two cover different
  price ranges and the book stays misaligned.
  `GateioPublicWebSocket.effective_depth()` returns the depth a given interval
  will actually serve.

Gate.io offers no stream resumption, so every reconnect re-snapshots each local
book before applying further updates.

## Quotes and trades

* **Quotes** come from `*.book_ticker`, the venue's real best bid/offer stream,
  with the venue's own sizes. An empty bid or ask side is treated as absent, not
  published as a zero.
* **Trades** come from `*.trades`. On spot the message carries the taker's
  `side` explicitly; on futures, delivery and options the sign of `size` carries
  it (positive is a taker buy). The venue trade id becomes the Nautilus
  `TradeId`.

A size that truncates to zero at the instrument's precision is treated as absent
rather than published as a zero-sized entry, which NautilusTrader would reject.

## Mark price, index price and funding rate

All three are fields of the `futures.tickers` stream, so one venue subscription
serves any combination of them; the client reference-counts subscribers and
unsubscribes from the venue channel only when the last one goes away. Funding
rates exist for perpetuals; delivery contracts publish mark and index prices but
no funding.

## Instruments

Gate.io publishes no instrument-definition channel, so instruments are refreshed
by a polling task controlled by `update_instruments_interval_mins` (default 60;
`None` disables it). This follows the same approach as other NautilusTrader
adapters for venues without such a channel.

## Using the WebSocket layer directly

The transport is usable without a Nautilus node:

```python
import asyncio

from nautilus_gateio import GateioProductType, GateioPublicWebSocket


async def main() -> None:
    ws = GateioPublicWebSocket(
        product=GateioProductType.PERP,
        handler=lambda message: print(message["channel"], message.get("result")),
    )
    await ws.connect()
    await ws.subscribe_book_ticker("BTC_USDT")
    await ws.subscribe_trades("BTC_USDT")
    await asyncio.sleep(10)
    await ws.disconnect()


asyncio.run(main())
```

Channels without a typed helper remain reachable through the underlying client:
`await ws.client.subscribe("options.mark_prices", ["BTC_USDT"])`.

## Reliability

`GateioWebSocketClient` reconnects with capped exponential backoff, replays its
subscriptions on the new connection, sends application-level pings when the link
goes idle, recycles a connection that has delivered nothing for
`recv_timeout_secs`, and exposes counters (`reconnects`, `messages_received`,
`subscribe_failures`, `last_message_ns`, `last_pong_ns`) for health monitoring.
Owners of local order books register an `on_reconnect` callback and re-snapshot
from it.
