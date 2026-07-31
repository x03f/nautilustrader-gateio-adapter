# Troubleshooting

Real failure modes, and what to do about them.

## The node hangs for a minute and then reports a timeout

```
[WARN] TradingNode: Timed out (60.0s) waiting for engines to connect and initialize
```

That line is the symptom, not the cause. The cause is one `[ERROR] Error on
'_connect'` line above it, and on a first run it is almost always
`MISSING_CREDENTIALS`:

```
[WARN]  ExecClient-GATE_IO: Cannot read /wallet/fee (MISSING_CREDENTIALS); falling back to /spot/fee
[WARN]  ExecClient-GATE_IO: Cannot read the account user id: Gate.io 401 MISSING_CREDENTIALS: ...
[ERROR] ExecClient-GATE_IO: Error on '_connect'
GateioError(Gate.io 401 MISSING_CREDENTIALS: channel spot.orders is private and requires API credentials)
```

Nothing checks at startup that credentials are present, because their absence is
a valid state: public market data needs none, and a data client without a key
runs and looks healthy. An execution client also builds and reaches `READY`, and
then fails at its first signed request. So a variable that is unset, misspelled
or exported in a different shell produces a healthy-looking start, a 60-second
wait, and that timeout.

The error names the pair for the environment the client is configured for —
`GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` on the testnet, the mainnet
pair otherwise. Check them in the process that actually runs the node:

```bash
python -c "import os; print([n for n in ('GATE_API_KEY','GATE_API_SECRET','GATE_TESTNET_API_KEY','GATE_TESTNET_API_SECRET') if os.getenv(n)])"
```

The package reads those four names and no others. `GATEIO_API_KEY` — with the
"IO" — is read by nothing.

The strategy never starts on this path, so no order is constructed and nothing
reaches the venue.

## `INVALID_SIGNATURE` or HTTP 401 on private requests

Two usual causes:

* **Clock drift.** The signature carries a Unix-second timestamp. Call
  `await client.sync_time()` once after constructing `GateioHttpClient`; it
  measures the offset against the venue clock and applies it to every subsequent
  signed request. Enabling NTP on the host is the durable fix.
* **Whitespace in the key or secret.** A value pasted with a trailing newline
  produces a wrong HMAC. `resolve_credentials()` strips whitespace from
  environment-sourced values, but values passed explicitly are used as given.

Also confirm the key pair matches the environment: testnet keys are not valid on
mainnet and vice versa.

## The client is pointing at the wrong exchange environment

`environment` defaults to **`"mainnet"`** on both the data and the execution
client. Version 0.1.0 defaulted execution to testnet, so a configuration carried
over from 0.1.0 that relied on the default now targets mainnet. Set
`environment="testnet"` explicitly if that is what you want.

Symptom: balances come back empty, or orders reference instruments the account
has never traded. Testnet balances exist only on `https://api-testnet.gateapi.io`
and mainnet balances only on `https://api.gateio.ws`.

The testnet account is a separate registration from the mainnet one, made on the
testnet's own site at `https://testnet.gate.com`; its key is issued there. The
API host above is where the adapter sends requests, not where an account is
opened.

## `ValueError: Gate.io has no testnet endpoint for ...`

Raised by the client constructor before any network activity. Gate.io publishes
testnet endpoints for spot and USDT perpetual futures only. Remove `INVERSE`,
`FUT` and `OPT` from `products`, or use `environment="mainnet"`.

## `USER_NOT_FOUND` / `WalletNotProvisionedError`

Gate.io creates the futures, delivery and options wallets on the **first
internal transfer into them**, and answers `USER_NOT_FOUND` until then. Move a
small amount in:

```python
await exec_client.transfer(currency="USDT", from_="spot", to="futures", amount="10", settle="usdt")
```

The adapter treats this as a configuration state, not a failure: it logs a
warning and skips that product rather than refusing to start.

## `INVALID_UNIFIED_ACCOUNT` / "Please open the Unified Account"

The account is not a unified account, or is not in the mode the endpoint needs.
Cross margin and the unified endpoints require the account to be upgraded out of
classic mode, which only the account owner can do. The adapter never changes an
account's mode. Note also the venue's own minimum balances for the richer
unified modes: `multi_currency` needs more than 500 USDT and `portfolio` more
than 1000 USDT — see [products.md](products.md#account-modes-spot-margin-cross-margin-unified).

## `FORBIDDEN`

The API key lacks the permission that ledger or endpoint needs. Grant it in the
key's permission settings — and grant nothing beyond what the strategy uses.
Never give a trading key withdrawal permission.

## `TOO_MANY_REQUESTS` (HTTP 429)

The built-in rate limiter paces requests and backs off on 429, retrying only
requests whose replay is provably safe. If you still see it:

* lower `max_requests_per_second` when constructing `GateioHttpClient`;
* raise `account_polling_interval_secs`;
* reduce the number of clients sharing one API key — the limiter is
  per-instance, not global. The factories already share one transport between
  the data and execution clients of the same configuration.

## `INVALID_CURRENCY_PAIR`, or an instrument that will not load

The symbol form is wrong. Use the canonical instrument ids:

| Product   | Correct form                                         |
|-----------|------------------------------------------------------|
| Spot      | `BTC_USDT.GATE_IO`                                   |
| Perpetual | `BTC_USDT-PERP.GATE_IO`                              |
| Delivery  | `BTC_USDT_YYYYMMDD.GATE_IO`                          |
| Option    | `BTC_USDT-YYYYMMDD-STRIKE-C.GATE_IO`, `-P` for a put |

Dated contracts are listed a few weeks at a time, so an id copied from a page
usually names an expired contract, and `load_ids` on an expired id fails
silently. [`examples/01_public_rest.py`](../examples/01_public_rest.py) prints
what the venue lists today.

Common mistakes: the venue string is `GATE_IO`, **not** `GATEIO` (it changed in
0.2.0); Gate.io pairs use an underscore (`BTC_USDT`, not `BTCUSDT`); and a
perpetual without `-PERP` resolves to the *spot* pair of the same name, which is
a different instrument. See [symbology.md](symbology.md).

## The WebSocket connects but no bars arrive

Not a bug: only **closed** bars are emitted. The first bar of a subscription
arrives when the interval it covers ends — up to a minute for `1-MINUTE`, up to
a day for `1-DAY`. Use a short interval while wiring things up. Delivery and
options publish no window-close flag, so their bars are additionally held until
the next bucket opens plus a short grace period.

## Nothing about the WebSocket appears in the log

The transport logs through the platform, under the component name
`GateioWebSocketClient`, so it is subject to the same configuration as every
other component:

```python
from nautilus_trader.config import LoggingConfig

# On the console:
LoggingConfig(log_level="DEBUG", log_component_levels={"GateioWebSocketClient": "DEBUG"})

# Or quieter on the console and complete in a file:
LoggingConfig(
    log_level="INFO",
    log_level_file="DEBUG",
    log_directory="logs",  # without this the file lands in the working directory
    log_component_levels={"GateioWebSocketClient": "DEBUG"},
)
```

`log_component_levels` raises a component within a sink; it cannot lift output
past the sink's own level, so `log_level="INFO"` keeps the transport's DEBUG
lines off the console whatever the component map says.

At `DEBUG` the transport reports each connection, each heartbeat failure and
each acknowledgement it could not match. Two things to know when reading it:
`log_components_only=True` suppresses every component not named in
`log_component_levels`, and a `Logger` built before the logging subsystem is
initialized discards its messages, which is what happens when the transport is
used standalone outside a `TradingNode`.

## The order book keeps resynchronizing

A gap in the incremental stream forces a REST re-snapshot. Occasional gaps are
normal; a constant stream of them usually means the snapshot depth and the
stream depth disagree. `order_book_snapshot_limit` must match what the stream
serves for the configured `order_book_update_interval_ms`
(`GateioPublicWebSocket.effective_depth()` reports it), and delivery and options
accept `100` and `1000` ms only.

## An order was rejected instead of being adjusted

That is deliberate. Anything Gate.io cannot express without changing the order's
meaning is rejected with the reason on the event, never silently altered. The
full list of rejection cases is in
[execution.md](execution.md#nothing-is-silently-altered). The most common ones:

* `reduce_only` on a spot order — reduce-only is a derivatives concept;
* `quote_quantity=True` anywhere but a spot market buy;
* a fractional quantity on a contract product — contracts are whole;
* FOK on an options order;
* a price-triggered order on options, or on a cross-margin spot ledger.

## An order sits in `SUBMITTED`, `PENDING_CANCEL` or `PENDING_UPDATE`

Look for a warning naming the order and ending in `is unresolved`. Gate.io did
not answer the command, so the client cannot say whether it was applied and does
not guess: it leaves the order in flight, which is the state NautilusTrader's
in-flight check reads. That check queries the venue every
`inflight_check_interval_ms`, and after `inflight_check_retries` unanswered
queries resolves the order itself — a `SUBMITTED` order to `REJECTED`, and (in
1.230.0) a `PENDING_CANCEL` or `PENDING_UPDATE` order to `CANCELED`. The
reasoning behind leaving it to the engine is in
[execution.md](execution.md#unknown-outcomes).

If such orders stay in flight:

* the engine's in-flight check must be running — it is on by default, but
  `inflight_check_interval_ms=0` disables it;
* a submission is queryable by client order id on every product, with no venue
  order id, so an unanswered submit resolves by lookup rather than by timeout.
  If the lookup keeps coming back empty, Gate.io really is not holding the order:
  look for `holds no ... order for` in the log, naming the client order id and
  the symbol that were searched;
* enabling `open_check_interval_secs` adds the open-order poll, a second source
  of truth for orders whose state the in-flight query could not settle.

Never resubmit such an order by hand before the venue state is known. That is
the one action the whole policy exists to prevent.

## `OrderDenied` instead of the order you submitted

`OrderDenied` means this adapter refused the order and sent nothing; the reason
on the event says which instruction it could not express. `OrderRejected` would
mean Gate.io refused it. The full list of refusals is
[products.md](products.md#what-is-refused-rather-than-translated); the common
ones are:

* `... is not supported by Gate.io` — the order type is outside
  `SUPPORTED_ORDER_TYPES` (MARKET, LIMIT, STOP_MARKET, STOP_LIMIT,
  MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED). Trailing stops and the other types have
  no Gate.io equivalent; implement them at the strategy level.
* `time in force ... is not supported` — Gate.io has `gtc`, `ioc`, `poc` and
  `fok` and nothing else. A downgrade would change the execution guarantee, so
  the order is refused instead.
* `post-only cannot be combined with ...` — post-only *is* Gate.io's `poc`, a
  maker-only order that rests, so it cannot also be immediate.
* `... is not a multiple of the ... tick size` — the price is off the venue's
  grid. `make_price()` rounds to the precision, not to the tick; use the
  instrument's `next_bid_price()` / `next_ask_price()`.

A denial is terminal and nothing was sent, so there is nothing at the venue to
reconcile: fix the instruction and submit a new order.

## "instrument not found" when subscribing or submitting

The instrument was never loaded. Configure the node's instrument provider with
the ids you trade:

```python
InstrumentProviderConfig(load_ids=frozenset(["BTC_USDT-PERP.GATE_IO"]))
```

Prefer explicit ids over loading everything — Gate.io lists thousands of spot
pairs and option contracts. For options, restrict with `options_underlyings`.

## An instrument is missing from a load that otherwise succeeded

Instruments that cannot be traded normally are not published: spot pairs the
venue reports as untradable or one-sided, delisting or inactive contracts,
expired delivery contracts, and options outside an active expiration.

A pair whose price scale the running NautilusTrader build cannot represent is
also rejected, with a warning naming it. Standard builds carry 9 decimal places;
a handful of Gate.io pairs quote up to 14. Publishing them anyway would mean
publishing zeroes as if they were venue prices.

## An import that used to work now fails

Version 0.2.0 removed several 0.1.0 modules and renamed the venue. See the
[migration guide](migration-0.1-to-0.2.md) for the complete list.

## Installation fails on version constraints

The package needs Python >= 3.12, < 3.15 and `nautilus_trader >= 1.230.0, < 2`:

```bash
python --version
python -c "import nautilus_trader; print(nautilus_trader.__version__)"
```

Install into a fresh virtual environment if either is out of range.
