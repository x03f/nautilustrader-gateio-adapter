# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A recovered trade whose order the engine refused to adopt no longer
  crashes startup reconciliation** (`REC-08` in docs/review-matrix.md, found
  and closed by live validation against the real venue). The recovery
  sweep's last-resort branch trusted the venue-order-id index as proof the
  single-report channel could book, but the engine also writes that index
  for an unclaimed external order it has just filtered
  (`filter_unclaimed_external_orders`) without creating the order — and a
  lone `FillReport` sent at such a dangling entry crashed the engine's
  fallback lookup (`Cache.orders(side=None)`, `TypeError: an integer is
  required`) instead of deferring, taking the whole startup mass status with
  it: the node reported RUNNING with an unreconciled execution state. The
  channel is now gated on the cached order object. An order the engine
  declined to adopt keeps its executions excluded with it, logged per trade
  — that refusal is the engine's configured ruling. A trade whose order
  statement cannot be obtained at all now makes the pass refuse honestly
  instead of returning a book that silently lacks a venue execution: the
  startup mass status is refused (`None`, so the kernel declines to start
  and the next attempt heals), the reconnect keeps its stale-but-honest
  state, and the stream route logs the standing loss for the next
  reconciliation to repair.

- **The staleness protection now covers every trade a recovery pass books
  over prior knowledge, closing the two doors its arming rule left open**
  (`REC-07` in docs/review-matrix.md, found by the eighth round's audit).
  The arming skipped fills whose order the cache did not hold when recovery
  began, so one outage trade riding an external or adopted order left its
  whole instrument unguarded, and a stale position answer — an absent row,
  or the kept zero-size row stamped in the same second — erased the
  pre-existing position together with the adopted trade, with a fabricated
  execution and reconciliation reporting success; independently, the reader
  popped the memory when the bookings netted to zero before comparing
  anything, so an ordinary zero-net outage round trip disarmed the
  instrument the same way. The arming is now keyed per instrument on prior
  knowledge — a cached order the trade extended, or a pre-existing open
  position — recorded before the pass books anything (so a position the
  pass opens never counts as pre-existing, and a trade the in-call sweep
  fails to book is still guarded), and the memory clears only on venue
  proof: an answer stamped strictly after the booked trades, or one that
  agrees with the post-booking book, with the bookings' net delta playing
  no part. The fresh-cache restart keeps its current-answer behaviour:
  reconstruction over no prior position still arms nothing.

- **An order report's average price is read strictly on filled rows, and a
  spot fill's fee currency must be stated for a nonzero fee.** The average
  price (`avg_deal_price`/`fill_price`) rode a forgiving reader whose
  default is 0 — it is the price the execution engine puts on any inferred
  stand-in fill it builds from the report, so an unreadable stated average
  priced a fabricated execution; it now fails the listing
  (`OrderReportsUnavailable`), while an absent average stays the smaller
  claim of none. A spot fill stating a fee without a readable
  `fee_currency` booked the commission in the quote currency — Gate.io
  documents the field on every spot trade row and charges the ordinary
  buy's fee in the base currency, so the guess misdenominated commission;
  it now refuses (`FillReportsUnavailable` on listings, a loud dropped
  frame on the stream), and a zero fee keeps the quote as the harmless
  denomination of zero.

- **A stale position answer can no longer erase a position recovered through
  orders this node held.** The read-skew rule guarding recovered trades
  recognised staleness only by equality with the book as it stood before the
  trades were booked, so an answer staler than its own memory — an absent row,
  or the kept zero-size row Gate.io serves for a traded contract — was
  believed as current, and the engine squared a pre-existing position to flat
  with a fabricated execution while reconciliation reported success. The rule
  now withholds every answer that does not contain the booked trades and
  cannot be shown, by the venue's own stamp, to postdate them; withholding
  degrades to a refused node start, never a fabrication. In the same change
  the memory arms only for trades that extended orders the cache held when
  recovery began, so an ordinary no-database restart whose closed round trip
  straddles the lookback window starts on the venue's current flat answer
  instead of freezing until the trades age out; and a position row with no
  readable venue timestamp is judged as unprovably fresh instead of being
  stamped with the local clock, which silently bypassed the rule. Residuals
  are stated on the method: the memory lives in-process (protection is one
  restart deep). One boundary survived that round as the open finding
  `REC-07` — trades booked onto orders adopted during the same pass armed
  no memory — and is closed by the first entry above.

- **A report field this client cannot read is no longer parsed as a confident
  number.** The strict reading introduced for the position `size` now covers
  every deciding field of the report surface: futures/delivery/options order
  `size`, `left` and `price`; spot order `side`, `type`, `amount`,
  `filled_amount`, `price` and the cash-buy status guard; fill `size`,
  `amount`, `price`, `side`, `fee` and execution time; the armed price-order
  fields; and the status arithmetic shared with the live stream. The forgiving
  defaults crossed the money line through the real engine: an unreadable
  remainder became a confident full fill the engine fabricated the rest of and
  closed locally while the venue held the order open, an unreadable fill size
  silently replaced a venue execution with a commission-less inferred fill,
  and an unreadable price booked an execution at zero. Unreadable now raises —
  trade listings answer `FillReportsUnavailable` carrying every readable row,
  order listings answer the new `OrderReportsUnavailable`, and the startup
  mass status is refused on either, which is the platform's own posture for a
  failed report query (a partial answer makes the engine infer stand-ins for
  the missing trades). The venue's affirmative zeros stay believed, and
  stringified integers parse exactly; decimal-sized (`enable_decimal`)
  contracts are refused loudly rather than silently truncated, a documented
  limitation of this alpha. A still-open quote-denominated spot market buy now
  answers the single-order query with the venue's own quote-denominated
  ACCEPTED statement — resolving the engine's inflight check honestly instead
  of feeding it silence until it fabricated a rejection — while listings keep
  yielding no report for it. Three edges found by the round's audit still ride
  forgiving readers and are recorded as residual risks in
  docs/review-matrix.md: the order report's average price, the spot fill's fee
  currency, and the spot stream's inferred `finished` for a payload stating
  neither status nor event.

- **A restart no longer loses a venue-confirmed trade the engine's
  deduplication would drop.** The sweep that re-offers recovered executions the
  grouped hand-over did not book used to run only after a WebSocket reconnect;
  a process restart built the same mass status, handed it over, and never
  looked at the outcome — so an order snapshot the venue had not caught up with
  was deleted as a duplicate together with the trades grouped under it, and the
  position gap was closed with an inferred fill carrying no venue trade id and
  no commission (on spot overstating the position by exactly the withheld
  base-currency fee), or not closed at all. The sweep now also runs inside
  `generate_mass_status`, before the execution engine reconciles anything, so
  the venue's own trades — id, price, fee — reach the cache first and a
  position report that already contains them reconciles cleanly. An order
  snapshot the sweep outran is withheld from the mass status (the engine would
  misread it as corrupted cache and abort node start), and a position answer
  equal to the pre-booking book that cannot be shown to postdate the booked
  trades is answered `PositionStatusUnavailable` rather than handed to the
  engine as current truth. An earlier repair staged off the engine's
  publication of the reconciled mass status was withdrawn for doubling the
  position; docs/roadmap.md (Stage 0) and docs/review-matrix.md record why.

- **A position row whose `size` this client cannot read is no longer answered
  as flat.** The row shapes were covered in the previous round; the field that
  decides the answer now is too. `size` is read strictly, so a missing key,
  null, an empty string, a non-numeric string, a boolean and any value that is
  not an exact whole number of lots fail the query naming the row and the
  field, instead of reading as `0` — which is FLAT, a claim the engine squares
  a live book against. A row that genuinely reads zero, including the
  stringified zeros Gate.io sends since v4.106.0, still squares the book.

- **An unfinished quote-denominated spot market buy no longer yields an order
  status report built from its running partial fill.** Gate.io publishes no
  base-denominated quantity for a cash buy until it finishes, so a listing read
  mid-match produced a report that restated the order to the partial figure and
  the remaining matches were refused as overfills. While the venue is still
  working the order there is now no report at all: its executions are recovered
  from the trade listing, and the order's own statement is re-read once the
  venue has finished it.

- **A failed trade listing is no longer reported to the execution engine as "no
  trades".** `generate_fill_reports` caught every per-product failure, logged it
  and returned whatever it had collected. The engine keeps exactly one brake
  against squaring a cached position to flat — it declines to do so when the fill
  query failed — and that flag is set from a query that *raises* and from nothing
  else, so the brake never engaged. A 5xx on the trade listing while the position
  query answered therefore closed the position with a synthetic trade id and no
  commission, and the loss was permanent: a closed position is not open, so it is
  never queried again and the venue's real closing trade is never applied. The
  query now raises `FillReportsUnavailable`, carrying the reports the products
  that did answer produced, so the engine gets the failure it needs; the
  recovery routes treat the raise as the failure it is (startup refuses the
  mass status, a reconnect pass aborts keeping its pre-reconnect state) rather
  than booking a partial account. `USER_NOT_FOUND` is unchanged: a wallet
  Gate.io has not created holds no trades, which is a definite answer of none.

- **A position row whose shape this client cannot read is no longer answered as
  flat.** A row that is not an object, a row carrying no venue symbol, a row
  whose instrument cannot be resolved and an empty `200` body were all dropped,
  and a query that dropped everything answered with an explicit flat report — a
  statement the venue never made, and one the engine squares a live book against.
  Such a row now fails the query instead, naming which row of how many and why. A
  row that reports zero size is unaffected: that is the venue saying the position
  is closed, and it must still square the book. The remaining half — a row whose
  *size* cannot be read — is closed by the entry above; `REC-02` in
  [docs/review-matrix.md](docs/review-matrix.md) records both halves.

### Changed

- **A refusal this adapter makes itself is now `OrderDenied`, not `OrderSubmitted`
  followed by `OrderRejected`.** An unsupported time in force, post-only on an
  immediate order, `reduce_only` on spot, a `display_qty` the venue's `iceberg`
  cannot carry, a fractional contract count, `quote_quantity` outside a spot
  market buy, an off-tick price, a conditional order on options — none of these
  consults Gate.io, so announcing a submission asserted a network fact that was
  false and attributed the refusal to the venue. Downstream that put the order
  through the engine's in-flight set for nothing, wrote a submission Gate.io
  never received into the persisted event stream an audit reads back, and charged
  the venue's rejection rate for local validation. The whole request is now built
  before the submission is announced — building it is what decides these
  refusals — so they arrive as `OrderDenied` while the order is still
  `INITIALIZED`, and `OrderRejected` means only what the platform says it means.
  A strategy matching on `OrderRejected` for these cases must handle
  `on_order_denied` instead.

- **A contract or option whose payload omits a margin or fee rate is skipped,
  not published at zero.** `maintenance_rate`, `maker_fee_rate` and
  `taker_fee_rate` were read through a converter that answers zero for a missing
  or unparseable value, and zero is a valid rate for all three: it tells
  `MarginAccount` that a position needs no maintenance margin and tells
  `Account.calculate_commission` that trading is free, neither of which is
  distinguishable afterwards from a rate Gate.io really published. Gate.io
  carries all three on every contract and option it lists, so a payload without
  one is a payload the parser does not understand; it is now skipped with a
  warning naming the field, the same answer an unrepresentable price scale
  already got. A spot pair is likewise refused when it carries no `fee` and the
  caller supplied no account fee tier.

- **Margin is now reported in the scope the venue actually holds the collateral
  in.** Every futures position produced a per-instrument `MarginBalance`,
  whatever margin mode it was held under. NautilusTrader gives the two scopes
  distinct meanings — per-instrument is isolated collateral, segregated to one
  position; account-wide (`instrument_id=None`, keyed by collateral currency) is
  what a cross-margin venue reports, because closing one position there frees
  collateral for every other — and keeps them in separate stores, so the scope
  decides which query answers at all. Gate.io marks the mode on the position:
  `leverage="0"` is cross margin, any positive `leverage` is isolated at that
  leverage. Cross positions are now summed per settlement currency into an
  account-wide entry, isolated positions stay per instrument, and a strategy on
  a cross-margined account gets an answer from `margin_init_for_currency()`
  instead of `None`. Account-wide entries are keyed by currency throughout, so a
  USDT-settled perpetual and the USDT options wallet add up rather than
  overwrite one another.

### Fixed

- **Reconnect recovery of an order that missed more than one fill.** Every
  recovered trade of one order is now handed over under the venue's own
  statement of that order, instead of one trade beside a cumulative filled
  quantity. The engine used to account for the difference by inventing a fill:
  the venue's second trade id was never recorded, its commission never withheld,
  and the position ended up overstated by exactly that fee. An order the venue
  reports under a second id is also rebased before its fills are applied, and the
  guard that restates an order from the venue listing now admits exactly the
  reports that cannot produce an inferred fill, read off the installed engine
  rather than assumed. Each of the three was closed against a demonstration of
  the damage and re-checked by reverting the fix. **This does not close recovery**
  — the same hand-over does not run on the startup path, and two further defects
  are open; `docs/execution.md` and `docs/review-matrix.md` state all three.

- **The WebSocket transport logs through the platform, not the standard
  library.** `nautilus_gateio/websocket/client.py` held a
  `logging.getLogger(__name__)`, so every reconnect, failed subscription replay,
  malformed frame and venue service notice went somewhere the Nautilus log file
  never saw and `log_level`, `log_level_file` and `log_component_levels` never
  reached — while `instruments.py` in the same package already used the platform
  `Logger`, so one run answered the operator's configuration in one place and
  ignored it in another. The transport now logs under the component name
  `GateioWebSocketClient`, which is what `log_component_levels` matches on. A
  package-wide test refuses any future standard-library logger in the tree.
- **No background task of the WebSocket transport escapes shutdown any more.**
  `disconnect()` cancelled three named attributes; the proactive close started
  by the venue's `upgrade` notification and the task wrapping a coroutine
  returned by the message handler were in neither, so they were referenced only
  by the event loop — collectable while suspended, and never awaited. Both are
  now registered, and `disconnect()` hands the whole registry to the platform's
  `cancel_tasks_with_timeout`, which snapshots strong references before
  cancelling and names anything that has not settled. A background task that
  fails is reported through the platform logger instead of surfacing as
  asyncio's "exception was never retrieved" at some later collection.
- **`GateioDataClient` no longer replaces the platform's task registry.** It
  overwrote `LiveMarketDataClient._tasks` — a `WeakSet` the base class populates
  from `create_task` and drains from `cancel_pending_tasks` — with a plain set,
  so every completed subscribe, unsubscribe and request task stayed reachable for
  the lifetime of the node. `_disconnect` then cancelled those tasks without
  awaiting them and cleared the collection, leaving the platform's bounded
  shutdown nothing to find and reporting a clean stop for work it had merely
  stopped tracking. The client now uses `create_task` and `cancel_pending_tasks`
  unchanged.
- **The data client releases the shared HTTP transport last.** `_disconnect`
  closed it first, while its own background tasks were still running: the
  transport is reference counted, so on the second client to disconnect that
  call actually closes the pool, and a request in flight then failed with
  `CLIENT_CLOSED` instead of being cancelled. Shutdown now settles the tasks,
  closes the sockets, and releases the transport in that order.
- **Unrealised PnL was counted twice in portfolio equity.** The futures wallet
  total was published as `total + unrealised_pnl`, and the options wallet total
  as `equity`, which Gate.io defines as balance plus position value. The
  platform computes equity for a margin account as
  `balances_total + Σ unrealized_pnl(open positions)`, so a 1000 USDT account
  holding 100 USDT of unrealised profit reported 1200 USDT of equity — and the
  unrealised profit was additionally published as *locked* collateral when
  nothing was reserved. `AccountBalance.total` is now the wallet balance alone:
  the figure Gate.io documents as excluding position PnL, and the one the
  in-tree Binance adapter reports. This also stops the REST poll and the
  `futures.balances` stream, which carries the wallet balance alone, from
  contradicting each other on every tick.
- **A position query the venue refused is no longer reported as FLAT.**
  `USER_NOT_FOUND` (the product wallet has not been created yet) and `FORBIDDEN`
  / `INVALID_UNIFIED_ACCOUNT` / `UNIFIED_ACCOUNT_NOT_ACTIVATED` (the key or the
  account mode may not read that ledger) arrived as one exception type and were
  both treated as "no position here". Only the first is a statement about
  positions; the others say nothing about what is open, and reaching the FLAT
  fallback with them handed the engine a claim the venue never made, which
  reconciliation then acted on by closing a still-open position through an
  inferred fill. The three refusal labels now raise `PositionStatusUnavailable`,
  which is how this client tells the engine a query went unanswered.
- **A poll that could not read every wallet no longer restates the whole
  account.** Two failures came out of one assumption, that a partial read is a
  snapshot. Under a Unified Account, a single failed read of the unified ledger
  dropped the list of currencies whose per-product wallets are echoes of the
  same funds, so the wallets were summed and a 1000 USDT account was published
  as 2000 USDT with `reported=True` for the RiskEngine to size against; the
  client now keeps the last unified snapshot it read, and publishes nothing at
  all rather than a sum it knows is inflated. Separately, margins were rebuilt
  from only the products that answered, and because `MarginAccount.apply`
  replaces its stores from the event rather than merging, one failed futures
  call deleted that wallet's margin from the platform's view; margins are now
  tracked per wallet and every live entry is republished each time.
- **`AccountState.ts_event` now carries the venue's timestamp** for a balance
  that arrived on the stream, instead of the moment this client parsed it. Under
  a reconnect burst the persisted account history could not be used to
  reconstruct when a balance actually changed. A REST snapshot still uses the
  local clock, because the venue attaches no time to one.
- **A re-pushed `full` order book snapshot no longer rolls the book backwards.**
  Gate.io documents that a full depth snapshot may be pushed on the incremental
  channel at any time. One whose `u` was not newer than the local update id was
  applied unconditionally, replacing the book with depth the stream had already
  superseded and republishing it downstream as a fresh snapshot batch — the exact
  case the REST path has refused since it was written. It is now discarded and
  counted in `snapshots_stale`. A `full` push carrying no `u` at all was accepted
  while the previous update id was kept, leaving the book holding one state and
  claiming another so that the next notification was measured against the wrong
  expectation; it now raises `OrderBookSequenceError` and the book is rebuilt
  from REST.
- **An optional notional bound that cannot be represented no longer discards the
  whole instrument.** `Money` carries the quote currency's precision, so a
  `max_quote_amount` finer than that currency floors to zero, and the platform
  requires a positive `max_notional`; the constructor error was swallowed by the
  parser's blanket handler and a tradable pair was lost over an optional field.
  The bound is now dropped with a warning, as the size bounds already were.
- **`FundingRateUpdate.next_funding_ns` no longer names a settlement that has
  already happened.** Gate.io's ticker stream carries no next-funding timestamp;
  the only source is `funding_next_apply` on the contract definition, which the
  instrument reload task refreshes hourly by default while the ticker pushes
  about once a second. Republished verbatim, the field pointed into the past for
  up to a whole refresh interval after every settlement, so
  `next_funding_ns - clock.timestamp_ns()` — the number anyone actually reads it
  for — came out negative, and because the field participates in the platform's
  equality and hashing for that data type, a stale value changed deduplication
  as well. The cached value is now treated as what it is, an exact point on the
  venue's funding grid, and rolled forward by whole `funding_interval` steps to
  the first settlement after the update's own timestamp. A contract that
  publishes no interval leaves the field unset rather than wrong.
- **One malformed candle no longer aborts an entire bar request.** NautilusTrader
  enforces the OHLC invariants inside the `Bar` constructor, and that
  construction sat outside the parser's guard, so a row from an illiquid delivery
  or option contract that violated them raised out of the request coroutine. The
  live path caught it one level up and lost only the candle; a historical request
  lost the whole response, and a strategy following the documented
  request-then-subscribe pattern never subscribed and sat silent. Such a row is
  now dropped and counted in the new `candles_dropped` health counter, and the
  request answers with the rest after logging how many it lost.
- **Mark and index prices keep the scale the venue published them with.** Both
  were built through `Instrument.make_price()`, whose precision comes from
  `order_price_round` — the grid orders must sit on. Gate.io publishes
  `mark_price_round` as a separate and finer minimum unit, so a BTC_USDT option
  marked 5797.7 was published as 5798 against a 1-unit order tick. A reference
  price is not an order price and does not live on the order grid; it is now
  built from the venue's own decimal string, at that string's precision raised to
  the scale the contract states so it cannot wobble between updates. A field the
  venue sends empty or unparseable now produces no update rather than a zero,
  which would be a price no participant ever saw.
- **A post-only termination no longer breaks the order state machine.** A
  terminal order message carrying `finish_as=poc` produced `OrderRejected`
  unconditionally, including for an order this client had already booked a fill
  against. The platform has no `PARTIALLY_FILLED -> REJECTED` transition, so the
  event raised `InvalidStateTrigger` inside the execution engine and the order
  stayed open locally while Gate.io had finished it. That case is now reported as
  `OrderCanceled`, the transition the platform does accept, and a replayed
  message for an already-closed order produces nothing.
- **A cancel-all for an unconfigured product no longer falls silent.** The
  command returned without a word about itself, so a strategy waiting on its
  cancels had nothing in the log tying the silence to what it sent. It now logs a
  warning naming the instrument, and still emits no rejection event — which is
  what the platform prescribes for a cancel-all that fails a local check.
- **A conditional spot order on a cross-margin ledger is refused with a reason.**
  Building the refusal message sorted a dict keyed by an enum that defines no
  ordering, so the refusal raised `TypeError` and never reached the strategy as a
  refusal.
- **A command the venue never answered is no longer reported as a rejection.**
  A submit, cancel or amend that failed on the transport, on a 5xx, or while
  reading the response produced `OrderRejected` / `OrderCancelRejected` /
  `OrderModifyRejected`, telling the strategy the venue refused a command it may
  well have applied. `OrderRejected` is terminal, so an order Gate.io was holding
  could never be represented locally again. Such outcomes are now logged and the
  order is left in flight for the execution engine to resolve, as
  NautilusTrader's order command outcome policy requires; only a venue refusal
  (a 4xx answer, or a failure that never left the process) still produces a
  rejection event. A whole-batch cancel failure no longer emits one
  `OrderCancelRejected` per order in the batch.
- **A replayed request that was never answered reports the ambiguity.** Cancels
  are replayed because cancelling twice is harmless, but a replay makes a
  duplicate harmless, not the outcome known; the transport raised
  `NETWORK_ERROR` — "this definitely did not happen" — for a cancel the venue may
  have applied. That case now raises `GateioRequestAmbiguousError`, and
  `NETWORK_ERROR` is reserved for requests no byte of which left the process.
- **A fully hidden order is no longer submitted fully displayed.** NautilusTrader
  reads `display_qty=0` as "hide the whole order"; Gate.io reads `iceberg=0` as
  "normal order" and does not support hiding the whole amount, so the instruction
  was inverted and the entire size rested visibly on the book. It is now refused,
  naming the venue restriction. A fractional `display_qty` on a derivative is
  refused too, rather than being truncated to `0` — which inverted it the same
  way.
- **Post-only no longer overrides IOC and FOK.** `post_only=True` mapped to `poc`
  whatever the time in force, so an order the strategy expected to terminate in
  milliseconds rested at the venue until it was cancelled. Post-only is Gate.io's
  `poc` time in force, a maker-only *resting* order: it still composes with GTC,
  and the two immediate combinations are now refused instead of substituted.
- **A completed order is no longer reported expired, and an untouched one is no
  longer reported filled.** The terminal status was read off Gate.io's
  `finish_as` reason before the fill quantities, which broke in both directions.
  An order that filled in full and came back with `finish_as=expired` was closed
  as `EXPIRED`, a state the platform allows no late fill out of, so the trade
  that completed it — which Gate.io publishes on a separate stream and routinely
  delivers afterwards — was discarded and the position was left short of it. In
  the other direction `liquidated` and `auto_deleveraged`, which Gate.io defines
  as cancellations, were reported `FILLED` with nothing filled, leaving the order
  open in the cache indefinitely. The quantities now decide whether an order
  completed; the reason only explains a non-completion.
- **A spot conditional order no longer discards `trigger_type`.** Gate.io's spot
  trigger object is `{price, rule, expiration}` with no price-type field, so a
  spot `STOP_MARKET` submitted with `MARK_PRICE` was armed on the last traded
  price instead — precisely the price such an order is usually written to avoid.
  Spot now accepts `DEFAULT` and `LAST_PRICE` and refuses the rest, as the
  futures path already did for the types it cannot encode.
- **A conditional order can no longer be armed as the opposite order type.** The
  venue takes a bare comparison rule and requires it to agree with the last
  price, and the rule was derived from the market alone. A BUY `STOP_MARKET`
  whose trigger sat below the market — a breakout entry — was therefore armed as
  a buy-if-touched that fires when the price *falls*, with no event to say so.
  When the market-implied rule contradicts the order type, the order is now
  rejected naming both.
- **Spot market orders no longer absorb a session time in force.** The spot path
  coerced everything but `FOK` to `ioc`, so `AT_THE_OPEN` and `AT_THE_CLOSE` were
  accepted there and rejected on the other three products. Every product now
  shares one mapping.
- **A base-denominated spot market buy keeps its execution guarantee.** The
  aggressive limit that stands in for Gate.io's quote-denominated market buy was
  always sent `ioc`, so a `MARKET`/`FOK` buy quietly became "fill whatever is
  available". The substitution is in the price only; the order's own time in
  force is now carried through.
- **Off-tick prices are refused instead of being sent.** The `BNB_USDT` perpetual
  and the longer-dated `ETH_USDT` delivery contracts quote two decimals but tick
  in `0.05`. `Instrument.make_price()` rounds to the precision and the
  `RiskEngine` checks precision rather than increment, so four of every five
  two-decimal prices reached the venue and came back as an opaque parameter
  error. An order or amendment whose price or trigger price is not a multiple of
  the instrument's tick is now rejected with the tick named.

### Added

- **`WalletQueryRefusedError` is part of the public API.** Gate.io answers a
  wallet query it will not serve with `FORBIDDEN`, `INVALID_UNIFIED_ACCOUNT` or
  `UNIFIED_ACCOUNT_NOT_ACTIVATED`, none of which says anything about what the
  ledger holds, while `USER_NOT_FOUND` means the wallet does not exist yet and
  therefore holds nothing. The adapter separates the two, but the type carrying
  the distinction was reachable only through an import path the documentation
  does not advertise, so no caller could act on it. It is now exported from the
  package root beside `WalletNotProvisionedError`, of which it remains a
  subclass — code that treats both alike stays correct, and code for which a
  refusal and an absence differ catches the refusal first.

- **Mark and index prices for options.** Gate.io publishes both per contract on
  `options.contract_tickers`, but the subscription guard admitted futures
  products only and the router matched the literal `futures.tickers`, so an
  options position had no mark price in the cache — and a node configured to
  value positions on mark prices lost its unrealized-PnL basis on exactly the
  instrument class where mark and last diverge most. Options now subscribe like
  any other derivative. Funding stays perpetual-only, because an option has no
  funding leg. The channel name is resolved by one function that both the
  subscribe path and the router read, so what is subscribed and what is routed
  cannot drift apart again.
- **Historical funding rates.** `request_funding_rates` answers from
  `GET /futures/{settle}/funding_rate`, a REST wrapper that had been in the
  package with no callers. The platform ships the whole path and five in-tree
  adapters implement the hook, so leaving it unimplemented was a gap rather than
  a choice. Perpetual-only, with the same refusal as the subscription; records
  are filtered to the requested window client-side, since the endpoint takes no
  time range. Each update carries the contract's funding `interval` but no next
  funding time — the endpoint publishes nothing about the next application, and
  deriving one from record timestamps that sit a second off the grid would
  reintroduce the approximation removed above.
- **Every instrument carries a tick scheme.** `Instrument.next_bid_price()`,
  `next_ask_price()` and their plural forms raised `ValueError` on every Gate.io
  instrument because none named one. Power-of-ten grids use NautilusTrader's
  pre-registered `FIXED_PRECISION_{n}`; a grid that is not a power of ten gets a
  `FixedTickScheme` registered as `GATEIO_TICK_{increment}_P{precision}` carrying
  the venue's own increment, which is how an on-tick price can be produced at all
  on those contracts.

### Documentation

- **What recovery does and does not do, on each path separately.** The recovery
  sections described one mechanism and one set of open defects; both were out of
  date and the description was true of the reconnect path only. `docs/execution.md`
  now states, under Startup as well as Reconnect, that the sweep which re-offers
  a trade the engine dropped runs only after a reconnect, and what a restart
  coinciding with a fill therefore costs. The three defects an independent review
  of the last round demonstrated are stated there, listed in
  `docs/review-matrix.md` as open, and `docs/roadmap.md` no longer claims the
  recovery work is closed.

- **What an unimplemented subscribe hook really does.** `docs/market-data.md`
  said the missing `OrderBookDepth10` subscription would "fail visibly rather
  than sitting there delivering nothing". It does both. The live client records
  the subscription before it starts the task that raises, and the data engine
  skips any instrument already in that list, so the caller gets one exception in
  the log and a phantom subscription the client reports as held for the rest of
  its life, never retried and never mentioned again. The page now says so, names
  the two platform call sites that cause it, and tells the reader to trust the
  log line rather than the subscription list. A test drives the real platform
  path, so the paragraph fails if a later NautilusTrader changes the ordering.
- **The order book's depth window is documented as an open question, not an
  answer.** The venue describes the incremental channel's `level` as the
  "optional depth level interested", which does not say whether a level pushed
  out of the top-N window is reported as removed. The adapter assumes neither
  reading — trimming would discard depth Gate.io may still be maintaining — and
  both the module and the page now state that, along with the one observation
  that would settle it.
- **Order emulation is documented.** Every order type this adapter denies —
  trailing stops, conditional orders on options — and every contingency
  relationship can be traded against Gate.io by letting NautilusTrader emulate
  the order locally and send this client only the `MARKET` or `LIMIT` it
  releases. The pages said "Unsupported" and "Contingent orders have to be
  managed by the strategy", neither of which was true. `docs/products.md` now
  carries an *Order emulation* section, including the caveat that installed
  1.230.0 accepts only three `emulation_trigger` values and **cancels** an order
  that names any other, and separates the types Gate.io cannot do from the ones
  this adapter has not implemented yet (both trailing types and attached
  take-profit / stop-loss exist at the venue).

## [0.2.0a1] - 2026-07-26

An alpha. 0.1.0 was a spot-only adapter with a flat module layout; 0.2.0a1 is a
multi-product connector built on real venue data throughout. See
[docs/migration-0.1-to-0.2.md](docs/migration-0.1-to-0.2.md) for the upgrade
path.

Released as an alpha, not a stable version, because real-world validation and
external user feedback are still limited. The test suite is extensive and runs
without credentials, but a passing suite is evidence about the code, not about
the exchange. Treat every capability as needing your own verification before it
carries money. The per-capability status is in
[docs/validation.md](docs/validation.md); the audit trail behind the code is in
[docs/review-matrix.md](docs/review-matrix.md).

0.1.0 remains available: its tag and release are untouched and its
implementation is preserved on the `legacy/v0.1.0` branch.

### Fixed since the 0.2.0 development line

- **Fills arriving before the order message are no longer lost.** Gate.io
  publishes order and user-trade updates on independent channels, so for a
  conditional order the first message that mentions the fired order is
  frequently its fill. Only the order path rebased the venue order id; the fill
  path emitted an event the platform then refused, and the exception was
  swallowed into a log line. The fill path now reconciles the identity first,
  and a fill whose identity cannot be reconciled inline goes through
  reconciliation instead of being emitted and dropped.
- **One timestamp conversion instead of two.** A binary-float copy in the data
  module disagreed with the canonical decimal implementation by 64 ns on
  millisecond timestamps, so one venue instant became two different values
  depending on which path carried it.
- **The shared HTTP transport is released on shutdown.** It was reference
  counted but nothing ever acquired or released it, so the connection pool
  outlived every trading node in the process. A cached transport that has been
  closed is now rebuilt rather than handed to the next node.
- **The documented book intervals match the code.** The configuration comment
  claimed spot and the perpetuals accept all three intervals; they accept two.

### Changed (breaking)

- **Venue string is now `GATE_IO`** (was `GATEIO`), matching how NautilusTrader
  identifies this exchange elsewhere. Every instrument id and bar type changes:
  `BTC_USDT.GATEIO` becomes `BTC_USDT.GATE_IO`.
- **Perpetual instrument ids carry a `-PERP` suffix** (`BTC_USDT-PERP.GATE_IO`).
  527 USDT perpetuals share their exact symbol with a spot pair, so the two must
  be distinguishable. Delivery and option symbols carry their expiry and take no
  suffix. An id without the suffix now resolves to the spot pair.
- **Execution defaults to mainnet** (was testnet). An execution client that
  silently points at a different exchange environment than the operator believes
  is more dangerous than one that requires the venue to be stated. Set
  `environment="testnet"` explicitly for the testnet, which serves spot and USDT
  perpetuals only; configuring any other product with it now raises `ValueError`
  before any network activity.
- **The `live_orders` kill switch and `LiveOrdersDisabledError` are removed.** A
  boolean inside the process is not a security boundary. The controls that bind
  are API key permissions, IP allow-listing, an explicitly chosen `environment`,
  and NautilusTrader's own sandbox/backtest execution.
- **Synthetic quotes are removed.** `emit_synthetic_quotes` is gone; quotes now
  come from the venue's real `book_ticker` best bid/offer stream. Nothing
  fabricated is published as venue data.
- **The paper-fill simulator is removed** (`PaperExecution`, `PaperFill`,
  `GateioPaperConfig`). Use NautilusTrader sandbox or backtest execution.
- **The standalone `reconcile()` helper is removed**, superseded by the real
  NautilusTrader report generators.
- **The package is sub-packaged**: `nautilus_gateio.common`, `.http`,
  `.websocket`. The top-level `__init__` re-exports the public API, so
  `from nautilus_gateio import GateioDataClient` keeps working; deep imports of
  the old flat modules must be updated.
- **The REST client is async and namespaced.** `GateioHttpClient` is a shared
  `async` transport; per-product calls live on `GateioSpotHttpAPI`,
  `GateioMarginHttpAPI`, `GateioFuturesHttpAPI`, `GateioOptionsHttpAPI` and
  `GateioWalletHttpAPI`. `ping()`, `balances()`, `open_orders()`,
  `place_order_validated()`, `cancel_all()` and `emergency_stop()` no longer
  exist on the client.
- **`GateioWebSocketClient` takes the endpoint and the product it serves**,
  because a Gate.io message is only interpretable together with the host it
  arrived on. Prefer `GateioPublicWebSocket` / `GateioPrivateWebSocket`.
- **Configuration fields changed.** Removed: `venue`, `use_websocket`,
  `poll_interval_secs`, `emit_synthetic_quotes`. Renamed:
  `account_poll_interval_secs` to `account_polling_interval_secs` (default
  30.0). Added: `products`, `options_underlyings`, `environment` on the data
  client, `base_url_ws`, `spot_account_mode`, `update_instruments_interval_mins`,
  `http_timeout_secs`, `max_retries`, `order_book_snapshot_limit`,
  `order_book_update_interval_ms`, `bars_timestamp_on_close`.
- Renamed public symbols: `instrument_id_to_gate_pair` to
  `instrument_id_to_gateio`, `gate_pair_to_instrument_id` to
  `gateio_to_instrument_id`, `build_currency_pair` to `parse_spot_instrument`,
  the futures clients to `GateioFuturesHttpAPI`, `GATEIO_WS_MAINNET` to
  `GATEIO_WS_SPOT`. `StaticInstrumentProvider` is removed.
- **Market orders are no longer emulated with a fixed 1% cross.** Spot market
  sells and quote-denominated market buys are native venue market orders; only a
  base-denominated spot market buy is expressed as an IOC limit, bounded by the
  pair's own published slippage cap.

### Added

- **Products**: spot, USDT perpetual futures, BTC-settled (inverse) perpetual
  futures, USDT delivery futures and USDT-settled options. One data client and
  one execution client multiplex every configured product.
- **Spot margin as an execution mode** (`spot_account_mode`): plain spot,
  isolated margin, cross margin and unified account, with the balance, borrow
  and repay endpoints each ledger needs.
- **Real market data**: trade ticks, best bid/offer quote ticks,
  sequence-validated order book deltas with resync on gap, closed bars from 1s
  to 7d, mark prices, index prices and funding rates.
- **Order types**: MARKET, LIMIT, STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED and
  LIMIT_IF_TOUCHED, the last four through each product's price-trigger endpoint.
  Time in force GTC, IOC and FOK, post-only via `poc`, plus `reduce_only`,
  `display_qty` (iceberg) and `quote_quantity` on a spot market buy.
- **Order modification** on spot and perpetuals; delivery and options reject it
  explicitly, because the venue has no amend endpoint there.
- **Private WebSocket** as the primary execution event source: orders,
  usertrades, balances and positions per product, with REST reconciliation after
  every reconnect.
- **Full reconciliation**: `generate_order_status_reports`,
  `generate_order_status_report`, `generate_fill_reports` and
  `generate_position_status_reports`, all against REST.
- **Internal wallet transfers** (`GateioExecutionClient.transfer`) between the
  account's own trading wallets, which is also how Gate.io creates the
  derivative wallets in the first place.
- Instruments for every product: `CurrencyPair`, `CryptoPerpetual` (linear and
  inverse), `CryptoFuture` and `CryptoOption`, with contract-count quantity
  semantics and the venue's multipliers.
- Documentation set rewritten against the current code, plus new pages on
  [symbology](docs/symbology.md), [products](docs/products.md),
  [migration](docs/migration-0.1-to-0.2.md), [validation
  status](docs/validation.md) and [releasing](docs/releasing.md).

### Fixed

- **Documentation described a testnet default that the code does not have.**
  Every page now states the mainnet default, and a regression test compares the
  documented configuration defaults against the actual struct fields.
- **Documentation advertised removed features** — the `live_orders` kill switch,
  synthetic quotes, the paper module, the standalone reconciliation helper and
  the old venue string. All removed, with a test that fails if the vocabulary
  reappears.
- **CI could not detect a broken package list.** The wheel verification now
  installs into a clean environment outside the source tree and imports each
  sub-package and every documented entry point, so a distribution missing
  `nautilus_gateio.common`, `.http` or `.websocket` fails the build.
- **Stale artefacts could be republished.** The build job removes `dist/`,
  `build/` and `*.egg-info` before building, and the release checklist uploads a
  version-pinned glob instead of `dist/*`.
- Examples rewritten against the current API; the credential-free ones are run
  as part of the release checklist.

### Security

- Order-mutating REST requests are never replayed automatically; an ambiguous
  outcome is reported as ambiguous rather than resubmitted.
- Hedge (dual) position mode is detected and refused, never switched on; a
  unified account is never upgraded automatically.
- No withdrawal, sub-account transfer, Earn, Gate Pay, P2P, Copy Trading or Gate
  Bots code exists in the package.

## [0.1.0] - 2026-07-25

Initial release.

### Added

- Spot market-data client (`GateioDataClient`) with WebSocket streaming and REST polling fallback for trade ticks and quotes.
- Spot execution client (`GateioExecutionClient`) with a testnet-first design: live mainnet orders are disabled unless explicitly enabled in configuration.
- Instrument provider (`GateioInstrumentProvider`) loading spot currency pairs from the Gate.io REST API, plus a `StaticInstrumentProvider` for offline use.
- Reusable HTTP client (`GateioHttpClient`) with retry handling and a token-bucket `RateLimiter`.
- Reusable WebSocket client (`GateioWebSocketClient`) with automatic reconnection and subscription replay.
- Gate.io API v4 request signing (`sign_request`) and client order ID generation/sanitization helpers.
- Order validation with typed errors (`OrderValidationError`, `LiveOrdersDisabledError`) and retry classification (`should_retry`).
- Rate limiting applied across REST endpoints to respect Gate.io API limits.
- Local paper-fill simulator (`PaperExecution`) for testing strategies without sending orders to the exchange.
- Experimental futures REST client (not integrated with the Nautilus execution path).
- Live client factories (`GateioLiveDataClientFactory`, `GateioLiveExecClientFactory`) for `TradingNode` integration.
- Order state reconciliation helper (`reconcile`).
- Documentation set: architecture, configuration, market data, execution, testing, and troubleshooting guides.
- Unit test suite (no network access required) and continuous integration workflow.

[Unreleased]: https://github.com/x03f/nautilustrader-gateio-adapter/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/x03f/nautilustrader-gateio-adapter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/x03f/nautilustrader-gateio-adapter/releases/tag/v0.1.0
