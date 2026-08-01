# Contributing

Bug reports, documentation corrections, tests and features are all welcome. This
is an external community adapter written in pure Python against NautilusTrader
1.230.0 — it is not an official NautilusTrader integration and is not affiliated
with Gate.io.

## What helps most right now

The released `0.2.0a1` carries a bounded mainnet record: spot, one USDT
perpetual, one option contract, and nothing beyond it — and nothing added to the
branch since has been run against the venue at all. Inverse perpetuals, delivery
futures and every margin ledger have never had an order sent. A passing suite is
evidence about the code rather than about the exchange, so the most valuable
contribution is evidence from the exchange:

* **Validation results.** If you exercise a path against the real venue, a pull
  request adding a row to [docs/validation.md](docs/validation.md) — date,
  product, instrument, what was submitted, what the venue did — moves the
  project further than most code changes. Products with no testnet endpoint
  (inverse perpetuals, delivery futures, options) are the least covered.
* **Divergences between the documentation and reality.** A page that describes
  an intention rather than the behavior is a defect; report it as one.
* **Reproducible bugs**, with the smallest snippet that shows them.

The README feature matrix states what is unsupported, and
[docs/validation.md](docs/validation.md) lists the paths that cannot easily be
validated and why. Those two are more reliable starting points than the [roadmap](docs/roadmap.md),
which goes stale.

## Development setup

```bash
git clone https://github.com/x03f/nautilustrader-gateio-adapter.git
cd nautilustrader-gateio-adapter
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### The version on this branch

The default branch carries a development version — `0.2.0a2.dev0` — which is
[PEP 440](https://peps.python.org/pep-0440/) for *after `0.2.0a1`, before the
next alpha*. It is deliberately not the released number, so a build of this
branch can never claim to be the release, and it is not bumped per commit:
`pip freeze` names the commit for a git install, which is the finer-grained
answer. A release sets it, following [docs/releasing.md](docs/releasing.md). A
pull request leaves it alone.

Python >= 3.12 and < 3.15, with `nautilus_trader` >= 1.230.0 and < 2, as pinned
in `pyproject.toml` on this branch. CI runs the whole of that range: 3.12, 3.13
and 3.14, plus one job that pins `nautilus_trader==1.230.0` so the declared
lower bound is tested rather than assumed. Each of those jobs runs the suite
twice, as `pytest` and as `python -m pytest` — the two build different
`sys.path`s, and the difference has hidden a broken suite before.

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

* **Test the behavior you change.** A bug fix needs a regression test that
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

On architecture: this package is Python throughout, and the reasoning is in
[docs/architecture.md](docs/architecture.md#the-deliberate-python-only-architecture).
A partial rewrite along other lines is not a change to open unannounced.

## Reporting an issue

Use the issue template, which asks for the adapter build — the line `pip freeze`
prints, which names the commit on a git install — and the `nautilus_trader`
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

## Asking a question

Questions belong in the tracker, on the **Question** form
([new issue](https://github.com/x03f/nautilustrader-gateio-adapter/issues/new/choose)).
Blank issues are turned off and Discussions are not enabled on this repository,
so that form is the route — and a question that turns out to be a defect is
relabeled rather than turned away.

## Code of conduct

Participation here is governed by the
[Contributor Covenant 2.1](CODE_OF_CONDUCT.md), unmodified. Behavior that
violates it is reported through the same private channel as a security problem,
or as an issue when it does not need to be private; the
[Enforcement](CODE_OF_CONDUCT.md#enforcement) section has both.

## License

MIT — see [LICENSE](LICENSE). Contributions are accepted under the same license;
there is no contributor license agreement, and you keep the copyright in what
you write.
