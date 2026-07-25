# Configuration

Every configuration class lives in `nautilus_gateio.config`. They are frozen
`msgspec` structs extending the standard NautilusTrader live-client configs, so
they can be embedded directly in a `TradingNodeConfig`.

The tables below are the complete field lists, taken from the class definitions
in `nautilus_gateio/config.py`. Fields inherited from `LiveDataClientConfig` /
`LiveExecClientConfig` (such as `instrument_provider`, `handle_revised_bars`,
`reconciliation` and `routing`) behave exactly as they do for any other
NautilusTrader adapter and are documented upstream.

## Environments

```python
from nautilus_gateio.config import MAINNET, TESTNET

MAINNET  # "mainnet"
TESTNET  # "testnet"
```

`environment` selects the REST host and the WebSocket hosts:

| `environment` | REST base URL |
|---|---|
| `"mainnet"` (default, both clients) | `https://api.gateio.ws` |
| `"testnet"` | `https://api-testnet.gateapi.io` |

**Execution defaults to `"mainnet"`.** This is deliberate, and it changed in
0.2.0: an execution client that silently pointed at a different exchange
environment than the operator believed would be more dangerous than one that
requires the venue to be stated. There is no local order kill switch — see
[Safety model](#safety-model).

Gate.io publishes testnet endpoints for **spot and USDT perpetual futures
only**. Configuring `INVERSE`, `FUT` or `OPT` together with
`environment="testnet"` raises `ValueError` from the client constructor, before
any network activity.

## `GateioDataClientConfig`

Extends `nautilus_trader.live.config.LiveDataClientConfig`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `api_key` | `str \| None` | `None` | API key. `None` reads the environment; public market data needs no credentials |
| `api_secret` | `str \| None` | `None` | API secret. `None` reads the environment |
| `environment` | `str` | `"mainnet"` | `"mainnet"` or `"testnet"` |
| `products` | `tuple[GateioProductType, ...]` | `(GateioProductType.SPOT,)` | Products to load instruments for and open public WebSocket streams on. One client multiplexes every configured product |
| `options_underlyings` | `tuple[str, ...] \| None` | `None` | Restricts option instrument loading to these underlyings, e.g. `("BTC_USDT",)`. Ignored unless `OPT` is configured |
| `base_url_http` | `str \| None` | `None` | Overrides the REST base URL derived from `environment` |
| `base_url_ws` | `str \| None` | `None` | Overrides the WebSocket URL for **every** configured product. Intended for single-product setups or a local aggregating proxy |
| `update_instruments_interval_mins` | `int \| None` | `60` | Interval of the instrument reload task. `None` disables reloading |
| `http_timeout_secs` | `float` | `20.0` | Per-request REST timeout |
| `max_retries` | `int` | `3` | REST attempts for rate-limited or transient server errors |
| `order_book_snapshot_limit` | `int` | `100` | Depth of the REST snapshot seeding each local book. Must be one of `1, 5, 10, 20, 50, 100`. Also the level requested on the WebSocket where the product allows one to be named |
| `order_book_update_interval_ms` | `int` | `100` | Push interval of the incremental depth stream. One of `20`, `100`, `1000`; `20` implies a 20-level stream, and delivery/options accept `100` and `1000` only |
| `bars_timestamp_on_close` | `bool` | `True` | Timestamp bars at the close of their interval (the Nautilus convention). `False` timestamps at the open, matching Gate.io's `t` field |

Helpers on the class:

* `is_testnet` (property) — whether `environment` selects the testnet.
* `resolve_http_url()` — the REST base URL, honouring `base_url_http`.
* `resolve_ws_url(product)` — the WebSocket URL for one product, honouring
  `base_url_ws`.

## `GateioExecClientConfig`

Extends `nautilus_trader.live.config.LiveExecClientConfig`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `api_key` | `str \| None` | `None` | API key. `None` reads the environment; trading always needs credentials |
| `api_secret` | `str \| None` | `None` | API secret. `None` reads the environment |
| `environment` | `str` | `"mainnet"` | `"mainnet"` or `"testnet"`. **Defaults to mainnet** |
| `products` | `tuple[GateioProductType, ...]` | `(GateioProductType.SPOT,)` | Products this client trades. Gate.io keeps a separate wallet per product, so enabling several aggregates several wallets into one Nautilus account |
| `options_underlyings` | `tuple[str, ...] \| None` | `None` | Restricts option instrument loading, as for the data client |
| `base_url_http` | `str \| None` | `None` | Overrides the REST base URL derived from `environment` |
| `base_url_ws` | `str \| None` | `None` | Overrides the private WebSocket URL for every configured product |
| `spot_account_mode` | `GateioSpotAccountMode` | `GateioSpotAccountMode.SPOT` | Ledger spot orders trade against: `SPOT`, `MARGIN` (isolated), `CROSS_MARGIN` or `UNIFIED`. The margin modes require the corresponding account type to be provisioned on Gate.io |
| `client_order_id_tag` | `str` | `"ng"` | Short tag embedded in generated Gate.io `text` client order ids |
| `account_polling_interval_secs` | `float` | `30.0` | Interval of the account state poll backing up the private WebSocket balance stream |
| `max_retries` | `int` | `3` | REST attempts for rate-limited or transient server errors |
| `http_timeout_secs` | `float` | `20.0` | Per-request REST timeout |

Helpers on the class: `is_testnet`, `resolve_http_url()`,
`resolve_ws_url(product)` — identical to the data client's.

## Validation helpers

Both structs are frozen, which rules out custom validation in `__post_init__`
without giving up immutability. Cross-field validation therefore runs in the
client constructors and raises `ValueError` with an explicit message before any
network activity. The same functions are public, so a configuration can be
checked up front:

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

Module-level constants worth knowing:

| Constant | Value |
|---|---|
| `config.TESTNET_PRODUCTS` | `(GateioProductType.SPOT, GateioProductType.PERP)` |
| `config.ORDER_BOOK_UPDATE_INTERVALS_MS` | `(20, 100, 1000)` |
| `common.constants.ORDER_BOOK_SNAPSHOT_LIMITS` | `(1, 5, 10, 20, 50, 100)` |

## Credentials and environment variables

`api_key` / `api_secret` default to `None`, in which case they are resolved from
the environment when the client is created
(`nautilus_gateio.common.credentials.resolve_credentials`):

| Variable | Used when |
|---|---|
| `GATE_API_KEY` / `GATE_API_SECRET` | `environment="mainnet"`, and as the fallback on testnet |
| `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` | `environment="testnet"`, falling back to the mainnet variables |

Resolution order: explicit config values first, then the environment, then empty
strings. Values are stripped of surrounding whitespace, because a key pasted
with a trailing newline otherwise produces signatures the venue silently
rejects. Empty values mean "no credentials", which is a valid state for public
market data. Credentials are never logged; `credentials.mask()` renders a safe
fingerprint for diagnostics.

## Safety model

Be explicit about what the adapter does and does not do:

* `environment` defaults to `"mainnet"` on **both** clients. Set
  `environment="testnet"` if you want the testnet.
* There is **no local order kill switch**. Version 0.1.0 had a switch on the
  HTTP client that refused order-mutating calls; it is gone. A flag inside the
  process is not a security boundary — the process holds the key either way,
  and the flag encouraged treating "the code will stop me" as a guarantee.
* The intended controls are, in order of strength:
  1. **API key permissions** on the Gate.io side — create a key with only the
     permissions the strategy needs, and never grant withdrawal permission to a
     trading key.
  2. **IP allow-listing** on the key.
  3. `environment="testnet"` for rehearsal (spot and USDT perpetuals only).
  4. NautilusTrader's own sandbox and backtest execution for simulation.
* The adapter never silently alters an order's meaning. Anything Gate.io cannot
  express is denied or rejected with a stated reason — see
  [execution.md](execution.md).
* The adapter never changes a venue-side account setting: hedge (dual) position
  mode is detected and refused, not switched, and a unified account mode is
  never upgraded automatically.

## Worked example

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

config = TradingNodeConfig(
    trader_id="EXAMPLE-001",
    data_clients={
        GATEIO: GateioDataClientConfig(
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
            environment="testnet",  # explicit: the default is mainnet
            products=(GateioProductType.SPOT,),
            spot_account_mode=GateioSpotAccountMode.SPOT,
        ),
    },
)
```
