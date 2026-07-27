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

- Independently verify the recovery and reconciliation fixes. Four rounds have now passed their
  harnesses and none has survived an attempt to refute it; passing is not evidence. What the
  fourth leaves open is stated in [execution.md](execution.md): reconnect recovery of an order
  that missed more than one fill, and a position query the venue refused being read as flat.
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
order report with a trade, a failing position query, and orders amended before filling._

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
