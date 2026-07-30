# Products

Gate.io is five venues behind one API key. Each product family has its own REST
namespace, its own WebSocket host and — the part that catches people out — its
own **wallet**. The adapter models that directly instead of hiding it:
`GateioProductType` is part of an instrument's identity, one data client
multiplexes every configured product, and one execution client aggregates the
wallets of the configured products into a single Nautilus account.

This page is the one to read before committing to the adapter. It states, per
product, what is supported, what is refused, and what the adapter refuses to
translate. Everything here was checked against the code and the test suite; the
[execution](execution.md) and [market data](market-data.md) pages explain the
mechanisms in more depth.

Two things to have in mind while reading it. This is an external community
package for NautilusTrader 1.230.0, written in pure Python and not an official
integration ([why](architecture.md#the-deliberate-python-only-architecture)). And
it is an alpha release, with live validation that reaches spot, one USDT
perpetual and one option contract and stops there
([validation status](validation.md)).

```python
from nautilus_gateio import GateioProductType, GateioSpotAccountMode

GateioProductType.SPOT      # spot pairs
GateioProductType.PERP      # USDT-margined perpetual futures (linear)
GateioProductType.INVERSE   # coin-margined perpetual futures (settle=btc)
GateioProductType.FUT       # USDT-margined delivery (dated) futures
GateioProductType.OPT       # USDT-settled options
```

Spot **margin** is not a product: it is a choice of ledger on spot instruments,
selected with `spot_account_mode` and expressed as `GateioSpotAccountMode`. See
[Account modes](#account-modes-spot-margin-cross-margin-unified).

## How to read the status column

This is alpha software. The statuses below describe how well a claim is grounded
in the repository — not how well it has survived contact with real money.

Live exercise is graded separately, and only on one page:
[validation status](validation.md). In short: the spot market-data paths are
confirmed on mainnet, as are the instrument load on every configured product and
the ticker-derived streams on the USDT perpetual; on the execution side, **spot,
one USDT perpetual and one option contract** — on the perpetual both position
sides, the reduce-only flag and its refusal, conditional orders armed, canceled
and re-armed without ever firing, and a position read back from the venue by a
node that did not open it; on the option a resting limit buy, an aggressive one
that filled, and a covered limit sell. No order has been sent to Gate.io for an
inverse perpetual or a delivery contract, and no margin, cross-margin or unified
spot ledger has carried one. A row below saying *implemented and mock-tested* is
making no claim whatsoever about the venue.

| Status                                  | Meaning here                                                                                                                             |
|-----------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------|
| Implemented and mock-tested             | The behavior is asserted by the test suite against stubbed venue responses or recorded payload shapes                                    |
| Implemented, mainnet validation pending | The code path exists and was read, but no test asserts this specific behavior                                                            |
| Experimental                            | Reachable through the lower-level HTTP or WebSocket classes, deliberately not wired into the NautilusTrader data or execution interfaces |
| Unsupported                             | The adapter refuses it, or no code exists for it                                                                                         |
| Not applicable                          | Gate.io does not have the concept for this product                                                                                       |

*Implemented and mock-tested* is a statement about the adapter agreeing with its
authors, never about Gate.io agreeing with the adapter. Only a live round trip
settles that, and the ones on record are listed, product by product, in
[validation status](validation.md).

The second label carries its historical name and is graded on the same axis as
the first: *implemented, mainnet validation pending* means no test asserts the
behavior, not that the venue has been asked and has not answered. A spot row
carrying it may still appear as mainnet-confirmed on the validation page, which
is the only page that grades live evidence.

## Products at a glance

| Product             | `GateioProductType` | Instrument class                      | REST namespace                               | WebSocket endpoint                          | Testnet |
|---------------------|---------------------|---------------------------------------|----------------------------------------------|---------------------------------------------|---------|
| Spot                | `SPOT`              | `CurrencyPair`                        | `/spot` (`/margin`, `/unified` for balances) | `wss://api.gateio.ws/ws/v4/`                | ✓       |
| Perpetual (linear)  | `PERP`              | `CryptoPerpetual`                     | `/futures/usdt`                              | `wss://fx-ws.gateio.ws/v4/ws/usdt`          | ✓       |
| Perpetual (inverse) | `INVERSE`           | `CryptoPerpetual` (`is_inverse=True`) | `/futures/btc`                               | `wss://fx-ws.gateio.ws/v4/ws/btc`           | -       |
| Delivery future     | `FUT`               | `CryptoFuture`                        | `/delivery/usdt`                             | `wss://fx-ws.gateio.ws/v4/ws/delivery/usdt` | -       |
| Option              | `OPT`               | `CryptoOption`                        | `/options`                                   | `wss://op-ws.gateio.live/v4/ws`             | -       |

Product support as a whole is *implemented and mock-tested*: the instrument
parsers, the per-product REST routing and the per-product order book tables all
have tests. Nothing beyond that is implied.

Configuring a product Gate.io does not serve on the testnet together with
`environment="testnet"` raises `ValueError` from the client constructor, before
any network activity. Only spot and USDT perpetuals have testnet endpoints.

The options endpoint above is the default the adapter uses; the settle-suffixed
variants (`.../v4/ws/usdt`, `.../v4/ws/btc`) exist as constants and can be
selected through `base_url_ws`. Some Gate.io documentation pages name an
`op-ws.gateio.ws` host instead; this project's endpoint table records that host
as not resolving, an observation that is not re-verified here.

## Instrument and quantity semantics

The single most important difference between products is what a `Quantity`
means. Getting this wrong is how an order becomes a hundred times larger than
intended.

| Product             | Instrument id                       | `Quantity` is                      | `size_precision`        | Notes                                                                                                                                                                                                                                                       |
|---------------------|-------------------------------------|------------------------------------|-------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Spot                | `BTC_USDT.GATE_IO`                  | an amount of the **base** currency | from `amount_precision` | Except a market buy — see below                                                                                                                                                                                                                             |
| Perpetual (linear)  | `BTC_USDT-PERP.GATE_IO`             | a **number of contracts**          | `0`                     | `multiplier` is the venue's `quanto_multiplier`, so `notional = contracts x multiplier x price`                                                                                                                                                             |
| Perpetual (inverse) | `BTC_USD-PERP.GATE_IO`              | a **number of contracts**          | `0`                     | Settles in the base currency; a `USD` quote is what marks a contract inverse. Gate.io sends `quanto_multiplier: "0"` here, so the face value falls back to one unit of the quote currency, and a contract publishing anything else is loaded with a warning |
| Delivery future     | `BTC_USDT_20260807.GATE_IO`         | a **number of contracts**          | `0`                     | Expiry is in the symbol; `expiration_ns` comes from the contract's `expire_time`, and `activation_ns` from its `create_time` where the payload carries one, otherwise `0`                                                                                   |
| Option              | `BTC_USDT-20260729-70000-C.GATE_IO` | a **number of contracts**          | `0`                     | European, USDT-settled; premium is `price x multiplier x size`                                                                                                                                                                                              |

Because every derivative has `size_precision = 0`, a fractional contract
quantity cannot be expressed. The adapter rejects it rather than truncating it,
on submission and on amendment alike (*implemented and mock-tested*).

### Price grids and tick schemes

`price_precision` says how many decimals a price may carry; it does **not** say
which of those prices the venue accepts. Gate.io publishes the real grid as
`order_price_round`, and for three of its ~3,100 instruments that is not a power
of ten: the `BNB_USDT` perpetual and the `ETH_USDT_20260925` and
`ETH_USDT_20261225` delivery contracts quote two decimals but tick in `0.05`.

Every instrument the adapter builds therefore carries a `tick_scheme_name`
(*implemented and mock-tested*):

| Grid                             | Scheme                                                                                                                |
|----------------------------------|-----------------------------------------------------------------------------------------------------------------------|
| A power of ten (everything else) | NautilusTrader's pre-registered `FIXED_PRECISION_{n}`                                                                 |
| Anything else                    | a `FixedTickScheme` registered by the adapter as `GATEIO_TICK_{increment}_P{precision}`, carrying the venue increment |

Use `instrument.next_bid_price()` / `next_ask_price()` to produce an order price.
`instrument.make_price()` rounds to the precision only, so on an off-decimal grid
it can return a price the venue refuses; such a price is rejected by the adapter
before the request rather than sent (see [what is refused rather than
translated](#what-is-refused-rather-than-translated)).

Instruments the venue reports as untradable are not published at all: spot pairs
marked `untradable` or currently one-sided around a listing or delisting window,
futures and delivery contracts that are delisting or inactive, and expired
delivery contracts. A one-sided spot pair is withheld because `CurrencyPair`
cannot express "buys only", and publishing it would let a strategy send the
disallowed side and collect an opaque venue rejection.

Gate.io lists thousands of option contracts. Restrict loading with
`options_underlyings=("BTC_USDT",)` unless you genuinely want all of them.

### Margin and fee rates

| Field                                    | Source                                                                   | What it means here                                                                                                                                                                                                                                                                                 |
|------------------------------------------|--------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `margin_init` (contracts)                | not venue-derived                                                        | Fixed at `1`. Gate.io publishes no initial-margin rate — `leverage_max` is a cap, not a requirement — and NautilusTrader's default `LeveragedMarginModel` computes `notional / leverage x margin_init`, so `1` reserves exactly what the venue reserves at whatever leverage the account is set to |
| `margin_maint` (contracts)               | `maintenance_rate`                                                       | The **first** risk-limit tier's rate. Larger positions fall into higher tiers; reconcile the real figure from the position's `average_maintenance_rate`                                                                                                                                            |
| `margin_init` / `margin_maint` (spot)    | —                                                                        | `0`. Correct for a cash ledger; see [account modes](#account-modes-spot-margin-cross-margin-unified) for the margin ledgers, where it is a known gap                                                                                                                                               |
| `margin_init` / `margin_maint` (options) | —                                                                        | `0`. Gate.io's option coefficients (`init_margin_high`, `init_margin_low`, `maint_margin_base`) are ratios of the **underlying** price applied to short positions, not of the premium notional, so they are not Nautilus margin ratios. They remain in `info`                                      |
| `maker_fee` / `taker_fee`                | `maker_fee_rate` / `taker_fee_rate`, or the pair's `fee` percent on spot | Fractions. Negative on every perpetual and delivery contract — a maker rebate — and the sign is carried through                                                                                                                                                                                    |

One asymmetry is worth stating plainly, because the error is silent and grows
with leverage. Gate.io divides **initial** margin by leverage and does **not**
divide maintenance margin by it; `LeveragedMarginModel` divides both. The initial
figure is therefore exact at any leverage, and the maintenance figure is the
venue's requirement divided by the account leverage — at 50x it understates
liquidation risk fifty-fold. Switching to `StandardMarginModel` corrects
maintenance and breaks initial by the same factor, so neither shipped model fits
this venue. `MarginModel` is a public base class and `MarginAccount.set_margin_model()`
accepts one, so a venue-shaped model is the complete answer; note that
`MarginModelConfig` reaches only the backtest engine in NautilusTrader 1.230.0,
so a live system must set it programmatically. Until then, read the framework's
maintenance figure as advisory and the venue's as authoritative.

None of these rates is ever assumed. Gate.io publishes `maintenance_rate`,
`maker_fee_rate` and `taker_fee_rate` on every contract and option it lists, and
a payload missing one is skipped with a warning naming the field rather than
published with a zero: a zero maintenance rate tells the account the position
needs no margin, and a zero fee tells it that trading is free, and neither is
distinguishable afterwards from a rate the venue really published.

## Order types by product

`SUPPORTED_ORDER_TYPES` is the authority in code; this table is its per-product
reading.

| Nautilus order type                                | Spot                                                                                                       | Perpetual / Inverse                                   | Delivery                           | Options                    | Status                      |
|----------------------------------------------------|------------------------------------------------------------------------------------------------------------|-------------------------------------------------------|------------------------------------|----------------------------|-----------------------------|
| `MARKET`                                           | native `type=market` for a sell or a quote-denominated buy; an aggressive limit for a base-denominated buy | `price="0"` with `tif=ioc`/`fok`                      | as perpetual                       | `price="0"` with `tif=ioc` | Implemented and mock-tested |
| `LIMIT`                                            | `type=limit`                                                                                               | `price` + `tif`                                       | as perpetual                       | `price` + `tif`            | Implemented and mock-tested |
| `STOP_MARKET`                                      | `POST /spot/price_orders`                                                                                  | `POST /futures/{settle}/price_orders`                 | `POST /delivery/usdt/price_orders` | -                          | Implemented and mock-tested |
| `STOP_LIMIT`                                       | as above                                                                                                   | as above                                              | as above                           | -                          | Implemented and mock-tested |
| `MARKET_IF_TOUCHED`                                | as above                                                                                                   | as above                                              | as above                           | -                          | Implemented and mock-tested |
| `LIMIT_IF_TOUCHED`                                 | as above                                                                                                   | as above                                              | as above                           | -                          | Implemented and mock-tested |
| `MARKET_TO_LIMIT`                                  | -                                                                                                          | -                                                     | -                                  | -                          | Unsupported                 |
| `TRAILING_STOP_MARKET`                             | -                                                                                                          | -                                                     | -                                  | -                          | Not implemented — see below |
| `TRAILING_STOP_LIMIT`                              | -                                                                                                          | -                                                     | -                                  | -                          | Not implemented — see below |
| Order lists, no contingency                        | batched through `POST /spot/batch_orders`                                                                  | batched through `POST /futures/{settle}/batch_orders` | submitted one at a time            | submitted one at a time    | Implemented and mock-tested |
| Order lists with a contingency (bracket, OCO, OTO) | -                                                                                                          | -                                                     | -                                  | -                          | Unsupported — see below     |

A hyphen above means the order is denied on this side. "Denied" and "rejected"
are different events, and the difference is the whole point: `OrderDenied` says
*Nautilus* refused the order, `OrderRejected` says *Gate.io* did. Everything this
adapter refuses on its own is a denial, whether
it is an unsupported order type, an unsupported time in force, an execution
instruction the endpoint has no field for or a fractional contract count. The
sequence is `OrderInitialized` -> `OrderDenied`, and no request reaches Gate.io.

The refusals are all decided while the request body is built, which happens
before `OrderSubmitted` is generated — so a rejection can only ever come from
the venue. That ordering is not a convention: the platform's order state machine
reaches `DENIED` from `INITIALIZED` alone, so a refusal announced after
`OrderSubmitted` could not be expressed as a denial at all.

The last four rows are not one kind of gap, and the difference decides what you
can do about each:

* `MARKET_TO_LIMIT` has no Gate.io equivalent **and** NautilusTrader cannot
  emulate it (`concepts/orders/emulated.md`, "Order types which can be
  emulated"). Nothing makes this one reachable.
* **Trailing stops are a gap in this adapter, not in the venue.** Gate.io has
  carried futures trailing-order endpoints (`POST
  /futures/{settle}/autoorder/v1/trail/create` and its siblings) since
  2026-02-02; this adapter does not call them yet, so both trailing types are
  denied for now. `TRAILING_STOP_LIMIT` would additionally have nowhere to put
  its limit price, since the venue's trailing request carries no sub-order price.
* **A contingent order list is refused, and the reason is identity, not
  linkage.** `submit_order_list` sends a list of plain orders to the venue: on
  spot and the two perpetual products as one batch request, elsewhere one order
  at a time. A list whose legs carry `linked_order_ids` or a contingency type —
  every bracket, OCO and OTO — is denied in full, every leg, with the reason on
  each. Gate.io does carry attached take-profit / stop-loss on spot and futures
  orders, which is the shape a Nautilus bracket has, but neither request model
  accepts a client-supplied identifier for the attached leg: the three Nautilus
  orders would reach the venue as one order with one id. Announcing two legs that
  can never acquire a venue order id is worse than refusing them — the live
  execution engine turns a submitted order the venue cannot identify into
  `OrderRejected(reason='UNKNOWN')` once the in-flight retries are spent, telling
  the strategy its stop-loss was rejected while Gate.io holds it live against the
  position.

None of that means contingent orders are unavailable to a strategy today — see
[order emulation](#order-emulation) below, which is how you get them without
waiting for either gap to close.

### Order emulation

Every order type this table denies, and every conditional order the options
product has no endpoint for, can still be traded against Gate.io — by letting
NautilusTrader emulate it locally and send this client only the `MARKET` or
`LIMIT` order it releases. Nothing in this adapter takes part: the platform's
own `OrderEmulator` watches the market data, and the released order arrives here
as an ordinary order. Pass an `emulation_trigger` when creating the order:

```python
order = self.order_factory.trailing_stop_market(
    instrument_id=instrument_id,
    order_side=OrderSide.SELL,
    quantity=instrument.make_qty(1),
    trailing_offset=Decimal("50"),
    trailing_offset_type=TrailingOffsetType.PRICE,
    emulation_trigger=TriggerType.DEFAULT,   # the emulator treats DEFAULT as BID_ASK
)
self.submit_order(order)
```

| Order type                                                          | Emulatable | Released to this client as |
|---------------------------------------------------------------------|------------|----------------------------|
| `LIMIT`, `STOP_MARKET`, `MARKET_IF_TOUCHED`, `TRAILING_STOP_MARKET` | ✓          | `MARKET`                   |
| `STOP_LIMIT`, `LIMIT_IF_TOUCHED`, `TRAILING_STOP_LIMIT`             | ✓          | `LIMIT`                    |
| `MARKET`, `MARKET_TO_LIMIT`                                         | -          | Not emulated.              |

Brackets and the contingency types work the same way. `OrderFactory.bracket()`
builds an order list, and `Strategy.submit_order_list` routes the whole list to
the emulator as soon as any leg carries an `emulation_trigger`; the emulator
submits the non-emulated legs individually as plain orders and enforces the
OTO / OCO / OUO links itself. That is why the contingency refusal above does not
stop a bracket strategy from running against this venue: with an
`emulation_trigger` the list never reaches this client as a contingent list.

Before relying on it:

* **Only three emulation triggers work.** Installed NautilusTrader 1.230.0
  accepts `DEFAULT`, `BID_ASK` and `LAST_PRICE` as an `emulation_trigger`. Any
  other value is not merely refused — the order is **canceled** with an error
  log. A derivatives strategy reaching for `MARK_PRICE` emulation loses the
  order. (This is separate from the order's own `trigger_type`, which is what a
  natively armed conditional order sends to Gate.io.)
* **The order is not at the venue until it releases.** It does not appear in the
  Gate.io UI or in this adapter's order status reports, it cannot be hit by a
  venue-side stop, and it survives a process restart only if a cache database is
  configured.
* **What releases is what this client then translates.** An emulated stop
  released as a `MARKET` buy on spot goes down the base-denominated market-buy
  path below, i.e. it reaches Gate.io as an aggressive limit.
* **Emulation is not a mark-price trigger for spot.** Emulation chooses which
  local price stream drives the trigger; it cannot invent a price Gate.io does
  not publish for the product.

This section describes the platform, not this package: the behavior above was
read from installed 1.230.0 (`execution/emulator.pyx`, `execution/manager.pyx`,
`trading/strategy.pyx`) and `concepts/orders/emulated.md`. No test in this
repository exercises the emulator.

### The spot market-order quirk

Gate.io spot market orders interpret `amount` differently per side: a market
**buy** spends a *quote* amount, a market **sell** delivers a *base* amount.
There is no venue field for "buy exactly this many base units".

| Nautilus order                          | Sent to Gate.io                                                                                                                                          |
|-----------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
| `MARKET` SELL                           | `type=market`, `amount` = base quantity                                                                                                                  |
| `MARKET` BUY with `quote_quantity=True` | `type=market`, `amount` = quote amount                                                                                                                   |
| `MARKET` BUY with a base quantity       | `type=limit`, priced through the book at the pair's own published `slippage` cap (5% if the pair publishes none), carrying the order's own time in force |

The third row is the only case on the regular order path where a Nautilus order
type reaches the venue as a different Gate.io order type — a zero price with
`ioc` on the contract products is Gate.io's own encoding of a market order, not
a substitution. Converting the quantity behind the caller's back would change
what was ordered; a limit at the venue's own slippage bound is the closest
faithful expression, and the venue cancels any unfilled remainder.

The substitution is in the **price and nothing else**. The time in force is the
one the order carries, so a `MARKET` / `FOK` buy is sent as a `fok` limit and
keeps its all-or-nothing guarantee rather than collapsing to "fill whatever is
available".
The reference price is taken from the far side of the cached quote, then the
cached last trade, then the cached quote's mid, then the venue ticker; if none of
those is available the order is rejected rather than priced by guesswork.

Fill quantities for such an order are read from `filled_amount` (base), never
from the submitted `amount`, which is quote-currency cash. The two denominations
are never compared: an order's completion is decided in quote units
(`filled_total` against `amount`), and its quantity in base units. A
quote-denominated buy carries a bound in base units while it works and is closed
with `OrderCanceled` on the venue's own `filled_amount` when Gate.io finishes
it, so it ends `CANCELED` rather than `FILLED` — see
[execution](execution.md#fills) for why the platform allows no other close
(*implemented and mock-tested*).

## Time in force by product

Gate.io accepts `gtc`, `ioc`, `poc` (post-only) and `fok`. Everything else has
no representation, and the adapter raises rather than downgrading — a downgrade
silently changes the execution guarantee the strategy asked for.

**Limit orders.** The accepted mappings are asserted from real request bodies
and the refusals from the mapping function, so this table is *implemented and
mock-tested*.

| `TimeInForce`                        | Spot   | Perpetual / Inverse / Delivery | Options                                |
|--------------------------------------|--------|--------------------------------|----------------------------------------|
| `GTC`                                | `gtc`  | `gtc`                          | `gtc`                                  |
| `IOC`                                | `ioc`  | `ioc`                          | `ioc`                                  |
| `FOK`                                | `fok`  | `fok`                          | denied — Gate.io options have no `fok` |
| `GTD`                                | denied | denied                         | denied                                 |
| `DAY`                                | denied | denied                         | denied                                 |
| `AT_THE_OPEN`, `AT_THE_CLOSE`        | denied | denied                         | denied                                 |
| `GTC` with `post_only=True`          | `poc`  | `poc`                          | `poc`                                  |
| `IOC` or `FOK` with `post_only=True` | denied | denied                         | denied                                 |

Gate.io has no separate post-only flag: the constraint *is* the `poc` time in
force, a maker-only order that rests until it is canceled. A post-only `GTC`
order is therefore exactly `poc`, and nothing is lost. An `IOC` or `FOK` order
that also sets `post_only=True` asks for maker-only *and* immediate, which `poc`
cannot express — sending `poc` anyway would leave an order resting that the
strategy expects to have self-canceled — so it is refused. Every other time in
force is already refused on its own.

A post-only order the venue would have crossed comes back as `OrderRejected`
with `due_post_only=True`, both when the venue answers with a post-only error
label and when a later order message carries `finish_as=poc`.

**Market orders.** One mapping serves every product. The `FOK` row and the
`AT_THE_OPEN` / `AT_THE_CLOSE` refusal are *implemented and mock-tested*; the
rest is *implemented, mainnet validation pending*.

| `TimeInForce`                 | Spot                                                                     | Perpetual / Inverse / Delivery | Options  |
|-------------------------------|--------------------------------------------------------------------------|--------------------------------|----------|
| `GTC`, `IOC`, `DAY`           | `ioc`                                                                    | `ioc`                          | `ioc`    |
| `FOK`                         | `fok`                                                                    | `fok`                          | rejected |
| `AT_THE_OPEN`, `AT_THE_CLOSE` | rejected                                                                 | rejected                       | rejected |
| `GTD`                         | not constructible: NautilusTrader itself refuses `GTD` on a market order |                                |          |

`GTC` and `DAY` collapse to `ioc` on every product because an order that cannot
rest has no meaningful resting duration; that is a re-expression of the same
instruction, not a change of guarantee. `AT_THE_OPEN` and `AT_THE_CLOSE` are a
different matter: Gate.io runs no sessions, so there is no open or close to be
active at, and the instruction is refused everywhere rather than absorbed.
Spot used to take a shortcut here that accepted them as `ioc`; it now goes
through the same mapping as the other three products.

**Conditional orders** (`STOP_*`, `*_IF_TOUCHED`) are different again, because
on a price-triggered order the Nautilus time in force describes how long the
*trigger* stays armed, while Gate.io expresses that with `trigger.expiration`
and lets the *fired* order carry its own `gtc` or `ioc`. The `GTC` rows and the
`FOK` refusal are *implemented and mock-tested*; the `GTD` expiration mapping is
*implemented, mainnet validation pending*.

| `TimeInForce`                               | Effect                                                                                                      |
|---------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| `GTC`                                       | Fired order is `gtc` (limit) or `ioc` (market); the trigger carries no expiry                               |
| `GTD`                                       | As `GTC`, plus `trigger.expiration` set to the remaining seconds; an expiry already in the past is rejected |
| `IOC`                                       | Fired order is `ioc`                                                                                        |
| `FOK`, `DAY`, `AT_THE_OPEN`, `AT_THE_CLOSE` | Rejected — the price-order endpoints accept `gtc` and `ioc` only                                            |

## Execution instructions by product

| Instruction                              | Spot                                                            | Perpetual / Inverse                                                       | Delivery                    | Options                     | Status                                                                                                                                          |
|------------------------------------------|-----------------------------------------------------------------|---------------------------------------------------------------------------|-----------------------------|-----------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| `post_only` (regular orders)             | `poc`; denied with IOC or FOK                                   | as spot                                                                   | as spot                     | as spot                     | Implemented and mock-tested (the mapping and the refusal); implemented, mainnet validation pending (the request bodies)                         |
| `post_only` (conditional orders)         | denied                                                          | denied                                                                    | denied                      | Not applicable              | Implemented and mock-tested                                                                                                                     |
| `reduce_only` (regular orders)           | denied — spot has no such flag                                  | `reduce_only=true`                                                        | `reduce_only=true`          | `reduce_only=true`          | Implemented and mock-tested (the spot refusal); implemented, mainnet validation pending (the request bodies)                                    |
| `reduce_only` (conditional orders)       | denied                                                          | `initial.reduce_only=true`                                                | as perpetual                | Not applicable              | Implemented and mock-tested                                                                                                                     |
| `display_qty` / iceberg (regular orders) | `iceberg` (decimal string)                                      | `iceberg` (whole contracts)                                               | `iceberg` (whole contracts) | `iceberg` (whole contracts) | Implemented and mock-tested (the refusals); implemented, mainnet validation pending (the request bodies)                                        |
| `display_qty=0` (fully hidden)           | -                                                               | -                                                                         | -                           | -                           | Implemented and mock-tested                                                                                                                     |
| `display_qty` (conditional orders)       | denied                                                          | denied                                                                    | denied                      | Not applicable              | Implemented and mock-tested                                                                                                                     |
| `quote_quantity`                         | market buy only; denied on a market sell and on any limit order | denied                                                                    | denied                      | denied                      | Implemented and mock-tested                                                                                                                     |
| Trigger reference price                  | `LAST_PRICE`/`DEFAULT` only; anything else denied               | `LAST_PRICE`/`DEFAULT`, `MARK_PRICE`, `INDEX_PRICE`; anything else denied | as perpetual                | Not applicable              | Implemented and mock-tested (the accepted types and both refusals); implemented, mainnet validation pending (the mark and index request bodies) |
| Hedge (dual) position mode               | Not applicable                                                  | refused at connect                                                        | Not applicable              | Not applicable              | Unsupported                                                                                                                                     |

Reduce-only is refused on spot rather than dropped: it is a derivatives concept,
and an order that quietly lost it would mean something different from the one
that was requested. Hedge mode is detected at connect and the client refuses to
start, because NautilusTrader nets positions per instrument and a venue holding
a separate long and short leg for one contract cannot be reconciled; the adapter
never switches the mode itself.

## What is refused rather than translated

The design rule is that an order the adapter cannot express faithfully is
refused, not approximated. Every case below was read in the code, and every one
is decided before a request reaches the venue — which is why a submission refusal
is `OrderDenied` and not `OrderRejected`: Gate.io was never asked. The status
column says whether the suite also pins the behavior.

| Situation                                                                                                                                            | Outcome                                                                                                                                                                                                                                                | Status                                                                                                        |
|------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| `MARKET_TO_LIMIT`, `TRAILING_STOP_MARKET`, `TRAILING_STOP_LIMIT`                                                                                     | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested (`TRAILING_STOP_MARKET`); implemented, mainnet validation pending (the other two) |
| An instrument whose product is not in the client's `products`                                                                                        | `OrderDenied`                                                                                                                                                                                                                                          | Implemented, mainnet validation pending                                                                       |
| An instrument the provider and cache do not hold                                                                                                     | `OrderDenied`                                                                                                                                                                                                                                          | Implemented, mainnet validation pending                                                                       |
| `FOK` on any options order, market or limit                                                                                                          | `OrderDenied` — Gate.io options accept `gtc`, `ioc`, `poc` only                                                                                                                                                                                        | Implemented and mock-tested                                                                                   |
| `AT_THE_OPEN` / `AT_THE_CLOSE` on a market order, on every product including spot                                                                    | `OrderDenied` — Gate.io runs no sessions, and spot used to accept these as `ioc`                                                                                                                                                                       | Implemented and mock-tested                                                                                   |
| `GTD`, `DAY`, `AT_THE_OPEN`, `AT_THE_CLOSE` on any limit order                                                                                       | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| `FOK` on a conditional order                                                                                                                         | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| `DAY`, `AT_THE_OPEN`, `AT_THE_CLOSE` on a conditional order                                                                                          | `OrderDenied`                                                                                                                                                                                                                                          | Implemented, mainnet validation pending                                                                       |
| `post_only` combined with `IOC` or `FOK` on a limit order                                                                                            | `OrderDenied` — `poc` rests until canceled, so the immediacy cannot survive the substitution                                                                                                                                                           | Implemented and mock-tested                                                                                   |
| `post_only` on a conditional order                                                                                                                   | `OrderDenied` — the fired order cannot carry `poc`                                                                                                                                                                                                     | Implemented and mock-tested                                                                                   |
| `display_qty` on a conditional order                                                                                                                 | `OrderDenied` — the price-order endpoints have no iceberg field                                                                                                                                                                                        | Implemented and mock-tested                                                                                   |
| `display_qty=0` on any regular order                                                                                                                 | `OrderDenied` — Nautilus means "fully hidden", Gate.io reads `iceberg=0` as "normal order" and does not support hiding the whole amount                                                                                                                | Implemented and mock-tested                                                                                   |
| A fractional `display_qty` on any derivative                                                                                                         | `OrderDenied` — the iceberg quantity is a contract count, and truncating it would display the whole order                                                                                                                                              | Implemented and mock-tested                                                                                   |
| A price or trigger price off the instrument's tick grid                                                                                              | `OrderDenied` on submit, `OrderModifyRejected` on amend — Gate.io accepts on-tick prices only                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| `reduce_only` on a conditional spot order                                                                                                            | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| `reduce_only` on a regular spot order                                                                                                                | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| Any conditional order on options                                                                                                                     | `OrderDenied` — Gate.io publishes no options price-order endpoint                                                                                                                                                                                      | Implemented and mock-tested                                                                                   |
| A conditional spot order while `spot_account_mode=CROSS_MARGIN`                                                                                      | `OrderDenied` — the spot price-order endpoint has no cross-margin ledger, and routing it to another ledger would trade a different account                                                                                                             | Implemented and mock-tested                                                                                   |
| A trigger type other than `DEFAULT`, `LAST_PRICE`, `MARK_PRICE`, `INDEX_PRICE` on a futures conditional order                                        | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| A trigger type other than `DEFAULT` or `LAST_PRICE` on a **spot** conditional order                                                                  | `OrderDenied` — the spot trigger object is `{price, rule, expiration}` with no price-type field, and spot has no mark or index price to name                                                                                                           | Implemented and mock-tested                                                                                   |
| A conditional order whose trigger price contradicts its own order type — a `STOP_*` at or beyond the market, an `*_IF_TOUCHED` on the far side of it | `OrderDenied` — Gate.io takes only a comparison rule and requires it to agree with the last price, so the sole rule it would accept encodes the opposite conditional type                                                                              | Implemented and mock-tested                                                                                   |
| A fractional contract quantity on any derivative                                                                                                     | `OrderDenied` on submit, `OrderModifyRejected` on amend                                                                                                                                                                                                | Implemented and mock-tested                                                                                   |
| `quote_quantity=True` anywhere except a spot market buy                                                                                              | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| A conditional order whose `expire_time` has already passed                                                                                           | `OrderDenied`                                                                                                                                                                                                                                          | Implemented and mock-tested                                                                                   |
| A base-denominated spot market buy with no quote, trade or ticker price                                                                              | `OrderDenied` — the order cannot be priced, so it cannot be built; resubmit with `quote_quantity=True` for Gate.io's native quote-denominated market buy                                                                                               | Implemented and mock-tested                                                                                   |
| An amendment of a delivery or options order                                                                                                          | `OrderModifyRejected` — neither venue namespace has an amend endpoint                                                                                                                                                                                  | Implemented, mainnet validation pending                                                                       |
| An amendment of an armed conditional order                                                                                                           | `OrderModifyRejected` — cancel and resubmit instead                                                                                                                                                                                                    | Implemented, mainnet validation pending                                                                       |
| An amendment carrying a new trigger price                                                                                                            | `OrderModifyRejected`                                                                                                                                                                                                                                  | Implemented, mainnet validation pending                                                                       |
| An amendment of a contract order that is not in the cache                                                                                            | `OrderModifyRejected` — the side determines the sign of `size`, and guessing it could flip a short into a long                                                                                                                                         | Implemented and mock-tested                                                                                   |
| `DELETE /options/orders` without a `contract` or `underlying` scope                                                                                  | `ValueError` from the HTTP layer, before the request — Gate.io would otherwise cancel every resting option order in the account                                                                                                                        | Implemented and mock-tested                                                                                   |
| `DELETE /spot/orders` without a pair, `DELETE /futures/{settle}/orders` without a contract                                                           | The scope is a required argument, so the call fails with `TypeError` and issues no request. Gate.io treats the spot `currency_pair` as optional and would cancel across every pair and ledger; the futures endpoint requires the contract at the venue | Implemented and mock-tested                                                                                   |

## Where the adapter substitutes rather than refuses

One known case does not follow the rule above. It is listed because a page that
only advertised the rule would be misleading. It is *implemented, mainnet
validation pending*: it was established by reading and exercising the code, and
no test currently pins it.

1. **Options cancel-all ignores the order side.** A side-scoped
   `CancelAllOrders` on an option contract cancels every resting order on that
   contract, both sides. Spot and futures honor the side (see below).

Two entries that used to sit in this list are gone because the substitutions
themselves are gone, and both are now in the refusal table above: a spot market
order no longer absorbs `AT_THE_OPEN` / `AT_THE_CLOSE` as `ioc`, and a spot
conditional order no longer drops a `trigger_type` it cannot carry.

Market-data subscriptions are deliberately more forgiving than order handling:
an order book depth or push interval a product does not serve is clamped to the
nearest supported value with a warning rather than failing the subscription. The
consequence of a mis-sized book is a different amount of data, not a different
trade.

## Amendment and cancellation

| Operation                              | Spot                                                                                              | Perpetual / Inverse                         | Delivery        | Options                           | Status                                                                                                                                     |
|----------------------------------------|---------------------------------------------------------------------------------------------------|---------------------------------------------|-----------------|-----------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| Amend price and/or quantity            | `PATCH /spot/orders/{id}`                                                                         | `PUT /futures/{settle}/orders/{id}`         | rejected        | rejected                          | Implemented and mock-tested (perpetual and inverse); implemented, mainnet validation pending (spot, and the delivery and options refusals) |
| Amend an armed conditional order       | rejected                                                                                          | rejected                                    | rejected        | Not applicable                    | Implemented, mainnet validation pending                                                                                                    |
| Cancel one order                       | `DELETE /spot/orders/{id}`                                                                        | `DELETE {base}/orders/{id}`                 | as perpetual    | `DELETE /options/orders/{id}`     | Implemented and mock-tested                                                                                                                |
| Cancel one armed conditional order     | by its armed id                                                                                   | by its armed id                             | by its armed id | Not applicable                    | Implemented and mock-tested                                                                                                                |
| Cancel all for one instrument          | pair-scoped, side honored                                                                         | contract-scoped, side mapped to `bid`/`ask` | as perpetual    | contract-scoped, **side ignored** | Implemented and mock-tested (spot, contracts); implemented, mainnet validation pending (options)                                           |
| Bulk disarm of conditional orders      | bulk when unscoped, individually by id when a side is named                                       | same                                        | same            | Not applicable                    | Implemented and mock-tested                                                                                                                |
| Batch cancel                           | `POST /spot/cancel_batch_orders`, 20 per request                                                  | falls back to sequential single cancels     | as perpetual    | as perpetual                      | Implemented, mainnet validation pending                                                                                                    |
| Account-wide cancel-all                | Unsupported on every product: each namespace requires a scope                                     |                                             |                 |                                   | Unsupported                                                                                                                                |
| Countdown cancel-all (dead-man switch) | REST method exists on spot, perpetual and options namespaces; the execution client never calls it |                                             |                 |                                   | Experimental                                                                                                                               |

A cancel command carries no side filter to the price-order endpoints, because
neither of them accepts one: a bulk disarm would take out both sides of the book
whenever a side was named. A side-scoped command therefore disarms the matching
price orders individually, by id, and leaves the other side alone. That
behavior is asserted for both spot and futures.

Conditional orders live in a second id space: Gate.io arms them under one id and,
when the trigger fires, creates a **new order with a different id**. The adapter
keeps both, so a cancel issued before the trigger fires goes to the armed id and
one issued afterwards goes to the fired order.

### Reports

Order status and fill reports are generated for every configured product;
position status reports are generated for the derivative products only, because
a spot balance is not a position. Two product-specific constraints are worth
knowing before relying on reconciliation: option fills can only be queried per
underlying, so `options_underlyings` (or an open option order or position) has to
supply one, and the futures and delivery fill endpoints accept no time range, so
the window is walked backwards by row offset until a page reaches past its start.
Both are *implemented, mainnet validation pending*; the conditional-order
identity that reconciliation restores after a restart is *implemented and
mock-tested*.

## Market data by product

| Data type                         | Spot | Perpetual | Inverse | Delivery | Options | Source                                                                                                                     | Status                                                                                                          |
|-----------------------------------|------|-----------|---------|----------|---------|----------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| `TradeTick`                       | ✓    | ✓         | ✓       | ✓        | ✓       | `<prefix>.trades`.                                                                                                         | Implemented and mock-tested                                                                                     |
| `QuoteTick` (real best bid/offer) | ✓    | ✓         | ✓       | ✓        | ✓       | `<prefix>.book_ticker`.                                                                                                    | Implemented and mock-tested                                                                                     |
| `OrderBookDeltas` (`L2_MBP`)      | ✓    | ✓         | ✓       | ✓        | ✓       | REST snapshot + `<prefix>.order_book_update`.                                                                              | Implemented and mock-tested                                                                                     |
| `OrderBookDepth10`                | ✓    | ✓         | ✓       | ✓        | ✓       | `<prefix>.order_book`, the venue's periodic snapshot channel.                                                              | Implemented and mock-tested                                                                                     |
| Order book snapshot request       | ✓    | ✓         | ✓       | ✓        | ✓       | REST `order_book`, depth clamped per product.                                                                              | Implemented and mock-tested                                                                                     |
| `Bar` (closed bars only)          | ✓    | ✓         | ✓       | ✓        | ✓       | `<prefix>.candlesticks`; options use `options.contract_candlesticks`.                                                      | Implemented and mock-tested                                                                                     |
| Historical bars and trades        | ✓    | ✓         | ✓       | ✓        | ✓       | Paginated REST, 1000 rows per call.                                                                                        | Implemented, mainnet validation pending                                                                         |
| `MarkPriceUpdate`                 | -    | ✓         | ✓       | ✓        | ✓       | `futures.tickers`; `options.contract_tickers` on options. Spot has no mark price.                                          | Implemented, mainnet validation pending                                                                         |
| `IndexPriceUpdate`                | -    | ✓         | ✓       | ✓        | ✓       | `futures.tickers`; `options.contract_tickers` on options. Spot has no index price.                                         | Implemented, mainnet validation pending                                                                         |
| `FundingRateUpdate`               | -    | ✓         | ✓       | -        | -       | `futures.tickers`. Only perpetual contracts fund.                                                                          | Implemented, mainnet validation pending                                                                         |
| Historical `FundingRateUpdate`    | -    | ✓         | ✓       | -        | -       | REST `/futures/{settle}/funding_rate`. Only perpetual contracts fund.                                                      | Implemented, mainnet validation pending                                                                         |
| Instrument updates                | ✓    | ✓         | ✓       | ✓        | ✓       | Periodic REST reload; Gate.io has no instrument channel.                                                                   | Implemented and mock-tested (loading and filtering); implemented, mainnet validation pending (the reload timer) |
| `InstrumentStatus`                | ✓    | ✓         | ✓       | ✓        | ✓       | Polled instrument listings; Gate.io has no status channel.                                                                 | Implemented and mock-tested                                                                                     |
| `InstrumentClose`                 | -    | -         | -       | ✓        | ✓       | REST settlement after expiry. The three continuous products never settle, and the subscription is refused with the reason. | Implemented and mock-tested                                                                                     |
| `GateioTicker` (venue ticker row) | ✓    | ✓         | ✓       | ✓        | ✓       | `<prefix>.tickers`, published as adapter-specific custom data.                                                             | Implemented and mock-tested                                                                                     |
| Historical `QuoteTick`            | -    | -         | -       | -        | -       | Gate.io publishes no quote history; the request is refused rather than answered from the current ticker row.               | Unsupported                                                                                                     |
| Book types other than `L2_MBP`    | -    | -         | -       | -        | -       | Only `L2_MBP` is assembled; any other book type is refused.                                                                | Unsupported                                                                                                     |
| Options underlying streams        | -    | -         | -       | -        | ✓       | `options.ul_*` through `GateioPublicWebSocket.client`, not routed into the data engine.                                    | Experimental                                                                                                    |

An option's greeks and implied volatilities arrive with the ticker row and reach
a strategy as `GateioTicker` fields, which are the venue's own strings. They are
not mapped onto the platform's `OptionGreeks` type, and `subscribe_option_greeks`
is unimplemented.

Gate.io has no dedicated mark, index or funding channel: all three are fields of
the ticker stream — `futures.tickers` on the futures products,
`options.contract_tickers` on options — so one subscription serves them and the
client reference counts it. A funding subscription is refused with an explanation
rather than silently producing nothing on any product that does not pay funding:
a delivery contract converges on its settlement price, and an option has no
funding leg. Mark and index prices are published on the scale the venue used
rather than on the instrument's order tick, and the next funding time is derived
from the venue's funding grid rather than replayed from the cached contract
definition; both are described in
[market-data.md](market-data.md#mark-price-index-price-and-funding-rate).

Bars come only from closed candlesticks and only for the `LAST` price type. The
WebSocket candlestick channel serves `10s` upwards, so 1-second bars are
available by historical request but not by subscription. Delivery and options
publish no bar-closed flag, so the client infers the close from the bucket
advancing or from the clock passing the bucket's end.

Per-product order book limits are the tables in
`nautilus_gateio.websocket.public` — spot and perpetuals push at 20 ms or 100 ms,
delivery and options at 100 ms or 1000 ms; snapshot depth reaches 100 everywhere
except options, which stop at 50.

## Account modes: spot, margin, cross margin, unified

Margin orders are still spot orders: the same `/spot/orders` endpoint with a
different `account` field. Select the ledger with `spot_account_mode`.

| `GateioSpotAccountMode` | `account` on a regular order | `put.account` on a conditional order   | Ledger                                             |
|-------------------------|------------------------------|----------------------------------------|----------------------------------------------------|
| `SPOT`                  | `spot`                       | `normal`                               | Plain cash spot                                    |
| `MARGIN`                | `margin`                     | `margin`                               | Isolated margin, scoped to one pair                |
| `CROSS_MARGIN`          | `cross_margin`               | none — conditional orders are rejected | Cross margin, reported through the unified account |
| `UNIFIED`               | `unified`                    | `unified`                              | Unified account                                    |

Ledger selection is *implemented and mock-tested* for the plain spot ledger,
whose regular and price-order bodies are both asserted, and for the wire
vocabulary of all four modes. The margin ledgers' request bodies and cross
margin's rejection of conditional orders are *implemented, mainnet validation
pending*.

The naming asymmetry in the third column is Gate.io's own and is preserved
verbatim, because the venue validates it: a price-triggered order names the
plain spot ledger `normal` where a regular order says `spot`.

Choosing any margin mode has two further consequences:

* the Nautilus account becomes `AccountType.MARGIN` instead of `CASH`, and
  `AccountFactory.register_cash_borrowing` is called for the venue so borrowed
  (negative) balances can be held (*implemented and mock-tested*);
* balances are read from that ledger's own endpoints — `/margin/accounts`,
  `/margin/cross/accounts` or `/unified/accounts` — with borrowed principal and
  accrued interest subtracted from the total, instead of only `/spot/accounts`
  (*implemented, mainnet validation pending*).

The account type also changes with the product set, and that mapping is
*implemented and mock-tested*: a client is a `CASH` account only when spot is
the sole product *and* the plain spot ledger is selected. Any derivative, or any
margin ledger, makes it a `MARGIN` account. A unified account additionally
replaces the per-wallet balances in the aggregate rather than adding to them,
because the venue's unified ledger already subsumes them.

Borrow and repay endpoints are exposed on `GateioMarginHttpAPI` because isolated
and cross margin cannot be supported without them. Every method that can create
a liability says so in its docstring. Borrowing accrues interest from the moment
it is drawn, and Gate.io can liquidate collateral to recover it.

A unified account has its own venue-side sub-mode (`classic`, `single_currency`,
`multi_currency`, `portfolio`), and Gate.io gates the richer modes behind
account-balance minimums. Those thresholds are venue policy: the adapter neither
enforces nor checks them, and **never changes an account's mode**.
`GateioMarginHttpAPI.unified_account_mode()` and `set_unified_account_mode()`
exist for a caller who wants to do it deliberately. Several unified response
fields are structurally zero outside `multi_currency` and `portfolio` modes, so
read `mode` before interpreting them.

### Degradation on an unprovisioned wallet

Gate.io reports "wallet not created yet", "account not in the required mode" and
"key lacks permission" as ordinary 4xx errors. The adapter translates all three
into `WalletNotProvisionedError` with an actionable message and skips that
product during an instrument load, a balance sweep or a reconciliation query,
instead of failing to start. A client configured for four products on an account
that has only ever funded spot therefore still starts, and says which wallets it
skipped. The error mapping and the instrument-load skip are *implemented and
mock-tested*; the balance and reconciliation skips are *implemented, mainnet
validation pending*.

## Wallet segregation and transfers

Funds do not move implicitly between product wallets. An account holding USDT in
the spot wallet cannot open a futures position until the balance is transferred,
and the futures, delivery and options wallets are **created by the first
transfer into them** — until then the venue answers that the user does not
exist.

```python
await exec_client.transfer(currency="USDT", from_="spot", to="futures", amount="25", settle="usdt")
```

`settle` is required whenever either end is a contract wallet, and
`currency_pair` whenever either end is isolated margin; both are validated
before the request. Gate.io routes every internal transfer through the spot
wallet, so moving between two derivative wallets takes two calls. `transfer()`
cannot send funds outside the account: the accepted account names are the
account's own trading wallets, and the request carries no address and no
recipient (*implemented and mock-tested*). The execution client logs a warning at
startup so the segregation is never a surprise.

That is a property of this module, not a security boundary. What an API key may
do is decided by the key's own permission set on Gate.io; a key used with this
adapter needs no withdrawal permission at all.

## Out of scope

The adapter has no code for withdrawals, sub-account transfers, Earn, Gate Pay,
P2P, Copy Trading or Gate Bots. They are unrelated to trading. This is a scope
decision rather than a safety mechanism — restricting what a key may do is what
Gate.io API key permissions are for.
