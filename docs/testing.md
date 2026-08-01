# Testing

## Running the suite

```bash
pip install -e '.[dev]'
pytest
```

The default run executes the unit suite only: `pyproject.toml` sets
`addopts = "-m 'not integration'"`, so anything marked `integration` is
deselected automatically.

Unit tests **use no network and no credentials**. Every exchange payload is a
recorded or synthetic fixture, and every transport is stubbed. A test that needs
the network belongs behind the `integration` marker.

Requirements: Python >= 3.12, < 3.15, and the `nautilus_trader` range pinned in
`pyproject.toml` (`>=1.230.0,<2`).

## What is covered

| Area                        | What the tests assert                                                                                                                                                                                           |
|-----------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Symbology                   | Instrument id to Gate.io symbol and back, per product, including the `-PERP` rule and every malformed-input error path                                                                                          |
| Enums and status mapping    | Order side, time in force, and the `status`/`finish_as`/filled-amount combinations that determine a Nautilus `OrderStatus`                                                                                      |
| Signing                     | HMAC-SHA512 REST and WebSocket signature vectors, client order id generation and sanitization against the venue's charset and length limits                                                                     |
| Errors                      | Typed hierarchy, label-to-error mapping, retry classification, and the capability-gating translation into `WalletNotProvisionedError`                                                                           |
| REST transport              | Header construction, query encoding, error translation, pacing, retry safety (mutating requests are never replayed), ambiguity reporting                                                                        |
| REST namespaces             | Path and parameter construction per product, including the `/futures` versus `/delivery` split and the endpoints that refuse to exist on a delivery namespace                                                   |
| WebSocket transport         | Subscribe and unsubscribe acknowledgement handling, replay after reconnect, backoff schedule, heartbeat and receive-timeout recycling                                                                           |
| Order books                 | The full synchronization algorithm: buffering, straddle detection, stale-snapshot rejection, gap detection and resync, zero-size deletions, both payload shapes                                                 |
| Instruments                 | Payload to instrument per product, precision guards, contract multipliers, fee conventions, and the rejection of unrepresentable price scales                                                                   |
| Instrument provider         | Multi-product loading, filtering of untradable and expired instruments, per-product degradation on an unprovisioned wallet                                                                                      |
| Data client                 | Subscription and request paths, closed-bar filtering, tick construction, mark/index/funding fan-out and reference counting                                                                                      |
| Execution client            | Order translation per product and order type, **every rejection path**, cancellation, amendment, trigger-order id handling, fill application and deduplication, balance aggregation, all four report generators |
| Configuration               | Defaults (including the mainnet default), URL derivation, product/environment validation, credential resolution order                                                                                           |
| Documentation and packaging | The documented configuration defaults match the code, documented imports resolve, no removed-feature vocabulary survives in the docs, and CI verifies the built wheel                                           |

## Integration tests

The suite is offline in full: no network, no credentials. The `integration`
marker is registered and deselected by default (`addopts = "-m 'not
integration'"`), and no test carries it yet, so `pytest -m integration` collects
nothing today. Live behavior is recorded by hand in
[validation.md](validation.md) rather than asserted by the suite. The rules below
are what a credentialed test must satisfy when the first one is written.

An integration test must skip itself cleanly — not fail — when credentials or
the network are unavailable, so a CI run without secrets stays green.

## Rules for credentialed tests

Credentialed tests never run in CI. Any test that authenticates must follow all
of these:

1. **Credentials from the environment only.** Never in code, never in a fixture,
   never in a recorded payload.
2. **Explicit opt-in.** A test that can place an order requires an explicit
   environment flag set for that run, in addition to credentials. Keys sitting
   in the environment must not be sufficient.
3. **Smallest possible size.** Just above the instrument's minimum, and priced
   far from the market when the order is not meant to fill.
4. **Clean up in teardown.** Cancel every order in a `finally` block or fixture
   teardown, including on assertion failure, so nothing leaks between runs.
5. **State the environment.** `environment` defaults to mainnet; a test that
   wants the testnet must say so, and a test that intends mainnet must be
   deliberate about it.

## Adding a test

* Put pure-function tests next to the module they cover; they should need
  nothing but the standard library and the package.
* Prefer a recorded venue payload over a hand-written dict for anything that
  parses exchange data — the shapes are documented in the WebSocket and HTTP
  module docstrings.
* For a new rejection path in the execution client, assert both that the order
  is denied or rejected **and** that the reason names the constraint. A silent
  substitution is the bug class these tests exist to catch.

## Continuous integration

The workflow in `.github/workflows/ci.yml` runs four jobs:

1. **Test** on Python 3.12, 3.13 and 3.14 — `ruff check`, `ruff format --check`,
   both `pytest` and `python -m pytest`, and an import smoke test that exercises
   the sub-packages. Both invocations are run because they differ: a bare
   `pytest` does not put the repository root on `sys.path`, and the suite was
   red in CI for weeks while green locally before that was understood.
1a. **Minimum platform** — installs the lowest `nautilus_trader` the package
   claims to support and runs the suite against it, so the lower bound is a
   tested claim rather than a guess.
2. **Build** — cleans `dist/`, `build/` and `*.egg-info`, builds the sdist and
   wheel, runs `twine check`, then installs the wheel into a clean virtual
   environment **outside the source tree** and verifies that every sub-package
   and every documented import resolves from the installed distribution. This
   is what makes a broken `packages.find` configuration fail CI instead of
   reaching PyPI.
3. **Examples** — byte-compiles and imports every example against the installed
   package without touching the network, so an example that no longer matches
   the API fails the build. Actually executing them requires the venue, which
   is why that step is a release check rather than a CI job (see
   [releasing.md](releasing.md)).

### What the suite does not check about CI

The suite does not assert that the workflow does its job. That was tried and
abandoned on purpose: a test that reads `ci.yml` is a second implementation of
GitHub Actions' semantics, and every version of it was defeated by a workflow
that satisfied the assertions and ran nothing — steps neutered with
`continue-on-error`, commands reduced to `echo`, triggers narrowed to
`workflow_dispatch` so the job never fires on a push at all. Each round closed
the hole it was shown and left another one level up.

Whether CI does its job is observable where it happens: in the run. A red badge
on the default branch is the signal, and the release checklist re-verifies the
artefacts independently of it. What the suite does check is the package —
including the built wheel and the documented imports resolving from an install
outside the source tree, which is the failure that once reached a release.
