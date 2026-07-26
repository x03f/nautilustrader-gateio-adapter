# Project checkpoint

## Global objective

Build and maintain a complete, independent Gate.io adapter for NautilusTrader: one that lets an
outside user obtain market data and trade Gate.io's main products safely, through Nautilus' standard
interfaces, without any knowledge of how it was developed.

The measure is not that a stranger can install it. It is that Gate.io is properly integrated into
NautilusTrader's model — data, execution, order and account state, recovery, across products and
account modes. The adapter must behave correctly not only on the happy path but after dropped
connections, restarts, missed events, and disagreement between REST, the private WebSocket and
Nautilus' own state.

Every product and account mode carries an explicit, earned status: implemented, mock-tested,
testnet-validated, or mainnet-confirmed. A capability is never described as more proven than it is.

**Where this release sits.** `v0.2.0a1` is the first publicly verifiable pre-release, not the
destination: close the remaining mechanisms, pass every release gate, run the bounded mainnet
validation, verify the installed wheel and a TradingNode, publish the documentation and the known
limitations.

**After it.** Real use by first users drives the path to beta, then a release candidate, then a
stable community release — widening live validation and fixing operational defects as they surface.
Paths that cannot be exercised within the current accounts, limits and release cycle are not closed
questions; they are scheduled for a cycle that can reach them.

**Rust.** Migrating individual components is a separate decision, taken only on a clear practical
benefit. It is not on this path and is not promised.

The sequence is: a provable Python alpha, then real operation and beta, then a stable community
release, and only then a decision about Rust.

---

Written from the repository and from executable evidence, not from conversation memory. Where a
tracking file and the code disagreed, the code decided and the disagreement is recorded.

_Branch `v0.2.0-dev`, HEAD `bf222f2`, version `0.2.0a1`._

## Architecture

Pure-Python NautilusTrader adapter for Gate.io, packaged as its own distribution. Multi-product:
spot, isolated/cross/unified margin, USDT and inverse perpetuals, delivery futures, options.

Decisions not to revisit without a reason: venue string `GATE_IO`; minimum-normalization symbology
with `-PERP` only on perpetuals (527 collide with a spot pair) and `raw_symbol` always exact;
nothing synthetic published as venue data; nothing silently converted — an order the venue cannot
express faithfully is denied or rejected with a stated reason; account-specific safety policy lives
outside the package; conditional orders keep both venue identities via `GateioTriggerLink`.

The Python-only architecture is a deliberate, stated deviation from the preferred in-tree Rust/PyO3
shape. A Rust migration is a separate future decision and is not being pursued.

## Landed since the verification audit (the implementation is not finished until the gate passes)

- **EXEC-1 fixed** (`c56882d`). The fill path now reconciles the venue order identity before
  emitting, so a fill arriving before the order message is no longer refused by `Order.apply` and
  swallowed by the engine. The rebase is guarded on the cache mapping, not on `order.venue_order_id`,
  because the order object is only updated once the engine applies the `OrderUpdated` and several
  fills can be handled before that. 11 regression tests; 9 fail against the previous implementation.
- **Review matrix reconciled with code truth.** Eight rows changed status.
- **Duplicate `timestamp_to_nanos` collapsed** (`3165197`). A test walks the tree and asserts exactly
  one definition, so a re-introduced copy fails the suite. The measured 64 ns divergence is gone.
- **Shared HTTP transport released on shutdown** (`3165197`). The factory registers one owner per
  client; a cached transport that has been closed is rebuilt rather than handed to the next node.
- **DOC-03 corrected**; **DP-8 reclassified** as a stated bound rather than a defect.
- **Both gate harness defects fixed**: the substring match on `error` in collection output, and the
  clean-environment build landing on an exhausted tmpfs.
- **Version `0.2.0a1`**, CHANGELOG rewritten, `docs/review-matrix.md` published,
  `legacy/v0.1.0` branch created at the released commit with the tag untouched.

## Open — release-blocking

The three gate harnesses found three defects that 1259 tests did not, all at the seam between
recovery and the event stream rather than inside any single path:

| Defect | What happens |
|---|---|
| Reconnect ordering | `_reconcile_after_reconnect` reconciles order reports before fill reports, so a fill from the outage is booked twice, or under a synthetic trade id, after which a later genuine fill is rejected as an overfill and lost |
| Fabricated execution | With an open spot position, startup reconciliation queries positions per instrument; the client answers FLAT for spot, and the engine synthesises an offsetting order and fill that never happened |
| Stuck spot order | A spot BUY whose fee is charged in the base currency ends PARTIALLY_FILLED with zero remaining and never leaves `cache.orders_open()` |

A fix pass with independent adversarial verification is in progress. None may be closed on the
strength of a passing scenario alone — the mechanism has to be explained.

## Release gate

Mainnet remains programmatically blocked. Conditions 7, 8 and 9 now have harnesses; 8 and 9 report
`passed=false` for the defects above, which is the gate working rather than failing.

Condition 10, account cleanliness, remains **PASS — initial preflight only**. It must be re-checked
before mainnet validation, between product scenarios, after any failed or interrupted scenario, and
after final validation.

## Validation

No live-venue validation has been performed. `docs/validation.md` carries an explicit placeholder
and no feature is marked Stable. Unified-account borrowing cannot be exercised at the balances in
use, because the venue gates the relevant account modes above thresholds those balances do not meet;
that path is implemented and covered offline and must be reported as unverified live.

## Graphify

Synchronised after every commit with `/opt/graphify/sync.sh gateio-adapter`; invariants pass on each
run (documentation nodes present, documentation-to-code edges present, graph not shrunk). Latest:
2602 nodes, 6158 edges, 173 documentation-to-code edges.

The graph found the duplicate `timestamp_to_nanos` (two nodes under one name) and showed that
`GateioTriggerLink` had no documentation edges at all — it is now documented in `docs/execution.md`.
No unresolved graph/code discrepancy is outstanding. Regulation: `/opt/shnalytics/docs/GRAPHIFY.md`.

## Raised during the documentation rewrite, not yet acted on

Verifying documentation against the source surfaced five things. None blocks the alpha; all are
recorded so they are not rediscovered later.

1. `config.py` `GateioExecClientConfig` Notes claims the constructor cross-validates the spot
   account mode against the configured products. It does not — only `validate_products` runs. The
   public page describes the code, so the docstring is the outlier and is simply wrong.
2. `AccountFactory.register_cash_borrowing(GATEIO)` is called when the spot mode is a margin mode,
   but those configurations produce a MARGIN account, and `allow_borrowing` is only consulted when
   constructing a `CashAccount`. The call appears to be a no-op and was left out of the docs rather
   than described as having an effect nobody could observe.
3. Two user-visible behaviours have no test: the hedge-mode refusal in
   `_assert_one_way_position_mode`, and the execution client skipping a product whose wallet read
   raises `WalletNotProvisionedError`. Both are cheap to cover with the existing harness.
4. With `spot_account_mode=UNIFIED` against a classic account, the unified read raises inside
   `_collect_spot_balances`, so the whole spot wallet is discarded for that cycle — including the
   plain balances already read. Reporting those and warning only about the unified ledger would
   degrade more gracefully.
5. `sync_time()` is called by nothing, so the `x-gate-exptime` submission deadline is never attached
   in the default configuration. Documented as such; if the deadline was meant to be active,
   `_connect` should call it.

Separately, and introduced by the seam-08 fix in this session: both clients close the shared
transport as the FIRST action of `_disconnect` and cancel their tasks afterwards. A request already
past the closed-check and awaiting the rate limiter then surfaces a raw httpx `RuntimeError` instead
of the adapter's own `GateioError(CLIENT_CLOSED)`. Cancelling tasks before releasing the transport
closes that window. Found by the TradingNode smoke harness.

## Plan from here to a stable release

Blocks, in dependency order. A block does not start until the one before it has evidence, not
intent.

### A — Correctness debt (blocks the alpha)

| | Work | State |
|---|---|---|
| A1 | Adversarially verify the round-two fixes. They pass their harnesses; round one did too and was refuted on all three | not run — session limit |
| A2 | Map the 90 numbered upstream test cases (`spec_exec_testing.md`, `spec_data_testing.md`) onto this suite. Nothing here references them, so coverage is unknown | not run — session limit |
| A3 | Replace the duplicated platform facilities. `secs_to_nanos`, `millis_to_nanos`, `nanos_to_secs` all exist in `nautilus_trader.core.datetime`; `data.py` already imports from that module, so these were written beside the originals | open |
| A4 | Audit the whole package for the same class of duplication, against `docs/concepts/` (30 docs), `docs/integrations/` (20 adapter pages) and the in-tree adapter source — none of which has been read | open |
| A5 | Run the TradingNode smoke from the INSTALLED wheel. It currently runs from the source tree, which is the arrangement that once hid a wheel built without its sub-packages | open |

### B — Gate

All ten conditions, with the extended harness scenarios. No condition is weakened to obtain a pass.

### C — Bounded live validation

Preflight, then the smallest orders that prove submission, acknowledgement, fill handling,
cancellation, the trigger transition, balance and position convergence, and restart recovery.
Preflight again between products and after any interrupted scenario. What the account and limits
cannot reach is classified, not simulated by proving an adjacent endpoint.

### D — Release v0.2.0a1

Packaging verified from the installed artefact, secret scans, documentation carrying the real
validation results, tag on the exact validated commit, report.

### E — Alpha to beta

Driven by what first users hit. Also the deferred technical decisions:

- Evaluate moving to `nautilus_pyo3.HttpClient` / `WebSocketClient` / `Quota` and
  `live.retry.RetryManagerPool`. The in-tree reference adapter uses all four; this package
  reimplemented each on `httpx` and `websockets`. Those platform clients are Rust behind PyO3, so a
  large part of what a "Rust migration" would buy is available without writing any Rust. Changing
  transport mid-release-cycle is a risk out of proportion to the benefit right now, which is why it
  is here and not in block A — deferred deliberately, not overlooked.
- Evaluate `test_kit.stubs` and `test_kit.mocks` in place of hand-built fixtures.

### F — Stable

Sustained live operation, the upstream case coverage closed, operational defects fixed. Only then
is a Rust decision worth taking, on demonstrated need.


## Next steps, in order

1. Land and independently verify the three blocker fixes.
2. Re-run both harnesses to passing receipts; run the full gate.
3. Controlled mainnet validation within the standing limits, with the account preflight re-run
   before and after.
4. Record results in `docs/validation.md`, finish the remaining documentation pages.
5. Clean-wheel and TradingNode verification from the installed artefact, secret scans.
6. Tag and release `v0.2.0a1`.

## Authorisation still required

Live validation has not been authorised to proceed past the gate. PyPI Trusted Publishing is not
configured and needs an action on the owner's account. Account mode and API permission changes are
out of scope by standing instruction.
