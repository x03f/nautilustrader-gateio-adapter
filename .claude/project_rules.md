# Project rules

How this adapter is developed. Read before changing anything.

Two commitments govern every decision here, and they pull in the same direction:

**This is a product that must be compatible with NautilusTrader and conform to its standards.** Not
"works with" — conforms to. The platform defines how an adapter must behave toward it: event
ordering, order and account state, report contracts, order-book flags, reconciliation, testing. Those
are not suggestions to be reinterpreted because a venue is unusual. Where Gate.io genuinely forces a
deviation, the deviation is stated and justified in writing; where it does not, we follow the
platform.

**This is a public package with documentation people will trust with money.** Every claim in it must
be true and checkable. A capability is described exactly as proven and no further.

---

## 1. Where answers come from

Never answer a question about NautilusTrader from memory, and never infer platform behaviour from how
this package happens to work. Consult sources in this order, and stop at the first that settles it:

| Order | Source | Why it ranks here |
|---|---|---|
| 1 | The installed platform source, `/opt/octobot/nautilus-venv/lib/python3.13/site-packages/nautilus_trader/` | This is the exact version we build against. Published docs describe `latest`, which can differ. |
| 2 | The in-tree adapter source, `.../nautilus_trader/adapters/` — especially `binance/` | The reference Python implementation. What it does is the convention, whatever any document says. |
| 3 | The pinned developer guide, `scratchpad/audit/guide/` (15 documents, byte-verified against commit `c3eed62afb477c3efdb078f6a934f7c5f8f7db61`) | Indexed in `.claude/nautilus_developer_guide_digest.md`; section 5 routes a task to the right document. |
| 4 | The upstream repository: `docs/concepts/`, `docs/integrations/`, `docs/api_reference/` | Concepts explain the model; integrations show how sibling crypto venues solved the same problems. |
| 5 | `https://nautilustrader.io/docs/latest/` | Convenient, but it tracks `latest`. Prefer 1–4 when the version matters. |
| 6 | Gate.io API documentation, and the collected specs in `scratchpad/spec/` | Authoritative for the venue, not for the platform. |
| 7 | General web search | Last. Never a basis for a behavioural claim about either the platform or the venue. |

The digest is an **index into** the pinned guide, not a replacement for it. When an exact contract
matters, open the original and cite its filename and section.

## 2. Check the platform before writing a utility

Before implementing anything that is not specific to Gate.io, look for it in the platform first.

This rule exists because it was broken. Five facilities were reimplemented that NautilusTrader ships
and its reference adapter uses: the nanosecond time converters in `core.datetime`, the Rust-backed
`HttpClient` and `WebSocketClient` in `core.nautilus_pyo3`, its `Quota` rate limiter, and
`live.retry.RetryManagerPool`. One of those duplications sat next to an import from the very module
that already contained the original.

```bash
grep -rn "<name>" /opt/octobot/nautilus-venv/lib/python3.13/site-packages/nautilus_trader/ --include=*.py --include=*.pyx | head
grep -rn "<name>" /opt/octobot/nautilus-venv/lib/python3.13/site-packages/nautilus_trader/adapters/ | head
```

"The platform has it" is a reason to look closer. "The reference adapter uses it" is a reason to use
it. Keeping our own version requires a **venue-specific requirement the platform facility cannot
express**, written down. A preference is not a reason.

## 3. Evidence, not intent

- A finding is closed when the defective behaviour is gone **and** a regression test fails against
  the old behaviour. Not when a scenario passes.
- A capability is proven at exactly one level: implemented, mock-tested, testnet-validated, or
  mainnet-confirmed. It is never described above the level it has earned.
- A harness that cannot fail proves nothing. Assert the damage — a lost execution, a doubled
  quantity, an order left open, an invented fill — not the shape of a particular fix.
- Two rounds of fixes on this project passed their harnesses and were then refuted. Passing is not
  evidence; surviving an attempt to refute it is.

## 4. Graphify is part of the change, not a chore afterwards

The code graph is how a later reader — including a later session with none of this context —
navigates the package, sees what a change touches, and finds which documentation page describes a
symbol. It is only worth that if it is current.

**After every coherent change, before the change is considered done:**

```bash
sudo -u graphify /opt/graphify/sync.sh gateio-adapter        # rebuild + invariants
sudo -u graphify /opt/graphify/semantic_links.py gateio-adapter   # author the edges a parser cannot infer
```

`sync.sh` rebuilds the graph and fails loudly if documentation nodes vanish, if the
documentation-to-code edges are missing, or if the graph shrinks unexpectedly. All three have
happened.

**Mechanical edges are not enough.** The AST gives calls and imports; a backtick match gives
documentation-to-code. Neither can express *why* two things are related — that this test proves that
finding is closed, that this harness proves that release-gate condition, that this module implements
that upstream contract. Those edges have to be written by someone who understands the change, which
means they are authored during the change and not reconstructed later.

Record them in `.claude/semantic_edges.json` as you go. What earns an edge:

- finding → the code that fixed it → the test that keeps it fixed → its validation command
- release-gate condition → the harness that proves it → what it does and does not prove
- module → the upstream contract it implements, with the document and section
- a deliberate deviation → the platform convention it departs from, and why
- a capability → the evidence for its current status

**Boundaries.** The graph is navigation, never authority. Nothing is marked fixed, passed or
supported because the graph says so — source, tests and observed behaviour decide, in that order.
Heuristic (`INFERRED`) edges are confirmed against the source before they influence anything. When
the graph and the code disagree, the code is right, the graph is stale, and fixing it is part of the
task. Queries are written in English; the code is English and a Russian query silently returns
nothing.

Full regulation: `/opt/shnalytics/docs/GRAPHIFY.md`.

## 5. Documentation

Every public claim is verified against the current code or tests before it is written. Documentation
that describes intent rather than behaviour is worse than none, because someone will trust it with
money.

The graph helps here in a way nothing else does: `affected "<Symbol>" --depth 1` lists the
documentation sections that reference a symbol, so a change and the pages it invalidates are updated
in the same commit. A symbol with no documentation edge is undocumented — that is how it was
discovered that the conditional-order identity transition, the subtlest behaviour in the package,
was described nowhere.

Never generate documentation from graph relationships alone. The graph says two things are related;
only the source says how.

## 6. What never goes in

No credentials, account identifiers, balances, real orders, fills or positions, authenticated
response bodies, server addresses, private paths, or anything about how the project is developed.
This applies to the package, the documentation, the graph, and commit messages alike. Fixtures are
sanitised and structural.

No AI, model or vendor is ever presented as an author, contributor, maintainer or copyright holder.
