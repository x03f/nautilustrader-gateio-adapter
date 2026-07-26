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
package for NautilusTrader 1.230.0, written in pure Python, not an official
integration: it deliberately departs from the preferred in-tree Rust and PyO3
adapter architecture, and no migration to that architecture is promised. And it
is an alpha release — suitable for evaluation and controlled use, with limited
real-world validation behind it.

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

This is alpha software. **No capability listed on this page has been exercised
against the live venue**, on mainnet or on the testnet — see
[validation status](validation.md). Statuses below therefore describe how well a
claim is grounded in the repository, not how well it has survived contact with
real money.

| Status | Meaning here |
|---|---|
| Implemented and mock-tested | The behaviour is asserted by the test suite against stubbed venue responses or recorded payload shapes |
| Implemented, mainnet validation pending | The code path exists and was read, but no test asserts this specific behaviour |
| Experimental | Reachable through the lower-level HTTP or WebSocket classes, deliberately not wired into the NautilusTrader data or execution interfaces |
| Unsupported | The adapter refuses it, or no code exists for it |
| Not applicable | Gate.io does not have the concept for this product |

*Implemented and mock-tested* is a statement about the adapter agreeing with its
authors, never about Gate.io agreeing with the adapter. Only a live round trip
settles that, and none has been recorded.

## Products at a glance

| Product | `GateioProductType` | Instrument class | REST namespace | WebSocket endpoint | Testnet |
|---|---|---|---|---|---|
| Spot | `SPOT` | `CurrencyPair` | `/spot` (`/margin`, `/unified` for balances) | `wss://api.gateio.ws/ws/v4/` | yes |
| Perpetual (linear) | `PERP` | `CryptoPerpetual` | `/futures/usdt` | `wss://fx-ws.gateio.ws/v4/ws/usdt` | yes |
| Perpetual (inverse) | `INVERSE` | `CryptoPerpetual` (`is_inverse=True`) | `/futures/btc` | `wss://fx-ws.gateio.ws/v4/ws/btc` | no |
| Delivery future | `FUT` | `CryptoFuture` | `/delivery/usdt` | `wss://fx-ws.gateio.ws/v4/ws/delivery/usdt` | no |
| Option | `OPT` | `CryptoOption` | `/options` | `wss://op-ws.gateio.live/v4/ws` | no |

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

| Product | Instrument id | `Quantity` is | `size_precision` | Notes |
|---|---|---|---|---|
| Spot | `BTC_USDT.GATE_IO` | an amount of the **base** currency | from `amount_precision` | Except a market buy — see below |
| Perpetual (linear) | `BTC_USDT-PERP.GATE_IO` | a **number of contracts** | `0` | `multiplier` is the venue's `quanto_multiplier`, so `notional = contracts x multiplier x price` |
| Perpetual (inverse) | `BTC_USD-PERP.GATE_IO` | a **number of contracts** | `0` | Settles in the base currency; a `USD` quote is what marks a contract inverse. Gate.io sends `quanto_multiplier: "0"` here, so the face value falls back to one unit of the quote currency, and a contract publishing anything else is loaded with a warning |
| Delivery future | `BTC_USDT_20260807.GATE_IO` | a **number of contracts** | `0` | Expiry is in the symbol; `expiration_ns` comes from the contract's `expire_time`, and `activation_ns` from its `create_time` where the payload carries one, otherwise `0` |
| Option | `BTC_USDT-20260729-70000-C.GATE_IO` | a **number of contracts** | `0` | European, USDT-settled; premium is `price x multiplier x size` |

Because every derivative has `size_precision = 0`, a fractional contract
quantity cannot be expressed. The adapter rejects it rather than truncating it,
on submission and on amendment alike (*implemented and mock-tested*).

Instruments the venue reports as untradable are not published at all: spot pairs
marked `untradable` or currently one-sided around a listing or delisting window,
futures and delivery contracts that are delisting or inactive, and expired
delivery contracts. A one-sided spot pair is withheld because `CurrencyPair`
cannot express "buys only", and publishing it would let a strategy send the
disallowed side and collect an opaque venue rejection.

Gate.io lists thousands of option contracts. Restrict loading with
`options_underlyings=("BTC_USDT",)` unless you genuinely want all of them.

## Order types by product

`SUPPORTED_ORDER_TYPES` is the authority in code; this table is its per-product
reading.

| Nautilus order type | Spot | Perpetual / Inverse | Delivery | Options | Status |
|---|---|---|---|---|---|
| `MARKET` | native `type=market` for a sell or a quote-denominated buy; an aggressive IOC limit for a base-denominated buy | `price="0"` with `tif=ioc`/`fok` | as perpetual | `price="0"` with `tif=ioc` | Implemented and mock-tested |
| `LIMIT` | `type=limit` | `price` + `tif` | as perpetual | `price` + `tif` | Implemented and mock-tested |
| `STOP_MARKET` | `POST /spot/price_orders` | `POST /futures/{settle}/price_orders` | `POST /delivery/usdt/price_orders` | rejected | Implemented and mock-tested |
| `STOP_LIMIT` | as above | as above | as above | rejected | Implemented and mock-tested |
| `MARKET_IF_TOUCHED` | as above | as above | as above | rejected | Implemented and mock-tested |
| `LIMIT_IF_TOUCHED` | as above | as above | as above | rejected | Implemented and mock-tested |
| `MARKET_TO_LIMIT` | denied | denied | denied | denied | Unsupported |
| `TRAILING_STOP_MARKET` | denied | denied | denied | denied | Unsupported |
| `TRAILING_STOP_LIMIT` | denied | denied | denied | denied | Unsupported |
| Order lists (bracket, OCO, OTO) | — | — | — | — | Unsupported |

"Denied" and "rejected" are different events and the difference is visible to a
strategy. An order type the adapter cannot express is **denied** before anything
is sent, so the sequence is `OrderInitialized` -> `OrderDenied`. Anything caught
while building the request body — an unsupported time in force, an execution
instruction the endpoint has no field for, a fractional contract count — is
**rejected** after `OrderSubmitted`, so the sequence is `OrderInitialized` ->
`OrderSubmitted` -> `OrderRejected`. Both happen without a request reaching
Gate.io.

Order lists are unsupported in the plainest sense: the adapter implements no
order-list submission, so the inherited coroutine raises `NotImplementedError`.
Contingent orders have to be managed by the strategy.

### The spot market-order quirk

Gate.io spot market orders interpret `amount` differently per side: a market
**buy** spends a *quote* amount, a market **sell** delivers a *base* amount.
There is no venue field for "buy exactly this many base units".

| Nautilus order | Sent to Gate.io |
|---|---|
| `MARKET` SELL | `type=market`, `amount` = base quantity |
| `MARKET` BUY with `quote_quantity=True` | `type=market`, `amount` = quote amount |
| `MARKET` BUY with a base quantity | `type=limit`, `time_in_force=ioc`, priced through the book at the pair's own published `slippage` cap (5% if the pair publishes none) |

The third row is the only case on the regular order path where a Nautilus order
type reaches the venue as a different Gate.io order type — a zero price with
`ioc` on the contract products is Gate.io's own encoding of a market order, not
a substitution. Converting the quantity behind the caller's back would change
what was ordered; an immediate-or-cancel limit at the venue's own slippage bound
is the closest faithful expression, and the venue cancels any unfilled
remainder.
The reference price is taken from the far side of the cached quote, then the
cached last trade, then the cached quote's mid, then the venue ticker; if none of
those is available the order is rejected rather than priced by guesswork.

Fill quantities for such an order are read from `filled_amount` (base), never
from the submitted `amount`, which is why a partially filled quote-denominated
buy never restates the order quantity (*implemented and mock-tested*).

## Time in force by product

Gate.io accepts `gtc`, `ioc`, `poc` (post-only) and `fok`. Everything else has
no representation, and the adapter raises rather than downgrading — a downgrade
silently changes the execution guarantee the strategy asked for.

**Limit orders.** The accepted mappings are asserted from real request bodies
and the refusals from the mapping function, so this table is *implemented and
mock-tested*.

| `TimeInForce` | Spot | Perpetual / Inverse / Delivery | Options |
|---|---|---|---|
| `GTC` | `gtc` | `gtc` | `gtc` |
| `IOC` | `ioc` | `ioc` | `ioc` |
| `FOK` | `fok` | `fok` | rejected — Gate.io options have no `fok` |
| `GTD` | rejected | rejected | rejected |
| `DAY` | rejected | rejected | rejected |
| `AT_THE_OPEN`, `AT_THE_CLOSE` | rejected | rejected | rejected |
| any of the above with `post_only=True` | `poc` | `poc` | `poc`, except `FOK`, which is rejected first |

Post-only overrides the time in force, because `poc` is how Gate.io expresses
the maker-only constraint. That override is unconditional on spot, futures and
delivery: a `GTD` or `FOK` limit order that also sets `post_only=True` is sent as
`poc` and loses its expiry or its fill-or-kill guarantee rather than being
refused — see [substitutions](#where-the-adapter-substitutes-rather-than-refuses).

A post-only order the venue would have crossed comes back as `OrderRejected`
with `due_post_only=True`, both when the venue answers with a post-only error
label and when a later order message carries `finish_as=poc`.

**Market orders.** The `FOK` row is *implemented and mock-tested* on every
product, including the options refusal; the rest of this table is *implemented,
mainnet validation pending*.

| `TimeInForce` | Spot | Perpetual / Inverse / Delivery | Options |
|---|---|---|---|
| `GTC`, `IOC`, `DAY` | `ioc` | `ioc` | `ioc` |
| `FOK` | `fok` | `fok` | rejected |
| `AT_THE_OPEN`, `AT_THE_CLOSE` | `ioc` — see [substitutions](#where-the-adapter-substitutes-rather-than-refuses) | rejected | rejected |
| `GTD` | not constructible: NautilusTrader itself refuses `GTD` on a market order | | |

`GTC` and `DAY` collapse to `ioc` on every product because an order that cannot
rest has no meaningful resting duration; that is a re-expression of the same
instruction, not a change of guarantee.

**Conditional orders** (`STOP_*`, `*_IF_TOUCHED`) are different again, because
on a price-triggered order the Nautilus time in force describes how long the
*trigger* stays armed, while Gate.io expresses that with `trigger.expiration`
and lets the *fired* order carry its own `gtc` or `ioc`. The `GTC` rows and the
`FOK` refusal are *implemented and mock-tested*; the `GTD` expiration mapping is
*implemented, mainnet validation pending*.

| `TimeInForce` | Effect |
|---|---|
| `GTC` | Fired order is `gtc` (limit) or `ioc` (market); the trigger carries no expiry |
| `GTD` | As `GTC`, plus `trigger.expiration` set to the remaining seconds; an expiry already in the past is rejected |
| `IOC` | Fired order is `ioc` |
| `FOK`, `DAY`, `AT_THE_OPEN`, `AT_THE_CLOSE` | Rejected — the price-order endpoints accept `gtc` and `ioc` only |

## Execution instructions by product

| Instruction | Spot | Perpetual / Inverse | Delivery | Options | Status |
|---|---|---|---|---|---|
| `post_only` (regular orders) | `poc` | `poc` | `poc` | `poc` | Implemented and mock-tested (the mapping); implemented, mainnet validation pending (the request bodies) |
| `post_only` (conditional orders) | rejected | rejected | rejected | Not applicable | Implemented and mock-tested |
| `reduce_only` (regular orders) | rejected — spot has no such flag | `reduce_only=true` | `reduce_only=true` | `reduce_only=true` | Implemented, mainnet validation pending |
| `reduce_only` (conditional orders) | rejected | `initial.reduce_only=true` | as perpetual | Not applicable | Implemented and mock-tested |
| `display_qty` / iceberg (regular orders) | `iceberg` (decimal string) | `iceberg` (contracts) | `iceberg` (contracts) | `iceberg` (contracts) | Implemented, mainnet validation pending |
| `display_qty` (conditional orders) | rejected | rejected | rejected | Not applicable | Implemented and mock-tested |
| `quote_quantity` | market buy only; rejected on a market sell and on any limit order | rejected | rejected | rejected | Implemented and mock-tested (the spot market buy); implemented, mainnet validation pending (the refusals) |
| Trigger reference price | last price only; `trigger_type` is not consulted | `LAST_PRICE`/`DEFAULT`, `MARK_PRICE`, `INDEX_PRICE`; anything else rejected | as perpetual | Not applicable | Implemented and mock-tested (last price); implemented, mainnet validation pending (mark and index, and the refusal) |
| Hedge (dual) position mode | Not applicable | refused at connect | Not applicable | Not applicable | Unsupported |

Reduce-only is refused on spot rather than dropped: it is a derivatives concept,
and an order that quietly lost it would mean something different from the one
that was requested. Hedge mode is detected at connect and the client refuses to
start, because NautilusTrader nets positions per instrument and a venue holding
a separate long and short leg for one contract cannot be reconciled; the adapter
never switches the mode itself.

## What is rejected rather than translated

The design rule is that an order the adapter cannot express faithfully is
refused, not approximated. Every case below was read in the code, and every one
is raised before a request reaches the venue. The status column says whether the
suite also pins the behaviour.

| Situation | Outcome | Status |
|---|---|---|
| `MARKET_TO_LIMIT`, `TRAILING_STOP_MARKET`, `TRAILING_STOP_LIMIT` | `OrderDenied` | Implemented and mock-tested (`TRAILING_STOP_MARKET`); implemented, mainnet validation pending (the other two) |
| An instrument whose product is not in the client's `products` | `OrderDenied` | Implemented, mainnet validation pending |
| An instrument the provider and cache do not hold | `OrderDenied` | Implemented, mainnet validation pending |
| `FOK` on any options order, market or limit | `OrderRejected` — Gate.io options accept `gtc`, `ioc`, `poc` only | Implemented and mock-tested |
| `AT_THE_OPEN` / `AT_THE_CLOSE` on a futures, delivery or options market order | `OrderRejected` | Implemented, mainnet validation pending |
| `GTD`, `DAY`, `AT_THE_OPEN`, `AT_THE_CLOSE` on any limit order | `OrderRejected` | Implemented and mock-tested (the mapping raises); implemented, mainnet validation pending (the resulting event) |
| `FOK` on a conditional order | `OrderRejected` | Implemented and mock-tested |
| `DAY`, `AT_THE_OPEN`, `AT_THE_CLOSE` on a conditional order | `OrderRejected` | Implemented, mainnet validation pending |
| `post_only` on a conditional order | `OrderRejected` — the fired order cannot carry `poc` | Implemented and mock-tested |
| `display_qty` on a conditional order | `OrderRejected` — the price-order endpoints have no iceberg field | Implemented and mock-tested |
| `reduce_only` on a conditional spot order | `OrderRejected` | Implemented and mock-tested |
| `reduce_only` on a regular spot order | `OrderRejected` | Implemented, mainnet validation pending |
| Any conditional order on options | `OrderRejected` — Gate.io publishes no options price-order endpoint | Implemented and mock-tested |
| A conditional spot order while `spot_account_mode=CROSS_MARGIN` | `OrderRejected` — the spot price-order endpoint has no cross-margin ledger, and routing it to another ledger would trade a different account | Implemented, mainnet validation pending |
| A trigger type other than `DEFAULT`, `LAST_PRICE`, `MARK_PRICE`, `INDEX_PRICE` on a futures conditional order | `OrderRejected` | Implemented, mainnet validation pending |
| A fractional contract quantity on any derivative | `OrderRejected`, on submit and on amend | Implemented and mock-tested |
| `quote_quantity=True` anywhere except a spot market buy | `OrderRejected` | Implemented, mainnet validation pending |
| A conditional order whose `expire_time` has already passed | `OrderRejected` | Implemented, mainnet validation pending |
| An amendment of a delivery or options order | `OrderModifyRejected` — neither venue namespace has an amend endpoint | Implemented, mainnet validation pending |
| An amendment of an armed conditional order | `OrderModifyRejected` — cancel and resubmit instead | Implemented, mainnet validation pending |
| An amendment carrying a new trigger price | `OrderModifyRejected` | Implemented, mainnet validation pending |
| An amendment of a contract order that is not in the cache | `OrderModifyRejected` — the side determines the sign of `size`, and guessing it could flip a short into a long | Implemented and mock-tested |
| `DELETE /options/orders` without a `contract` or `underlying` scope | `ValueError` from the HTTP layer, before the request — Gate.io would otherwise cancel every resting option order in the account | Implemented and mock-tested |
| `DELETE /spot/orders` without a pair, `DELETE /futures/{settle}/orders` without a contract | The scope is a required argument, so the call fails with `TypeError` and issues no request. Gate.io treats the spot `currency_pair` as optional and would cancel across every pair and ledger; the futures endpoint requires the contract at the venue | Implemented and mock-tested |

## Where the adapter substitutes rather than refuses

Four known cases do not follow the rule above. They are listed because a page
that only advertised the rule would be misleading. All four are *implemented,
mainnet validation pending*: they were established by reading and exercising the
code, and no test currently pins them.

1. **Post-only wins over the time in force.** A limit order with
   `post_only=True` is sent as `poc` whatever its time in force, so a `GTD` order
   loses its expiry and a `FOK` order loses its fill-or-kill guarantee instead of
   being refused. Options are the exception, and only by accident: the
   options-specific `FOK` check runs first and rejects that combination.
2. **Spot market orders accept any time in force.** On spot, anything that is
   not `FOK` becomes `ioc`, including `AT_THE_OPEN` and `AT_THE_CLOSE`, which
   the futures, delivery and options paths reject. The resulting execution is
   the same immediate-or-cancel market order either way, but the refusal is
   inconsistent across products.
3. **Spot conditional orders ignore `trigger_type`.** The Gate.io spot
   price-order endpoint triggers on the last price and has no price-type field,
   so a spot `STOP_MARKET` submitted with `MARK_PRICE` or `INDEX_PRICE` is
   accepted and armed against the last price instead. The futures path rejects
   unsupported trigger types explicitly.
4. **Options cancel-all ignores the order side.** A side-scoped
   `CancelAllOrders` on an option contract cancels every resting order on that
   contract, both sides. Spot and futures honour the side (see below).

Market-data subscriptions are deliberately more forgiving than order handling:
an order book depth or push interval a product does not serve is clamped to the
nearest supported value with a warning rather than failing the subscription. The
consequence of a mis-sized book is a different amount of data, not a different
trade.

## Amendment and cancellation

| Operation | Spot | Perpetual / Inverse | Delivery | Options | Status |
|---|---|---|---|---|---|
| Amend price and/or quantity | `PATCH /spot/orders/{id}` | `PUT /futures/{settle}/orders/{id}` | rejected | rejected | Implemented and mock-tested (perpetual and inverse); implemented, mainnet validation pending (spot, and the delivery and options refusals) |
| Amend an armed conditional order | rejected | rejected | rejected | Not applicable | Implemented, mainnet validation pending |
| Cancel one order | `DELETE /spot/orders/{id}` | `DELETE {base}/orders/{id}` | as perpetual | `DELETE /options/orders/{id}` | Implemented and mock-tested |
| Cancel one armed conditional order | by its armed id | by its armed id | by its armed id | Not applicable | Implemented and mock-tested |
| Cancel all for one instrument | pair-scoped, side honoured | contract-scoped, side mapped to `bid`/`ask` | as perpetual | contract-scoped, **side ignored** | Implemented and mock-tested (spot, contracts); implemented, mainnet validation pending (options) |
| Bulk disarm of conditional orders | bulk when unscoped, individually by id when a side is named | same | same | Not applicable | Implemented and mock-tested |
| Batch cancel | `POST /spot/cancel_batch_orders`, 20 per request | falls back to sequential single cancels | as perpetual | as perpetual | Implemented, mainnet validation pending |
| Account-wide cancel-all | Unsupported on every product: each namespace requires a scope | | | | Unsupported |
| Countdown cancel-all (dead-man switch) | REST method exists on spot, perpetual and options namespaces; the execution client never calls it | | | | Experimental |

A cancel command carries no side filter to the price-order endpoints, because
neither of them accepts one: a bulk disarm would take out both sides of the book
whenever a side was named. A side-scoped command therefore disarms the matching
price orders individually, by id, and leaves the other side alone. That
behaviour is asserted for both spot and futures.

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

| Data type | Spot | Perpetual | Inverse | Delivery | Options | Source | Status |
|---|---|---|---|---|---|---|---|
| `TradeTick` | yes | yes | yes | yes | yes | `<prefix>.trades` | Implemented and mock-tested |
| `QuoteTick` (real best bid/offer) | yes | yes | yes | yes | yes | `<prefix>.book_ticker` | Implemented and mock-tested |
| `OrderBookDeltas` (`L2_MBP`) | yes | yes | yes | yes | yes | REST snapshot + `<prefix>.order_book_update` | Implemented and mock-tested |
| Order book snapshot request | yes | yes | yes | yes | yes | REST `order_book`, depth clamped per product | Implemented and mock-tested |
| `Bar` (closed bars only) | yes | yes | yes | yes | yes | `<prefix>.candlesticks`; options use `options.contract_candlesticks` | Implemented and mock-tested |
| Historical bars and trades | yes | yes | yes | yes | yes | Paginated REST, 1000 rows per call | Implemented, mainnet validation pending |
| `MarkPriceUpdate` | Not applicable | yes | yes | yes | Unsupported | `futures.tickers` | Implemented, mainnet validation pending |
| `IndexPriceUpdate` | Not applicable | yes | yes | yes | Unsupported | `futures.tickers` | Implemented, mainnet validation pending |
| `FundingRateUpdate` | Not applicable | yes | yes | Not applicable | Not applicable | `futures.tickers` | Implemented, mainnet validation pending |
| Instrument updates | yes | yes | yes | yes | yes | Periodic REST reload; Gate.io has no instrument channel | Implemented and mock-tested (loading and filtering); implemented, mainnet validation pending (the reload timer) |
| Book types other than `L2_MBP` | Unsupported on every product | | | | | | Unsupported |
| Options underlying, ticker and greeks streams | Not applicable | Not applicable | Not applicable | Not applicable | raw subscription only | `GateioPublicWebSocket.client`, not routed into the data engine | Experimental |

Gate.io has no dedicated mark, index or funding channel: all three are fields of
the futures ticker, so one subscription serves them and the client reference
counts it. A funding subscription on a delivery contract is refused with an
explanation rather than silently producing nothing, because a delivery contract
converges on its settlement price instead of paying funding.

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

| `GateioSpotAccountMode` | `account` on a regular order | `put.account` on a conditional order | Ledger |
|---|---|---|---|
| `SPOT` | `spot` | `normal` | Plain cash spot |
| `MARGIN` | `margin` | `margin` | Isolated margin, scoped to one pair |
| `CROSS_MARGIN` | `cross_margin` | none — conditional orders are rejected | Cross margin, reported through the unified account |
| `UNIFIED` | `unified` | `unified` | Unified account |

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
