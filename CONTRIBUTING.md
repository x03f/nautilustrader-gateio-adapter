# Contributing

Thank you for your interest in improving the Gate.io adapter for NautilusTrader.
Contributions of all kinds are welcome: bug reports, documentation fixes, tests,
and new features.

## Development setup

```bash
git clone https://github.com/x03f/nautilustrader-gateio-adapter.git
cd nautilustrader-gateio-adapter
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Python 3.12 or 3.13 is required (matching `nautilus_trader` support).

## Running tests

```bash
pytest
```

Unit tests must not require network access or credentials — integration tests
that need Gate.io credentials are marked with `@pytest.mark.integration` and are
deselected by default (see `pyproject.toml`).

## Linting and formatting

```bash
ruff check .
ruff format .
```

CI runs both; please make sure they pass locally before opening a pull request.

## Pull request guidelines

- Include tests for any behavior change. Bug fixes should include a regression test.
- Keep the feature matrix in `README.md` honest — if you add or change a
  capability, update the matrix and the relevant page under `docs/` in the
  same pull request.
- Never commit credentials, API keys, or account identifiers — not even in test
  fixtures or recorded responses. Use placeholder values.
- All code, comments, docstrings, and documentation must be in English.
- Keep changes focused; unrelated refactoring belongs in a separate pull request.
- Add a short entry to `CHANGELOG.md` under `[Unreleased]`.

## Code style

- Type annotations on all public functions and methods (the package ships `py.typed`).
- Docstrings on public classes and functions.
- No network calls in unit tests — use fakes or recorded payloads with
  placeholder data.
- Follow the existing module layout; prefer small, composable functions.

## Roadmap: contributions especially welcome

These areas are known gaps where pull requests are particularly appreciated:

- Private (authenticated) WebSocket channels for order and balance updates.
- Order-book depth streams and book deltas.
- Real-time quote and trade subscriptions beyond the current channel set.
- Stop / conditional order types.
- Integrating the experimental futures REST client with the Nautilus
  execution path.
- Start-up order and position reconciliation improvements.

If you plan a larger change, please open an issue first to discuss the design.
