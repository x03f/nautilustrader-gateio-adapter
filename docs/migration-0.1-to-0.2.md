# Migrating from 0.1.0 to 0.2.0a1

`0.1.0` was the previous experimental, primarily spot-oriented implementation.
`0.2.0a1` is a substantial redesign rather than an incremental patch: the venue
string, the instrument ids, the package layout, the REST and WebSocket clients,
the configuration fields and the execution model all changed. Upgrading is a
port, not a version bump.

`0.2.0a1` is an **alpha**. Every capability referred to below is implemented and
covered by the offline test suite, and none of it has been validated against the
live venue — the per-capability position is in [validation.md](validation.md).
Re-testing your own strategy after the port is not optional.

Read this page in full first. Most of these changes fail loudly: an
`ImportError`, a `TypeError` from a constructor, a `ValueError` raised before
any network activity. Four do not. The execution environment default (§1), the
meaning of the `environment` string (§2), the perpetual suffix (§3) and the bar
timestamp (§11) all keep working after the upgrade and mean something different
from what they meant before. Those are the ones that cost money. The venue
string (§4) sits between the two: it breaks instrument ids loudly, and a
configuration dictionary key quietly.

## What 0.1.0 actually was

Worth stating plainly, because the 0.2.0a1 feature list is much longer and it is
easy to assume the old release did more than it did.

| Area | 0.1.0 | 0.2.0a1 |
|---|---|---|
| Products in the Nautilus path | spot only | spot, USDT perpetual, BTC-settled (inverse) perpetual, USDT delivery, USDT-settled options |
| Futures | a separate, experimental REST client, not wired into the execution path | first-class, through the same data and execution clients |
| Market data published | closed bars, plus a fabricated quote per bar | trades, real best bid/offer quotes, sequence-validated book deltas, bars, mark and index prices, funding rates |
| Order types | MARKET (emulated) and LIMIT; everything else rejected | MARKET, LIMIT, STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED |
| Time in force | the venue was always sent `gtc` for a LIMIT order | GTC, IOC, FOK, post-only (`poc`), with anything unsupported rejected rather than coerced |
| Fill detection | REST polling, every 5 s by default | the private WebSocket, with REST polling as a backstop |
| Order modification | logged a warning and did nothing | amends spot and perpetuals; delivery and options reject explicitly |
| Start-up reconciliation | the four report generators returned empty results | all four implemented against REST |
| Transport | synchronous REST, one WebSocket endpoint | `async` REST with a typed namespace per product, one WebSocket connection per product endpoint |

## Where 0.1.0 has gone

Nothing was deleted. The history of the release is preserved so that a working
deployment can stay where it is:

* the `v0.1.0` tag is untouched, and its GitHub release remains published;
* the implementation is retained on the branch `legacy/v0.1.0`, which points at
  the same commit as the tag;
* `0.1.0` receives no further changes — no fixes, no backports. It is kept
  readable, not maintained.

To stay on it, pin the tag:

```bash
pip install "nautilustrader-gateio-adapter @ git+https://github.com/x03f/nautilustrader-gateio-adapter@v0.1.0"
```

The full record of what changed is in the
[changelog](../CHANGELOG.md).

## Summary of breaking changes

The numbers are the sections below. The second column is what you will see if
you miss the row — the silent ones deserve the most attention, whatever else
they cost you to fix.

| § | Change | Fails how |
|---|---|---|
| 1 | **Execution defaults to mainnet** (was testnet) | silently — orders reach the real venue |
| 2 | **`environment` is matched exactly**: anything that is not `"testnet"` is mainnet | silently — a value 0.1.0 read as testnet now selects mainnet |
| 3 | Perpetual instrument ids gain a `-PERP` suffix | silently — an un-suffixed id resolves to the spot pair |
| 4 | Venue string `GATEIO` -> `GATE_IO` | loudly for instrument ids, silently for a config dictionary key |
| 5 | Synthetic quotes removed | quotes stop arriving unless you subscribe to them |
| 6 | The paper-fill simulator removed | `ImportError` |
| 7 | The standalone `reconcile()` helper removed, and real reconciliation now runs | `ImportError`, then a behavioural change at the next start-up |
| 8 | The `live_orders` kill switch removed | silently — there is no in-process order block any more |
| 9 | Flat modules replaced by sub-packages; several top-level names withdrawn | `ImportError` |
| 10 | The REST client is `async`, namespaced, and returns venue payloads unchanged | loudly on the missing `await`, silently on the payload shape |
| 11 | Bars are timestamped at the interval **close** by default (was the open) | silently — a stored series shifts by one interval |
| 12 | The WebSocket client takes an endpoint and a product | `TypeError` |
| 13 | Configuration fields renamed and re-scoped | a 0.1.0 config will not construct |
| 14 | The instrument provider signature, the account id and the account type changed | `TypeError`; stale persisted account state |

---

## 1. Execution defaults to mainnet

**This is the change most likely to cost money.**

```text
GateioExecClientConfig().environment
# 0.1.0: "testnet"
# 0.2.0a1: "mainnet"
```

A 0.1.0 configuration that relied on the default was talking to the testnet. The
same configuration under 0.2.0a1 talks to **mainnet, with real funds**.

If you want the testnet, say so:

```python
from nautilus_gateio import GateioExecClientConfig

config = GateioExecClientConfig(environment="testnet")
```

The reasoning is that an execution client which silently points at a different
exchange environment than the operator believes is more dangerous than one that
requires the venue to be stated. The failure mode of the old default was "my
orders vanished into a sandbox"; the failure mode of an unstated environment in
general is worse, and the fix for both is the same — be explicit.

Gate.io serves only spot and USDT perpetuals on the testnet. Configuring any
other product together with `environment="testnet"` raises `ValueError` from the
client constructor, before any network activity:

```text
Gate.io has no testnet endpoint for OPT; testnet supports SPOT, PERP
```

The data client also gained an `environment` field defaulting to `"mainnet"`.
That is not a behavioural change: 0.1.0's data client had no environment at all
and always used the mainnet REST and WebSocket hosts.

## 2. `environment` is now matched exactly

Related to §1 and easier to miss. The two releases decide what an environment
string means in opposite ways:

| `environment` | 0.1.0 resolves to | 0.2.0a1 resolves to |
|---|---|---|
| `"mainnet"` | mainnet | mainnet |
| `"testnet"` | testnet | testnet |
| `"test"`, `"sandbox"`, `""`, a typo | **testnet** | **mainnet** |

0.1.0 treated anything that was not `"mainnet"` as the testnet; 0.2.0a1 treats
only `"testnet"` (case-insensitively, surrounding whitespace stripped) as the
testnet and everything else as mainnet. The direction was reversed deliberately,
for the same reason as §1: under the old rule a value nobody recognised quietly
selected a venue, and the safe-looking half of that behaviour hid the unsafe
half. Under the new rule an unrecognised value selects the environment you have
to opt out of, which is the one you will notice.

Neither release validates the string. If you build the environment value at
runtime — from an environment variable, a CLI flag, a deployment template —
check it against the exact literal before handing it to the config.

## 3. Perpetuals gain `-PERP`

Gate.io reuses one symbol for a spot market and a USDT perpetual contract on a
large number of pairs (527 at the time of the survey recorded in
[symbology.md](symbology.md)), so a perpetual id has to be distinguishable from
the spot id:

```text
spot        BTC_USDT.GATE_IO
perpetual   BTC_USDT-PERP.GATE_IO
inverse     BTC_USD-PERP.GATE_IO
delivery    BTC_USDT_20260807.GATE_IO
option      BTC_USDT-20260729-70000-C.GATE_IO
```

Delivery and option symbols carry their expiry and collide with nothing, so they
take no suffix. `-PERP` is the established NautilusTrader convention for exactly
this case; the full reasoning is in [symbology.md](symbology.md).

**A perpetual id written without `-PERP` resolves to the spot pair of the same
name.** It does not raise: `BTC_USDT.GATE_IO` is a valid, loadable spot
instrument. The two have different quantity semantics — a spot `Quantity` is an
amount of base currency, a perpetual `Quantity` is a number of contracts — so
the mistake is not caught downstream either. Check every hard-coded id.

## 4. Venue: `GATEIO` -> `GATE_IO`

```python
from nautilus_gateio import GATEIO

GATEIO  # 0.1.0: "GATEIO"     0.2.0a1: "GATE_IO"
```

NautilusTrader's own tooling identifies this exchange as `GATE_IO`, and using
the same string keeps adapter instruments interchangeable with data loaded
through other NautilusTrader components.

Every instrument id and bar type therefore changes:

```text
BTC_USDT.GATEIO                          ->  BTC_USDT.GATE_IO
BTC_USDT.GATEIO-1-MINUTE-LAST-EXTERNAL   ->  BTC_USDT.GATE_IO-1-MINUTE-LAST-EXTERNAL
```

Anything that persisted an instrument id — a cache, a database, a saved config,
a strategy constant — must be updated. Ids are strings; there is no automatic
migration.

The venue is no longer configurable. 0.1.0 exposed a `venue` field on both
config classes and on `GateioInstrumentProvider`; 0.2.0a1 takes the venue from
`nautilus_gateio.common.constants` and the clients register themselves against
it unconditionally. A venue that can differ between the instrument provider, the
data client and the execution client is a routing failure waiting to happen, and
nothing about this exchange needed the flexibility.

One consequence is quiet. The client factories build their `ClientId` from the
key you use in `TradingNodeConfig`, so a hard-coded `"GATEIO"` key now produces
a client whose id does not match the venue it registers for:

```python
from nautilus_gateio import GATEIO, GateioDataClientConfig

data_clients = {GATEIO: GateioDataClientConfig()}  # use the constant, not a literal
```

## 5. Synthetic quotes removed

`GateioDataClientConfig.emit_synthetic_quotes` is gone. 0.1.0 fabricated a
`QuoteTick` around each bar close (close +/- 0.5 basis points, unit sizes) so
that quote-driven fill simulations had something to consume, and it did so by
default. Nothing fabricated is published as venue data any more: a data feed that
invents the top of book is indistinguishable from one that reports it, right up
to the point where a strategy trades on it.

0.2.0a1 subscribes to the venue's real `book_ticker` best bid/offer stream
instead. If something of yours depended on a quote arriving with every bar,
subscribe to quotes explicitly:

```python
self.subscribe_quote_ticks(instrument_id)
```

For fill simulation, use NautilusTrader's own sandbox or backtest execution.

## 6. Paper execution removed

The local fill simulator (`PaperExecution`, `PaperFill`, `GateioPaperConfig`) is
gone. NautilusTrader's sandbox and backtest execution engines simulate fills
against the same data, are maintained upstream and are tested far more widely
than a second simulator inside an exchange adapter ever would be.

## 7. Standalone reconciliation helper removed — and real reconciliation now runs

The read-only `reconcile()` diagnostic is gone. It compared local state against
the exchange and returned a report; it never touched the Nautilus cache, because
0.1.0 did not implement the platform's reconciliation interface at all — its
four report generators returned empty results, which the framework reads as "the
venue has nothing", i.e. fresh-start semantics.

0.2.0a1 implements the real interface:

| Method | Source |
|---|---|
| `generate_order_status_reports` | open plus recently finished orders, for every enabled product |
| `generate_order_status_report` | a single lookup by venue or client order id |
| `generate_fill_reports` | `my_trades` per product over the lookback window |
| `generate_position_status_reports` | futures, delivery and options positions |

This is a behavioural change even for code that never called `reconcile()`.
NautilusTrader enables reconciliation by default
(`LiveExecEngineConfig.reconciliation` is `True`), so on the next start-up your
node will be handed the orders, fills and positions that already exist on the
account, instead of the empty lists 0.1.0 returned. A restart with resting
orders and open positions becomes a supported path — and an account carrying
state you did not expect the node to adopt will now show it. Review your
`external_order_claims` and your strategy's start-up assumptions before the
first run. When the engine asks for no explicit window, the report builders look
back 24 hours. Details are in [execution.md](execution.md).

## 8. The `live_orders` kill switch is gone

0.1.0 shipped a `live_orders` flag on the HTTP client: order-mutating calls
raised `LiveOrdersDisabledError` unless it was set, and the documentation
described this as a guarantee that credentials alone could never place an order.

Both the flag and `LiveOrdersDisabledError` are removed, along with
`emergency_stop()`, which flipped the flag back off. A boolean inside the
process is not a security boundary — the process holds the key either way, and
the flag encouraged treating "the code will stop me" as a control.

Replace it with controls that actually bind:

1. **API key permissions** on the Gate.io side. Grant only what the strategy
   uses; never grant withdrawal permission to a trading key.
2. **IP allow-listing** on the key.
3. `environment="testnet"` for rehearsal (spot and USDT perpetuals only).
4. NautilusTrader sandbox or backtest execution for simulation.

If you had code catching `LiveOrdersDisabledError`, delete the handler — the
call now simply reaches the venue.

## 9. Sub-packaged layout

```text
0.1.0 (flat)              0.2.0a1
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
config.py                 config.py, with credential resolution in common/credentials.py
```

The top-level `__init__` re-exports the public API, so
`from nautilus_gateio import GateioDataClient, GateioHttpClient` still works.
Deep imports of the old flat modules must be updated — including
`from nautilus_gateio.config import resolve_credentials`, which now lives in
`nautilus_gateio.common.credentials` (and remains available from the top level).

Renamed public symbols:

| 0.1.0 | 0.2.0a1 |
|---|---|
| `instrument_id_to_gate_pair` | `instrument_id_to_gateio` (returns `(product, symbol)`) |
| `gate_pair_to_instrument_id` | `gateio_to_instrument_id(product, symbol)` |
| `build_currency_pair` | `parse_spot_instrument`, plus one parser per product |
| `GateioFuturesPublicClient` / `GateioFuturesPrivateClient` | `GateioFuturesHttpAPI(client, settle=..., delivery=...)` |
| `GATEIO_WS_MAINNET` | `GATEIO_WS_SPOT`, plus one constant per product endpoint |

Withdrawn from the public API entirely:

| 0.1.0 name | Position in 0.2.0a1 |
|---|---|
| `PaperExecution`, `PaperFill`, `GateioPaperConfig` | removed (see §6) |
| `reconcile` | removed (see §7) |
| `LiveOrdersDisabledError` | removed (see §8) |
| `StaticInstrumentProvider` | removed — use NautilusTrader's own static provider |
| `RateLimiter` | no longer exported; it is an internal detail of the HTTP transport |
| `validate_order` (in `schemas.py`) | removed — order validation happens in the execution client, which rejects with a stated reason |

`RateLimiter` is the one that catches people out: `from nautilus_gateio import
RateLimiter` used to work and now raises `ImportError`. The class still exists
inside `nautilus_gateio.http.client`, but it is not part of the supported
surface and its constructor arguments have changed.

## 10. The REST client is async, namespaced, and returns venue payloads unchanged

0.1.0's `GateioHttpClient` was synchronous, with spot methods on the class, and
it decoded each response into a flat dictionary of its own design. 0.2.0a1's is
an `async` shared transport that handles signing, pacing, retries and error
translation, with one typed namespace per product on top:

```text
# 0.1.0
with GateioHttpClient() as client:
    candles = client.candles("BTC_USDT", interval="1m", limit=5)
    book = client.order_book("BTC_USDT", limit=5)
```

```python
import asyncio

from nautilus_gateio import GateioHttpClient, GateioSpotHttpAPI


async def main() -> None:
    async with GateioHttpClient() as client:
        spot = GateioSpotHttpAPI(client)
        candles = await spot.candlesticks("BTC_USDT", interval="1m", limit=5)
        book = await spot.order_book("BTC_USDT", limit=5)


asyncio.run(main())
```

The namespaces are `GateioSpotHttpAPI`, `GateioMarginHttpAPI`,
`GateioFuturesHttpAPI` (perpetual, inverse and delivery, selected by `settle`
and `delivery`), `GateioOptionsHttpAPI` and `GateioWalletHttpAPI`.

**The payload shape changed as well, and this part is silent.** 0.1.0 returned
parsed dictionaries — `candles()` produced `{"ts", "open", "high", "low",
"close", "volume"}` per row, `order_book()` produced float ladders,
`balances()` produced `{currency: {available, locked}}`. The 0.2.0a1 namespaces
return the decoded venue payload unchanged, which for candlesticks means Gate's
positional array `[timestamp_s, quote_volume, close, high, low, open,
base_volume, closed]` — note that the close precedes high, low and open. The
adapter translates into NautilusTrader objects one layer up, in
`nautilus_gateio.instruments` and the data client, so the namespaces stay a
faithful, auditable view of the API. Code that consumed the old parsed
dictionaries has to be rewritten against the venue's own schema.

None of `ping()`, `balances()`, `open_orders()`, `place_order_validated()`,
`cancel_all()`, `emergency_stop()` or `ticker_last()` exists on the client any
more. Most have an equivalent on the namespace for the product concerned:
`cancel_all()` keeps its name, `balances()` became `accounts()`,
`ticker_last()` became `tickers()`, `ping()` became `server_time()`, and open
orders are listed by `open_orders()` on spot and `list_orders()` on the futures
and delivery namespaces. `place_order_validated()` and
`emergency_stop()` have no replacement — order construction and validation are
the execution client's job, and §8 explains why the emergency stop is gone. See
the namespace docstrings and [market-data.md](market-data.md).

## 11. Bars are timestamped at the close

`GateioDataClientConfig.bars_timestamp_on_close` is new and defaults to `True`,
which is the NautilusTrader convention. 0.1.0 had no such option and timestamped
every bar at the **open** of its interval, which is the `t` field Gate.io sends.

The effect is silent: a one-minute bar that 0.1.0 published with
`ts_event = 12:00:00` is published by 0.2.0a1 with `ts_event = 12:01:00`. Any
series you stored under 0.1.0 is offset by one interval against a series
recorded now, and any strategy that compares a bar timestamp against another
clock will be off by the bar period.

Set `bars_timestamp_on_close=False` to keep the old behaviour, at the cost of
disagreeing with the rest of the platform.

## 12. The WebSocket client takes an endpoint and a product

Gate.io partitions its streams across separate hosts — spot, USDT perpetuals,
BTC-settled perpetuals, delivery futures and options each have their own — and a
message is only interpretable together with the host it arrived on, because the
channel names differ per product. The client constructor therefore requires
both:

```python
from nautilus_gateio import GateioProductType, GateioWebSocketClient

ws = GateioWebSocketClient(
    url="wss://api.gateio.ws/ws/v4/",
    product=GateioProductType.SPOT,
    handler=print,
)
```

0.1.0's client took `on_bar` and `on_trade` callbacks and defaulted to the one
spot endpoint. 0.2.0a1 takes a single `handler` receiving every decoded message.

Prefer `GateioPublicWebSocket` and `GateioPrivateWebSocket`, which pick the
endpoint and the channel names for a product themselves:

```python
from nautilus_gateio import GateioProductType, GateioPublicWebSocket

ws = GateioPublicWebSocket(product=GateioProductType.PERP, handler=print)
```

## 13. Configuration changes

### `GateioDataClientConfig`

| 0.1.0 field | Position in 0.2.0a1 |
|---|---|
| `venue` | removed — the venue is `GATE_IO` (see §4) |
| `base_url_http` | kept; now `None` by default and an override of the URL derived from `environment` |
| `base_url_ws` | kept; now `None` by default, and an override for **every** configured product's endpoint |
| `use_websocket` | removed — the WebSocket is the transport; REST serves requests and snapshots |
| `poll_interval_secs` | removed — there is no bar-polling fallback |
| `emit_synthetic_quotes` | removed (see §5) |
| — | new: `api_key`, `api_secret`, `environment`, `products`, `options_underlyings`, `update_instruments_interval_mins`, `http_timeout_secs`, `max_retries`, `order_book_snapshot_limit`, `order_book_update_interval_ms`, `bars_timestamp_on_close` |

### `GateioExecClientConfig`

| 0.1.0 field | Position in 0.2.0a1 |
|---|---|
| `environment` | kept — **default changed to `"mainnet"`** (see §1) |
| `venue` | removed |
| `account_poll_interval_secs` (default 5.0) | renamed `account_polling_interval_secs` (default 30.0); it is now a safety net behind the private WebSocket, not the primary fill source |
| `client_order_id_tag` | kept |
| `base_url_http` | kept |
| — | new: `products`, `options_underlyings`, `base_url_ws`, `spot_account_mode`, `max_retries`, `http_timeout_secs` |

Both classes are frozen `msgspec` structs, so cross-field validation runs in the
client constructors rather than in `__post_init__`. `validate_products`,
`validate_book_interval_ms` and `validate_snapshot_limit` are public if you want
to check a configuration before building a client. The full reference is in
[configuration.md](configuration.md).

`GateioPaperConfig` was removed with the paper module.

The defaults for `products` are spot-only on both clients, so a spot-only 0.1.0
deployment does not have to name its products to keep behaving as it did.

## 14. Instrument provider and account identity

`GateioInstrumentProvider` now requires an `http_client` (0.1.0 built one for
you if you passed nothing), drops the `venue` argument, and gains `products` and
`options_underlyings`:

```python
from nautilus_gateio import GateioHttpClient, GateioInstrumentProvider, GateioProductType

provider = GateioInstrumentProvider(
    GateioHttpClient(),
    products=(GateioProductType.SPOT, GateioProductType.PERP),
)
```

The account the execution client reports against also changed, which matters if
anything of yours persisted or keyed on it:

| | 0.1.0 | 0.2.0a1 |
|---|---|---|
| `AccountId` | `GATEIO-SPOT` | `GATE_IO-master` |
| `AccountType` | always `CASH` | `CASH` only when spot is the sole product *and* it trades the plain spot ledger; `MARGIN` otherwise |

Gate.io keeps a separate wallet per product, and the client aggregates the
wallets of the enabled products into that one account — hence `master` rather
than a product name. See [products.md](products.md) for what that means per
product.

## Behavioural changes that are not API changes

* **Fills arrive over the private WebSocket**, not by REST polling. A fill is
  now published when the venue pushes it rather than when the next poll happens,
  and the REST poll — every 30 s by default, against 5 s in 0.1.0 — is a
  backstop that refreshes account state and catches anything the stream missed.
  The end-to-end latency of that has not been measured against the live venue.
* **Market orders are no longer emulated with a fixed 1% cross.** 0.1.0 sent
  every MARKET order as an IOC limit priced 1% through the last trade. In
  0.2.0a1, spot market sells and quote-denominated market buys are native venue
  market orders, and derivative market orders use the venue's own
  `price="0"`/IOC form. Only a base-denominated spot market buy is still
  expressed as an IOC limit — Gate.io's native spot market buy spends a *quote*
  amount, so it cannot say "buy exactly this many base units" — and it is
  bounded by the pair's own published slippage cap rather than a hard-coded
  percentage. A pair that publishes no cap falls back to a documented default;
  see [execution.md](execution.md).
* **Time in force is honoured.** 0.1.0 sent `gtc` for every LIMIT order
  regardless of what the order asked for, so an IOC or FOK limit rested on the
  book. 0.2.0a1 maps GTC, IOC and FOK, expresses post-only as `poc`, and
  refuses anything Gate.io cannot express — a time in force such as GTD or DAY,
  or reduce-only on a spot order — with a stated reason. Every refusal the
  adapter makes on its own is `OrderDenied` before submission, whether it is an
  order type the venue does not have or terms it cannot be given;
  `OrderRejected` means Gate.io itself refused. Code that relied on the silent
  downgrade will see a denial where it previously saw a resting order, which is
  the intended direction: a refusal you can read beats an order that is not the
  one you submitted.
* **Order modification works** on spot and perpetuals. 0.1.0 logged a warning
  and did nothing, leaving the command unanswered; 0.2.0a1 amends where the
  venue has an amend endpoint and emits `OrderModifyRejected` with a reason where
  it does not (delivery and options).
* **More order types.** STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED and
  LIMIT_IF_TOUCHED route to each product's price-trigger endpoint; `reduce_only`
  and iceberg (`display_qty`) are honoured. Options have no price-trigger
  endpoint, so conditional types are rejected there.
* **Bar intervals widened** from eight (1m to 1d) to eleven (1s to 7d).

## Upgrade checklist

1. Replace `GATEIO` with `GATE_IO` in every persisted or hard-coded instrument
   id and bar type, and use the `GATEIO` constant — not a literal — as the key
   in `TradingNodeConfig.data_clients` and `exec_clients`.
2. Add `-PERP` to every perpetual id.
3. Set `environment` explicitly on `GateioExecClientConfig`, and check that the
   value is exactly `"mainnet"` or `"testnet"` if you compute it at runtime.
4. Delete `venue`, `use_websocket`, `poll_interval_secs` and
   `emit_synthetic_quotes` from your configs; rename
   `account_poll_interval_secs` to `account_polling_interval_secs` and check
   that the new 30 s default suits you.
5. Remove any `live_orders` argument and any `LiveOrdersDisabledError` handler;
   review the key's permissions on the Gate.io side instead.
6. Convert direct REST usage to `await` plus the product namespace, and rewrite
   whatever consumed the old parsed payloads against the venue schema.
7. Replace `from nautilus_gateio import RateLimiter` and any other withdrawn
   top-level import (§9).
8. Subscribe to quotes explicitly if you were relying on synthetic ones.
9. Replace paper-trading usage with NautilusTrader sandbox or backtest
   execution.
10. Decide on `bars_timestamp_on_close` before you mix new bars into a stored
    series.
11. Expect reconciliation to hand you real venue state on start-up; review the
    account, your `external_order_claims` and your strategy's start-up
    assumptions.
12. Re-run the strategy on the testnet (spot or USDT perpetuals) before pointing
    it at mainnet, and treat the first mainnet run as validation — see
    [validation.md](validation.md).
