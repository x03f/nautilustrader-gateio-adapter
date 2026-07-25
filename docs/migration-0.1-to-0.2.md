# Migrating from 0.1.0 to 0.2.0

Version 0.2.0 is a rewrite. 0.1.0 was a spot-only adapter with a flat module
layout; 0.2.0 is a multi-product connector (spot, margin, perpetual, inverse
perpetual, delivery, options) built on real venue data throughout.

Read this page in full before upgrading: several changes are silent at import
time and only show up at runtime.

## Summary of breaking changes

| # | Change | Impact |
|---|---|---|
| 1 | Venue string `GATEIO` -> `GATE_IO` | every instrument id changes |
| 2 | Perpetual instrument ids gain a `-PERP` suffix | new ids for contracts |
| 3 | Synthetic quotes removed | `QuoteTick`s are now real, and only real |
| 4 | Paper execution module removed | use Nautilus sandbox/backtest execution |
| 5 | Standalone reconciliation helper removed | superseded by real report generation |
| 6 | Flat modules replaced by sub-packages | deep imports move; top-level imports keep working |
| 7 | **Execution defaults to mainnet** (was testnet) | *read this one twice* |
| 8 | The `live_orders` kill switch is gone | there is no in-process order block any more |
| 9 | The REST client is async, with typed namespaces | every direct REST call changes shape |
| 10 | Config fields renamed and re-scoped | 0.1.0 configs will not construct |

## 1. Venue: `GATEIO` -> `GATE_IO`

```python
from nautilus_gateio import GATEIO

GATEIO  # 0.1.0: "GATEIO"     0.2.0: "GATE_IO"
```

NautilusTrader's own tooling identifies this exchange as `GATE_IO`, and using
the same string keeps adapter instruments interchangeable with data loaded
through other NautilusTrader components.

Every instrument id, bar type and cached instrument therefore changes:

```text
BTC_USDT.GATEIO                     ->  BTC_USDT.GATE_IO
BTC_USDT.GATEIO-1-MINUTE-LAST-EXTERNAL  ->  BTC_USDT.GATE_IO-1-MINUTE-LAST-EXTERNAL
```

Anything that persisted instrument ids — a cache, a database, a saved config, a
strategy constant — must be updated. Ids are strings; there is no automatic
migration.

## 2. Perpetuals gain `-PERP`

527 of Gate.io's USDT perpetual contracts share their exact symbol with a spot
pair, so a perpetual id must be distinguishable from the spot id:

```text
spot        BTC_USDT.GATE_IO
perpetual   BTC_USDT-PERP.GATE_IO
inverse     BTC_USD-PERP.GATE_IO
delivery    BTC_USDT_20260807.GATE_IO
option      BTC_USDT-20260729-70000-C.GATE_IO
```

Delivery and option symbols carry their expiry and collide with nothing, so they
take no suffix. Full reasoning in [symbology.md](symbology.md).

**A perpetual id without `-PERP` silently resolves to the spot pair of the same
name.** That is a different instrument with different quantity semantics. Check
every hard-coded id.

## 3. Synthetic quotes removed

`GateioDataClientConfig.emit_synthetic_quotes` is gone. 0.1.0 fabricated a
`QuoteTick` around each bar close (close +/- 0.5 bp, unit sizes) so that
quote-driven fill simulations had something to consume. Nothing fabricated is
published as venue data any more.

0.2.0 subscribes to the venue's real `book_ticker` best bid/offer stream
instead. If a component of yours depended on a quote arriving with every bar,
subscribe to quotes explicitly:

```python
self.subscribe_quote_ticks(instrument_id)
```

For fill simulation, use NautilusTrader's own sandbox or backtest execution.

## 4. Paper execution removed

The local fill simulator (`PaperExecution`, `PaperFill`, `GateioPaperConfig`) is
gone. NautilusTrader's sandbox and backtest execution engines simulate fills
against the same data and are maintained upstream; a second simulator inside an
exchange adapter was the wrong place for that logic.

## 5. Standalone reconciliation helper removed

The read-only `reconcile()` diagnostic is gone. 0.2.0 implements the real
NautilusTrader reconciliation interface instead:

| Method | Source |
|---|---|
| `generate_order_status_reports` | open plus recently finished orders, every enabled product |
| `generate_order_status_report` | single lookup by venue or client order id |
| `generate_fill_reports` | `my_trades` per product over the lookback window |
| `generate_position_status_reports` | futures, delivery and options positions |

The framework calls these on startup; a restart with resting orders and open
positions is a supported path rather than a fresh start.

## 6. Sub-packaged layout

```text
0.1.0 (flat)              0.2.0
------------------------  -----------------------------------------
constants.py              common/constants.py
errors.py                 common/errors.py
signing.py                common/signing.py
symbols.py                common/symbols.py
schemas.py                common/parsing.py + instruments.py
http.py                   http/client.py + http/{spot,margin,futures,options,wallet}.py
websocket.py              websocket/client.py + websocket/{public,private}.py
futures.py                http/futures.py (as a typed namespace)
paper.py                  removed
reconcile.py              removed
```

The top-level `__init__` re-exports the public API, so
`from nautilus_gateio import GateioDataClient, GateioHttpClient` still works.
Deep imports (`from nautilus_gateio.http import ...` where `http` was a module)
must be updated.

Renamed public symbols:

| 0.1.0 | 0.2.0 |
|---|---|
| `instrument_id_to_gate_pair` | `instrument_id_to_gateio` (returns `(product, symbol)`) |
| `gate_pair_to_instrument_id` | `gateio_to_instrument_id(product, symbol)` |
| `build_currency_pair` | `parse_spot_instrument` (plus one parser per product) |
| `GateioFuturesPublicClient` / `GateioFuturesPrivateClient` | `GateioFuturesHttpAPI(client, settle=..., delivery=...)` |
| `StaticInstrumentProvider` | removed — use NautilusTrader's own static provider |
| `LiveOrdersDisabledError` | removed (see change 8) |
| `PaperExecution`, `PaperFill`, `GateioPaperConfig` | removed (see change 4) |
| `reconcile` | removed (see change 5) |
| `GATEIO_WS_MAINNET` | `GATEIO_WS_SPOT` (plus one constant per product) |

## 7. Execution defaults to mainnet

**This is the change most likely to cost money.**

```python
GateioExecClientConfig().environment
# 0.1.0: "testnet"
# 0.2.0: "mainnet"
```

A 0.1.0 configuration that relied on the default was talking to the testnet. The
same configuration under 0.2.0 talks to **mainnet with real funds**.

If you want the testnet, say so:

```python
GateioExecClientConfig(environment="testnet")
```

The reasoning: an execution client that silently points at a different exchange
environment than the operator believes is more dangerous than one that requires
the venue to be stated. The failure mode of the old default was "my orders
vanished into a sandbox"; the failure mode of an unstated environment in
general is worse, and the fix for both is the same — be explicit.

Note that Gate.io serves only spot and USDT perpetuals on the testnet.
Configuring any other product with `environment="testnet"` now raises
`ValueError` from the client constructor.

## 8. The `live_orders` kill switch is gone

0.1.0 shipped a `live_orders` flag on the HTTP client: order-mutating calls
raised `LiveOrdersDisabledError` unless it was set, and the documentation
described this as a guarantee that "credentials alone can never place an order".

Both the flag and `LiveOrdersDisabledError` are removed. A boolean inside the
process is not a security boundary — the process holds the key either way, and
the flag encouraged treating "the code will stop me" as a control.

Replace it with controls that actually bind:

1. **API key permissions** on the Gate.io side. Grant only what the strategy
   uses; never grant withdrawal permission to a trading key.
2. **IP allow-listing** on the key.
3. `environment="testnet"` for rehearsal.
4. NautilusTrader sandbox/backtest execution for simulation.

If you had code catching `LiveOrdersDisabledError`, delete the handler — the
call now simply reaches the venue.

## 9. The REST client is async and namespaced

0.1.0's `GateioHttpClient` was synchronous with spot methods on the class.
0.2.0's is an `async` shared transport, with one typed namespace per product:

```python
# 0.1.0
with GateioHttpClient() as client:
    candles = client.candles("BTC_USDT", interval="1m", limit=5)
    book = client.order_book("BTC_USDT", limit=5)

# 0.2.0
async with GateioHttpClient() as client:
    spot = GateioSpotHttpAPI(client)
    candles = await spot.candlesticks("BTC_USDT", interval="1m", limit=5)
    book = await spot.order_book("BTC_USDT", limit=5)
```

Namespaces: `GateioSpotHttpAPI`, `GateioMarginHttpAPI`, `GateioFuturesHttpAPI`
(perpetual, inverse and delivery via `settle` / `delivery`),
`GateioOptionsHttpAPI`, `GateioWalletHttpAPI`.

The methods `ping()`, `balances()`, `open_orders()`, `place_order_validated()`,
`cancel_all()` and `emergency_stop()` no longer exist. Their replacements live
on the namespace for the product concerned; see the class docstrings.

The WebSocket client also changed shape — it now takes the endpoint and the
product it serves, because a Gate.io message is only interpretable together with
the host it arrived on:

```python
GateioWebSocketClient(url=..., product=GateioProductType.SPOT, handler=...)
```

Prefer `GateioPublicWebSocket` / `GateioPrivateWebSocket`, which pick the
endpoint and the channel names for a product.

## 10. Configuration changes

### `GateioDataClientConfig`

| 0.1.0 field | Status in 0.2.0 |
|---|---|
| `venue` | removed — the venue is `GATE_IO`, from `common.constants` |
| `base_url_http` | kept (now an override of the `environment`-derived URL) |
| `base_url_ws` | kept (now overrides **every** configured product's endpoint) |
| `use_websocket` | removed — the WebSocket is the transport; REST serves requests and snapshots |
| `poll_interval_secs` | removed — there is no bar-polling fallback |
| `emit_synthetic_quotes` | removed (see change 3) |
| — | new: `environment`, `products`, `options_underlyings`, `update_instruments_interval_mins`, `http_timeout_secs`, `max_retries`, `order_book_snapshot_limit`, `order_book_update_interval_ms`, `bars_timestamp_on_close` |

### `GateioExecClientConfig`

| 0.1.0 field | Status in 0.2.0 |
|---|---|
| `environment` | kept — **default changed to `"mainnet"`** |
| `venue` | removed |
| `account_poll_interval_secs` (default 5.0) | renamed `account_polling_interval_secs` (default 30.0); it is now a safety net behind the private WebSocket, not the primary fill source |
| `client_order_id_tag` | kept |
| — | new: `products`, `options_underlyings`, `base_url_ws`, `spot_account_mode`, `max_retries`, `http_timeout_secs` |

### `GateioPaperConfig`

Removed together with the paper module.

The full 0.2.0 reference is in [configuration.md](configuration.md).

## Behavioural changes that are not API changes

* **Fills arrive over the private WebSocket**, not by REST polling. Fill latency
  drops accordingly, and the REST poll is now a backstop.
* **Market orders are no longer emulated with a fixed 1% cross.** Spot market
  sells and quote-denominated market buys are native venue market orders. Only a
  base-denominated spot market buy is expressed as an IOC limit, bounded by the
  pair's **own published slippage cap** rather than a hard-coded percentage.
* **More order types.** STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED and
  LIMIT_IF_TOUCHED route to each product's price-trigger endpoint; post-only
  (`poc`), FOK, `reduce_only` and iceberg (`display_qty`) are honoured.
* **Order modification works** on spot and perpetuals; delivery and options
  reject it explicitly, because the venue has no amend endpoint there.
* **Rejections are explicit.** Anything the venue cannot express is denied or
  rejected with a stated reason. Code that relied on a silent downgrade (for
  example a time in force being quietly coerced) will now see an
  `OrderRejected`.

## Upgrade checklist

1. Replace `GATEIO` with `GATE_IO` in every persisted or hard-coded instrument
   id and bar type.
2. Add `-PERP` to every perpetual id.
3. Set `environment` explicitly on `GateioExecClientConfig`.
4. Delete `emit_synthetic_quotes`, `use_websocket`, `poll_interval_secs` and
   `venue` from your configs; rename `account_poll_interval_secs`.
5. Remove any `live_orders` argument and any `LiveOrdersDisabledError` handler;
   review the key's permissions on the Gate.io side instead.
6. Convert direct REST usage to `await` plus the product namespace.
7. Subscribe to quotes explicitly if you were relying on synthetic ones.
8. Replace paper-trading usage with NautilusTrader sandbox or backtest
   execution.
9. Re-run your strategy on the testnet (spot or USDT perpetuals) before pointing
   it at mainnet.
