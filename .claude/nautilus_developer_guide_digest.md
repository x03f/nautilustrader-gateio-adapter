# NautilusTrader developer-guide digest (pinned reference)

Development metadata for this repository. Not public adapter documentation, and **not a replacement
for the upstream source**: this is an index that tells a future reader which upstream document to
open and what that document actually requires.

Pinned commit: `c3eed62afb477c3efdb078f6a934f7c5f8f7db61`
Source: `nautechsystems/nautilus_trader`, `docs/developer_guide/`

The local copies used to build this digest were verified byte-identical to that commit by comparing
git blob hashes against the GitHub API — all 15 of 15 matched. That check is what makes "pinned"
a fact rather than a claim.

**When an exact requirement matters, reopen the original document and cite its filename and
section.** This register is an index into the source, and an index can be wrong in ways the source
cannot. The register is generated from a per-document extraction pass rather than typed by hand,
because the previous revision of this digest was hand-written and accumulated 45 substantive
errors — requirements attributed to documents that did not state them, classifications not derived
from the source, and consequences presented as if they were quoted register content.

---

## 1. Source manifest

Every document in the pinned directory, read start to finish. The final line of each file is
recorded verbatim as evidence that reading reached the end rather than stopping at a heading.

| Document | Lines | Read in full | Headings | Requirements | Final line (verbatim) |
|---|---|---|---|---|---|
| `adapters.md` | 2463 | yes | 114 | 72 | See the full [Execution Testing Spec](spec_exec_testing.m... |
| `benchmarking.md` | 220 | yes | 8 | 13 | group names, register in `Cargo.toml`, and start measuring. |
| `coding_standards.md` | 141 | yes | 11 | 26 | ::: |
| `design_principles.md` | 26 | yes | 2 | 4 | copies. The same ownership rule keeps the in-memory model... |
| `docs.md` | 250 | yes | 27 | 18 | - Keep parameter descriptions concise but complete. |
| `environment_setup.md` | 539 | yes | 12 | 20 | ``` |
| `ffi.md` | 142 | yes | 7 | 12 | New FFI code must use `PyCapsule` with destructors and fo... |
| `index.md` | 28 | yes | 2 | 2 | - [FFI Memory Contract](ffi.md) |
| `python.md` | 115 | yes | 10 | 14 | For more information, see the [Cython docs](https://cytho... |
| `releases.md` | 371 | yes | 12 | 15 | ``` |
| `rust.md` | 1589 | yes | 77 | 99 | you must follow these constraints to maintain forward/bac... |
| `spec_data_testing.md` | 889 | yes | 38 | 40 | --- |
| `spec_exec_testing.md` | 1878 | yes | 78 | 57 | \| `can_unsubscribe` \| bool \| True \| 9 \| |
| `test_datasets.md` | 337 | yes | 19 | 30 | files above. Raw HISTDATA CSV files remain user-fetched. |
| `testing.md` | 434 | yes | 38 | 40 | ::: |

Totals: **462 material requirements**, of which **138 are MUST**, and **213**
carry a risk of silently wrong behaviour rather than a loud failure.

---

## 2. Classification

| Level | Meaning |
|---|---|
| MUST | Mandatory for a correct integration. Violation produces wrong behaviour or breaks the platform contract. |
| SHOULD | Strongly recommended; deviation needs a stated reason. |
| CONVENTION | In-tree style or structure. Binding for upstream contribution, not for an external package. |
| OPTIONAL | Discretionary. |
| CONTEXT | Background that changes how the rest is read. |

Classification follows the source's own wording and is not strengthened beyond it. Where the
upstream text is genuinely ambiguous about how binding a statement is, that ambiguity is listed in
section 6 rather than resolved silently in this table.

**Accuracy of the Section column, measured rather than assumed.** Every one of the 462
citations was checked against the headings of its pinned source file:

| Result | Count |
|---|---|
| Matches a heading verbatim | 257 |
| Hierarchical label (`Parent -> Child`), every part a real heading | 53 |
| Partial match | 15 |
| Descriptive pointer, not a literal heading | 137 |

So the Section column is a **pointer, not a quotation**. Roughly two thirds resolve to a heading you
can search for; the remainder are human-readable labels such as "Execution Testing Spec (preamble)"
that identify a region rather than a titled section. Open the file and search the requirement text
itself if the pointer does not land.

---

## 3. Requirement register by area

Each requirement carries the document and section it came from. A requirement that applies to more
than one area appears under each.

Areas each requirement touches, as a coverage index:

| Area | Requirements | Tag |
|---|---|---|
| Adapter architecture | 50 | `arch` |
| Instrument provider | 16 | `prov` |
| Data client | 66 | `data` |
| Execution client | 92 | `exec` |
| Reconciliation | 23 | `recon` |
| Order books | 14 | `books` |
| HTTP and WebSocket | 43 | `net` |
| Python | 70 | `py` |
| Rust | 167 | `rust` |
| FFI / PyO3 | 48 | `ffi` |
| Testing | 144 | `test` |
| Datasets and fixtures | 32 | `fixt` |
| Benchmarking | 15 | `bench` |
| Documentation | 62 | `docs` |
| Packaging and releases | 52 | `pkg` |

| Level | Requirement | Areas | Source | Section |
|---|---|---|---|---|
| MUST! | Implement execution reconciliation: "Generate order, fill, and position status reports for startup reconciliation." Milestone: "Execution client submits orders, receives ... | recon exec | `adapters.md` | Adapter implementation sequence -> Phase 4: Order execution (4.5) |
| MUST! | "Start sessions with existing open orders to verify the adapter reconciles state on connect before issuing new commands." "Seed preloaded positions and confirm position ... | test recon exec | `adapters.md` | Common test scenarios -> State management |
| MUST! | Data client `connect()` order: (1) fetch instruments via REST (`bootstrap_instruments()`); (2) cache locally into the client's instrument map and HTTP client cache; | data prov | `adapters.md` | Connection lifecycle -> Data client |
| MUST! | Execution client `connect()` order: (1) `ensure_instruments_initialized_async()` (early-return if already cached, else fetch via REST and cache to HTTP client, WebSocket ... | exec recon | `adapters.md` | Connection lifecycle -> Execution client |
| MUST! | "Each adapter's `common/credential.rs` must provide two things: 1. `credential_env_vars()` free function: returns environment variable names as a tuple. 2. | net arch | `adapters.md` | HTTP client patterns -> Credential module structure |
| MUST! | Credential env var naming: Mainnet/Live `{VENUE}_API_KEY` / `{VENUE}_API_SECRET`; Testnet `{VENUE}_TESTNET_API_KEY` / `{VENUE}_TESTNET_API_SECRET`; | net arch py | `adapters.md` | HTTP client patterns -> Environment variable conventions |
| MUST! | "Nautilus uses `UnixNanos` (nanoseconds since epoch). Most venues deliver `ms`. Convert at the parser boundary using `nautilus_core::datetime::millis_to_nanos`; | data exec | `adapters.md` | HTTP client patterns -> Timestamp conventions |
| MUST! | `LiveDataClient` handles non-market data (news feeds, custom streams) via `_connect`, `_disconnect`, `_subscribe`, `_unsubscribe`, `_request`. | data py | `adapters.md` | Python adapter layer -> DataClient / MarketDataClient |
| MUST! | Subclass `LiveExecutionClient` and implement the "required overrides": `_connect`, `_disconnect`, `generate_order_status_report(GenerateOrderStatusReport) -> ... | exec recon py | `adapters.md` | Python adapter layer -> ExecutionClient |
| MUST | Subclass `nautilus_trader.common.providers.InstrumentProvider` and implement the "minimal overrides required for a complete integration": `load_all_async(filters)` ... | prov py | `adapters.md` | Python adapter layer -> InstrumentProvider |
| MUST! | "When implementing `_subscribe_order_book_deltas` or streaming order book data, adapters **must** set `RecordFlag` flags correctly on each `OrderBookDelta`." `F_LAST` ... | books data | `adapters.md` | Python adapter layer -> MarketDataClient -> Order book delta flag requirements |
| MUST! | "Both data and execution clients follow a strict initialization order during `connect()` to prevent race conditions with reconciliation and strategy startup. | data exec recon | `adapters.md` | Rust adapter patterns -> Connection lifecycle (`connect`) |
| MUST! | "All clients that cache instruments must implement three methods with standardized names: `cache_instruments()` (plural, bulk replace), `cache_instrument()` (singular ... | prov net arch | `adapters.md` | Rust adapter patterns -> Instrument cache standardization |
| MUST! | "The live runner calls sync `ExecutionClient` and `DataClient` trait methods from within a tokio runtime. | rust data exec | `adapters.md` | Task management -> Never use `block_on` in trait methods |
| MUST! | "Adapters should ship two layers of coverage: the Rust crate that talks to the venue and the Python glue that exposes it to the wider platform. | test | `adapters.md` | Testing |
| MUST! | "**Test data sourcing**: Test data must be obtained from either official API documentation examples or directly from the live API via network calls. | test fixt | `adapters.md` | Testing -> Rust testing -> Test file organization |
| MUST! | Use two enums: `{Venue}WsFrame` (serde-deserialized wire frames covering every JSON shape the venue can send — login responses, subscription acks, channel data, order ... | exec recon net | `adapters.md` | WebSocket client patterns -> Message routing |
| MUST! | On reconnection restore authentication and subscriptions: (1) preserve original subscription arguments in a separate collection keyed by topic (`subscription_args ... | net data exec | `adapters.md` | WebSocket client patterns -> Reconnection logic |
| MUST! | Two subscription states, Pending and Confirmed, with transitions: `mark_subscribe()` -> Pending; `confirm()` Pending->Confirmed; | net data | `adapters.md` | WebSocket client patterns -> Subscription management -> Subscription lifecycle |
| MUST! | 'iai is deterministic (immune to system noise) but results are machine-specific. Use it for regression detection within CI, not for cross-machine comparisons.' | bench | `benchmarking.md` | Tooling overview (:::note) |
| MUST! | Rule 1: 'Set up outside the timing loop. All work that doesn't change between iterations belongs in the surrounding code or in `iter_batched_ref`'s setup closure, not in ... | bench | `benchmarking.md` | Writing Criterion benchmarks |
| MUST! | '`iai` requires functions that take no parameters. Keep them small so the instruction count is meaningful and so changes outside the function don't leak into the ... | bench | `benchmarking.md` | Writing iai benchmarks |
| MUST! | "Once a message (request, response, event, or command) is created, its fields must not be mutated." The scope is explicitly all four message kinds - requests, responses ... | arch data exec books recon py | `design_principles.md` | Message immutability |
| MUST! | "Components treat incoming messages as input. If a component needs a different representation, it derives new local state or a new message explicitly." This is the ... | arch exec data py | `design_principles.md` | Message immutability |
| MUST! | 'NautilusTrader *must* compile and run on **Linux, macOS, and Windows**. Please keep portability in mind (use `std::path::Path`, avoid Bash-isms in shell scripts ... | arch py pkg test | `environment_setup.md` | Environment Setup (preamble, :::info admonition) |
| MUST! | Required for Rust/PyO3 on Linux and macOS after `uv sync`: `export PYO3_PYTHON="$PWD/.venv/bin/python"`; | rust ffi test | `environment_setup.md` | Setup > 4. Configure environment variables |
| MUST | Every constructor (*_new) MUST have a matching *_drop exported next to it. Complex values (OrderBook, SyntheticInstrument, TimeEventAccumulator) are heap-allocated with ... | ffi rust books | `ffi.md` | Box-backed `*_API` wrappers (owned Rust objects) |
| MUST | Validate parameters BEFORE heap allocation, to fail fast and avoid allocating invalid objects. | ffi rust | `ffi.md` | Box-backed `*_API` wrappers (owned Rust objects) |
| MUST | The Python/Cython binding must guarantee *_drop is invoked exactly once. Preferred for new code: wrap the pointer in a PyCapsule created with ... | ffi py rust | `ffi.md` | Box-backed `*_API` wrappers (owned Rust objects) |
| MUST | Final sentence, a merge gate: "New FFI code must use PyCapsule with destructors and follow this template before it can be merged." | ffi rust pkg | `ffi.md` | Box-backed `*_API` wrappers (owned Rust objects) |
| MUST | Step 1 (Rust): build a Vec<T> and convert with into() - this LEAKS the vector and transfers ownership of the raw allocation to foreign code. | ffi rust | `ffi.md` | CVec lifecycle |
| MUST! | Step 2 (foreign - Python/Cython/C): use the data while the CVec value is in scope, and "Do not modify the fields ptr, len, cap." | ffi py | `ffi.md` | CVec lifecycle |
| MUST! | Step 3 (foreign): EXACTLY ONCE, call the type-specific drop helper exported by Rust (e.g. | ffi rust books | `ffi.md` | CVec lifecycle |
| MUST! | Cython helpers that allocate temporary C buffers with PyMem_Malloc, wrap them in a CVec and return the address inside a PyCapsule create EVERY such capsule with a ... | ffi py books | `ffi.md` | Capsules created on the Python side |
| MUST! | When Rust pushes a heap-allocated value into Python it MUST use PyCapsule::new_with_destructor, with a destructor that reconstructs the original Box<T> or Vec<T> and ... | ffi rust | `ffi.md` | Capsules created on the Rust side (PyO3 bindings) |
| MUST! | Rust panics must never unwind across extern "C" functions - unwinding into C or Python is undefined behaviour and can corrupt the foreign stack or leave ... | ffi rust | `ffi.md` | Fail-fast panics at the FFI boundary |
| MUST! | There is no generic cvec_drop; the old one always treated the buffer as Vec<u8>. "Using it with any other element type causes a size-mismatch during deallocation and ... | ffi rust | `ffi.md` | Why there is no generic `cvec_drop` anymore |
| MUST! | For .pyx and .pxd files, all functions and methods returning void or a primitive C type (bint, int, double) must include the `except *` keyword in the signature. | py ffi | `python.md` | Cython (legacy) |
| MUST! | The NumPy docstring spec is used throughout the codebase. "This needs to be followed consistently so the docs build correctly." - a build-correctness consequence, not ... | py docs | `python.md` | Docstrings |
| MUST! | Do not use truthiness to test for None on anything other than collections. "Always use if foo is None: (or is not None) to check for a None value... | py data exec prov books net | `python.md` | PEP-8 |
| MUST! | "All function and method signatures *must* include type annotations" (must is italicised in the source). Examples given for __init__, on_bar, on_save, on_load. | py arch exec data prov | `python.md` | Type hints |
| MUST! | 'Do not use `cargo publish --workspace` for CI releases.' The release job runs `scripts/ci/publish-cargo-crates.sh`, which publishes crates one at a time in dependency ... | pkg rust | `releases.md` | Crates.io publishing |
| MUST! | Sequencing rules that must be kept intact when editing `.github/workflows/build.yml`: (a) 'The draft GitHub release must exist before any release asset upload or package ... | pkg | `releases.md` | Stable release workflow |
| MUST! | 'GitHub recommends creating a draft release, attaching all assets, then publishing the draft before enabling release immutability. | pkg | `releases.md` | Stable release workflow |
| MUST | Adapter crates under `crates/adapters/` must use `get_runtime().spawn()` instead of `tokio::spawn()`. | rust ffi arch | `rust.md` | Adapter runtime patterns |
| MUST! | Install custom runtimes before first use: Rust-native binaries owning `main()` may call `set_runtime()` before `LiveNode::build()` or any adapter/client usage. | rust arch ffi | `rust.md` | Adapter runtime patterns |
| MUST! | Cancellation safety: call out whether the function is cancellation-safe and what invariants still hold when it is cancelled. | rust net exec data | `rust.md` | Async patterns |
| MUST! | Timeout patterns: wrap network or long-running awaits with timeouts (`tokio::time::timeout`) and propagate or handle the timeout error. | rust net exec data | `rust.md` | Async patterns |
| MUST | All Rust files must include the standardized copyright header (Copyright (C) 2015-2026 Nautech Systems Pty Ltd, LGPL v3.0 licence block). | rust pkg | `rust.md` | Code style and conventions / File header requirements |
| MUST | Always use the `FAILED` constant for `.expect_display()` messages on `CorrectnessResult`, and import the trait that provides it: `use ... | rust | `rust.md` | Constructor patterns |
| MUST! | Use `debug_assert!` (and `debug_assert_eq!`/`_ne!`) only for internal invariants the correctness module does not model — field relationships, monotonic sequences, CAS ... | rust | `rust.md` | Design by contract |
| MUST | All struct and enum fields must have documentation with terminating periods. | rust docs | `rust.md` | Documentation standards / Field documentation |
| MUST | Document all public functions with: purpose and behavior; explanation of input argument usage; error conditions if applicable; panic conditions if applicable. | rust docs | `rust.md` | Documentation standards / Function documentation |
| MUST | All modules must have module-level documentation (`//!`) starting with a brief description. | rust docs | `rust.md` | Documentation standards / Module-Level documentation |
| MUST | For Safety documentation use the `SAFETY:` prefix followed by a short description explaining why the unsafe operation is valid; | rust ffi | `rust.md` | Documentation standards / Safety documentation format |
| MUST! | For one-key removal from an `IndexMap`, `shift_remove` preserves insertion order at O(n) cost while `swap_remove` is O(1) but swaps the last entry into the removed slot ... | rust exec recon | `rust.md` | Hash collections / AHashMap vs IndexMap microbenchmarks |
| MUST! | `AHash` randomizes its hasher per process, so `AHashMap`/`AHashSet` iteration order varies between runs. | rust exec recon test | `rust.md` | Hash collections / Iteration-order determinism |
| MUST | Install the Cap'n Proto compiler at the version specified in `tools.toml` in the repository root (see environment_setup.md#capn-proto). | rust | `rust.md` | Installing Cap'n Proto |
| MUST! | Do not use the `hash` pyclass attribute with `eq_int` enums. PyO3's auto-generated `__hash__` uses Rust's `DefaultHasher`, which produces different values than Python's ... | ffi py | `rust.md` | PyO3 enum conventions |
| MUST | When exposing Rust functions to Python via PyO3 the Rust symbol must be prefixed `py_*` to make its purpose explicit inside the Rust codebase, and `#[pyo3(name = "…")]` ... | ffi rust py | `rust.md` | PyO3 naming conventions |
| MUST | Several core subsystems rely on runtime invariants rather than compile-time guarantees. | rust arch | `rust.md` | Runtime invariants |
| MUST! | The actor registry, component registry and message bus each use `thread_local!` storage: an object registered on one thread is never visible from another. | rust arch data exec | `rust.md` | Runtime invariants / Thread-local registries |
| MUST! | `ActorRef` guards must be obtained and dropped within a single synchronous scope, never stored in a struct field, never held across an `.await` point, and never sent to ... | rust arch | `rust.md` | Runtime invariants / `ActorRef` usage rules |
| MUST | Four practices: (1) use `clone_py_object()` for Python object cloning, including inside manual `Clone` impls; | ffi rust | `rust.md` | Rust-Python memory management / Best practices |
| MUST! | Do not use `Arc<PyObject>` in callback-holding structs: Rust `Arc` holding Python objects increases the Python refcount, Python objects may reference Rust objects ... | ffi rust | `rust.md` | Rust-Python memory management / The reference cycle problem |
| MUST | Use plain `PyObject` (no `Arc` wrapper) and clone via `nautilus_core::python::clone_py_object`, implementing `Clone` manually ... | ffi rust | `rust.md` | Rust-Python memory management / The solution: GIL-based cloning |
| MUST | If a function is `unsafe` to call there must be a `Safety` section in the documentation explaining why it is unsafe, covering the invariants callers must uphold and how ... | rust ffi | `rust.md` | Safety policy |
| MUST | Crate-level lint: every crate that exposes FFI symbols enables `#![deny(unsafe_op_in_unsafe_fn)]`. | rust ffi | `rust.md` | Safety policy |
| MUST! | CVec contract: for raw vectors crossing the FFI boundary read the FFI Memory Contract (ffi.md). | ffi rust | `rust.md` | Safety policy |
| MUST! | When evolving Cap'n Proto schemas: additive changes only, adding new fields at the end; never remove fields (mark deprecated ones in comments); | rust test | `rust.md` | Schema evolution guidelines |
| MUST! | Do not clone a value out of a container and then mutate the clone (the pattern inherited from the Cython port). | rust exec recon arch | `rust.md` | Shared mutability storage |
| MUST! | `Cache::order_mut` takes `&mut Cache`, so strategies and adapters receiving a `CacheView` (which only exposes immutable cache borrows) cannot reach it. | rust exec arch | `rust.md` | Shared mutability storage |
| MUST | For events with many constructor arguments the canonical test builder is a fluent spec under `events/<event>/spec/<name>.rs`. | rust test | `rust.md` | Test specs (bon builders) |
| MUST! | Use a custom spec rather than `derive_builder::Builder` with `builder(default)`: the latter bypasses the production constructor, so invariants added later are not ... | rust test | `rust.md` | Test specs (bon builders) |
| MUST! | `AHashMap` is not thread-safe. Wrapping it in `Arc` only enables sharing the pointer across threads, it does not coordinate mutation. | rust | `rust.md` | Thread-safe hash map patterns |
| MUST! | Python type stubs (`.pyi`) are generated from Rust source via pyo3-stub-gen; every type and function exposed to Python needs a matching stub annotation so generated ... | ffi rust py | `rust.md` | Type stub annotations |
| MUST! | When implementing `Send` or `Sync` unsafely: (1) document exactly which fields violate the trait requirements; | rust ffi | `rust.md` | Unsafe Send/Sync requirements |
| MUST! | Before committing schema changes run `make check-capnp-schemas`, which skips with a warning if capnp is not installed (acceptable locally), fails on regeneration errors ... | rust test | `rust.md` | Verifying schema consistency |
| MUST! | Keep related dependencies aligned: `capnp`/`capnpc` (exact), `arrow`/`parquet` (major.minor), `datafusion`/`object_store`, `dydx-proto`/`prost`/`tonic`. | rust pkg | `rust.md` | Versioning guidance |
| MUST | "**Each adapter must pass the subset of tests matching its supported data types.**" — stated unconditionally and in bold at line 9. | test data | `spec_data_testing.md` | Data Testing Spec (intro) |
| MUST! | Skip when: **Never**. On start, request all instruments for the venue; `on_instruments` receives the list. | prov data test | `spec_data_testing.md` | Group 1: Instruments — TC-D01: Request instruments |
| MUST! | Skip when: **Never**. Load a single instrument by `InstrumentId` via the provider. Pass criteria: "Instrument loaded with correct ID, price precision, size increment ... | prov test | `spec_data_testing.md` | Group 1: Instruments — TC-D03: Load specific instrument |
| MUST | Stream `OrderBookDeltas` into `on_order_book_deltas`. Pass criteria: "Deltas received with valid instrument ID; | books data test | `spec_data_testing.md` | Group 2: Order book — TC-D10: Subscribe book deltas |
| MUST! | Periodic `OrderBook` snapshots into `on_order_book`. Pass criteria: "Book snapshots received with bid/ask levels; | books data test | `spec_data_testing.md` | Group 2: Order book — TC-D11: Subscribe book at interval |
| MUST! | `OrderBookDepth10` snapshots into `on_order_book_depth`. Pass criteria: "Depth snapshots received with up to 10 bid/ask levels; | books data test | `spec_data_testing.md` | Group 2: Order book — TC-D12: Subscribe book depth |
| MUST! | Subscribe to deltas with `manage_book=True`; the actor applies each delta to a local `OrderBook`. | books data test | `spec_data_testing.md` | Group 2: Order book — TC-D14: Managed book from deltas |
| MUST! | Skip when: **Never**. `QuoteTick` events into `on_quote_tick`. Pass criteria: "At least one `QuoteTick` received with valid bid/ask prices and sizes; **bid < ask**." | data test | `spec_data_testing.md` | Group 3: Quotes — TC-D20: Subscribe quotes |
| MUST! | Skip when: **Never**. `TradeTick` events into `on_trade_tick`. Pass criteria: "At least one `TradeTick` received with valid price, size, and **aggressor side**." | data test | `spec_data_testing.md` | Group 4: Trades — TC-D30: Subscribe trades |
| MUST! | `Bar` events into `on_bar` for a configured `BarType` (example: `BTCUSDT-PERP.VENUE-1-MINUTE-LAST-EXTERNAL`). | data test | `spec_data_testing.md` | Group 5: Bars — TC-D40: Subscribe bars |
| MUST | `MarkPriceUpdate` into `on_mark_price`; pass criteria "At least one `MarkPriceUpdate` received with valid instrument ID and mark price." Skip ONLY when the instrument is ... | data test | `spec_data_testing.md` | Group 6: Derivatives data — TC-D50: Subscribe mark prices |
| MUST! | `IndexPriceUpdate` into `on_index_price`; valid instrument ID and index price. Skip only when not a derivative or unsupported. | data test | `spec_data_testing.md` | Group 6: Derivatives data — TC-D51: Subscribe index prices |
| MUST! | `FundingRateUpdate` into `on_funding_rate`; valid instrument ID and rate. Skip only when the instrument is not a perpetual or the adapter does not provide funding rates. | data test | `spec_data_testing.md` | Group 6: Derivatives data — TC-D52: Subscribe funding rates |
| MUST! | Stop the actor with `can_unsubscribe=True` (the default). Event sequence: "Data subscriptions removed; | data net test | `spec_data_testing.md` | Group 9: Lifecycle — TC-D70: Unsubscribe on stop |
| MUST | Before running data tests the target instrument must be available and loadable via the instrument provider. | prov test | `spec_data_testing.md` | Prerequisites |
| MUST | "Each adapter must pass the subset of tests matching its supported capabilities." (bolded in the source). The matrix is capability-scoped, not all-or-nothing. | exec test | `spec_exec_testing.md` | Execution Testing Spec (preamble) |
| MUST! | StopMarket / StopLimit / MarketIfTouched / LimitIfTouched accepted with the correct trigger price (and limit price for STOP_LIMIT and LIT), and "The order should NOT ... | exec | `spec_exec_testing.md` | Group 3: Stop and conditional orders (TC-E20..TC-E27) |
| MUST! | Risk engine bypassed (`LiveRiskEngineConfig(bypass=True)`) "to avoid interference" during execution acceptance runs. | test exec | `spec_exec_testing.md` | Prerequisites |
| MUST | Reconciliation enabled (`LiveExecEngineConfig(reconciliation=True)`) "to verify state consistency" on every exec test run. | recon test | `spec_exec_testing.md` | Prerequisites |
| MUST! | Market order lifecycle emits `OrderInitialized` -> `OrderSubmitted` -> `OrderAccepted` -> `OrderFilled`; | exec | `spec_exec_testing.md` | TC-E01 / TC-E02: Market BUY / SELL - submit and fill |
| MUST! | "Some adapters simulate market orders as aggressive limit IOC orders (check adapter guide). | exec | `spec_exec_testing.md` | TC-E01: Considerations |
| MUST! | With `use_quote_quantity=True`: "Order submitted with quote currency quantity; fill quantity is in base currency." The unit flips between submission and fill reporting. | exec | `spec_exec_testing.md` | TC-E05: Market order with quote quantity |
| MUST | On stop the closing order is on the opposite side of the position; "Position closed (net quantity = 0), no open orders remaining". | exec | `spec_exec_testing.md` | TC-E06: Close position via market order on stop |
| MUST | Limit order emits `OrderInitialized` -> `OrderSubmitted` -> `OrderAccepted`; "Order is open on the venue with correct price, quantity, side, TIF=GTC"; | exec | `spec_exec_testing.md` | TC-E10 / TC-E11: Limit BUY/SELL GTC - submit and accept |
| MUST | Option order cancel emits `OrderPendingCancel` -> `OrderCanceled` and disappears from venue open orders; | exec recon | `spec_exec_testing.md` | TC-E100 / TC-E101: Cancel option order / reconcile option position |
| MUST! | An unfilled IOC order terminates as `OrderCanceled`: "The venue should cancel the unfilled IOC order; verify `OrderCanceled` event (not `OrderExpired`)." | exec | `spec_exec_testing.md` | TC-E14: Limit IOC passive - no fill |
| MUST! | GTD order "accepted with GTD TIF and correct expiry timestamp" (driven by `order_expire_time_delta_mins`). | exec | `spec_exec_testing.md` | TC-E17: Limit GTD - submit and accept |
| MUST! | "Some venues may report expiry as a cancel; verify the adapter maps this to `OrderExpired`." GTD expiry must surface as `OrderExpired` even when the venue's wire message ... | exec | `spec_exec_testing.md` | TC-E18: Limit GTD expiry |
| MUST! | Modify emits `OrderPendingUpdate` -> `OrderUpdated` with the new price, and the order must exit `PendingUpdate`. | exec | `spec_exec_testing.md` | TC-E30 / TC-E31: Modify limit BUY/SELL price |
| MUST | Cancel-replace emits `OrderPendingCancel` -> `OrderCanceled` -> `OrderInitialized` -> `OrderSubmitted` -> `OrderAccepted`, leaving "Two distinct orders in the cache: the ... | exec | `spec_exec_testing.md` | TC-E32 / TC-E33: Cancel-replace limit BUY/SELL |
| MUST | Stop trigger amend emits `OrderPendingUpdate` -> `OrderUpdated` with the new trigger price; cancel-replace of a stop emits the full cancel-then-resubmit sequence. | exec | `spec_exec_testing.md` | TC-E34 / TC-E35: Modify / cancel-replace stop order |
| MUST! | If the adapter does not support modify, an attempted modify produces `OrderModifyRejected` "with reason; original order remains unchanged"; | exec | `spec_exec_testing.md` | TC-E36: Modify rejected |
| MUST! | Cancel emits `OrderPendingCancel` -> `OrderCanceled`; "Verify the `OrderCanceled` event contains the correct `venue_order_id`." `cancel_orders_on_stop=True` is the ... | exec | `spec_exec_testing.md` | TC-E40: Cancel single limit order |
| MUST! | Three distinct cancel paths must work: cancel-all on stop (default), `use_individual_cancels_on_stop=True`, and `use_batch_cancel_on_stop=True` ("All orders canceled via ... | exec net | `spec_exec_testing.md` | TC-E41 / TC-E42 / TC-E43: Cancel all / individual / batch on stop |
| MUST | Cancelling a non-open order produces `OrderCancelRejected` with a reason indicating "the order is not in a cancelable state". Skip when: Never. | exec | `spec_exec_testing.md` | TC-E44: Cancel already-canceled order |
| MUST! | With `use_post_only=True` at a passive price the order is "accepted as a maker order; post-only flag acknowledged by venue". | exec | `spec_exec_testing.md` | TC-E60: PostOnly accepted |
| MUST! | With `reduce_only_on_stop=True` the closing order carries the reduce-only flag and the position is fully closed. | exec | `spec_exec_testing.md` | TC-E61: ReduceOnly on close |
| MUST! | With `order_display_qty` < `order_qty`: "Order accepted with display quantity set; only display qty visible on the book." | exec | `spec_exec_testing.md` | TC-E62: Display quantity (iceberg) |
| MUST | A post-only order priced to cross produces `OrderInitialized` -> `OrderSubmitted` -> `OrderRejected` "with reason indicating post-only violation" (driven by ... | exec | `spec_exec_testing.md` | TC-E70: PostOnly rejection |
| MUST | A reduce-only order with no position to reduce produces `OrderRejected` "with reason indicating reduce-only violation" (driven by `test_reject_reduce_only=True`, which ... | exec | `spec_exec_testing.md` | TC-E71: ReduceOnly rejection |
| MUST! | An unsupported order type produces `OrderDenied` "pre-submission rejection by adapter", "before reaching venue", with a reason. | exec | `spec_exec_testing.md` | TC-E72: Unsupported order type |
| MUST! | An unsupported time-in-force produces `OrderDenied` pre-submission with a reason. Skip when: Never - "every adapter has unsupported TIF options to test". | exec | `spec_exec_testing.md` | TC-E73: Unsupported TIF |
| MUST! | "Position opened on start; market order submitted and filled BEFORE limit order maintenance begins." Ordering between the opening market order and the maintenance loop ... | exec | `spec_exec_testing.md` | TC-E80: Open position on start |
| MUST! | On stop, "All strategy-owned open orders canceled" and "All strategy-owned positions closed; net position = 0". Scope is strategy-owned state, not the whole account. | exec | `spec_exec_testing.md` | TC-E81 / TC-E82: Cancel orders / close positions on stop |
| MUST! | "Use `external_order_claims` to claim the instrument so the adapter reconciles orders for it" and "Verify that the reconciled order count matches the venue-reported ... | recon test | `spec_exec_testing.md` | TC-E84: Considerations |
| MUST! | Starting with `reconciliation=True` generates an `OrderStatusReport` per open order; "Each open order is loaded into the cache with correct `venue_order_id` ... | recon exec | `spec_exec_testing.md` | TC-E84: Reconcile open orders |
| MUST! | `FillReport` per historical fill; "Each filled order is loaded into the cache with correct `venue_order_id`, status=FILLED, fill price, fill quantity, and commission." ... | recon exec | `spec_exec_testing.md` | TC-E85: Reconcile filled orders |
| MUST! | `PositionStatusReport` per position; "Position loaded into cache with correct instrument, side, quantity, and entry price matching the venue"; | recon exec | `spec_exec_testing.md` | TC-E86 / TC-E87: Reconcile open long / short position |
| MUST | For options the rejection point is explicitly adapter-dependent: "`OrderDenied` (pre-submission) or `OrderSubmitted` -> `OrderRejected` (post-submission)". | exec docs | `spec_exec_testing.md` | TC-E94 / TC-E96: Unsupported order type / conditional order for options |
| MUST! | Authority split between the two files: 'metadata.json is authoritative for provenance, licensing, and redistribution rules. | fixt | `test_datasets.md` | Adding a new dataset |
| MUST! | 'Tests that rely on user-fetched data should: Be marked or grouped separately from default CI tests. Skip with a clear message when the local dataset is absent. | fixt test py | `test_datasets.md` | Adding a new dataset |
| MUST | For datasets NautilusTrader cannot redistribute: (1) 'Commit a manifest and metadata.json, but do not commit the real vendor data or derived Parquet output.' (2) ... | fixt test | `test_datasets.md` | Curation workflow > User-fetched pipelines (restricted redistribution) |
| MUST! | Explicit prohibitions - 'Do not: Upload restricted vendor datasets to the public R2 bucket. | fixt test pkg | `test_datasets.md` | Curation workflow > User-fetched pipelines (restricted redistribution) |
| MUST | Use the user-fetched model when 'a vendor license, entitlement model, or access control does not allow NautilusTrader to redistribute the data through the public repo or ... | fixt test | `test_datasets.md` | Dataset categories |
| MUST! | 'When a schema change invalidates a large Parquet file, regenerate it from the original source data using the curation tests below. | fixt | `test_datasets.md` | Regenerating datasets |
| MUST! | 'Every curated dataset that stores or redistributes a concrete artifact must include a metadata.json with at minimum:' `file` (filename), `sha256` (SHA-256 hash of the ... | fixt | `test_datasets.md` | Required metadata |
| MUST! | User-fetched datasets use the same fields where applicable and must additionally include: `distribution` ('Must be "user-fetch"'), `fetch_method`, `fetch_reference` ... | fixt | `test_datasets.md` | Required metadata |
| MUST! | 'Do not write python/tests/ cases that probe Rust panic paths in process with pytest.raises(BaseException) or similar broad catches. | test py ffi | `testing.md` | Running tests > Python tests |
| MUST! | 'Do not capture log output to assert on log messages. Log capture in tests is fragile because loggers are global state, test execution order is non-deterministic, and ... | test py | `testing.md` | Test style > General |
| SHOULD | Phase 6: `LiveDataClientConfig`/`LiveExecClientConfig` subclasses, factory functions instantiating clients from configuration, credential resolution from environment ... | arch py docs | `adapters.md` | Adapter implementation sequence -> Phase 6/7 |
| SHOULD! | "Cover each supported order type (limit, market, stop, conditional, etc.) under every venue time-in-force option, expiries, and rejection handling." "Submit buy and sell ... | test exec | `adapters.md` | Common test scenarios -> Order flow |
| SHOULD! | "Exercise adapters across every venue behaviour they claim to support. Incorporate these scenarios into the Rust and Python suites." Test each supported product family ... | test prov | `adapters.md` | Common test scenarios -> Product coverage |
| SHOULD | "Use the `RetryManager` from `nautilus_network` for consistent retry behavior." | net rust | `adapters.md` | HTTP client patterns -> Error handling and retry logic |
| SHOULD! | Parsers live in `common/parse.rs` (cross-cutting) or `http/parse.rs` (REST-specific), take venue data plus context (account IDs, timestamps, instrument references) and ... | data exec arch | `adapters.md` | HTTP client patterns -> Parser functions |
| SHOULD! | Keep signing in a `Credential` struct under `common/credential.rs`: API keys as `Ustr`, secrets in `Box<[u8]>` with `#[zeroize]`; | net | `adapters.md` | HTTP client patterns -> Request signing and authentication |
| SHOULD | For adapters with multiple client types, define an adapter-level error enum aggregating component errors (HTTP, WebSocket, Build) via `#[from]`, enabling unified ... | arch net | `adapters.md` | Rust adapter patterns -> Error taxonomy (`common/error.rs`) |
| SHOULD! | When polling instrument status via REST, put diff logic in `common/status.rs` with signature `diff_and_emit_statuses(new_statuses, cached_statuses, subscriptions ... | data | `adapters.md` | Rust adapter patterns -> Instrument status diffing (`common/status.rs`) |
| SHOULD! | When sophisticated retry logic is needed, define a retry classification module distinguishing Retryable (with `retry_after`), NonRetryable and Fatal errors, with helpers ... | net exec | `adapters.md` | Rust adapter patterns -> Retry classification (`common/retry.rs`) |
| SHOULD! | Use `tokio_util::sync::CancellationToken` created at construction and cloned into each spawned task, selected on alongside primary work; cancel on disconnect. | rust data exec | `adapters.md` | Task management -> Graceful shutdown with `CancellationToken` |
| SHOULD! | "Wrap all spawned work with a `spawn_task()` method that provides error logging and handle tracking" using `get_runtime().spawn()`. | rust data exec | `adapters.md` | Task management -> Spawning async tasks (`spawn_task`) |
| SHOULD! | Infrastructure: mock Axum server serving HTTP endpoints and WS channels; `TestServerState` tracking connections/subscriptions/auth; | test data exec | `adapters.md` | Testing -> Rust testing -> Data and execution client integration testing |
| SHOULD | "Never use bare `tokio::time::sleep()` with arbitrary durations. Tests become flaky under CI load and slower than necessary." Use `wait_until_async` to poll for ... | test | `adapters.md` | Testing -> Rust testing -> Integration tests (CI robustness) |
| SHOULD! | HTTP: happy paths converting a public resource into Nautilus domain models; credential guard (private endpoint without credentials -> structured error, then with ... | test net | `adapters.md` | Testing -> Rust testing -> Integration tests (HTTP / WebSocket coverage) |
| SHOULD | Test (in source modules): deserialization of venue JSON into structs; parsers converting venue types to Nautilus domain models; | test | `adapters.md` | Testing -> Rust testing -> Unit tests |
| SHOULD! | Three areas. Message types: deserialize every message variant from `test_data/` fixtures; | test net | `adapters.md` | Testing -> Rust testing -> WebSocket unit test coverage |
| SHOULD! | Authentication is event-driven: handler processes the `Login` response and returns `{Venue}WsMessage::Authenticated` immediately; | net exec | `adapters.md` | WebSocket client patterns -> Authentication |
| SHOULD! | Three-step shutdown: (1) send `HandlerCommand::Disconnect` so the handler cleans up gracefully; | net | `adapters.md` | WebSocket client patterns -> Disconnection lifecycle (`close`) |
| SHOULD! | "Channel send failures (client -> handler) should propagate loudly as `Result<(), Error>`." "WebSocket send failures (handler -> network) should be retried by the ... | net exec | `adapters.md` | WebSocket client patterns -> Error handling (Client-side / Handler-side) |
| SHOULD! | `WsDispatchState` only dedups within one stream. "When an adapter receives fills from multiple sources (WebSocket user data and HTTP reconciliation), a separate ... | exec recon | `adapters.md` | WebSocket client patterns -> Message routing -> Cross-source fill deduplication |
| SHOULD! | Execution dispatch state in `websocket/dispatch.rs` tracks emitted lifecycle events to prevent duplicates across reconnections and fast-fill races: `order_identities` ... | exec recon | `adapters.md` | WebSocket client patterns -> Message routing -> `WsDispatchState` |
| SHOULD! | "Some venues use the same WebSocket protocol for all product types but serve them on separate endpoints (e.g., Bybit provides distinct URLs for Linear, Spot, and ... | data net arch | `adapters.md` | WebSocket client patterns -> Multi-product WebSocket management |
| SHOULD! | Support both WebSocket control-frame pings (handled by `WebSocketClient` via the `PingHandler` callback) and application-level text pings (e.g. | net | `adapters.md` | WebSocket client patterns -> Ping/Pong handling |
| SHOULD | Two complementary Rust frameworks. Criterion measures 'Wall-clock time with confidence bands'; prefer it for 'Anything >= 100 ns; absolute measurement; | bench rust | `benchmarking.md` | Tooling overview |
| SHOULD! | Rule 3: 'Use `iter_batched_ref` for mutating benches. It excludes input `Drop` from the timed region, which otherwise dominates the measurement on benches that own large ... | bench | `benchmarking.md` | Writing Criterion benchmarks |
| SHOULD! | `exclude-newer = "3 days"`: `uv lock` ignores package versions published within the last 3 days. | pkg | `environment_setup.md` | Dependency management |
| SHOULD! | `no-build-package` is an explicit list of every third-party package locked in `uv.lock`; uv 'refuses to build any of them from source'. | pkg test | `environment_setup.md` | Dependency management |
| SHOULD | `prek install` to register the hook. 'Before opening a pull-request run the formatting and lint suite locally so that CI passes on the first attempt': `make format` then ... | py rust pkg | `environment_setup.md` | Setup > 3. Set up pre-commit |
| SHOULD | When a private method needs context (a tricky precondition or side effect), prefer a short inline `#` comment near the relevant logic rather than a docstring. | py | `python.md` | Private methods |
| SHOULD | Use PEP 604 union syntax for optional types: `-> Instrument \| None` preferred; `-> Optional[Instrument]` explicitly listed under "Avoid". | py | `python.md` | Type hints |
| SHOULD! | Use `get_runtime().block_on()` for sync-to-async bridges when synchronous code in adapters needs to call async functions. | rust arch | `rust.md` | Adapter runtime patterns |
| SHOULD | Async function naming needs no special suffix — prefer natural names. Return `anyhow::Result` from async functions to match synchronous conventions. | rust net | `rust.md` | Async patterns |
| SHOULD | Avoid `.clone()` in hot paths (favour borrowing or shared ownership via `Arc`); avoid `.unwrap()` in production code — generally propagate errors with `?` or map into ... | rust | `rust.md` | Common anti-patterns |
| SHOULD | Where unsafe code relies on invariants add defense mechanisms: type verification (check types at runtime before casting, e.g. | rust ffi | `rust.md` | Defense in depth |
| SHOULD | Prefer the type system first — ownership, lifetimes, `Send`/`Sync`, `Result`/`Option`, exhaustive matching, newtypes and visibility encode most contracts at compile time ... | rust | `rust.md` | Design by contract |
| SHOULD | For most preconditions use the `nautilus_core::correctness` module — it is the project's design-by-contract mechanism and should be the default. | rust | `rust.md` | Design by contract |
| SHOULD | Mechanism-selection table: public API input against named preconditions → `check_*` from `nautilus_core::correctness`; | rust | `rust.md` | Design by contract |
| SHOULD | Use `anyhow::Result<T>` for fallible functions as the primary pattern; use `thiserror` for domain-specific error types; propagate with `?`; | rust | `rust.md` | Error handling |
| SHOULD! | Prefer additive feature flags: enabling a feature must not break existing functionality. Use descriptive flag names. Document every feature in crate-level documentation. | rust pkg | `rust.md` | Feature flag conventions |
| SHOULD | When a collection is lookup-only (no `.iter()`, `.values()`, `.keys()`, `.into_iter()`, `.drain()`, or `for x in map`), iteration order is irrelevant and ... | rust | `rust.md` | Hash collections / Iteration-order determinism |
| SHOULD | Prefer `AHashMap`/`AHashSet` for lookup-heavy hot paths where iteration order does not feed observable state (AES-NI, 2-3x faster than SipHash, low collision rates ... | rust | `rust.md` | Hash collections / Performance |
| SHOULD | Use standard `HashMap` when cryptographic security is required — hash flooding attacks are a concern when handling untrusted user input in network protocols. | rust net data | `rust.md` | Hash collections / Performance |
| SHOULD | Enums exposed to Python should use these `pyclass` attributes: `frozen` (immutable value types), `eq, eq_int` (equality with enum instances and integer discriminants) ... | ffi py | `rust.md` | PyO3 enum conventions |
| SHOULD | Reach for `Rc<RefCell<T>>` (single-threaded) or `Arc<RwLock<T>>` (multi-threaded) storage only when all three hold: the value is mutated after insertion; | rust | `rust.md` | Shared mutability storage |
| SHOULD! | Determinism: under `cargo nextest` each test runs in a fresh process so the per-thread UUID sequence resets automatically; | rust test | `rust.md` | Test specs (bon builders) |
| SHOULD | Decision tree: (1) iteration order observable on the DST path → `IndexMap`/`IndexSet`; (2) otherwise by access pattern — immutable after construction → `Arc<AHashMap>` ... | rust | `rust.md` | Thread-safe hash map patterns |
| SHOULD | For types that parse from strings provide both conversions: `FromStr` for fallible parsing returning `Result`, and `impl<T: AsRef<str>> From<T>` for ergonomic infallible ... | rust | `rust.md` | Type conversion patterns |
| SHOULD | Subscribe to instrument updates; `on_instrument` receives the instrument with correct `instrument_id` and valid fields. | prov data test | `spec_data_testing.md` | Group 1: Instruments — TC-D02: Subscribe instrument |
| SHOULD | One-time book snapshot request returning bid/ask levels with valid prices and sizes via the historical data callback. | books data test | `spec_data_testing.md` | Group 2: Order book — TC-D13: Request book snapshot |
| SHOULD | Historical delta request; pass criteria "Deltas received with valid timestamps and book actions." Python config `request_book_deltas=True`; Rust "Not yet supported... | books data test | `spec_data_testing.md` | Group 2: Order book — TC-D15: Request historical book deltas |
| SHOULD | Historical quotes delivered via `on_historical_data`; pass criteria "Quotes received with valid timestamps, bid/ask prices and sizes." Reference config uses ... | data test | `spec_data_testing.md` | Group 3: Quotes — TC-D21: Request historical quotes |
| SHOULD! | Historical trades via `on_historical_data`; pass criteria "Trades received with valid timestamps, prices, sizes, and **trade IDs**." | data test | `spec_data_testing.md` | Group 4: Trades — TC-D31: Request historical trades |
| SHOULD! | Historical bars via callback; pass criteria "Bars received with valid OHLCV values and **ascending timestamps**." | data test | `spec_data_testing.md` | Group 5: Bars — TC-D41: Request historical bars |
| SHOULD | Historical funding rate request with a **default 7-day lookback**; pass criteria "Funding rates received with valid timestamps and rate values." Config ... | data test | `spec_data_testing.md` | Group 6: Derivatives data — TC-D53: Request historical funding rates |
| SHOULD | `InstrumentStatus` into `on_instrument_status`; pass criteria "Status events received with valid `MarketStatusAction` (e.g. | data test | `spec_data_testing.md` | Group 7: Instrument status — TC-D60: Subscribe instrument status |
| SHOULD! | `InstrumentClose` into `on_instrument_close` with valid close price and close type. Considerations: "Close events typically fire at end-of-session for traditional ... | data test docs | `spec_data_testing.md` | Group 7: Instrument status — TC-D61: Subscribe instrument close |
| SHOULD! | `OptionGreeks` into `on_option_greeks`; pass criteria "Greeks received with valid delta, gamma, vega, theta values." Considerations: greeks only for option instruments; | data test | `spec_data_testing.md` | Group 8: Option greeks — TC-D62: Subscribe option greeks |
| SHOULD! | `OptionChainSlice` into `on_option_chain`; pass criteria "Chain snapshot contains greeks for instruments matching the series." Considerations: "Option chain ... | data test | `spec_data_testing.md` | Group 8: Option greeks — TC-D63: Subscribe option chain |
| SHOULD! | The adapter must accept a `subscribe_params` dict of adapter-specific subscription parameters passed through from the tester. | data docs test | `spec_data_testing.md` | Group 9: Lifecycle — TC-D71: Custom subscribe params |
| SHOULD! | The adapter must accept a `request_params` dict of adapter-specific request parameters. | data docs test | `spec_data_testing.md` | Group 9: Lifecycle — TC-D72: Custom request params |
| SHOULD! | "If the venue offers a demo/testnet mode, use credentials created for that environment. | test net data | `spec_data_testing.md` | Prerequisites |
| SHOULD | "Legacy examples still use `nautilus_trader.live.node.TradingNode`, but new Rust-backed PyO3 adapters should prefer `nautilus_trader.live.LiveNode`. | test arch | `spec_data_testing.md` | Prerequisites |
| SHOULD! | The reference Python node setup configures the data engine as `LiveDataEngineConfig(time_bars_build_with_no_updates=False)` before running the DataTester. | test data | `spec_data_testing.md` | Prerequisites |
| SHOULD | Maintain a quick sanity run for "after adapter changes or between development iterations": market order opens a position on start, post-only limit buy + sell at ... | test exec | `spec_exec_testing.md` | Basic smoke test |
| SHOULD | "Data connectivity should be verified first using the Data Testing Spec" - run spec_data_testing before the exec matrix. | test data | `spec_exec_testing.md` | Execution Testing Spec (preamble) |
| SHOULD | "Document adapter-specific behavior (how a venue simulates market orders, handles TIF options, etc.) in the adapter's own guide, not here. | docs exec | `spec_exec_testing.md` | Execution Testing Spec (preamble) |
| SHOULD! | If brackets are supported: entry + take-profit + stop-loss are three accepted orders (entry below bid, TP above ask, SL below entry); | exec | `spec_exec_testing.md` | Group 6: Bracket orders (TC-E50..TC-E53) |
| SHOULD | Use demo/testnet credentials when the venue offers that mode; demo and production keys "are typically separate and not interchangeable; | exec test | `spec_exec_testing.md` | Prerequisites |
| SHOULD | Account funded with sufficient margin for the test instrument/quantities; target instrument available and loadable via the instrument provider before exec tests. | prov test | `spec_exec_testing.md` | Prerequisites |
| SHOULD | "Legacy examples still use `nautilus_trader.live.node.TradingNode`, but new Rust-backed PyO3 adapters should prefer `nautilus_trader.live.LiveNode`. | exec arch | `spec_exec_testing.md` | Prerequisites (Python node setup) |
| SHOULD! | "Fill price should be within the recent bid/ask spread. Partial fills are valid; verify the cumulative filled quantity matches the order quantity." | exec | `spec_exec_testing.md` | TC-E01: Considerations |
| SHOULD! | FOK fills completely "in a single fill event" or is cancelled with no fill; "FOK requires the entire quantity to be fillable; | exec | `spec_exec_testing.md` | TC-E15 / TC-E16: Limit FOK fill / no fill |
| SHOULD | DAY TIF accepted; "DAY orders may behave differently on 24/7 crypto venues vs traditional markets" - verify behavior outside trading hours if applicable. | exec docs | `spec_exec_testing.md` | TC-E19: Limit DAY - submit and accept |
| SHOULD! | "The `order_params` dict is opaque to the ExecTester and passed through to the adapter"; | exec docs | `spec_exec_testing.md` | TC-E63: Custom order params |
| SHOULD | With `can_unsubscribe=True` (default), stopping removes data subscriptions: "No further data events received after stop; clean disconnection." | data net | `spec_exec_testing.md` | TC-E83: Unsubscribe on stop |
| SHOULD! | "Some adapters may only report fills within a lookback window" - the lookback bound is adapter behaviour that must be known and documented. | recon docs | `spec_exec_testing.md` | TC-E85: Considerations |
| SHOULD! | Alternative option pricing is passed via `order_params` (e.g. OKX `px_usd`, `px_vol`). "The `price` field on the order object may be a placeholder when alternative ... | exec | `spec_exec_testing.md` | TC-E92: Limit with alternative pricing |
| SHOULD | "Some venues use a dedicated order type for options FOK orders (e.g. OKX uses `op_fok`). | exec | `spec_exec_testing.md` | TC-E99: FOK limit option |
| SHOULD | 'The manifest should be machine-readable and stable. It should capture the minimum information needed to reproduce the fetch and transform steps on another machine.' | fixt | `test_datasets.md` | Adding a new dataset |
| SHOULD | 'The default distribution order for new datasets is: 1. Checked in small data. 2. Public R2 large data. 3. User-fetched data. | fixt | `test_datasets.md` | Curation workflow > User-fetched pipelines (restricted redistribution) |
| SHOULD! | 'New datasets should be stored as Nautilus Parquet (not raw vendor formats).' Rationale given: consistent data types across all test datasets; | fixt test | `test_datasets.md` | Storage format |
| SHOULD | 'User-fetched datasets should also end up as Nautilus Parquet after the local transform step. | fixt | `test_datasets.md` | Storage format |
| SHOULD | 'Aim for high coverage without sacrificing appropriate error handling or causing "test induced damage" to the architecture.' Some branches are untestable without ... | test | `testing.md` | Code coverage |
| SHOULD! | `pragma: no cover` is restricted to two named cases only: (1) 'Asserting an abstract method raises NotImplementedError when called', (2) 'Asserting the final condition ... | test py | `testing.md` | Excluded code coverage |
| SHOULD! | 'Fuzzing introduces unstructured or malicious data to the system to verify it fails gracefully.' Use cases explicitly include 'Network boundaries, exchange data parsers ... | test net data | `testing.md` | Fuzzing |
| SHOULD | 'When building or modifying core types, write property tests to cover the mathematical boundaries.' | test | `testing.md` | Fuzzing |
| SHOULD! | 'Prefer hand-written stubs that return fixed values over mocking frameworks. Use MagicMock only when you need to assert call counts/arguments or simulate complex state ... | test py | `testing.md` | Mocks |
| SHOULD! | 'Property testing verifies that logic holds for *all* valid inputs, not just hand-picked examples.' Use cases: core domain types (Price, Quantity, UnixNanos), accounting ... | test rust | `testing.md` | Property-based testing |
| SHOULD! | Performance tests: use `--benchmark-disable-gc` because 'the flag prevents garbage collection from skewing results'. | test bench | `testing.md` | Running tests |
| SHOULD | 'Group assertions when possible: perform all setup/act steps first, then assert together to avoid the act-assert-act smell.' | test | `testing.md` | Test style > General |
| SHOULD! | 'Import model types from nautilus_trader.model, not from nautilus_trader.core.nautilus_pyo3.' | test py ffi | `testing.md` | Test style > Python tests (`python/tests/`) |
| SHOULD | Escalate test layers on trigger conditions, not by default: Unit (small enumerable case set) -> Parametrized (same shape across discrete inputs such as order side ... | test | `testing.md` | Testing policy > Mechanism ladder |
| SHOULD | 'Apply the rule at module granularity, not crate granularity: an adapter crate contains pure parsers and I/O-bound client loops, and each row applies to a different ... | test arch | `testing.md` | Testing policy > Projection rule |
| SHOULD! | Pure functions, whether or not they have stated invariants (explicitly including 'Codecs, adapter parsers, formatters' and 'Reconciliation kernels, portfolio math') ... | test data exec recon | `testing.md` | Testing policy > Projection rule |
| SHOULD! | Stateful, synchronous modules (examples given: 'Cache, order book'): layers that apply are Unit, parametrized, and property over transitions. | test books | `testing.md` | Testing policy > Projection rule |
| SHOULD | Stateful, async modules (examples: 'Live engine, execution manager'): layers that apply are Unit, integration, deterministic simulation. | test exec | `testing.md` | Testing policy > Projection rule |
| SHOULD | I/O bound, venue-contract modules (example: 'Adapter client loops'): layers that apply are Integration, spec acceptance, boundary fuzz. | test net data exec | `testing.md` | Testing policy > Projection rule |
| SHOULD | 'Prefer a proptest over a hand-written edge-case test when the invariant spans a whole class of inputs. | test | `testing.md` | Testing policy > When not to add coverage |
| SHOULD! | 'Do not duplicate a live spec acceptance card as an integration test. Link to it instead.' | test exec data | `testing.md` | Testing policy > When not to add coverage |
| SHOULD! | 'Do not pad coverage with tests that assert language or framework guarantees (Option::is_some after Some(..), Vec::len after push).' | test | `testing.md` | Testing policy > When not to add coverage |
| SHOULD! | 'Add debug_assert! only where a test can reach it. Release builds strip the check, so an unexercised assertion has no signal.' A targeted unit test counts as a harness; | test rust | `testing.md` | Testing policy > When not to add coverage |
| SHOULD! | 'When waiting for background work to complete, prefer the polling helpers `await eventually(...)` from nautilus_trader.test_kit.functions and `wait_until_async(...)` ... | test py exec net | `testing.md` | Waiting for asynchronous effects |
| CONVENTION! | "Follow this dependency-driven order when building an adapter. Each phase builds on the previous one. Implement the Rust core before any Python layer." | arch rust | `adapters.md` | Adapter implementation sequence |
| CONVENTION | `ExecutionEventEmitter` offers `emit_account_state(balances, margins, reported, ts_event)` (builds `AccountState` from raw parameters via the internal ... | exec | `adapters.md` | Connection lifecycle -> Account state emission |
| CONVENTION! | Data clients emit through an unbounded channel obtained at construction (`get_data_event_sender()`). | data | `adapters.md` | Connection lifecycle -> Data event emission |
| CONVENTION! | "Every Rust module, struct, and public method must have documentation comments. Use third-person declarative voice (e.g., "Returns the account ID" not "Return the ... | docs rust | `adapters.md` | Documentation -> Rust documentation requirements |
| CONVENTION! | Two-layer HTTP architecture: a raw client (`{Venue}RawHttpClient`) with low-level methods matching venue endpoints and venue-specific types, and a domain client ... | net arch | `adapters.md` | HTTP client patterns -> Client structure |
| CONVENTION | Raw client methods mirror venue endpoints (`get_instruments`, `get_balance`, `place_order`) with venue types; | net arch | `adapters.md` | HTTP client patterns -> Method naming and organization |
| CONVENTION! | Use `derive_builder` with `#[builder(setter(into, strip_option), default)]` and `#[serde(skip_serializing_if = "Option::is_none")]` so optional fields are omitted from ... | net rust | `adapters.md` | HTTP client patterns -> Query parameter builders |
| CONVENTION! | Configure rate limiting through `HttpClient` with `LazyLock<Quota>` statics. Naming: REST quotas `{VENUE}_REST_QUOTA`; WebSocket quotas `{VENUE}_WS_{OPERATION}_QUOTA`; | net exec | `adapters.md` | HTTP client patterns -> Rate limiting |
| CONVENTION! | Put request/response representations in `src/http/models.rs` deriving `serde::Deserialize` (plus `Serialize` when sending); | rust net docs | `adapters.md` | Modeling venue payloads -> REST models / WebSocket messages |
| CONVENTION | Provide `LiveDataClientConfig` and `LiveExecClientConfig` subclasses holding adapter settings; key attributes `api_key`, `api_secret`, `base_url`. | py arch | `adapters.md` | Python adapter layer -> Configuration |
| CONVENTION | "When implementing adapter classes, group methods by category in this order: 1. Connection handlers: `_connect`, `_disconnect`; 2. | py arch | `adapters.md` | Python adapter layer -> Method ordering convention |
| CONVENTION! | Config structs derive `bon::Builder` and implement `Default` delegating to the builder so defaults live in exactly one place ("Never duplicate default values in the ... | rust ffi arch | `adapters.md` | Rust adapter patterns -> Configurations -> Builder and Default / Field type rules / Python constructors / Default values |
| CONVENTION! | Mirror the Rust surface through PyO3: register classes with `m.add_class::<T>()` in `python/mod.rs`; prefix Rust methods `py_*` and expose via `#[pyo3(name = "...")]`; | ffi rust | `adapters.md` | Rust adapter patterns -> Python exports / Python bindings |
| CONVENTION! | Provide bidirectional symbol conversion: `format_instrument_id(venue_symbol, product_type)` -> Nautilus `InstrumentId` (appending/transforming product-type suffixes ... | arch prov | `adapters.md` | Rust adapter patterns -> Symbol normalization (`common/symbol.rs`) |
| CONVENTION | Do not fully qualify adapter or Nautilus domain types; import at module level. Only fully qualify `anyhow` and `tokio` types. | rust | `adapters.md` | Rust adapter patterns -> Type qualification / String interning |
| CONVENTION! | Define URL constants and environment-aware resolvers (`get_ws_base_url(testnet)`) in `common/urls.rs`; | net arch | `adapters.md` | Rust adapter patterns -> URL resolution / Configurations (`config.rs`) |
| CONVENTION! | The Python layer provides five components: Instrument Provider (`InstrumentProvider`), Data Client (`LiveDataClient`/`LiveMarketDataClient`), Execution Client ... | arch py prov data exec | `adapters.md` | Structure of an adapter -> Python layer (`nautilus_trader/adapters/your_adapter`) |
| CONVENTION! | In-tree adapters use a layered architecture: a Rust core (`crates/adapters/<name>/`) for HTTP client, WebSocket client, parsing and PyO3 bindings, with a prescribed file ... | arch rust | `adapters.md` | Structure of an adapter -> Rust core (`crates/adapters/your_adapter/`) |
| CONVENTION! | Layout `tests/integration_tests/adapters/your_adapter/` with `conftest.py`, `test_data.py`, `test_execution.py`, `test_providers.py`, `test_factories.py`, `__init__.py`. | test py | `adapters.md` | Testing -> Python testing |
| CONVENTION! | "WebSocket channels on latency-sensitive paths are intentionally **unbounded**. The platform prioritizes latency and prefers an explicit crash (OOM) over delaying or ... | net arch | `adapters.md` | WebSocket client patterns -> Backpressure strategy |
| CONVENTION! | Two-layer WS architecture. Outer client `{Venue}WebSocketClient` orchestrates lifecycle/auth/subscriptions, keeps Python-visible state in `Arc<DashMap<K,V>>`, tracks ... | net rust | `adapters.md` | WebSocket client patterns -> Client structure / Connection state tracking |
| CONVENTION | "Types prefixed with the venue name (e.g., `OKX`, `Bitmex`) contain raw exchange-specific types. | arch rust | `adapters.md` | WebSocket client patterns -> Message routing -> Message type naming convention |
| CONVENTION | Channel names reflect the transformation stage, not the destination: `raw_*` for raw WebSocket frames (`Message`), `out_*` for venue-specific message types. | rust net | `adapters.md` | WebSocket client patterns -> Naming conventions -> Channel naming / Field naming / Type naming |
| CONVENTION! | When a venue exposes multiple WS endpoints with distinct protocols or encodings, split `websocket/` into `streams/` (market data pub/sub) and `trading/` (order ... | net arch exec | `adapters.md` | WebSocket client patterns -> Split WebSocket architectures / When to split |
| CONVENTION | The outer client exposes `stream()` handing ownership of `out_rx` to the caller as an async stream, consumed once by the data/exec client in a `tokio::select!` loop with ... | net | `adapters.md` | WebSocket client patterns -> Stream consumption (`stream`) / Subscription topic helpers / Handler configuration constants |
| CONVENTION! | Venue-specific topic delimiters: BitMEX `:` (`trade:XBTUSD`), OKX `:` (`trades:BTC-USDT-SWAP`), Bybit `.` (`orderbook.50.BTCUSDT`). | net | `adapters.md` | WebSocket client patterns -> Subscription management -> Topic format patterns |
| CONVENTION! | Each crate keeps benchmarks in a local `benches/` folder as `crates/<crate_name>/benches/foo_criterion.rs` and `foo_iai.rs`. | bench rust | `benchmarking.md` | Directory layout |
| CONVENTION | Commands: all benches in one crate `cargo bench -p nautilus-execution`; one module `cargo bench -p nautilus-execution --bench matching_core`; | bench rust | `benchmarking.md` | Running benches locally |
| CONVENTION | Leave one blank line above every comment block or docstring so it is visually separated from code. | py rust | `coding_standards.md` | Comment conventions |
| CONVENTION | Use sentence case in comments - capitalize the first letter, keep the rest lowercase unless proper nouns or acronyms. Do not use double spaces after periods. | py rust | `coding_standards.md` | Comment conventions |
| CONVENTION | Single-line comments must NOT end with a period, unless the line ends with a URL or inline Markdown link, in which case punctuation is left exactly as the link requires. | py rust | `coding_standards.md` | Comment conventions |
| CONVENTION | Multi-line comments separate sentences with commas (not period-per-line); the final line should end with a period. | py rust | `coding_standards.md` | Comment conventions |
| CONVENTION | Keep comments concise; explain only the non-obvious - less is more. Avoid emoji symbols in text. | py rust docs | `coding_standards.md` | Comment conventions |
| CONVENTION | Limit subject titles to 60 characters or fewer; capitalize the subject line; do not end it with a period. | pkg | `coding_standards.md` | Commit messages |
| CONVENTION | Rust doc comments are written in the INDICATIVE mood - "Returns a cached client." This is the deliberate counterpart to python.md's imperative-mood rule for Python ... | rust docs | `coding_standards.md` | Doc comment mood |
| CONVENTION | For long lines and calls with more than a couple of arguments, break to a new line aligned at the next logical indent, rather than hanging 'vanity' alignment off the ... | py rust | `coding_standards.md` | Formatting |
| CONVENTION | The closing parenthesis goes on its own new line, aligned at the logical indent. | py rust | `coding_standards.md` | Formatting |
| CONVENTION | Multiple hanging parameters or arguments end with a trailing comma. | py rust | `coding_standards.md` | Formatting |
| CONVENTION | Abbreviations are acceptable for private/internal fields (e.g. _price_prec, _size_prec) to keep hot-path code concise. | py rust | `coding_standards.md` | Naming conventions |
| CONVENTION! | User-facing API uses full, descriptive names for public properties, function parameters, return types, AND metric names/labels (price_precision, size_precision). | py arch exec data prov | `coding_standards.md` | Naming conventions |
| CONVENTION | Error messages and logs use full words for clarity ("price precision", not "price prec"). "The user should never see abbreviated terminology." | py docs | `coding_standards.md` | Naming conventions |
| CONVENTION | Shell scripts use bash (not POSIX sh) and must be portable across Linux and macOS. User-facing scripts (e.g. | pkg test | `coding_standards.md` | Shell script portability |
| CONVENTION | Shebang: always use #!/usr/bin/env bash for portability. | pkg | `coding_standards.md` | Shell script portability |
| CONVENTION! | GNU vs BSD utility differences must be handled: sed -i needs a backup extension (sed -i.bak); stat -c%s vs stat -f%z must be detected; | pkg test | `coding_standards.md` | Shell script portability |
| CONVENTION! | macOS ships bash 3.2; avoid bash 4+ features in user-facing scripts: associative arrays (declare -A), readarray/mapfile, ${var,,}/${var^^} case conversion. | pkg test | `coding_standards.md` | Shell script portability |
| CONVENTION | Error messages: avoid ", got". Use ", was", ", received" or ", found". Example given: "Expected string, was {type(value)}" not "Expected string, got {type(value)}". | py rust | `coding_standards.md` | Terminology and phrasing |
| CONVENTION | Spell "hardcoded" as a single word, not "hard-coded" or "hard coded". | py docs | `coding_standards.md` | Terminology and phrasing |
| CONVENTION | Use single-letter `e` for caught errors/exceptions: Rust Err(e) and \|e\| (not err/error); Python `except SomeError as e:` (not `as err:` / `as error:`). | py rust | `coding_standards.md` | Terminology and phrasing |
| CONVENTION | Applies to ALL source files (Rust, Python, Cython, shell, etc.): use spaces only, never hard tab characters. | py rust pkg | `coding_standards.md` | Universal formatting rules |
| CONVENTION | "Lines should generally stay below 100 characters; wrap thoughtfully when necessary." | py rust | `coding_standards.md` | Universal formatting rules |
| CONVENTION | Prefer American English spelling (color, serialize, behavior). | py docs | `coding_standards.md` | Universal formatting rules |
| CONVENTION! | Document parameters and return types clearly; include usage examples for complex APIs; explain any side effects or important behavior; | docs py | `docs.md` | API documentation |
| CONVENTION | Five admonition kinds with defined purposes: :::note (supplementary context, not essential), :::info (important information to be aware of), :::tip (helpful ... | docs | `docs.md` | Admonitions |
| CONVENTION! | Four selection questions (learning walkthrough -> tutorial; "How do I...?" for someone who knows the system -> how-to; why it works this way -> explanation; | docs | `docs.md` | Choosing the right type |
| CONVENTION! | Use backticks for inline code, method names, class names and configuration options; code blocks for multi-line examples. | docs | `docs.md` | Code references |
| CONVENTION | Markdown tables use symmetrical column widths based on the widest content in each column, with column separators (\|) aligned vertically and consistent spacing around ... | docs | `docs.md` | Column alignment and spacing |
| CONVENTION | Most pages fit one of four Divio types, each with its own section: Tutorial (tutorials/), How-to guide (how_to/), Explanation (concepts/), Reference (api_reference/). | docs | `docs.md` | Documentation types |
| CONVENTION | Explicit exception directly relevant to a venue adapter: "`integrations/` pages mix reference (capabilities, symbology) with how-to content (setup, configuration) so ... | docs arch | `docs.md` | Documentation types |
| CONVENTION! | Provide practical, working examples; include necessary imports and context; use realistic variable names and values; add comments to explain non-obvious parts. | docs | `docs.md` | Examples and code samples |
| CONVENTION | Favor simplicity over complexity, less is more; concise yet readable prose; standardization in conventions, style and patterns; | docs | `docs.md` | General principles |
| CONVENTION | Title case for the main page heading (# level 1 only); sentence case for ALL subheadings (## level 2 and below); | docs | `docs.md` | Headings |
| CONVENTION! | Use active voice ("Configure the adapter", not "The adapter should be configured"); present tense for current functionality; future tense ONLY for planned features; | docs | `docs.md` | Language and tone |
| CONVENTION | Wrap lines at no more than ~100-120 characters for readability and diff reviews; break long sentences at natural points (after commas, conjunctions, phrases); | docs | `docs.md` | Line length and wrapping |
| CONVENTION | Use descriptive link text (avoid "click here" or "this link"); reference external documentation where appropriate; keep all internal links relative and accurate. | docs | `docs.md` | Links and references |
| CONVENTION | Use hyphens (-) for unordered list bullets; avoid * or + to keep Markdown style consistent. Numbered lists only when order matters. | docs | `docs.md` | Lists |
| CONVENTION | All notes and descriptions have terminating periods; keep notes concise but informative; use sentence case (capitalize only the first letter and proper nouns). | docs | `docs.md` | Notes and descriptions |
| CONVENTION | Use ✓ for supported features and - for unsupported features, explicitly "(not ✗ or other symbols)". | docs | `docs.md` | Support indicators |
| CONVENTION! | "Base capability matrices on the Nautilus domain model, not exchange-specific terminology." Mention exchange-specific terms in parentheses or notes when necessary for ... | docs arch exec data | `docs.md` | Technical terminology |
| CONVENTION! | Following any change to `.rs`, `.pyx` or `.pxd` files, recompile with `uv run --no-sync python build.py` or `make build`; | rust pkg | `environment_setup.md` | Builds |
| CONVENTION! | Cap'n Proto is required for serialization schema compilation; the required version is defined in `tools.toml`. | rust pkg | `environment_setup.md` | Cap'n Proto |
| CONVENTION | `[tool.uv]` in `pyproject.toml` enforces `required-version = "==0.11.14"` so 'all developers and CI use the same uv version'. | pkg | `environment_setup.md` | Dependency management |
| CONVENTION! | To change the pinned uv version, edit `required-version` in BOTH `pyproject.toml` and `python/pyproject.toml`, then update the `rev` in `.pre-commit-config.yaml` to ... | pkg | `environment_setup.md` | Dependency management > Updating uv |
| CONVENTION | Install with development and test dependencies: `uv sync --active --all-groups --all-extras` or `make install`. | pkg | `environment_setup.md` | Setup > 1. Install dependencies |
| CONVENTION | 'NautilusTrader pins every development tool so that all contributors and CI run identical versions.' `make install-tools` installs: cargo CLIs pinned in `Cargo.toml` ... | pkg test | `environment_setup.md` | Setup > 2. Install development tools |
| CONVENTION | `make install-tools` uses cargo-binstall to fetch `prek` as a prebuilt binary rather than compiling from source; | pkg rust | `environment_setup.md` | Setup > 2. Install development tools > One-off prerequisite: cargo-binstall |
| CONVENTION | Tool versions live in exactly two files: `Cargo.toml` `[workspace.metadata.tools]` for cargo-installable crates, `tools.toml` for everything else (`prek`, `osv-scanner` ... | pkg | `environment_setup.md` | Setup > 2. Install development tools > Single source of truth for versions |
| CONVENTION | Compact one-time bootstrap for a new Linux/macOS machine, in order: install platform tools (`build-essential clang lld curl git make pkg-config` on Ubuntu, `xcode-select ... | pkg rust | `environment_setup.md` | Setup > Quick setup |
| CONVENTION | Python docstrings are written in the IMPERATIVE mood - "Return a cached client." This is deliberately the opposite of the indicative mood coding_standards.md requires ... | py docs | `python.md` | Docstrings |
| CONVENTION | Use truthiness to check for EMPTY COLLECTIONS (e.g. `if not my_list:`) rather than comparing explicitly to None or to empty. | py | `python.md` | PEP-8 |
| CONVENTION | Do not add docstrings to private methods (prefixed with _), because docstrings generate public-facing API documentation and incorrectly imply the method is part of the ... | py docs | `python.md` | Private methods |
| CONVENTION! | When exposing Rust types to Python via PyO3, choose #[getter] vs a plain method "based on what the call site communicates, not whether the value can change". | ffi rust py | `python.md` | Properties vs methods (PyO3 bindings) |
| CONVENTION | ruff is used to lint the codebase; "Ruff rules can be found in the top-level pyproject.toml, with ignore justifications typically commented." - i.e. | py pkg | `python.md` | Ruff |
| CONVENTION | Use descriptive test names explaining the scenario, e.g. test_currency_with_negative_precision_raises_overflow_error, test_sma_with_no_inputs_returns_zero_count ... | test py | `python.md` | Test naming |
| CONVENTION | Use TypeVar for reusable generic components, e.g. `T = TypeVar("T")` / `class ThrottledEnqueuer(Generic[T]):`. | py | `python.md` | Type hints |
| CONVENTION | 'Credit external contributors: `thanks @username` or `thanks for reporting @username`.' 'Include issue/PR numbers for community contributions and complex features ... | docs pkg | `releases.md` | Attribution |
| CONVENTION | The `publish-cargo-crates` job uses crates.io Trusted Publishing through GitHub Actions OIDC and no persistent cargo token. | pkg rust | `releases.md` | Crates.io publishing |
| CONVENTION! | 'Post-publish verification treats an existing crate version as `previously_published` only when crates.io shows it was trusted-published by this repository. | pkg | `releases.md` | Crates.io publishing |
| CONVENTION! | Pre-release on `develop`: finalize `RELEASES.md` (review all items, remove empty sections); ensure versions set in `pyproject.toml` and workspace `Cargo.toml`; | pkg docs | `releases.md` | Release checklist |
| CONVENTION | Per-section wording: Enhancements start with 'Added', use backticks for code elements, 'Be specific about what was added, not how'. | docs pkg | `releases.md` | Release notes > Enhancements / Breaking Changes / Fixes / Internal Improvements / Documentation Updates / Deprecations |
| CONVENTION | Use exactly these sections in this order: 1. Enhancements, 2. Breaking Changes, 3. Security, 4. Fixes, 5. Internal Improvements, 6. Documentation Updates, 7. | docs pkg | `releases.md` | Release notes > Sections |
| CONVENTION | Security covers 'Security hardening and fixes that prevent crashes, undefined behavior, or data corruption. | docs pkg | `releases.md` | Release notes > Security |
| CONVENTION | Next-version template to place at the top of `RELEASES.md`: heading `# NautilusTrader <VERSION> Beta`, then `Released on TBD (UTC).`, then the seven `###` sections in ... | docs pkg | `releases.md` | Release notes template |
| CONVENTION! | Include in Security if the change addresses: memory safety (overflow, underflow, divide-by-zero that threatens stability); | docs pkg | `releases.md` | Security classification |
| CONVENTION | 'Use sentence case (capitalize first word only).' 'Do not end with periods.' 'Use backticks for code elements.' 'Focus on **what** changed, not how.' Be specific - the ... | docs | `releases.md` | Style |
| CONVENTION | Import `get_runtime` from the `live` module re-export (`use nautilus_common::live::get_runtime;`), not the full path `nautilus_common::live::runtime::get_runtime`. | rust arch | `rust.md` | Adapter runtime patterns |
| CONVENTION | Consistent attribute usage and ordering: `#[repr(C)]` first, then `#[derive(...)]`, then `#[cfg_attr(feature = "python", pyo3::pyclass(...))]`, then `#[cfg_attr(feature ... | rust ffi | `rust.md` | Attribute patterns |
| CONVENTION | Do not use box-style banner or separator comments (e.g. `// ====== Some Section ======`, `// ========== Test Fixtures ==========`). | rust docs | `rust.md` | Box-style banner comments |
| CONVENTION | Align cargo features, profiles and flags across build targets because cargo's cache is keyed by their exact combination. | rust test | `rust.md` | Build configurations / Aligned targets (testing and linting) |
| CONVENTION | In `[dependencies]`, list internal `nautilus-*` crates first alphabetically, blank line, then external required deps alphabetically, blank line, then `optional = true` ... | rust pkg | `rust.md` | Cargo manifest conventions |
| CONVENTION! | Add `"python"` to every `extension-module` feature list that builds a Python artefact, keeping it adjacent to `"pyo3/extension-module"`. | rust ffi | `rust.md` | Cargo manifest conventions |
| CONVENTION | Import formatting is handled by rustfmt via `make format` (groups std / external / local, alphabetical within group). | rust | `rust.md` | Code formatting |
| CONVENTION | Use SCREAMING_SNAKE_CASE for constants with descriptive names and doc comments (e.g. `NANOSECONDS_IN_SECOND`, `BAR_SPEC_1_MINUTE_LAST`). | rust | `rust.md` | Constants and naming conventions |
| CONVENTION | Use the `new()` vs `new_checked()` convention consistently: `new_checked` returns `CorrectnessResult<Self>` and documents `# Errors`; | rust ffi | `rust.md` | Constructor patterns |
| CONVENTION | Style: prefix `debug_assert!` messages with `Invariant:` and state the positive rule, not the failure (`debug_assert!(next > last, "Invariant: time is strictly monotonic ... | rust | `rust.md` | Design by contract |
| CONVENTION | Rust docs are built separately by `make docs-rust`, which runs `cargo +nightly doc --all-features --no-deps --workspace`. | rust docs | `rust.md` | Documentation builds |
| CONVENTION | Use third-person declarative voice for all doc comments (e.g. "Returns the account ID", not "Return the account ID"). | rust docs | `rust.md` | Documentation standards |
| CONVENTION | Single-line errors and panics docs use sentence case ("Returns an error if the currency conversion fails.", "Panics if `currency` is `None` and `self.base_currency` is ... | rust docs | `rust.md` | Documentation standards / Errors and panics documentation format |
| CONVENTION | Rustdoc section headers use Title Case matching the Rust standard library convention: `# Examples`, `# Errors`, `# Panics`, `# Safety`, `# Notes`, `# Thread Safety`, `# ... | rust docs | `rust.md` | Documentation standards / Section header casing |
| CONVENTION | Use lowercase for `.context()` messages so error chaining reads naturally (`.context("failed to parse timestamp")`), except proper nouns/acronyms which stay capitalized ... | rust | `rust.md` | Error handling |
| CONVENTION | Fully qualify logging macros so the backend is explicit: use `log::…` (`log::debug!`, `log::info!`, `log::warn!`) for all Rust components. | rust docs | `rust.md` | Logging |
| CONVENTION | Keep modules focused on a single responsibility; use `mod.rs` as the module root when defining submodules; prefer relatively flat hierarchies over deep nesting; | rust arch | `rust.md` | Module organization |
| CONVENTION | Use `rstest` with `#[case(...)]` attributes and `#[case]` parameters for parameterized tests. | rust test | `rust.md` | Parameterized testing |
| CONVENTION | Use the `proptest` crate for property-based tests, in a separate `property_tests` module (not inside `mod tests`) to keep deterministic unit tests separate from ... | rust test | `rust.md` | Property-based testing |
| CONVENTION | Organize re-exports alphabetically and place them at the end of `lib.rs` files, separating crate-root re-exports from module-level re-exports. | rust | `rust.md` | Re-export patterns |
| CONVENTION | The project pins a specific Rust version via `rust-toolchain.toml`. Keep the toolchain synchronized with CI (`rustup update`, `rustup show`). | rust pkg | `rust.md` | Rust version management |
| CONVENTION | Schema files live in `crates/serialization/schemas/capnp/` split into `common/` (base types, identifiers, enums), `commands/`, `events/`, `data/`. | rust test | `rust.md` | Schema development workflow |
| CONVENTION | Prefer inline format strings with variable names (`anyhow::bail!("Failed to subtract {n} months from {datetime}")`) over positional arguments. | rust | `rust.md` | String formatting |
| CONVENTION | Use descriptive test names that explain the scenario (`test_sma_with_no_inputs`, `test_sma_with_single_input`, `test_symbol_is_composite`). | rust test | `rust.md` | Test naming |
| CONVENTION | Spec anatomy: derive `bon::Builder` with `finish_fn = into_spec` so the generated finish method does not collide with the custom `build()`; | rust test | `rust.md` | Test specs (bon builders) |
| CONVENTION | Use `mod tests` as the standard test module name unless compartmentalization is specifically needed; | rust test | `rust.md` | Testing conventions |
| CONVENTION | The project uses rustfmt (`rustfmt.toml`) for formatting, clippy (`clippy.toml`) for linting, and cbindgen for C header generation. | rust | `rust.md` | Tooling configuration |
| CONVENTION! | Always fully qualify `anyhow` macros (`anyhow::bail!`, `anyhow::anyhow!`) and `anyhow::Result<T>`. | rust | `rust.md` | Type qualification |
| CONVENTION | Placement: on structs/enums use `#[cfg_attr(feature = "python", ...)]` and place the stub annotation directly below `pyo3::pyclass`; | ffi rust | `rust.md` | Type stub annotations |
| CONVENTION | In Cargo.toml add `pyo3-stub-gen` as an optional dependency and include it in the `python` feature list (`python = ["pyo3", "pyo3-stub-gen"]`). | ffi rust pkg | `rust.md` | Type stub annotations |
| CONVENTION | Use workspace inheritance for shared dependencies (`serde = { workspace = true }`); pin versions directly only for crate-specific deps outside the workspace; | rust pkg | `rust.md` | Versioning guidance |
| CONVENTION! | "Document adapter-specific data behavior (custom channels, throttling, snapshot semantics, etc.) in the adapter's own guide, not here." This is a documentation ... | docs data | `spec_data_testing.md` | Data Testing Spec (intro) |
| CONVENTION | API credentials are supplied via environment variables `{VENUE}_API_KEY` / `{VENUE}_API_SECRET` when the venue requires authentication for the data being tested. | data net test | `spec_data_testing.md` | Prerequisites |
| CONVENTION! | Rust node setup reference lives at `crates/adapters/{adapter}/examples/node_data_tester.rs`; | rust test | `spec_data_testing.md` | Prerequisites |
| CONVENTION | "Each group below begins with a summary table, followed by detailed test cards. Test IDs use spaced numbering to allow insertion without renumbering." Any adapter-side ... | docs test | `spec_data_testing.md` | Prerequisites |
| CONVENTION | "Test IDs use spaced numbering to allow insertion without renumbering." The ID range is NOT contiguous - 64 cases exist between TC-E01 and TC-E101. | test docs | `spec_exec_testing.md` | Execution Testing Spec (preamble) |
| CONVENTION | Environment variables `{VENUE}_API_KEY`, `{VENUE}_API_SECRET` (or sandbox variants). | net exec | `spec_exec_testing.md` | Prerequisites |
| CONVENTION | Seven-step procedure: (1) curate per the workflow; (2) write metadata.json with all required fields; (3) small data: commit to tests/test_data/<source>/; | fixt test | `test_datasets.md` | Adding a new dataset |
| CONVENTION | Preferred user-fetched layout: `tests/test_data/<source>/<slug>/` containing metadata.json, manifest.json, README.md. | fixt | `test_datasets.md` | Adding a new dataset |
| CONVENTION | In-tree Rust workflow for datasets needing format conversion: (1) write a curation function in crates/testkit/src/<source>/ gated behind #[cfg(test)] or an #[ignore] ... | fixt rust | `test_datasets.md` | Curation workflow > Complex pipelines (parse + transform) |
| CONVENTION | 'Use scripts/curate-dataset.sh <slug> <filename> <download-url> <licence>. This creates a versioned directory (v1/<slug>/) with the file, LICENSE.txt, and metadata.json ... | fixt | `test_datasets.md` | Curation workflow > Simple files (single download) |
| CONVENTION | Small data (< 1 MB) 'is checked directly into tests/test_data/<source>/ alongside a metadata.json file. | fixt test | `test_datasets.md` | Dataset categories |
| CONVENTION! | Large data (> 1 MB) 'is hosted as Parquet in the R2 test-data bucket. A SHA-256 checksum is recorded in tests/test_data/large/checksums.json. | fixt test | `test_datasets.md` | Dataset categories |
| CONVENTION | `<source>_<instrument>_<date>_<datatype>.parquet`. Examples given: `itch_AAPL_2019-01-30_deltas.parquet`, `tardis_BTCUSDT_2020-09-01_depth10.parquet` ... | fixt | `test_datasets.md` | Naming convention |
| CONVENTION | Explicit carve-out: 'For user-fetched datasets without a single committed or mirrored artifact, file, sha256, and size_bytes may be omitted from metadata.json. | fixt | `test_datasets.md` | Required metadata |
| CONVENTION | 'Use ZSTD compression (level 3) with 1M row groups.' | fixt | `test_datasets.md` | Storage format |
| CONVENTION! | 'Tests that download large data files share target paths across test binaries. Because nextest runs each binary in a separate process, concurrent downloads to the same ... | fixt test rust | `test_datasets.md` | Test runner serialization |
| CONVENTION | 'The NAUTILUS_DATA_DIR environment variable overrides the base data path used by these tutorials. | fixt test docs | `test_datasets.md` | Tutorial test data |
| CONVENTION! | In-tree procedure for a new data type across six layers; does not bind an external adapter that only consumes existing types. | test rust docs | `testing.md` | Data type testing > Adding a new data type |
| CONVENTION | 'Run tests with pytest, our primary test runner. Use parametrized tests and fixtures (e.g., @pytest.mark.parametrize) to avoid repetitive code and improve clarity.' ... | test py | `testing.md` | Fuzzing |
| CONVENTION | 'For new live adapter examples and docs in the v2 path, prefer nautilus_trader.live.LiveNode. | test arch docs | `testing.md` | Running tests > Python tests |
| CONVENTION | In-tree build and invocation mechanics only; does not bind an external distribution. v1 legacy Cython suite at repo-root tests/ via `make pytest`; | test pkg | `testing.md` | Running tests > v1 legacy Python tests / Python tests / Rust tests |
| CONVENTION | 'Name test functions after what they exercise; you do not need to encode the expected assertions in the name.' Add docstrings when they clarify setup, scenarios, or ... | test py | `testing.md` | Test style > General |
| CONVENTION | 'Use unwrap, expect, or direct panic!/assert calls inside tests; clarity and conciseness matter more than defensive error handling here.' | test rust | `testing.md` | Test style > General |
| CONVENTION! | 'Use pytest-style free functions and fixtures. Do not use test classes.' Write each test as a standalone `def test_*()` function. | test py | `testing.md` | Test style > Python tests (`python/tests/`) |
| CONVENTION! | 'Test providers live in python/tests/providers.py. Use TestInstrumentProvider and TestDataProvider for common instruments and data.' 'Mark tests that depend on ... | test py fixt | `testing.md` | Test style > Python tests (`python/tests/`) |
| CONVENTION | 'The v1 legacy test suite uses a mix of test classes and free functions. New tests added to this suite may follow either pattern, but free functions with fixtures are ... | test py | `testing.md` | Test style > v1 legacy Python tests (`tests/`) |
| CONVENTION! | In-tree Rust only, no analogue in a pure-Python external package. Before promoting a module to deterministic simulation testing: time/task/runtime/signal primitives ... | rust test | `testing.md` | Testing policy > DST readiness |
| CONVENTION! | 'Replay-sensitive IDs (trade_id, venue_order_id) are pure functions of their inputs; see crates/execution/src/reconciliation/ids.rs. | rust recon test | `testing.md` | Testing policy > DST readiness |
| OPTIONAL! | Complex adapters may centralize venue->Nautilus conversion in `factories.rs` (e.g. `create_instrument()` dispatching on instrument type). | arch | `adapters.md` | Rust adapter patterns -> Factory module (`factories.rs`) |
| OPTIONAL | `cargo-flamegraph` produces a sampled call-stack profile for one bench, 'Useful when a bench shows a regression but it's not obvious which inner call is responsible.' ... | bench rust | `benchmarking.md` | Generating a flamegraph |
| OPTIONAL! | `perf` must be available: `sudo apt install linux-tools-common linux-tools-$(uname -r)` on Debian/Ubuntu. | bench | `benchmarking.md` | Generating a flamegraph > Linux |
| OPTIONAL | 'DTrace requires root, so `cargo flamegraph` must be run with `sudo`': `sudo cargo flamegraph --bench matching -p nautilus-common --profile bench`. | bench | `benchmarking.md` | Generating a flamegraph > macOS |
| OPTIONAL | Ready-to-copy starter files live in `docs/dev_templates/`: `criterion_template.rs` and `iai_template.rs`. | bench rust | `benchmarking.md` | Templates |
| OPTIONAL | Optionally use a body to explain the change, separated from the subject by a blank line, kept under 100 character width, with bullet points with or without terminating ... | pkg | `coding_standards.md` | Commit messages |
| OPTIONAL | Gitlint is available to enforce commit-message standards automatically but is explicitly "opt-in and not enforced in CI". | pkg | `coding_standards.md` | Gitlint (optional) |
| OPTIONAL | For urgent security or bug fixes, override `exclude-newer` on the command line: `uv lock --exclude-newer-package "somepackage=1 day"` ... | pkg | `environment_setup.md` | Dependency management > Bypassing the cooldown |
| OPTIONAL! | The cranelift codegen backend significantly reduces dev/test/IDE-check build time but needs the nightly toolchain (`rustup install nightly`, `rustup override set ... | rust | `environment_setup.md` | Faster builds |
| OPTIONAL! | The Nautilus CLI manages the PostgreSQL database and trading operations; install with `make install-cli` (wraps `cargo install`). | test | `environment_setup.md` | Nautilus CLI developer guide / Introduction / Install / Commands / Database |
| OPTIONAL | Configure rust-analyzer with the same environment as `make build-debug` for faster compiles and IDE checks. | rust | `environment_setup.md` | Rust analyzer settings |
| OPTIONAL! | `.docker/docker-compose.yml` bootstraps the working environment via `docker-compose up -d` (or per-service, e.g. `docker-compose up -d postgres`). | test pkg | `environment_setup.md` | Services |
| OPTIONAL | Two stated exceptions where a docstring on a private method is acceptable: very complex methods with non-trivial logic, multiple steps or important edge cases; | py | `python.md` | Private methods |
| OPTIONAL | Reference reading for unsafe work: The Rustonomicon; The Rust Reference – Unsafety; Safe Bindings in Rust (Russell Johnston); Google – Rust and C interoperability. | rust ffi | `rust.md` | Resources |
| OPTIONAL | Recommended manifest fields: `slug` (stable dataset identifier), `vendor`, `source_type` (api / portal-download / purchased-archive), `source_filters` (symbols, event ... | fixt | `test_datasets.md` | Adding a new dataset |
| OPTIONAL | 'You may maintain a private mirror for internal CI or employees when the license permits internal sharing. | fixt | `test_datasets.md` | Curation workflow > User-fetched pipelines (restricted redistribution) |
| OPTIONAL | Recommended provenance fields beyond the required set: `instrument` (symbols covered), `date` (trading dates covered), `format` (e.g. | fixt | `test_datasets.md` | Required metadata |
| OPTIONAL | Developer tooling only. `make cargo-test-debug` for debug symbols; IntelliJ rstest run-configuration workaround (`::case_n`, n starts at 1); | test rust ffi | `testing.md` | Debugging Rust tests / Python + Rust Mixed Debugging |
| CONTEXT | "Instruments are the foundation: both data and execution clients depend on them." Milestone: `InstrumentProvider.load_all_async()` returns valid Nautilus instruments. | prov arch | `adapters.md` | Adapter implementation sequence -> Phase 2: Instrument definitions |
| CONTEXT | "See the full [Data Testing Spec](spec_data_testing.md) for the `DataTester` test matrix." "See the full [Execution Testing Spec](spec_exec_testing.md) for the ... | test | `adapters.md` | Data testing spec / Execution testing spec |
| CONTEXT | Adapters connect to trading venues/data providers, translating native APIs into the platform's unified interface and normalized domain model. | arch | `adapters.md` | Introduction |
| CONTEXT! | 'This document is the practitioner reference for writing and running NautilusTrader benchmarks. | bench docs | `benchmarking.md` | Benchmarking (preamble) |
| CONTEXT! | 'Benchmark binaries are compiled with the custom `[profile.bench]` defined in the workspace `Cargo.toml`. | bench rust ffi | `benchmarking.md` | Generating a flamegraph (closing note) |
| CONTEXT | CI scripts (scripts/ci/*) run on Linux runners, so bash 4+ and GNU tools are acceptable there. | test pkg | `coding_standards.md` | Shell script portability |
| CONTEXT! | "See [Message Bus: message integrity](../concepts/message_bus.md#message-integrity) for the ownership rules that follow from this." The normative ownership rules are ... | arch py | `design_principles.md` | Message immutability |
| CONTEXT | Eight properties the invariant protects: determinism, temporal integrity, safer concurrency (readers need no coordination against later rewrites), easier debugging ... | arch test | `design_principles.md` | Message immutability |
| CONTEXT! | The docs site (fumadocs) provides built-in MDX components in all .md files with no imports needed: Tabs/Tab, Steps/Step, Accordions/Accordion, Files/Folder/File ... | docs pkg | `docs.md` | MDX components |
| CONTEXT | PyCharm Professional is recommended because it interprets Cython syntax; alternatively VS Code with a Cython extension. | pkg py rust | `environment_setup.md` | Environment Setup (preamble) |
| CONTEXT | NautilusTrader exposes C-compatible types so compiled Rust can be consumed from Cython-generated C extensions or other native languages. | ffi rust | `ffi.md` | FFI Memory Contract (preamble) |
| CONTEXT | The developer guide comprises 14 linked documents plus this index: environment_setup, design_principles, coding_standards, rust, python, testing, test_datasets, docs ... | docs | `index.md` | Contents |
| CONTEXT! | NautilusTrader uses a Rust core with Python bindings architecture: Rust handles networking, data parsing, order matching and other performance-critical operations; | arch rust ffi py | `index.md` | Developer Guide (preamble) |
| CONTEXT! | Three-branch model: `develop` = active development, 'publishes dev wheels to Cloudflare R2 on every push'; | pkg test | `releases.md` | Overview |
| CONTEXT! | The project maintains two independent version numbers: `pyproject.toml` scopes the Python package (example `1.223.0`) and the workspace `Cargo.toml` scopes the Rust ... | pkg arch | `releases.md` | Versioning |
| CONTEXT | Rust is chosen for the mission-critical core: strong type system, ownership model, compile-time checks eliminate memory errors and data races by construction; | rust arch | `rust.md` | # Rust |
| CONTEXT | Tests are exempt from the spawn rule: `#[tokio::test]` creates its own runtime context so `tokio::spawn()` works correctly, and the enforcement hook skips test files and ... | rust test | `rust.md` | Adapter runtime patterns |
| CONTEXT | The `nautilus-serialization` crate provides optional Cap'n Proto serialization for efficient data interchange. | rust | `rust.md` | Cap'n Proto serialization |
| CONTEXT | The codebase uses unsafe in three categories: FFI boundaries (raw pointer operations for C interop, see ffi.md); | rust ffi | `rust.md` | Categories of unsafe code |
| CONTEXT | Design by contract states obligations between a function and its callers: preconditions (what the function requires), postconditions (what it guarantees), invariants ... | rust arch | `rust.md` | Design by contract |
| CONTEXT | Generated Rust files are checked into `crates/serialization/generated/capnp/` for docs.rs compatibility (the docs build environment lacks the capnp compiler) ... | rust pkg | `rust.md` | Generated code |
| CONTEXT | Three concerns drive hash-collection choice — iteration-order determinism (the primary filter), performance, thread safety. | rust | `rust.md` | Hash collections |
| CONTEXT | Measured trade-offs from `crates/core/benches/hash_map.rs`: `AHashMap` is roughly 3x faster on pure lookup; | rust bench | `rust.md` | Hash collections / AHashMap vs IndexMap microbenchmarks |
| CONTEXT | Python bindings are provided via PyO3, allowing users to import NautilusTrader crates directly in Python without a Rust toolchain. | ffi py | `rust.md` | Python bindings |
| CONTEXT | Both registries store `Rc<UnsafeCell<dyn Trait>>` in thread-local maps but differ: the actor registry allows aliasing (multiple guards), permits re-entrant access ... | rust | `rust.md` | Runtime invariants / Actor registry vs component registry |
| CONTEXT | `clone_py_object()` acquires the Python GIL before cloning, uses Python's native reference counting via `clone_ref()`, avoids Rust `Arc` wrappers that interfere with ... | ffi | `rust.md` | Rust-Python memory management / Why this works |
| CONTEXT | `build` (release) and `build-debug` (dev) targets include `extension-module` plus a feature subset and will trigger rebuilds; this is expected and unavoidable. | rust ffi | `rust.md` | Separate target (Python extension building) / Rebuild triggers to avoid |
| CONTEXT | Orders in `Cache` use `AHashMap<ClientOrderId, SharedCell<OrderAny>>` internally; the smart-pointer leak stays internal. | rust exec recon | `rust.md` | Shared mutability storage |
| CONTEXT | Costs of `Rc<RefCell<T>>` to weigh before adopting: every access pays a runtime borrow check; the smart-pointer type leaks at write boundaries; | rust | `rust.md` | Shared mutability storage |
| CONTEXT | `unsafe` Rust is necessary for Cython/Rust interoperation. Using it shifts responsibility for guaranteeing correctness from the compiler onto the developer; | rust ffi | `rust.md` | Unsafe Rust |
| CONTEXT | The `DataTester` exists in BOTH Python (`nautilus_trader.test_kit.strategies.tester_data`) and Rust (`nautilus_testkit::testers`). | test data py | `spec_data_testing.md` | Data Testing Spec (intro) |
| CONTEXT | Groups are ordered least-derived to most-derived (instruments and raw book first, then quotes, trades, bars, derivatives). | test | `spec_data_testing.md` | Data Testing Spec (intro) |
| CONTEXT! | Documented Python defaults that the acceptance run inherits unless overridden: `can_unsubscribe=True`, `requests_start_delta=1 hour`, `book_type=L2_MBP` ... | test data | `spec_data_testing.md` | DataTester configuration reference |
| CONTEXT! | Explicit cross-language divergence: "Note: Rust `DataTesterConfig::new` sets `manage_book=true`, while Python defaults it to `False`." | test py rust | `spec_data_testing.md` | DataTester configuration reference |
| CONTEXT | `subscribe_option_chain` has no row in the configuration table (consistent with TC-D63's note that chains are not yet configurable via `DataTesterConfig`); | test | `spec_data_testing.md` | DataTester configuration reference |
| CONTEXT! | Defaults that silently shape every test outcome: `tob_offset_ticks`=500, `stop_offset_ticks`=100, `open_position_time_in_force`=GTC ... | test exec | `spec_exec_testing.md` | ExecTester configuration reference |
| CONTEXT | `limit_time_in_force`, `stop_time_in_force`, `stop_trigger_type`, `close_positions_time_in_force` and `emulation_trigger` exist in the config but are never referenced by ... | test exec | `spec_exec_testing.md` | ExecTester configuration reference |
| CONTEXT | "An adapter that passes groups 1-5 is considered baseline compliant." Groups 6-10 are above baseline. | exec test recon | `spec_exec_testing.md` | Execution Testing Spec (preamble) |
| CONTEXT | The harness exists in both languages: Python `nautilus_trader.test_kit.strategies.tester_exec`, Rust `nautilus_testkit::testers`. | test exec | `spec_exec_testing.md` | Execution Testing Spec (preamble) |
| CONTEXT | Options cases require a `CryptoOption` instrument and an OTM option with reasonable liquidity. | exec prov | `spec_exec_testing.md` | Group 10: Options trading |
| CONTEXT | Driven by `enable_stop_buys` / `enable_stop_sells` + `stop_order_type` (default STOP_MARKET), `stop_offset_ticks` (default 100), `stop_limit_offset_ticks` (required for ... | test exec | `spec_exec_testing.md` | Group 3: Stop and conditional orders |
| CONTEXT | 'These datasets predate this policy and use raw vendor formats (CSV/CSV.gz) without metadata.json. They remain valid for existing tests. | fixt | `test_datasets.md` | Legacy datasets |
| CONTEXT | Concrete regeneration recipes. ITCH: source 01302019.NASDAQ_ITCH50.gz (~4.4 GB) from NASDAQ EMI, symlinked into /tmp, regenerated via `cargo test -p nautilus-testkit ... | fixt rust | `test_datasets.md` | Regenerating datasets > ITCH AAPL L3 deltas / Tardis Deribit BTC-PERPETUAL L2 deltas |
| CONTEXT | 'Target standards for curating, storing, and consuming external datasets used as test fixtures. New datasets should follow these standards. | fixt test | `test_datasets.md` | Test Datasets |
| CONTEXT | Acquisition instructions for tutorial fixtures: Binance depth snapshots from data.binance.vision (BTCUSDT T_DEPTH for 2022-11-01, snap and update CSVs; | fixt test | `test_datasets.md` | Tutorial test data > Obtaining the data |
| CONTEXT | The per-type coverage table doubles as an adapter checklist: every listed data type carries an 'Adapter spec' checkmark except CustomData, which is marked '-'. | test data books | `testing.md` | Data type testing > Coverage per data type |
| CONTEXT | Each data type is expected to be tested at nine layers. Eight are in-tree (crates/data/tests/engine.rs, crates/common/src/actor/tests.rs ... | test data | `testing.md` | Data type testing > Test layer matrix |
| CONTEXT | Automated tests are treated as executable specifications: they document intended behaviour, enable refactoring, catch regressions, and act as living examples. | test | `testing.md` | Testing |
| CONTEXT | Tests and runtime contracts form one design system. The design-by-contract ladder (rust.md) pushes invariants into the type system; | test | `testing.md` | Testing policy |
| CONTEXT | 'The surface probe in crates/common/src/live/dst.rs only pins the re-export shape; it does not check that callers actually use the seam. | rust test | `testing.md` | Testing policy > DST readiness |
| CONTEXT | 'The formal verification rung is aspirational: no Kani or Prusti harness has landed in the workspace. | test rust | `testing.md` | Testing policy > Mechanism ladder |

`!` after the level marks a requirement that fails silently — see section 4.

---

## 4. High-risk correctness checklist

The MUST requirements whose violation produces **silently wrong behaviour** — no exception, no log,
just incorrect data or state. These are the ones worth re-reading in the source before touching the
corresponding code, because tests written against the wrong assumption will pass.

| Requirement | How it fails silently | Source |
|---|---|---|
| Implement execution reconciliation: "Generate order, fill, and position status reports for startup reconciliation." Milestone: "Execution client ... | Without reports, restart silently proceeds with a stale/empty view of venue orders and positions; no error is raised. | `adapters.md` |
| "Start sessions with existing open orders to verify the adapter reconciles state on connect before issuing new commands." "Seed preloaded positions ... | Trading before reconciliation completes silently duplicates or conflicts with orders already resting at the venue. | `adapters.md` |
| Data client `connect()` order: (1) fetch instruments via REST (`bootstrap_instruments()`); | Connecting the WS before caching instruments means the handler parses messages without precision metadata — silently wrong prices/quantities or ... | `adapters.md` |
| Execution client `connect()` order: (1) `ensure_instruments_initialized_async()` (early-return if already cached, else fetch via REST and cache to ... | Signalling connected before the account is registered lets reconciliation run without a portfolio account — orders and PnL are computed against ... | `adapters.md` |
| "Each adapter's `common/credential.rs` must provide two things: 1. `credential_env_vars()` free function: returns environment variable names as a ... | Resolution scattered into config objects means one code path picks up an env var and another does not — the adapter silently connects unauthenticated ... | `adapters.md` |
| Credential env var naming: Mainnet/Live `{VENUE}_API_KEY` / `{VENUE}_API_SECRET`; Testnet `{VENUE}_TESTNET_API_KEY` / `{VENUE}_TESTNET_API_SECRET`; | Silently degrading to unauthenticated mode is the explicit hazard: public endpoints keep working, private ones return empty results, and the adapter ... | `adapters.md` |
| "Nautilus uses `UnixNanos` (nanoseconds since epoch). Most venues deliver `ms`. | A missed ms->ns conversion yields timestamps ~1e6x too small; the platform accepts them silently, corrupting ordering, replay and any time-based ... | `adapters.md` |
| `LiveDataClient` handles non-market data (news feeds, custom streams) via `_connect`, `_disconnect`, `_subscribe`, `_unsubscribe`, `_request`. | Not overriding a hook the adapter claims to support leaves the base `NotImplementedError`/no-op in place: the subscription is accepted by the engine ... | `adapters.md` |
| Subclass `LiveExecutionClient` and implement the "required overrides": `_connect`, `_disconnect` ... | The five `generate_*` methods are the reconciliation surface; leaving any unimplemented means that class of venue state is never recovered on restart. | `adapters.md` |
| "When implementing `_subscribe_order_book_deltas` or streaming order book data, adapters **must** set `RecordFlag` flags correctly on each ... | Explicitly called out by the document: ":::warning A missing `F_LAST` is a silent bug: no error is raised, but subscribers never receive the data ... | `adapters.md` |
| "Both data and execution clients follow a strict initialization order during `connect()` to prevent race conditions with reconciliation and strategy ... | Initialization deferred past `connect()` lets reconciliation and strategies start against an incomplete client — a race that produces wrong state ... | `adapters.md` |
| "All clients that cache instruments must implement three methods with standardized names: `cache_instruments()` (plural, bulk replace) ... | Confusing bulk-replace with upsert silently drops instruments from the cache; downstream parsing then fails or produces wrong precision. | `adapters.md` |
| "The live runner calls sync `ExecutionClient` and `DataClient` trait methods from within a tokio runtime. | Fails loudly (panic), not silently. NOTE: Rust/tokio-specific; a pure-Python adapter's analogue is not calling blocking I/O inside asyncio callbacks. | `adapters.md` |
| "Adapters should ship two layers of coverage: the Rust crate that talks to the venue and the Python glue that exposes it to the wider platform. | NOTE: the `tests/` vs `#[cfg(test)]` split is a Rust-crate layout rule; the same document places all Python adapter tests under ... | `adapters.md` |
| "**Test data sourcing**: Test data must be obtained from either official API documentation examples or directly from the live API via network calls. | Fabricated fixtures make a parser look correct against invented shapes while it silently mis-parses real venue payloads (negative precision ... | `adapters.md` |
| Use two enums: `{Venue}WsFrame` (serde-deserialized wire frames covering every JSON shape the venue can send — login responses, subscription acks ... | Two independent silent failures: (a) a WS send that fails after retries and is not converted into `OrderRejected` leaves the engine holding a ... | `adapters.md` |
| On reconnection restore authentication and subscriptions: (1) preserve original subscription arguments in a separate collection keyed by topic ... | Failing to replay subscriptions after a reconnect is pure silent data loss: the socket is up, the client reports connected, and no market data or ... | `adapters.md` |
| Two subscription states, Pending and Confirmed, with transitions: `mark_subscribe()` -> Pending; `confirm()` Pending->Confirmed; | Not checking the ack `op` field re-confirms a topic the user just unsubscribed from: the state store shows an active subscription that the venue is ... | `adapters.md` |
| 'iai is deterministic (immune to system noise) but results are machine-specific. | YES - silent. Comparing instruction counts taken on two different machines yields a clean, deterministic-looking delta that means nothing. | `benchmarking.md` |
| Rule 1: 'Set up outside the timing loop. All work that doesn't change between iterations belongs in the surrounding code or in `iter_batched_ref`'s ... | YES - both are silent. Setup inside the timed body inflates every sample; the benchmark still completes and reports a confident number with tight ... | `benchmarking.md` |
| '`iai` requires functions that take no parameters. Keep them small so the instruction count is meaningful and so changes outside the function don't ... | YES - the document itself uses the word 'misleading'. Varying setup inflates counts while iai still reports them as a clean deterministic figure, so ... | `benchmarking.md` |
| "Once a message (request, response, event, or command) is created, its fields must not be mutated." The scope is explicitly all four message kinds - ... | HIGH / silent. Mutating a message raises nothing and the live path keeps working. | `design_principles.md` |
| "Components treat incoming messages as input. If a component needs a different representation, it derives new local state or a new message ... | Same silent class as above; in an adapter the tempting violation is amending a received order/event object in place during reconciliation or fill ... | `design_principles.md` |
| 'NautilusTrader *must* compile and run on **Linux, macOS, and Windows**. Please keep portability in mind (use `std::path::Path`, avoid Bash-isms in ... | YES - silent. A Linux-only CI matrix goes green while the distribution is broken on macOS/Windows (hardcoded POSIX paths, `/` separators, uvloop-only ... | `environment_setup.md` |
| Required for Rust/PyO3 on Linux and macOS after `uv sync`: `export PYO3_PYTHON="$PWD/.venv/bin/python"`; | Dormant for a pure-Python distribution (no PyO3 build step). If a Rust core is ever added, a stale `PYO3_PYTHON` binds the extension to a different ... | `environment_setup.md` |
| Step 2 (foreign - Python/Cython/C): use the data while the CVec value is in scope, and "Do not modify the fields ptr, len, cap." | SILENT then fatal - a mutated len/cap is not validated anywhere; the corruption only appears when the drop helper reconstructs the Vec with ... | `ffi.md` |
| Step 3 (foreign): EXACTLY ONCE, call the type-specific drop helper exported by Rust (e.g. | A forgotten drop is entirely silent - a per-message leak in a hot streaming path degrades a long-running node with no error. | `ffi.md` |
| Cython helpers that allocate temporary C buffers with PyMem_Malloc, wrap them in a CVec and return the address inside a PyCapsule create EVERY such ... | A pure-Python consumer that receives such a capsule and tries to free it double-frees. | `ffi.md` |
| When Rust pushes a heap-allocated value into Python it MUST use PyCapsule::new_with_destructor, with a destructor that reconstructs the original ... | PyCapsule::new(…, None) compiles and runs correctly - it just leaks forever, silently. | `ffi.md` |
| Rust panics must never unwind across extern "C" functions - unwinding into C or Python is undefined behaviour and can corrupt the foreign stack or ... | Omission gives undefined behaviour rather than a clean crash - stack corruption and half-dropped resources can manifest arbitrarily far from the ... | `ffi.md` |
| There is no generic cvec_drop; the old one always treated the buffer as Vec<u8>. | SILENT and the worst kind - allocator bookkeeping corruption produces no immediate error and surfaces later as unrelated heap failures. | `ffi.md` |
| For .pyx and .pxd files, all functions and methods returning void or a primitive C type (bint, int, double) must include the `except *` keyword in ... | SILENT by the guide's own words - the exception is swallowed and execution continues with an undefined return value. | `python.md` |
| The NumPy docstring spec is used throughout the codebase. "This needs to be followed consistently so the docs build correctly." - a build-correctness ... | A malformed docstring can break or silently mis-render generated API documentation rather than failing a lint. | `python.md` |
| Do not use truthiness to test for None on anything other than collections. "Always use if foo is None: (or is not None) to check for a None value... | HIGHEST silent-failure item in this document, and directly live for a venue adapter. | `python.md` |
| "All function and method signatures *must* include type annotations" (must is italicised in the source). | For a distributed library, missing annotations degrade downstream type checking silently - consumers' mypy/pyright passes cleanly against Any. | `python.md` |
| 'Do not use `cargo publish --workspace` for CI releases.' The release job runs `scripts/ci/publish-cargo-crates.sh`, which publishes crates one at a ... | YES - silent, and the analogue applies to any PyPI distribution with extras. A published package whose optional feature/extra resolves to a ... | `releases.md` |
| Sequencing rules that must be kept intact when editing `.github/workflows/build.yml`: (a) 'The draft GitHub release must exist before any release ... | Rule (c) is directly transferable to any external PyPI distribution using Trusted Publishing. | `releases.md` |
| 'GitHub recommends creating a draft release, attaching all assets, then publishing the draft before enabling release immutability. | YES - irreversible. Publishing the GitHub release before every asset is attached cannot be corrected once immutability is on: the assets and tag are ... | `releases.md` |
| Install custom runtimes before first use: Rust-native binaries owning `main()` may call `set_runtime()` before `LiveNode::build()` or any ... | A current-thread runtime or one lacking I/O/timer drivers violates adapter assumptions; | `rust.md` |
| Cancellation safety: call out whether the function is cancellation-safe and what invariants still hold when it is cancelled. | A function cancelled mid-await that is not cancellation-safe can drop an already-consumed message or leave a subscription half-registered with no ... | `rust.md` |
| Timeout patterns: wrap network or long-running awaits with timeouts (`tokio::time::timeout`) and propagate or handle the timeout error. | An un-timed-out network await hangs the task forever with no log and no error; connection lifecycle appears to be progressing while it is dead. | `rust.md` |
| Use `debug_assert!` (and `debug_assert_eq!`/`_ne!`) only for internal invariants the correctness module does not model — field relationships ... | A `debug_assert!` guarding public API input is compiled out in release, so invalid input flows through unvalidated in exactly the builds that matter ... | `rust.md` |
| For one-key removal from an `IndexMap`, `shift_remove` preserves insertion order at O(n) cost while `swap_remove` is O(1) but swaps the last entry ... | Calling `swap_remove` on an IndexMap adopted specifically for DST determinism silently destroys the ordering guarantee that motivated the type ... | `rust.md` |
| `AHash` randomizes its hasher per process, so `AHashMap`/`AHashSet` iteration order varies between runs. | Per-process hasher randomization produces different fill ordering and different event sequences run-to-run with no error; | `rust.md` |
| Do not use the `hash` pyclass attribute with `eq_int` enums. PyO3's auto-generated `__hash__` uses Rust's `DefaultHasher`, which produces different ... | A broken hash/eq contract silently corrupts dict and set membership — equal values hash to different buckets, so lookups miss without any error. | `rust.md` |
| The actor registry, component registry and message bus each use `thread_local!` storage: an object registered on one thread is never visible from ... | Accessing the registry or message bus from a non-event-loop thread does not error — the lookup simply returns nothing because the thread-local map is ... | `rust.md` |
| `ActorRef` guards must be obtained and dropped within a single synchronous scope, never stored in a struct field, never held across an `.await` ... | These rules are stated as enforced "by convention" rather than by the compiler; | `rust.md` |
| Do not use `Arc<PyObject>` in callback-holding structs: Rust `Arc` holding Python objects increases the Python refcount, Python objects may reference ... | A leaked cycle produces unbounded memory growth in a long-running live node with no error or log; the process simply degrades. | `rust.md` |
| CVec contract: for raw vectors crossing the FFI boundary read the FFI Memory Contract (ffi.md). | Calling `vec_drop_*` zero times leaks silently; calling it more than once is a double free — memory corruption with no immediate error. | `rust.md` |
| When evolving Cap'n Proto schemas: additive changes only, adding new fields at the end; never remove fields (mark deprecated ones in comments); | Reusing a field number or removing a field does not fail to compile — a peer on the old schema decodes the new field's bytes into the old field's ... | `rust.md` |
| Do not clone a value out of a container and then mutate the clone (the pattern inherited from the Cython port). | Explicitly named by the document as SILENT staleness — the mutated clone and the canonical entry diverge with no error. | `rust.md` |
| `Cache::order_mut` takes `&mut Cache`, so strategies and adapters receiving a `CacheView` (which only exposes immutable cache borrows) cannot reach ... | Directly relevant beyond Rust: an adapter must never mutate a cached order itself. | `rust.md` |
| Use a custom spec rather than `derive_builder::Builder` with `builder(default)`: the latter bypasses the production constructor, so invariants added ... | A builder that bypasses the production constructor makes tests pass against objects that could never be constructed in production; | `rust.md` |
| `AHashMap` is not thread-safe. Wrapping it in `Arc` only enables sharing the pointer across threads, it does not coordinate mutation. | Mutating an `Arc<AHashMap>` from multiple threads is a data race — corruption or torn reads, not a compile error or panic. | `rust.md` |
| Python type stubs (`.pyi`) are generated from Rust source via pyo3-stub-gen; every type and function exposed to Python needs a matching stub ... | A missing stub annotation does not fail the build — the type simply vanishes from the generated `.pyi`, so downstream type checking silently loses ... | `rust.md` |
| When implementing `Send` or `Sync` unsafely: (1) document exactly which fields violate the trait requirements; | An unsafe `Send` whose single-thread invariant is later broken by a caller is undefined behavior with no diagnostic — the type compiles and usually ... | `rust.md` |
| Before committing schema changes run `make check-capnp-schemas`, which skips with a warning if capnp is not installed (acceptable locally), fails on ... | Without this check, committed generated code can drift from the schema; encoders and decoders then disagree about field layout and produce ... | `rust.md` |
| Keep related dependencies aligned: `capnp`/`capnpc` (exact), `arrow`/`parquet` (major.minor), `datafusion`/`object_store` ... | Version skew between capnp/capnpc or arrow/parquet can produce silently mis-decoded serialized data rather than a build failure. | `rust.md` |
| Skip when: **Never**. On start, request all instruments for the venue; `on_instruments` receives the list. | SILENT. Wrong `price_precision` / `size_increment` do not raise — they silently mis-round every price and quantity the adapter later constructs, and ... | `spec_data_testing.md` |
| Skip when: **Never**. Load a single instrument by `InstrumentId` via the provider. | SILENT. If `load_async` populates a provider-internal dict but never reaches the engine cache, `self.cache.instrument(...)` returns None and ... | `spec_data_testing.md` |
| Periodic `OrderBook` snapshots into `on_order_book`. Pass criteria: "Book snapshots received with bid/ask levels; | SILENT. An adapter that emits a snapshot on every venue update instead of on the interval still delivers `OrderBook` objects — the case only fails if ... | `spec_data_testing.md` |
| `OrderBookDepth10` snapshots into `on_order_book_depth`. Pass criteria: "Depth snapshots received with up to 10 bid/ask levels; | SILENT. Levels emitted in the wrong order still form a valid `OrderBookDepth10` object; consumers read the wrong best bid/ask with no exception. | `spec_data_testing.md` |
| Subscribe to deltas with `manage_book=True`; the actor applies each delta to a local `OrderBook`. | HIGHEST SILENT RISK IN THE DOCUMENT. This is the end-to-end acceptance test for the `RecordFlag` contract (F_LAST / F_SNAPSHOT). | `spec_data_testing.md` |
| Skip when: **Never**. `QuoteTick` events into `on_quote_tick`. Pass criteria: "At least one `QuoteTick` received with valid bid/ask prices and sizes; | SILENT. A parser that swaps the bid and ask fields (or maps bid_size to ask_size) emits perfectly well-formed `QuoteTick`s. | `spec_data_testing.md` |
| Skip when: **Never**. `TradeTick` events into `on_trade_tick`. Pass criteria: "At least one `TradeTick` received with valid price, size, and ... | SILENT. Aggressor-side inversion (venue 'taker side' vs 'maker side' conventions differ per venue) yields valid `TradeTick`s with reversed ... | `spec_data_testing.md` |
| `Bar` events into `on_bar` for a configured `BarType` (example: `BTCUSDT-PERP.VENUE-1-MINUTE-LAST-EXTERNAL`). | SILENT, twice over. (1) Positional mis-mapping of the venue's OHLCV array (Gate.io-style array klines) produces valid Bars with impossible OHLC ... | `spec_data_testing.md` |
| `IndexPriceUpdate` into `on_index_price`; valid instrument ID and index price. Skip only when not a derivative or unsupported. | SILENT if mark price and index price are wired to the same venue field — both streams flow, but basis/funding calculations built on them are wrong. | `spec_data_testing.md` |
| `FundingRateUpdate` into `on_funding_rate`; valid instrument ID and rate. Skip only when the instrument is not a perpetual or the adapter does not ... | SILENT. Funding rates are commonly published as percent vs fraction and per-interval vs annualised; a scale error passes every structural check. | `spec_data_testing.md` |
| Stop the actor with `can_unsubscribe=True` (the default). Event sequence: "Data subscriptions removed; | SILENT. An `unsubscribe_*` that is a no-op (or that removes local bookkeeping without sending the venue-side unsubscribe frame) leaves the venue ... | `spec_data_testing.md` |
| StopMarket / StopLimit / MarketIfTouched / LimitIfTouched accepted with the correct trigger price (and limit price for STOP_LIMIT and LIT), and "The ... | An inverted trigger-direction mapping (buy-stop below market) fires instantly and reads as a normal fill; nothing errors. | `spec_exec_testing.md` |
| Risk engine bypassed (`LiveRiskEngineConfig(bypass=True)`) "to avoid interference" during execution acceptance runs. | A run with the risk engine active can mask an adapter defect by pre-empting the order, so the case appears to pass for the wrong reason. | `spec_exec_testing.md` |
| Market order lifecycle emits `OrderInitialized` -> `OrderSubmitted` -> `OrderAccepted` -> `OrderFilled`; | Skipping `OrderAccepted` (jumping submitted->filled) leaves the order FSM in an unexpected state with no exception raised; | `spec_exec_testing.md` |
| "Some adapters simulate market orders as aggressive limit IOC orders (check adapter guide). | HIGH. Gate.io spot commonly synthesises market orders; if the adapter emits a limit-IOC-shaped event stream, strategies observe a different order ... | `spec_exec_testing.md` |
| With `use_quote_quantity=True`: "Order submitted with quote currency quantity; | HIGH. Reporting the fill quantity in quote currency (100 USDT rather than 0.0009 BTC) silently corrupts position size and P&L; no exception is raised. | `spec_exec_testing.md` |
| An unfilled IOC order terminates as `OrderCanceled`: "The venue should cancel the unfilled IOC order; | HIGH, explicitly called out by the guide. Mapping an IOC kill to `OrderExpired` is accepted by the FSM; | `spec_exec_testing.md` |
| GTD order "accepted with GTD TIF and correct expiry timestamp" (driven by `order_expire_time_delta_mins`). | An expiry timestamp in the wrong unit or timezone is accepted by the venue and simply expires at the wrong time. | `spec_exec_testing.md` |
| "Some venues may report expiry as a cancel; verify the adapter maps this to `OrderExpired`." GTD expiry must surface as `OrderExpired` even when the ... | HIGH, and the exact mirror-image of TC-E14. A naive 'venue says cancelled -> emit OrderCanceled' mapping satisfies TC-E14 and silently violates ... | `spec_exec_testing.md` |
| Modify emits `OrderPendingUpdate` -> `OrderUpdated` with the new price, and the order must exit `PendingUpdate`. | HIGH. 'If the event never arrives, the order stays in `PendingUpdate` and the tester stops modifying it.' No exception is raised - the venue-side ... | `spec_exec_testing.md` |
| If the adapter does not support modify, an attempted modify produces `OrderModifyRejected` "with reason; original order remains unchanged"; | Swallowing an unsupported modify (emitting no event) leaves the order in PendingUpdate forever - a silent hang rather than an error. | `spec_exec_testing.md` |
| Cancel emits `OrderPendingCancel` -> `OrderCanceled`; "Verify the `OrderCanceled` event contains the correct `venue_order_id`." ... | A wrong or missing venue_order_id on the cancel event silently breaks order identity across restart and reconciliation. | `spec_exec_testing.md` |
| Three distinct cancel paths must work: cancel-all on stop (default), `use_individual_cancels_on_stop=True`, and `use_batch_cancel_on_stop=True` ("All ... | A batch-cancel endpoint that partially succeeds while the adapter reports full success leaves live orders on the venue after shutdown. | `spec_exec_testing.md` |
| With `use_post_only=True` at a passive price the order is "accepted as a maker order; post-only flag acknowledged by venue". | Dropping the post-only flag on the wire is invisible in the event stream but changes fee tier and fill behaviour. | `spec_exec_testing.md` |
| With `reduce_only_on_stop=True` the closing order carries the reduce-only flag and the position is fully closed. | Silently dropping reduce-only on the close order can open an opposite position instead of flattening. | `spec_exec_testing.md` |
| With `order_display_qty` < `order_qty`: "Order accepted with display quantity set; only display qty visible on the book." | Ignoring display qty exposes full size on the book - accepted, no error, wrong market impact. | `spec_exec_testing.md` |
| An unsupported order type produces `OrderDenied` "pre-submission rejection by adapter", "before reaching venue", with a reason. | HIGH. The failure mode is substitution, not omission: mapping an unsupported type onto the nearest supported one places a real order with different ... | `spec_exec_testing.md` |
| An unsupported time-in-force produces `OrderDenied` pre-submission with a reason. | HIGH. Coercing an unmapped TIF (e.g. AT_THE_OPEN, GTD) to GTC changes order lifetime silently; nothing in the event stream reveals the substitution. | `spec_exec_testing.md` |
| "Position opened on start; market order submitted and filled BEFORE limit order maintenance begins." Ordering between the opening market order and ... | Racing the two produces limit orders priced/sized against a position that does not exist yet, with no error. | `spec_exec_testing.md` |
| On stop, "All strategy-owned open orders canceled" and "All strategy-owned positions closed; net position = 0". | Cancel-all-on-account instead of strategy-owned silently kills unrelated orders. | `spec_exec_testing.md` |
| "Use `external_order_claims` to claim the instrument so the adapter reconciles orders for it" and "Verify that the reconciled order count matches the ... | Without external_order_claims reconciliation appears to run and reports zero orders - a false clean bill of health rather than an error. | `spec_exec_testing.md` |
| Starting with `reconciliation=True` generates an `OrderStatusReport` per open order; | HIGH. A report with wrong price/qty/side reconciles cleanly - the engine simply believes something false about live venue state. | `spec_exec_testing.md` |
| `FillReport` per historical fill; "Each filled order is loaded into the cache with correct `venue_order_id`, status=FILLED, fill price, fill ... | HIGH. Commission mis-parsed (wrong currency, sign, or per-fill vs cumulative) yields wrong realised P&L with nothing failing anywhere. | `spec_exec_testing.md` |
| `PositionStatusReport` per position; "Position loaded into cache with correct instrument, side, quantity, and entry price matching the venue"; | HIGH. A wrong average entry price or an inverted side reconciles without complaint and poisons every subsequent P&L and risk calculation. | `spec_exec_testing.md` |
| Authority split between the two files: 'metadata.json is authoritative for provenance, licensing, and redistribution rules. | SILENT: if the two files disagree and a reader trusts the wrong one, a dataset can be redistributed under a licence claim that metadata.json never ... | `test_datasets.md` |
| 'Tests that rely on user-fetched data should: Be marked or grouped separately from default CI tests. | SILENT: a test that quietly passes (rather than skipping with a message) when its dataset is absent reports success while asserting nothing. | `test_datasets.md` |
| Explicit prohibitions - 'Do not: Upload restricted vendor datasets to the public R2 bucket. | CI that depends on credentials or paid data appears green for maintainers who hold them and is permanently red or skipped for everyone else, so a ... | `test_datasets.md` |
| 'When a schema change invalidates a large Parquet file, regenerate it from the original source data using the curation tests below. | SILENT or misleading: regenerating without updating checksums.json makes ensure_test_data_exists() either reject the new file or keep serving a stale ... | `test_datasets.md` |
| 'Every curated dataset that stores or redistributes a concrete artifact must include a metadata.json with at minimum:' `file` (filename), `sha256` ... | Missing licence/original_url creates undetectable licensing exposure: the fixture works fine technically while redistribution rights are unknown. | `test_datasets.md` |
| User-fetched datasets use the same fields where applicable and must additionally include: `distribution` ('Must be "user-fetch"'), `fetch_method` ... | A restricted dataset without `public_mirror: false` can be picked up by tooling and mirrored publicly without any error being raised. | `test_datasets.md` |
| 'Do not write python/tests/ cases that probe Rust panic paths in process with pytest.raises(BaseException) or similar broad catches. | SILENT: the test passes locally against the debug extension and aborts the whole interpreter against the release wheel, so the defect only appears in ... | `testing.md` |
| 'Do not capture log output to assert on log messages. Log capture in tests is fragile because loggers are global state, test execution order is ... | SILENT: because loggers are global and execution order is non-deterministic, a log-capture assertion can pass or fail depending on which other tests ... | `testing.md` |

---

## 5. Task-triggered reading map

Which original document to open for which kind of work. Open the source, not this digest, once the
task is chosen.

| Working on | Open |
|---|---|
| A new client or the overall adapter skeleton | `adapters.md` — structure and phases |
| Instrument loading and the provider | `adapters.md` — instrument provider |
| Subscriptions, order books, bars, quotes | `adapters.md` — data client; `spec_data_testing.md` |
| Order submission, modification, cancellation | `adapters.md` — execution client; `spec_exec_testing.md` |
| Startup reconciliation and mass status | `adapters.md` — reconciliation; `spec_exec_testing.md` |
| WebSocket lifecycle, reconnect, replay | `adapters.md` — connection and subscription lifecycle |
| Writing or restructuring tests | `testing.md`, then the relevant `spec_*_testing.md` |
| Fixtures and recorded venue payloads | `test_datasets.md` |
| Performance work or a regression claim | `benchmarking.md` |
| Anything touching the Rust core | `rust.md`, then `ffi.md` |
| Python style, typing, async idiom | `python.md`, `coding_standards.md` |
| Docstrings and public documentation | `docs.md` |
| Version bumps and release mechanics | `releases.md` |
| Setting up a development environment | `environment_setup.md` |

---

## 6. Ambiguities in the upstream guide

Places where the pinned documents contradict themselves, or where prose and the accompanying table
or code disagree. Recorded so that a future reader does not spend time deciding whether they
misread. **These are observations about the source, not defects in this project**, and none of them
were resolved unilaterally here.

82 recorded:

1. "Baseline data compliant" is undefined against skippable cases. Line 13: "An adapter that passes groups 1–4 is considered baseline data compliant." But every case in Group 2 carries "Skip when: ...
2. "Skip when: N/A" is undefined and collides with the document's own "Never". TC-D71 and TC-D72 both record "**Skip when:** N/A (adapter-specific)." Elsewhere the document uses an explicit "Never." for non-skippable cases (TC-D01 ...
3. '## Required metadata' is contradicted by its own carve-out and by its source-of-truth claim. 'Every curated dataset that stores or redistributes a concrete artifact must include a metadata.json with **at minimum**: [file ...
4. '## Storage format' states 'Raw vendor files should stay **outside the repo** and outside the public R2 bucket', but '## Regenerating datasets > Tardis' instructs `wget -O ...
5. '### Feature flag conventions' says 'Document every feature in the crate-level documentation', and '#### Module-Level documentation' gives a `# Feature flags` example — but '#### Section header casing' lists the canonical header ...
6. '### Safety policy' requires 'cover all `unsafe` blocks with unit tests' without saying what counts as coverage for an `unsafe` block whose contract is a caller obligation that cannot be violated from safe test code (the `unsafe ...
7. '#### Module-Level documentation' says 'All modules must have module-level documentation', but the file-header requirement, the `mod tests` convention and the `property_tests` module convention are all module-defining and the ...
8. 'Hash collections' guidance for network clients is unqualified and cuts against the determinism rule. '#### Performance' says 'Network clients: Prefer standard `HashMap` for network-facing components where security considerations ...
9. 'user-fetch' is defined as a *distribution model* in '## Dataset categories' (with a mandatory `distribution: "user-fetch"` metadata field), but the closing line of the document asserts it as a *status* the legacy table cannot ...
10. A second Python/Rust divergence is present but not flagged. The configuration reference calls out only one: "Note: Rust `DataTesterConfig::new` sets `manage_book=true`, while Python defaults it to `False`." Yet the same table ...
11. APPLICABILITY UNSTATED for external packages, 'Environment variable conventions': "Environment variable resolution should happen in core Rust code, not Python bindings." For a pure-Python distribution there is no core Rust code ...
12. Adapter runtime enforcement scope vs the block_on carve-out: rust.md scopes '### Adapter runtime patterns' to 'Adapter crates (under `crates/adapters/`)' and says the `check_tokio_usage.sh` hook enforces them, but item 5 exempts ...
13. Authentication scope per case is unstated. Prerequisites require credentials "when the venue requires authentication for the data being tested", but no test card indicates which cases need authentication.
14. BACKPRESSURE RULE HAS NO CROSS-LANGUAGE STATEMENT: "WebSocket channels on latency-sensitive paths are intentionally **unbounded**.
15. British/American spelling collide inside a single section, against a guide-wide rule. The required metadata key is `licence` (British), while '### Simple files' says curate-dataset.sh 'creates a versioned directory (v1/<slug>/) ...
16. CASES DECLARED NON-SKIPPABLE THAT THE HARNESS CANNOT DRIVE. The document is framed as an ExecTester matrix, yet TC-E13 'requires manual order creation', TC-E36 must be driven 'programmatically, not via ExecTester auto-maintain' ...
17. CONDITIONALS WITHOUT THRESHOLDS. Several sections gate themselves on undefined judgement calls with no criterion given: "For adapters with multiple client types, define an adapter-level error enum" ('Error taxonomy');
18. CONFIG PARAMETERS WITH NO CORRESPONDING CASE. `emulation_trigger` is listed as affecting groups 2 and 3, yet no test card mentions emulated orders;
19. CONTRADICTORY RUST BUILDER API within one document. 'Basic smoke test' uses `.with_open_position_on_start_qty(Some(dec!(0.001)))`;
20. Cap'n Proto verification is simultaneously required and skippable: '### Verifying schema consistency' says 'Before committing schema changes, ensure generated files are up-to-date: make check-capnp-schemas', then states the ...
21. DST enforcement scope is stated as partly discretionary: 'The pre-commit hook `check-dst-conventions` enforces `IndexMap` / `IndexSet` in `crates/live/src/manager.rs` and `crates/execution/src/matching_engine/engine.rs`...
22. DST readiness is written as a set of hard preconditions but is self-declared unenforceable: 'The `surface` probe in crates/common/src/live/dst.rs only pins the re-export shape;
23. Direct contradiction between testing.md and python.md on test naming, with neither acknowledging the other. testing.md '### General': 'Name test functions after what they exercise;
24. Documentation voice diverges by language with no cross-reference: rust.md '### Documentation standards' mandates 'third-person declarative voice for all doc comments (e.g., "Returns the account ID" not "Return the account ID")' ...
25. GROUP 10 NUMBERING. TC-E93, TC-E95, TC-E97 and TC-E98 are absent from a group otherwise running E90-E101. The general note 'Test IDs use spaced numbering to allow insertion without renumbering' explains gaps between groups;
26. Group 8 violates the document's own numbering rule. Line 66: "Test IDs use spaced numbering to allow insertion without renumbering." Every group opens a fresh decade — Group 1 -> TC-D01..03, Group 2 -> D10s, Group 3 -> D20s ...
27. INCONSISTENT TYPE for the `ExecTesterConfig::new` quantity argument. Prerequisites: `ExecTesterConfig::new(strategy_id, instrument_id, client_id, order_qty)`; Basic smoke test: `...
28. Line-length limit is stated twice with different numbers and overlapping scope. coding_standards.md '### Universal formatting rules' opens "The following applies to **all** source files (Rust, Python, Cython, shell, etc.)" and ...
29. MUTUAL EXCLUSIVITY OF THE MAINTENANCE FLAGS IS UNSTATED. `modify_orders_to_maintain_tob_offset` and `cancel_replace_orders_to_maintain_tob_offset` (and their stop-order twins) are presented as alternative drivers for Group 4, but ...
30. No runtime guidance for a pure-Python external adapter. Prerequisites steers "new Rust-backed PyO3 adapters" to `nautilus_trader.live.LiveNode` and notes that "Legacy examples still use `nautilus_trader.live.node.TradingNode`" ...
31. PASS CRITERION CONTRADICTED BY ITS OWN CONSIDERATION - TC-E70. Pass criteria: 'Order rejected by venue; `OrderRejected` event received with reason indicating post-only violation.' Considerations: 'Some venues may partially fill ...
32. PROSE vs EXAMPLE MISMATCH, 'Connection lifecycle → Data client': the numbered list has five steps (fetch, cache locally, emit `DataEvent::Instrument`, `ws.cache_instruments()`, connect WS) but the accompanying code shows only ...
33. RISK POSTURE FOR PRODUCTION RUNS IS UNRESOLVED. Prerequisites list 'Demo/testnet account with valid API credentials (preferred, not required)' and immediately after direct 'If the venue offers a demo/testnet mode, use credentials ...
34. Rust node setup is given only as an in-tree path with no external analogue. "**Rust node setup** (reference: `crates/adapters/{adapter}/examples/node_data_tester.rs`)" — the reference is a path inside the upstream monorepo.
35. SAME PATTERN at TC-E52. Pass criteria: 'After entry fill, TP and SL orders are live on the venue.' Considerations: 'The TP/SL activation mechanism varies by venue (some activate immediately, some are OCA groups).' On an OCA-group ...
36. SCOPE UNSTATED, 'tests/' reservation vs Python layout. "The `tests/` directory is reserved for integration tests that require external infrastructure ... Unit tests ...
37. SELF-CONTRADICTION on how IOC/FOK limit orders are driven. TC-E13 Considerations: 'This test requires manual order creation or adapter-specific configuration, as the ExecTester's default limit order placement uses GTC TIF.' But ...
38. SELF-CONTRADICTION, 'Disconnection lifecycle (`close`)': the prose says "The `close()` method follows a three-step shutdown sequence: signal, command, await", but the code comments are "// 1.
39. SELF-CONTRADICTION, subscription replay vs deliberate unsubscribe. 'Subscription lifecycle' states "User unsubscribes \| `mark_unsubscribe()` \| Confirmed \| Pending \| Temporarily pending until ack" and, in the same ...
40. STALE HEADING, 'Channel naming: `raw` -> `msg` -> `out`': the heading names three stages but the table beneath defines only two (`raw` = "Raw WebSocket frames", `out` = "Venue-specific messages"), and the closing text says "Use ...
41. STRUCTURAL DEFECT, heading nesting around 'Naming conventions': `### Naming conventions` (line 1389) is followed by `#### Channel naming` (1393), then `### Backpressure strategy` (1423) — a sibling h3 — and then `#### Field ...
42. Structural: lines 122-127 of testing.md are orphaned under '## Fuzzing' with no subheading and belong to three other topics - 'When building or modifying core types, write property tests to cover the mathematical boundaries' ...
43. TC-D14 presupposes an initial snapshot that no requirement establishes. Its pass criteria read "...book is not empty **after initial snapshot**", but nothing in Group 2 requires a delta subscription to begin with a snapshot ...
44. TC-D14's Rust example contradicts (or redundantly restates) the stated Rust default. The configuration reference says "Rust `DataTesterConfig::new` sets `manage_book=true`", yet TC-D14's Rust snippet explicitly chains ...
45. TC-D21 has an unexplained missing Rust config. TC-D21 (Request historical quotes) supplies a Python config block and then simply stops — no Rust block, and no note. Its direct analogue TC-D31 (Request historical trades) has both.
46. TC-D63 is required-but-unrunnable through the standard harness. The Group 8 summary lists it as a test case, and the document header asserts "Each adapter must pass the subset of tests matching its supported data types" — yet its ...
47. TEMPLATE vs TABLE MISMATCH, 'MarketDataClient': `SubscribeOptionGreeks` and `UnsubscribeOptionGreeks` are used as parameter annotations in the class body (lines 2124, 2166) but are not among the ~33 imports listed above it — the ...
48. TERM OVERLOADED, "factories". 'Structure of an adapter → Python layer' defines them as "**Factories**: Converts venue-specific data to Nautilus domain models", and the file comment says `factories.py # Instrument factories`.
49. Terminating-period rules conflict across documents for the same text. coding_standards.md '### Comment conventions' item 4: "**Single-line comments** *must not* end with a period *unless* the line ends with a URL or inline ...
50. The LiveNode preference is stated at two different scopes in three documents. testing.md:148-150 - 'For new live adapter **examples and docs in the v2 path**, prefer nautilus_trader.live.LiveNode' (no implementation-language ...
51. The PyO3 abort prohibition is scoped by directory while its stated cause is not. 'Do not write **python/tests/** cases that probe Rust panic paths in process with pytest.raises(BaseException) ...
52. The `<instrument>` component of the naming convention is defined inconsistently by the document's own examples. '## Naming convention' gives `<source>_<instrument>_<date>_<datatype>.parquet` with examples ...
53. The `swap_remove` recommendation and the determinism rule are stated in adjacent paragraphs without linking their precondition: 'Prefer `swap_remove` over `shift_remove` when iteration order does not matter after the removal' ...
54. The doc-comment mood rules are split by language but the PyO3 surface is left unassigned. coding_standards.md '### Doc comment mood' requires indicative for Rust ("Returns a cached client.");
55. The mechanism ladder presents 'Formal verification' as a rung with a trigger condition ('A pure function has crisp invariants and a bounded input space worth a proof'), then immediately disclaims it: 'no Kani or Prusti harness ...
56. The projection rule assigns property-based testing and fuzzing to 'adapter parsers', but the only tooling the document names for either rung is Rust-specific: 'We use [proptest] **in Rust** to enforce invariants' and the fuzzing ...
57. The size thresholds leave an uncovered boundary: '**Small data** (< 1 MB) is checked directly into tests/test_data/<source>/' and '**Large data** (> 1 MB) is hosted as Parquet in the R2 test-data bucket.' A dataset of exactly 1 ...
58. UNRESOLVED UPSTREAM, 'Rust documentation requirements' → What NOT to document: "Files in the `python/` module (PyO3 bindings). Documentation conventions are TBD (*may* use numpydoc specification)." Explicitly open in the source.
59. UNRESOLVED whether `OrderAccepted` is required for orders the venue never rests. TC-E13..TC-E16 all assert '`OrderInitialized` -> `OrderSubmitted` -> `OrderAccepted` -> `OrderFilled`/`OrderCanceled`' for IOC and FOK.
60. VOICE CONFLICT between adapters.md and python.md: 'Rust documentation requirements' mandates "third-person declarative voice (e.g., "Returns the account ID" not "Return the account ID")" and opens with "All adapter documentation ...
61. `OrderDenied` EMITTER vs BYPASSED RISK ENGINE. Prerequisites mandate 'Risk engine bypassed (`LiveRiskEngineConfig(bypass=True)`)', while TC-E72/E73 require `OrderDenied`, described only as 'pre-submission rejection by adapter' / ...
62. `tests/test_data/local/` is given two mutually incompatible layouts. '## Adding a new dataset': 'Use tests/test_data/local/<source>/<slug>/ as the standard local cache path for **generated artifacts** [Nautilus Parquet].
63. benchmarking.md TOOLING TABLE conflicts with the sentence that follows it. The table partitions by function size - Criterion 'Anything >= 100 ns', iai 'Sub-100 ns functions' - presenting a mutually exclusive selection rule.
64. benchmarking.md is DELIBERATELY INCOMPLETE and the missing half sits outside the guide directory. 'For policy (what we benchmark, when, with what rigor, how it ties into CI), see [`/BENCHMARKING.md`](../../BENCHMARKING.md) at the ...
65. benchmarking.md uses INCONSISTENT BENCH NAMES across sections. 'Running benches locally' uses `--bench matching_core -p nautilus-execution`; 'Generating a flamegraph' uses `--bench matching -p nautilus-common --profile bench`.
66. block_on: rust.md '### Adapter runtime patterns' item 3 prescribes 'Use `get_runtime().block_on()` for sync-to-async bridges: When synchronous code needs to call async functions in adapters', illustrated with `fn ...
67. coding_standards.md '### Shell script portability' defines two script classes and leaves the middle case unruled. User-facing scripts must avoid bash 4+ features and work on Windows via Git Bash or WSL;
68. coding_standards.md contradicts itself on commit-message body width. '## Commit messages' item 3: "Keep under **100 character** width." '### Gitlint (optional)': "**79-character body width**: Aligns with Python's PEP 8 ...
69. environment_setup.md HEADING STRUCTURE makes the Nautilus CLI guide's scope undefined. '## Nautilus CLI developer guide' (line 408) is immediately followed by '## Introduction' (410), '## Install' (429) and '## Commands' (438) at ...
70. environment_setup.md gives CONTRADICTORY GUIDANCE on how much Cap'n Proto handling is needed. The preamble says 'Ubuntu's default package is typically too old, so you may need to install from source (see below)', which reads as a ...
71. environment_setup.md gives TWO DIFFERENT `uv sync` INVOCATIONS for the same step. 'Quick setup' (line 57): `uv sync --all-groups --all-extras`. '1. Install dependencies' (line 82): `uv sync --active --all-groups --all-extras`.
72. environment_setup.md names TWO DIFFERENT ENV VARS for the Postgres superuser and never reconciles them. 'Services' (line 382): 'postgres: Postgres database with root user `POSTGRES_USER` which defaults to `postgres`'.
73. environment_setup.md refers to 'both lockfiles in this repo' (line 212, under `exclude-newer`) without ever identifying them.
74. ffi.md leaves a load-bearing gap between '## CVec lifecycle' step 3 and '## Capsules created on the Python side'. Step 3 says the foreign side must, "Exactly once, call the *type-specific* drop helper exported by Rust".
75. ffi.md states the Python-side capsule rule descriptively but the Rust-side rule normatively. Python side: "**Every such capsule is created with a destructor**" - a statement of fact about the current codebase.
76. index.md asserts the Rust-core architecture without saying whether it is descriptive of the platform or prescriptive for integrations.
77. python.md '### Docstrings' / '#### Private methods' is internally unresolved. The prohibition's stated rationale is that "Docstrings generate public-facing API documentation" and would "incorrectly imply they are part of the ...
78. python.md '### Properties vs methods (PyO3 bindings)' is scoped to PyO3 by its heading and first sentence ("When exposing Rust types to Python via PyO3"), but every criterion it gives is language-neutral API design (cheap and ...
79. releases.md MERMAID GRAPH encodes ordering constraints the prose rules do not. The graph has `sdist_asset --> wheel_assets` (sdist upload gates wheel upload) and `build_sdist --> sdist_asset` with `tag --> build_sdist`.
80. releases.md SELF-CONTRADICTION on the crates.io bootstrap path. 'Crates.io publishing' says 'Crates that have never been published still need an initial manual publish before crates.io allows the trusted publisher configuration' ...
81. test_datasets.md '## Legacy datasets' preamble contradicts its own table. Preamble: 'These datasets predate this policy and use raw vendor formats (CSV/CSV.gz) **without metadata.json**.
82. testing.md scopes its Python style rules by upstream directory and gives no rule for out-of-tree suites. '### Python tests (`python/tests/`)' mandates free functions, forbids test classes, and mandates `nautilus_trader.model` ...

---

## 7. Deliberate deviations

This project is an **external community adapter**, not an in-tree integration. That distinction
governs which requirements bind it.

1. **Functional correctness requirements apply in full.** Anything governing how an adapter must
   behave toward the platform — event ordering, order-book flags, reconciliation reports, identity
   handling, precision — is binding regardless of where the code lives. These are the MUST rows
   above, and section 4 lists the subset that fails silently.

2. **In-tree conventions do not automatically bind an external package.** Repository layout, build
   tooling, the upstream release process, crate structure and the in-tree test directory split
   govern contributions to the NautilusTrader repository. They are classified CONVENTION and kept
   visible rather than dropped, so that the decision stays reviewable — but they must not become
   release blockers for this package by default.

3. **The implementation is pure Python.** Every Rust and PyO3 requirement is recorded and none is
   currently actionable, because there is no Rust core here to hold to them.

4. **A Rust/PyO3 migration is a separate decision**, not a consequence of this digest. Nothing here
   should be read as recommending one, and stylistic parity with in-tree Rust adapters is not by
   itself a reason to introduce Rust.

5. **This digest has no authority of its own.** It indexes the pinned source. Where the two
   disagree, the source is right and this file is a bug.
