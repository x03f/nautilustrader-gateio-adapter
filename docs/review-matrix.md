# Review matrix

A cross-cutting review of the 0.2.0 rework recorded 52 findings. This page is the tracked state of
each one: what it was, where it lived, the test that keeps it from coming back, and the residual
risk if any remains.

It is published because an alpha should be honest about what was found rather than only about what
works. Every status here was re-derived from the source, not carried over from an earlier report —
where a tracking file and the code disagreed, the code decided.

Run any row's validation command from the repository root.

## Summary

| Status | Count | Meaning |
|---|---|---|
| Fixed | 51 | the defective behaviour is gone and a regression test fails against it |
| Accepted | 1 | a deliberate bound rather than a defect; stated in the documentation |

Severity of the 52 as first classified: 10 critical, 19 major, 21 minor, 2 nit.

## Findings


### Critical (10)

| ID | Finding | Status | Code | Regression test |
|---|---|---|---|---|
| `API-01` | Top-level `__init__.py` exports nothing — there is no public API at all | FIXED | — | `tests/test_package.py::TestInstalledWheel::test_every_documented_public_import_works_from_the_installed_wheel` |
| `DOC-01` | Docs state execution defaults to testnet; the code now defaults to mainnet — a money-losing doc inversion | FIXED | `nautilus_gateio/config.py:278` | `tests/test_docs.py::TestConfigurationReference::test_execution_environment_default_is_documented_as_mainnet` |
| `DP-1` | cancel_all_orders(side=...) also disarms every price-triggered order on the instrument, on both sides, and ... | FIXED | `nautilus_gateio/execution.py:1662` | `tests/test_execution_orders.py::TestCancelAllSideScoping::test_side_scoped_cancel_does_not_bulk_disarm` |
| `DP-2` | Order-creating, transfer and borrow POSTs are transparently retried on 5xx and network timeouts, with no ... | FIXED | `nautilus_gateio/http/client.py:80` | `tests/test_http_client.py::test_500_on_post_spot_orders_is_not_retried` |
| `EXEC-1` | Every price-triggered order loses all its fills once it fires | FIXED | `nautilus_gateio/execution.py:2117` | `tests/test_execution_events.py::TestFillBeforeOrderUpdate` |
| `EXEC-4` | Base-currency fee netting is applied to fills but not to OrderStatusReport, so spot buys never close and ... | FIXED | `nautilus_gateio/execution.py:2468` | `tests/test_execution_reports.py::TestSpotBaseFeeNettingInReports::test_report_filled_qty_matches_the_event_stream` |
| `GIO-DOM-1` | Reconciliation silently discards orders, fills and positions whose instrument the provider did not load | FIXED | `nautilus_gateio/execution.py:779` | `tests/test_execution_reports.py::TestMissingInstrumentHandling::test_unknown_instrument_is_loaded_rather_than_dropped` |
| `PKG-01` | Built wheel/sdist omits every new sub-package — pip install produces an unimportable adapter | FIXED | — | `tests/test_package.py::TestWheelContents::test_the_wheel_contains_every_source_module` |
| `md-01` | Fractional futures sizes round to zero at size_precision=0, the whole delta batch is dropped, and the local ... | FIXED | `nautilus_gateio/data.py:1156` | `tests/test_data_client.py::test_fractional_contract_size_does_not_drop_the_delta_batch` |
| `seam-01-wheel-missing-subpackages` | Built wheel/sdist omits common/, http/ and websocket/ — the installed package cannot import at all | FIXED | `nautilus_gateio/__init__.py:99` | `tests/test_package.py::TestWheelContents::test_the_wheel_contains_every_source_module` |

### Major (19)

| ID | Finding | Status | Code | Regression test |
|---|---|---|---|---|
| `CI-01` | CI's wheel verification cannot detect the broken package list (PKG-01) | FIXED | — | `tests/test_docs.py::TestCiWheelVerification::test_verification_imports_every_sub_package` |
| `DOC-02` | README, docs/ and examples/ still describe v0.1.0 in full — removed concepts, removed venue string, removed ... | FIXED | — | `tests/test_docs.py::TestNoRemovedVocabulary::test_the_kill_switch_removal_is_stated_explicitly` |
| `DP-3` | `_payload_quantity` treats the spot market-buy `amount` (a quote-currency cash amount) as a base quantity, ... | FIXED | `nautilus_gateio/execution.py:2316` | `tests/test_execution_orders.py::TestSpotMarketBuyQuoteSemantics::test_payload_quantity_uses_filled_base_not_quote_amount` |
| `DP-4` | README and docs/ still advertise the removed `live_orders` kill switch and a testnet-by-default execution ... | FIXED | `nautilus_gateio/config.py:203` | `tests/test_docs.py::TestNoRemovedVocabulary::test_the_kill_switch_removal_is_stated_explicitly` |
| `DP-5` | Price-triggered order builders silently drop reduce_only, post_only and time_in_force instead of rejecting ... | FIXED | `nautilus_gateio/execution.py:1504` | `tests/test_execution_orders.py::TestConditionalOrderRejections::test_spot_price_order_rejects_post_only` |
| `EXEC-2` | A single private-WS balance tick replaces the aggregated multi-wallet balance with one wallet's balance | FIXED | `nautilus_gateio/execution.py:2519` | `tests/test_execution_events.py::TestWalletBalanceAggregation::test_spot_tick_keeps_the_futures_wallet_contribution` |
| `EXEC-3` | A spot.orders update event restates a quote-denominated market buy back into quote units after it was ... | FIXED | `nautilus_gateio/execution.py:2263` | `tests/test_execution_orders.py::TestSpotMarketBuyQuoteSemantics::test_payload_quantity_uses_filled_base_not_quote_amount` |
| `EXEC-5` | FOK is silently downgraded to IOC on futures, delivery and options market orders | FIXED | `nautilus_gateio/execution.py:1239` | `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_futures_market_sends_fok` |
| `EXEC-6` | Futures and delivery fill reports ignore the requested lookback window and are capped at 100 rows with no ... | FIXED | `nautilus_gateio/execution.py:3659` | `tests/test_execution_reports.py::TestFillReportPagination::test_futures_fills_are_paged_beyond_the_first_hundred` |
| `GIO-DOM-2` | margin_init derived from leverage_max under-reserves initial margin by 20x-200x | FIXED | `nautilus_gateio/instruments.py:114` | `tests/test_instruments.py::TestContractMargins::test_margin_init_is_not_derived_from_leverage_max` |
| `GIO-DOM-3` | Spot price precision is clamped without a guard, producing zero prices on standard-precision NautilusTrader ... | FIXED | `nautilus_gateio/instruments.py:565` | `tests/test_instruments.py::TestStandardPrecisionGuard::test_unrepresentable_spot_pair_is_rejected` |
| `PKG-02` | pyproject version is still 0.1.0 while `__version__` is 0.2.0 | FIXED | `nautilus_gateio/__init__.py:99` | `tests/test_package.py::TestPublicApi::test_version_matches_pyproject` |
| `TEST-01` | Test suite does not collect: 10 of 13 modules import removed v0.1.0 modules | FIXED | — | — |
| `TEST-02` | Source-cleanliness scan silently stopped covering 76% of the package when it became sub-packaged | FIXED | — | `tests/test_package.py::TestSourceCleanliness::test_the_scan_covers_the_whole_tree` |
| `md-03` | A single REST error while seeding or resyncing a book kills that order-book subscription permanently | FIXED | `nautilus_gateio/data.py:950` | `tests/test_data_client.py::test_rest_failure_while_seeding_is_retried_not_fatal` |
| `seam-02-empty-top-level-init` | Top-level __init__ re-exports nothing, so every documented public import path raises ImportError | FIXED | — | `tests/test_package.py::TestPublicApi::test_every_exported_name_is_importable_from_the_package` |
| `seam-03-exec-reconnect-no-reconciliation` | Execution client's on_reconnect only refreshes balances — orders and fills missed during the disconnect are ... | FIXED | — | `tests/test_execution_events.py::TestReconnectReconciliation::test_reconnect_requeries_orders_and_fills` |
| `seam-04-snapshot-limit-not-per-product` | _request_order_book_snapshot uses the global depth table, so an options snapshot request fails at the venue ... | FIXED | — | `tests/test_data_client.py::test_options_snapshot_request_uses_the_options_depth_table` |
| `seam-05-tests-examples-target-old-layout` | Tests and examples still target the 0.1.0 flat layout — 10 of 13 test modules fail collection, so nothing ... | FIXED | — | `tests/test_docs.py::TestExamples::test_example_imports_and_defines_main` |

### Minor (21)

| ID | Finding | Status | Code | Regression test |
|---|---|---|---|---|
| `DOC-03` | config.py and data.py disagree about which book intervals spot and perpetuals accept | FIXED | `nautilus_gateio/config.py:58` | `tests/test_data_client.py::test_per_product_intervals_match_the_venue` |
| `DP-6` | Futures amend truncates a fractional contract quantity and assumes BUY sign when the order is not in the cache | FIXED | `nautilus_gateio/execution.py:1893` | `tests/test_execution_orders.py::TestFuturesAmend::test_fractional_contract_quantity_is_rejected_not_truncated` |
| `DP-7` | options.cancel_all accepts no scope at all, unlike the deliberately scoped spot and futures equivalents | FIXED | `nautilus_gateio/http/options.py:426` | `tests/test_http_namespaces.py::test_options_cancel_all_requires_a_scope` |
| `DP-8` | Report builders fetch a single 100-row page, so fills and open orders beyond the first page are silently ... | ACCEPTED | `nautilus_gateio/execution.py` | `tests/test_execution_reports.py::TestFillReportPagination::test_futures_fills_are_paged_beyond_the_first_hundred` |
| `EXEC-7` | Price-triggered orders silently drop post_only, display_qty and (on spot) reduce_only | FIXED | `nautilus_gateio/execution.py:1504` | `tests/test_execution_orders.py::TestConditionalOrderRejections::test_futures_price_order_rejects_post_only` |
| `EXEC-8` | WebSocket reconnect refreshes only the account state, leaving order and fill gaps unreconciled | FIXED | `nautilus_gateio/execution.py:683` | `tests/test_execution_events.py::TestReconnectReconciliation::test_reconnect_requeries_orders_and_fills` |
| `EXEC-9` | Fills are applied without checking whether the order is already closed | FIXED | `nautilus_gateio/execution.py:2398` | `tests/test_execution_events.py::TestFillOnClosedOrder::test_fill_after_cancel_goes_to_the_reconciliation_path` |
| `GIO-DOM-4` | Inverse contract face value is hardcoded to 1 and never consults the payload | FIXED | `nautilus_gateio/instruments.py:487` | `tests/test_instruments.py::TestNotionalArithmetic::test_inverse_face_value_prefers_a_populated_payload_value` |
| `GIO-DOM-5` | Spot pairs in the one-sided buyable/sellable states are published as fully tradable | FIXED | `nautilus_gateio/providers.py:75` | `tests/test_providers.py::TestSpotFilters::test_one_sided_pair_is_skipped` |
| `GIO-DOM-6` | The whole test suite fails to collect, so instruments.py and providers.py have zero coverage | FIXED | — | `tests/test_package.py::TestImports::test_every_module_imports` |
| `SEAM-01` | Provider uses the deprecated /spot/fee while the rest of the package prefers /wallet/fee | FIXED | `nautilus_gateio/providers.py:398` | `tests/test_providers.py::TestFeeTier::test_wallet_fee_is_preferred` |
| `SEAM-02` | `timestamp_to_nanos` is defined twice and exported twice under the same name | FIXED | `nautilus_gateio/common/parsing.py:56` | `tests/test_parsing.py::TestSingleCanonicalTimestampConversion` |
| `md-02` | apply_snapshot accepts a REST snapshot older than the book's current state, rolling the book backwards and ... | FIXED | `nautilus_gateio/books.py:316` | `tests/test_books.py::test_stale_snapshot_is_rejected_and_leaves_the_book_untouched` |
| `md-04` | A trade with no `id` field yields the fabricated TradeId "None" instead of being rejected | FIXED | `nautilus_gateio/data.py:1201` | `tests/test_data_client.py::test_trade_without_an_id_is_dropped` |
| `md-05` | A subscribe that fails transiently (mid-reconnect, ack timeout) is dropped from the replay set and never ... | FIXED | `nautilus_gateio/websocket/client.py:338` | `tests/test_websocket_client.py::test_subscribe_while_disconnected_keeps_the_subscription_for_replay` |
| `md-06` | Fractional futures BBO sizes silently become zero on QuoteTick | FIXED | `nautilus_gateio/data.py:1250` | `tests/test_data_client.py::test_book_ticker_with_an_unrepresentable_size_is_skipped` |
| `md-07` | Delivery and options bars are only released when the next bucket produces a candle, and carry a stale ts_init | FIXED | `nautilus_gateio/data.py:1325` | `tests/test_data_client.py::test_closed_bucket_is_published_on_the_clock_without_a_window_flag` |
| `md-08` | books.py and data.py have no unit tests at all, including the gap/resync algorithm the design called out | FIXED | — | `tests/test_books.py::test_full_sequence_buffers_then_applies_from_the_straddling_update` |
| `seam-06-providers-bypass-http-namespaces` | Instrument provider hand-rolls REST paths instead of using the typed HTTP namespaces, duplicating ... | FIXED | — | `tests/test_providers.py::TestUsesTypedHttpNamespaces::test_every_product_is_loaded_through_its_namespace` |
| `seam-07-duplicated-timestamp-conversion` | timestamp_to_nanos implemented three times with two different algorithms and two different ms/s thresholds | FIXED | `nautilus_gateio/common/parsing.py:56` | `tests/test_parsing.py::TestSingleCanonicalTimestampConversion` |
| `seam-08-http-client-never-closed` | The shared GateioHttpClient is created by the factory but closed by nobody — close() is dead code | FIXED | `nautilus_gateio/factories.py:88` | `tests/test_factories.py::TestSharedTransportLifecycle` |

### Nit (2)

| ID | Finding | Status | Code | Regression test |
|---|---|---|---|---|
| `PKG-03` | Stale 0.1.0 artifacts sitting in dist/ can be republished by `twine upload dist/*` | FIXED | — | `tests/test_docs.py::TestReleaseArtefactHygiene::test_build_job_cleans_before_building` |
| `seam-09-duplicated-book-depth-tables` | Per-product book interval/level tables are maintained separately in data.py and websocket/public.py | FIXED | — | `tests/test_data_client.py::test_the_data_client_reads_the_websocket_layer_tables` |

## Residual risks

Findings whose fix left something worth knowing about.

**`API-01`** — This test (test_package.py:437-449) execs `from nautilus_gateio import <all 54 names>` inside a venv that has only the installed wheel, so it fails both on an empty __init__ and on a name that exists in the tree but not in the wheel. Additional live guards: TestPublicApi::test_every_exported_name_is_importable_from_the_package and ::test_documented_entry_points_are_exported (test_package.py:144-16

**`DOC-01`** — tests/test_docs.py:141-146 asserts both halves — `GateioExecClientConfig().environment == "mainnet"` AND that the documented default cell parsed out of docs/configuration.md equals "mainnet" — so it fails on either side drifting. Backed by ::test_no_page_claims_execution_defaults_to_testnet (test_docs.py:148-162), which regex-scans every non-historical page, and by ::test_documented_defaults_match

**`DP-1`** — Four tests in TestCancelAllSideScoping (tests/test_execution_orders.py:830-923) all pass: side-scoped spot cancel asserts `not harness.spot.called("cancel_price_orders")` and that only "A-BUY" was disarmed while the SELL link survives; test_side_scoped_cancel_reports_the_disarmed_order asserts the OrderCanceled event (the second half of the finding); test_unscoped_cancel_still_bulk_disarms keeps t

**`DP-2`** — Also verified passing: test_read_timeout_on_post_spot_orders_is_not_retried, the parametrized test_no_mutating_request_is_replayed_on_5xx and test_transfer_and_borrow_are_not_replayed_on_timeout (covering /wallet/transfers and the margin/uni loan POSTs the finding named), plus the positive controls test_get_is_retried_on_5xx / test_delete_is_retried_on_5xx_because_cancelling_twice_is_harmless / te

**`EXEC-1`** — Spot conditional orders carry no client id, so a fired spot order still resolves via a REST re-read of armed price orders; that path is covered by tests but not yet mainnet-validated.

**`EXEC-4`** — The test builds the report and the fill from the same payload and asserts `report.filled_qty == fill_qty == Quantity.from_str('0.009990')` — the old code reported 0.010000, so it fails against the old behaviour. The sibling test_fully_filled_report_can_close_the_order asserts `report.order_status == FILLED and report.filled_qty == report.quantity`, which is the precise condition whose absence caus

**`GIO-DOM-1`** — Ran the whole class: `tests/test_execution_reports.py::TestMissingInstrumentHandling` = 4 passed (0.07s). The class covers fills (test_unknown_instrument_is_loaded_rather_than_dropped), orders (test_order_reports_also_load_the_instrument), positions (test_position_reports_also_load_the_instrument) and the give-up path (test_unloadable_instrument_is_dropped_after_an_attempt). These fail against the

**`PKG-01`** — The test is a genuine regression test, not a bystander: tests/test_package.py:280-318 builds a wheel from a pristine copy of the tree in a tmpdir with the repo removed from PYTHONPATH, then test_package.py:386-394 asserts every file returned by rglob is inside the archive. Against the old `packages = ["nautilus_gateio"]` it would report 19 missing modules. Companion guards: TestWheelContents::test

**`md-01`** — The test seeds a PERP book (size_precision asserted == 0 at tests/test_data_client.py:121) and feeds bids [("99","0.3"),("98","2")]; it asserts exactly one OrderBookDeltas batch, DELETE@99 with size 0, UPDATE@98 with size 2, and book_levels_not_representable == 1. Against the old code the 0.3 level became a zero-sized UPDATE, NautilusTrader raised, and no batch was published — the `len(batches) ==

**`seam-01-wheel-missing-subpackages`** — Ran `tests/test_package.py::TestWheelContents tests/test_package.py::TestInstalledWheel tests/test_package.py::TestPublicApi` -> 135 passed in 31.36s. The test genuinely fails against the old behaviour: with `packages = ["nautilus_gateio"]` the built wheel omits common/, http/, websocket/, so `expected - names` would be non-empty, and TestInstalledWheel::test_every_module_imports_from_the_installe

**`CI-01`** — tests/test_docs.py:541-545 parses the workflow, extracts the 'Verify the installed wheel...' step's heredoc, and asserts every sub-package discovered from the source tree appears in it — so adding a fourth sub-package without updating CI fails the unit suite. Against the old one-line check it fails immediately. Three more guards in the same class: ::test_verification_runs_outside_the_source_tree (

**`DOC-02`** — tests/test_docs.py is a new 620-line suite built for this finding; 156 tests, all passing. The cited node (test_docs.py:277-284) is the strictest single assertion — it requires the docs to say plainly 'no local order kill switch' and 'defaults to `\"mainnet\"`', which no v0.1.0 page could satisfy. Broader mechanical coverage, all parametrized per page/script: ::test_page_does_not_advertise_removed

**`DP-3`** — The test feeds the exact defective payload (amount="500" quote cash, filled_amount="0.005") and asserts `quantity == Quantity.from_str("0.005000")` and `quantity != Quantity.from_str("500.000000")` — it fails against the old helper. Three sibling tests also pass (tests/test_execution_orders.py:515-577): test_unfilled_market_buy_reports_no_quantity (None until something fills), test_order_status_re

**`DP-4`** — tests/test_docs.py is a genuine doc-drift guard, not a smoke test: TestNoRemovedVocabulary::test_page_does_not_advertise_removed_features (tests/test_docs.py:244) parametrizes over every doc page and fails on any line containing `live_orders`/`LiveOrdersDisabledError` unless the line also carries a removal marker; test_example_does_not_use_removed_api (:271) does the same for every example script;

**`DP-5`** — Seven tests in TestConditionalOrderRejections (tests/test_execution_orders.py:582-738) pass and each fails against the old silently-dropping builders: spot rejects post_only / display_qty / reduce_only / FOK, futures rejects post_only and display_qty, futures *keeps* reduce_only (test_futures_price_order_keeps_reduce_only), and test_spot_price_order_body_is_exact pins the whole submitted payload.

**`EXEC-2`** — Test fails against the old behaviour: it feeds a PERP tick (500 USDT) then a SPOT tick (1000 USDT) and asserts `_balances['USDT'] == (1500, 1400)`; the old assigning code would have left 1000. TestWalletBalanceAggregation (3 tests) and TestUnifiedAccountDoubleCounting (5 tests) all pass. RESIDUAL (second half of the suggested fix, NOT implemented): the derivative branch does not carry `unrealised_

**`EXEC-3`** — The test asserts `_payload_quantity` returns 0.005 BTC and explicitly `!= Quantity.from_str('500.000000')` for a payload with `amount='500'` (quote cash) — it fails against the old `amount`-reading implementation. The whole class (4 tests, including test_no_order_updated_for_a_market_order which asserts no OrderUpdated is emitted at all for a market order, and test_order_status_report_never_states

**`EXEC-5`** — The test submits a FOK MARKET on PERP and asserts the exact body `{'contract':'BTC_USDT','size':-500,'text':...,'price':'0','tif':'fok'}` — the old hard-coded `tif='ioc'` fails it. All 7 tests in the class pass, covering spot limit/market-sell/market-quote-buy FOK, futures and delivery market FOK, and options market/limit FOK rejection (asserting 'fill-or-kill' appears in the rejection reason and

**`EXEC-6`** — The test wires 150 futures fills and asserts all 150 come back and that the recorded call kwargs are `offsets == [0, 100]` — the old code made one call with no `offset` kwarg at all, so it fails. All 4 tests in the class pass, including test_futures_paging_stops_once_the_window_start_is_passed (proves the lookback window is honoured) and test_paging_is_capped. RESIDUAL, worth noting but not the de

**`GIO-DOM-2`** — Ran `tests/test_instruments.py::TestContractMargins` = 8 passed, and the named node id individually = passed. The class asserts `instrument.margin_init == Decimal(1) == CONTRACT_MARGIN_INIT` for perpetual and delivery (tests/test_instruments.py:348, :354), explicitly asserts `instrument.margin_init != Decimal(1) / leverage_max` (:361), and checks `leverage_max`/`cross_leverage_default`/`maintenanc

**`GIO-DOM-3`** — Ran `tests/test_instruments.py::TestStandardPrecisionGuard` + `::test_high_precision_pair_is_rejected_on_this_build_too` = passed (part of a 19-passed run), and the named node individually = passed. TestStandardPrecisionGuard monkeypatches `instruments.MAX_PRECISION` to 9 (tests/test_instruments.py:262-264), i.e. it simulates the stock PyPI wheel that the review venv is not, which is exactly the b

**`PKG-02`** — test_package.py:127-129 parses pyproject.toml with tomllib and asserts equality with nautilus_gateio.__version__, so it fails on any future drift; TestWheelContents::test_the_wheel_version_matches_the_package_version (test_package.py:408-409) additionally pins the built wheel's filename. Residual (not the reported defect): the version is still declared twice rather than single-sourced via `dynamic

**`TEST-01`** — No single test node can cover this: the defect was that the suite failed to *collect*, so the guard is the suite collecting at all, which .github/workflows/ci.yml:37 (`pytest -q`) enforces on every push. I verified it directly rather than via a proxy test (collect-only clean, 1237/1237 pass). Nearest adjacent guards, neither of which counts: tests/test_package.py::TestImports::test_the_module_walk

**`TEST-02`** — This is a true regression test: against the old non-recursive glob it would find 8 files and fail on `len(files) >= 25`, and it would also fail the per-subpackage assertion at test_package.py:185-188. Its docstring (test_package.py:181-182) names the exact defect: 'a scan that silently stopped recursing when the package became sub-packaged (it then covered 8 of 27 files)'.

**`md-03`** — The test makes the first `_fetch_book_snapshot` raise GateioClientError(502) and the second succeed, then asserts 2 attempts, `is_synced`, that deltas were published, and snapshot_errors["PERP"] == 1. Against the old code the first error escaped the retry loop to the catch-all, which only logged, so attempts == 1 and no deltas — the test fails. Also verified passing: test_persistent_rest_failure_s

**`seam-02-empty-top-level-init`** — Ran as part of the TestPublicApi/TestWheelContents/TestInstalledWheel batch -> 135 passed. Two independent guards would fail against the old empty __init__: TestPublicApi::test_documented_entry_points_are_exported (test_package.py:150-169) pins the 13 documented names into __all__, and TestInstalledWheel::test_the_documented_quick_start_imports_work_from_the_installed_wheel (test_package.py:451-46

**`seam-03-exec-reconnect-no-reconciliation`** — Ran `tests/test_execution_events.py::TestReconnectReconciliation` -> 4 passed in 0.10s. test_reconnect_requeries_orders_and_fills (test_execution_events.py:693-718) asserts `env.perp.called("list_orders")` and `env.perp.called("my_trades")` and that an OrderStatusReport plus the FillReport TradeId('T-GAP') reached the engine — none of which happens under the old balances-only handler. test_reconne

**`seam-04-snapshot-limit-not-per-product`** — Ran together with test_nearest_snapshot_limit_is_per_product and test_the_data_client_reads_the_websocket_layer_tables plus TestUsesTypedHttpNamespaces -> 15 passed. The test asserts the recorded options REST call is `{'contract': OPTION_SYMBOL, 'limit': 50, 'with_id': True}` (tests/test_data_client.py:727); under the old global table the default snapshot limit of 100 would have been passed straig

**`seam-05-tests-examples-target-old-layout`** — Verified by running the whole suite (1237 passed) — this parametrised test executes `importlib.util.spec_from_file_location` + `exec_module` on each of the six example scripts, so an example still importing `nautilus_gateio.constants`/`.signing`/`.schemas` fails it. tests/test_docs.py::TestNoRemovedVocabulary::test_example_does_not_use_removed_api (line 270-271) is a second guard, and tests/test_p

**`DP-6`** — Both halves of the finding are covered and both tests pass: the fractional test amends a SELL 10 to Quantity 2.5 and asserts an OrderModifyRejected containing "whole contracts" plus `not env.perp.called("amend_order")` (the old code would have sent size=-2); test_unknown_order_is_rejected_instead_of_assuming_buy deliberately keeps the order out of the cache and asserts the rejection reason contain

**`DP-7`** — Five tests pass and cover the guard and its edges: test_options_cancel_all_requires_a_scope (bare call raises), test_options_cancel_all_rejects_a_side_only_scope (side="bid" is not a scope), test_options_cancel_all_rejects_blank_scope_values (parametrized over blank strings/None), and the positive controls test_options_cancel_all_accepts_a_contract_scope / ..._an_underlying_scope. The first three

**`DP-8`** — Report paging stops at 20 pages (2000 rows) and logs a warning naming the cap. An account with more open orders or fills than that in one listing window reports a truncated view. Documented as a limitation for the alpha.

**`EXEC-7`** — The test submits a post-only STOP_LIMIT on PERP and asserts exactly one rejection containing 'post-only' AND that `create_price_order` was never called — the old code silently dropped the flag and submitted, so it fails. All 8 tests in the class pass: spot rejects post_only / display_qty / reduce_only / FOK, futures rejects post_only / display_qty, futures KEEPS reduce_only, and the exact spot bod

**`EXEC-8`** — The test runs `_reconcile_after_reconnect` and asserts `env.perp.called('list_orders')`, `env.perp.called('my_trades')`, that an OrderStatusReport was emitted and that the gap fill T-GAP came through as a FillReport — the old account-state-only implementation never touches those endpoints, so it fails. All 4 tests in the class pass, including test_reconnect_uses_the_last_stream_event_as_the_window

**`EXEC-9`** — The test cancels the order via the order stream, then delivers the fill, and asserts `env.events_of(OrderFilled) == []` while exactly one FillReport with trade_id T-LATE and venue_order_id 900001 was emitted — the old code called generate_order_filled on the CANCELED order, so it fails. The sibling test_a_duplicate_late_fill_is_not_reported_twice pins the re-seeded dedup. Both pass.

**`GIO-DOM-4`** — Ran the named node individually = passed. The test (tests/test_instruments.py:447-472, docstring "GIO-DOM-4: a real `quanto_multiplier` must win over the fallback") feeds `quanto_multiplier: "10"` on an inverse contract and asserts `instrument.multiplier == Quantity.from_int(10)` and `!= INVERSE_CONTRACT_FACE_VALUE`, plus the base-denominated notional 7*10/64000. Against the old hardcoded `return

**`GIO-DOM-5`** — Ran the named node individually = passed, and `tests/test_providers.py::TestSpotFilters` as a whole = passed. It is parametrized over ["buyable", "sellable", "BUYABLE"] (tests/test_providers.py:356-364) and asserts the pair is absent and `provider.count == 1`; against the old `trade_status == "untradable"`-only filter the pair is published and the test fails. Sibling tests cover buy_start/sell_sta

**`GIO-DOM-6`** — Ran the named node = passed. Caveat, stated plainly: `test_every_module_imports` is parametrized over every SOURCE module and guards the package tree, not test-module collection — it is the closest standing guard but it is not a strict regression test for "the test suite does not collect". The decisive evidence is the run itself (1237 collected, 1237 passed, 0 errors) plus the existence and passin

**`SEAM-01`** — Ran the named node individually = passed, and `tests/test_providers.py::TestFeeTier` (4 tests, header comment "SEAM-01: the account fee tier comes from /wallet/fee", tests/test_providers.py:264) = passed. `test_wallet_fee_is_preferred` spies on `GateioWalletHttpAPI.fee` and asserts `seen == ["GateioWalletHttpAPI.fee"]`, `"/wallet/fee" in http.paths` AND `"/spot/fee" not in http.paths` (:285-286) —

**`SEAM-02`** — Measured 64 ns data/execution divergence is gone; both paths now resolve to one function object.

**`md-02`** — The unit test applies a snapshot at id 100, then a `full: true` stream message advancing the book to id 500, then asserts `pytest.raises(SnapshotStaleError)` for a REST snapshot at id 400 and that best_bid/best_ask/last_update_id are unchanged and snapshots_stale == 1. Old code applied it unconditionally, so no exception is raised and the assertions fail. Also verified: test_books.py::test_snapsho

**`md-04`** — Feeds a spot.trades update with no `id` key and asserts `harness.trades() == []` and trade_ticks_skipped == 1. Old code published a TradeTick with trade_id "None", so `trades() == []` fails. Also verified passing: test_trade_with_an_empty_id_is_dropped (the `""` case) and test_trade_with_an_id_keeps_the_venue_id_verbatim (guards against over-correction).

**`md-05`** — Asserts the raised GateioError has label WS_NOT_CONNECTED and that `client.subscriptions` still contains the channel afterwards. Old code popped it on any exception, so the subscriptions assertion fails. Also verified passing: test_subscribe_ack_timeout_keeps_the_subscription_for_replay (WS_ACK_TIMEOUT), test_subscribe_rejected_by_the_venue_drops_the_subscription (asserts the venue-rejection path

**`md-06`** — Parametrized over ('0.4','7'), ('12','0.4'), ('0','7'), (None,'7'), ('12',''), it asserts `harness.quotes() == []` and quote_ticks_skipped == 1 for each. Against the old code the fractional 0.4 case published a QuoteTick with size 0, so `quotes() == []` fails. The complementary test_book_ticker_publishes_a_quote_with_both_sizes confirms a valid quote still gets through with both sizes intact.

**`md-07`** — Feeds a single options.contract_candlesticks candle (no `w` flag) for a bucket that closed ten minutes ago and asserts exactly one Bar is published, that ts_event is the bucket close, that `ts_init >= ts_event` (the re-stamp), and that nothing remains in `_bar_pending`. Old code parked it and published nothing until a newer bucket arrived, so `len(bars) == 1` fails. Also verified passing: test_an_

**`md-08`** — Ran the parametrized node ids [spot] and [futures] plus test_gap_raises_order_book_sequence_error_and_unsyncs[spot] — all pass. For this finding the test IS the fix, so 'would fail against the old behaviour' is trivially true: neither file existed, and collection would error. Named node run: `pytest 'tests/test_books.py::test_full_sequence_buffers_then_applies_from_the_straddling_update[futures]'

**`seam-06-providers-bypass-http-namespaces`** — Ran `tests/test_providers.py::TestUsesTypedHttpNamespaces` (5 tests) as part of a 15-passed batch. The test monkeypatch-spies the namespace methods and asserts the exact call sequence ['GateioSpotHttpAPI.currency_pairs', 'GateioFuturesHttpAPI.contracts' x3, 'GateioOptionsHttpAPI.underlyings/expirations/contracts'] (test_providers.py:181-200) — hand-rolled `self._http.get(...)` calls never touch th

**`seam-07-duplicated-timestamp-conversion`** — A test asserts the tree defines the conversion exactly once, so a re-introduced copy fails the suite.

**`seam-08-http-client-never-closed`** — The instrument provider holds the transport without acquiring; it never outlives the client that built it, so the count stays balanced.

**`PKG-03`** — test_docs.py:575-592 parses the build job's steps and asserts a step matching `rm -rf .*dist/` exists, that a `python -m build` step exists, that the clean comes *before* the build, and that the clean block also removes build/ and egg-info. Against the old workflow (no clean step at all) it fails. Two companions: ::test_release_guide_documents_the_clean_and_a_pinned_upload (test_docs.py:600-608, r

**`seam-09-duplicated-book-depth-tables`** — Ran in the 15-passed batch. The test asserts object identity — `data_module.BOOK_LEVELS is public_module.BOOK_LEVELS` and the same for BOOK_INTERVALS_MS (test_data_client.py:753-756) — which two separately maintained literal dicts can never satisfy, so it fails against the old behaviour. Residual (not part of this finding): config.py:61 still hard-codes `ORDER_BOOK_UPDATE_INTERVALS_MS = (20, 100,
