# Errors

This page is the error taxonomy: every exception this adapter raises on
purpose, what each one says about the venue versus the request, what the caller
should do about it, and — for most of them — why the caller is NautilusTrader
rather than you.

## Who actually sees an exception

Strategy code sees **events, not exceptions**. The execution client converts
every failure of an order command into the outcome the platform defines for it:
a refusal this client decided becomes `OrderDenied`, a refusal Gate.io made and
proved becomes `OrderRejected`, and a failure that proves neither leaves the
order in flight for the execution engine's in-flight check to resolve. The
reconciliation exceptions (`PositionStatusUnavailable`, `FillReportsUnavailable`,
`OrderReportsUnavailable`) are likewise raised *at* the engine, which treats a
raised query as a failed query — that is the point of raising them.

You meet the exceptions directly in two situations: when driving the HTTP
namespaces or the instrument provider from your own code (the scripts in
`examples/` do this), and when reading the log lines the client writes as it
handles them for you.

## The taxonomy at a glance

| Exception                     | Import from                   | Says                                                                                       | Your move                                                          |
|-------------------------------|-------------------------------|--------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| `GateioError`                 | `nautilus_gateio`             | Base of every venue/transport failure; carries `status`, `label`, `message`                | Branch on the subclass or the label                                |
| `GateioClientError`           | `nautilus_gateio`             | 4xx — Gate.io refused this request (bad params, auth, limits)                              | Fix the request; retrying unchanged repeats the refusal            |
| `GateioServerError`           | `nautilus_gateio`             | 5xx — Gate.io reported an internal failure                                                 | Usually retryable; on a mutating call see the ambiguity section    |
| `GateioRequestAmbiguousError` | `nautilus_gateio.http.client` | The request may have reached the venue; the outcome is unknown                             | Reconcile (query the order, poll the transfer) before resubmitting |
| `GateioAmbiguousServerError`  | `nautilus_gateio.http.client` | 5xx on a mutating request — both of the above at once                                      | Same as ambiguous; server-error handlers still catch it            |
| `OrderValidationError`        | `nautilus_gateio`             | The order violates an exchange constraint (tick grid, whole contracts, expiry in the past) | Correct the order; nothing was sent                                |
| `UnsupportedOrderError`       | `nautilus_gateio`             | Gate.io cannot express this order without changing its meaning                             | Submit what the venue can express, or emulate locally              |
| `WalletNotProvisionedError`   | `nautilus_gateio`             | This wallet does not exist yet on the account — a definite absence                         | Ignorable until you fund/transfer into the wallet                  |
| `WalletQueryRefusedError`     | `nautilus_gateio`             | Gate.io refused to answer the query — nothing is known about the ledger                    | Fix the key permission or account mode; do not read as "empty"     |
| `PositionStatusUnavailable`   | `nautilus_gateio.execution`   | A position query got no usable answer                                                      | Handled by the engine; fix the underlying refusal it names         |
| `FillReportsUnavailable`      | `nautilus_gateio.execution`   | A trade listing did not answer in full; carries what did answer readably                   | Handled by the engine; fix what the message names                  |
| `OrderReportsUnavailable`     | `nautilus_gateio.execution`   | An order listing did not answer in full; carries what did parse                            | Handled by the engine; at startup the node refuses to start        |

`should_retry(error)` is also exported at the package root: it answers "is this
failure worth retrying" the same way the transport answers it internally.
`error_from_response(status, label, message)`, which builds the right subclass
from a raw response, lives in `nautilus_gateio.common.errors`.

## The response hierarchy: `GateioError`

Gate.io API v4 reports failures as a JSON body of the form
`{"label": ..., "message": ...}` under an HTTP status code. `GateioError`
preserves all three — `status`, `label`, `message` — so a caller can branch on
either the HTTP status or the venue's own label, and its string form is
`Gate.io {status} {label}: {message}`. Error messages never include request
signatures or credentials.

`error_from_response` sorts by status: 5xx builds `GateioServerError`,
everything else `GateioClientError`. Three errors are raised with **status 0 or
a locally assigned status** because no response produced them — each is raised
only when no byte of the request left the process:

| Label                 | Status | Raised when                                                                            |
|-----------------------|--------|----------------------------------------------------------------------------------------|
| `NETWORK_ERROR`       | 0      | Every attempt failed before anything was sent — the venue cannot have seen the request |
| `CLIENT_CLOSED`       | 0      | The shared `GateioHttpClient` was closed (or acquired after closing)                   |
| `MISSING_CREDENTIALS` | 401    | A signed endpoint was called without API credentials                                   |

That reservation is deliberate: a status-0 error is *proof of non-delivery*,
which is what lets the execution client treat these as definitive rather than
ambiguous (see below). `NETWORK_ERROR` is never used for a request that was on
the wire — that case has its own type.

## What a 4xx and a 5xx mean

A `GateioClientError` is the venue's own refusal, and it is **definitive**: the
platform's live-trading rules name HTTP 400, 401, 403 and 429 as proof of
non-acceptance, so the execution client may emit `OrderRejected` on it.
Retrying the same request unchanged will produce the same refusal, with two
exceptions the transport already knows about (429 and `REQUEST_EXPIRED`, both
rejected *before* processing — see retries).

A `GateioServerError` is Gate.io reporting an internal failure. On a read it is
usually transient and the transport replays it. On a mutating request it is
something worse: a 5xx can be reported either before or after the venue applied
the request, so the transport deliberately does *not* replay it and raises
`GateioAmbiguousServerError` instead.

## Ambiguity: `GateioRequestAmbiguousError`

Gate.io offers no request-level idempotency token, so the transport never
replays a mutating request unless the venue has proved it was rejected before
processing. When that proof is missing and the request may already have been
applied, the error raised is `GateioRequestAmbiguousError`, in one of three
shapes:

* a mutating request (`POST`, `PUT`, `PATCH`) failed *after* it was on the wire
  — replaying it could execute it twice, so it was not replayed;
* an idempotent request (including `DELETE`) was replayed to exhaustion and
  never answered — a replay makes a duplicate harmless, not the outcome known,
  and a cancel the venue applied but could not report back is indistinguishable
  from one it never received;
* Gate.io answered 5xx to a mutating request — raised as
  `GateioAmbiguousServerError`, which subclasses both `GateioServerError` (so
  code branching on server errors is unaffected) and
  `GateioRequestAmbiguousError` (so one `isinstance` check identifies "this
  mutation may or may not have been applied").

The caller's obligation is to **reconcile, not resubmit**: query the order by
its client order id, or poll the transfer status. Resubmitting on ambiguity is
how an order gets executed twice.

Both classes live in `nautilus_gateio.http.client`, not in the package root:
you only need their names when driving the HTTP layer yourself, and
`GateioRequestAmbiguousError` *is* a `GateioError`, so existing handlers catch
it without knowing the name.

### What the platform does with ambiguity

Inside the execution client this classification is `is_ambiguous_outcome`, and
the handling is the whole handling NautilusTrader prescribes: the client logs
the failure, emits **no** event, and leaves the order in `SUBMITTED`,
`PENDING_UPDATE` or `PENDING_CANCEL` — exactly the states the engine's
in-flight check (`_check_inflight_orders`, verified in installed 1.230.0)
re-queries through `generate_order_status_report`. The venue's answer resolves
the order; if the query itself stays unanswered, the engine — not this client —
decides the outcome once `inflight_check_retries` is spent. Anything that is
not a `GateioError` at all is classified ambiguous too, because the adapter
failing around a request (say, while parsing a success response) proves nothing
about what the venue did. Emitting a rejection there instead would be
unrecoverable: `OrderRejected` is terminal, and the `OrderAccepted` that
reconciliation would later need to apply raises `InvalidStateTrigger` against
it.

## Retries: what the transport replays for you

The transport (`GateioHttpClient`) replays a failed request only when the
replay provably cannot change the outcome at the venue:

* **Idempotent methods** — `GET`, `HEAD`, `OPTIONS`, `DELETE` — are replayed on
  transient failures. `DELETE` qualifies because Gate.io's cancel endpoints
  answer a second cancel with `ORDER_NOT_FOUND` / `ORDER_CLOSED` rather than
  performing a second action.
* **Mutating methods** are replayed only on HTTP 429 or the labels that prove
  the request was rejected before processing: `TOO_MANY_REQUESTS`,
  `REQUEST_EXPIRED`.
* Everything else on a mutating request is raised, never replayed.

`should_retry` is the transient-condition test the transport combines with the
rules above: true for any `GateioServerError`, and for a `GateioError` with
status 429 or one of the labels `TOO_MANY_REQUESTS`, `SERVER_ERROR`,
`INTERNAL`, `TIMEOUT`, `REQUEST_EXPIRED`. Note the division of labour: whether
a failure is *worth* retrying (`should_retry`) and whether this particular
request is *safe* to retry (method plus label) are separate questions, and a
replay happens only when both answer yes. On 429 the client also backs off its
own pacing (`max_retries`, default 3, bounds total attempts; the default pace
is 8 requests/second — see [configuration.md](configuration.md), "What is not
configurable").

## The adapter's own refusals: `OrderValidationError` and `UnsupportedOrderError`

Both are pre-flight refusals raised while the request is being built, before
anything is sent — which is why `is_ambiguous_outcome` classifies them
definitive however deep in a submit path they occur. The distinction between
them:

* **`OrderValidationError`** — the order as given violates an exchange
  constraint and a corrected order could be submitted: a price or trigger price
  off the instrument's tick grid, a fractional or non-positive contract
  quantity, a `display_qty` in fractional contracts, an expire time already in
  the past, a base-denominated spot market buy that cannot be priced because no
  reference price is available, a cancel with no venue order id known.
* **`UnsupportedOrderError`** — the order *as a concept* cannot be represented
  on Gate.io without changing its meaning, and the adapter refuses instead of
  silently substituting: reduce-only on spot, post-only on a market order,
  post-only combined with IOC/FOK, `quote_quantity` on derivatives, a
  conditional order whose trigger sits on the side of the market that would arm
  it as the opposite order type.

What you observe as a user depends on where the refusal lands:

* On **submission**, both are caught and become one `OrderDenied` before any
  event claims a request reached Gate.io — as does anything else that fails
  while the request is being built, since nothing has been sent yet.
  `OrderDenied` is the platform's event for "denied by Nautilus" — no
  `venue_order_id`, no `account_id`, no venue involved.
* On **amendment**, they become `OrderModifyRejected` with the reason; on
  **cancellation** (a cancel for an order with no venue order id known),
  `OrderCancelRejected`.
* After a submission was already announced, a malformed *success* payload can
  raise `OrderValidationError` or `ValueError` while being parsed — that is
  handled as an ambiguous outcome (the venue may be holding the order), not as
  a denial.

## Wallet states: `WalletNotProvisionedError` and `WalletQueryRefusedError`

Gate.io reports two very different facts through the same 4xx error shape, and
`require_wallet` (in `nautilus_gateio.http.margin`, applied to every wallet,
balance and position read) separates them by label:

* **`WalletNotProvisionedError`** — label `USER_NOT_FOUND`. Gate.io creates the
  futures, delivery and options wallets on the first internal transfer into
  them and reports `USER_NOT_FOUND` until then. A wallet the venue has not
  created holds nothing, so this is a **definite absence**: an account trading
  only spot starts cleanly, a fill query treats the product as answering
  "none", and the position path may honestly say there is no position there.
* **`WalletQueryRefusedError`** — labels `FORBIDDEN` (the API key lacks the
  permission), `INVALID_UNIFIED_ACCOUNT` and `UNIFIED_ACCOUNT_NOT_ACTIVATED`
  (the account is not in the mode the endpoint needs). The venue rejected the
  *question*. Nothing follows about what the wallet holds — it may hold an open
  position of any size.

The second is a subclass of the first, and the direction is deliberate: a
caller that legitimately treats both alike — balances, where a ledger that
cannot be read keeps its previous figures either way — keeps working with one
`except WalletNotProvisionedError`. A caller for which the difference decides
whether an execution is invented must catch `WalletQueryRefusedError` **first**.
The position path is that caller: filing a refusal as an absence would let the
engine square a live position with a reconciliation order and an inferred fill
(see below). The exception messages carry the remedy for each label;
[troubleshooting.md](troubleshooting.md) has the same remedies with more
context.

## What a report parser believes: the exact-read rule

The reconciliation exceptions in the next section are fed by one rule, applied
to every field of a venue report that decides money — an order's side, type,
quantity and filled quantity, a fill's price, quantity, fee and execution time,
a position's size:

* **A value the venue stated and this client cannot read raises.** Gate.io
  moved its futures size fields from integer to string in v4.106.0, so a shape
  drift is a live possibility, and the forgiving readers turned unreadable
  bytes into confident claims — an unreadable position size read as FLAT, an
  unreadable `left` read as fully filled. The strict readers (`to_lot_count`
  and `to_exact_decimal` in `nautilus_gateio.common.parsing`) refuse instead,
  naming the field and the value.
* **A value the venue did not state makes no claim.** Absence takes the
  documented smaller meaning where one exists — an absent fee is a fee of zero,
  an absent price makes the order a market order — or leaves the question to
  the caller. It is never treated as an unreadable value.
* **An explicit zero is believed.** A close-position order genuinely has
  `size: 0`; a fill listing stating a zero amount states "no execution". The
  venue's affirmative zero is read exactly, not filtered as if it were a
  default.

Two consequences of the rule are worth knowing because they change what a
report carries. A spot fill that states a **nonzero fee but no `fee_currency`**
is refused rather than booked in a guessed currency — Gate.io states the fee
currency on every documented spot trade row, and it is the base currency for
the ordinary buy and the quote for the ordinary sell, so there is no correct
guess. And the **average price** on an order status report follows all three
clauses: stated-and-unreadable raises, an unfilled order's absent average makes
no claim, and a readable zero is reported as no average, because zero is not a
price.

The raise from a single row does not surface as a `ValueError` at the engine:
the listing callers convert it into the loud per-query exception below, so an
unreadable row fails the listing it belongs to instead of silently shrinking
it.

## Reconciliation signals: the `*Unavailable` exceptions

`PositionStatusUnavailable`, `FillReportsUnavailable` and
`OrderReportsUnavailable` live in `nautilus_gateio.execution` and are **raised
at NautilusTrader**, not at you. All three exist because a returned list cannot
say "I was not answered" — to the engine, a missing report and a report of
absence are the same thing, and the only channel an execution client has for
"the query failed" is to raise.

### `PositionStatusUnavailable`

Raised by `generate_position_status_reports` when the venue was asked about a
position and no usable answer came back:

* a `WalletQueryRefusedError` on the product's ledger, or any other per-product
  failure;
* a position row this client cannot read — the raise names the row, the field
  and the value;
* an account-wide query that would have to omit an open spot position it has no
  authority to answer for (Gate.io keeps no venue-side spot position, and an
  account-wide answer that merely omitted it would be read as a claim of
  flatness);
* a **stale read**: during recovery, a position row — or an absent row asserted
  as flat — that neither contains the venue trades this recovery pass just
  booked nor carries a venue timestamp postdating them. The answer was read
  before those trades, so it is not a statement that they did not happen, and
  asserting FLAT from it would have the engine square away the very trades the
  trade listing named.

Raising is load-bearing: verified against installed 1.230.0, the engine's
`_did_position_status_query_fail` skips a venue whose query raised, where a
silent or FLAT answer would flow into `_create_flat_position_report` and close
a still-open position through an execution nobody performed.

### `FillReportsUnavailable`

Raised by `generate_fill_reports` when the trade listing did not answer in
full: a product's listing endpoint failed, or the listing answered with rows
this client cannot read under the exact-read rule. The engine keeps exactly one
brake against squaring a position to flat on a failed query: its
`_query_and_find_missing_fills` sets `had_fill_query_errors` when a client's
`generate_fill_reports` raises — and from nothing else (verified in installed
1.230.0) — and `_process_cached_position_discrepancies` refuses to square while
that flag is set. A client that logged the failure and returned what it had
would report the failure as "no fills", the brake would never engage, and the
position would be closed with a synthetic trade id and zero commission —
permanently, because a closed position is not queried again.

Everything the venue *did* answer readably is carried on the exception as
`.reports` rather than discarded, folded across products, for a caller that
can use a loud partial answer. The adapter's own recovery paths deliberately
are not such callers: reconciling order reports whose filled quantities the
missing trades were meant to back makes the engine mint commission-less
inferred stand-in fills for the difference. So at startup a failed order or
trade listing refuses the whole mass status (see `OrderReportsUnavailable`
below), and the reconnect handler catches this exception and keeps the
pre-reconnect state — stale but honest — until a listing answers in full.

### `OrderReportsUnavailable`

Raised by `generate_order_status_reports` under the same rule: a product's
order listing failed, or a listing row's deciding field could not be read. The
two are the same answer — what the venue holds is unknown, and a report set
that merely omits the unreadable part is indistinguishable from "the venue has
no such order", which would leave a cached order the omitted row would have
closed open locally forever.

What the engine does with the raise, verified against installed 1.230.0, is
worth stating precisely because the three paths differ:

* **At startup**, this client's `generate_mass_status` turns the raise into a
  `None` mass status, `reconcile_execution_state` returns `False`, and the
  kernel refuses to start the trader. Nothing is fabricated; the node does not
  run until the venue answers in full.
* **On the continuous open-order check** (`open_check_interval_secs`), the
  raise is swallowed *per client*: the engine gathers report tasks with
  `return_exceptions=True`, logs an ERROR and proceeds, treating this client's
  answer as empty. Under the default `open_check_open_only=True` that is
  harmless — orders missing from the empty answer are only debug-logged and
  retry counters tick. With `open_check_open_only=False`, an own order whose
  row stays persistently unreadable is counted missing on every cycle, and
  once `open_check_missing_retries` is exhausted the engine resolves it with a
  fabricated `REJECTED` or `CANCELED` while the venue may hold it open. That
  is a **documented limitation** of running that non-default mode against this
  alpha: if you enable `open_check_open_only=False`, treat a repeated
  `OrderReportsUnavailable` ERROR in the log as an operational page, not
  noise.
* **On the single-order query** (the in-flight check), the caller catches the
  failure and answers `None`, which the engine treats as an unanswered query
  and retries.

### What does not raise

A `WalletNotProvisionedError` inside any of these queries does **not** raise
them: a ledger that does not exist holds no orders, no trades and no positions,
which is a definite answer, not a failed query.

## What NautilusTrader handles for you

The division of labor:

* **Order commands**: every exception becomes `OrderDenied`, `OrderRejected`,
  `OrderModifyRejected`/`OrderCancelRejected`, or a logged unresolved outcome
  the engine's in-flight check resolves. You handle events.
* **Transient transport failures**: retried by `GateioHttpClient` within the
  safety rules above; you see log lines, not exceptions.
* **Reconciliation failures**: the `*Unavailable` exceptions are consumed by
  the engine's reconciliation and position-check machinery; your job is to fix
  what the message names (usually a key permission, an account mode, or a
  payload shape this client refuses to guess about).
* **What is yours**: exceptions from code that calls the HTTP namespaces, the
  instrument provider or the symbology helpers directly — there the taxonomy
  above is the contract.

Offline tests drive the real platform state machine. The only error handling a
live run has exercised is what the recorded runs met: a post-only refusal and a
repeated cancel on spot, a reduce-only refusal on the perpetual, and the local
denial of a sell the account could not cover (see
[validation.md](validation.md)).
