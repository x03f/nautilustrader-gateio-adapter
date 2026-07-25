# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-26

A rewrite. 0.1.0 was a spot-only adapter with a flat module layout; 0.2.0 is a
multi-product connector built on real venue data throughout. See
[docs/migration-0.1-to-0.2.md](docs/migration-0.1-to-0.2.md) for the upgrade
path.

### Changed (breaking)

- **Venue string is now `GATE_IO`** (was `GATEIO`), matching how NautilusTrader
  identifies this exchange elsewhere. Every instrument id and bar type changes:
  `BTC_USDT.GATEIO` becomes `BTC_USDT.GATE_IO`.
- **Perpetual instrument ids carry a `-PERP` suffix** (`BTC_USDT-PERP.GATE_IO`).
  527 USDT perpetuals share their exact symbol with a spot pair, so the two must
  be distinguishable. Delivery and option symbols carry their expiry and take no
  suffix. An id without the suffix now resolves to the spot pair.
- **Execution defaults to mainnet** (was testnet). An execution client that
  silently points at a different exchange environment than the operator believes
  is more dangerous than one that requires the venue to be stated. Set
  `environment="testnet"` explicitly for the testnet, which serves spot and USDT
  perpetuals only; configuring any other product with it now raises `ValueError`
  before any network activity.
- **The `live_orders` kill switch and `LiveOrdersDisabledError` are removed.** A
  boolean inside the process is not a security boundary. The controls that bind
  are API key permissions, IP allow-listing, an explicitly chosen `environment`,
  and NautilusTrader's own sandbox/backtest execution.
- **Synthetic quotes are removed.** `emit_synthetic_quotes` is gone; quotes now
  come from the venue's real `book_ticker` best bid/offer stream. Nothing
  fabricated is published as venue data.
- **The paper-fill simulator is removed** (`PaperExecution`, `PaperFill`,
  `GateioPaperConfig`). Use NautilusTrader sandbox or backtest execution.
- **The standalone `reconcile()` helper is removed**, superseded by the real
  NautilusTrader report generators.
- **The package is sub-packaged**: `nautilus_gateio.common`, `.http`,
  `.websocket`. The top-level `__init__` re-exports the public API, so
  `from nautilus_gateio import GateioDataClient` keeps working; deep imports of
  the old flat modules must be updated.
- **The REST client is async and namespaced.** `GateioHttpClient` is a shared
  `async` transport; per-product calls live on `GateioSpotHttpAPI`,
  `GateioMarginHttpAPI`, `GateioFuturesHttpAPI`, `GateioOptionsHttpAPI` and
  `GateioWalletHttpAPI`. `ping()`, `balances()`, `open_orders()`,
  `place_order_validated()`, `cancel_all()` and `emergency_stop()` no longer
  exist on the client.
- **`GateioWebSocketClient` takes the endpoint and the product it serves**,
  because a Gate.io message is only interpretable together with the host it
  arrived on. Prefer `GateioPublicWebSocket` / `GateioPrivateWebSocket`.
- **Configuration fields changed.** Removed: `venue`, `use_websocket`,
  `poll_interval_secs`, `emit_synthetic_quotes`. Renamed:
  `account_poll_interval_secs` to `account_polling_interval_secs` (default
  30.0). Added: `products`, `options_underlyings`, `environment` on the data
  client, `base_url_ws`, `spot_account_mode`, `update_instruments_interval_mins`,
  `http_timeout_secs`, `max_retries`, `order_book_snapshot_limit`,
  `order_book_update_interval_ms`, `bars_timestamp_on_close`.
- Renamed public symbols: `instrument_id_to_gate_pair` to
  `instrument_id_to_gateio`, `gate_pair_to_instrument_id` to
  `gateio_to_instrument_id`, `build_currency_pair` to `parse_spot_instrument`,
  the futures clients to `GateioFuturesHttpAPI`, `GATEIO_WS_MAINNET` to
  `GATEIO_WS_SPOT`. `StaticInstrumentProvider` is removed.
- **Market orders are no longer emulated with a fixed 1% cross.** Spot market
  sells and quote-denominated market buys are native venue market orders; only a
  base-denominated spot market buy is expressed as an IOC limit, bounded by the
  pair's own published slippage cap.

### Added

- **Products**: spot, USDT perpetual futures, BTC-settled (inverse) perpetual
  futures, USDT delivery futures and USDT-settled options. One data client and
  one execution client multiplex every configured product.
- **Spot margin as an execution mode** (`spot_account_mode`): plain spot,
  isolated margin, cross margin and unified account, with the balance, borrow
  and repay endpoints each ledger needs.
- **Real market data**: trade ticks, best bid/offer quote ticks,
  sequence-validated order book deltas with resync on gap, closed bars from 1s
  to 7d, mark prices, index prices and funding rates.
- **Order types**: MARKET, LIMIT, STOP_MARKET, STOP_LIMIT, MARKET_IF_TOUCHED and
  LIMIT_IF_TOUCHED, the last four through each product's price-trigger endpoint.
  Time in force GTC, IOC and FOK, post-only via `poc`, plus `reduce_only`,
  `display_qty` (iceberg) and `quote_quantity` on a spot market buy.
- **Order modification** on spot and perpetuals; delivery and options reject it
  explicitly, because the venue has no amend endpoint there.
- **Private WebSocket** as the primary execution event source: orders,
  usertrades, balances and positions per product, with REST reconciliation after
  every reconnect.
- **Full reconciliation**: `generate_order_status_reports`,
  `generate_order_status_report`, `generate_fill_reports` and
  `generate_position_status_reports`, all against REST.
- **Internal wallet transfers** (`GateioExecutionClient.transfer`) between the
  account's own trading wallets, which is also how Gate.io creates the
  derivative wallets in the first place.
- Instruments for every product: `CurrencyPair`, `CryptoPerpetual` (linear and
  inverse), `CryptoFuture` and `CryptoOption`, with contract-count quantity
  semantics and the venue's multipliers.
- Documentation set rewritten against the current code, plus new pages on
  [symbology](docs/symbology.md), [products](docs/products.md),
  [migration](docs/migration-0.1-to-0.2.md), [validation
  status](docs/validation.md) and [releasing](docs/releasing.md).

### Fixed

- **Documentation described a testnet default that the code does not have.**
  Every page now states the mainnet default, and a regression test compares the
  documented configuration defaults against the actual struct fields.
- **Documentation advertised removed features** — the `live_orders` kill switch,
  synthetic quotes, the paper module, the standalone reconciliation helper and
  the old venue string. All removed, with a test that fails if the vocabulary
  reappears.
- **CI could not detect a broken package list.** The wheel verification now
  installs into a clean environment outside the source tree and imports each
  sub-package and every documented entry point, so a distribution missing
  `nautilus_gateio.common`, `.http` or `.websocket` fails the build.
- **Stale artefacts could be republished.** The build job removes `dist/`,
  `build/` and `*.egg-info` before building, and the release checklist uploads a
  version-pinned glob instead of `dist/*`.
- Examples rewritten against the current API; the credential-free ones are run
  as part of the release checklist.

### Security

- Order-mutating REST requests are never replayed automatically; an ambiguous
  outcome is reported as ambiguous rather than resubmitted.
- Hedge (dual) position mode is detected and refused, never switched on; a
  unified account is never upgraded automatically.
- No withdrawal, sub-account transfer, Earn, Gate Pay, P2P, Copy Trading or Gate
  Bots code exists in the package.

## [0.1.0] - 2026-07-25

Initial release.

### Added

- Spot market-data client (`GateioDataClient`) with WebSocket streaming and REST polling fallback for trade ticks and quotes.
- Spot execution client (`GateioExecutionClient`) with a testnet-first design: live mainnet orders are disabled unless explicitly enabled in configuration.
- Instrument provider (`GateioInstrumentProvider`) loading spot currency pairs from the Gate.io REST API, plus a `StaticInstrumentProvider` for offline use.
- Reusable HTTP client (`GateioHttpClient`) with retry handling and a token-bucket `RateLimiter`.
- Reusable WebSocket client (`GateioWebSocketClient`) with automatic reconnection and subscription replay.
- Gate.io API v4 request signing (`sign_request`) and client order ID generation/sanitization helpers.
- Order validation with typed errors (`OrderValidationError`, `LiveOrdersDisabledError`) and retry classification (`should_retry`).
- Rate limiting applied across REST endpoints to respect Gate.io API limits.
- Local paper-fill simulator (`PaperExecution`) for testing strategies without sending orders to the exchange.
- Experimental futures REST client (not integrated with the Nautilus execution path).
- Live client factories (`GateioLiveDataClientFactory`, `GateioLiveExecClientFactory`) for `TradingNode` integration.
- Order state reconciliation helper (`reconcile`).
- Documentation set: architecture, configuration, market data, execution, testing, and troubleshooting guides.
- Unit test suite (no network access required) and continuous integration workflow.

[Unreleased]: https://github.com/x03f/nautilustrader-gateio-adapter/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/x03f/nautilustrader-gateio-adapter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/x03f/nautilustrader-gateio-adapter/releases/tag/v0.1.0
