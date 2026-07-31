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

This adapter is pure Python, which is a deliberate, stated deviation from the shape the platform
prefers for the adapters it ships itself. The argument, and what the decision does not excuse, is in
[architecture.md](architecture.md#the-deliberate-python-only-architecture).

One consequence belongs on a roadmap: adopting `nautilus_trader.core.nautilus_pyo3` is not a
migration away from Python. Its `HttpClient`, `WebSocketClient` and `Quota` are ordinary Python APIs
implemented in Rust underneath, so taking them is taking the platform. Stage 4 evaluates this
adapter's own transport against them.

## Stages

Each stage has exit criteria that are checkable by someone who did not do the work. A stage is not
finished because its changes are written; it is finished when its criteria hold.

### Stage 0 — Foundation

Close what is already known to be wrong before adding anything.

- **Recovery and reconciliation.** Reworked until the startup and reconnect routes provably agree.
  What that took: the unapplied-fill sweep now runs inside `generate_mass_status`, before the
  engine reconciles anything; every field of a venue report that decides money is read strictly and
  raises rather than defaulting; a position answer that cannot be shown to contain or postdate the
  trades just booked is withheld rather than believed. Two residuals stand by design — the
  staleness memory is one restart deep, and a compensating trade stamped in the same second as the
  answer is withheld until the venue produces a distinguishable row. Per-defect detail, and what
  each round's audit refuted, is in [review-matrix.md](review-matrix.md).
- Fix the defects an audit against the platform documentation found, starting with the ten that
  produce silently wrong behavior rather than an error.
- Replace the facilities reimplemented here that the platform already ships, where the replacement
  is safe to make now.

_Exit: every known correctness defect closed with a regression test that fails against the old
behavior; the full suite green; the release gate no worse than before._

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
regression tests for each; the order-type matrix regenerated from behavior rather than intent._

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
open forever or discard a fill. Both are recorded with the recovery findings in the
[review matrix](review-matrix.md#recovery-findings-raised-after-this-review).

_Exit met: results recorded per product and account mode, and every capability's status earned
rather than assumed — which for most products means a status well below confirmed._

### Stage 7 — Release `v0.2.0a1`

Packaging verified from the installed artifact — the wheel and the source distribution both
installed into a clean environment outside the source tree and exercised there — documentation
carrying the real validation results rather than a placeholder, and the tag placed on the exact
commit the validation runs were made against.

_Exit met: released. The release gate passed all ten conditions on the tagged commit — 1833 tests,
the wheel built and installed clean, its documented public imports verified from that install, the
TradingNode smoke, reconnect and restart recovery over 43 scenarios, REST/WebSocket/Nautilus
reconciliation, and a clean account preflight. At the release the default branch carried the same
tree as the tag, which mattered because the superseded `v0.1.0` release directs readers to install
from it._

### Stage 8 — Toward beta and a stable release

Driven by what real use surfaces. Widening live validation, fixing operational defects, and the
technical decisions deferred out of the alpha.

This is where the default branch is: it reports `0.2.0a2.dev0` — past `0.2.0a1`, before the next
alpha.

## How work is done here

**Answers come from the platform, not from invention.** The order is: the installed
`nautilus_trader` source, then its in-tree adapters, then its developer guide, then its concepts and
integration documentation. Only when none of those settles a question is a decision made here — and
then it is written down as a decision, with its reasoning.

**Before implementing a utility, look for it in the platform.** Keeping a local version requires a
venue-specific requirement the platform's cannot express, stated in the code.

**Evidence closes work, not intent.** A defect is fixed when the behavior is gone and a test fails
against the old behavior. A capability is proven at exactly the level it has been exercised.

**Every public symbol is described somewhere under `docs/`.** A symbol with no documentation is
treated as a gap, and closing it is part of the change that introduced it.

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the development environment, the test suite and how to
report an issue. The current state of the audit that drives Stage 0 is in
[review-matrix.md](review-matrix.md).
