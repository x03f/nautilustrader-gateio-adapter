# Project checkpoint

Written from the repository and from a code-level verification pass, not from conversation memory.
Every status here was re-derived from the current tree; where a tracking file disagreed with the
code, the code won and the disagreement is recorded.

_State as of commit `eae2f50`, branch `v0.2.0-dev`._

## Architecture

Pure-Python NautilusTrader adapter for Gate.io, packaged as its own distribution. Multi-product:
spot, isolated/cross/unified margin, USDT and inverse perpetuals, delivery futures, options.

Decisions that should not be revisited without a reason:

- Venue string is `GATE_IO`, matching how NautilusTrader already names this venue in its Tardis
  integration, so instruments stay interchangeable across tooling.
- Minimum-normalization symbology: only perpetuals carry a suffix (`-PERP`), because 527 of them
  collide with a spot pair of the same name. `raw_symbol` is always the exact venue string.
- Nothing synthetic is published as venue data.
- Nothing is silently converted: where Gate.io cannot express an order as written, the adapter
  denies or rejects it with a stated reason.
- Account-specific safety policy lives outside this package, in the private validation harness.
- Conditional orders keep both venue identities (armed and fired) via `GateioTriggerLink`.

## Position

- Branch `v0.2.0-dev`, HEAD `eae2f50`, working tree clean, CI green on all four jobs.
- Draft PR #3 open against `main`. `main` still holds released v0.1.0 and is untouched.
- 27 package modules, 21 test modules, 1237 tests passing, no network or credentials required.

## Review findings — verified against code

52 findings. The tracking file `review_matrix.json` records 51 FIXED / 1 OPEN. A per-finding
verification against the current source disagrees in both directions:

| Verified status | Count |
|---|---|
| CLOSED | 46 |
| PARTIAL | 5 |
| OPEN | 1 |

Not closed:

| ID | Sev | What actually remains |
|---|---|---|
| `EXEC-1` | **critical** | Only the order-payload path rebases the venue order id. `_handle_fill_payload` does not, so a fill arriving before the order payload for a fired conditional order raises inside `Order.apply` and the framework swallows it — the fill is lost. Gate.io orders `*.orders` and `*.usertrades` independently, so the race is real. |
| `DP-8` | minor | Report paging is capped at 20 pages; beyond that, rows are dropped with a warning. |
| `SEAM-02` / `seam-07` | minor | `timestamp_to_nanos` is still defined twice — `common/parsing.py` (decimal, exact) and `data.py` (binary float). Measured divergence 64 ns on millisecond timestamps, so the data path and the execution path disagree about the same instant. The regression test exercises only the canonical copy. |
| `seam-08` | minor | The HTTP client's `close()` is reachable and reference-counted but no client ever calls it. |
| `DOC-03` | minor | Two prose claims in `config.py` still describe book intervals that the authoritative table contradicts. |

`TEST-01` is closed but without a verified regression test.

`md-08` is recorded OPEN in the tracking file and is in fact closed: `tests/test_books.py` exists
and covers the gap/resync algorithm.

**The tracking file has not been reconciled with these results.** Doing so is a deliberate
follow-up, not an oversight — see next steps.

## Release gate

Executable gate: 4 of 10 conditions pass. Mainnet remains blocked, correctly.

| # | Condition | State |
|---|---|---|
| 1, 2 | critical / serious findings resolved | pass **per the tracking file**, which the verification above contradicts for `EXEC-1` |
| 3 | obsolete v0.1 tests removed | reported fail — **false positive in the harness**: it greps the substring `error` in `--collect-only` output, and 142 test names contain it. Collection exits 0 with no errors. |
| 4 | full suite green | pass |
| 5, 6 | wheel built and installed into a clean environment | reported fail — **environmental**: `/tmp` tmpfs was exhausted. Since freed. CI performs the same check and passes. |
| 7 | end-to-end TradingNode smoke | **genuinely not done**, no receipt |
| 8 | execution reconnect and restart recovery | **genuinely not done**, no receipt |
| 9 | REST / private WS reconciliation, no duplicate fills | **genuinely not done**, no receipt |
| 10 | clean-account preflight | pass — **initial preflight only**, must be re-run before and after any live validation |

The gate has not been re-run since the disk was freed; the recorded state is left as it stands so
that nothing appears to have been relaxed silently.

## Validation

No live-venue validation has been performed. `docs/validation.md` ships an explicit placeholder and
no feature is marked Stable. Unified-account borrowing cannot be exercised at the balances in use:
the venue gates the relevant account modes above thresholds the test balances do not meet. That
path is implemented and covered offline and must be reported as unverified live, not implied
otherwise.

## File ownership

No agent holds any file. The last multi-agent run completed; the working tree is clean and every
change is committed.

## Next steps, in order

1. Fix `EXEC-1`'s remaining path: rebase the venue order id in `_handle_fill_payload` as
   `_handle_order_payload` already does, with a regression test that delivers the fill *before* the
   order payload.
2. Collapse the duplicate `timestamp_to_nanos`; point the regression test at both copies or remove
   the second one.
3. Reconcile the tracking matrix with the verified statuses above, and regenerate the rendered view
   (`REVIEW_MATRIX.md` is a stale render, ~1 h behind the JSON).
4. Repair gate condition 3's substring check, then re-run the gate.
5. Write the missing gate receipts: conditions 7, 8, 9.
6. Only then: live validation within the standing limits, re-running the account preflight before
   and after.

## Authorization still required from the owner

- Live validation on the venue has not been authorized to proceed past the gate.
- PyPI Trusted Publishing is not configured; that needs an action on the owner's PyPI account.
- Any change to account mode or API permissions is out of scope by standing instruction.

## Working practice

The Graphify knowledge graph is kept current for this repository and consulted before edits, to see
the blast radius of a change including which documentation pages reference a symbol. Regulation:
`/opt/shnalytics/docs/GRAPHIFY.md`. A symbol with no documentation edges is undocumented —
`GateioTriggerLink` currently is.
