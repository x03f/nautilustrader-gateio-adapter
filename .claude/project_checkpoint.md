# Project checkpoint

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

## Completed since the verification audit

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
