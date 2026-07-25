# Execution

`GateioExecutionClient` routes orders to Gate.io spot over REST and turns
exchange state into Nautilus events. Account type is `CASH`, OMS type is
`NETTING`.

**The default environment is testnet.** See
[Testnet by default](#testnet-by-default-opting-into-mainnet) below.

## Order lifecycle

A successful limit-order round trip produces this event sequence:

```text
Strategy.submit_order
  -> OrderSubmitted        (generated immediately on receipt)
  -> REST POST /spot/orders
  -> OrderAccepted         (venue order id assigned)
  -> OrderFilled           (zero or more, as fill deltas are detected by polling)
  -> AccountState          (pushed after each detected fill)
```

Failure and cancellation paths:

* REST rejection (validation error, insufficient balance, API error) ->
  `OrderRejected` with the exchange message.
* Unsupported order type -> `OrderRejected` without any REST call.
* `Strategy.cancel_order` -> REST DELETE -> `OrderCanceled`.
* Exchange-side cancellation (e.g. the unfilled remainder of an IOC order)
  is detected by the poll loop and surfaced as `OrderCanceled`.

Fills are detected by REST polling, not a private WebSocket: an immediate
check ~0.6 s after submission (aggressive orders usually fill instantly)
plus a periodic loop every `account_poll_interval_secs` (default 5.0 s).
Expect fill-report latency up to one poll interval for resting orders.

## Supported

### LIMIT (GTC)

`LIMIT` orders are submitted as Gate.io `limit` orders with
`time_in_force="gtc"`, using the order's price and base-currency quantity
verbatim.

### MARKET (emulated as IOC limit)

Gate.io spot market-**buy** orders interpret the `amount` field as *quote*
currency (how much USDT to spend), while Nautilus `MARKET` orders specify
*base* quantity. To keep quantity semantics exact, the client emulates
`MARKET` as an aggressive `limit` IOC order:

1. Fetch the last traded price for the pair.
2. Price the order across the spread: `last * 1.01` for buys, `last * 0.99`
   for sells, rounded to the instrument's price precision.
3. Submit as `limit` with `time_in_force="ioc"`.

Consequences:

* Worst-case slippage is bounded at 1% from the last price — beyond that the
  order simply does not fill.
* Any unfilled remainder is cancelled by the exchange (IOC), which the poll
  loop surfaces as `OrderCanceled` after any partial `OrderFilled` deltas.

### Cancel and cancel-all

* `cancel_order` — requires a venue order id (an order not yet accepted
  cannot be cancelled; a warning is logged). On success generates
  `OrderCanceled`.
* `cancel_all_orders` — fetches open orders for the instrument's pair and
  cancels them one by one, continuing past individual failures.

### Partial fills

The poll loop compares the exchange's filled amount (`amount - left`)
against the last reported fill and emits an `OrderFilled` for each positive
delta, so a partially filled order generates incremental fill events with
correct quantities. Fill price uses the exchange's average deal price when
available, falling back to the order price, then the last traded price.

### Client order id propagation

The Nautilus `ClientOrderId` is propagated to the exchange through Gate.io's
`text` field. The `text` field must start with `t-`, use only
`[0-9A-Za-z_.-]`, and be at most 28 characters, so:

* the client sends `"t-" + client_order_id`, sanitized to the allowed
  charset and truncated to the 28-character limit
  (`sanitize_client_order_id`);
* if sanitization strips the id entirely (no valid characters), a generated
  id is used instead (`t-<tag>-<timestamp><seq>`, tag from
  `client_order_id_tag`).

In every case the Nautilus-id-to-venue-id mapping is kept in memory, so
order tracking works regardless of what lands in `text`. Keep your Nautilus
client order ids at 26 characters or fewer: longer ids are truncated on the
exchange side, and two ids sharing the same 26-character prefix would
collide in the `text` field (Gate.io rejects duplicate `text` values for
open orders).

### Account state

Balances are fetched from `/spot/accounts` and pushed as `AccountState` on
connect, after every detected fill, and on every poll cycle. Zero-total
currencies are skipped.

## Not supported

* **Stop orders** (`STOP_MARKET`, `STOP_LIMIT`, trailing stops) — rejected
  with `OrderRejected`. Implement stops at the strategy level.
* **Post-only** — no `poc` time-in-force mapping.
* **Order modification** — `modify_order` logs a warning and does nothing;
  cancel and re-submit at the strategy level.
* **Time-in-force beyond GTC/IOC** — LIMIT is always GTC; the IOC path is
  reserved for the MARKET emulation.
* **Private WebSocket** — order/fill/balance updates arrive by REST polling
  only.
* **Start-up reconciliation** — `generate_order_status_reports`,
  `generate_fill_reports`, and `generate_position_status_reports` return
  empty results (fresh-start semantics). Pre-existing open orders are not
  adopted. For a read-only diagnostic comparison of local vs. exchange
  state, use the standalone `nautilus_gateio.reconcile.reconcile()` helper.

## Testnet by default; opting into mainnet

`GateioExecClientConfig` targets the Gate.io testnet host
(`https://api-testnet.gateapi.io`) unless you explicitly set
`environment="mainnet"`. To trade real funds:

```python
config = GateioExecClientConfig(environment="mainnet")
```

Before doing so, understand the risks in this adapter's current state:

* fills are polled, so fill and cancellation reports lag by up to one poll
  interval;
* there is no start-up reconciliation — restarting the node forgets open
  orders, which remain live on the exchange;
* the MARKET emulation can leave a cancelled remainder instead of a full
  fill in fast markets.

Use small sizes, prefer LIMIT orders, and monitor open orders on the
exchange side until you have validated the behavior for your workload.

## Fees currency caveat

`OrderFilled` events report the fee amount and currency exactly as returned
by Gate.io. On spot, fees are typically charged in the *received* currency
(base for buys, quote for sells) — and can be charged in GT when GT-fee
deduction is enabled on the account. If the fee currency is unknown to
Nautilus, the adapter creates a generic crypto currency with precision 8
(`get_currency`), which is sufficient for accounting but carries no metadata.
Portfolio calculations that assume quote-currency fees should account for
this.
