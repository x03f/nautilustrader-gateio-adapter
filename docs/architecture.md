# Architecture

How the adapter is put together: the package layout, the runtime dataflows, and
the reasoning behind the choices that are not self-evident from the code.

Two facts frame everything below.

**This is an external, community-maintained package.** It is not part of the
NautilusTrader distribution and is not endorsed by Nautech Systems or by
Gate.io. It is written in pure Python and developed against `nautilus_trader`
1.230.0. NautilusTrader's stated preference for adapters inside its own tree is
a Rust core with a thin PyO3 binding, and this package deliberately does not
follow that.
[The deliberate Python-only architecture](#the-deliberate-python-only-architecture)
explains why, and — more importantly — what that decision does *not* excuse.

**The release is alpha.** The behavior described here is implemented and
covered by the unit suite against recorded and simulated venue payloads.
Live-venue validation covers the market-data paths, spot execution, a series of
orders on one USDT perpetual including a position read back from the venue by a
node that did not open it, and three orders on one option contract; no inverse
or delivery order and no margin spot ledger has been exercised against Gate.io.
See [validation.md](validation.md).
Where this page classifies a capability it uses exactly one of: *implemented and
mock-tested*, *implemented, mainnet validation pending*, *experimental*,
*unsupported*, *not applicable*. Nothing here is described as stable or
production ready, because nothing has earned either word yet.

## Package layout

The package is sub-packaged by concern. The top-level `__init__` re-exports the
public API, so `from nautilus_gateio import GateioDataClient` works regardless of
where a symbol physically lives.

```text
nautilus_gateio/
  __init__.py            public API re-exports, __version__
  common/
    constants.py         venue id, REST/WS endpoints, interval maps, limits
    enums.py             GateioProductType, account modes, TIF, status mapping
    errors.py            typed error hierarchy and retry classification
    credentials.py       environment resolution and masking
    signing.py           HMAC-SHA512 request and WebSocket signing, client ids
    symbols.py           instrument id <-> Gate.io symbol (the only such place)
    parsing.py           tolerant payload field conversion
    status.py            instrument listing payload -> MarketStatusAction, and the diff
  http/
    client.py            shared transport: signing, pacing, retries, errors
    spot.py margin.py futures.py options.py wallet.py   typed namespaces
  websocket/
    client.py            one resilient connection to one endpoint
    public.py            public channels for one product
    private.py           authenticated channels for one product
  books.py               local L2 book assembly and sequence validation
  types.py               venue-native data types (GateioTicker)
  instruments.py         Gate.io payload -> NautilusTrader instrument
  providers.py           GateioInstrumentProvider (multi-product, filtered)
  config.py              GateioDataClientConfig, GateioExecClientConfig
  data.py                GateioDataClient
  execution.py           GateioExecutionClient
  factories.py           TradingNode factories, shared-transport caching
```

The dependency direction is one-way, from the bottom of this table upwards:

| Layer                                     | Imports                                                               | NautilusTrader types in its own API                                   |
|-------------------------------------------|-----------------------------------------------------------------------|-----------------------------------------------------------------------|
| `books.py`                                | standard library only                                                 | none                                                                  |
| `common/*`                                | standard library only                                                 | identifiers and enums only (`constants.py`, `enums.py`, `symbols.py`) |
| `http/*`                                  | `common/*`, `httpx`                                                   | none — namespaces return decoded JSON unchanged                       |
| `websocket/*`                             | `common/*`, `websockets`, the platform's logger and task cancellation | none — the transport hands raw envelopes to a callback                |
| `instruments.py`, `providers.py`          | `http/*`, `common/*`                                                  | instrument and provider types                                         |
| `data.py`, `execution.py`, `factories.py` | everything below                                                      | the full live-client surface                                          |

The consequence worth knowing is that the REST and WebSocket layers can be used
directly, without a trading node: nothing in their signatures is a NautilusTrader
object. (The distribution still depends on `nautilus_trader`, because
`common/constants.py` builds a `Venue` and `common/enums.py` maps onto the
framework's order enums.)

`books.py` has no framework dependency at all: it deals in `Decimal` prices and
sizes, so the snapshot-plus-increment synchronization algorithm can be unit
tested without a trading environment. Turning its output into venue data types is
the data client's job.

## The two clients and their factories

| Component                     | Base class              | Role                                                                                       |
|-------------------------------|-------------------------|--------------------------------------------------------------------------------------------|
| `GateioDataClient`            | `LiveMarketDataClient`  | subscriptions and historical requests for every configured product                         |
| `GateioExecutionClient`       | `LiveExecutionClient`   | order submission, modification, cancellation, account state and all four report generators |
| `GateioLiveDataClientFactory` | `LiveDataClientFactory` | builds the data client from `GateioDataClientConfig`                                       |
| `GateioLiveExecClientFactory` | `LiveExecClientFactory` | builds the execution client from `GateioExecClientConfig`                                  |

Both factories are registered with a `TradingNode` under the venue's client id:

```python
from nautilus_gateio import (
    GATEIO,
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
)

node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
```

A node config can carry both entries declaratively instead, with one caveat about
the execution factory; see
[configuration.md](configuration.md#registering-from-a-declarative-config).

There is one client of each kind, not one per product. A single data client
multiplexes every product named in `products`, and a single execution client
trades all of them through one Nautilus account. Gate.io keeps a **separate
wallet per product** and funds do not move between them implicitly, so the
execution client aggregates those wallets into one account and says so in a
start-up warning rather than letting the segregation surprise anyone at the
first order.

The factories perform no I/O. Construction opens no socket and issues no
request; the venue is first contacted in `_connect()`. This is asserted by the
factory tests, which run with the network blocked.

## Shared, reference-counted REST transport

`factories.py` caches the HTTP transport on `(api_key, api_secret, base_url,
timeout_secs, max_retries)` and the instrument provider on `(http_client,
products, options_underlyings, instrument_provider_config)`. A data client and
an execution client configured alike therefore share one connection pool and one
instrument load, which is the pattern the adapters bundled with NautilusTrader
1.230.0 use.

The cache is `functools.lru_cache(1)` — again matching the bundled adapters — so
it holds exactly one entry. Two differently configured transports do not
coexist: asking for a second configuration evicts the first, and asking for the
first again builds a new instance. Within a single node this is invisible;
a process that builds nodes for two environments will simply not get sharing
between them.

Shutdown is reference counted. Each owner calls `acquire()` when it takes the
transport and `close()` when it releases it in `_disconnect`, and the underlying
`httpx` pool is closed exactly once, by the last release. `close()` is
idempotent and safe to call from a component that never acquired, so shutdown
paths need no coordination. A cached entry that has been closed is discarded and
rebuilt on the next request, because handing a closed client to a second node in
the same process would fail on its first call.

## Typed REST namespaces over one transport

The REST side is deliberately the opposite shape to the WebSocket side: one
transport, many typed namespaces.

`GateioHttpClient` owns everything that is true of every request — HMAC-SHA512
signing, client-side pacing with exponential backoff on HTTP 429, translation of
`{"label", "message"}` error bodies into the typed error hierarchy, the venue
clock offset, and the retry policy. The namespace classes
(`GateioSpotHttpAPI`, `GateioMarginHttpAPI`, `GateioFuturesHttpAPI`,
`GateioOptionsHttpAPI`, `GateioWalletHttpAPI`) each take that client and return
decoded payloads unchanged.

The clock offset is opt-in and worth knowing about, because two features hang off
it. Signatures embed a timestamp, so a drifting local clock produces
`INVALID_SIGNATURE`; and Gate.io's optional `x-gate-exptime` submission deadline
bounds how late a request delayed in flight may still be accepted. Calling
`sync_time()` measures the offset against the venue and enables both. Neither
client calls it during connect, so an unsynchronized transport signs with the
local clock and sends no deadline header — the deadline is deliberately withheld
rather than computed from a clock that has not been checked, since an
unsynchronized clock would expire valid requests.

Perpetual and delivery futures share one namespace class, because Gate.io serves
them from paths that differ only in two segments. The `/futures/{settle}` versus
`/delivery/{settle}` split is expressed once, in the constructor:

```python
perp = GateioFuturesHttpAPI(client, settle="usdt")
inverse = GateioFuturesHttpAPI(client, settle="btc")
dated = GateioFuturesHttpAPI(client, settle="usdt", delivery=True)
```

The handful of endpoints that genuinely exist only for perpetuals — funding
rates, order amendment, dual position mode — raise rather than quietly routing
somewhere else.

The retry policy is the part of this layer with money attached, so it is stated
plainly. Gate.io offers no request-level idempotency token, and a transparent
replay of a mutating request can execute it twice. The client therefore replays
only where a replay is provably harmless: `GET`, `HEAD`, `OPTIONS` and `DELETE`
always; `POST`, `PUT` and `PATCH` only when the venue has stated the request was
rejected before it was processed (HTTP 429, or a `TOO_MANY_REQUESTS` /
`REQUEST_EXPIRED` label), or when the transport failed before a byte left the
process. Any other failure of a mutating request raises
`GateioRequestAmbiguousError`, whose contract is "this may or may not have been
applied — reconcile before resubmitting" rather than a silent retry.

Replaying does not make an outcome known, so the same error is raised for a
request that reached the venue and was never answered however often it was
replayed; `NETWORK_ERROR` is reserved for the case where no byte of any attempt
left the process. A cancel is why that distinction is load-bearing: `DELETE` *is*
replayed, and reporting a definitive failure for an order the venue had already
canceled is what the execution client would then tell the strategy.

## One WebSocket transport per product

Gate.io serves each product family from its own WebSocket host, and perpetual
and delivery futures share the `futures.*` channel namespace while living on
different hosts. A message is therefore only interpretable in combination with
the endpoint it arrived on, which is why `GateioProductType` is part of the
identity of a `GateioWebSocketClient` rather than a parameter of each call. The
product also selects the application-level ping channel and whether the
fractional-size handshake header applies.

| Product                  | Channel namespace | Default endpoint                            |
|--------------------------|-------------------|---------------------------------------------|
| Spot                     | `spot.*`          | `wss://api.gateio.ws/ws/v4/`                |
| Perpetual (linear, USDT) | `futures.*`       | `wss://fx-ws.gateio.ws/v4/ws/usdt`          |
| Perpetual (inverse, BTC) | `futures.*`       | `wss://fx-ws.gateio.ws/v4/ws/btc`           |
| Delivery futures         | `futures.*`       | `wss://fx-ws.gateio.ws/v4/ws/delivery/usdt` |
| Options                  | `options.*`       | `wss://op-ws.gateio.live/v4/ws`             |

Gate.io publishes testnet WebSocket endpoints for spot and USDT perpetuals only,
which is why configuring any other product together with
`environment="testnet"` is rejected before a connection is attempted rather than
failing later at the socket.

`GateioWebSocketClient` is transport only: it connects, signs private
subscription requests, keeps the connection alive, reconnects with capped
exponential backoff, replays the subscription set and hands decoded envelopes to
a callback. It contains no trading logic. `GateioPublicWebSocket` and
`GateioPrivateWebSocket` wrap one such connection each and give the channels
typed methods.

The data client opens one public connection per configured product; the
execution client opens one private connection per configured product. Both pass
an `on_reconnect` callback, because Gate.io has no server-side session resume:

* the data client rebuilds every local order book from a fresh REST snapshot,
  since a book is stale by definition after a gap;
* the execution client refreshes account state and re-queries orders and fills
  over the outage window.

Subscription replay itself belongs to the transport, and is safe because
Gate.io treats repeated subscriptions as additive. A subscription that failed
for a transport reason (`WS_NOT_CONNECTED` during a reconnect window,
`WS_ACK_TIMEOUT`) is kept and replayed; only an outright venue rejection removes
it.

### Logging and background tasks in the transport

The transport logs through NautilusTrader's logging subsystem
(`nautilus_trader.common.component.Logger`), under the component name
`GateioWebSocketClient`. Reconnects, subscription replay failures, malformed
frames and the venue's service notifications therefore land in the Nautilus log
file and obey `log_level`, `log_level_file` and `log_component_levels` like
every other component. Using a standard-library logger here meant none of that
applied, and the package contradicted itself, since `instruments.py` already
logged through the platform.

Every background task the transport starts — the receive loop, the heartbeat,
the subscription replay, the proactive close triggered by the venue's `upgrade`
notification and the task wrapping a coroutine returned by the message handler —
is registered in one collection, and `disconnect()` hands that collection to the
platform's `cancel_tasks_with_timeout`. That is what takes a strong reference
snapshot before canceling, so a task held only by the event loop cannot be
collected mid-cancellation, and what reports the task names if they do not
settle in time. A task the transport does not register is a task shutdown cannot
account for.

This does not require a running trading node: a `Logger` built before the
logging subsystem is initialized simply discards its messages, so the REST and
WebSocket layers remain usable standalone as described above.

## Instrument provider

`GateioInstrumentProvider` subclasses NautilusTrader's `InstrumentProvider` and
implements `load_all_async`, `load_ids_async` and `load_async`. It loads each
configured product independently through the typed namespaces, so the venue's
path layout is expressed in exactly one place.

Two behaviors in it are deliberate and worth knowing before an account starts:

**Per-product degradation.** Gate.io creates the futures, delivery and options
wallets on first use and answers `USER_NOT_FOUND` until then; a key may also lack
permission for a product, or the account may not be in the mode an endpoint
needs. These are configuration states, not failures. Each is logged and that one
product is skipped, so an account trading only spot still starts cleanly with
several products configured.

**Rejection rather than clamping.** `instruments.py` is a set of pure payload
transformations, and a payload that cannot be represented faithfully yields
`None` plus a warning rather than raising — one bad entry never aborts a batch
load. The important case is price scale: a few Gate.io spot pairs quote more
decimals than a standard NautilusTrader build can represent, and quantizing such
a price yields `0.000000000`, which `Price` accepts in silence. Publishing the
instrument anyway would mean publishing zeroes as if they were venue prices, so
the instrument is not published at all.

The provider additionally withholds instruments that cannot be traded normally:
untradable or one-sided spot pairs, delisting or inactive contracts, expired
delivery contracts, and options outside an active expiration. When credentials
are present it reads the account's fee tier once, from `GET /wallet/fee` with the
deprecated `GET /spot/fee` as a fallback, so spot instruments carry real fees
rather than the deprecated per-pair percentage.

## Symbology

All conversion between NautilusTrader instrument ids and Gate.io symbols lives in
`nautilus_gateio/common/symbols.py`. No other module builds or takes apart an
instrument id. Three decisions define it; [symbology.md](symbology.md) is the
full reference.

**The venue string is `GATE_IO`, with an underscore.** NautilusTrader already
identifies this exchange as `GATE_IO` in its own tooling — the Tardis
integration's venue mapping matches on exactly that string — so instruments
produced here interoperate with data loaded through other NautilusTrader
components. Version 0.1.0 used `GATEIO`; the change is breaking and is covered
by the [migration guide](migration-0.1-to-0.2.md).

**Minimum normalization.** A Gate.io symbol is used verbatim unless the venue
genuinely reuses it, and the adapter invents no vocabulary of its own. There is
no `-SPOT`, `-FUT`, `-OPT` or `-INVERSE` suffix, because none is needed to tell
those apart.

**`-PERP`, on perpetuals only, and only because they collide.** A survey of the
venue's listings recorded 527 USDT perpetual contracts whose exact symbol is
also a spot pair: `BTC_USDT` is both a spot market and a perpetual. Two distinct
instruments cannot share one instrument id, so the perpetual has to carry
something the spot pair does not, and `-PERP` is the established NautilusTrader
convention for precisely this case. Delivery contracts (`BTC_USDT_20260807`) and
options (`BTC_USDT-20260729-70000-C`) carry their expiry inside the symbol and
collide with nothing — no spot pair contains a dash or a second underscore — so
they take no suffix.

**`raw_symbol` is always the venue's own string.** Whatever the instrument id
looks like, `raw_symbol` on the instrument is the exact symbol Gate.io uses, so
a round trip back to the API never has to reverse a transformation. The suffix
exists in the instrument id and nowhere else.

| Product             | Instrument id                       | `raw_symbol`                |
|---------------------|-------------------------------------|-----------------------------|
| Spot                | `BTC_USDT.GATE_IO`                  | `BTC_USDT`                  |
| Perpetual (linear)  | `BTC_USDT-PERP.GATE_IO`             | `BTC_USDT`                  |
| Perpetual (inverse) | `BTC_USD-PERP.GATE_IO`              | `BTC_USD`                   |
| Delivery future     | `BTC_USDT_20260807.GATE_IO`         | `BTC_USDT_20260807`         |
| Option              | `BTC_USDT-20260729-70000-C.GATE_IO` | `BTC_USDT-20260729-70000-C` |

## Dataflow: market data

```text
Gate.io WS (per product endpoint)          Gate.io REST
        |                                         |
        v                                         v
GateioPublicWebSocket                    GateioHttpClient + namespaces
        |  raw envelope                           |  snapshots, history
        v                                         v
              GateioDataClient._handle_ws_message
                        |
                        v
   TradeTick            <- *.trades
   QuoteTick            <- *.book_ticker (the venue's own best bid/offer)
   OrderBookDeltas      <- REST snapshot + *.order_book_update, assembled by
                           GateioOrderBook: sequence-validated, resync on gap
   Bar                  <- *.candlesticks, closed intervals only
   MarkPriceUpdate      <- futures.tickers / options.contract_tickers
   IndexPriceUpdate     <- futures.tickers / options.contract_tickers
   FundingRateUpdate    <- futures.tickers, and REST funding_rate on request
   OrderBookDepth10     <- *.order_book, the venue's periodic snapshot channel
   InstrumentStatus     <- polled instrument listings; Gate.io has no status channel
   InstrumentClose      <- REST settlement after expiry, delivery and options only
   GateioTicker         <- *.tickers, published as CustomData
                        |
                        v
                 Nautilus DataEngine
```

Nothing is published that the venue did not send: quotes come from the real
`book_ticker` best bid/offer stream, not from a synthesized spread. Sizes that
truncate to zero at the instrument's precision are dropped rather than published
as zeros, and an empty book side is absent rather than zero.

Gate.io publishes no instrument-definition channel, so instrument updates come
from a polling task on `update_instruments_interval_mins` instead of a stream —
the same approach other NautilusTrader adapters take for venues without one.

## Dataflow: execution

```text
Strategy -> SubmitOrder / ModifyOrder / CancelOrder
                        |
                        v
              GateioExecutionClient
                translate (reject if not expressible)
                        |
                        v
              product REST namespace  ---------------> Gate.io
                        ^                                 |
                        |                                 v
              REST reconciliation                private WebSocket
              (reports, account poll)         orders / usertrades /
                        |                     balances / positions
                        +----------------+----------------+
                                         v
                          OrderAccepted / OrderFilled / OrderCanceled
                                 AccountState, reports
                                         |
                                         v
                              Nautilus ExecutionEngine
```

The private WebSocket is the primary event source and REST is the reconciliation
source. Position updates arrive on both, and only the REST view is published as
reports: one fill must not produce two competing views of the same position. All
four report generators (`generate_order_status_report`,
`generate_order_status_reports`, `generate_fill_reports`,
`generate_position_status_reports`) are implemented against REST, so a restart
with resting orders and open positions is a supported path — implemented and
mock-tested, mainnet validation pending.

## Design decisions

**Contracts, not coins.** For every derivative a `Quantity` is a number of
contracts, matching the venue's `size` field, with the face value carried in
`multiplier`. `size_precision` is therefore `0` on every contract instrument:
fractional contracts do not exist on this venue. This is how Gate.io computes
notional and how `Instrument.notional_value()` computes it.

**Reject rather than substitute.** An order Gate.io cannot express without
changing its meaning raises `UnsupportedOrderError` and becomes an explicit
denial or rejection carrying the reason, rather than a different order sent
quietly. Reduce-only on spot is the clearest case: it is a derivatives concept,
Gate.io's spot endpoint has no such flag, and dropping the flag would change what
the strategy asked for. Which time-in-force values, flags and order types each
product accepts is set out per case in [execution.md](execution.md).

The one deliberate substitution is documented and logged on every use: a
base-denominated spot market buy, which Gate.io's spot API cannot express
directly, is sent as an aggressive limit bounded by the pair's own published
slippage cap. It substitutes the price and only the price — the order's own time
in force rides along, so a fill-or-kill buy stays fill-or-kill.

**Degrade rather than fail.** "Wallet not created yet", "account not in the
required mode" and "key lacks permission" are configuration states, not
failures. They skip one product with an actionable message instead of aborting
the load.

**Refuse rather than reconfigure the account.** A perpetual futures account in
hedge (dual) position mode holds a separate long and short leg for one contract,
which cannot be reconciled against Nautilus netting. The execution client checks
the position mode at connect and refuses to start, naming the venue-side setting
to change. It never changes that setting on the operator's behalf.

**No local kill switch.** A boolean inside the process is not a security
boundary. Control over what a key may do belongs on the key itself
(permissions, IP allowlist) and in the explicit choice of `environment` — which
defaults to mainnet, so that an execution client can never be pointed at a
different exchange environment than the operator believes.

## The deliberate Python-only architecture

NautilusTrader's developer guide describes the preferred shape of an adapter
that lives inside its repository: a Rust core doing the venue work, with a thin
PyO3 layer exposing it to Python. This adapter is pure Python, top to bottom.
That is a deviation, it is deliberate, and it is stated here rather than left
for a reader to discover.

**Why.** This is an externally distributed package, installed from PyPI
alongside `nautilus_trader` rather than merged into it. A Rust core would mean
publishing compiled wheels per platform and Python version, a build toolchain
for contributors, and an FFI boundary to keep correct — cost that buys an
external package very little, since the adapter's work is JSON parsing and
socket handling bounded by network latency, not by the interpreter. Pure Python
also keeps the whole implementation readable and modifiable by the people most
likely to need to: users debugging their own venue behavior.

**What this does not excuse.** The distinction that matters is between
*functional correctness* requirements and *repository convention*.

Requirements governing how an adapter must behave toward the platform apply in
full, wherever the code lives: event ordering and lifecycle, order-book record
flags, timestamp units, identity handling, precision, and the content of
reconciliation reports. A strategy cannot tell which language its adapter is
written in, and none of those requirements becomes optional because this package
is external. Where the adapter falls short of one, that is a defect, not a
consequence of this decision.

In-tree conventions are a different matter: repository layout, crate structure,
the upstream build and release process, the in-tree test directory split. Those
govern contributions to the NautilusTrader repository and do not bind a
separately distributed package. They are not treated as release blockers here.

Every Rust and PyO3 requirement in the upstream guide — FFI memory contracts,
`PyCapsule` destructors, `unsafe` policy, crate lint configuration — is
inapplicable rather than unmet, because there is no Rust in this package to hold
to them.

**What this is not.** This page does not claim architectural conformance with an
official in-tree adapter, and this package is not one. A future Rust/PyO3
migration is a possible separate project. It is **not promised**, has no
timeline, and nothing here should be read as a commitment to it or as a
recommendation of it; stylistic parity with in-tree Rust adapters would not on
its own be a reason to introduce Rust.

## Testing seams

The layering exists so that the parts with hard-to-reproduce failure modes can
be tested without a venue.

* `books.py` — pure `Decimal` in, changes out. No framework, no I/O, so
  out-of-order and gapped update sequences are ordinary unit tests.
* `instruments.py` — pure payload transformations; an unrepresentable payload
  yields `None` plus a warning, so both the accept and the reject path are
  assertable from a fixture.
* `http/*` — namespaces take a client, and the client's `request()` is the
  single network seam to stub.
* `websocket/*` — the transport takes a handler callable; tests drive it with
  recorded envelopes rather than a live socket.
* `common/*` — pure functions throughout.
* `factories.py` — construction is I/O-free, so the factory tests run with the
  network blocked and fail loudly if that ever stops being true.

See [testing.md](testing.md) for what the suite covers, and
[validation.md](validation.md) for what it does not: no amount of unit testing
establishes that the venue agrees with the adapter's model of it.
