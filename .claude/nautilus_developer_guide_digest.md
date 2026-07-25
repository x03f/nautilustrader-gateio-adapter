# NautilusTrader developer-guide digest (pinned reference)

Development metadata for this repository. Not public adapter documentation, and not a
replacement for the upstream source: this is an **index** that tells a future session which
upstream document to open, and what that document actually requires.

**Pinned commit:** `c3eed62afb477c3efdb078f6a934f7c5f8f7db61`
**Pinned tree:** <https://github.com/nautechsystems/nautilus_trader/tree/c3eed62afb477c3efdb078f6a934f7c5f8f7db61/docs/developer_guide>

Every requirement below cites its source file and section so the exact wording can be reopened.
When a requirement matters to a decision, open the upstream file — do not rely on this summary,
and do not rely on remembered NautilusTrader behaviour.

---

## 1. Source manifest

Base URL for every row (append the filename):
`https://raw.githubusercontent.com/nautechsystems/nautilus_trader/c3eed62afb477c3efdb078f6a934f7c5f8f7db61/docs/developer_guide/`

| File | Size | Purpose | Major topics | Read in full |
|---|---|---|---|---|
| `index.md` | 1 KB | Entry point | States the platform architecture: Rust core, Python bindings, PyO3 bridge | yes |
| `design_principles.md` | 1.6 KB | Core invariant | Message immutability and the properties it protects (determinism, temporal integrity, replay, auditability) | yes |
| `coding_standards.md` | 6.9 KB | Style across all languages | Spaces not tabs, ~100-col lines, American spelling, comment conventions, error-message phrasing, naming, formatting, commit messages, gitlint | yes |
| `python.md` | 4.9 KB | Python conventions | PEP-8 with explicit `is None`, mandatory type hints, PEP 604 unions, NumPy docstrings in imperative mood, no docstrings on private methods, PyO3 property-vs-method rule, test naming, ruff | yes |
| `rust.md` | 61 KB | Rust conventions | Cargo/feature conventions, module organisation, error handling, async and adapter runtime patterns, PyO3 bindings and stubs, hash-collection determinism, shared mutability, design by contract, unsafe policy, Cap'n Proto | yes |
| `ffi.md` | 6.3 KB | FFI memory contract | `abort_on_panic` at every `extern "C"`, `CVec` ownership lifecycle, type-specific drop helpers, `PyCapsule::new_with_destructor`, `*_new`/`*_drop` pairing | yes |
| `testing.md` | 22 KB | Test policy | Mechanism ladder (unit → parametrized → property → integration → fuzz → spec acceptance → DST → formal), projection rule by module shape, when NOT to add coverage, DST readiness, mocks policy, coverage policy, data-type test layer matrix | yes |
| `test_datasets.md` | 15.5 KB | Fixture provenance | Small/large/user-fetched categories, required `metadata.json` fields, Nautilus Parquet storage standard, naming convention, curation workflow, redistribution limits | yes |
| `spec_data_testing.md` | 39 KB | Data acceptance spec | `DataTester` matrix TC-D01…TC-D72 in 9 groups, pass criteria per case, full `DataTesterConfig` reference | yes |
| `spec_exec_testing.md` | 92 KB | Execution acceptance spec | `ExecTester` matrix TC-E01…TC-E101 in 10 groups, event sequences, reconciliation cases, full `ExecTesterConfig` reference | yes |
| `adapters.md` | 119 KB | Adapter specification | Layered structure, 7-phase build sequence, Rust patterns, HTTP/WebSocket patterns, connection lifecycle, message routing, dedup, task management, testing layout, Python adapter layer, order-book flags | yes |
| `benchmarking.md` | 7.2 KB | Performance tooling | Criterion vs iai, `benches/` layout, writing rules, flamegraphs | yes |
| `docs.md` | 8.8 KB | Documentation style | Divio doc types, language and tone, table formatting, `✓`/`-` support indicators, headings case, admonitions, MDX components | yes |
| `environment_setup.md` | 18.4 KB | Upstream dev environment | uv/prek/Rust/Cap'n Proto setup, PyO3 env vars, dependency supply-chain settings, Nautilus CLI, rust-analyzer config | yes |
| `releases.md` | 12.8 KB | Release process | Three-branch model, stable release ordering rules, dual versioning, crates.io Trusted Publishing, release checklist, release-note sections and wording | yes |

---

## 2. Normative requirement register

Classification: **MUST** (explicit correctness/contract), **SHOULD** (strong upstream
recommendation), **CONVENTION** (upstream repository or in-tree adapter convention),
**OPTIONAL**, **CONTEXT**. Wording is not strengthened beyond the source.

### Order books and data

| # | Source | Section | Requirement | Class | Area | Consequence if violated |
|---|---|---|---|---|---|---|
| R1 | adapters.md | Order book delta flag requirements | Adapters **must** set `RecordFlag` correctly on each `OrderBookDelta` | MUST | order books | Guide calls a missing `F_LAST` "a silent bug": with buffering enabled subscribers never receive data |
| R2 | adapters.md | same | `F_LAST` on the last delta of every logical event group; `DataEngine` uses it as the flush signal | MUST | order books | Deltas accumulate indefinitely, never published |
| R3 | adapters.md | same | `F_SNAPSHOT` on all deltas of a snapshot sequence (CLEAR followed by ADDs) | MUST | order books | Consumers cannot distinguish snapshot from increments |
| R4 | adapters.md | same | Empty-book snapshot: the CLEAR delta must carry `F_SNAPSHOT \| F_LAST` | MUST | order books | Buffered consumers never receive the empty book |
| R5 | adapters.md | same | Each venue update message ends with a delta carrying `F_LAST`; batched venue messages terminate each logical group | MUST | order books | Same flush failure as R2 |
| R6 | adapters.md | Timestamp conventions | `ts_event` is the converted venue timestamp; `ts_init` is the clock read; convert ms→ns at the parser boundary; when the venue gives no timestamp use the clock for both | CONVENTION | data, execution | Inconsistent event ordering and replay |
| R7 | design_principles.md | Message immutability | Once created, a message's fields must not be mutated | MUST | whole adapter | Loses determinism, temporal integrity, replay fidelity, auditability |

### Execution and reconciliation

| # | Source | Section | Requirement | Class | Area | Consequence |
|---|---|---|---|---|---|---|
| R10 | adapters.md | ExecutionClient (Python adapter layer) | Implement `generate_order_status_report`, `generate_order_status_reports`, `generate_fill_reports`, `generate_position_status_reports`, `generate_mass_status` | MUST | reconciliation | Startup reconciliation cannot run |
| R11 | adapters.md | Phase 4.5 | Reconciliation reports exist so state is recovered on connect | MUST | reconciliation | Restart loses venue state |
| R12 | adapters.md | Connection lifecycle | Execution `connect()` order: instruments → WS connect → subscribe → stream handler → fetch account state → **await account registered** → signal connected. All initialisation completes inside `connect()` | MUST | execution lifecycle | Race with reconciliation and strategy startup |
| R13 | adapters.md | Connection lifecycle (data) | Data `connect()` order: fetch instruments → cache locally → emit to data engine → cache to WebSocket → connect WebSocket | MUST | data lifecycle | Messages arrive before instruments are known |
| R14 | adapters.md | `WsDispatchState` | Track emitted lifecycle events (`emitted_accepted`, `triggered_orders`, `filled_orders`) to prevent duplicates across reconnects and fast-fill races | SHOULD | execution | Duplicate `OrderAccepted`/`OrderFilled` on replay |
| R15 | adapters.md | Cross-source fill deduplication | When fills arrive from both WebSocket and HTTP reconciliation, dedup at trade-ID level (bounded FIFO set) | SHOULD | execution | The same fill emitted twice |
| R16 | adapters.md | Message routing → two-tier routing contract | Tracked order → order events (synthesising missing lifecycle events); external/unknown order → reports for reconciliation | MUST | execution | Unknown orders corrupt or bypass reconciliation |
| R17 | spec_exec_testing.md | TC-E72 / TC-E73 | Unsupported order type or TIF must produce `OrderDenied` **before** reaching the venue (distinct from venue `OrderRejected`) | MUST | execution | Silent conversion changes order semantics |
| R18 | spec_exec_testing.md | TC-E84…TC-E87, TC-E101 | Reconcile open orders, historical fills, open long/short positions and option positions from a prior session | MUST | reconciliation | Restart recovery unproven |
| R19 | adapters.md | Common test scenarios → State management | Start sessions with existing open orders and preloaded positions; reconcile before issuing new commands | MUST | reconciliation | Duplicate or conflicting orders after restart |

### HTTP, WebSocket, credentials

| # | Source | Section | Requirement | Class | Area | Consequence |
|---|---|---|---|---|---|---|
| R20 | adapters.md | Environment variable conventions | `{VENUE}_API_KEY` / `{VENUE}_API_SECRET`, testnet variant `{VENUE}_TESTNET_API_KEY` / `_SECRET`; names centralised, never duplicated as literals | CONVENTION | configuration | Users cannot predict variable names |
| R21 | adapters.md | same | Invalid credentials **must** fail fast, never silently degrade to unauthenticated mode | MUST | security | Silent loss of private functionality |
| R22 | adapters.md | Credential module structure | Config structs are DTOs; credential resolution lives in the credential module | CONVENTION | configuration | Resolution logic scattered |
| R23 | adapters.md | Error handling / Retry classification | Distinguish retryable, non-retryable and fatal errors; classify from HTTP status and rate-limit headers | SHOULD | HTTP | Unsafe retries or lost recoverable errors |
| R24 | adapters.md | Rate limiting | Per-operation quotas and rate-limit keys (subscription / order / cancel / amend) | CONVENTION | HTTP, WebSocket | Order traffic throttled by subscription traffic |
| R25 | adapters.md | Reconnection logic | On reconnect: restore authentication, then replay tracked subscriptions from stored original arguments | MUST | WebSocket | Silent data loss after reconnect |
| R26 | adapters.md | Subscription lifecycle | Failed subscriptions stay pending and are retried on reconnect; `subscription_count()` reports confirmed only | CONVENTION | WebSocket | Phantom subscriptions |
| R27 | adapters.md | Disconnection lifecycle (`close`) | Shutdown order: send disconnect command → set stop signal → await task with timeout, abort if stuck | SHOULD | WebSocket | Hung shutdown, leaked tasks |
| R28 | adapters.md | Ping/Pong handling | Support both control-frame pings and application-level text pings; respond early in the loop | SHOULD | WebSocket | Venue drops the connection |
| R29 | adapters.md | Backpressure strategy | Latency-sensitive WebSocket channels are intentionally unbounded; do not add bounded channels or backpressure unless the latency requirement changes | CONVENTION | WebSocket | Added latency contrary to platform intent |
| R30 | adapters.md | Never use `block_on` in trait methods | Sync client trait methods run inside a tokio runtime; spawn instead of blocking | MUST (Rust) | Rust core | Panic: runtime within runtime |
| R31 | adapters.md | Graceful shutdown with `CancellationToken` | Coordinate task shutdown with a cancellation token; reset it on reconnect | SHOULD (Rust) | Rust core | Tasks survive disconnect |

### Structure, symbology, instruments

| # | Source | Section | Requirement | Class | Area | Consequence |
|---|---|---|---|---|---|---|
| R40 | adapters.md | Structure of an adapter; Adapter implementation sequence | Adapters follow a layered architecture: **Rust core** for networking/parsing plus a Python layer; "Implement the Rust core before any Python layer" | CONVENTION (in-tree) | architecture | See §6 — this is the in-tree structural convention, not a functional-correctness rule |
| R41 | adapters.md | Python layer | Python layer provides InstrumentProvider, Data Client, Execution Client, Factories, Configuration | CONVENTION | architecture | Unfamiliar layout for NT users |
| R42 | adapters.md | Symbol normalization | Provide bidirectional conversion: venue symbol → `InstrumentId` and back; suffix-based product types are the common pattern (Bybit `-SPOT`/`-LINEAR`/`-INVERSE`/`-OPTION`; Binance USD-M appends `-PERP`). Where the raw symbol maps 1:1, no dedicated module is needed | CONVENTION | symbology | Ambiguous instrument identity |
| R43 | adapters.md | Instrument cache standardization | Clients that cache instruments implement `cache_instruments()`, `cache_instrument()`, `get_instrument()` | CONVENTION | instruments | Non-standard cache surface |
| R44 | adapters.md | Method ordering convention | Group adapter methods: connection handlers, subscribe, unsubscribe, request | CONVENTION | Python layer | Harder review |
| R45 | adapters.md | InstrumentProvider | Implement `load_all_async`, `load_ids_async`, `load_async` | MUST | instruments | Provider contract unmet |
| R46 | adapters.md | Parser functions | Map venue enums explicitly with match statements rather than automatic conversions that hide mapping errors; check empty strings before parsing | SHOULD | parsing | Silent mis-mapping |

### Rust core and PyO3 (dormant until a Rust core exists)

| # | Source | Section | Requirement | Class | Area | Consequence |
|---|---|---|---|---|---|---|
| R70 | rust.md | Design by contract | Prefer the type system first; then `check_*` from `nautilus_core::correctness` at API boundaries; `debug_assert!` for internal invariants; always-on `assert!` for soundness-critical checks | SHOULD (Rust) | Rust core | Contracts unenforced or enforced in the wrong layer |
| R71 | rust.md | Design by contract | Never use `debug_assert!` for public API input — release builds strip it | MUST (Rust) | Rust core | Unvalidated input in release builds |
| R72 | rust.md | Adapter runtime patterns | In adapter crates use `get_runtime().spawn()`, not `tokio::spawn()`: called from Python threads the latter panics (no thread-local runtime) | MUST (Rust adapters) | Rust core | Panic when invoked from Python |
| R73 | rust.md | Constructor patterns | Pair `new_checked()` (fallible) with `new()` (panics via `expect_display(FAILED)`) | CONVENTION | Rust core | Inconsistent construction contract |
| R74 | rust.md | Hash collections | Where iteration order feeds observable state on the DST path, use `IndexMap`/`IndexSet`, not `AHash*` (AHash randomises per process) | MUST (Rust, DST path) | Rust core | Non-deterministic replay |
| R75 | rust.md | Safety policy | Every `unsafe fn` documents a `Safety` section; every unsafe block carries a `SAFETY:` comment; FFI-exposing crates enable `#![deny(unsafe_op_in_unsafe_fn)]`; unsafe blocks covered by unit tests | MUST (Rust) | Rust core, FFI | Undefined behaviour |
| R76 | rust.md | Rust-Python memory management | Do not wrap `PyObject` in `Arc` in callback-holding structs; clone through `clone_py_object()` under the GIL | MUST (PyO3) | FFI/PyO3 | Reference cycles neither GC can collect |
| R77 | rust.md | PyO3 naming conventions | Rust symbol prefixed `py_*`, Python name published via `#[pyo3(name = "...")]` | CONVENTION | PyO3 | Inconsistent binding surface |
| R78 | rust.md | PyO3 enum conventions | Do not combine the `hash` pyclass attribute with `eq_int`; provide a manual `__hash__` returning the discriminant | MUST (PyO3) | PyO3 | Hash contract violated (`a == b` without `hash(a) == hash(b)`) |
| R79 | rust.md | Testing conventions | Use `#[rstest]`; keep `proptest` cases in a separate `property_tests` module; no Arrange/Act/Assert separator comments | CONVENTION | Rust testing | Inconsistent with upstream tooling |
| R80 | rust.md | Common anti-patterns | Avoid `.clone()` on hot paths, `.unwrap()` in production, `String` where `&str` suffices, exposed interior mutability, large payloads in `Result` | SHOULD (Rust) | Rust core | Performance and robustness regressions |

### Testing, data, docs, release

| # | Source | Section | Requirement | Class | Area | Consequence |
|---|---|---|---|---|---|---|
| R50 | adapters.md | Testing → Test data sourcing | Test data must come from official API documentation examples or the live API; never fabricate it | MUST | testing | Missing real edge cases (negative precision, scientific notation, unexpected types) |
| R51 | adapters.md | Testing | Unit tests live beside the code; the `tests/` directory is for integration tests needing external infrastructure | CONVENTION | testing | Misplaced tests |
| R52 | adapters.md | CI robustness | Never use bare sleeps with arbitrary durations; poll for conditions with a timeout helper | SHOULD | testing | Flaky CI |
| R53 | testing.md | Mechanism ladder / Projection rule | Choose test layers by module shape; adapter parsers → unit + parametrized + property + fuzz; client loops → integration + spec acceptance + boundary fuzz | SHOULD | testing | Wrong coverage for the risk |
| R54 | testing.md | Test style | Do not capture log output to assert on log messages; assert observable behaviour | SHOULD | testing | Fragile tests |
| R55 | testing.md | Mocks | Prefer hand-written stubs over mocking frameworks; do not mock what you are testing | SHOULD | testing | Test-induced damage |
| R56 | spec_data_testing.md | whole document | Each adapter must pass the subset of `DataTester` cases matching its supported data types; groups 1–4 = baseline data compliance | MUST (for compliance claims) | testing | Cannot claim data compliance |
| R57 | spec_exec_testing.md | whole document | Each adapter must pass the subset of `ExecTester` cases matching its capabilities; groups 1–5 = baseline compliance; verify data connectivity first | MUST (for compliance claims) | testing | Cannot claim execution compliance |
| R58 | test_datasets.md | Required metadata / Storage format | Curated datasets carry `metadata.json` (file, sha256, size, original_url, licence, added_at); new datasets stored as Nautilus Parquet | CONVENTION | fixtures | Unclear provenance and licensing |
| R59 | test_datasets.md | User-fetched pipelines | Do not commit vendor-derived data with unclear redistribution rights; default CI must not depend on vendor credentials | MUST | fixtures | Licensing exposure |
| R60 | docs.md | Support indicators / tables | Use `✓` supported, `-` unsupported; capability matrices based on the Nautilus domain model, not venue terminology | CONVENTION | documentation | Inconsistent docs |
| R61 | docs.md | Code references | Reference code as `file_path::function_name`, not line numbers | CONVENTION | documentation | Stale references |
| R62 | releases.md | Release notes | Fixed section order (Enhancements, Breaking Changes, Security, Fixes, Internal Improvements, Documentation Updates, Deprecations); sentence case, no trailing periods, backticks for code | CONVENTION (upstream repo) | releases | N/A for an external repo except as a style model |
| R63 | releases.md | Security classification | Memory safety, UB, data integrity, input validation and build hardening go under Security; plain logic panics under Fixes | CONVENTION | releases | Mis-categorised release notes |
| R64 | python.md | Type hints / docstrings | All function and method signatures must include type annotations; NumPy docstrings; no docstrings on private methods | CONVENTION | Python | Inconsistent with upstream style |
| R65 | coding_standards.md | Universal formatting | Spaces only, ~100 columns, American spelling, no emoji in text, single-letter `e` for caught errors, avoid ", got" in error messages | CONVENTION | all code | Style drift |
| R66 | ffi.md | whole document | Every `*_new` has a matching `*_drop`; drop exactly once; wrap `extern "C"` bodies in `abort_on_panic`; Rust-side capsules use `PyCapsule::new_with_destructor` | MUST (when FFI exists) | FFI | Double-free, leak, or UB |

---

## 3. Applicability map for this project

This adapter is an **external, Python-only package** (`nautilus_gateio`), not an in-tree adapter.

| Area | Guide material | Applicability |
|---|---|---|
| Adapter architecture | adapters.md Structure, Phases 1–7 | **Relevant only if proposing upstream inclusion** for the Rust-core parts; the Python-layer component list (provider, data client, exec client, factories, config) is **applicable now** and already followed |
| Instrument provider | adapters.md InstrumentProvider; spec_data_testing TC-D01…D03 | **Applicable now** |
| Market-data client | adapters.md MarketDataClient hook list; spec_data_testing groups 2–8 | **Applicable now** (hook names and semantics are the live contract) |
| Execution client | adapters.md ExecutionClient; spec_exec_testing groups 1–10 | **Applicable now** |
| Reconciliation | adapters.md Phase 4.5, two-tier routing; spec_exec_testing TC-E84…E87, E101 | **Applicable now** — highest-value area |
| Order books | adapters.md Order book delta flag requirements | **Applicable now**, non-negotiable |
| HTTP | adapters.md HTTP client patterns, retry classification, rate limiting, credentials | **Applicable when modifying this area**; the Rust type names are illustrative, the behaviours (signing, classification, quotas, fail-fast credentials) are functional |
| WebSocket | adapters.md WebSocket client patterns, subscription lifecycle, reconnection, ping/pong, close | **Applicable when modifying this area**; two-layer client/handler split is a Rust-specific structural convention |
| Python | python.md, coding_standards.md | **Applicable now** for style alignment; not correctness |
| Rust | rust.md, adapters.md Rust patterns | **Relevant only to a future Rust-core migration** |
| FFI / PyO3 | ffi.md, python.md PyO3 property rule | **Relevant only to a future Rust-core migration**; nothing in this package crosses an FFI boundary today |
| Testing | testing.md ladder and projection rule | **Applicable now** |
| Fixtures and datasets | test_datasets.md | **Applicable when adding fixtures**; the R2 bucket and repo paths are upstream-specific, the provenance rules are general |
| Benchmarking | benchmarking.md | **Not applicable to an external adapter** as written (Criterion/iai are Rust tooling); revisit with a Rust core |
| Coding standards | coding_standards.md | **Applicable now** |
| Documentation | docs.md | **Applicable now** for capability tables and terminology |
| Packaging and releases | releases.md | **Not applicable to an external adapter** (upstream three-branch model, crates.io); the release-note section taxonomy is a useful model — **requires later verification** if proposing upstream inclusion |
| Environment setup | environment_setup.md | **Not applicable** (upstream repo tooling); becomes relevant for upstream contribution |

---

## 4. High-risk correctness checklist

Use before merging changes in the named area. Every item traces to a guide requirement.

- [ ] **Book flags** — snapshot deltas carry `F_SNAPSHOT`; the final delta of every group carries
      `F_LAST`; an empty-book CLEAR carries both. (R1–R5, adapters.md)
- [ ] **Snapshot/delta behaviour** — a resync republishes a clean snapshot rather than leaving a
      stale book; sequence continuity is validated. (adapters.md order-book section; books contract)
- [ ] **Lifecycle order** — data: instruments → cache → emit → cache-to-WS → connect. Execution:
      instruments → WS → subscribe → stream → account state → await account registered → connected.
      All of it inside `connect()`. (R12, R13)
- [ ] **Order identity** — venue order id changes (conditional order fired) are handled by the one
      event the framework allows to carry a new id; tracked-vs-external classification is explicit.
      (R16; NautilusTrader `Order.apply` contract)
- [ ] **Duplicate suppression** — lifecycle events are emitted once; fills dedup by venue trade id
      across WebSocket and REST. (R14, R15)
- [ ] **Missing events** — a fill that arrives only over REST (stream gap) still reaches the engine.
      (R11, R15)
- [ ] **Reconciliation reports** — all four generators implemented and exercised with pre-existing
      open orders, historical fills, and open long/short positions. (R10, R18, R19)
- [ ] **Unsupported operations** — denied before submission with a reason, never silently converted.
      (R17)
- [ ] **Timestamps** — `ts_event` from the venue, `ts_init` from the clock, ms→ns at the parser
      boundary. (R6)
- [ ] **Retries and error classification** — mutating requests are not retried in a way that can
      double-execute; retryable/non-retryable/fatal are distinguished. (R23)
- [ ] **Credentials** — resolved centrally, fail fast when invalid, never silently unauthenticated.
      (R21, R22)
- [ ] **Reconnect** — auth restored, subscriptions replayed from stored arguments, books resynced.
      (R25, R26)
- [ ] **Async shutdown** — disconnect command before stop signal; tasks awaited with timeout and
      aborted if stuck; no orphaned tasks. (R27, R31)
- [ ] **Instrument caching** — cache surface is standard and populated before any message that
      needs it. (R43, R13)
- [ ] **Message immutability** — no mutation of a message after construction. (R7)
- [ ] **Clean installation and packaging** — the built artifact contains every sub-package and the
      documented public imports resolve from the installed distribution, not the source tree.
      (project requirement; guide's Phase 7.4 examples requirement is the nearest upstream anchor)
- [ ] **Test data provenance** — fixtures come from official docs or the live API, never invented.
      (R50)
- [ ] **Spec tests** — the `DataTester` and `ExecTester` subsets matching claimed capabilities pass.
      (R56, R57)
- [ ] **FFI ownership** *(only if a Rust core is ever added)* — `*_new`/`*_drop` pairing, drop
      exactly once, `abort_on_panic` at the boundary, capsules with destructors. (R66)

---

## 5. Task-triggered reading map

Open the listed upstream documents **before** starting the work. Retrieve only these, not the
whole guide.

| Trigger | Read |
|---|---|
| Adapter architecture change | `adapters.md` (Structure, Adapter implementation sequence, Python adapter layer) + `design_principles.md` |
| Data client change | `adapters.md` (MarketDataClient, Connection lifecycle → Data client) + `spec_data_testing.md` |
| Order book work | `adapters.md` (Order book delta flag requirements) + `spec_data_testing.md` group 2 |
| Execution client change | `adapters.md` (ExecutionClient, Message routing, `WsDispatchState`, Cross-source fill deduplication) + `spec_exec_testing.md` |
| Reconciliation work | `adapters.md` (Phase 4.5, Connection lifecycle → Execution client) + `spec_exec_testing.md` group 9 |
| HTTP client change | `adapters.md` (HTTP client patterns, Request signing, Credential module structure, Environment variable conventions, Error handling and retry logic, Rate limiting) |
| WebSocket change | `adapters.md` (WebSocket client patterns, Authentication, Subscription management, Reconnection logic, Ping/Pong, Disconnection lifecycle, Message routing, Backpressure) |
| Symbology change | `adapters.md` (Symbol normalization) |
| Instrument provider change | `adapters.md` (InstrumentProvider, Instrument cache standardization) + `spec_data_testing.md` group 1 |
| Python code change | `python.md` + `coding_standards.md` |
| Rust change *(future)* | `rust.md` + `coding_standards.md` |
| PyO3 / FFI change *(future)* | `ffi.md` + `rust.md` + `python.md` |
| Market-data tests | `spec_data_testing.md` + `testing.md` + `test_datasets.md` |
| Execution tests | `spec_exec_testing.md` + `testing.md` + `test_datasets.md` |
| Adding a fixture or dataset | `test_datasets.md` |
| Release | `releases.md` + `docs.md` + `testing.md` |
| Documentation change | `docs.md` |
| Performance work | `benchmarking.md` |
| Considering upstream inclusion | `adapters.md` (whole) + `rust.md` + `ffi.md` + `environment_setup.md` + `releases.md` |

---

## 6. Known deliberate deviations

Recorded as facts, not defects. Nothing here is a licence to skip a correctness requirement.

1. **This adapter is an external package.** It is distributed as `nautilus-gateio-adapter`
   (import package `nautilus_gateio`) and installed alongside NautilusTrader, not merged into
   `nautilus_trader/adapters/`. Upstream repository conventions (branch model, crates.io
   publishing, `RELEASES.md`, `tests/integration_tests/adapters/<adapter>/` placement,
   `make` targets, uv pinning) describe the upstream repository and do not bind this project.

2. **The implementation is Python-only.** `adapters.md` describes a layered architecture with a
   Rust core (`crates/adapters/<name>/`) for networking and parsing plus PyO3 bindings, and says
   to implement the Rust core before the Python layer. This project implements the Python layer
   only, on `httpx` and `websockets`.

3. **That difference is architectural, not a correctness gap.** The guide's Rust sections
   prescribe *how* an in-tree adapter is built (crate layout, `ArcSwap` connection state,
   `RetryManager`, `SubscriptionState`, `Ustr` interning, `spawn_task`, `CancellationToken`).
   The *behaviours* those patterns exist to guarantee — signing, retry classification, rate-limit
   quotas, subscription replay on reconnect, ping/pong, ordered shutdown, dedup of lifecycle
   events and fills — are language-independent and **do apply**. Absence of Rust is not by itself
   a defect; absence of those behaviours would be.

4. **A Rust/PyO3 migration is a separate future decision.** It has not been taken, and nothing in
   this digest authorises starting one. If it is ever taken, `rust.md`, `ffi.md` and the Rust
   sections of `adapters.md` become binding, and the FFI memory contract (R66) becomes a hard
   correctness requirement rather than a dormant one.

5. **Functional correctness requirements apply regardless.** Order-book flags, reconciliation
   reports, lifecycle ordering, duplicate suppression, explicit rejection of unsupported
   operations, timestamp conventions and message immutability are required of any adapter in any
   language.

6. **Some upstream items have no external analogue.** `benchmarking.md` (Criterion/iai),
   `environment_setup.md` (upstream toolchain) and most of `releases.md` (three-branch model,
   crates.io Trusted Publishing) are not applicable to this repository as written; their
   *principles* (measure before optimising, pin tools, order release steps so artifacts exist
   before publication) remain useful.

---

## 7. Reading completeness

All fifteen documents at the pinned commit were read in full during the ingestion pass:
`index.md`, `design_principles.md`, `coding_standards.md`, `python.md`, `rust.md`, `ffi.md`,
`testing.md`, `test_datasets.md`, `spec_data_testing.md`, `spec_exec_testing.md`, `adapters.md`,
`benchmarking.md`, `docs.md`, `environment_setup.md`, `releases.md`
(416,894 bytes / 9,422 lines total).

This digest is an index. When an exact requirement drives a decision, open the pinned upstream
file and cite the filename and section in the decision record.

## 8. Open documentation ambiguities

Points where the guide is unclear or internally inconsistent, to revisit when they become
load-bearing:

1. **Rust core: requirement or convention?** `index.md` and `adapters.md` state the layered
   Rust+Python architecture and say "Implement the Rust core before any Python layer", but the
   guide never says an adapter *must* be Rust-backed, and the Python adapter layer is specified
   independently and completely. The instruction reads as an in-tree build order rather than a
   correctness rule; it should be confirmed with maintainers before any upstream-inclusion claim.
2. **`LiveNode` vs `TradingNode`.** `spec_data_testing.md` and `spec_exec_testing.md` say new
   Rust-backed PyO3 adapters should prefer `nautilus_trader.live.LiveNode`, while
   `TradingNode` remains the legacy v1 runtime. Which runtime an external Python-only adapter
   should target for its own acceptance runs is not stated.
3. **Spec-test applicability to external adapters.** Both specs say "each adapter must pass the
   subset of tests matching its supported capabilities", but the `DataTester` / `ExecTester`
   harnesses are described from inside the upstream repository. Whether an external package may
   claim compliance by running the same testers from the installed distribution is not addressed.
4. **Book-depth coverage gaps.** `spec_data_testing.md` marks TC-D12 (book depth) and TC-D15
   (historical book deltas) as "Not yet supported" in the Rust `DataTester`, so the reference
   implementation cannot currently exercise them; the pass criteria still apply to adapters.
5. **Backpressure vs bounded channels.** `adapters.md` instructs that latency-sensitive channels
   stay unbounded and explicitly prefers an OOM crash to dropping data. The guide does not say
   how this interacts with a Python asyncio adapter, where the equivalent trade-off differs.
6. **Documentation of PyO3 binding files.** `adapters.md` states documentation conventions for
   files under `python/` are "TBD (may use numpydoc specification)" — unresolved upstream.
