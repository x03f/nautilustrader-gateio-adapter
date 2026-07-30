# Roadmap

Where this adapter is going and how it gets there. Written so that a contributor can see what is
being worked on, judge whether a change belongs, and pick something up.

## The goal

A complete, independent Gate.io adapter for NautilusTrader — one that lets anyone obtain market data
and trade Gate.io's main products through Nautilus' standard interfaces.

The measure is not that the package installs. It is that Gate.io is properly integrated into
NautilusTrader's model: data, execution, order and account state, recovery, across products and
account modes. It must hold not only on the happy path but after dropped connections, restarts,
missed events, and disagreement between REST, the private WebSocket and Nautilus' own state.

Every product and account mode carries a status it has earned — implemented, mock-tested,
testnet-validated, or mainnet-confirmed — and is never described as more proven than it is.

## On Python

NautilusTrader's in-tree adapters are moving to a Rust core with a thin PyO3 binding, and its
developer guide recommends that shape. This adapter is pure Python. That is a deliberate, stated
deviation, not an oversight.

It changes nothing about correctness. Every behavioural contract the platform defines — event
ordering, order and account state, report contracts, order-book flags, reconciliation — applies in
full and is treated as binding here. What does not bind an externally distributed package is the
in-tree repository layout, build tooling and release process.

Note that using `nautilus_trader.core.nautilus_pyo3` is not a Rust migration. Its `HttpClient`,
`WebSocketClient` and `Quota` are ordinary Python APIs that happen to be implemented in Rust
underneath; adopting them is adopting the platform.

A Rust rewrite is considered only after a full Python release stands at the level of the official
adapters, and only on demonstrated need. It is not promised and has no timeline.

## Stages

Each stage has exit criteria that are checkable by someone who did not do the work. A stage is not
finished because its changes are written; it is finished when its criteria hold.

### Stage 0 — Foundation

Close what is already known to be wrong before adding anything.

- **Recovery and reconciliation.** Six rounds passed their own harnesses and none survived an
  attempt to refute it; passing is not evidence. The sixth round set out to close the three
  defects listed below and was put in front of three independent verifiers — one on
  restart-versus-reconnect parity, one on the unreadable-position claim, one on claim accuracy.
  All three refuted it. One of the three fixes it attempted survived, one survived but did not go
  far enough, and one was withdrawn. The seventh round closed what remained against a gate that
  now owns the parity comparison that refuted the sixth, and passed that gate and the repository
  suite. The refutation attempt then ran, in the same three-way shape, and two of the three
  verifiers refuted the round. Claim accuracy held in full: the commit describes exactly what the
  tree does, every cited test fails on revert, and the gate fails the exact scenarios claimed on
  the reverted tree. What the other two broke, and what they left standing, is recorded per fix
  below; the remainders were `REC-05` and `REC-06` in the
  [review matrix](review-matrix.md#recovery-findings-raised-after-this-review). The eighth round
  closed `REC-06` in full and narrowed `REC-05` to one surviving family, recorded as `REC-07`;
  the ninth round closed that family — the eighth- and ninth-round blocks at the end of this
  bullet record what each round claimed, what its audit found, and what remains.

  **Closed.** The fill-report query now raises when a product's trade listing fails, carrying the
  reports the other products did answer. That raise is the only thing that arms the engine's brake
  against squaring a position to flat on a failed query, so a 5xx on the trade listing no longer
  closes a position with a synthetic trade id and no commission. Reverting it makes four tests and
  a two-cycle harness scenario fail.

  **Built in the seventh round, against a gate that now measures both routes; the refutation
  kept the mechanisms and re-opened two boundaries.** The release gate's dual-route parity family
  drives one set of venue answers through a reconnect and through a restart on independent
  caches, anchors each outcome to the account state the venue's answers describe, and only then
  compares the routes field by field — so a repair of one route that leaves the other behind
  fails the gate instead of surviving to the next refutation.
  - The unreadable-size half of the position finding: `size` is now read strictly
    (`to_lot_count`), so a missing key, null, an empty string, a non-numeric string, a boolean and
    any value that is not an exact whole number of lots raise `PositionStatusUnavailable` naming
    the row and the field, while a row that genuinely reads zero — including the stringified zeros
    of v4.106.0 — still parses to FLAT and still squares the book. Held by
    `TestUnreadablePositionSizes` and `TestLotCount`; every unreadable-size case fails on the
    previous tree. The refutation left this standing and broke its boundary: strictness stops at
    this one field. The deciding fields of the fill and order parses — futures order `left` and
    `size`, futures and options fill `size`, spot fill `amount`, every fill `price` — still ride
    forgiving readers with silent defaults, and through the real engine an unreadable `left`
    becomes a confident full fill the engine fabricates the rest of, an unreadable fill `size`
    silently replaces a venue-confirmed execution with an inferred fill carrying no commission,
    and reconciliation reports success over both. That is `REC-06`, and the repair direction is
    the round's own tool: the same strict read and the same raise on every field that decides
    money, keeping the venue's affirmative zero as the one believed zero.
  - The restart half: the unapplied-fill sweep now also runs *inside* `generate_mass_status`,
    before the engine has reconciled anything — the one moment on the startup route at which
    cached orders carry their venue ids, nothing has been deduplicated, and no position report has
    been reconciled. A fill booked there updates the cache, so the engine's duplicate filter
    deletes a now-matching snapshot harmlessly and a position report that already contains the
    trade reconciles against a book that already carries it. Two consequences are handled
    explicitly: a snapshot the sweep outran is withheld from the mass status where the engine
    would misread it as corrupted cache and fail node start, and a position answer equal to the
    pre-booking book that cannot be shown to postdate the booked trades is answered
    `PositionStatusUnavailable` (the read-skew rule in `_position_answer_is_stale`). Held by
    `TestStartupRecoverySweep`, `TestStalePositionAnswersAfterRecovery`, and the gate's seven
    dual-route parity scenarios, all of which fail on the previous tree. The refutation left the
    sweep standing — order state is right on both routes across a 33-cell restart/reconnect
    matrix in which the previous tree fails 23 cells — and broke the read-skew rule, in both
    directions. Equality with the pre-booking book is the only staleness it recognises, so an
    answer staler than that memory — an absent row, or a kept zero-size row, both of which
    Gate.io produces for a traded contract — erases a pre-existing position with a fabricated
    execution and a reconciliation that reports success, while the same shapes over a flat
    pre-outage book are withheld fail-safe; and a *current* answer that happens to equal that
    memory is refused, so an ordinary fresh-cache startup whose closed round trip straddles the
    lookback window cannot start until the trades age out. That is `REC-05`, and the repair
    direction is written on the method itself: an answer that cannot contain the trades just
    booked and cannot be shown to postdate them supports no claim, whichever book it equals —
    withholding it degrades to the refusal already accepted for the flat slice, never to a
    fabrication.
  - REC-04, older than these rounds: an unfinished quote-denominated spot market buy no longer
    yields an order status report built from its running partial `filled_amount`; its executions
    come from the trade listing and the order's statement from a re-read once the venue has
    finished it. Held by the restated
    `TestSpotMarketBuyQuoteSemantics::test_order_status_report_never_states_the_quote_amount` and
    the gate's mid-match market-buy scenario beside its caught-up control.

  **Withdrawn, and recorded so it is not tried again.** The sixth round's repair for the restart
  path hung the sweep on the execution engine's publication of a mass status it has just
  reconciled, on the grounds that this is the one moment both recovery routes share. The topic is
  shared; the engine's state when it fires is not. A reconnect mass status carries no position
  reports and a startup one does, and the engine reconciles those position reports *before* it
  publishes. So on the startup path the sweep booked the venue's real trade on top of the fill the
  engine had just inferred for the same trade: against a venue holding four lots short the account
  held eight, with a fabricated reconciliation order beside it, and nothing corrects it because the
  periodic position check is off by default. A restart turned a lost execution into a doubled
  position, which is the more damaging half of the property the release gate exists to prove. The
  change is out of the tree; the ordering that makes that hook wrong is recorded in the graph.

  The reason no scenario caught it is recorded too, and is the more useful half of the round. Every
  restart fixture answered the position endpoint with an empty book while the trade listing
  reported a four-lot match — a state no perpetual account can be in, and the one answer under
  which a client that books the trade twice still reads as correct. That fixture now answers with
  the position the recovered trade creates, and fails on both trees.

  **The eighth round: the class closed on one surface, found alive on another.** The seventh
  round died of half-closure — the right tool applied to one field of many, and a rule wrong in
  the direction no fixture measured — so the eighth set out to close both findings across their
  whole surface, as one change each where the refutations showed the halves collide. Its audit
  re-ran the round-seven refuters' own 33-cell matrix (33/33 clean, up from 29/33), ran the
  release gate at the fixed tree (41 scenarios, 194 checks, pass) and against the reverted tree
  (exactly the seven claimed scenarios fail), re-derived the deciding-field census (every row
  strict or excluded with a stated reason), and verified every fixer claim and receipt. What that
  established, per finding:
  - `REC-05` — claimed closed; the audit found it narrowed, not closed. The staleness rule now
    withholds every position answer that does not contain the trades this pass booked and cannot
    be shown, by the venue's own stamp, to postdate them — not only the answer equal to the
    pre-booking book. Believing anything staler than the memory is what erased a pre-existing
    SHORT 6 with a fabricated execution while reconciliation reported success. Withholding
    degrades to a refused node start, never a fabrication. In the same change the arming
    narrowed: only bookings that extended orders the cache held when recovery began arm the
    memory, because the pre-booking book is only refutable knowledge when this node held it —
    arming fresh-cache and adopted-order bookings is what froze the ordinary no-database restart
    of a partial-window round trip against the venue's current flat row (R7C-01), and broadening
    the withhold without narrowing the arming makes that freeze strictly worse. An unreadable row
    timestamp is judged as 0, never promoted to local now (R7C-02). The audit confirmed all of
    that closed — and then drove the arming exception itself through the real engine: a pass
    whose outage trade rode an external or adopted order arms nothing for its instrument, so the
    same stale shapes erase the pre-existing position together with the adopted bookings, in a
    node that starts (two cells, R8-F1 and R8-F2, measured by no prior round or gate scenario).
    That remainder was recorded as `REC-07` in the review matrix and closed by the ninth round
    (the block below). Residuals stated on the method and in the gate receipt: the memory is one
    restart deep, and a same-second compensating trade stays withheld until a distinguishable
    row.
  - `REC-06` — claimed closed; the audit could not refute it. Every deciding field of the fill,
    order and trigger parses — and the status arithmetic shared with the stream — is read
    strictly, with the round-seven pattern widened by a decimal-aware sibling
    (`to_exact_decimal`) and strict side/type/status conversions. Unreadable raises: trade
    listings answer `FillReportsUnavailable` carrying every readable row, order listings answer
    the new `OrderReportsUnavailable`, and startup refuses the mass status on either — the
    platform's own posture for a failed report query, adopted after the refutation showed the
    partial-answer path fabricating commission-less stand-ins for the missing trades. Explicit
    readable zeros stay believed; stringified integers parse exactly; decimal-sized
    (`enable_decimal`) contracts are refused loudly rather than truncated, a documented alpha
    limitation. The still-open spot cash market buy answers the single-order query with the
    venue's own quote-denominated ACCEPTED statement (closing the fabricated inflight rejection
    of R7C-03) and stays silent in listings, which is the REC-04 constraint. The below-bar edges
    the audit found — the order report's average price, the spot fill's fee currency, the spot
    stream's inferred `finished` for a payload stating neither status nor event, and an
    overstating docstring on the open-order check, since corrected — are recorded as residual
    risks in the review matrix.

  Held by the widened `TestStalePositionAnswersAfterRecovery`, the new
  `TestUnreadableContractOrderFields` / `TestUnreadableSpotOrderFields` / `TestUnreadableFillRows`
  / `TestOpenCashMarketBuySingleOrderQuery` / `TestExactDecimal` families, and seven release-gate
  scenarios that port the refuters' cells — the four pre-existing-position matrix cells, the
  fresh-cache round trip, and the confident-zero engine cases — every one of which fails against
  the pre-round-eight tree and ends fail-safe on this one.

  **The ninth round: the two doors closed as one class.** The audit's cells were ported first —
  two new release-gate scenarios (`stale_answer_cannot_erase_through_the_adopted_order_door`,
  `stale_answer_cannot_erase_through_the_zero_net_door`: the auditor's exact shapes on both
  routes, plus a second restart on the venue's caught-up row that must release the refused
  start) and the zero-net and adopted-arming regression tests — and proven to fail against the
  pre-repair tree with the audit's own signatures before anything changed. The repair then
  closed the class rather than the cells: the arming exception is keyed per instrument on prior
  knowledge (a cached order the trade extended, or a pre-existing open position), not per
  order, so every venue trade the pass books over prior knowledge guards the instrument
  whatever order it rode; the snapshots and the recording happen before the pass books
  anything, which keeps the R7C-01 fresh-cache trade (reconstruction over no prior position
  still arms nothing) and also guards a trade the in-call sweep fails to book — the engine
  books it from the returned mass status after any post-sweep arming would have run; and the
  reader no longer pops the memory at delta zero, so a zero-net round trip guards like any
  other booking set and only the two venue proofs (a strictly-later stamp, agreement with the
  post-booking book) clear it. The auditor's matrix extension re-run on the repaired tree shows
  all four `REC-07` cells withheld fail-safe with routes in parity and every control unchanged;
  the round-seven 33-cell matrix stays 33/33. In the same round the audit's two closable
  below-bar edges closed (`avg_px` strict on filled rows, spot `fee_currency` required for a
  nonzero fee) — the review matrix's residual-risks section records what remains. The round's
  own audit could not refute any of it: it drove the invariant as a matrix through the real
  engine — order provenance crossed with net delta, answer shape, pre-existing position, route
  and pass, including seven cells no round had measured (adopted-order arming against agreeing,
  disagreeing, postdating-flat and unreadably-stamped answers; the zero-net agreement
  discriminator; the second restart in both directions; the reconnect-armed memory against its
  per-instrument reader) — and every cell landed honest: venue proof clears and reconciles,
  everything else withholds fail-safe with the book intact, the fresh-cache restart keeps
  starting, and the reverted tree reproduces the erasures exactly where the round claimed. The
  two problems the audit recorded are diagnostics-only — staleness entries armed for spot
  instruments are inert, and a failed pass re-records its trades so the debug delta inflates
  across retries — stated on the arming method and under the review matrix's residual risks.
- ~~Close the two open recovery defects above with a scenario that asserts the damage, and extend
  the restart scenarios to cover the pairings only the reconnect scenarios exercise today.~~ Done:
  the dual-route parity family asserts the damage per route before comparing them, the restart
  fix books the recovered fills inside `generate_mass_status` — before anything can square the
  book against a position report that already contains them — and the engine's partial-window
  fill adjustment is untouched.
- Fix the defects an audit against the platform documentation found, starting with the ten that
  produce silently wrong behaviour rather than an error.
- Replace the facilities reimplemented here that the platform already ships, where the replacement
  is safe to make now.

_Exit: every known correctness defect closed with a regression test that fails against the old
behaviour; the full suite green; the release gate no worse than before._

### Stage 1 — Execution contract

Make the execution client behave as the platform specifies, especially where it currently guesses.

- Ambiguous outcomes. A submit, cancel or amend whose result the venue did not confirm must be
  resolved the way the platform expects, not by assuming failure. An order the venue may have
  accepted must not be reported rejected.
- Event completeness. Every event the platform defines for an execution client is either generated
  where it applies or documented as inapplicable, with a reason.
- Order fidelity. Each order type, time in force and execution flag is either expressed faithfully
  or refused with a stated reason. Nothing is silently converted into something else.

_Exit: every deviation from the execution model either removed or documented as venue-forced;
regression tests for each; the order-type matrix regenerated from behaviour rather than intent._

### Stage 2 — Data contract

- Publish the data types the platform defines for a derivatives venue, including funding rate,
  mark price and index price, using its first-class types rather than approximations.
- Instrument precision and tick schemes correct for every product, including contracts that do not
  tick in powers of ten.
- Order-book flags and sequencing exactly as the platform requires.

_Exit: every data type Gate.io provides and the platform models is published or documented as out
of scope; precision verified against the live instrument definitions._

### Stage 3 — State and reconciliation

- Account state, balances and margin reported as the platform's accounting model expects.
- Position reports that state venue truth and never assert a position the venue does not model.
- Startup, reconnect and restart reconciliation converging with the platform's own view.

_Exit: the reconciliation and restart harnesses pass, including the scenarios that pair an open
order report with a trade, a failing position query, and orders amended before filling — and each
of those pairings is exercised on the restart route as well as the reconnect route. That
qualification is here because the harnesses passed without it while a restart lost a
venue-confirmed execution: a scenario set that covers one route only proves one route._

### Stage 4 — Infrastructure

- Logging through the platform's logger so operator log configuration applies to every component.
- Task lifecycle through the platform's own machinery, so shutdown is bounded and nothing leaks.
- Transport, rate limiting and retries evaluated against the platform's clients.

_Exit: no component logs outside the platform's system; no client holds its own task registry;
shutdown leaves nothing pending._

### Stage 5 — Test specification coverage

NautilusTrader publishes numbered test cases defining what an adapter must be shown to do. Each is
mapped to a test here, or classified as not applicable to Gate.io with a reason.

_Exit: every case has a status; the cases that cannot be settled offline form the live validation
plan._

### Stage 6 — Validation

Done for the alpha, and deliberately not finished. Bounded live validation ran on mainnet at the
smallest size each instrument permits, with account cleanliness checked before, between and after.
It confirmed the market-data paths, the spot execution path end to end, a series of orders on one
USDT perpetual including a position read back from the venue by a node that had not opened it, and
three orders on one option contract — and it left inverse perpetuals, delivery futures, every margin
ledger and the adoption of a resting order into a fresh cache unproven at the venue. Every result,
including the runs that failed and the recorded checks that turned out not to check what they
claimed, is in the [validation status](validation.md).

The runs paid for themselves in defects: `REC-08`, where a node start crashed because the recovery
sweep read the engine's index entry for a filtered external order as a bookable one, and `REC-09`,
where a quote-denominated spot market buy carried an estimated quantity that could leave the order
open for ever or discard a fill. Both are recorded with the recovery findings in the
[review matrix](review-matrix.md#recovery-findings-raised-after-this-review).

_Exit met: results recorded per product and account mode, and every capability's status earned
rather than assumed — which for most products means a status well below confirmed._

### Stage 7 — Release `v0.2.0a1`

Packaging verified from the installed artefact — the wheel and the source distribution both
installed into a clean environment outside the source tree and exercised there — documentation
carrying the real validation results rather than a placeholder, and the tag placed on the exact
commit the validation runs were made against.

_Exit met: released. The release gate passed all ten conditions on the tagged commit — 1833 tests,
the wheel built and installed clean, its documented public imports verified from that install, the
TradingNode smoke, reconnect and restart recovery over 43 scenarios, REST/WebSocket/Nautilus
reconciliation, and a clean account preflight. The default branch carries the same tree as the tag,
which matters because the superseded `v0.1.0` release directs readers to install from it._

### Stage 8 — Toward beta and a stable release

Driven by what real use surfaces. Widening live validation, fixing operational defects, and the
technical decisions deferred out of the alpha.

## How work is done here

**Answers come from the platform, not from invention.** The order is: the installed
`nautilus_trader` source, then its in-tree adapters, then its developer guide, then its concepts and
integration documentation. Only when none of those settles a question is a decision made here — and
then it is written down as a decision, with its reasoning.

**Before implementing a utility, look for it in the platform.** An audit found fifty facilities
reimplemented that NautilusTrader already ships. Keeping a local version requires a venue-specific
requirement the platform's cannot express, stated in the code.

**Evidence closes work, not intent.** A defect is fixed when the behaviour is gone and a test fails
against the old behaviour. A capability is proven at exactly the level it has been exercised.

**The code graph is part of a change.** This repository is indexed into a code graph that records
what calls what, which documentation section describes which symbol, and which test covers which
defect. It is refreshed as part of every change, not afterwards, so that impact analysis and
documentation stay honest. A symbol with no documentation edge is undocumented, which is how several
gaps here were found.

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the development environment, the test suite and how to
report an issue. The current state of the audit that drives Stage 0 is in
[review-matrix.md](review-matrix.md).
