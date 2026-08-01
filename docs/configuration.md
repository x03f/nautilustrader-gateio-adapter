# Configuration

Every configuration class lives in `nautilus_gateio.config`. Both are frozen
`msgspec` structs extending the standard NautilusTrader live-client configs
(`LiveDataClientConfig` / `LiveExecClientConfig`), so they can be embedded
directly in a `TradingNodeConfig`.

This adapter is an external, community-maintained integration for
NautilusTrader 1.230.0, written in pure Python, and released as **alpha**.
Live-venue validation covers market data, spot execution, a series of orders on
one USDT perpetual and three on one option contract, and no further: see
[validation.md](validation.md) for what has and has not been exercised. Read
this page as the description of what the code does, not as a promise about what
the venue will do with it.

The two field tables below are complete for the fields the adapter adds. Fields
inherited from the NautilusTrader base configs are covered separately under
[Inherited fields](#inherited-fields).

## The environment default is mainnet

`GateioDataClientConfig` and `GateioExecClientConfig` both set
`environment="mainnet"`. A client that is not told otherwise signs its requests
against the live exchange, with live funds.

That is a deliberate choice, and it reverses 0.1.0: in that release the field
started on the testnet for order execution, and a configuration relying on the
default was talking to a sandbox. The change is recorded in full in
[migration-0.1-to-0.2.md](migration-0.1-to-0.2.md). The reasoning: a client
whose default quietly points at a different exchange environment than the
operator assumes is more dangerous than one that requires the environment to be
stated. The failure modes are asymmetric. If the default is the live venue and
you meant to rehearse, your first order tells you immediately, at a size you
chose. If the default is a rehearsal venue and you meant to trade, you can
spend a long time watching orders that were never real.

Safety therefore comes from explicit configuration and from venue-side controls,
not from a default that points somewhere harmless. See
[Safety model](#safety-model).

One consequence worth stating plainly: only the exact string `"testnet"`
(case-insensitively, surrounding whitespace ignored) selects the testnet.
`is_testnet()` compares against that one value, so `"test"`, `"sandbox"`,
`"prod"`, `"live"` and `""` are all treated as mainnet rather than rejected. A
typo in this field is not an error; it is the live exchange.

```python
from nautilus_gateio.config import MAINNET, TESTNET

MAINNET  # "mainnet"
TESTNET  # "testnet"
```

| `environment`                         | REST base URL                    |
|---------------------------------------|----------------------------------|
| `"mainnet"` (default on both clients) | `https://api.gateio.ws`          |
| `"testnet"`                           | `https://api-testnet.gateapi.io` |

### Which products exist on the testnet

Gate.io publishes testnet endpoints for **spot and USDT-margined perpetual
futures only**. `config.TESTNET_PRODUCTS` names exactly those two. There is no
testnet endpoint for BTC-settled (inverse) perpetuals, delivery futures or
options, so configuring `INVERSE`, `FUT` or `OPT` together with
`environment="testnet"` raises `ValueError` from the client constructor, before
any network activity.

| Product                           | Mainnet | Testnet |
|-----------------------------------|---------|---------|
| Spot                              | ✓       | ✓       |
| USDT-margined perpetual (`PERP`)  | ✓       | ✓       |
| BTC-settled perpetual (`INVERSE`) | ✓       | -       |
| Delivery futures (`FUT`)          | ✓       | -       |
| Options (`OPT`)                   | ✓       | -       |

A hyphen in the testnet column means Gate.io publishes no testnet endpoint for
that product at all.

That check runs on the product set, not on the URLs, so it also applies when
`base_url_ws` is set. The override itself is honored — an explicit URL is the
operator's decision — but the product/environment combination is still rejected
first.

Environment selection, URL resolution and this validation are *implemented and
mock-tested* (`tests/test_config.py`, `tests/test_factories.py`).

## The venue string is `GATE_IO`

The venue is `GATE_IO`, exported as the `GATEIO` constant from
`nautilus_gateio` (defined in `nautilus_gateio.common.constants`). Instrument
ids therefore read `BTC_USDT.GATE_IO`, `BTC_USDT-PERP.GATE_IO` and so on; see
[symbology.md](symbology.md).

The underscore is not a style preference. NautilusTrader 1.230.0 already
identifies this venue as `GATE_IO` — its Tardis integration maps that venue
string onto Gate.io's exchange feeds — so using the same string keeps
instruments loaded through other NautilusTrader tooling interoperable with
instruments loaded through this adapter. Version 0.1.0 of this package used
`GATEIO`, which did not line up; the migration guide covers the change.

Use the constant as the key of the client dictionaries. The key becomes the
client id, while the venue is always `GATE_IO` regardless of the key, and
keeping the two identical avoids a client id that names something the engine
does not route by:

```python
from nautilus_gateio import GATEIO, GateioLiveDataClientFactory, GateioLiveExecClientFactory

node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
```

## Credentials and environment variables

`api_key` / `api_secret` default to `None`, in which case they are resolved from
the environment when the client is created
(`nautilus_gateio.common.credentials.resolve_credentials`):

| Variable                                           | Used when                                               |
|----------------------------------------------------|---------------------------------------------------------|
| `GATE_API_KEY` / `GATE_API_SECRET`                 | `environment="mainnet"`, and as the fallback on testnet |
| `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` | `environment="testnet"`                                 |

Resolution order is: explicit configuration values, then the environment, then
empty strings. Values are stripped of surrounding whitespace, because a key
pasted with a trailing newline otherwise produces signatures the venue rejects
without explaining why. An explicit `""` is honored as "no credentials" and is
not replaced from the environment.

Empty credentials are a valid state: public market data needs none. A signed
request attempted without them fails locally with the label
`MISSING_CREDENTIALS` rather than being sent. Credentials are never logged;
`credentials.mask` renders a short fingerprint for diagnostics. It is
NautilusTrader's own `mask_api_key`, the one OKX and Deribit log through, rather
than a copy of it: an absent credential renders `<empty>`, anything up to eight
characters renders `***` without disclosing its length, and anything longer
renders as its first four and last four characters (`abcd...wxyz`).

The testnet fallback deserves one caution. With `environment="testnet"` and only
the mainnet variables set, the mainnet key is used against the testnet host —
the fallback exists so that a single pair of variables can drive both, but a
mainnet key is not valid there and the venue will reject the signature. Setting
`GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` explicitly avoids the
ambiguity.

Credential resolution and masking are *implemented and mock-tested*
(`tests/test_config.py`).

## `GateioDataClientConfig`

Extends `nautilus_trader.live.config.LiveDataClientConfig`.

| Field                              | Type                            | Default                     | Meaning                                                                                                                                                             |
|------------------------------------|---------------------------------|-----------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `api_key`                          | `str \| None`                   | `None`                      | API key. `None` reads the environment; public market data needs no credentials                                                                                      |
| `api_secret`                       | `str \| None`                   | `None`                      | API secret. `None` reads the environment                                                                                                                            |
| `environment`                      | `str`                           | `"mainnet"`                 | `"mainnet"` or `"testnet"`; anything else is treated as mainnet                                                                                                     |
| `products`                         | `tuple[GateioProductType, ...]` | `(GateioProductType.SPOT,)` | Products to load instruments for and open public WebSocket streams on. One client multiplexes every configured product                                              |
| `options_underlyings`              | `tuple[str, ...] \| None`       | `None`                      | Restricts option instrument loading to these underlyings, e.g. `("BTC_USDT",)`. Ignored unless `OPT` is configured                                                  |
| `base_url_http`                    | `str \| None`                   | `None`                      | Overrides the REST base URL derived from `environment`                                                                                                              |
| `base_url_ws`                      | `str \| None`                   | `None`                      | Overrides the WebSocket URL for **every** configured product. Intended for a single-product setup or a local aggregating proxy                                      |
| `update_instruments_interval_mins` | `NonNegativeInt \| None`        | `60`                        | Interval of the instrument reload task. `None` (or `0`) disables reloading; a negative period is refused                                                            |
| `http_timeout_secs`                | `PositiveFloat`                 | `20.0`                      | Per-request REST timeout. Must be above `0`                                                                                                                         |
| `max_retries`                      | `PositiveInt`                   | `3`                         | Total REST attempts for a request the transport may safely repeat. Must be at least `1`; below that the configuration is **refused**, not clamped                   |
| `order_book_snapshot_limit`        | `PositiveInt`                   | `100`                       | Depth of the REST snapshot seeding each local book. Must be one of `1, 5, 10, 20, 50, 100`. Also the level requested on the WebSocket where the product accepts one |
| `order_book_update_interval_ms`    | `PositiveInt`                   | `100`                       | Push interval of the incremental depth stream. Must be one of `20`, `100`, `1000`                                                                                   |
| `bars_timestamp_on_close`          | `bool`                          | `True`                      | Timestamp bars at the close of their interval (the Nautilus convention). `False` timestamps at the open, matching Gate.io's `t` field                               |

Helpers on the class:

* `is_testnet` (property) — whether `environment` selects the testnet.
* `resolve_http_url()` — the REST base URL, honoring `base_url_http`.
* `resolve_ws_url(product)` — the WebSocket URL for one product, honoring
  `base_url_ws`.

### Book interval and depth are venue-constrained

`order_book_update_interval_ms` is validated against the union of intervals
Gate.io accepts anywhere (`20`, `100`, `1000`), because the per-product sets
differ: spot and the perpetuals take `20` and `100`, delivery and options take
`100` and `1000`. A value the *subscribed* product does not accept is adjusted
to a supported one at subscription time, with a warning, rather than failing the
subscription — `100` is preferred as the fallback because it is the interval
every product serves at full depth. Depth behaves the same way: a level the
product does not stream is rounded up to the nearest one it does.
[market-data.md](market-data.md) describes what the resulting stream looks like.

## `GateioExecClientConfig`

Extends `nautilus_trader.live.config.LiveExecClientConfig`.

| Field                           | Type                            | Default                      | Meaning                                                                                                                                            |
|---------------------------------|---------------------------------|------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `api_key`                       | `str \| None`                   | `None`                       | API key. `None` reads the environment; trading always needs credentials                                                                            |
| `api_secret`                    | `str \| None`                   | `None`                       | API secret. `None` reads the environment                                                                                                           |
| `environment`                   | `str`                           | `"mainnet"`                  | `"mainnet"` or `"testnet"`; anything else is treated as mainnet                                                                                    |
| `products`                      | `tuple[GateioProductType, ...]` | `(GateioProductType.SPOT,)`  | Products this client trades. Gate.io keeps a separate wallet per product, so enabling several aggregates several wallets into one Nautilus account |
| `options_underlyings`           | `tuple[str, ...] \| None`       | `None`                       | Restricts option instrument loading, as for the data client                                                                                        |
| `base_url_http`                 | `str \| None`                   | `None`                       | Overrides the REST base URL derived from `environment`                                                                                             |
| `base_url_ws`                   | `str \| None`                   | `None`                       | Overrides the private WebSocket URL for every configured product                                                                                   |
| `spot_account_mode`             | `GateioSpotAccountMode`         | `GateioSpotAccountMode.SPOT` | Which ledger spot orders trade against: `SPOT`, `MARGIN` (isolated), `CROSS_MARGIN` or `UNIFIED`. See [Account model](#account-model)              |
| `client_order_id_tag`           | `str`                           | `"ng"`                       | Short tag embedded in generated Gate.io `text` client order ids                                                                                    |
| `account_polling_interval_secs` | `NonNegativeFloat`              | `30.0`                       | Interval of the REST account-state poll that backs up the private WebSocket balance stream. `0` disables the poll; a negative interval is refused  |
| `max_retries`                   | `PositiveInt`                   | `3`                          | Total REST attempts for a request the transport may safely repeat. Must be at least `1`; below that the configuration is **refused**, not clamped  |
| `http_timeout_secs`             | `PositiveFloat`                 | `20.0`                       | Per-request REST timeout. Must be above `0`                                                                                                        |

Helpers on the class: `is_testnet`, `resolve_http_url()`,
`resolve_ws_url(product)` — identical to the data client's.

### What `max_retries` may and may not repeat

The count is the total number of attempts, not additional ones, and it does not
apply uniformly. The shared transport replays `GET`, `HEAD`, `OPTIONS` and
`DELETE` on any transient failure — HTTP 429, a 5xx, a retryable venue label, or
a connection error proving the request never left the process — because
repeating them cannot change the outcome at the venue. It replays an order
submission, amendment, transfer or borrow **only** when the venue has stated
that the request was rejected before it was processed (HTTP 429, or a label
meaning the same). Any other failure of a mutating request is raised as an
ambiguous-request error telling the caller to reconcile rather than resubmit.
Raising `max_retries` therefore does not increase the risk of a duplicate order.
This classification is *implemented and mock-tested*
(`tests/test_http_client.py`).

## Numbers outside their range are refused, not repaired

Every numeric field above carries one of NautilusTrader's constrained types, and
a value outside that range is refused with a `ValueError` naming the field. This
happens on **both** ways in:

```python
GateioExecClientConfig(max_retries=0)
# ValueError: `max_retries` is out of range for GateioExecClientConfig:
#             Expected `int` >= 1, was 0

ImportableConfig(
    path="nautilus_gateio.config:GateioExecClientConfig",
    config={"max_retries": 0},
).create()
# msgspec.ValidationError: Expected `int` >= 1 - at `$.max_retries`
```

The two exception types differ because the second is raised by `msgspec`'s
decoder, but `msgspec.ValidationError` is a subclass of `ValueError`, so one
`except ValueError` catches either. The constrained type alone would only cover
the second: a `msgspec` constraint is checked when a struct is *decoded*, and
writing the config in Python — the form every example in this repository uses —
decodes nothing. The classes therefore also check themselves in `__post_init__`,
which `msgspec` runs after a direct construction as well as after a decode.

Because a construction that would fail can no longer succeed,
`NautilusConfig.validate()` keeps its declared contract: it still returns `True`
for every configuration that exists, rather than raising.

Two fields are non-negative rather than positive —
`update_instruments_interval_mins` and `account_polling_interval_secs` — because
`0` is this adapter's documented spelling of "run no such task". What is refused
there is a negative period.

Range is not the same as set. `order_book_update_interval_ms` and
`order_book_snapshot_limit` are additionally checked against the discrete values
Gate.io actually serves; `37` ms is a positive integer and still wrong. Those two
checks run in the client constructor and are callable on their own as
`validate_book_interval_ms()` and `validate_snapshot_limit()`.

### `max_retries` below `1` used to be clamped

Until `0.2.0a2` the shared transport did `max(1, max_retries)`, and both tables
above promised that. A deployment that asked for no retries silently got one
attempt. The clamp is gone: `GateioHttpClient(max_retries=0)` now raises
`ValueError` too. It has to be at least `1` because the attempt loop treats it as
a count of attempts — with `0` the request is never sent, and the caller would be
handed `TOO_MANY_REQUESTS` about a request that never left the process. An early
refusal naming the field is worth more than a number quietly replaced by a
different one.

## Inherited fields

These come from the NautilusTrader base configs and behave here as they do for
any other adapter; they are documented upstream.

| Field                 | Applies to   | Default                                     | Note for this adapter                                                                                                                                                                                          |
|-----------------------|--------------|---------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `instrument_provider` | both clients | `InstrumentProviderConfig()`                | `load_all` / `load_ids` select what the provider loads. `filters` may carry `{"options_underlyings": [...]}`, which overrides the `options_underlyings` field                                                  |
| `routing`             | both clients | `RoutingConfig(default=False, venues=None)` | The client registers itself for the `GATE_IO` venue. Setting `default=True` makes it the engine's fallback client for venues no other client claims, which is rarely what you want with a single-venue adapter |
| `handle_revised_bars` | data client  | `False`                                     | Standard NautilusTrader behavior                                                                                                                                                                               |

## Choosing products

One client of each kind serves every configured product. Gate.io gives each
product family its own REST namespace, its own WebSocket host and its own
wallet, so `products` is what decides which endpoints are opened and which
wallets are read. The tuple is de-duplicated, order preserved, and an empty
tuple is rejected. [products.md](products.md) describes what each product means
in NautilusTrader terms.

Options are the one case where loading everything is a poor default: Gate.io
lists thousands of option contracts. `options_underlyings` restricts the load
(for example `("BTC_USDT",)`), and is ignored unless `OPT` is configured.

A product whose wallet has never been provisioned is skipped with a warning
rather than aborting start-up, because Gate.io creates the futures, delivery and
options wallets on the first internal transfer into them, and reports
`USER_NOT_FOUND` until then. For instrument loading this is *implemented and
mock-tested* (`tests/test_providers.py`); the execution client applies the same
rule when it reads that product's wallet.

## Account model

Gate.io keeps a separate wallet per product, and — on spot — several ledgers
that all trade through the same order endpoints. Two configuration decisions
follow from that: how the Nautilus account is typed, and which ledger a spot
order names.

### One Nautilus account

The execution client aggregates the wallets of the enabled products into a
single Nautilus account, `GATE_IO-master`, with `OmsType.NETTING`. Funds do not
move between Gate.io wallets implicitly: USDT in the spot wallet cannot margin a
futures position until it is transferred. The client logs that warning at
connect and exposes `transfer()` for the internal move; see
[execution.md](execution.md).

The account type is not configured directly. It is derived:

| Configuration                                              | Nautilus `AccountType` |
|------------------------------------------------------------|------------------------|
| `products == (SPOT,)` and `spot_account_mode=SPOT`         | `CASH`                 |
| any margin spot mode (`MARGIN`, `CROSS_MARGIN`, `UNIFIED`) | `MARGIN`               |
| any derivative product configured, alone or with spot      | `MARGIN`               |

That derivation is *implemented and mock-tested* (`tests/test_factories.py`).

### Which ledger a spot order names

`spot_account_mode` sets the `account` field Gate.io reads on spot requests.
Margin on Gate.io is not a separate market: the same `/spot/orders` endpoints
serve every ledger, and the field is what selects one.

| `spot_account_mode` | Regular spot orders | Price-triggered spot orders |
|---------------------|---------------------|-----------------------------|
| `SPOT` (default)    | `spot`              | `normal`                    |
| `MARGIN` (isolated) | `margin`            | `margin`                    |
| `CROSS_MARGIN`      | `cross_margin`      | not expressible             |
| `UNIFIED`           | `unified`           | `unified`                   |

The asymmetry in the right-hand column is Gate.io's, not the adapter's: a
price-triggered spot order says `normal` where a regular order says `spot`, and
the price-order schema has no cross-margin value at all. Rather than silently
downgrade such an order to a different ledger, the client rejects it with an
explicit reason. Regular spot orders and the plain-spot price-order encoding are
*implemented and mock-tested*; the margin and unified encodings are
*implemented, mainnet validation pending*.

Two boundaries are easy to trip over:

* The mode only takes effect when `SPOT` is among `products`. With, say,
  `products=(PERP,)` and `spot_account_mode=UNIFIED`, no spot order is ever sent
  and no unified ledger is read; the only visible effect is that the account is
  typed `MARGIN`.
* The margin modes require the corresponding account type to exist on Gate.io.
  Nothing in the configuration provisions one.

### Classic versus Unified account

Gate.io accounts are in `classic` mode until the owner upgrades them; the
upgraded modes are `single_currency`, `multi_currency` and `portfolio`. The
distinction matters here for one concrete reason: a Unified Account reports a
single cross-product balance per currency that already contains the spot and
derivative wallets, while every one of those wallets keeps answering its own
endpoint with the same funds.

So the aggregation rule differs by mode, and the adapter implements both:

* **Classic** — the per-product wallets are summed.
* **Unified** — a currency reported by the unified ledger *replaces* the
  per-product wallets for that currency instead of being added to them. Summing
  would multiply the account's equity by the number of enabled products.

The unified ledger is what makes the second rule possible: it is the only
statement that names the currencies whose per-product wallets are echoes. A poll
that cannot read it therefore publishes nothing rather than fall back to summing,
because falling back is exactly the arithmetic that doubles the account.

This is *implemented and mock-tested*, including the case of a wallet stream
update arriving after the aggregate was built (`tests/test_execution_events.py`)
and the case of the unified ledger failing mid-session
(`tests/test_execution_accounting.py`).

The adapter never changes the account's mode. `GET /unified/unified_mode` and
`PUT /unified/unified_mode` exist in the REST namespace
(`GateioMarginHttpAPI.unified_account_mode` and `set_unified_account_mode`) for
deliberate use by an operator, and neither client calls them: switching how a
whole account is margined should never be a side effect of connecting a trading
client. There is likewise no automatic mode probe at start-up — the mode you
configure is the mode that is used.

That means a mismatch is your responsibility to avoid, and it fails in a
specific way. With `spot_account_mode=UNIFIED` against an account still in
classic mode, the unified ledger read fails with
`INVALID_UNIFIED_ACCOUNT` / `UNIFIED_ACCOUNT_NOT_ACTIVATED`, which the adapter
translates into a "wallet not provisioned" warning naming the remedy — and the
whole spot wallet is skipped for that refresh, so the account reports no spot
balance at all. Orders sent with `account="unified"` from a classic account are
rejected by the venue. Configure `UNIFIED` only for an account that is actually
unified.

### Borrowing

The margin ledgers borrow against collateral, which is what distinguishes them
from a cash trade, and the REST namespace exposes the borrow and repay endpoints
for isolated margin (`POST /margin/uni/loans`) and for the unified account
(`POST /unified/loans`). Every method that can create a liability says so in its
own docstring.

Neither client borrows on your behalf. No code path in the data or execution
client calls a borrow, repay, auto-repay or leverage endpoint. Debt therefore
arises only if you call one of those endpoints yourself, or if the ledger you
selected borrows on the venue side while filling an order — that second case is
a property of the Gate.io account's own settings, not of this adapter, and it
has not been exercised here.

Status, stated exactly: the borrow and repay calls are *implemented, mainnet
validation pending*. The transport's refusal to replay a borrow after an
ambiguous failure — the property that matters most, since a replayed borrow can
draw the loan twice — is *implemented and mock-tested*
(`tests/test_http_namespaces.py`).

The unified-account path cannot be exercised at all below the venue-side
thresholds Gate.io places on the account modes that permit borrowing: only
`multi_currency` and `portfolio` accounts may borrow, and reaching either mode
requires the account to hold more than a minimum balance the venue sets. Those
thresholds are Gate.io's policy; this package neither enforces nor verifies
them, and the figures recorded in [validation.md](validation.md) come from the
venue's documentation rather than from anything measured here. The consequence
for this page is simple: the unified borrowing path is implemented and
unvalidated, and no claim about how the venue behaves on it should be read into
its presence.

### Position mode

The client trades one-way (netted) positions, because NautilusTrader nets
positions per instrument and a venue holding a separate long and short leg for
the same contract cannot be reconciled against that model. A perpetual-futures
account in hedge (dual) position mode is detected at connect and refused with an
explanatory error naming the venue-side change required; the adapter does not
switch the setting itself.

Hedge mode as such is *unsupported*. The refusal is *implemented, mainnet
validation pending*, and — unlike most of the behavior on this page — it is not
covered by a unit test, so treat the exact message and the delivery-futures and
options exemption (neither has a hedge mode) as read from the source rather than
demonstrated.

## Validation helpers

Being frozen does not rule out `__post_init__`: `msgspec` runs it after the
fields are set but before the struct is handed back, which is why the per-field
bounds above are enforced there and hold on both doors. What it does rule out is
*changing* a value from there, so `__post_init__` can refuse but never correct.

Cross-field validation is a separate matter, and it runs in the client
constructors rather than on the struct — not for want of a hook, but because
the fields it compares live in different places: `products` against
`environment`, and `spot_account_mode` against `products`. It raises
`ValueError` with an explicit message before any network activity. The same
functions are public, so a configuration can be checked up front:

```python
from nautilus_gateio.config import (
    validate_book_interval_ms,
    validate_products,
    validate_snapshot_limit,
)

validate_products(config.products, config.environment)  # -> de-duplicated tuple
validate_book_interval_ms(config.order_book_update_interval_ms)
validate_snapshot_limit(config.order_book_snapshot_limit)
```

What each client validates on construction:

| Client    | Checks                                                                                          |
|-----------|-------------------------------------------------------------------------------------------------|
| Data      | product set (non-empty, real members, served by the environment), book interval, snapshot depth |
| Execution | product set only                                                                                |

The execution client does not cross-check `spot_account_mode` against
`products`, for the reason given above: a mode that names no configured ledger
is inert rather than wrong.

Module-level constants worth knowing:

| Constant                                      | Value                                              |
|-----------------------------------------------|----------------------------------------------------|
| `config.TESTNET_PRODUCTS`                     | `(GateioProductType.SPOT, GateioProductType.PERP)` |
| `config.ORDER_BOOK_UPDATE_INTERVALS_MS`       | `(20, 100, 1000)`                                  |
| `common.constants.ORDER_BOOK_SNAPSHOT_LIMITS` | `(1, 5, 10, 20, 50, 100)`                          |

## What is not configurable

Some transport behavior is fixed in this release, and knowing which is part of
configuring the adapter honestly:

* **Request pacing.** The shared REST transport paces itself at 8 requests per
  second and backs off on HTTP 429. There is no configuration field for it.
* **The submission-deadline header.** Gate.io accepts an `x-gate-exptime` header
  bounding how late a delayed order may still be accepted. The transport
  supports it and withholds it until the venue clock offset has been measured by
  an explicit `sync_time()` call, because an unsynchronized clock would expire
  valid requests. Neither client makes that call, so in the default
  configuration the header is not sent. The header logic itself is *implemented
  and mock-tested* (`tests/test_http_client.py`).
* **Per-endpoint rate-limit budgets**, request-weight accounting and local order
  throttling are *unsupported*: none is implemented, and none is planned for
  this release.

## Safety model

Be explicit about what the adapter does and does not do:

* `environment` defaults to `"mainnet"` on **both** clients.
* There is **no local order kill switch**. Version 0.1.0 had a flag on the HTTP
  client that refused order-mutating calls; it was removed. A flag inside the
  process is not a security boundary — the process holds the key either way —
  and its presence encouraged treating "the code will stop me" as a guarantee.
* The controls that do bind, in order of strength:
  1. **API key permissions** on the Gate.io side. Create a key with only the
     permissions the strategy needs, and never grant withdrawal permission to a
     trading key. This adapter implements no withdrawal endpoint at all, but the
     key does not know that.
  2. **IP allowlisting** on the key.
  3. `environment="testnet"` for rehearsal, which covers spot and USDT
     perpetuals.
  4. NautilusTrader's own backtest and sandbox execution for simulation.
* The adapter never silently alters an order's meaning. Anything Gate.io cannot
  express is rejected with a stated reason — see [execution.md](execution.md).
* The adapter never changes a venue-side account setting: hedge position mode is
  refused rather than switched, the unified account mode is never upgraded, and
  no automatic borrowing is performed.

Live validation reaches spot, one USDT perpetual and one option contract (see
[validation.md](validation.md)); everything else on this page is offline
evidence. [troubleshooting.md](troubleshooting.md) covers what the common
start-up failures mean.

## Worked example

The two clients are configured independently, so `environment` has to be stated
on each of them. Leaving it off one client is how a node ends up trading in one
environment while watching prices from another.

```python
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import TradingNodeConfig

from nautilus_gateio import (
    GATEIO,
    GateioDataClientConfig,
    GateioExecClientConfig,
    GateioProductType,
    GateioSpotAccountMode,
)

# A rehearsal node: both clients on the testnet, which serves spot and USDT
# perpetuals. Drop the two `environment` arguments and this trades live.
config = TradingNodeConfig(
    trader_id="EXAMPLE-001",
    data_clients={
        GATEIO: GateioDataClientConfig(
            environment="testnet",
            products=(GateioProductType.SPOT, GateioProductType.PERP),
            instrument_provider=InstrumentProviderConfig(
                load_ids=frozenset(["BTC_USDT.GATE_IO", "BTC_USDT-PERP.GATE_IO"]),
            ),
            order_book_update_interval_ms=100,
            order_book_snapshot_limit=100,
        ),
    },
    exec_clients={
        GATEIO: GateioExecClientConfig(
            environment="testnet",
            products=(GateioProductType.SPOT,),
            spot_account_mode=GateioSpotAccountMode.SPOT,
        ),
    },
)
```

Credentials are left to the environment here, which is the usual arrangement:
`GATE_TESTNET_API_KEY` and `GATE_TESTNET_API_SECRET` for the configuration
above. Runnable scripts, including one that places and cancels a single testnet
order behind an explicit opt-in, are in [the examples](../examples/README.md).

## Registering from a declarative config

A node config can carry the client entries itself, which is how a pip-installed
adapter is wired in without being imported in your own code.

```python
from nautilus_trader.common.config import ImportableConfig, ImportableFactoryConfig

config = TradingNodeConfig(
    trader_id="GATEIO-001",
    data_clients={
        "GATE_IO": ImportableConfig(
            path="nautilus_gateio.config:GateioDataClientConfig",
            config={"products": ["SPOT", "PERP"], "instrument_provider": {"load_all": True}},
            factory=ImportableFactoryConfig(
                path="nautilus_gateio.factories:GateioLiveDataClientFactory",
            ),
        ),
    },
    exec_clients={
        "GATE_IO": ImportableConfig(
            path="nautilus_gateio.config:GateioExecClientConfig",
            config={"environment": "testnet", "products": ["SPOT"], "spot_account_mode": "spot"},
        ),
    },
)

node = TradingNode(config=config)
node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
node.build()
```

The execution factory has to be registered in Python. Giving the exec entry a
`factory=ImportableFactoryConfig(...)` makes `node.build()` raise
`AttributeError: 'GateioLiveExecClientFactory' object has no attribute
'__name__'`: `nautilus_trader/live/node_builder.py::TradingNodeBuilder.build_exec_clients`
reads `__name__` off the factory object to recognize the sandbox factory, and
`ImportableFactoryConfig.create()` hands it an instance rather than the class.
Registering the factory first makes the builder skip that construction. The
data-client path carries no such check.

`products` takes the enum names (`"SPOT"`, `"PERP"`, `"INVERSE"`, `"FUT"`,
`"OPT"`, uppercase) and `spot_account_mode` takes the venue's own strings
(`"spot"`, `"margin"`, `"cross_margin"`, `"unified"`, lowercase); `"SPOT"` there
raises `msgspec.ValidationError`. And `instrument_provider.load_ids` cannot be
given from a declarative config at all, because `ImportableConfig.create()`
decodes without a hook: string ids raise ``msgspec.ValidationError: Expected
`nautilus_trader.model.identifiers.InstrumentId`, got `str` - at
`$.instrument_provider.load_ids[0]` ``. Use `load_all` with `filters`, or build
the config in Python.

All four behaviors were verified against `nautilus_trader` 1.230.0 on Python
3.13.
