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

`OrderSubmitted` is generated before the REST call. The venue's response to that
call is fed through **the same handler as the WebSocket order stream**, so the
REST path and the stream path cannot drift apart in how they interpret an order
object.

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
| LIMIT | `price`, `tif` `gtc`/`ioc`/`fok`; post-only maps to `poc` |
| STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED | the product's price-triggered ("auto order") endpoint |

Supported order types are exactly:

```python
from nautilus_gateio.execution import CONDITIONAL_ORDER_TYPES, SUPPORTED_ORDER_TYPES
```

`SUPPORTED_ORDER_TYPES` = MARKET, LIMIT, STOP_MARKET, STOP_LIMIT,
MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED. Anything else is **denied** before a
network call, with the reason on the `OrderDenied` event.
`CONDITIONAL_ORDER_TYPES` is the subset routed to the price-triggered endpoint.

Time in force: GTC, IOC and FOK, plus post-only through `poc`. Flags honoured:
`reduce_only` (derivatives only), `display_qty` (iceberg, regular orders only),
`quote_quantity` (spot market buy only). Sizes on futures, delivery and options
are **contract counts**, sent as a signed integer — positive for a buy, negative
for a sell.

One translation is worth singling out because it is not literal:

* **A base-denominated spot market buy becomes an aggressive IOC limit.**
  Gate.io's native spot market buy spends a *quote* amount, so it cannot express
  "buy exactly this many base units". Converting the quantity behind the caller's
  back would change the order, so it is sent as an immediate-or-cancel limit
  priced through the book by the pair's **own published `slippage` cap** (5% if
  the pair definition carries none); the venue fills at or better than that bound
  and cancels the remainder. The substitution is logged at INFO with the
  reference price and the cap. The reference price is the cached ask, then the
  cached last trade, then the cached quote mid, then the REST ticker.

### Nothing is silently altered

Every case Gate.io cannot express is rejected with a stated reason instead of
being changed into something the venue does accept:

| Situation | Result |
|---|---|
| GTD, DAY, AT_THE_OPEN or AT_THE_CLOSE on a limit order | rejected, naming the supported set (GTC, IOC, FOK, and post-only via `poc`) |
| On a futures or delivery market order, a time in force other than GTC, DAY, IOC or FOK; on an options market order, anything but GTC, DAY or IOC | rejected |
| `reduce_only` on a spot order | rejected — reduce-only is a derivatives concept and dropping it changes the order |
| `quote_quantity=True` anywhere but a spot market buy | rejected |
| `quote_quantity=True` on a spot market **sell** | rejected — Gate.io market sells take a base amount |
| post-only on a market order | rejected |
| FOK on an options order | rejected — the venue offers `gtc`, `ioc` and `poc` there |
| Fractional contract quantity on a derivative | rejected — contracts are whole |
| Price-triggered order on options | unsupported — the venue has no such endpoint for options |
| Price-triggered spot order under `CROSS_MARGIN` | rejected — the venue's spot price-trigger endpoint has no cross-margin ledger |
| post-only or `display_qty` on a price-triggered order | rejected — the fired order accepts neither, and dropping the flag would submit a materially different order |
| Trigger type other than LAST_PRICE, MARK_PRICE or INDEX_PRICE on futures or delivery | rejected |
| A base-denominated spot market buy with no reference price available | rejected, suggesting `quote_quantity=True` |
| Amending the trigger price of a working order | rejected — the venue cannot do it |

Four exceptions to the rule, stated here rather than buried. The first is by
design; the last three are places where the client currently does adjust the
order, and you should know which:

* **GTD is accepted on a conditional order**, where it describes how long the
  trigger stays armed rather than how long the fired order rests. It is sent as
  `trigger.expiration` in seconds, and an expire time already in the past is
  rejected. On a regular order GTD is rejected.
* **A market order's time in force becomes `ioc`.** GTC and DAY carry no meaning
  for an order that cannot rest, so mapping them to `ioc` changes nothing the
  venue does, and FOK is honoured where the venue offers it. On futures,
  delivery and options anything else is rejected — but a **spot** market order
  takes a shorter path that coerces *every* time in force except FOK to `ioc`,
  so AT_THE_OPEN and AT_THE_CLOSE are accepted there and silently sent as `ioc`
  rather than rejected. Treat the spot market path as accepting IOC and FOK
  only.
* **`post_only=True` overrides the time in force.** It maps to `poc`, which is
  Gate.io's maker-only *resting* order. A post-only limit order submitted with
  IOC or FOK is therefore sent as `poc` and rests until cancelled instead of
  terminating immediately. Do not combine post-only with an immediate time in
  force and expect the immediacy to survive.
* **A spot conditional order ignores `trigger_type`.** Gate.io's spot
  price-order endpoint takes only a comparison rule against the last traded
  price, so there is no field to carry a mark- or index-price trigger. A
  non-default `trigger_type` on a spot conditional order is armed as a
  last-price trigger rather than rejected. On futures and delivery the trigger
  type is transmitted and an unsupported one is rejected.

The comparison rule itself follows the current market when a last price is known
(from the cached trade tick, the cached quote mid, or the REST ticker), because
that is what the venue validates the rule against. Without one it follows the
semantics of the Nautilus order type: a stop is placed away from the market in
the direction of the trade, an if-touched order towards it.

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
  trigger orders. Per-item failures are reported as individual
  `OrderCancelRejected` events with the venue's own label and message.

A bulk price-order cancel answers with price-order objects, not regular orders.
Those are matched back to this client's armed orders and closed explicitly,
rather than being pushed through the order-payload path where they would not be
understood.

### Rejection, denial and expiry

| Venue outcome | Nautilus event |
|---|---|
| Unsupported order type, unknown instrument, unconfigured product | `OrderDenied`, before any network call |
| Validation failure while building the request body | `OrderRejected` after `OrderSubmitted` |
| Venue error on submission | `OrderRejected`, carrying the venue's label and message |
| Post-only order that would have taken liquidity | `OrderRejected` with `due_post_only=True` |
| `finish_as=expired` | `OrderExpired` |
| `finish_as=cancelled`, `reduce_only`, `reduce_out`, `position_closed` | `OrderCanceled` |
| Unfilled remainder of an `ioc`, `fok` or self-trade-prevention order | `OrderCanceled` (`OrderFilled` if it in fact filled completely) |
| `finish_as=liquidated` or `auto_deleveraged` | treated as filled; the closing event is the fill on the trade channel |

Post-only rejection is detected on both paths the venue uses: the error labels
`ORDER_POC_IMMEDIATE` and `POC_FILL_IMMEDIATELY` on the submission response, and
`finish_as=poc` on a terminal order message. Both produce a rejection flagged as
post-only, so NautilusTrader can treat it as the non-event it usually is rather
than as a failure.

Expiry and cancellation are emitted only when the order is not already closed,
so a duplicate terminal message from the venue cannot drive the order's state
machine twice. The per-order bookkeeping (the `text` alias, the applied trade
ids and the trigger link) is dropped at the same point.

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
* On spot, a fee charged in the currency being received is netted off the fill
  quantity when `fee_currency == base_currency`, following the same convention as
  other NautilusTrader crypto adapters. The reports do the same netting on
  cumulative filled quantity, because a report that claimed more filled quantity
  than its own fills can reach would leave reconciliation with an order it can
  never close.
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

Status: implemented and mock-tested, including the restart-across-the-transition
path (`TestRestartAcrossTheTriggerTransition` in
`tests/test_execution_triggers.py`). Mainnet validation pending.

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
  immediately.
* **An identity that cannot be reconciled.** The fill names an order the client
  knows, but with a venue order id that is neither its current one nor one this
  client can explain. Emitting the fill would have it rejected and dropped, so it
  is routed through **reconciliation** instead — where an id mismatch is expected
  and resolved against the venue — and logged at ERROR, loudly enough to act on.
  The trade id is recorded, so a replay of the same fill does not produce a
  second report.

A fill that arrives after its order has already been reported closed is handled
the same way: `Order.apply` refuses an `OrderFilled` on a closed order, so the
fill goes through the reconciliation path, which knows how to apply it to an
order that has already finished.

Status: implemented and mock-tested. The regression suite is
`TestFillBeforeOrderUpdate` in `tests/test_execution_events.py`, which covers a
fill before any order message, several fills before it, a late order message
(idempotent), a duplicate fill after the rebase, a reconnect between the fill and
the order message, and a restart between them. Mainnet validation pending.

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
| `generate_order_status_report` | single lookup by venue order id or client order id, following the armed or fired id as appropriate |
| `generate_fill_reports` | `my_trades` per product over the lookback window, sorted by `(ts_event, trade_id)` because reconciliation applies fills in list order |
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
  query the order rather than to resubmit it.
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
