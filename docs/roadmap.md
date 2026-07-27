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

- **Recovery and reconciliation are not closed.** Five rounds have now passed their harnesses and
  none has survived an attempt to refute it; passing is not evidence. The fifth round was put in
  front of three independent verifiers — one on position authority, one on restatement
  completeness, one asked to make the client fabricate an execution — and all three refuted it.

  What the round did close, each proven by reverting the change and watching the damage return:
  an order that missed more than one fill now recovers every trade under the venue's own statement
  of that order, instead of one trade beside a cumulative quantity that made the engine mint a fill
  of its own; an order the venue reports under a second id is rebased before its fills are applied;
  and the restatement guard now admits exactly the reports that cannot produce an inferred fill,
  read off the installed engine rather than assumed. The fabrication angle could not be refuted:
  across 338 randomised reconnect cases run against this round and against its parent with the same
  seeds, the parent replaced a real venue trade with a synthetic one ten times and this round never
  did.

  What is open, each demonstrated end to end against a real `LiveExecutionEngine` and stated in
  [execution.md](execution.md):
  - The unapplied-fill sweep runs only after a WebSocket reconnect. On the startup path the
    engine's deduplication drops a venue-confirmed trade and nothing re-offers it, so a restart
    that coincides with a fill understates the position and leaves an order working a quantity the
    venue has already matched. Two verifiers reached this independently, from different angles.
  - A position row the client cannot parse — no symbol field, a non-object row, an empty `200`
    body — becomes an explicit flat report, which the engine squares the live book against. The
    refusal-versus-absence typing this round added is correct and does not cover this route.
  - The fill-report query never raises, so the engine's own brake against squaring a book on a
    failed query never engages; a 5xx on the trade listing closes the position with a synthetic
    trade id and no commission.

  The bookkeeping lesson is recorded with them: the round's commit message credited itself with a
  fix its parent had already made and with a finding-matrix update its diff did not contain. Claims
  about a change are now checked against the tree that carries it, not against the message.
- Close the three open recovery defects above with a scenario that asserts the damage, and extend
  the restart scenarios to cover the pairings only the reconnect scenarios exercise today.
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

Bounded live validation on the venue: the smallest orders that prove submission, acknowledgement,
fill handling, cancellation, the conditional-order transition, balance and position convergence, and
restart recovery. Account cleanliness checked before, between and after.

_Exit: results recorded per product and account mode; every capability's status earned rather than
assumed._

### Stage 7 — Release `v0.2.0a1`

Packaging verified from the installed artefact, documentation carrying the real validation results,
tag on the exact validated commit.

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
