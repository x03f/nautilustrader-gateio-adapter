# Architecture

How the adapter is put together: the package layout, the runtime dataflows, and
the reasoning behind the less obvious choices.

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
  http/
    client.py            shared transport: signing, pacing, retries, errors
    spot.py margin.py futures.py options.py wallet.py   typed namespaces
  websocket/
    client.py            one resilient connection to one endpoint
    public.py            public channels for one product
    private.py           authenticated channels for one product
  books.py               local L2 book assembly and sequence validation
  instruments.py         Gate.io payload -> NautilusTrader instrument
  providers.py           GateioInstrumentProvider (multi-product, filtered)
  config.py              GateioDataClientConfig, GateioExecClientConfig
  data.py                GateioDataClient
  execution.py           GateioExecutionClient
  factories.py           TradingNode factories, shared-transport caching
```

| Layer | Depends on | Framework coupling |
|---|---|---|
| `common/*`, `books.py` | nothing but the standard library (and Nautilus identifier types in `symbols.py`) | minimal |
| `http/*`, `websocket/*` | `common/*` | none |
| `instruments.py`, `providers.py` | `http/*`, `common/*` | Nautilus instruments |
| `data.py`, `execution.py`, `factories.py` | everything below | full Nautilus live clients |

`books.py` deliberately has no framework dependency at all: it deals in
`Decimal` prices and sizes, so the synchronisation algorithm can be unit tested
without a trading environment. Turning its output into venue data types is the
data client's job.

## One transport per product

Gate.io serves each product family from its own WebSocket host, and perpetual
and delivery futures share the `futures.*` channel namespace while living on
different hosts. A message is therefore only interpretable in combination with
the endpoint it arrived on, which is why `GateioProductType` is part of the
identity of a `GateioPublicWebSocket` / `GateioPrivateWebSocket` rather than a
parameter of each call.

The REST side is the opposite: one transport, many typed namespaces.
`GateioHttpClient` owns signing, pacing, retries and error translation, and each
namespace class is a thin wrapper that returns decoded payloads unchanged. The
`/futures/{settle}` versus `/delivery/{settle}` split is expressed once, in
`GateioFuturesHttpAPI`'s constructor arguments.

## Shared, reference-counted REST transport

`factories.py` caches the HTTP client and the instrument provider on
`(credentials, environment, products)`, so a data client and an execution client
with the same configuration share one connection pool and one instrument load —
the same approach the official adapters take. Shutdown is reference counted:
each owner calls `acquire()` when it takes the transport and `close()` when it
releases it, and the last owner closes it exactly once. `close()` is idempotent,
so shutdown paths need no coordination.

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
      +-----------------+------------------+--------------------+
      v                 v                  v                    v
  TradeTick         QuoteTick        OrderBookDeltas           Bar
  (trades)        (book_ticker)   (GateioOrderBook, seq-       (closed
                                   validated, resync on gap)   candles)
                        |
                        v
                 Nautilus DataEngine
```

Nothing is published that the venue did not send. Sizes that truncate to zero at
the instrument's precision are dropped rather than published as zeros, and an
empty book side is absent rather than zero.

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

The private WebSocket is the primary event source and REST is the
reconciliation source. Position updates arrive on both, and only the REST view
is published as reports — one fill must not produce two competing views of the
same position.

## Design decisions

**Venue string `GATE_IO`.** NautilusTrader's own tooling already identifies this
exchange as `GATE_IO`, so instruments produced here interoperate with data
loaded through other NautilusTrader components. See
[symbology.md](symbology.md).

**Minimum normalization.** A Gate.io symbol is used verbatim unless the venue
genuinely reuses it. Only perpetuals do (527 measured collisions with spot
pairs), so only perpetuals carry a suffix.

**Contracts, not coins.** For every derivative a `Quantity` is a number of
contracts, matching the venue's `size` field, with the face value in
`multiplier`. This is how Gate.io computes notional and how
`Instrument.notional_value()` computes it.

**Reject rather than clamp.** An instrument whose price scale the running
NautilusTrader build cannot represent is not published at all. Quantising such a
price yields `0.000000000`, which `Price` accepts silently; publishing it would
mean publishing zeroes as if they were venue prices.

**Reject rather than substitute.** Any order Gate.io cannot express without
changing its meaning is denied or rejected with a reason, never quietly turned
into a different order. The one documented exception — a base-denominated spot
market buy sent as an IOC limit at the pair's own slippage cap — is logged
explicitly on every use.

**Degrade rather than fail.** "Wallet not created yet", "account not in the
required mode" and "key lacks permission" are configuration states, not
failures. They become `WalletNotProvisionedError` with an actionable message and
skip one product, so an account trading only spot still starts cleanly.

**No local kill switch.** A boolean inside the process is not a security
boundary. Control belongs on the key (permissions, IP allow-list) and in the
explicit choice of `environment`.

## Testing seams

* `books.py` — pure `Decimal` in, changes out. No framework, no I/O.
* `instruments.py` — pure payload transformations; an unrepresentable payload
  yields `None` plus a warning, so one bad entry never aborts a batch load.
* `http/*` — namespaces take a client; the client's `request()` is the single
  network seam to stub.
* `websocket/*` — the transport takes a handler callable; tests drive it with
  recorded envelopes.
* `common/*` — pure functions throughout.

See [testing.md](testing.md).
