# Market data

`GateioDataClient` publishes Gate.io market data into a NautilusTrader live node.
Public market data needs no credentials: a client configured without an API key
can stream and request everything described here.

Everything this client publishes is data the venue actually sent. Nothing is
interpolated and no data type is manufactured out of another one — quotes come
from the venue's own best bid/offer stream, trades from the public trade stream,
bars from closed candlesticks, and book deltas from the incremental depth stream
aligned against a REST snapshot. Version 0.1.0 had an option that fabricated
quote ticks around bar closes; it was removed in 0.2.0 and has no replacement,
because a quote that no participant could have traded against is indistinguishable
from real data once it reaches a strategy (see the
[migration guide](migration-0.1-to-0.2.md)).

One client multiplexes every product named in `products`. Gate.io serves each
product family from its own WebSocket host, so the client opens one public socket
per configured product and routes each message by the socket it arrived on and
the channel it names. A subscription for a product that is not configured is
refused with an error naming the configured set, rather than opening a connection
the configuration never asked for.

## Maturity of this page

This is an alpha release of an external, community-maintained adapter, written in
pure Python against NautilusTrader 1.230.0. **Live validation of market data
covers the spot streams and requests, the instrument load on every configured
product, and the ticker-derived streams on the USDT perpetual — and stops
there.** The statuses below mean:

* *implemented and mock-tested* — the path is exercised by the offline test suite
  against payload shapes that mirror what Gate.io sends, with no socket opened and
  no credentials read;
* *implemented, mainnet validation pending* — the code path exists and was read
  and reviewed, but the offline suite does not cover it end to end;
* *unsupported* — not implemented.

The **mainnet** column is a separate axis: it names the products for which a
recorded live run exists, with the run itself written down in
[validation.md](validation.md). A dash means the venue has never been observed
to serve this path through the adapter.

| Capability                                                           | Status                                                         | Mainnet                                                |
|----------------------------------------------------------------------|----------------------------------------------------------------|--------------------------------------------------------|
| Instrument loading through the provider                              | implemented and mock-tested                                    | spot, perpetual, delivery, options                     |
| Instrument reload task inside the data client                        | implemented, mainnet validation pending                        | —                                                      |
| Trade ticks                                                          | implemented and mock-tested                                    | spot                                                   |
| Quote ticks from `book_ticker`                                       | implemented and mock-tested                                    | spot                                                   |
| Order book deltas, sequence validation and gap resync                | implemented and mock-tested                                    | spot (deltas, interval snapshots and the managed book) |
| Order book snapshot on request                                       | implemented and mock-tested                                    | spot                                                   |
| Bars from the candlestick streams                                    | implemented and mock-tested                                    | spot                                                   |
| Historical bars and trades over REST                                 | implemented; the offline suite covers the HTTP layer only      | spot                                                   |
| Mark price, index price, funding rate                                | implemented and mock-tested                                    | USDT perpetual                                         |
| Historical funding rates over REST                                   | implemented; the offline suite covers the HTTP layer only      | USDT perpetual                                         |
| Book resynchronization after a reconnect                             | implemented, mainnet validation pending                        | —                                                      |
| `OrderBookDepth10` from the periodic `*.order_book` snapshot channel | implemented and mock-tested                                    | —                                                      |
| Instrument status, polled from the instrument listings               | implemented and mock-tested                                    | —                                                      |
| Instrument close for delivery futures and options                    | implemented and mock-tested                                    | —                                                      |
| `GateioTicker` custom data (the venue's whole ticker row)            | implemented and mock-tested                                    | —                                                      |
| Historical quotes                                                    | unsupported; Gate.io publishes no quote history on any product | not applicable                                         |

Nothing here is described as stable. A dash means no recorded run credits that
path to the venue: the instrument reload timer and the post-reconnect book
resynchronization were never observed live, and the delivery and options streams
were subscribed in a run that counts arrivals per data type rather than per
instrument, so nothing is attributed to them individually. The detail is in
[validation.md](validation.md).

## Connecting

`_connect()` runs in a fixed order, and the order is the point:

1. initialize the instrument provider over REST;
2. publish every loaded currency and instrument into the cache and the data
   engine;
3. open one public WebSocket per configured product;
4. start the instrument reload task and the pending-bar flush task.

Instruments are cached before any socket is opened because every parse needs the
instrument's price and size precision. A message parsed without it would not fail
loudly — it would produce a plausible price at the wrong scale.

The provider only loads what NautilusTrader's own provider configuration tells it
to. With neither `load_all=True` nor `load_ids` on the `InstrumentProviderConfig`,
nothing is loaded, and the client then drops every message for which it holds no
instrument — in most cases without an error, because an unknown instrument is not
a venue failure. If a subscription is acknowledged but no data reaches the
strategy, that configuration is the first thing to check.

```python
from nautilus_trader.common.config import InstrumentProviderConfig

from nautilus_gateio import GateioDataClientConfig, GateioProductType

config = GateioDataClientConfig(
    products=(GateioProductType.SPOT, GateioProductType.PERP),
    instrument_provider=InstrumentProviderConfig(
        load_ids=frozenset(["BTC_USDT.GATE_IO", "BTC_USDT-PERP.GATE_IO"]),
    ),
)
```

A complete runnable node is in
[`examples/04_trading_node_data.py`](../examples/04_trading_node_data.py).

## Instruments

Gate.io publishes no instrument-definition channel, so there is nothing to
subscribe to. Instrument state is refreshed by a polling task controlled by
`update_instruments_interval_mins` (default 60 minutes; `None` disables it),
which is the same approach other NautilusTrader adapters take for venues without
such a channel. `subscribe_instruments` logs that fact and republishes what is
already cached; `request_instrument` loads a single instrument on demand if the
cache does not hold it.

Which instruments exist per product, and what a `Quantity` means on each, is in
[products.md](products.md) and [symbology.md](symbology.md).

## Subscriptions

| Nautilus subscription                            | Gate.io source                                                                 | Products                              |
|--------------------------------------------------|--------------------------------------------------------------------------------|---------------------------------------|
| `subscribe_trade_ticks`                          | `{spot,futures,options}.trades`                                                | all                                   |
| `subscribe_quote_ticks`                          | `{spot,futures,options}.book_ticker`                                           | all                                   |
| `subscribe_order_book_deltas`                    | REST snapshot plus `*.order_book_update`, sequence-validated                   | all                                   |
| `subscribe_order_book_depth`                     | `*.order_book`, the venue's periodic snapshot channel                          | all                                   |
| `subscribe_bars`                                 | `*.candlesticks`, closed bars only                                             | all                                   |
| `subscribe_mark_prices`                          | `futures.tickers` / `options.contract_tickers`, `mark_price` field             | perpetual, inverse, delivery, options |
| `subscribe_index_prices`                         | `futures.tickers` / `options.contract_tickers`, `index_price` field            | perpetual, inverse, delivery, options |
| `subscribe_funding_rates`                        | `futures.tickers`, `funding_rate` field                                        | perpetual, inverse                    |
| `subscribe_data` (`GateioTicker`)                | `futures.tickers` / `options.contract_tickers` / `spot.tickers`, the whole row | all                                   |
| `subscribe_instrument_status`                    | REST instrument listings, polled on the reload cadence                         | all                                   |
| `subscribe_instrument_close`                     | REST settlement, polled after expiry                                           | delivery, options                     |
| `subscribe_instruments` / `subscribe_instrument` | REST, refreshed by the reload task                                             | all                                   |

A hook this client does not implement fails in a way worth knowing about, and
`subscribe_option_greeks` is now the one that demonstrates it.
`LiveMarketDataClient` records the subscription *before* it starts the task that
raises `NotImplementedError`, so a caller gets both: one exception in the log,
and a subscription the client then reports as held for the rest of its life. The
data engine skips anything already in that list, so the subscription is never
retried and no second message is ever logged. Read the log line, not the
subscription list. (Verified against the installed NautilusTrader 1.230.0.) It is
also why every refusal in this client is a log line and a return rather than a
raise.

## Requests

| Nautilus request              | Gate.io source                        | Behavior                                                                                                                                                                                                                                                                        |
|-------------------------------|---------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `request_bars`                | REST `*/candlesticks`                 | Paginated at 1000 rows per call; buckets that have not closed yet are dropped; rows are keyed by open time so overlapping pages collapse; a row the platform rejects is dropped and counted rather than failing the request; results sorted oldest-first and trimmed to `limit` |
| `request_trade_ticks`         | REST `*/trades`                       | At most 1000 rows, filtered client-side to the `start`/`end` window (see the caveat below)                                                                                                                                                                                      |
| `request_funding_rates`       | REST `/futures/{settle}/funding_rate` | Perpetual only; at most 1000 records, filtered client-side to the `start`/`end` window; sorted oldest-first                                                                                                                                                                     |
| `request_order_book_snapshot` | REST `*/order_book`                   | Depth clamped to a value the product accepts; published as one `F_SNAPSHOT` batch                                                                                                                                                                                               |
| `request_instrument`          | Instrument provider                   | Loads that one instrument from the venue if it is not already cached                                                                                                                                                                                                            |
| `request_instruments`         | Instrument provider                   | Answers with the provider's current contents; loads nothing                                                                                                                                                                                                                     |
| `request_quote_ticks`         | none                                  | Refused at error level, naming the venue and the alternatives. Gate.io publishes no quote history on any product: `GET /*/tickers` is one current row, not a series, and answering from it would invent history                                                                 |

**A refused request never completes.** The platform opens a request group for
every historical request and only a response closes it, so a caller awaiting the
callback for `request_quote_ticks` waits either way; what the refusal changes is
that the log carries a sentence naming the venue fact rather than a traceback.
This is the same shape Binance and Polymarket use for the histories their venues
do not publish.

**Caveat on venue-wide instrument requests.** The plural form is a read of the
provider, not a fetch. A client configured with
`InstrumentProviderConfig(load_all=False)` and no `load_ids` therefore completes
the request with an empty instruments response and no diagnostic — the request
succeeds, and nothing arrives. Configure the provider with what the strategy
needs, or ask for instruments one at a time. This is the same choice the Tardis,
Polymarket and dYdX adapters make, all of them provider-backed; BitMEX, Kraken,
OKX and Deribit take the other one and re-fetch from the venue on the plural
request. Answering from the provider keeps one definition of an instrument in
the process, which is what reconciliation and order validation both read.

**How the platform answers an instrument request.** This surprises people, so it
is worth stating: NautilusTrader 1.230.0 does not deliver instruments in the
response. Every instrument is handled individually and published on
`data.instrument.<venue>.*`, and the final `DataResponse` carries an empty data
list — the data engine's response handling forces it for an `Instrument`
response (`DataEngine._handle_response`, `data/engine.pyx`, verified against
1.230.0). There is no plural instrument callback on `Actor` at all; the method
of that name on the platform's own `DataTester` is never invoked by anything.
Read the instruments in `on_instrument` as they arrive. A caller that counts
what came back in the response will read zero no matter which adapter answered,
and that is a property of the platform rather than of the venue behind it.

**Caveat on historical trades.** The client asks the venue only for the most
recent rows and applies `start`/`end` itself; it does not page backwards through
the venue's trade history. A window that lies further back than the last 1000
trades of the instrument therefore returns nothing rather than an error. Bars are
the supported way to request history over a long window.

## Trades

Spot messages carry the taker's `side` explicitly. Futures, delivery and options
carry a signed `size` instead, where a positive value is a taker buy and a
negative one a taker sell; there is no separate side field on those products.

* The `TradeId` is always the venue's own trade id, verbatim. A row that arrives
  without one is discarded and counted in `trade_ticks_skipped`, because an
  invented id cannot be deduplicated by a consumer or matched during
  reconciliation, and publishing the literal string `None` as an id is worse than
  publishing nothing.
* Sizes are truncated toward zero at the instrument's `size_precision`. Gate.io
  itself truncates futures sizes the same way unless the fractional-size opt-in
  is requested, which this adapter deliberately does not request: a contract
  instrument reports `size_precision = 0`, so a fraction has no representation in
  the data that would be published.
* A trade whose size truncates to zero — less than one contract — is discarded
  and counted, rather than published as a zero-sized trade.
* Timestamps use `create_time_ms` where present and `create_time` otherwise. The
  unit is decided by magnitude rather than by field name, because a few Gate.io
  endpoints report seconds in a field whose name says milliseconds.

## Quotes

Quotes come from `*.book_ticker`, the venue's real best bid/offer stream, with the
venue's own sizes and its own event timestamp. Nothing is derived from trades or
from bar closes.

A quote is skipped, and counted in `quote_ticks_skipped`, when either side's price
or size is missing or empty, or when either size truncates to zero at the
instrument's precision. The alternative would be a quote asserting a zero-sized
top of book, which is a stronger and more misleading claim than no quote at all.

## Order books

The client serves `L2_MBP` only; a subscription for another book type is refused
with an explicit log message rather than approximated.

Gate.io publishes depth as a REST snapshot plus an incremental WebSocket stream.
The snapshot carries an `id`; every incremental notification carries the range of
update ids it covers, `U` (first) to `u` (last).
`nautilus_gateio.books.GateioOrderBook` implements the venue's documented
synchronization algorithm and is deliberately free of framework dependencies — it
deals in `Decimal` prices and sizes, so it can be tested without a trading
environment:

1. subscribe to `*.order_book_update` and buffer the notifications;
2. fetch the REST snapshot with `with_id=true` and keep its `id`;
3. discard buffered notifications whose `u` is not newer than the snapshot;
4. start applying at the notification that straddles the snapshot,
   `U <= id + 1 <= u`;
5. if the snapshot predates the whole buffer, fetch a newer one — the unconsumed
   notifications are kept for that retry;
6. if a later notification has `U > previous u + 1`, updates were lost:
   `OrderBookSequenceError` is raised, the book is marked unsynchronized and must
   be rebuilt from a new snapshot;
7. a snapshot that is not newer than the state the book already holds raises
   `SnapshotStaleError` and is discarded, so a slow REST response cannot roll a
   book backwards that has meanwhile resynchronized itself.

Level amounts are absolute, not deltas: a size of `0` deletes the level. A
notification with empty `a`/`b` arrays still advances the update id and is not
skipped — treating it as nothing to do would manufacture a gap on the next
message. Gate.io may also push a `full: true` message on the incremental channel;
that is itself a complete snapshot of the subscribed depth and resynchronizes the
book without a REST call.

A `full` push is placed in the stream on the same terms as a REST snapshot,
because the venue documents it as re-pushable at any time:

* one whose `u` is not newer than the local update id describes depth the stream
  has already replaced. It is discarded and counted in `snapshots_stale`, and
  nothing is republished — the book is already correct;
* one carrying no `u` at all cannot be placed in the stream. Keeping the previous
  id would leave the book holding one state while claiming another, so every
  later notification would be measured against the wrong expectation. It raises
  `OrderBookSequenceError` and the book is rebuilt from REST.

`SnapshotStaleError` is deliberately not a subclass of `OrderBookSequenceError`.
A stale snapshot is not a sequence break: the book is still correct and the caller
has nothing to do, whereas a sequence break requires a rebuild.

#### Depth beyond the subscribed window

The incremental channel takes a depth `level`, which the venue describes as the
"optional depth level interested. Only updates within are notified". Whether a
price level that falls out of the top-N window because a better level appeared is
reported as removed is not stated anywhere in the venue's documentation, and this
adapter does not assume an answer: it neither trims the local book to N levels —
that would drop depth the venue may still be maintaining — nor claims the book is
bounded. If the venue does prune, the book stays at N levels by itself; if it does
not, levels below the window survive carrying the last size the venue confirmed
for them, and a snapshot batch republishes them.

One line of a live session settles it: compare `GateioOrderBook.depth` for a busy
instrument against the configured level after a few minutes. Until that
observation exists, treat depth far from the touch as indicative on a long-lived
subscription.

### Gap detection and resynchronization

When the live stream breaks sequence, the client:

* increments the `gaps` and `resyncs` counters for that product and logs a
  warning naming how many updates were missed;
* publishes **no** deltas for the offending message — the local book and the
  venue's have already diverged;
* resets the local book, which starts buffering again, and schedules a fresh REST
  snapshot;
* republishes the rebuilt book as a snapshot batch once the snapshot and the
  buffered notifications have been merged.

Seeding a book is retried, because an unsynchronized book buffers every further
notification and would otherwise stay silent for the life of the process:

| Condition                                                   | Handling                                                                                                                                                                                            |
|-------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Snapshot older than the buffered notifications              | Up to four attempts, 0.5 s apart, then a retry scheduled 5 s later; counted in `snapshot_retries`. Routine at subscription time — the venue's REST snapshot lags its own stream by roughly a second |
| REST failure (network, 5xx, exhausted rate-limit retries)   | Same retry path; counted in `snapshot_errors`. Never fatal to the subscription                                                                                                                      |
| Snapshot missing its `id` (for example stripped by a proxy) | Same retry path                                                                                                                                                                                     |
| Snapshot older than the live book                           | Discarded, counted in `snapshots_stale`; the book is already correct                                                                                                                                |

A book is never abandoned. If a subscription produces nothing at all, the counters
in `metrics()` distinguish "still trying to seed" from "seeded and quiet".

### Record flags

NautilusTrader groups a stream of `OrderBookDelta` records into batches using
`RecordFlag`, and a missing flag is a silent bug rather than a loud one: without
`F_LAST` the platform never releases the batch, and subscribers simply receive
nothing. The client sets both flags explicitly.

| Batch                                                                           | Flags                                                                      |
|---------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| Snapshot (initial seed, resync, `full: true`, or `request_order_book_snapshot`) | `F_SNAPSHOT` on every delta; the final delta additionally carries `F_LAST` |
| Incremental update                                                              | No flags on the deltas; the final delta carries `F_LAST`                   |

A snapshot batch is a `CLEAR` followed by one `ADD` per level: bids first, best
price first, then asks. Every delta in a batch carries the same `sequence` — the
venue update id the local book reflects, which is the last notification's `u` for
an incremental batch and the snapshot's `id`, or the last replayed notification's
`u`, for a snapshot batch. Every delta in a batch also carries the same
`ts_event`, taken from the venue's millisecond timestamp and falling back to the
local clock only when the venue sent none.

Within an incremental batch the action follows the size that is actually
published, not the raw venue value: a level whose size truncates to zero at the
instrument's precision becomes a `DELETE`, never an `UPDATE` with a zero size.
NautilusTrader rejects a zero-sized `UPDATE`, and the resulting exception would
abort the whole batch and leave the local book permanently diverged from the
venue. Levels dropped this way are counted in `book_levels_not_representable`.

### Intervals and depth levels

Book limits differ per product. The table below is
`BOOK_INTERVALS_MS`, `BOOK_LEVELS` and `BOOK_SNAPSHOT_LIMITS` in
`nautilus_gateio/websocket/public.py`, which are the single source of truth: the
data client imports them rather than restating the numbers.

| Product                        | `order_book_update` intervals | Stream depth levels                                    | Snapshot depths       |
|--------------------------------|-------------------------------|--------------------------------------------------------|-----------------------|
| Spot                           | 20 ms, 100 ms                 | 20 or 100, implied by the interval and not requestable | 5, 10, 20, 50, 100    |
| Perpetual (linear and inverse) | 20 ms, 100 ms                 | 20, 50, 100 — but 20 ms serves 20 levels only          | 1, 5, 10, 20, 50, 100 |
| Delivery futures               | 100 ms, 1000 ms               | 5, 10, 20, 50, 100                                     | 1, 5, 10, 20, 50, 100 |
| Options                        | 100 ms, 1000 ms               | 5, 10, 20, 50                                          | 1, 5, 10, 20, 50      |

Spot and the perpetuals do not accept a 1000 ms interval; it was withdrawn.
Options top out at 50 levels everywhere, including on the REST snapshot endpoint.

Configuration is validated against the union of these tables
(`order_book_update_interval_ms` must be 20, 100 or 1000;
`order_book_snapshot_limit` must be 1, 5, 10, 20, 50 or 100), because the
configuration is checked before any product is known. The per-product clamp
happens at subscription time and is logged as a warning:

* an interval the product does not accept becomes 100 ms where the product offers
  it, otherwise the smallest one it does offer — 100 ms rather than the
  numerically smallest value, because on spot and the perpetuals 20 ms would
  silently cut the stream down to 20 levels;
* a depth the product does not stream is rounded **up** to the next level it
  serves, or capped at the deepest one;
* on the perpetuals, asking for more than 20 levels at 20 ms moves the interval to
  100 ms rather than losing the depth;
* spot takes no depth parameter at all, so a requested depth that differs from the
  interval's implied depth is reported and ignored.

The depth that is clamped is the `depth` given to `subscribe_order_book_deltas`,
falling back to `order_book_snapshot_limit` when the subscription names none. The
REST snapshot is then requested at whatever depth the stream will actually serve,
so the two always cover the same price range; there is nothing for the operator to
keep in sync by hand. On spot this means `order_book_snapshot_limit` has no
effect: the interval decides.

Worked examples, all logged when they adjust anything:

| Configured interval / effective depth | Spot             | Perpetual   | Delivery     | Options     |
|---------------------------------------|------------------|-------------|--------------|-------------|
| 20 ms, 100                            | 20 ms, 20 levels | 100 ms, 100 | 100 ms, 100  | 100 ms, 50  |
| 100 ms, 100                           | 100 ms, 100      | 100 ms, 100 | 100 ms, 100  | 100 ms, 50  |
| 1000 ms, 100                          | 100 ms, 100      | 100 ms, 100 | 1000 ms, 100 | 1000 ms, 50 |
| 100 ms, 25                            | 100 ms, 100      | 100 ms, 50  | 100 ms, 50   | 100 ms, 50  |
| 100 ms, 10                            | 100 ms, 100      | 100 ms, 20  | 100 ms, 10   | 100 ms, 10  |

### What the platform builds on top

Subscribing with `managed=True` lets NautilusTrader maintain the `OrderBook`
from the deltas, and a non-zero snapshot interval on the subscription makes the
data engine publish periodic book snapshots from that managed book.

### Depth (`OrderBookDepth10`)

`subscribe_order_book_depth` reads Gate.io's *other* book channel: the periodic
`*.order_book` snapshot, which carries a complete book of the subscribed depth in
every message. It is a different venue channel from the incremental stream, so it
needs no REST seed, no sequence algorithm and no rebuild after a reconnect, and
holding both costs two venue subscriptions.

* Ten levels per side, which is what `OrderBookDepth10` holds and what Gate.io
  serves on all five products. Both sides are sorted by the adapter and padded to
  ten with the platform's `NULL_ORDER`, because the type requires the two sides
  to be the same length and Gate.io routinely sends asymmetric sides on thin
  contracts.
* `bid_counts` and `ask_counts` are zeros: Gate.io publishes aggregated price
  levels with no per-level order count, and the type documents zero as "not
  available".
* `flags` is `F_LAST` — one message is one complete book event. `F_SNAPSHOT`
  would be a misstatement; it means the message came from a replay or snapshot
  server, and this is a live push.
* `sequence` is the venue's own (`id` on the contract products, `lastUpdateId` on
  spot). A push that is not newer than the last one is dropped and counted in
  `order_book_depths_out_of_order`; the watermark is forgotten on a reconnect,
  because the venue restarts the sequence on a new connection.
* The push interval may be chosen per subscription through
  `params={"interval": ...}`. Only spot offers a choice — `"100ms"` or
  `"1000ms"` — and every contract product accepts `"0"` alone, which is push on
  change. An interval the product does not serve falls back to the product's
  first accepted value, with a log line naming what it accepts.
* A level whose size truncates to zero at the instrument's `size_precision` is
  skipped and its slot given to the next level that survives.

Both book subscriptions on one instrument give NautilusTrader's single cached
`OrderBook` two writers — `apply_depth` replaces the book, so a depth message
discards every delta level below the tenth. The adapter cannot fix that (the
engine owns the cached book), so it warns and leaves the choice to the caller.

## Bars

Subscribe with a standard bar type using `EXTERNAL` aggregation and the `LAST`
price type, for example `BTC_USDT.GATE_IO-1-MINUTE-LAST-EXTERNAL`. Anything else
is refused rather than approximated: Gate.io publishes only last-price
candlesticks, and `bar_type_to_interval` raises `ValueError` naming the supported
set for a specification with no Gate.io equivalent. On a subscription the client
catches that and logs it as an error; it never substitutes a different interval.

| Bar specification | Gate.io interval | Subscription | Request |
|-------------------|------------------|--------------|---------|
| `1-SECOND`        | `1s`             | -            | ✓       |
| `10-SECOND`       | `10s`            | ✓            | ✓       |
| `1-MINUTE`        | `1m`             | ✓            | ✓       |
| `5-MINUTE`        | `5m`             | ✓            | ✓       |
| `15-MINUTE`       | `15m`            | ✓            | ✓       |
| `30-MINUTE`       | `30m`            | ✓            | ✓       |
| `1-HOUR`          | `1h`             | ✓            | ✓       |
| `4-HOUR`          | `4h`             | ✓            | ✓       |
| `8-HOUR`          | `8h`             | ✓            | ✓       |
| `1-DAY`           | `1d`             | ✓            | ✓       |
| `7-DAY`           | `7d`             | ✓            | ✓       |

The candlestick WebSocket channels do not carry the one-second interval, so
`1-SECOND` is available through `request_bars` only; a subscription for it is
refused with an error naming the intervals the channel accepts.

**Only closed bars are published.** Spot and perpetual candlestick messages carry
a window-close flag (`w`), which publishes the bar immediately. Delivery and
options publish no such flag at all, and Gate.io documents that it may be missing
on the other products, so a bucket is also released when either of the following
happens:

* a candle for a newer bucket arrives; or
* the clock passes the bucket's close by a five-second grace period. A flush task
  checks once a second.

The clock path is what makes an illiquid contract usable. Waiting for the next
bucket's candle means waiting for the next trade, which on a thin delivery or
option contract can be many intervals away, or never. A bar released this way is
re-stamped so that `ts_init` reports when it was published rather than when the
last candle update arrived, while `ts_event` keeps describing the bucket.

Each bucket is published exactly once per bar type; Gate.io repeats the closing
update, and repeats are dropped. Either way the first bar of a subscription
arrives only after the interval it covers has ended — a one-minute subscription
produces nothing for up to a minute, plus the grace period on the products that
send no window-close flag.

`bars_timestamp_on_close` (default `True`) controls whether a bar is stamped at
the close of its interval, which is the NautilusTrader convention, or at the open,
which is what Gate.io's `t` field means.

Volume follows the venue's own accounting: on spot the base-currency amount
(field `a`, not the quote-currency turnover in `v`), and on every contract product
the contract count in `v`.

**A candle the platform rejects is dropped, not fatal.** NautilusTrader enforces
the OHLC invariants in the `Bar` constructor — `high` at least `open`, `low` and
`close`; `low` at most `open` and `close` — and illiquid delivery and option
candles do occasionally violate them. Such a row is dropped and counted in the
`candles_dropped` health counter; a historical request logs one warning naming
how many rows it lost and answers with the rest. It does not fail the request,
because a request that answers with nothing is indistinguishable from a venue
with no history, and the documented request-then-subscribe pattern subscribes
from inside that response's callback.

## Mark price, index price and funding rate

These are Nautilus `MarkPriceUpdate`, `IndexPriceUpdate` and `FundingRateUpdate`.
Gate.io has no dedicated channel for any of them: they are fields of the ticker
stream — `futures.tickers` on the three futures products, `options.contract_tickers`
on options — so one venue subscription serves any combination. The client
reference-counts the subscribers and unsubscribes from the venue channel only when
the last one goes away, so canceling mark prices does not silently stop funding
rates.

**Products.** Mark and index prices exist for every derivative: the three futures
products and options. Funding is perpetual-only — a delivery contract converges on
its settlement price and reports a basis in that field, and an option has no
funding leg at all, so a funding subscription for either is refused rather than
accepted and left silent. On spot all three are refused: the spot ticker is
24-hour trade statistics and carries none of them.

**Scale.** A mark or index price is published on the scale the venue published it
with, not rounded onto the instrument's order tick. Gate.io states two independent
minimum units, `order_price_round` and `mark_price_round`, and they differ on real
contracts: the BTC_USDT perpetual quotes orders in 0.1 and marks in 0.01, and the
BTC_USDT options quote orders in 1 and mark in 0.1, where quantizing would publish
a mark of 5797.7 as 5798. `mark_price_round` acts as a floor on the precision so
the scale does not wobble when a value happens to end in a zero. A field the venue
sends empty or unparseable produces no update at all rather than a zero.

**Next funding time.** `FundingRateUpdate.next_funding_ns` is derived, and this is
worth knowing before trading on it. The ticker carries no next-funding timestamp;
the only source is `funding_next_apply` on the contract definition, which the
instrument reload task refreshes every `update_instruments_interval_mins`
(60 minutes by default) while the ticker pushes about once a second. Published
verbatim it would name a settlement that has already happened for up to a whole
refresh interval. Instead the cached value is treated as what it is — an exact
point on the venue's funding grid — and rolled forward by whole `funding_interval`
steps to the first settlement after the update's own timestamp. Where the contract
publishes no `funding_interval` there is nothing to roll it forward with, and the
field is omitted rather than sent wrong. `interval` is attached whenever the
contract states one.

`request_funding_rates` answers from `GET /futures/{settle}/funding_rate`, whose
records carry only an application timestamp and a rate. Those updates therefore
carry `interval` — a property of the contract — but no `next_funding_ns`, since
the endpoint publishes nothing about the next application.

**Not published from these channels.** `options.contract_tickers` also carries
`mark_iv`, `bid_iv`, `ask_iv` and the full greek set, and Gate.io serves the same
fields from `GET /options/tickers`. They are reachable through the transport and
the REST namespaces but are not yet mapped onto the platform's `OptionGreeks`
type, so `subscribe_option_greeks` is unimplemented — see the note under
[Subscriptions](#subscriptions) for what an unimplemented subscribe hook does.

## The venue ticker row (`GateioTicker`)

The three types above are what NautilusTrader models. The rest of the ticker row —
24-hour statistics, the delivery basis, the implied volatilities and greeks, the
*indicative* next funding rate, the open interest — has no platform type, and
this client publishes it as an adapter-specific data type, which is what the
in-tree adapters do with the same problem (`BinanceTicker`, `BetfairTicker`).

```python
from nautilus_trader.model.data import DataType

from nautilus_gateio import GATEIO_CLIENT_ID, GateioTicker

self.subscribe_data(
    DataType(GateioTicker, metadata={"instrument_id": instrument_id}),
    client_id=GATEIO_CLIENT_ID,
)
```

Use the metadata form shown above. The platform addresses a custom data type
that carries metadata by that metadata, so a subscription taken out with a bare
`DataType(GateioTicker)` and a separate `instrument_id` argument listens on a
different topic from the one the rows are published on.

* Every field is the venue's own string, kept under the venue's own name. One row
  mixes an order-tick price, a contract count, a base-currency turnover and a
  dimensionless implied volatility, and quantizing them all onto one precision
  would change values.
* Mark price, index price and funding rate are deliberately **absent** from the
  type: they are published from the same message as the platform's own types, and
  carrying them twice would give a strategy two sources for one number.
* A field the venue did not send for this product is the empty string. The
  platform's Arrow schema builder for custom data accepts no optional field, so
  "absent" has to be a value of the field's own type.
* Ticker subscribers share the one venue channel with mark, index and funding
  subscribers, and the reference count means canceling one does not stop the
  others.
* The type registers itself with the platform's msgpack and Arrow serializers on
  import, so it can be persisted to a catalog or sent over an external message
  bus. If a node uses an external message bus and should not publish it, name it
  in `MessageBusConfig.types_filter`.

## Instrument status and instrument close

Gate.io publishes no instrument-status channel on any product, so
`subscribe_instrument_status` is served by polling the same instrument listings
the reload task already reads (`update_instruments_interval_mins`, 60 minutes by
default). The in-tree adapters for venues without such a channel (Bybit, Kraken,
dYdX) do the same.

* Subscribing reports the current status at once, then only on change. Without
  the immediate reading a strategy subscribing to a healthy instrument would
  learn nothing until it stopped being healthy.
* `InstrumentStatus.reason` names the venue field that decided the action,
  verbatim: `in_delisting=true`, `status=delisted`, `trade_status=untradable`,
  `expire_time=... elapsed`, `is_active=false`.
* Only three actions are ever emitted: `TRADING`, `PRE_OPEN` (a spot pair whose
  payload states a `buy_start` or `sell_start` still ahead) and
  `NOT_AVAILABLE_FOR_TRADING`. `HALT`, `PAUSE`, `CROSS`, `PRE_CLOSE` and the rest
  would be inventions of venue intent, and Gate.io's listings do not distinguish
  them.
* `is_quoting` and `is_short_sell_restricted` stay `None`: the fields are
  tri-state precisely so an adapter can decline to guess, and Gate.io publishes
  neither.
* An instrument that disappears from a listing that was *read* is reported
  `NOT_AVAILABLE_FOR_TRADING`; one that disappears because the request failed is
  not, since a single failed listing would otherwise look like a mass delisting.
* **The honest limit:** a halt shorter than the poll interval is invisible. There
  is no channel to see it on.

`subscribe_instrument_close` watches for the settlement of a dated contract.
`InstrumentClose.close_price` is not optional and has no null form, so it is
served only where a real settlement price exists: delivery futures and options.
On spot, perpetual and inverse perpetual markets the subscription is refused with
that reason — they trade continuously and never settle.

* `close_type` is always `CONTRACT_EXPIRED`. `END_OF_SESSION` is never emitted:
  Gate.io has no sessions, so naming one would be a fabrication.
* A delivery close is published only when the contract's `settle_price` and the
  ticker's agree. Two sources that disagree publish nothing.
* An option's close is its own value — the settlement row's `profit` divided by
  the contract multiplier — cross-checked against the intrinsic value implied by
  the row's settle price and strike. The row's `settle_price` is the *underlying's*
  averaged price, and publishing that as the option's close would be wrong by
  orders of magnitude.
* The watcher polls for a bounded time after expiry and then gives up rather than
  polling forever for a settlement the venue never publishes.

## Reconnection and resubscription

Gate.io offers no stream resumption. The transport reconnects with capped
exponential backoff and jitter, replays every subscription it still holds
(Gate.io treats repeated subscriptions as additive, so replaying is safe), sends
an application-level ping when the link goes idle, and recycles a connection that
has delivered nothing for `recv_timeout_secs`. A subscription that failed because
the socket was down or because the venue did not acknowledge it in time is kept
for replay; only an outright venue rejection removes it, since replaying a
rejection only earns another one.

Once the subscriptions have been replayed, the data client is called back and
rebuilds every local order book for that product from a fresh REST snapshot. A
local book is stale by definition after a disconnect, so nothing is published from
it until the new snapshot has landed; the deltas that follow are a `F_SNAPSHOT`
batch, exactly as at subscription time.

## Deduplication

What is deduplicated, and what is not, is worth stating precisely:

* **Bars** — one publication per bucket per bar type. The repeated closing update
  Gate.io sends is dropped.
* **Historical bars** — pages are keyed by bucket open time, so an overlap between
  consecutive pages collapses to one bar.
* **Book updates** — a notification whose range is entirely older than the local
  update id is dropped and counted; the single notification that straddles the
  snapshot is applied once, which is safe because level sizes are absolute. A
  level whose size has not changed produces no delta.
* **Book subscriptions** — a second subscription for an instrument that already
  has a local book is refused with a warning, and only one seeding or resync task
  can be in flight per instrument.
* **Trades and quotes are not deduplicated.** They are published as received. The
  venue trade id is preserved verbatim precisely so that a consumer that needs
  deduplication can do it.

## Health counters

`GateioDataClient.metrics()` returns cumulative counters. Per product:
`reconnects`, `gaps`, `resyncs`, `snapshot_retries`, `snapshot_errors` and
`messages`. Alongside them: `published` counted per data type (including the
skip counters named above), `candles_dropped` per bar type, `book_gaps` per
instrument, how many local books exist and how many of them are currently
synchronized, and the underlying connection statistics for each socket.

The distinction that matters when reading them: `gaps` counts sequence breaks in a
live stream, each of which forces a resync and means data was genuinely lost.
`snapshot_retries` counts the unrelated case of a REST snapshot arriving older
than the buffered notifications, which is routine at subscription time and loses
nothing. `book_gaps` is per book and includes both.

## Using the WebSocket layer directly

The transport is usable without a Nautilus node — useful for checking what the
venue actually sends before deciding whether the adapter's interpretation of it is
right:

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

The book assembly is equally standalone, and takes plain payload dictionaries:

```python
from nautilus_gateio.books import GateioOrderBook, OrderBookSequenceError, SnapshotStaleError
```

The authoritative limit tables can be read the same way, so a tool does not have
to hard-code them:

```python
from nautilus_gateio.websocket.public import (
    BOOK_INTERVALS_MS,
    BOOK_LEVELS,
    BOOK_SNAPSHOT_LIMITS,
)
```

See [`examples/02_public_websocket.py`](../examples/02_public_websocket.py) for a
complete script, [configuration.md](configuration.md) for the full field
reference, and [troubleshooting.md](troubleshooting.md) for the failure modes
these counters point at.
