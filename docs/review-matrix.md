# Review matrix

A cross-cutting review of the 0.2.0 rework recorded 52 findings. This page is the tracked state of
each one: what it was, where it lived, the test that keeps it from coming back, and the residual
risk if any remains.

It is published because an alpha should be honest about what was found rather than only about what
works. Every status here was re-derived from the source, not carried over from an earlier report —
where a tracking file and the code disagreed, the code decided.

Findings raised later, by the rounds of work on recovery and the attempts to refute them, are in
[a section of their own](#recovery-findings-raised-after-this-review) at the end. As of the ninth
round every one of them is closed, each behind regression tests and release-gate scenarios proven
to fail on the tree that carried the defect; the residuals that stay open by design — and the
below-bar limitations — are stated on the rows and under residual risks. This page is not a claim
that nothing is wrong; it is a claim that what is known is written down.

Run any row's validation command from the repository root.

## Summary

| Status | Count | Meaning |
|---|---|---|
| Fixed | 51 | the defective behaviour is gone and a regression test fails against it |
| Accepted | 1 | a deliberate bound rather than a defect; stated in the documentation |

Severity of the 52 as first classified: 10 critical, 19 major, 21 minor, 2 nit. The seven recovery
findings below are outside this count.

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

The seventh round of that work was refuted in its turn, which is where `REC-05` and `REC-06` come
from. What held under the seventh refutation: the sweep and its placement inside
`generate_mass_status` (order state is correct on both recovery routes across a 33-cell
restart/reconnect matrix in which the previous tree fails 23 cells), the strict reading of the
position `size` field, and the raised fill-query failures — every cited regression test fails on
revert. The eighth round restated the staleness rule for both directions and widened the strict
reading to every deciding field of the report surface. Its audit verified every claim — the
refuters' own 33-cell matrix re-runs clean, the release gate fails exactly the claimed scenarios
on the reverted tree, and the field census is strict or excluded with a stated reason — and that
closes `REC-06` and `REC-02` in full. The same audit refuted the *closure* of `REC-05`: the
erasure family survived through the arming rule's own exception, in cells neither round had
measured, and that remainder was recorded as `REC-07`. The ninth round closed `REC-07`'s class —
the arming is keyed per instrument on prior knowledge instead of per order, recorded before the
pass books anything, and the reader clears only on venue proof, with the bookings' net delta
playing no part — with the audit's own cells ported into the release gate and the repository
tests first, each proven failing on the pre-repair tree. The ninth round's own audit could not
refute that closure: it drove the invariant as a matrix through the real execution engine —
order provenance crossed with net delta, answer shape, pre-existing position, route and pass,
including seven cells no prior round had measured — and every adversarial shape landed on the
honest side, with venue proof clearing and reconciling, everything else withheld fail-safe, and
every control unchanged. The two problems it recorded are diagnostics-only and sit under
residual risks. Live validation against the real venue then raised — and closed — `REC-08`: a
node start crashed on a pairing no offline round had built, the engine's own filtering of an
unclaimed external order read back as a bookable state. The same live validation raised `REC-09`
on the one order type whose quantity the venue and this client each state in a different
denomination, and it is closed the same way: from the venue's own figure rather than from
arithmetic performed on its behalf. Residuals that stay open by design are stated on the rows and
on the methods.

| ID | Finding | Status | Code | Evidence |
|---|---|---|---|---|
| `REC-01` | The unapplied-fill sweep ran only after a WebSocket reconnect, so on the startup path the engine's deduplication dropped a venue-confirmed trade and nothing re-offered it: the position was understated (or squared by an invented execution that erased the venue's trade id, price and fee), and the order was left working a quantity the venue already matched | FIXED — the sweep, the prune and the in-call placement stand; the stale-position rule that shipped beside them is tracked as `REC-05`, whose surviving remainder `REC-07` closed in the ninth round | `nautilus_gateio/execution.py` `generate_mass_status` (the sweep now runs inside it, before the engine reconciles anything), `_hand_over_unapplied_fills`, `_prune_reports_the_sweep_outran`, `_record_recovery_bookings`, `_position_answer_is_stale`, `_withhold_stale_position_reports` | `TestStartupRecoverySweep`, `TestStalePositionAnswersAfterRecovery`; the dual-route parity family of the release gate (one venue answer set driven through both recovery routes, anchored to venue truth, then compared field by field) fails 7 scenarios on the previous tree and none on this one |
| `REC-02` | A position row the client cannot read is answered with an explicit flat report, and the engine squares the live book against it with a reconciliation order and an inferred fill. The row shapes were closed first (a non-object row, an unresolvable symbol or instrument); the field that decides the answer is now closed too: `size` is read strictly, so a missing key, null, an empty string, a non-numeric string, a boolean, and any value that is not an exact whole number of lots raise `PositionStatusUnavailable` naming the row and the field, while a row that genuinely reads zero still squares the book | FIXED — the strict read of `size` stands with its tests, and the rest of the surface closed when `REC-06` did | `nautilus_gateio/execution.py` `_parse_position_report`, `_position_reports_for_product`; `nautilus_gateio/common/parsing.py` `to_lot_count` | `TestUnreadablePositionRows` (shapes), `TestUnreadablePositionSizes` (the deciding field, both routes, with zero-size and stringified-size controls), `TestLotCount` |
| `REC-03` | `generate_fill_reports` caught every per-product failure, so the engine's brake against squaring a position on a failed fill query never engaged; a 5xx on the trade listing closed the position with a synthetic trade id and no commission | FIXED | `nautilus_gateio/execution.py` `generate_fill_reports`, `FillReportsUnavailable` | `TestFailedFillQueriesAreSurfaced`; removing the raise makes those tests and a two-cycle harness scenario fail, while the control — a listing that answers with nothing to find — still squares the book |
| `REC-04` | A quote-denominated spot market buy whose order listing is read mid-match loses trades: Gate.io publishes no base-denominated quantity for an unfinished market buy, so the report restated the order to the running partial figure and the remaining matches were rejected as overfills. An unfinished cash buy now yields no order status report at all: its executions are recovered from the trade listing, and the order's own statement is re-read once the venue has finished it | FIXED | `nautilus_gateio/execution.py` `_parse_spot_order_fields` | `TestSpotMarketBuyQuoteSemantics::test_order_status_report_never_states_the_quote_amount` (asserts no report while open, the final base figure once finished, never the cash amount); release-gate scenario `dual_route_parity_spot_market_buy_read_mid_match` with its caught-up control |
| `REC-05` | A stale position answer erases a pre-existing position, and the staleness rule is wrong in both directions. `_position_answer_is_stale` recognises a stale answer only when it equals exactly the book as it stood before this pass booked the recovered trades. An answer staler than that — an absent row, or a zero-size row stamped in the same second, both of which Gate.io produces for a traded contract — matches neither the pre-booking nor the post-booking book, so it is taken as a current statement: the engine squares a pre-existing short six lots to flat with a reconciliation order and an inferred fill, and reconciliation reports success, so nothing downstream corrects it. The same shapes over a flat pre-outage book are withheld fail-safe — the staler the answer, the more confidently it is applied. In the other direction, an ordinary fresh-cache startup whose closed round trip straddles the lookback window makes a *current* answer read as stale, and the node refuses to start on every attempt until the trades age out of the 24-hour window. A row carrying no readable timestamp falls back to the local clock and bypasses the rule entirely | FIXED across the eighth and ninth rounds — the eighth closed the measured surface, the ninth closed the family through `REC-07`. The round rebuilt the rule in one change with the arming rule (broadening the withhold without narrowing the arming makes the fresh-cache freeze strictly worse): an answer now stands only when it contains the booked trades or is stamped strictly after them by the venue's own clock — anything else is withheld, degrading to a refused node start; the memory arms only for bookings that extended orders the cache held when recovery began, so the venue's current answer governs fresh-cache and adopted-order bookings; an unreadable row timestamp is judged as 0, never as local now. The round's audit verified all of that — the refuting matrix cells, the fresh-cache freeze (R7C-01) and the local-clock bypass (R7C-02) are closed — and then demonstrated the same erasure alive through the arming exception itself: that remainder was `REC-07`, closed in the ninth round (see its row for the mechanism). Residuals stated on the method: the memory is one restart deep and a same-second compensating trade is withheld until a distinguishable row | `nautilus_gateio/execution.py` `_position_answer_is_stale`, `_record_recovery_bookings`, `_withhold_stale_position_reports`, `generate_position_status_reports`, `_parse_position_report` | `TestStalePositionAnswersAfterRecovery` (the pre-existing-position, unreadable-timestamp and arming families all fail on the pre-fix tree); release-gate scenarios `stale_position_answer_cannot_erase_a_preexisting_position` (the four refuting matrix cells, fail-safe on this tree, fabricating on the previous one) and `fresh_cache_partial_window_round_trip_starts` (recon True first pass and every pass; False on the previous tree) |
| `REC-06` | The strict reading that closed `REC-02` covers exactly one field. The other fields that decide money in the fill and order parses still ride forgiving readers with silent defaults: futures order `left` and `size`, futures and options fill `size`, spot fill `amount`, and every fill `price`. An unreadable `left` turns a partly-matched order into a confident full fill — the engine fabricates the difference, closes the order locally while the venue holds it open, then mints a phantom round trip to square the book the fabrication broke; a venue-canceled order with an unreadable `left` reports fully filled. An unreadable fill `size` silently drops a venue-confirmed execution, and an inferred fill with zero commission stands in its place. An unreadable `price` books an execution at price zero. A decimal size is truncated and booked short — and the startup sweep books that truncation into the cache itself. The changelog sentence cited for the position fix stringifies every futures size and quantity field, and decimal strings are current documented payloads for decimal-enabled contracts. In the same scope: a still-open spot cash market buy answers its status query with nothing, which after five consecutive in-flight-check misses fabricates a rejection for a live order | FIXED in the eighth round: every deciding field of the fill, order and trigger parses — and the shared status arithmetic serving the stream — is read strictly (`to_lot_count`, the new `to_exact_decimal`, strict side/type/status conversions). Unreadable raises: trade listings answer `FillReportsUnavailable` carrying every readable row, order listings answer the new `OrderReportsUnavailable`, and startup refuses the mass status on either — the platform's own posture — while the stream drops the one unreadable frame loudly. Explicit readable zeros stay believed (the close-position order, zero-quantity rows, absent fees), and stringified integers parse exactly. Decimal-sized (`enable_decimal`) contracts are refused loudly rather than truncated — a documented alpha limitation. The still-open spot cash market buy now answers the single-order query with the venue's own quote-denominated ACCEPTED statement, resolving the inflight check without restating anything, and stays silent in listings. The eighth round's audit could not refute this closure; the three below-bar gaps it found at the edges of the census (`avg_px`, spot `fee_currency`, the spot stream's inferred `finished`) are recorded under residual risks | `nautilus_gateio/execution.py` `_parse_contract_order_fields`, `_parse_spot_order_fields`, `_open_cash_buy_as_quote`, `_order_status`, `_parse_fill_report`, `_fill_quantity_and_commission`, `_build_trigger_order_report`, `generate_order_status_reports`, `generate_fill_reports`, `generate_mass_status`, `OrderReportsUnavailable`; `nautilus_gateio/common/parsing.py` `to_exact_decimal`; `nautilus_gateio/common/enums.py` `order_side_from_gateio`, `order_type_from_gateio`, `order_status_from_gateio` | `TestUnreadableContractOrderFields`, `TestUnreadableSpotOrderFields`, `TestUnreadableFillRows`, `TestOpenCashMarketBuySingleOrderQuery`, `TestExactDecimal`, the strict-conversion tests in `test_enums.py` — 72 of them fail against the pre-fix package code; release-gate scenarios `unreadable_order_remainder_refuses_startup`, `unreadable_fill_size_refuses_startup`, `unreadable_spot_fill_amount_refuses_startup`, `unreadable_spot_order_row_refuses_startup` reproduce the refutation's engine cases and end fail-safe on this tree, fabricating on the previous one |
| `REC-07` | One booking outside the staleness memory disarms it for the whole instrument. `_record_recovery_bookings` deliberately arms nothing for fills booked onto orders the cache did not hold when recovery began — the trade that unfroze the fresh-cache restart (R7C-01). The eighth round's audit drove the consequence through the real execution engine on the release gate's own substrate: when the outage trade rode an external or manually placed order, the pass books it correctly, arms nothing, and a stale position answer — an absent row, or a zero-size row stamped in the same second, the shapes already adjudicated venue-real — is believed as current; the engine squares the pre-existing SHORT 2 together with the adopted 4 to flat with a reconciliation order and a fabricated fill, `reconcile_execution_state` returns True, and the node starts trading on net 0 against a venue holding short 6. The verdict records a second bar-crossing cell of the same family (R8-F2). At the time of the audit neither round seven's matrix nor the release gate measured these cells, and the gate scenario named `stale_position_answer_cannot_erase_a_preexisting_position` covered only bookings on orders the cache held; the ninth round ported both doors into scenarios of their own before fixing anything | FIXED in the ninth round, in one change with three snapshots and one reader ordering. The arming is keyed per instrument on prior knowledge, not per order: `_record_recovery_bookings` records every venue trade the pass sets out to book on an instrument for which the cache held a pre-existing open position or an order the trade extended — regardless of the provenance of the order the trade rode and regardless of the net delta — with the order set, the open-position set and the unbooked-trade set all snapshotted before the pass books anything, so a position the pass opens can never count as pre-existing (the R7C-01 fresh-cache trade survives: reconstruction over no prior position still arms nothing) and a trade the in-call sweep fails to book (an unanswered single-order re-read) is still guarded, because the engine books it from the returned mass status after any post-sweep arming would have run. `_position_answer_is_stale` no longer pops the memory at delta zero: the strictly-later venue stamp and agreement with the post-booking book are the only two proofs that clear it, for every entry alike | `nautilus_gateio/execution.py` `_record_recovery_bookings`, `_position_answer_is_stale`, and the snapshot sites in `generate_mass_status` and `_reconcile_after_reconnect` | Release-gate scenarios `stale_answer_cannot_erase_through_the_adopted_order_door` and `stale_answer_cannot_erase_through_the_zero_net_door` (the auditor's exact shapes — pre-existing SHORT 2, absent row and same-second zero row, both routes, plus a second restart on the venue's caught-up row that releases the refused start) fail against the pre-repair tree with the audit's signatures (net squared to 0, one reconciliation order, a fabricated fill, recon True) and end fail-safe on this one; `TestStalePositionAnswersAfterRecovery` zero-net and adopted-arming tests fail on the pre-repair package code; the auditor's own matrix extension re-run on this tree shows all four REC-07 cells withheld with routes in parity and every control (fresher-wins, agreement-clears, current-row, known-order withheld family) unchanged |
| `REC-08` | The degraded no-statement booking reads a dangling index entry as an order and crashes mass-status assembly. Found by live validation, not by any offline round: a settled foreign spot market buy from an earlier process sat in the trade listing's window while a fresh cache gave the spot finished-order listing no symbol to sweep, so the startup sweep re-read the single order — and Gate.io answered. The node ran with `filter_unclaimed_external_orders=True`: the engine dropped the unclaimed external order inside `_generate_order` and *still* indexed its client/venue order ids afterwards (`_reconcile_execution_mass_status`, installed live/execution_engine.py:1915-1925). The sweep's last-resort branch tested only the index and sent a lone `FillReport`; the engine resolved the index, missed the order object, and fell into `_find_order_by_venue_order_id(order_side=None)`, whose `Cache.orders(side=None)` call the Cython signature refuses — `TypeError("an integer is required")` escaped `generate_mass_status`, the engine logged "Failed to generate mass status", reconciliation failed, and the node reported RUNNING while unusable. Two layers: the adapter read the engine's silent refusal as presence (the swallowed-refusal-as-presence motif, one level above the payload readers), and a degraded artefact was allowed to crash mass-status assembly through the platform's own invalid `order_side=None` default | FIXED, same day as found. The last-resort channel is gated on the cached order *object*, never the index, with three exits: order cached — the lone sends proceed as before, loudly; statement delivered but not adopted — the executions are excluded together with their order and each exclusion logged, because filtering unclaimed external orders is the engine's configured ruling; no statement obtainable — `_hand_over_fills_with_their_order` raises `FillReportsUnavailable` and each pass refuses honestly: a `None` startup mass status (kernel declines to start, the next attempt re-reads and heals), a kept reconnect state, a logged standing loss on the stream route. `_hand_over_fill` applies the same gate so a runtime stream fill at a dangling index takes the grouped route | `nautilus_gateio/execution.py` `_hand_over_fills_with_their_order` (the order-object gate and the three exits), `_hand_over_fill`, `_hand_over_stream_fill`, and the `FillReportsUnavailable` catches in `generate_mass_status` and `_reconcile_after_reconnect` | `TestRecoveredTradeWithoutAdoptedOrder` — the engine-integration test drives the exact live pairing through the real installed `LiveExecutionEngine` and fails on the pre-fix tree with the live failure's own `TypeError("an integer is required")` from `Cache.orders`; the dangling-index, stream-route and refused-mass-status tests fail on the pre-fix tree on the lone send and the silently lighter book. The platform defect (`_find_order_by_venue_order_id`'s `order_side=None` default) and the engine's dangling index entry stand upstream; this adapter no longer feeds the one or trusts the other |
| `REC-09` | A quote-denominated spot market buy is left open for ever, and can also lose a fill. Gate.io denominates a spot market buy in the quote currency and states its base total only when the order finishes; the client restated the order on the first fill to `cash / first_fill_price` instead. NautilusTrader's `Order._filled` (model/orders/base.pyx:1176-1180) is a raw `filled_qty + last_qty < quantity` with no unit or tolerance, so that estimate decides the order's terminal state, and it is an arithmetic the venue never took part in: one increment high and no fill can ever complete the order — it stays in `cache.orders_open()` for the life of the process while the venue has finished it, reconciliation returns "reconciled" without closing it (there is no FILLED branch in `_handle_order_status_transitions`, and `OrderUpdated` advances no FSM), and the cancel that follows is answered `ORDER_NOT_FOUND` and turned into an `OrderCancelRejected` that hands the order back to PARTIALLY_FILLED; one increment low and `ExecutionEngine._check_overfill` discards the venue's fill outright, so a trade that happened is not booked. On a fill that sweeps two price levels the estimate cannot come out right at all — every later match of a buy is at a worse price than the one divided by. Found by the mainnet TC-E05 step, whose own checks passed: they asserted fills and the closing position, never that the order reached a terminal state. Second defect on the same order, separable: `_order_status` compared the base `filled_amount` against the quote `amount`, which Gate.io's own documentation warns against, so the answer depended on the price of the pair — on a cheap pair a half-spent cash buy read FILLED, on an expensive one the same order read CANCELED | FIXED. The order no longer carries an estimate of anything. While it works, its quantity is a **bound** built from the venue's own fill amounts — one size increment above the base credited so far — which removes the low case by construction: no fill can be discarded, and none can close the order before the venue says it is finished. The terminal `spot.orders` frame is where Gate.io states `filled_amount`, so that figure replaces the bound and the order is closed with `OrderCanceled`, which preserves the filled quantity and is the transition the platform holds for the real-world case (base.pyx:132-133). A Gate.io spot market buy is IOC or FOK, so what the cash did not buy was canceled rather than left working; the documented consequence is that a cash buy ends CANCELED rather than FILLED even when it spends all its cash, and the outcome is read from `filled_qty` and the position. A fill still in flight when the order closes is already covered by `_handle_late_fill`. The units mix is closed separately: a cash buy's completion is decided in quote units (`filled_total` against `amount`), never across denominations. The third part is the cancel: Gate.io's own error tables class `ORDER_NOT_FOUND`, `ORDER_CLOSED`, `ORDER_CANCELLED` and `ORDER_FINISHED` as benign idempotent races on cancel, and this client's transport replays `DELETE`, so one of those labels is the ordinary answer to a cancellation that worked. They are no longer reported as refusals: the order is re-read and its own statement decides, falling back to `OrderCanceled` only when the re-read cannot answer at all. This is a label-level exception to the type-level rule in `is_ambiguous_outcome`, written as one, with the reason stated; `CANCEL_FAIL` and `NO_CHANGE` stay refusals because neither says the order is gone | `nautilus_gateio/execution.py` `is_cash_buy_payload`, `_raise_cash_buy_bound` (replacing `_maybe_convert_quote_quantity`), `_close_finished_cash_buy`, the cash-buy branch of `_handle_order_payload`, `_order_status`, `_payload_quantity`, `CANCEL_ALREADY_DONE_LABELS`, `_resolve_cancel_of_a_vanished_order` | `tests/test_execution_cash_buy.py` — 12 of its 15 pre-existing-behaviour tests fail against the pre-fix package code, including every parametrised fill price and the two-price sweep; the three that pass on both trees are the exact-divisor coincidence the old suite was built on, plus two controls. The fill price is parametrised deliberately: the original suite case used 600 USDT at exactly 60000.00, the one arrangement where the estimate and the venue agree, which is why it stayed green while the venue did not. `tests/test_execution_ambiguity.py::TestCancelOfAnOrderTheVenueNoLongerHolds` — six of its eight fail against the pre-fix code (all four labels, the unanswerable re-read, and the partly filled order that must keep its fills); the two that pass are the `CANCEL_FAIL` / `NO_CHANGE` controls, which must stay refusals |

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
in `_position_answer_is_stale`).

That read-skew rule is where the refutation of the seventh round landed: equality with the
pre-booking book was the only staleness it recognised, so its protection reached exactly as far as
the fixtures that shaped it — a flat book before the recovered trades. `REC-05` records what lay
beyond that boundary and `REC-06` records the same lesson on the parsing surface. The eighth round
closed `REC-06` across the whole deciding surface, and rebuilt the rule so that it withholds
everything it cannot prove current — for instruments whose recovered trades extended orders this
node held. The round's audit then showed the lesson repeating one boundary out: the arming
exception that unfroze the fresh-cache restart left an instrument with an adopted-order booking
entirely unguarded, and the erasure survived there (`REC-07`). The ninth round closed that class
by re-keying the exception to the unit the erasure lives at — the instrument and its prior
knowledge, not the order — and by removing the reader's zero-net shortcut; the row above records
the mechanism and the evidence. The sweep's placement, which the withdrawn repair got wrong and
this one got right, was never in question: order state is correct on both routes across the whole
refutation matrix.

## Residual risks

What remains worth knowing after the fix. These are things that can still affect a user;
notes about test mechanics are deliberately left out.

**`seam-08-http-client-never-closed`** — The instrument provider holds the transport without acquiring; it never outlives the client that built it, so the count stays balanced.

**`EXEC-1`** — Spot conditional orders carry no client id, so a fired spot order still resolves via a REST re-read of armed price orders; that path is covered by tests but not yet mainnet-validated.

**`DP-8`** — Report paging stops at 20 pages (2000 rows) and logs a warning naming the cap. An account with more open orders or fills than that in one listing window reports a truncated view. Documented as a limitation for the alpha.

**`REC-06` edges, found by the eighth round's audit (below the blocking bar):** the ninth round
closed two of the three. The order report's average price (`avg_deal_price`/`fill_price`) is read
strictly on filled rows — it is the price the engine puts on any inferred stand-in fill, so a
stated-and-unreadable value fails the listing (`OrderReportsUnavailable`) while an absent one
stays the smaller claim of no average (regression rows in `TestUnreadableContractOrderFields` and
`TestUnreadableSpotOrderFields`, failing on the pre-repair tree). A spot fill that states a
nonzero fee without a readable `fee_currency` now refuses (`FillReportsUnavailable` on the
listings, a loud dropped frame on the stream) instead of guessing the quote currency: Gate.io
documents the field on every spot trade row and the fee is base for the ordinary buy, so the
guess misdenominated commission; a zero fee keeps the quote as the harmless denomination of zero
(`TestUnreadableFillRows`, failing on the pre-repair tree). Still open as a limitation: the spot
status arithmetic infers `finished` for a stream payload stating neither `status` nor `event` —
no documented venue payload reaches that branch, but it contradicts the rule that absence makes
no claim. It remains a strict-read candidate for the next pass at this surface.

**Two diagnostic bounds of the staleness memory, found by the ninth round's audit** — neither
touches money or availability, and both are stated on `_record_recovery_bookings`. Entries armed
for spot instruments are inert but permanent: the pre-booking snapshot includes spot positions,
yet every spot position query answers before the staleness rule is consulted, so nothing ever
reads or clears such an entry. And because arming precedes booking, a recovery pass that fails
after arming re-records the same still-unbooked trades on its next attempt, so the net-delta
figure in the reader's debug line inflates across retries — the venue timestamp the decision
uses is idempotent under the max, and the delta takes no part in any decision.

**`OrderReportsUnavailable` under the engine's continuous open-order check** — the engine swallows
the raise per client and proceeds on an empty answer. Under the default `open_check_open_only=True`
that is harmless; with `open_check_open_only=False` an own order whose listing row stays unreadable
is counted missing every cycle and, once `open_check_missing_retries` is exhausted, is resolved
with a fabricated REJECTED or CANCELED. Run this alpha with the default, or accept that resolution.

**`SEAM-02`** — Measured 64 ns data/execution divergence is gone; both paths now resolve to one function object.
