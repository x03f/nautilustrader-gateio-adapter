# Review matrix

A cross-cutting review of the 0.2.0 rework recorded 52 findings. This page is the tracked state of
each one: what it was, where it lived, the test that keeps it from coming back, and the residual
risk if any remains.

It is published because an alpha should be honest about what was found rather than only about what
works. Every status here was re-derived from the source, not carried over from an earlier report —
where a tracking file and the code disagreed, the code decided.

Findings raised later, by the rounds of work on recovery and the attempts to refute them, are in
[a section of their own](#recovery-findings-raised-after-this-review) at the end. Four of those are
open. This page is not a claim that nothing is wrong; it is a claim that what is known is written
down.

Run any row's validation command from the repository root.

## Summary

| Status | Count | Meaning |
|---|---|---|
| Fixed | 51 | the defective behaviour is gone and a regression test fails against it |
| Accepted | 1 | a deliberate bound rather than a defect; stated in the documentation |

Severity of the 52 as first classified: 10 critical, 19 major, 21 minor, 2 nit. The four open
recovery findings below are outside this count.

## Findings


### Critical (10)

| ID | Finding | Status | Code | Regression test |
|---|---|---|---|---|
| `API-01` | Top-level `__init__.py` exports nothing — there is no public API at all | FIXED | — | `tests/test_package.py::TestInstalledWheel::test_every_documented_public_import_works_from_the_installed_wheel` |
| `DOC-01` | Docs state execution defaults to testnet; the code now defaults to mainnet — a money-losing doc inversion | FIXED | `nautilus_gateio/config.py:278` | `tests/test_docs.py::TestConfigurationReference::test_execution_environment_default_is_documented_as_mainnet` |
| `DP-1` | cancel_all_orders(side=...) also disarms every price-triggered order on the instrument, on both sides, and ... | FIXED | `nautilus_gateio/execution.py:1662` | `tests/test_execution_orders.py::TestCancelAllSideScoping::test_side_scoped_cancel_does_not_bulk_disarm` |
| `DP-2` | Order-creating, transfer and borrow POSTs are transparently retried on 5xx and network timeouts, with no ... | FIXED | `nautilus_gateio/http/client.py:80` | `tests/test_http_client.py::test_500_on_post_spot_orders_is_not_retried` |
| `EXEC-1` | Every price-triggered order loses all its fills once it fires | FIXED | `nautilus_gateio/execution.py:2117` | `tests/test_execution_events.py::TestFillBeforeOrderUpdate` |
| `EXEC-4` | Base-currency fee netting is applied to fills but not to OrderStatusReport, so spot buys never close and ... | SUPERSEDED — the netting itself was the defect: the platform takes a base-currency commission off the position, so netting it off `last_qty` too subtracted it twice. Neither the fill nor the report nets it now. | `nautilus_gateio/execution.py` `_fill_quantity_and_commission` | `tests/test_execution_events.py::TestSpotBaseFeeReporting::test_the_position_is_short_by_the_fee_exactly_once` |
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

## Recovery findings raised after this review

The 52 above came from one cross-cutting review of the 0.2.0 rework. Recovery and reconciliation
have since been worked on in rounds, each round ending with an independent attempt to refute it, and
those attempts raise findings of their own. They are recorded here because this page is where a
reader looks for what is known to be wrong, and kept separate because they are not part of that
review and are not closed.

| ID | Finding | Status | Code | Evidence |
|---|---|---|---|---|
| `REC-01` | The unapplied-fill sweep ran only after a WebSocket reconnect, so on the startup path the engine's deduplication dropped a venue-confirmed trade and nothing re-offered it: the position was understated (or squared by an invented execution that erased the venue's trade id, price and fee), and the order was left working a quantity the venue already matched | FIXED | `nautilus_gateio/execution.py` `generate_mass_status` (the sweep now runs inside it, before the engine reconciles anything), `_hand_over_unapplied_fills`, `_prune_reports_the_sweep_outran`, `_record_recovery_bookings`, `_position_answer_is_stale`, `_withhold_stale_position_reports` | `TestStartupRecoverySweep`, `TestStalePositionAnswersAfterRecovery`; the dual-route parity family of the release gate (one venue answer set driven through both recovery routes, anchored to venue truth, then compared field by field) fails 7 scenarios on the previous tree and none on this one |
| `REC-02` | A position row the client cannot read is answered with an explicit flat report, and the engine squares the live book against it with a reconciliation order and an inferred fill. The row shapes were closed first (a non-object row, an unresolvable symbol or instrument); the field that decides the answer is now closed too: `size` is read strictly, so a missing key, null, an empty string, a non-numeric string, a boolean, and any value that is not an exact whole number of lots raise `PositionStatusUnavailable` naming the row and the field, while a row that genuinely reads zero still squares the book | FIXED | `nautilus_gateio/execution.py` `_parse_position_report`, `_position_reports_for_product`; `nautilus_gateio/common/parsing.py` `to_lot_count` | `TestUnreadablePositionRows` (shapes), `TestUnreadablePositionSizes` (the deciding field, both routes, with zero-size and stringified-size controls), `TestLotCount` |
| `REC-03` | `generate_fill_reports` caught every per-product failure, so the engine's brake against squaring a position on a failed fill query never engaged; a 5xx on the trade listing closed the position with a synthetic trade id and no commission | FIXED | `nautilus_gateio/execution.py` `generate_fill_reports`, `FillReportsUnavailable` | `TestFailedFillQueriesAreSurfaced`; removing the raise makes those tests and a two-cycle harness scenario fail, while the control — a listing that answers with nothing to find — still squares the book |
| `REC-04` | A quote-denominated spot market buy whose order listing is read mid-match loses trades: Gate.io publishes no base-denominated quantity for an unfinished market buy, so the report restated the order to the running partial figure and the remaining matches were rejected as overfills. An unfinished cash buy now yields no order status report at all: its executions are recovered from the trade listing, and the order's own statement is re-read once the venue has finished it | FIXED | `nautilus_gateio/execution.py` `_parse_spot_order_fields` | `TestSpotMarketBuyQuoteSemantics::test_order_status_report_never_states_the_quote_amount` (asserts no report while open, the final base figure once finished, never the cash amount); release-gate scenario `dual_route_parity_spot_market_buy_read_mid_match` with its caught-up control |

`REC-01` is stated in [execution.md](execution.md) under both Startup and Reconnect, because a reader
of either section needs it.

The first repair for `REC-01` was written and withdrawn, and stays recorded because the shape of it
is a trap. It started the sweep from the execution engine's publication of a mass status it had just
reconciled, on the grounds that this is the one moment both recovery routes share. The topic is
shared; the engine's state when it fires is not. A reconnect mass status carries no position
reports and a startup one does, and the engine reconciles those position reports before it
publishes — so on the startup path the sweep booked the venue's real trade on top of the fill the
engine had just inferred for the same trade, leaving the account holding eight lots against a venue
holding four. The repair that stands does the opposite: the fills are booked *inside*
`generate_mass_status`, before the engine has reconciled anything, so a position report that
already contains them reconciles against a cache that already carries them, and the engine's
partial-window fill adjustment is left intact. Two consequences of booking first are handled
explicitly: an order snapshot the sweep outran is withheld from the mass status where the engine
would misread it as corrupted cache and fail node start, and a position answer equal to the
pre-booking book that cannot be shown to postdate the booked trades is answered as
`PositionStatusUnavailable` rather than handed to the engine as current truth (the read-skew rule
in `_position_answer_is_stale`, which also documents its residual risk).

## Residual risks

What remains worth knowing after the fix. These are things that can still affect a user;
notes about test mechanics are deliberately left out.

**`seam-08-http-client-never-closed`** — The instrument provider holds the transport without acquiring; it never outlives the client that built it, so the count stays balanced.

**`EXEC-1`** — Spot conditional orders carry no client id, so a fired spot order still resolves via a REST re-read of armed price orders; that path is covered by tests but not yet mainnet-validated.

**`DP-8`** — Report paging stops at 20 pages (2000 rows) and logs a warning naming the cap. An account with more open orders or fills than that in one listing window reports a truncated view. Documented as a limitation for the alpha.

**`SEAM-02`** — Measured 64 ns data/execution divergence is gone; both paths now resolve to one function object.
