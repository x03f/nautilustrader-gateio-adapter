# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/x03f/nautilustrader-gateio-adapter/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/x03f/nautilustrader-gateio-adapter/releases/tag/v0.1.0
