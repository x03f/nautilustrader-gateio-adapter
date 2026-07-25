# Execution

`GateioExecutionClient` trades every configured product through a single
NautilusTrader account: spot (optionally on a margin ledger), USDT perpetual
futures, BTC-settled (inverse) perpetual futures, USDT delivery futures and
USDT-settled options.

**`environment` defaults to `"mainnet"`.** Set `environment="testnet"`
explicitly if you want the Gate.io testnet, and note that Gate.io serves only
spot and USDT perpetuals there. There is no local order kill switch; see
[Safety model](configuration.md#safety-model).

## Account model

| Aspect | Behaviour |
|---|---|
| Account id | `GATE_IO-master` — one Nautilus account per execution client |
| Account type | `CASH` when spot is the only product **and** `spot_account_mode=SPOT`; `MARGIN` in every other combination |
| OMS type | `NETTING`. Hedge (dual) position mode is detected at connect and refused with an explanatory error — the client never changes a venue-side account setting |
| Balances | Aggregated per currency across the wallets of the enabled products. A unified account reports one cross-product balance per currency that replaces the per-product wallets rather than adding to them |
| Margins | `MarginBalance` per instrument, derived from the position's `margin` and `maintenance_rate` |

Gate.io keeps a **separate wallet per product** and funds never move between
them implicitly. The client logs a warning at startup so this is never a
surprise, and exposes `transfer()` to move funds between the account's own
trading wallets (see [products.md](products.md#wallet-segregation-and-transfers)).

Selecting any `spot_account_mode` other than `SPOT` registers cash borrowing for
the venue, so the account can hold the negative balances a margin ledger
produces.

## Event sources

The **private WebSocket is the primary event source**:

| Channel | Drives |
|---|---|
| `{spot,futures,options}.orders` | the order lifecycle |
| `{spot,futures,options}.usertrades` | fills |
| `{spot,futures,options}.balances` | account state |
| `{futures,options}.positions` | parsed and logged, never published as reports |

Positions are deliberately not forwarded from the stream: REST is the single
reconciliation source for positions, which keeps one fill from producing two
competing views of the same position.

A REST account poll (`account_polling_interval_secs`, default 30 s) refreshes
account state as a safety net. Gate.io provides no replay or resume on any
private channel, so every reconnect is followed by REST reconciliation over the
window since the last stream event.

The private channels for futures, delivery and options are addressed by the
numeric account user id, which the venue does **not** validate at subscribe
time — a wrong id is acknowledged and then silently delivers nothing. The client
therefore fetches the real user id at connect rather than guessing it.

## Order translation

| Nautilus order | Gate.io encoding |
|---|---|
| MARKET, spot SELL | `type=market`, `amount` = base quantity, `tif` ioc (fok honoured) |
| MARKET, spot BUY with `quote_quantity=True` | `type=market`, `amount` = quote amount |
| MARKET, spot BUY with a base quantity | aggressive IOC `limit` priced by the pair's published `slippage` cap |
| MARKET, derivatives | `price="0"` with `tif=ioc` (fok on futures and delivery only) |
| LIMIT | `price`, `tif` gtc/ioc/fok; post-only maps to `poc` |
| STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED | the product's price-triggered ("auto order") endpoint |

Supported order types are exactly:

```python
from nautilus_gateio.execution import CONDITIONAL_ORDER_TYPES, SUPPORTED_ORDER_TYPES
```

`SUPPORTED_ORDER_TYPES` = MARKET, LIMIT, STOP_MARKET, STOP_LIMIT,
MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED. Anything else is **denied** before a
network call, with the reason on the `OrderDenied` event.

Supported time in force: GTC, IOC, FOK, plus post-only through `poc`. Flags
honoured: `reduce_only` (derivatives), `display_qty` (iceberg),
`quote_quantity` (spot market buy only).

### Nothing is silently altered

Every case Gate.io cannot express is rejected with a stated reason instead of
being changed into something the venue does accept:

| Situation | Result |
|---|---|
| GTD, AT_THE_OPEN, AT_THE_CLOSE, or any other time in force | rejected, naming the supported set |
| `reduce_only` on a spot order | rejected — reduce-only is a derivatives concept and dropping it changes the order |
| `quote_quantity=True` anywhere but a spot market buy | rejected |
| `quote_quantity=True` on a spot market **sell** | rejected — Gate.io market sells take a base amount |
| post-only on a market order | rejected |
| FOK on an options order | rejected — the venue offers gtc, ioc and poc there |
| Fractional contract quantity on a derivative | rejected — contracts are whole |
| Price-triggered order on options | rejected — the venue has no such endpoint for options |
| Price-triggered spot order under `CROSS_MARGIN` | rejected — the venue's price-trigger endpoint has no cross-margin ledger |
| A base-denominated spot market buy with no reference price available | rejected, suggesting `quote_quantity=True` |

The one translation that is not literal is the base-denominated spot market
buy. Gate.io's native spot market buy spends a *quote* amount, so it cannot
express "buy exactly this many base units". Converting the quantity behind the
caller's back would change the order, so it is sent as an immediate-or-cancel
limit order priced through the book by the pair's **own published slippage
cap**; the venue fills at or better than that bound and cancels the remainder.
The substitution is logged at INFO with the reference price and the cap.

## Modification

`ModifyOrder` maps to Gate.io's amend endpoints, with explicit rejections where
the venue has no equivalent:

| Case | Result |
|---|---|
| Spot | `PATCH /spot/orders/{id}` with the new amount and/or price |
| Perpetual, inverse | `PUT /futures/{settle}/orders/{id}` with the signed size and/or price |
| Delivery | rejected — the venue cannot amend delivery orders |
| Options | rejected — the venue cannot amend options orders |
| An armed price-triggered order | rejected — cancel and resubmit |
| A new trigger price | rejected — the venue cannot amend a working order's trigger |
| Neither quantity nor price given | rejected |
| Contract quantity change while the order is not in the cache | rejected — the side determines the sign of `size`, and guessing it could flip a short into a long |

## Cancellation

* `cancel_order` — cancels one order by venue order id, taking the price-trigger
  id space into account for armed conditional orders.
* `cancel_all_orders` — cancels per product and instrument, including armed
  price-triggered orders, filtered by side where requested.
* `batch_cancel_orders` — batched where the venue supports it, falling back to
  individual cancels for armed trigger orders.

## Price-triggered orders

Gate.io's "auto orders" live in their **own id space**: an armed trigger order
has one id, and the order it creates when it fires has another. The client keeps
both, indexed in each direction, for the life of the order, so the identity
survives the transition and a restart. Trigger types map onto the venue's price
types per product; a trigger type the product does not offer is rejected.

## Fills

* The Nautilus `TradeId` is the **venue trade id** from `*.usertrades` /
  `my_trades`, so the framework's duplicate-fill guard works across the
  WebSocket and REST reconciliation paths. Trade ids are never synthesised.
* Applied trade ids are remembered per order, so a replayed `usertrades` message
  cannot fill twice.
* On spot, a fee charged in the currency being received is netted off the fill
  quantity when `fee_currency == base_currency`, following the same convention
  as other NautilusTrader crypto adapters.
* Fee amount and currency are reported exactly as the venue returns them. Spot
  fees are usually charged in the received currency and may be charged in GT
  when GT-fee deduction is enabled on the account.

## Client order ids

The Nautilus `ClientOrderId` travels in Gate.io's `text` field, which must start
with `t-`, hold at most 28 further characters and use only `[0-9A-Za-z_.-]`.

* An id that fits is embedded verbatim, so the mapping is recoverable from the
  venue alone after a restart.
* An id that does not fit is replaced by a generated id
  (`t-<tag>-<counter>`, tag from `client_order_id_tag`) and the pair is kept in
  an in-memory alias table.

Keeping Nautilus client order ids within the venue's limit is therefore worth
doing: it is what makes a restart able to re-identify resting orders without any
local state.

## Reconciliation

All four report generators are implemented against REST:

| Method | Source |
|---|---|
| `generate_order_status_reports` | open plus recently finished orders across every enabled product, paginated, including armed price-triggered orders |
| `generate_order_status_report` | single lookup by venue order id or client order id |
| `generate_fill_reports` | `my_trades` per product over the lookback window |
| `generate_position_status_reports` | futures, delivery and options positions (netting) |

Start-up order is instruments, then account state, then reports. The intended
guarantee is that a restart with a resting order and an open position loses and
duplicates nothing.

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
derivative wallets takes two calls. Account state is refreshed after a
successful transfer.

## Operational notes

* Restarting the node while orders rest on the venue is supported through
  reconciliation, but the venue remains the source of truth: an order the local
  cache never saw is adopted from the report, not invented.
* Options and delivery orders cannot be amended; strategies that rely on
  amendment should cancel and resubmit on those products.
* Rate limiting is applied client-side; a `429` is retried with backoff for
  requests whose replay is provably safe. Order-mutating requests are never
  replayed automatically — an ambiguous outcome is reported as ambiguous
  instead of being resent.
