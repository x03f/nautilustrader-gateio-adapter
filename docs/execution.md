# Execution

`GateioExecutionClient` trades every configured product through a single
NautilusTrader account: spot (optionally on a margin ledger), USDT perpetual
futures, BTC-settled (inverse) perpetual futures, USDT delivery futures and
USDT-settled options.

This page describes what happens to an order after it is submitted, and what
happens when the connection, the process or the venue's own bookkeeping gets in
the way. Two behaviours have sections of their own — [conditional order
identity](#conditional-order-identity) and [the fill-before-order
race](#the-fill-before-order-race) — because they are where an otherwise
reasonable implementation silently loses fills.

## Maturity

This is an external community adapter for NautilusTrader 1.230.0, implemented in
pure Python. It is not an official NautilusTrader integration, and it is not
affiliated with Gate.io or Nautech Systems. It deliberately deviates from the
preferred in-tree Rust/PyO3 adapter architecture; a Rust migration is a possible
future project, not a plan.

**No part of the execution path has been validated against the live venue.**
What exists is an offline test suite that drives the real NautilusTrader `Order`
state machine with recorded venue payload shapes, so an event sequence the
framework would reject fails a test rather than passing quietly. That proves the
adapter is internally consistent; it does not prove Gate.io agrees with it.

Capabilities on this page carry one of these labels:

| Label | Meaning |
|---|---|
| implemented and mock-tested | Implemented, and exercised by the offline execution tests |
| implemented, mainnet validation pending | Implemented, but not exercised by the execution tests — only the layer below it (an HTTP namespace or a parser) is covered |
| experimental | Implemented; the behaviour or the interface may still change |
| unsupported | Not available. The client says so explicitly instead of approximating |
| not applicable | The concept does not exist on that product, or not in this layer |

*Mainnet validation pending* applies to every row on the page, including the
ones labelled *implemented and mock-tested*. See
[validation.md](validation.md), which is where real-venue results get recorded.

**`environment` defaults to `"mainnet"`.** Set `environment="testnet"`
explicitly for the Gate.io testnet, and note that Gate.io serves only spot and
USDT perpetuals there — configuring any other product against the testnet is
refused before a connection is opened. There is no local order kill switch; see
[configuration.md](configuration.md).

## Account routing

One execution client is one Nautilus account, and every configured product is
routed through it.

| Aspect | Behaviour |
|---|---|
| Account id | `GATE_IO-master` |
| Account type | `CASH` when spot is the only product **and** `spot_account_mode=SPOT`; `MARGIN` in every other combination |
| OMS type | `NETTING` |
| Hedge (dual) position mode | unsupported — detected at connect and refused with an explanatory error |
| Balances | Aggregated per currency across the wallets of the enabled products |
| Margins | `MarginBalance` per instrument on futures and delivery, one account-level `MarginBalance` on options; published only for a `MARGIN` account |

Routing an order is a two-step lookup. The instrument id determines the product
(see [symbology.md](symbology.md)), and the product determines both the REST
namespace and the venue wallet the order settles against. An instrument whose
product is not in `products` is refused rather than guessed at: a submission is
denied before any network call, a cancel or amend is rejected with a reason, and
a cancel-all is logged and dropped.

Spot orders additionally name a ledger. `spot_account_mode` is sent as the
`account` field on every spot order, so `SPOT`, `MARGIN` (isolated),
`CROSS_MARGIN` and `UNIFIED` select which spot ledger trades. Any margin mode
registers cash borrowing for the venue with NautilusTrader's account factory,
because a margin ledger can hold a negative balance that a cash account would
refuse.

Position mode is checked at connect, once per perpetual product (delivery
futures and options have no hedge mode, and the check is skipped with a warning
when the wallet is not provisioned at all). Nautilus nets positions per
instrument, so an account holding separate long and short legs of the same
contract cannot be reconciled; the client raises with the venue-side remedy
spelled out and never changes the setting itself. Changing an account-wide venue
setting on the operator's behalf is not this client's decision to make.

Gate.io keeps a **separate wallet per product**, and funds never move between
them implicitly. The client logs a warning at startup so this is never a
surprise, and exposes [`transfer()`](#transfers) to move funds between the
account's own trading wallets. See [products.md](products.md) for the wallet
model itself.

## Event sources

The **private WebSocket is the primary event source**, one connection per
product:

| Channel | Drives |
|---|---|
| `{spot,futures,options}.orders` | the order lifecycle |
| `{spot,futures,options}.usertrades` | fills |
| `{spot,futures,options}.balances` | account state |
| `{futures,options}.positions` | parsed and logged at debug level, never published as reports |

Position updates are deliberately not forwarded. REST is the single
reconciliation source for positions, and publishing the stream as well would let
one fill produce two competing views of the same position.

A REST account poll (`account_polling_interval_secs`, default 30 s) refreshes
account state as a safety net behind the balance stream.

Gate.io publishes armed price-triggered orders on **no** private channel that
this client subscribes to, so an armed order's own state changes — expiry, or a
disarm performed elsewhere — are visible only through reconciliation. See
[conditional order identity](#conditional-order-identity).

The private channels for futures, delivery and options are addressed by the
numeric account user id, which the venue does **not** validate at subscribe
time: a wrong id is acknowledged and then silently delivers nothing. The client
therefore fetches the real user id at connect (`/wallet/fee`, falling back to
`/spot/fee`) and refuses to start a derivative product without it.

## The order lifecycle

### Submission and acceptance

The Gate.io request is built first and `OrderSubmitted` is generated only once
it is ready to go out, immediately before the REST call. That ordering is the
whole mechanism behind [rejection and denial](#rejection-denial-and-expiry):
building the request is what decides every refusal this client makes on its own,
so those refusals land while the order is still `INITIALIZED` and
`OrderSubmitted` never claims a request that was not sent.

The venue's response to that call is fed through **the same handler as the
WebSocket order stream**, so the REST path and the stream path cannot drift
apart in how they interpret an order object.

`OrderAccepted` is generated when an order object reports `open` (or open with a
non-zero filled amount) while the Nautilus order is still `SUBMITTED`; a later
order object for an already-accepted order becomes `OrderUpdated` instead, and
only when the quantity or price actually differs. A market order is never
restated this way — Gate.io has no resting quantity or price to amend on one,
and its payload quantity is denominated differently depending on side.

A conditional order is accepted directly on the armed id when the venue returns
it; if the venue accepts a price-triggered order without returning an id, that
is treated as an error and the order is rejected rather than left unidentifiable.

Note that an order object reporting `FILLED` does **not** close the order. The
closing event is the fill itself, which arrives on the trade channel carrying the
venue trade id that de-duplication depends on.

### Order translation

| Nautilus order | Gate.io encoding |
|---|---|
| MARKET, spot SELL | `type=market`, `amount` = base quantity, `time_in_force` `ioc` (`fok` honoured) |
| MARKET, spot BUY with `quote_quantity=True` | `type=market`, `amount` = quote amount |
| MARKET, spot BUY with a base quantity | aggressive IOC `limit` priced by the pair's published `slippage` cap |
| MARKET, futures and delivery | `price="0"` with `tif=ioc` (`fok` honoured) |
| MARKET, options | `price="0"` with `tif=ioc`; `fok` is rejected |
| LIMIT | `price`, `tif` `gtc`/`ioc`/`fok`; post-only GTC maps to `poc` |
| STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED | the product's price-triggered ("auto order") endpoint |

Supported order types are exactly:

```python
from nautilus_gateio.execution import CONDITIONAL_ORDER_TYPES, SUPPORTED_ORDER_TYPES
```

`SUPPORTED_ORDER_TYPES` = MARKET, LIMIT, STOP_MARKET, STOP_LIMIT,
MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED. Anything else is **denied** before a
network call, with the reason on the `OrderDenied` event.
`CONDITIONAL_ORDER_TYPES` is the subset routed to the price-triggered endpoint.

Time in force: GTC, IOC and FOK, plus post-only through `poc`. Post-only is
Gate.io's `poc` time in force — a maker-only order that *rests* — so it composes
with GTC and is refused, not substituted, when it is combined with IOC or FOK.
Flags honoured: `reduce_only` (derivatives only), `display_qty` (iceberg, regular
orders only, and never zero — see below), `quote_quantity` (spot market buy
only). Sizes on futures, delivery and options are **contract counts**, sent as a
signed integer — positive for a buy, negative for a sell.

Order prices must sit on the instrument's tick grid. On almost every Gate.io
instrument the tick is a power of ten and any price of the right precision
qualifies, but a few contracts quote two decimals and tick in `0.05` (the
`BNB_USDT` perpetual and the longer-dated `ETH_USDT` delivery contracts).
`Instrument.make_price()` rounds to the *precision*, not to the tick, so it can
return an off-grid price on those; every instrument this adapter builds carries a
tick scheme, so use `instrument.next_bid_price()` / `next_ask_price()` to price
on the grid. An off-grid price is rejected before the request, on submit and on
amend.

One translation is worth singling out because it is not literal:

* **A base-denominated spot market buy becomes an aggressive limit.**
  Gate.io's native spot market buy spends a *quote* amount, so it cannot express
  "buy exactly this many base units". Converting the quantity behind the caller's
  back would change the order, so it is sent as a limit order priced through the
  book by the pair's **own published `slippage` cap** (5% if the pair definition
  carries none); the venue fills at or better than that bound and cancels the
  remainder. The substitution is logged at INFO with the reference price and the
  cap. The reference price is the cached ask, then the cached last trade, then
  the cached quote mid, then the REST ticker.

  The price is the only thing substituted. The order's own time in force rides
  along, so a `MARKET`/`FOK` buy goes out as a `fok` limit and stays
  all-or-nothing instead of being downgraded to immediate-or-cancel.

### Nothing is silently altered

Every case Gate.io cannot express is rejected with a stated reason instead of
being changed into something the venue does accept:

| Situation | Result |
|---|---|
| GTD, DAY, AT_THE_OPEN or AT_THE_CLOSE on a limit order | rejected, naming the supported set (GTC, IOC, FOK, and post-only via `poc`) |
| On any market order, a time in force other than GTC, DAY, IOC or FOK — and on options, FOK as well | rejected; spot goes through the same mapping as the other three products, so AT_THE_OPEN and AT_THE_CLOSE are refused there too |
| `reduce_only` on a spot order | rejected — reduce-only is a derivatives concept and dropping it changes the order |
| `quote_quantity=True` anywhere but a spot market buy | rejected |
| `quote_quantity=True` on a spot market **sell** | rejected — Gate.io market sells take a base amount |
| post-only on a market order | rejected |
| post-only combined with IOC or FOK | rejected — `poc` is a resting maker-only order, so the immediacy could not survive the substitution |
| FOK on an options order | rejected — the venue offers `gtc`, `ioc` and `poc` there |
| Fractional contract quantity on a derivative | rejected — contracts are whole |
| `display_qty=0` (a fully hidden order) | rejected — Gate.io reads `iceberg=0` as a normal, fully displayed order and does not support hiding the whole amount |
| A fractional `display_qty` on a derivative | rejected — the iceberg quantity is a contract count, and truncating it to zero would display the whole order |
| A price or trigger price off the instrument's tick grid | rejected — the venue accepts on-tick prices only, and moving the price to the nearest tick would submit a different order |
| Price-triggered order on options | unsupported — the venue has no such endpoint for options |
| Price-triggered spot order under `CROSS_MARGIN` | rejected — the venue's spot price-trigger endpoint has no cross-margin ledger |
| post-only or `display_qty` on a price-triggered order | rejected — the fired order accepts neither, and dropping the flag would submit a materially different order |
| Trigger type other than LAST_PRICE, MARK_PRICE or INDEX_PRICE on futures or delivery | rejected |
| Trigger type other than DEFAULT or LAST_PRICE on **spot** | rejected — the spot trigger object is `{price, rule, expiration}`, with no price-type field, and spot has no mark or index price to name |
| A conditional order whose trigger price contradicts its order type | rejected — see the comparison rule below |
| A base-denominated spot market buy with no reference price available | rejected, suggesting `quote_quantity=True` |
| Amending the trigger price of a working order | rejected — the venue cannot do it |

One exception to the rule, stated here rather than buried, and it is by design:

* **GTD is accepted on a conditional order**, where it describes how long the
  trigger stays armed rather than how long the fired order rests. It is sent as
  `trigger.expiration` in seconds, and an expire time already in the past is
  rejected. On a regular order GTD is rejected. Know the limit of the
  approximation: if the trigger fires shortly before `expire_time`, the order it
  creates rests as `gtc` and outlives the expiry, because Gate.io's price-order
  endpoints carry no expiry for the *fired* order. A strategy that used GTD to
  bound its exposure should set `manage_gtd_expiry=True`, which makes
  NautilusTrader keep its own timer and cancel at `expire_time`.

### The comparison rule and the order type

Gate.io models no difference between a stop and an if-touched order: the trigger
carries a bare comparison rule, and the venue requires that rule to agree with
the current last price (rule `1`, fire at or above, needs a trigger above the
market; rule `2`, at or below, needs one below it). One rule per order is
therefore submittable, and it is the one the market implies.

That is enough for a well-formed order, where the market-implied rule and the
rule the order type implies are the same: a stop sits away from the market in
the direction of the trade, an if-touched order towards it. When they disagree —
a BUY `STOP_MARKET` whose trigger is below the market, or a stop whose level the
market has already run through — the only rule Gate.io accepts is the one that
encodes the *other* conditional type, so the order is rejected with both rules
named instead of being armed as something else. Refusing costs nothing that was
available: the alternative was never "submit it as asked" but a venue rejection.
If what you want is a conditional order that is already in the market, emulate
it (see [Order emulation](products.md#order-emulation)) — the platform releases
it immediately.

With no last price to hand (no cached trade tick, quote mid or REST ticker) the
order type decides the rule on its own and the venue makes the final call.

### Modification

`ModifyOrder` maps to Gate.io's amend endpoints, with explicit rejections where
the venue has no equivalent:

| Case | Result | Status |
|---|---|---|
| Spot | `PATCH /spot/orders/{id}` with the new amount and/or price | implemented and mock-tested |
| Perpetual, inverse | `PUT /futures/{settle}/orders/{id}` with the signed size and/or price | implemented and mock-tested |
| Delivery | rejected — the venue cannot amend delivery orders | unsupported |
| Options | rejected — the venue cannot amend options orders | unsupported |
| An armed price-triggered order | rejected — cancel and resubmit | unsupported |
| A new trigger price | rejected — the venue cannot amend a working order's trigger | unsupported |
| Neither quantity nor price given | rejected | not applicable |
| Contract quantity change while the order is not in the cache | rejected — the side determines the sign of `size`, and guessing it could flip a short into a long | not applicable |

A fired conditional order is amendable like any other order on the products that
support amendment: the amend addresses the fired id, not the armed one.

### Cancellation

* `cancel_order` — cancels one order by venue order id, taking the price-trigger
  id space into account: an order still armed is disarmed by its armed id, an
  order that has fired is cancelled by its fired id.
* `cancel_all_orders` — cancels per product and instrument, including armed
  price-triggered orders. Neither price-order endpoint accepts a side filter, so
  a **side-scoped** command disarms the matching price orders one at a time, by
  id, rather than bulk-disarming both sides of the book.
* `batch_cancel_orders` — batched in groups of 20 where the venue supports it
  (spot only), falling back to individual cancels elsewhere and for armed
  trigger orders. Per-item failures — the `succeeded=false` entries of a
  successful response — are reported as individual `OrderCancelRejected` events
  with the venue's own label and message. A failure of the whole request carries
  no per-order result, so it is reported for none of them: unless the venue
  refused the request outright, every order in the chunk stays `PENDING_CANCEL`
  (see [Unknown outcomes](#unknown-outcomes)).

A bulk price-order cancel answers with price-order objects, not regular orders.
Those are matched back to this client's armed orders and closed explicitly,
rather than being pushed through the order-payload path where they would not be
understood.

### Rejection, denial and expiry

**Who refused the order decides which event says so.** `OrderDenied` is the
platform's event for an order *Nautilus* will not submit — "denied by Nautilus
for being invalid, unprocessable, or exceeding a risk limit", transitioning
`INITIALIZED -> DENIED`. `OrderRejected` means the *venue* refused a submission:
"rejected by the trading venue", `SUBMITTED -> REJECTED`. This client keeps the
line by building the whole request before announcing anything, so every refusal
it makes on its own is decided while the order is still `INITIALIZED`.

The platform enforces the ordering: its state table reaches `DENIED` from
`INITIALIZED` and `RELEASED` only, so a refusal announced after `OrderSubmitted`
could not be expressed as a denial at all.

| Outcome | Nautilus event |
|---|---|
| Unknown instrument, unconfigured product, unsupported order type | `OrderDenied`, before any network call |
| Any instruction Gate.io cannot express (the [refusal table](products.md#what-is-refused-rather-than-translated)) | `OrderDenied` — decided while the request is built, so nothing is sent |
| Any other failure while building the request | `OrderDenied`, naming the failure |
| Venue **refusal** on submission (a 4xx answer) | `OrderRejected`, carrying the venue's label and message |
| Post-only order that would have taken liquidity | `OrderRejected` with `due_post_only=True` |
| Submission whose outcome the venue never confirmed | nothing — see [Unknown outcomes](#unknown-outcomes) |
| `finish_as=expired` | `OrderExpired`, unless the quantities say the order completed |
| `finish_as=cancelled`, `reduce_only`, `reduce_out`, `position_closed` | `OrderCanceled` |
| Unfilled remainder of an `ioc`, `fok` or self-trade-prevention order | `OrderCanceled` (`OrderFilled` if it in fact filled completely) |
| `finish_as=liquidated` or `auto_deleveraged` | `OrderCanceled` — Gate.io defines both as cancellations, and what the position did is carried by the fills on the trade channel |

Those last rows share one rule: **the quantities decide whether an order
completed; the reason only explains a non-completion.** A terminal message whose
filled amount has reached its total is read as filled whatever reason it
carries, and only below that total does the reason choose between expiry and
cancellation. Reading the reason first fails in both directions — a completed
order closed as `EXPIRED` has no transition left for the fill that completed it,
and an untouched order closed as `FILLED` is never closed by anything.

An `OrderDenied` is terminal, and that is the point: nothing was sent, so there
is nothing at the venue to reconcile and the strategy is told so definitively
instead of inferring it from a rejection that names Gate.io.

Post-only rejection is detected on both paths the venue uses: the error labels
`ORDER_POC_IMMEDIATE` and `POC_FILL_IMMEDIATELY` on the submission response, and
`finish_as=poc` on a terminal order message. Both produce a rejection flagged as
post-only, so NautilusTrader can treat it as the non-event it usually is rather
than as a failure. A `finish_as=poc` message for an order this client has
already booked a fill against is reported as `OrderCanceled` instead: the
platform has no `PARTIALLY_FILLED -> REJECTED` transition, both sides agree the
order is finished, and cancellation is the transition it accepts.

Expiry and cancellation are emitted only when the order is not already closed,
so a duplicate terminal message from the venue cannot drive the order's state
machine twice. The per-order bookkeeping (the `text` alias, the applied trade
ids and the trigger link) is dropped at the same point.

### Events this client generates, and events it does not

An execution client's event surface is defined by the platform, not by the
venue. All twelve events an `ExecutionClient` can publish in 1.230.0 are
generated here: `AccountState`, `OrderDenied`, `OrderSubmitted`,
`OrderRejected`, `OrderAccepted`, `OrderModifyRejected`, `OrderCancelRejected`,
`OrderUpdated`, `OrderCanceled`, `OrderTriggered`, `OrderExpired` and
`OrderFilled`.

The rest of the event model belongs to other components, and this client
deliberately produces none of it:

| Event | Produced by |
|---|---|
| `OrderInitialized` | `OrderFactory`, when the strategy creates the order |
| `OrderEmulated`, `OrderReleased` | `OrderEmulator` |
| `OrderPendingUpdate`, `OrderPendingCancel` | `Strategy`, before the command reaches this client |
| `PositionOpened`, `PositionChanged`, `PositionClosed` | `ExecutionEngine`, derived from fills |
| `PositionAdjusted` | `Position.apply_adjustment`, inside the model |

Gate.io pushes position updates on a private channel; they are parsed and logged
but never published, because REST reconciliation is the single source for
positions and a second view would race the engine's own fill-driven derivation.

`OrderFillVoided` is documented upstream but does **not** exist in 1.230.0 —
no event, no `VOIDED` order status, no generator. Gate.io does void trades
(self-trade-prevention reversals, erroneous-trade cancellations), so until the
platform ships the event a voided trade leaves an uncorrectable `OrderFilled` in
the order and position history. The version floor is pinned by a test, so the
build that lifts it will not pass unnoticed.

### Unknown outcomes

A rejection event states a fact about the venue: it refused the command.
NautilusTrader therefore allows `OrderRejected`, `OrderCancelRejected` and
`OrderModifyRejected` only where the venue's answer proves that refusal
(concepts/live.md, "Order command outcome policy"). Everything else is
*ambiguous*, and an ambiguous command is logged and left in flight — `SUBMITTED`,
`PENDING_CANCEL` or `PENDING_UPDATE` — for the execution engine's in-flight
check, open-order poll or reconciliation to resolve. This client emits no event
at all in that case.

| Failure | Classified as | Why |
|---|---|---|
| A 4xx answer from Gate.io (including 429) | definitive | the venue answered, and the answer is a refusal |
| A failure before any byte was sent (`NETWORK_ERROR`) | definitive | the venue cannot have seen the request |
| The adapter's own pre-flight refusal | definitive | nothing was sent |
| `GateioRequestAmbiguousError` — sent and unanswered, replayed or not | ambiguous | the venue may have applied it |
| A 5xx answer | ambiguous | Gate.io can raise it before or after applying the request |
| A whole-batch failure with no per-order results | ambiguous | it says nothing about the individual cancels |
| Anything the client could not read after the request went out | ambiguous | including a price order the venue armed without returning its id |

The distinction is not caution, it is recoverability. `OrderRejected` is
terminal, so an order Gate.io is actually holding could never be represented
locally again — reconciliation's `OrderAccepted` is refused by the order's state
machine — and the position would drift for the life of the process.
`OrderCancelRejected` and `OrderModifyRejected` are not terminal but revert the
order to `ACCEPTED` carrying state the venue no longer has, and nothing later
re-compares price or quantity: the open-order poll reconciles an order only when
its open state or filled quantity disagrees with the venue.

What the engine then does with a `PENDING_CANCEL` or `PENDING_UPDATE` order it
cannot confirm is the engine's decision, and the platform's own sources differ:
live.md's in-flight timeout table leaves both unresolved, while the installed
1.230.0 generates `OrderCanceled` for both once `inflight_check_retries` is
spent. The installed behaviour is what runs. Either way that decision is taken
after querying the venue, which is more than this client knows when the request
fails.

This client does not query the venue itself after an ambiguous failure.
`LiveExecutionEngine` already re-queries every in-flight order through
`generate_order_status_report`, bounded by `inflight_check_retries`; a second
poll here would race it.

That query carries the client order id and, for a `SUBMITTED` order, **no venue
order id** — the venue id is only assigned by `OrderAccepted`, which is exactly
the event an ambiguous submit never received. It is answered on every product.
The client order id travels in the order's `text`, which is registered while the
request body is built rather than when the response arrives, so it is known even
when nothing came back at all. Gate.io resolves that `text` in place of the venue
id on the spot and perpetual single-order endpoints; on delivery and options,
whose single-order endpoints document the venue id only, the order is located in
the product's own order listings, where every row carries its `text`. Resting
orders are searched first and recently finished ones after, so a submit that
filled before its answer was lost is found as well.

## Fills

* The Nautilus `TradeId` is the **venue trade id** from `*.usertrades` /
  `my_trades`. Trade ids are never synthesised, because NautilusTrader's own
  duplicate-fill guard is keyed on them: a synthesised id would make the same
  fill applicable twice across the WebSocket and REST paths.
* Applied trade ids are additionally remembered per order inside the client, so
  a replayed `usertrades` message cannot fill twice even before the framework
  sees it.
* Partial and multiple fills need no special handling: each fill is one
  `OrderFilled` with its own trade id, quantity, price, commission and liquidity
  side, and the order closes when the venue's own accounting says it is complete.
* A zero-quantity fill, or one with no venue trade id, is discarded with a log
  line rather than being turned into an event with an invented identity.
* On spot, a fee charged in the currency being received is **not** netted off the
  fill quantity, and the order status reports do not net it either. Gate.io does
  credit `amount - fee` base units for a match of `amount`, but NautilusTrader
  accounts for that itself: `Position.apply` raises a
  `PositionAdjusted(COMMISSION, -commission)` for every fill on a `CurrencyPair`
  commissioned in its base currency, and subtracts it from the position. An
  adapter that also nets it off `last_qty` has the fee taken off twice, and every
  spot BUY position ends short by the cumulative fee. `last_qty` and `commission`
  are two independent facts and are reported as two independent facts; the report
  states the same gross quantities, so reconciliation never has to square the two
  accounts of one order against each other.
* Fee amount and currency are reported exactly as the venue returns them. Spot
  fees are usually charged in the received currency and may be charged in GT when
  GT-fee deduction is enabled on the account. A derivative fill is commissioned in
  the instrument's settlement currency; a payload that carries no `fee` field at
  all is reported with a zero commission rather than an invented one.
* A quote-denominated spot market buy is restated in base units at the venue's
  own fill price before its first fill is applied, so that position and PnL
  arithmetic downstream works in one unit.

## Conditional order identity

This is the subtlest thing the client does, and it is what makes conditional
orders on Gate.io correct rather than merely functional.

Gate.io's price-triggered orders ("auto orders") live in **their own id space**.
The venue arms one under one id and, when the trigger fires, creates a
**brand-new order with a different id**. From that moment every order update,
every cancel and every fill names the new id. Both identities stay meaningful
for the whole life of the order:

| Identity | What it is for |
|---|---|
| **armed id** | The only handle that can disarm the order, and the only key the venue's price-order listings answer to — which is what makes the link rebuildable after a restart |
| **fired id** | The id every subsequent order update, cancel and fill carries |

Discarding either one loses the order. The client therefore keeps both, in a
`GateioTriggerLink`, indexed in each direction:

```text
armed id  <->  client order id  <->  fired id
```

```python
from nautilus_gateio.execution import GateioTriggerLink
```

The live map is readable as `GateioExecutionClient.trigger_links` (a copy, keyed
by `ClientOrderId`), which is the quickest way to see what the client believes
about an order that has just fired.

### The transition goes through `OrderUpdated`

When a message arrives naming an id the order does not hold, the client rebases
the order onto the new id by emitting `OrderUpdated` with
`venue_order_id_modified=True`. That is deliberate and it is not decoration:
**`OrderUpdated` is the only event NautilusTrader's `Order.apply` accepts
carrying a venue order id different from the one already on the order.** Emit
`OrderTriggered` or `OrderFilled` first and the state machine rejects it — and
every later event for that order with it, including all of its fills.

The order of operations is:

1. Attach the fired id to the link, keeping the armed id.
2. Write the new mapping into the Nautilus cache synchronously
   (`add_venue_order_id(..., overwrite=True)`). This matters because the `Order`
   object is only updated once the execution engine has applied the
   `OrderUpdated`, and several fills for a freshly fired order can be handled
   before that happens. The cache mapping is the one signal that does not depend
   on event delivery, and it is what stops the rebase being emitted twice.
3. Emit `OrderUpdated` with `venue_order_id_modified=True`.
4. Emit `OrderTriggered` — but only for the order types NautilusTrader considers
   triggerable (`STOP_LIMIT`, `LIMIT_IF_TOUCHED`). For stop-market style orders
   the rebasing update *is* the whole transition.

Read `GateioTriggerLink` and `_maybe_swap_trigger_venue_order_id` in
`nautilus_gateio/execution.py` if the ordering matters to you. The regression
suite for the transition is `TestTriggerVenueOrderIdRebase` in
`tests/test_execution_events.py`; the identity suites are in
`tests/test_execution_triggers.py`.

### Rebuilding the link after a restart

A restart loses the in-memory map, so it is rebuilt from the venue rather than
assumed. What the venue offers differs by product, and spot is the hard case:

| Product | Client id on the price order | Fired id on the price order |
|---|---|---|
| Perpetual, inverse, delivery | `initial.text` echoes the `t-` client id | `trade_id` (absent or `0` until it fires) |
| Spot | none at all — `put.text` is an order-source marker such as `api` | `fired_order_id` |

On spot the fired order carries no client id either, so `fired_order_id` on the
armed price order is the *only* link between the Nautilus order and the order
that is actually working at the venue. Reconciliation therefore lists **armed
and finished** price orders on every product: a finished price order produces no
report of its own — the order it fired is reported as a normal order — but it
restores the identity map, without which that normal order would be reported as
somebody else's.

Cancel and amend follow the same map after a restart: an order still armed is
disarmed by its armed id, an order that has fired is addressed by its fired id.

### Reporting the order a trigger fired

Gate.io keeps the trigger on the armed price order and none of it on the order
that fires, so the venue's statement about that second object is "an accepted
limit order with no trigger". Handed on unchanged it collides with the Nautilus
order it belongs to, which is a `STOP_LIMIT` that has already been `TRIGGERED`,
and the collision repeats on every reconciliation pass: the engine attempts
`TRIGGERED -> ACCEPTED`, which the order state machine refuses, and
`_should_update` compares the report's absent trigger price against the order's
real one and publishes an `OrderUpdated` for an amendment nobody made.

So the report of a fired order is restated from the link: it carries the trigger
price and trigger type of the Nautilus order, and it reports `TRIGGERED` rather
than `ACCEPTED` while it rests — for `STOP_LIMIT`, `LIMIT_IF_TOUCHED` and
`TRAILING_STOP_LIMIT`, the types NautilusTrader has that state for. A market-style
stop keeps the venue's own status, since the platform has no `TRIGGERED` state for
it. `ts_triggered` is carried only while the local order has not recorded the
trigger itself — that field is how a trigger which fired while this client was
down is recovered, and repeating one the order already applied would be dropped
by the state machine.

Status: implemented and mock-tested, including the restart-across-the-transition
path (`TestRestartAcrossTheTriggerTransition` in
`tests/test_execution_triggers.py`) and the fired-order report
(`TestFiredConditionalOrderReports` in `tests/test_execution_reports.py`).
Mainnet validation pending.

## The fill-before-order race

Gate.io publishes `*.orders` and `*.usertrades` on **independent channels with
no ordering between them**. In practice the first message that mentions a fired
conditional order is frequently its fill. A client that assumes the order message
comes first will emit that fill against the armed id, `Order.apply` will refuse
it, the execution engine will swallow the resulting exception into a log line,
and the fill is simply gone — the position then disagrees with the venue with no
loud failure anywhere.

The client resolves the identity from the fill path itself:

1. **From the venue `text`**, when there is one. A value that does not start with
   `t-` is one of Gate.io's own source markers (`api`, `web`, `liquidation`) and
   is never treated as a client id.
2. **From the trigger links**, by fired id and then by armed id. The links are
   consulted *before* the cache index, because a fired conditional order is a new
   venue object and the cache still maps the client order id to the armed id
   until the rebasing `OrderUpdated` has been applied.
3. **From the cache index**, by venue order id.

If that yields the order, the rebase described above runs *before* the fill is
emitted, so the `OrderFilled` carries the identity the order will hold.

### When the identity cannot be resolved inline

Two cases, and neither one drops the fill:

* **No identity at all.** A spot fired order arrives with no usable `text` and an
  id nothing knows. Rather than report it as an external order — which would
  strand the Nautilus order that is in fact its parent — the client re-reads its
  own armed price orders for that product and instrument from REST and matches
  them on `fired_order_id`. The attempt is remembered per venue order id so it
  happens once; if no armed order matches, the order genuinely is external and is
  reported as such, with a warning. If there are no armed candidates at all, no
  re-read is scheduled and the fill goes down the external-order path
  immediately — which asks the venue for the order the trade belongs to and hands
  the two over together as an `ExecutionMassStatus`. A `FillReport` on its own
  would not survive: `LiveExecutionEngine._reconcile_fill_report_single` resolves
  the order only through `cache.client_order_id(report.venue_order_id)`, and on an
  id that index has never seen it logs "deferring reconciliation" and drops the
  trade, with no deferral queue and no retry behind it.
* **An identity this client cannot explain.** The fill names an order the client
  knows — through the `text` alias registered for it — but carries a venue order
  id that order does not hold. That is the venue speaking about a replacement
  object it created, so the identity is **rebased** onto it through
  `OrderUpdated`, with a warning, and the fill is then emitted normally.
  Reconciliation is not an option here and never was: `create_order_filled_event`
  stamps the reconciled fill with `report.venue_order_id`, so `Order.apply`
  refuses it there for exactly the same reason, and `_reconcile_fill_report` turns
  the resulting `ValueError` into a log line.

### A fill that arrives after the order was closed

`Order.apply` refuses an `OrderFilled` on a closed order, so a late fill can only
be booked through reconciliation — and only from a status NautilusTrader's own
order state table can still leave. Among the terminal statuses it holds exactly
two such entries, `(CANCELED, PARTIALLY_FILLED)` and `(CANCELED, FILLED)`, both
annotated "Real world possibility". That covers the ordinary case, an IOC/FOK
cancellation whose `*.orders` message beats its `*.usertrades` message.

There is no transition out of EXPIRED, REJECTED or DENIED for a fill. Handing one
of those to reconciliation raises `InvalidStateTrigger` inside
`_reconcile_fill_report`, which catches it, logs it and returns False — the
execution is discarded either way, but through reconciliation it is discarded
under a generic error against a report nobody reads. Those are reported by the
client instead, at ERROR, naming the trade id, quantity, price and commission, so
the position can be squared. This is a real gap, not a handled case: the venue
traded, and NautilusTrader cannot be told about it on this version.

Status: implemented and mock-tested. The regression suite is
`TestFillBeforeOrderUpdate` in `tests/test_execution_events.py`, which covers a
fill before any order message, several fills before it, a late order message
(idempotent), a duplicate fill after the rebase, a reconnect between the fill and
the order message, and a restart between them; `TestFillOnClosedOrder` covers the
late fill on a canceled and on an expired order and holds the fillable-terminal
status set against the platform's own state machine. Mainnet validation pending.

## Balances, positions and funding

**Balances** are aggregated per currency across the wallets of the enabled
products, from the balance stream and from the REST poll. Three things about the
aggregation are worth stating:

* A stream update for one wallet never overwrites another wallet's contribution;
  balances are tracked per wallet and only then summed.
* A **unified account** reports one cross-product balance per currency that
  already contains the per-product wallets, while every one of those wallets
  keeps answering its own endpoint with the same funds. Summing them would
  multiply the account's equity by the number of enabled products, so a currency
  the unified wallet reports **replaces** the per-product wallets instead of
  being added to them.
* Margin ledgers subtract borrowed principal and accrued interest from the total,
  and the futures wallet total includes unrealised PnL. Free is clamped to total,
  and the difference is published as locked.

An account that has never been funded returns no rows at all. Rather than fail to
register, the client warns and reports the settlement currency of each enabled
product as zero, which states exactly that.

**Positions** are reconciled from REST only, for futures, delivery and options
(netting). When a position report is requested for a specific instrument and the
venue returns nothing, an explicit FLAT report is emitted — otherwise a stale
local position could never be closed.

That rule is meant to cover only the case where the venue answered, and it
currently reaches past it: a position query the venue refuses for a permission or
account-mode reason (`FORBIDDEN`, `INVALID_UNIFIED_ACCOUNT`,
`UNIFIED_ACCOUNT_NOT_ACTIVATED`) is folded into the same path as a wallet the
account has never opened, and a refusal then reads as flat. A position the venue
still holds can be closed locally that way, by an execution nobody made. Open and
known; until it is fixed, give the API key its futures read permission before
relying on position reconciliation.

**Funding** is *not applicable* as an execution event: the client emits no
funding cash-flow event. Realised funding is reflected in the futures wallet
balance, and therefore in the next `AccountState` from the balance stream or the
account poll. Funding *rates* are market data, not execution — see
[market-data.md](market-data.md).

Status: balance aggregation across spot, margin, unified and futures wallets is
implemented and mock-tested; the options balance and options report paths are
implemented, mainnet validation pending, and are not covered by the execution
test suite.

## Reconciliation and restart

All four report generators are implemented against REST:

| Method | Source |
|---|---|
| `generate_order_status_reports` | open plus recently finished orders across every enabled product, paginated, including armed price-triggered orders |
| `generate_order_status_report` | single lookup by venue order id or client order id, on every product, following the armed or fired id as appropriate |
| `generate_fill_reports` | `my_trades` per product over the lookback window, narrowed to one order when the command names a venue order id, sorted by `(ts_event, trade_id)` because reconciliation applies fills in list order |
| `generate_position_status_reports` | futures, delivery and options positions (netting) |

### Startup

`connect()` runs in a fixed order: load instruments and publish them into the
cache, read the account user id, check the position mode, refresh account state,
wait for the account to register, then open the private sockets and start the
account poll. The execution engine asks for reports once the client reports
connected, so reconciliation cannot race the instrument cache or the account
registration it depends on.

The engine's `reconciliation_lookback_mins` sets the window. When it passes none,
the client uses its own default of **24 hours**.

Restarting the node while orders rest on the venue is a supported path, but the
venue remains the source of truth: an order the local cache never saw is adopted
from the report, not invented. Whether NautilusTrader adopts an unrecognised
order at all is the engine's decision, governed by `external_order_claims` on the
strategy and `filter_unclaimed_external_orders` on the execution engine — this
client's job is to report it accurately, which includes reporting it with no
client order id when the venue supplies none.

Keeping Nautilus client order ids within Gate.io's `text` limit is what makes a
restart able to re-identify resting orders with no local state at all; see
[client order ids](#client-order-ids).

### Reconnect

**Gate.io replays nothing on reconnect.** There is neither replay nor resume on
any private channel, and no sequence numbers on `*.orders`, `*.usertrades` or
`*.balances`, so every transition that happened while the socket was down is
never delivered. Refreshing the account state alone would leave the order and
fill gaps unreconciled.

So on every reconnect the client re-runs the same REST queries reconciliation
uses, over the window from the last stream event it saw (bounded to the 24-hour
lookback) to now, and feeds the results back through the execution engine. The
subscriptions themselves are replayed by the WebSocket layer; this is about the
state that changed in between.

The results go over as **one** `ExecutionMassStatus`, each recovered trade
grouped under the order report it belongs to. That grouping is what lets the
engine restate the order's quantity from the venue's own snapshot *before* the
trade lands on it, which is the ordering both halves of the problem need: an
order report reconciled alone makes the engine invent a fill to account for a
quantity it cannot see, and a fill reconciled alone cannot restate anything.

Grouping also makes delivery of a trade conditional on the report it travels
with, so the client checks the outcome rather than trusting it: anything the
grouped pass did not book is re-offered afterwards, still with the venue's
statement of the order — either by handing that snapshot over first, or, when
the sweep has no usable snapshot (Gate.io publishes no base-denominated quantity
for a resting market buy, whose quantity is a quote cash amount), by re-reading
the order and handing the two over together. A trade re-offered on its own would
be applied against whatever quantity the local order still carries, and since
`OrderUpdated` triggers no state transition, an order whose quantity the venue
has moved on from could never reach a terminal status again.

That is what the code does; relying on it is another matter (*experimental*).
Four attempts at this path have each closed the case their own scenario named
and been refuted on another, and two defects are open in it now. When an outage
leaves a single order with more than one unbooked trade, the re-read hand-over
states the order's cumulative filled quantity beside a single trade, so the
engine accounts for the difference by inferring a fill of its own: the venue's
second trade id is never recorded, its commission is never withheld, and the
position ends up wrong by that fee. When the direct re-read lags the trade
listing, the same route can close the order at the partial quantity, and the
remaining venue-confirmed execution is then refused as an overfill — the order
being terminal, no later reconnect recovers it. One missed fill per order is
what the tests cover, and what the two paragraphs above describe accurately.

### Duplicate suppression

Everything above depends on the same fill being recognised as the same fill,
whichever path delivers it. That works because the `TradeId` is always the venue
trade id:

* NautilusTrader's execution engine skips a fill report whose trade id the order
  already carries.
* The client separately remembers the trade ids it has applied per order,
  including ones it routed through reconciliation, so a replay produces neither a
  second event nor a second report.
* Order reports are matched to the cached order by client order id and venue
  order id, both of which the conditional-order identity map keeps resolvable
  across the trigger transition.

### The paging bound, stated honestly

Gate.io caps a listing at 100 rows, so a single call would silently truncate
reconciliation to the newest 100 orders or fills. Every listing is therefore
paged, and paging stops on the first short page, or when a page already reaches
back past the start of the window, or at a hard cap of **20 pages** — that is
**2000 rows per listing**.

On reaching that cap the client logs a warning naming the listing and the number
of rows collected, and says that the window may be incompletely reconciled. It
does not fail, and it does not pretend the result is complete. If you see that
warning, the window you asked for is larger than one reconciliation pass can
cover on that endpoint; narrow it.

The futures and delivery fill and finished-order endpoints accept no time range
at all, so their windows are walked by row offset and stopped as soon as a page
contains a row older than the requested start.

## Client order ids

The Nautilus `ClientOrderId` travels in Gate.io's `text` field, which must start
with `t-`, hold at most 28 further characters and use only `[0-9A-Za-z_.-]`.

* An id that fits is embedded verbatim, so the mapping is recoverable from the
  venue alone after a restart.
* An id that does not fit is replaced by a generated id
  (`t-<tag>-<counter>`, tag from `client_order_id_tag`) and the pair is kept in
  an in-memory alias table. After a restart that alias is gone, and such an order
  is reported as an unknown (external) order rather than decoded into an id
  Nautilus never issued.

Keeping client order ids within the venue's limit is therefore worth doing: it is
the difference between a restart that re-identifies its own resting orders and
one that adopts them as somebody else's.

## Transfers

```python
await exec_client.transfer(
    currency="USDT",
    from_="spot",
    to="futures",
    amount="25",
    settle="usdt",         # required when either end is a contract wallet
    # currency_pair="BTC_USDT",  # required when either end is isolated margin
)
```

Only the account's own trading wallets are valid endpoints; the request carries
no address and no recipient, so it cannot send funds out of the account. Gate.io
routes every internal transfer through the spot wallet, so moving between two
derivative wallets takes two calls. A derivative wallet is created by the first
transfer into it. Account state is refreshed after a successful transfer.

Status: implemented, mainnet validation pending. The wallet endpoint underneath
is mock-tested — body construction, rejection of non-internal wallets, the
`settle` requirement, and the guarantee that a transfer is never replayed after a
timeout or a 5xx — but the execution-client wrapper is not covered by the
execution tests.

## Operational notes

* Options and delivery orders cannot be amended; strategies that rely on
  amendment should cancel and resubmit on those products.
* Rate limiting is applied client-side, and a `429` is retried with backoff.
  Order-mutating requests are **never** replayed automatically unless the venue
  has stated that the request was rejected before it was processed; when the
  outcome is genuinely unknown the error raised says so, and the remedy is to
  query the order rather than to resubmit it. The execution client acts on that
  distinction rather than reporting every failure as a rejection — see
  [Unknown outcomes](#unknown-outcomes).
* One bad payload never kills the stream or the batch: a message that cannot be
  parsed is logged and skipped, and a report that cannot be built is dropped with
  a warning naming the order.
* An instrument the provider was not started with is **loaded** rather than
  ignored when reconciliation encounters it. Venue state is not discarded because
  a filter was narrower than the account's actual history; if the definition
  cannot be fetched either, the loss is logged as an error rather than passing
  silently.
* Nothing on this page has been exercised against Gate.io itself. Start on the
  testnet, then start small, and record what you find in
  [validation.md](validation.md).
