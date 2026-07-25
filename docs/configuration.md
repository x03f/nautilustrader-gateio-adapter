# Configuration

All configuration classes live in `nautilus_gateio.config`. They are frozen
(immutable) Nautilus config classes and can be embedded directly in a
`TradingNodeConfig`.

## GateioDataClientConfig

Extends `nautilus_trader.live.config.LiveDataClientConfig` (so the standard
fields such as `instrument_provider` are also available).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `venue` | `str` | `"GATEIO"` | Venue string used in instrument ids (`BTC_USDT.GATEIO`) |
| `base_url_http` | `str` | `"https://api.gateio.ws"` | REST endpoint for market data. Public data is always served from mainnet — Gate.io has no public spot testnet market-data feed |
| `base_url_ws` | `str` | `"wss://api.gateio.ws/ws/v4/"` | WebSocket endpoint for market data |
| `use_websocket` | `bool` | `True` | WebSocket is the primary transport; `False` forces REST polling only |
| `poll_interval_secs` | `float` | `5.0` | REST polling cadence for the fallback transport |
| `emit_synthetic_quotes` | `bool` | `True` | Emit a synthetic `QuoteTick` around each closed bar (close +/- 0.5 bp, unit sizes). Exists for quote-driven fill simulations; **not** real quotes — see [market-data.md](market-data.md) |

## GateioExecClientConfig

Extends `nautilus_trader.live.config.LiveExecClientConfig`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `api_key` | `str \| None` | `None` | API key; `None` reads environment variables (see below) |
| `api_secret` | `str \| None` | `None` | API secret; `None` reads environment variables |
| `environment` | `str` | `"testnet"` | `"testnet"` or `"mainnet"`. Anything other than `"mainnet"` (case-insensitive) is treated as testnet |
| `base_url_http` | `str \| None` | `None` | Explicit REST endpoint override; when `None` it derives from `environment` via `resolve_base_url()` |
| `venue` | `str` | `"GATEIO"` | Venue string |
| `account_poll_interval_secs` | `float` | `5.0` | Cadence of the fill/balance polling loop (no private WebSocket yet — fills are detected via REST polling) |
| `client_order_id_tag` | `str` | `"ng"` | Short tag embedded in generated Gate.io `text` client order ids |

Helpers on the class:

* `is_testnet` (property) — `True` unless `environment.lower() == "mainnet"`.
* `resolve_base_url()` — returns `base_url_http` if set, otherwise the
  testnet or mainnet host based on `environment`.

## GateioPaperConfig

Configuration for the local paper-fill simulator (`PaperExecution`). No
exchange orders are ever placed.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `starting_balances` | `dict[str, float] \| None` | `None` | Virtual starting balances (`None` means `{"USDT": 10_000.0}`) |
| `taker_fee` | `float` | `0.0016` | Simulated taker fee rate |
| `maker_fee` | `float` | `0.00075` | Simulated maker fee rate |

## Environment variables

| Variable | Used for |
|---|---|
| `GATE_API_KEY` | Mainnet API key (also the fallback for testnet) |
| `GATE_API_SECRET` | Mainnet API secret (also the fallback for testnet) |
| `GATE_TESTNET_API_KEY` | Testnet API key |
| `GATE_TESTNET_API_SECRET` | Testnet API secret |

## Credential resolution order

Credentials are resolved by `resolve_credentials(api_key, api_secret, testnet)`
at client-creation time:

1. Explicit `api_key` / `api_secret` config values, when not `None`.
2. Otherwise, from the environment:
   * testnet: `GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET`, falling
     back to `GATE_API_KEY` / `GATE_API_SECRET` if unset;
   * mainnet: `GATE_API_KEY` / `GATE_API_SECRET`.
3. Missing values resolve to empty strings — public market data still works
   without credentials; private calls raise a clear error.

All resolved values are stripped of surrounding whitespace, so keys pasted
with trailing newlines do not break request signing.

## Endpoints: testnet vs. mainnet

| Purpose | Mainnet | Testnet |
|---|---|---|
| REST | `https://api.gateio.ws` | `https://api-testnet.gateapi.io` |
| WebSocket (spot market data) | `wss://api.gateio.ws/ws/v4/` | — (no public spot testnet market data) |

Notes:

* Public spot market data (bars, tickers, order books) is only available on
  mainnet. The data client therefore defaults to mainnet endpoints even when
  execution targets testnet — this is the intended pairing.
* The testnet host serves authenticated spot endpoints for Gate.io testnet
  accounts, and the (fully public) futures testnet.

## Safety defaults

The adapter uses a layered opt-in model — each layer must be crossed
explicitly, and credentials alone never enable order flow:

1. **Execution defaults to testnet.** `GateioExecClientConfig` targets the
   Gate.io testnet host unless you set `environment="mainnet"`.
2. **The `live_orders` switch.** `GateioHttpClient` refuses order-mutating
   calls (`place_order`, `cancel_order`, ...) unless constructed with
   `live_orders=True`; otherwise they raise `LiveOrdersDisabledError`. The
   execution client sets this flag itself, because wiring it into a
   `TradingNode` *is* the explicit opt-in — but any `GateioHttpClient` you
   construct directly is read-only by default.
3. **Examples are gated.** The order-placing example
   (`examples/06_testnet_orders.py`) refuses to run unless
   `GATEIO_ALLOW_ORDERS=YES` is set, hard-codes the testnet host (not
   overridable by environment), requires testnet credentials, and caps the
   order notional.
