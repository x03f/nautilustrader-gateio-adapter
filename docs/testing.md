# Testing

## Running the unit tests

```bash
pip install -e '.[dev]'
pytest
```

The default run executes the unit suite only: `pyproject.toml` sets
`addopts = "-m 'not integration'"`, so anything marked `integration` is
deselected automatically. Unit tests use no network access and no
credentials — all exchange payloads are synthetic fixtures (see
`tests/conftest.py`: the `FakeMarket` data source and the `btc_spec` pair
specification).

Requirements: Python >= 3.12 and the pinned `nautilus_trader` range from
`pyproject.toml` (`>=1.230.0,<2`).

## What is covered

| Test module | Covers |
|---|---|
| `test_config.py` | Config defaults, credential resolution order, testnet/mainnet URL derivation |
| `test_errors.py` | Error hierarchy, `error_from_response` mapping, `should_retry` classification |
| `test_factories.py` | Data/exec client factory construction |
| `test_http_client.py` | REST client behavior against mocked transports: signing headers, error translation, `live_orders` gating, order validation |
| `test_paper.py` | Paper simulator: book-walking fills, slippage, fees, minimum checks, balance accounting |
| `test_providers.py` | Instrument building from pair specs, currency fallback, static provider |
| `test_ratelimit.py` | Rate-limiter pacing and 429 backoff behavior |
| `test_schemas.py` | Payload parsers (orders, candles, balances, fills, futures contracts) and `validate_order` |
| `test_signing.py` | HMAC-SHA512 signature vectors, client-order-id generation and sanitization |
| `test_symbols.py` | Instrument-id/pair conversion, underscore and compatibility forms, error cases |
| `test_websocket.py` | WS message handling: closed-bar filter, dedup, out-of-order drop, gap detection, backoff schedule |

## Integration tests

Policy: tests that talk to the real exchange are marked
`@pytest.mark.integration` and are **skipped by default**. They only run
when explicitly selected:

```bash
export GATE_TESTNET_API_KEY=...      # testnet credentials only
export GATE_TESTNET_API_SECRET=...
pytest -m integration
```

An integration test must skip itself cleanly (not fail) when the required
credentials are absent, so CI without secrets stays green.

## Safety rules for credentialed tests

Any test that authenticates against the exchange must follow all of these:

1. **Never mainnet.** Credentialed tests target the testnet host only
   (`GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET` against
   `https://api-testnet.gateapi.io`). Mainnet credentials must never appear
   in a test environment.
2. **Tiny notionals.** Any order placed by a test uses the smallest size the
   instrument allows (just above the exchange minimum notional), priced far
   from the market when the order is not meant to fill.
3. **Cancel in teardown.** Every test that places orders cancels them in a
   `finally` block or fixture teardown — including on assertion failure —
   so no state leaks between runs. `GateioHttpClient.cancel_all` exists for
   exactly this.
4. **Explicit order-flow opt-in.** Order-placing code paths still require
   the `live_orders=True` switch; tests must not work around it.
5. **No credentials in code or fixtures.** Credentials come from the
   environment only; recorded payloads and fixtures must be synthetic or
   scrubbed.
