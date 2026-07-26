# Contributing

Bug reports, documentation corrections, tests and features are all welcome. This
is an external community adapter written in pure Python against NautilusTrader
1.230.0 — it is not an official NautilusTrader integration and is not affiliated
with Gate.io.

## What helps most right now

`0.2.0a1` is an alpha with an extensive offline test suite and **no recorded
validation against the live venue**. A passing suite is evidence about the code,
not about the exchange, so the most valuable contribution is evidence from the
exchange:

* **Validation results.** If you exercise a path against the real venue, a pull
  request adding a row to [docs/validation.md](docs/validation.md) — date,
  product, instrument, what was submitted, what the venue did — moves the
  project further than most code changes. Products with no testnet endpoint
  (inverse perpetuals, delivery futures, options) are the least covered.
* **Divergences between the documentation and reality.** A page that describes
  an intention rather than the behaviour is a defect; report it as one.
* **Reproducible bugs**, with the smallest snippet that shows them.

The README feature matrix states what is unsupported, and
[docs/validation.md](docs/validation.md) lists the paths that cannot easily be
validated and why. Those two are more reliable starting points than a roadmap,
which goes stale.

## Development setup

```bash
git clone https://github.com/x03f/nautilustrader-gateio-adapter.git
cd nautilustrader-gateio-adapter
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Python >= 3.12 and < 3.15, with `nautilus_trader` >= 1.230.0 and < 2, as pinned
in `pyproject.toml`. CI runs on 3.12 and 3.13.

## Running the tests

```bash
pytest                      # the offline suite
pytest tests/test_docs.py   # the documentation invariants
pytest -m integration       # credentialed tests, deselected by default
```

The default run uses no network and no credentials: `pyproject.toml` sets
`addopts = "-m 'not integration'"`, and every exchange payload in the suite is a
fixture. An integration test must skip itself cleanly — never fail — when
credentials or the network are absent, so a run without secrets stays green.

`tests/test_docs.py` fails when a page promises something the code no longer
does: it checks the documented configuration defaults against the real structs,
that every documented import resolves, that removed vocabulary has not survived,
and that the feature matrix uses the closed status vocabulary. Run it after any
documentation change. [docs/testing.md](docs/testing.md) has the full picture,
including the rules a credentialed test must follow.

## Linting and formatting

Formatting and lint rules are configured in `pyproject.toml`: `ruff`, line
length 100, rule sets `E`, `F`, `W`, `I`, `UP`, `B`. CI runs exactly:

```bash
ruff check nautilus_gateio tests examples
ruff format --check nautilus_gateio tests examples
```

Use `ruff format nautilus_gateio tests examples` to apply the formatting.

## Pull requests

* **Test the behaviour you change.** A bug fix needs a regression test that
  fails without the fix.
* **Update the documentation in the same pull request.** If a capability
  changes, the README feature matrix and the relevant page under `docs/` change
  with it. The status vocabulary is closed and enforced; nothing becomes
  *Stable* without a recorded mainnet result in
  [docs/validation.md](docs/validation.md).
* **Add a `CHANGELOG.md` entry** under `[Unreleased]` for anything user-visible.
* **No credentials, account identifiers or real account data** in code, tests,
  fixtures or recorded payloads. Placeholders only.
* **English throughout** — code, comments, docstrings, documentation.
* **Type annotations on public functions and methods** (the package ships
  `py.typed`), and docstrings on public classes and functions. Prefer small,
  composable functions and follow the existing module layout.
* **Keep it focused.** Unrelated refactoring belongs in its own pull request.
* **Open an issue first for a larger design change**, so the discussion happens
  before the work.

On architecture: NautilusTrader prefers a Rust core with a thin PyO3 layer for
the adapters it ships in-tree. This package is Python throughout — a deliberate
choice for an external package, explained in
[docs/architecture.md](docs/architecture.md), which does not exempt it from any
behavioural requirement. A Rust migration is a possible future project and is
not being promised, so a partial Rust rewrite is not a change to open
unannounced.

## Reporting an issue

Use the issue template, which asks for the adapter version, the `nautilus_trader`
version, the Python version, the operating system and the environment. Beyond
that, a report that can be acted on states which product and which path
(subscription, order submission, reconciliation), what happened, what you
expected instead, a minimal reproduction, and the relevant traceback.

Leave out, in the issue and in anything attached to it:

* API keys and secrets;
* the account's numeric user id, and any other account identifier;
* balances and position sizes;
* venue order ids and client order ids;
* raw authenticated responses.

Replace them with placeholders and describe the shape instead. An issue is
public and permanent, and adapter logs contain more account detail than is
obvious at a glance — [SECURITY.md](SECURITY.md) explains exactly which parts
and why.

Security problems do not go in an issue: report them privately, as described in
[SECURITY.md](SECURITY.md).

## Licence

MIT — see [LICENSE](LICENSE). Contributions are accepted under the same licence;
there is no contributor licence agreement, and you keep the copyright in what
you write.
