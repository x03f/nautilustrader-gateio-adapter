# Specification case coverage

NautilusTrader's adapter guide numbers the cases an adapter is expected to be
exercised against — `TC-E*` for execution, `TC-D*` for market data — and asks a
project to record, for every case, either the test that closes it or the reason
it is skipped. This page is that record.

**Source of the case list.** Upstream `spec_exec_testing.md` and
`spec_data_testing.md` at commit `e3ef686` (branch `develop`, 2026-08-01). That
revision numbers **95** cases: 69 execution and 26 data. The previous revision
numbered 90; the five additions are `TC-E74`–`TC-E78`, the *Ambiguous outcome
failures* group, which the specification states collectively rather than one
section each.

Three identifiers a reader may expect — `TC-E07`, `TC-E08`, `TC-E09` — appear in
no revision of the specification and are not counted here. An earlier internal
map carried them as rows, which is where a denominator of 93 for the previous
revision came from; the honest figure there was 90, and here it is 95.

**What the Rung column means.** It is the project's one status vocabulary, the
[evidence ladder](validation.md#the-evidence-ladder), and nothing on this page
adds a rung to it:

* **implemented** — the code path exists and has been read; nothing in the suite
  asserts the case. This is an open gap, not a pass.
* **unit-tested** — the suite asserts the case against a recorded or synthetic
  payload, one request body, one parse or one refusal at a time.
* **offline-harness** — the case was driven as a *sequence* through
  `ExecHarness`, with the platform's own `Cache`, `MessageBus` and `Order` state
  machine applying every event.
* **mainnet-confirmed** — a recorded run against Gate.io did it. No row on this
  page carries this rung; what mainnet has seen is on
  [validation.md](validation.md).
* **skipped** — the specification's own *Skip when* condition applies. The
  Evidence cell names the venue fact or the capability limit, with a source.

A rung grades the *evidence*, not the case, so a row can carry a rung and still
name something unasserted. That is what the **Not yet asserted** column is for:
a rung with an empty gap column is a case the suite closes in full.

**How each row was checked.** Every named test was opened and read against the
specification's own pass criteria for that case. A test whose name matched but
whose body asserted something else was not accepted as evidence — an earlier
internal map failed exactly that way in both directions, citing tests that did
not assert the requirement and marking closed cases open. Rows marked
*unverified* below were carried over without that check and must be treated as
unknown, not as coverage.

**What keeps this page honest.** `tests/test_spec_coverage.py` fails when a test
named here does not exist in the suite, when a case identifier is missing,
duplicated or invented, or when a row states neither evidence nor a reason. It
cannot check that a named test asserts what the case requires — that remains a
reading, and the reading is the paragraph above.

Test names are given as pytest node ids without parameters; a parametrized test
named here covers every one of its cases.

---

## Execution: Group 1 — Market orders

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E01 | Market BUY — submit and fill | unit-tested | `tests/test_execution_market_buy.py::TestThePriceIsTheAskLiftedByThePairsCap::test_the_whole_body_is_a_limit_at_the_ask_plus_the_pairs_cap`, `tests/test_execution_market_buy.py::TestTheFallbackChain::test_the_venue_ticker_is_the_last_resort`, `tests/test_execution_market_buy.py::TestTheFallbackChain::test_an_unpriceable_buy_is_refused_rather_than_priced_from_nothing` | No test drives a market BUY through `OrderFilled` to a LONG position, so the event sequence and the "fill price within market range" criterion rest on the live venue |
| TC-E02 | Market SELL — submit and fill | unit-tested | `tests/test_execution_market_buy.py::TestOnlyABaseDenominatedBuyIsRewritten::test_a_market_sell_stays_a_native_market_order`, `tests/test_execution_modify.py::TestTheSignOfSizeCarriesTheSide::test_a_submitted_contract_order_states_its_side_as_the_sign_of_size` | The mirror of TC-E01: no test carries a market SELL to `OrderFilled` and a SHORT position |
| TC-E03 | Market order with IOC TIF | unit-tested | `tests/test_execution_order_fidelity.py::TestSpotMarketTimeInForce::test_a_resting_instruction_on_a_market_order_still_collapses_to_ioc`, `tests/test_enums.py::TestTimeInForce::test_supported_values_map_to_the_venue_vocabulary` | An *explicitly* requested IOC is asserted on spot only; the contract and options bodies are not driven with one |
| TC-E04 | Market order with FOK TIF | unit-tested | `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_spot_market_sell_sends_fok`, `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_futures_market_sends_fok`, `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_delivery_market_sends_fok`, `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_options_market_rejects_fok`, `tests/test_execution_market_buy.py::TestThePriceIsTheAskLiftedByThePairsCap::test_a_fill_or_kill_buy_is_capped_and_still_all_or_nothing` | The fill half, shared with TC-E01 |
| TC-E05 | Market order with quote quantity | offline-harness | `tests/test_execution_cash_buy.py::TestTheInFlightQuantityIsABound::test_the_bound_sits_one_increment_above_the_venues_credit`, `tests/test_execution_cash_buy.py::TestTheVenuesFinishFrameClosesTheOrder::test_a_fully_spent_cash_buy_closes_on_the_venues_base_total`, `tests/test_execution_cash_buy.py::TestTheUnitsAreNeverMixed::test_the_status_of_a_cash_buy_is_decided_in_quote_units`, `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_spot_market_quote_buy_sends_fok` | — |
| TC-E06 | Close position via market order on stop | implemented | The closing order is the `ExecTester`'s, not the adapter's; the adapter-side path is TC-E01/TC-E02's market order. `close_positions_qty_precision` and the sub-precision residual criterion belong to the tester | Reachable only in a live `ExecTester` session |

## Execution: Group 2 — Limit orders

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E10 | Limit BUY GTC — submit and accept | offline-harness | `tests/test_execution_modify.py::TestCancelReplace::test_a_cancel_then_resubmit_uses_a_fresh_identity` — a spot limit BUY is submitted, the ack and the `spot.orders` frame are read, and the order reaches `ACCEPTED` carrying the venue's own id | No test asserts that a plain GTC limit body carries `time_in_force: "gtc"`; the whole-body assertion exists only for the FOK sibling |
| TC-E11 | Limit SELL GTC — submit and accept | unit-tested | `tests/test_execution_modify.py::TestTheSignOfSizeCarriesTheSide::test_a_submitted_contract_order_states_its_side_as_the_sign_of_size` (perpetual and delivery limit SELL bodies) | No spot limit SELL is driven from submit to `OrderAccepted` |
| TC-E12 | Limit BUY and SELL pair | offline-harness | `tests/test_execution_order_lists.py::TestAPlainListIsBatched::test_two_spot_orders_go_out_as_one_batch_request`, `tests/test_execution_order_lists.py::TestAPlainListIsBatched::test_every_order_is_announced_before_the_request_leaves` | Both orders stop at `SUBMITTED`; nothing carries the pair to two independent `OrderAccepted` |
| TC-E13 | Limit IOC aggressive fill | unit-tested | `tests/test_enums.py::TestTimeInForce::test_supported_values_map_to_the_venue_vocabulary`, `tests/test_enums.py::TestOrderStatusMapping::test_fully_filled_immediate_order_is_filled` | No test submits an IOC limit and fills it |
| TC-E14 | Limit IOC passive — no fill | unit-tested | `tests/test_enums.py::TestOrderStatusMapping::test_unfilled_immediate_order_is_canceled`, `tests/test_enums.py::TestOrderStatusMapping::test_partially_filled_immediate_order_is_canceled` — an `ioc` termination is `CANCELED`, which is the criterion ("not `OrderExpired`") | The venue frame is not driven through the client to an `OrderCanceled` event |
| TC-E15 | Limit FOK fill | unit-tested | `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_spot_limit_sends_fok` (whole body), `tests/test_enums.py::TestOrderStatusMapping::test_fully_filled_immediate_order_is_filled` | "Fills completely in a single fill event" is not driven anywhere |
| TC-E16 | Limit FOK no fill | unit-tested | `tests/test_enums.py::TestOrderStatusMapping::test_unfilled_immediate_order_is_canceled` | As TC-E14 |
| TC-E17 | Limit GTD — submit and accept | skipped | Gate.io has no GTD time in force on a regular order: `time_in_force_to_gateio` raises for it (`gateio_nt/common/enums.py:202-205`), pinned by `tests/test_enums.py::TestTimeInForce::test_unsupported_values_are_rejected_never_downgraded`. GTD survives only on a price-triggered order, as `trigger.expiration` (`gateio_nt/execution.py:3796-3810`), and a past expiry is refused: `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_a_past_expire_time_on_a_conditional_order_is_denied` | — |
| TC-E18 | Limit GTD expiry | skipped | Same venue fact as TC-E17. The terminal status a lapse produces is pinned for the conditional path by `tests/test_execution_order_fidelity.py::TestTerminalStatusFollowsTheQuantities::test_an_unfilled_expiry_is_still_expired` and `tests/test_execution_order_fidelity.py::TestTerminalStatusFollowsTheQuantities::test_a_partially_filled_expiry_is_still_expired` | — |
| TC-E19 | Limit DAY — submit and accept | skipped | Gate.io has no DAY time in force; the same function refuses it, and the client denies rather than submits: `tests/test_enums.py::TestTimeInForce::test_unsupported_values_are_rejected_never_downgraded`, `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_an_unsupported_time_in_force_is_denied_not_rejected` | — |

## Execution: Group 3 — Stop and conditional orders

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E20 | StopMarket BUY | unit-tested | `tests/test_execution_order_fidelity.py::TestTheTriggerRuleKeepsTheOrderType::test_a_well_formed_conditional_order_still_arms` (stop BUY above the market arms with rule 1), `tests/test_execution_order_fidelity.py::TestTheTriggerRuleKeepsTheOrderType::test_a_stop_buy_below_the_market_is_not_turned_into_a_dip_buy` | No test carries an armed stop to `OrderAccepted` with the trigger price recorded on the order |
| TC-E21 | StopMarket SELL | unit-tested | `tests/test_execution_order_fidelity.py::TestTheTriggerRuleKeepsTheOrderType::test_a_well_formed_conditional_order_still_arms` (stop SELL below the market arms with rule 2), `tests/test_execution_order_fidelity.py::TestTheTriggerRuleKeepsTheOrderType::test_a_breached_sell_stop_is_refused_rather_than_inverted` | As TC-E20 |
| TC-E22 | StopLimit BUY | implemented | The armed body is asserted only in refusals (`tests/test_execution_orders.py::TestConditionalOrderRejections::test_spot_price_order_rejects_post_only`); the *fired* stop-limit is covered by `tests/test_execution_reports.py::TestFiredConditionalOrderReports::test_a_resting_fired_order_reports_triggered` | Nothing asserts the accepted STOP_LIMIT request — trigger price and limit price together — on any product |
| TC-E23 | StopLimit SELL | implemented | As TC-E22 | As TC-E22 |
| TC-E24 | MarketIfTouched BUY | unit-tested | `tests/test_execution_order_fidelity.py::TestTheTriggerRuleKeepsTheOrderType::test_a_well_formed_conditional_order_still_arms` (MIT BUY below the market arms with rule 2), `tests/test_execution_order_fidelity.py::TestTheTriggerRuleKeepsTheOrderType::test_an_if_touched_buy_above_the_market_is_not_turned_into_a_breakout` | The accept event, as TC-E20 |
| TC-E25 | MarketIfTouched SELL | unit-tested | `tests/test_execution_order_fidelity.py::TestTheTriggerRuleKeepsTheOrderType::test_a_well_formed_conditional_order_still_arms` (MIT SELL above the market arms with rule 1) | The accept event, as TC-E20 |
| TC-E26 | LimitIfTouched BUY | unit-tested | `tests/test_execution_orders.py::TestConditionalOrderRejections::test_spot_price_order_body_is_exact` — the whole `{market, put, trigger}` request for a LIT BUY | The accept event, as TC-E20 |
| TC-E27 | LimitIfTouched SELL | implemented | The BUY body above; nothing exercises the SELL side of LIT | The request a LIT SELL sends, and its trigger rule |

## Execution: Group 4 — Order modification

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E30 | Modify limit BUY price | offline-harness | `tests/test_execution_modify.py::TestSuccessfulAmend::test_a_spot_price_amend_emits_order_updated_with_the_new_price`, `tests/test_execution_modify.py::TestSuccessfulAmend::test_a_perpetual_price_amend_emits_order_updated_with_the_new_price`, `tests/test_execution_modify.py::TestSuccessfulAmend::test_the_amend_is_native_and_never_becomes_cancel_and_create` | — |
| TC-E31 | Modify limit SELL price | offline-harness | The same three tests, parametrized over both sides; plus `tests/test_execution_modify.py::TestTheSignOfSizeCarriesTheSide::test_an_amended_contract_size_takes_its_sign_from_the_order_side` | — |
| TC-E32 | Cancel-replace limit BUY | offline-harness | `tests/test_execution_modify.py::TestCancelReplace::test_a_cancel_then_resubmit_uses_a_fresh_identity` — the original reaches `CANCELED`, the replacement `ACCEPTED` under its own id, and a late fill for the original is not booked against the replacement | — |
| TC-E33 | Cancel-replace limit SELL | implemented | The BUY case above; the sell side of cancel-replace is not driven | The same sequence on a SELL |
| TC-E34 | Modify stop trigger price | skipped | Gate.io has no native trigger amend: a price-triggered order is cancelled and re-armed, so the specification's own note ("venues without a replace for a trigger — skip TC-E34 and run TC-E35 instead", `spec_exec_testing.md:1114`) applies. The client sends the amend to `amend_order`, which addresses resting orders only (`gateio_nt/execution.py`, `_modify_order`) | — |
| TC-E35 | Cancel-replace stop order | offline-harness | `tests/test_execution_triggers.py::TestRestartAcrossTheTriggerTransition::test_cancel_after_a_restart_still_disarms_by_the_armed_id`, `tests/test_execution_events.py::TestTriggerVenueOrderIdRebase::test_cancel_while_armed_uses_the_armed_id`, `tests/test_execution_events.py::TestTriggerVenueOrderIdRebase::test_cancel_after_trigger_uses_the_fired_id` | The specification's added criterion — after re-arming, force a reconciliation and confirm exactly one live trigger order remains — is not asserted |
| TC-E36 | Modify rejected | offline-harness | `tests/test_execution_ambiguity.py::TestAmbiguousAmend::test_a_proven_refusal_is_still_a_modify_reject`, `tests/test_execution_ambiguity.py::TestAmbiguousAmend::test_ambiguous_amend_stays_pending`, `tests/test_execution_orders.py::TestOffTickPrices::test_an_off_tick_amend_price_is_refused` | — |

## Execution: Group 5 — Order cancellation

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E40 | Cancel single limit order | offline-harness | `tests/test_execution_cancel.py::TestCancelSucceeds::test_a_plain_limit_cancel_emits_exactly_one_order_canceled` (spot, perpetual and delivery, both sides), `tests/test_execution_cancel.py::TestCancelSucceeds::test_a_repeated_confirmation_frame_produces_no_second_event`, `tests/test_execution_cancel.py::TestCancelSucceeds::test_a_partly_filled_order_keeps_its_fill_through_the_cancel` | — |
| TC-E41 | Cancel all on stop | offline-harness | `tests/test_execution_cancel.py::TestCancelAllReachesPlainOrders::test_cancel_all_cancels_the_resting_limits_as_well_as_the_armed_trigger`, `tests/test_execution_orders.py::TestCancelAllSideScoping::test_unscoped_cancel_still_bulk_disarms`, `tests/test_execution_orders.py::TestCancelAllSideScoping::test_side_scoped_cancel_does_not_bulk_disarm` | — |
| TC-E42 | Individual cancels on stop | offline-harness | `tests/test_execution_cancel.py::TestCancelSucceeds::test_three_individual_cancels_produce_three_events_and_no_cross_talk` | — |
| TC-E43 | Batch cancel on stop | unit-tested | `tests/test_execution_cancel.py::TestBatchCancel::test_a_fully_successful_batch_sends_every_order_in_one_request`, `tests/test_execution_cancel.py::TestBatchCancel::test_a_batch_of_twenty_five_is_split_and_still_carries_every_order`, `tests/test_execution_cancel.py::TestBatchCancel::test_a_batch_cancel_on_a_contract_product_falls_back_to_one_request_each` | The event half on spot: Gate.io's batch answer carries no order object, and the client emits nothing for a succeeded row, so the orders stay `PENDING_CANCEL` until the engine's in-flight check resolves them. The test module states this rather than freezing it into an assertion |
| TC-E44 | Cancel already-canceled order | offline-harness | `tests/test_execution_ambiguity.py::TestCancelOfAnOrderTheVenueNoLongerHolds::test_the_order_is_re_read_and_closed_on_its_own_statement` (all four benign labels), `tests/test_execution_ambiguity.py::TestCancelOfAnOrderTheVenueNoLongerHolds::test_an_unanswerable_re_read_still_closes_the_order`, `tests/test_execution_ambiguity.py::TestCancelOfAnOrderTheVenueNoLongerHolds::test_a_label_that_does_not_say_the_order_is_gone_is_still_a_refusal` | — |

## Execution: Group 6 — Bracket orders

Gate.io carries attached take-profit and stop-loss, but neither the spot shape
(`stop_profit`/`stop_loss`) nor the futures shape (`tpsl_*_trigger_price`)
carries a client-supplied identifier for the attached leg, so three Nautilus
orders would reach the venue as one order with one id. Announcing
`OrderSubmitted` for a leg that can never acquire a venue order id is
destructive rather than untidy: `LiveExecutionEngine._resolve_inflight_order`
turns it into `OrderRejected` once the in-flight retries are spent, and the
strategy is told its stop-loss was rejected while Gate.io holds it live. The
client therefore denies a contingent list wholesale, which is the
specification's *Skip when: no bracket support*.

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E50 | Bracket BUY | skipped | The venue fact above; the refusal is pinned by `tests/test_execution_order_lists.py::TestAContingentListIsRefusedWholesale::test_every_leg_of_a_bracket_is_denied_and_nothing_is_sent`, `tests/test_execution_order_lists.py::TestAContingentListIsRefusedWholesale::test_the_denial_names_the_venue_fact_and_the_way_round_it`, `tests/test_execution_order_lists.py::TestAContingentListIsRefusedWholesale::test_no_leg_is_left_without_a_terminal_state` | — |
| TC-E51 | Bracket SELL | skipped | The same venue fact as TC-E50 — no client-supplied identifier for an attached leg on either shape — and the same refusal, which is parametrized over the side: `tests/test_execution_order_lists.py::TestAContingentListIsRefusedWholesale::test_every_leg_of_a_bracket_is_denied_and_nothing_is_sent` | — |
| TC-E52 | Bracket entry fill activates TP/SL | skipped | The same venue fact as TC-E50: the list never reaches Gate.io, so there is no attached leg for an entry fill to activate. `tests/test_execution_order_lists.py::TestAContingentListIsRefusedWholesale::test_no_leg_is_left_without_a_terminal_state` | — |
| TC-E53 | Bracket with post-only entry | skipped | The same venue fact as TC-E50; the post-only flag on the entry changes nothing, because the list is refused before any leg is built. The refusal follows the contingency rather than the `is_bracket` shortcut: `tests/test_execution_order_lists.py::TestAContingentListIsRefusedWholesale::test_an_oco_bracket_is_denied_although_is_bracket_says_no`, `tests/test_execution_order_lists.py::TestAContingentListIsRefusedWholesale::test_a_single_linked_leg_refuses_the_whole_list` | — |

## Execution: Group 7 — Order flags

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E60 | PostOnly accepted | unit-tested | `tests/test_execution_orders.py::TestPostOnlyWithImmediateTimeInForce::test_post_only_gtc_is_still_sent_as_poc`, `tests/test_enums.py::TestTimeInForce::test_post_only_gtc_maps_to_poc`, `tests/test_execution_orders.py::TestPostOnlyWithImmediateTimeInForce::test_spot_limit_is_refused` (post-only with an immediate TIF is refused rather than silently rested) | No post-only order is driven to `OrderAccepted` |
| TC-E61 | ReduceOnly on close | unit-tested | `tests/test_execution_modify.py::TestReduceOnlyOnRegularOrders::test_a_regular_contract_order_carries_reduce_only_to_the_venue` (perpetual and delivery, market and limit, both sides), `tests/test_execution_modify.py::TestReduceOnlyOnRegularOrders::test_an_order_that_is_not_reduce_only_carries_no_such_key`, `tests/test_execution_modify.py::TestReduceOnlyOnRegularOrders::test_spot_has_no_reduce_only_so_the_order_is_refused_rather_than_stripped`, `tests/test_execution_orders.py::TestConditionalOrderRejections::test_futures_price_order_keeps_reduce_only` | The position actually closing — no test opens a position and reduces it |
| TC-E62 | Display quantity (iceberg) | unit-tested | `tests/test_execution_orders.py::TestDisplayQuantity::test_spot_limit_sends_the_displayed_portion`, `tests/test_execution_orders.py::TestDisplayQuantity::test_spot_hidden_order_is_refused_not_inverted`, `tests/test_execution_orders.py::TestDisplayQuantity::test_futures_hidden_order_is_refused_not_inverted`, `tests/test_execution_orders.py::TestDisplayQuantity::test_options_hidden_order_is_refused_not_inverted`, `tests/test_execution_orders.py::TestDisplayQuantity::test_a_fractional_contract_display_is_refused_not_truncated` | The accept event |
| TC-E63 | Custom order params | skipped | The client reads no `command.params` on any order path; the decision is recorded at `gateio_nt/execution.py` (`_submit_order`), and the specification marks this case *N/A (adapter-specific)* | — |

## Execution: Group 8 — Rejection handling

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E70 | PostOnly rejection | offline-harness | `tests/test_execution_ambiguity.py::TestAmbiguousSubmit::test_post_only_rejection_keeps_its_flag`, `tests/test_execution_events.py::TestPostOnlyTermination::test_an_untouched_order_is_rejected_with_the_post_only_flag`, `tests/test_execution_events.py::TestPostOnlyTermination::test_a_partly_filled_order_is_canceled_not_rejected` | — |
| TC-E71 | ReduceOnly rejection | unit-tested | `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_reduce_only_on_a_regular_spot_order_is_denied` — on spot the refusal is local and can never be an `OrderRejected` | The venue-side refusal on a contract product: no test answers a reduce-only order with the venue's own rejection label |
| TC-E72 | Unsupported order type | offline-harness | `tests/test_execution_orders.py::TestOrderRouting::test_unsupported_order_type_is_denied`, `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_a_globally_unsupported_type_is_denied_on_an_option_too`, `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_a_builder_failure_of_any_kind_is_denied_not_left_in_flight` | — |
| TC-E73 | Unsupported TIF | offline-harness | `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_an_unsupported_time_in_force_is_denied_not_rejected`, `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_a_session_time_in_force_on_a_market_order_is_denied`, `tests/test_execution_order_fidelity.py::TestSpotMarketTimeInForce::test_a_session_time_in_force_is_refused_on_spot_too`, `tests/test_execution_order_lists.py::TestNoOrderIsEverLeftAtInitialized::test_a_time_in_force_the_venue_cannot_express_is_denied_not_dropped` | — |
| TC-E74 | Ambiguous submit failure | offline-harness | `tests/test_execution_ambiguity.py::TestAmbiguousSubmit::test_ambiguous_submit_leaves_the_order_in_flight` (four failure shapes: read timeout after send, 5xx on a mutating request, 5xx on a replayed request, unreadable response), `tests/test_execution_ambiguity.py::TestAmbiguousSubmit::test_a_venue_that_kept_the_order_can_still_be_reconciled`, `tests/test_execution_ambiguity.py::TestAmbiguousSubmit::test_a_proven_refusal_is_still_rejected`, `tests/test_execution_ambiguity.py::TestOutcomeClassification::test_unknown_outcomes_are_ambiguous` | — |
| TC-E75 | Ambiguous cancel failure | offline-harness | `tests/test_execution_ambiguity.py::TestAmbiguousCancel::test_ambiguous_cancel_stays_pending`, `tests/test_execution_ambiguity.py::TestAmbiguousCancel::test_a_proven_refusal_is_still_a_cancel_reject` | — |
| TC-E76 | Ambiguous modify failure | offline-harness | `tests/test_execution_ambiguity.py::TestAmbiguousAmend::test_ambiguous_amend_stays_pending`, `tests/test_execution_ambiguity.py::TestAmbiguousAmend::test_a_proven_refusal_is_still_a_modify_reject` | — |
| TC-E77 | Ambiguous batch failure (whole batch, no per-order result) | offline-harness | `tests/test_execution_ambiguity.py::TestAmbiguousBatchCancel::test_a_whole_batch_failure_resolves_nothing`, `tests/test_execution_ambiguity.py::TestAmbiguousBatchCancel::test_a_refused_batch_request_is_definitive`, `tests/test_execution_order_lists.py::TestABatchThatFailedAsAWhole::test_an_ambiguous_batch_leaves_every_order_in_flight`, `tests/test_execution_order_lists.py::TestABatchThatFailedAsAWhole::test_a_server_failure_is_ambiguous_too` | — |
| TC-E78 | Per-order batch reject | offline-harness | `tests/test_execution_ambiguity.py::TestAmbiguousBatchCancel::test_per_order_failures_are_still_definitive`, `tests/test_execution_order_lists.py::TestABatchResponseIsReadPerItem::test_only_the_failed_order_is_rejected`, `tests/test_execution_order_lists.py::TestABatchResponseIsReadPerItem::test_attribution_follows_the_client_id_not_the_row_position`, `tests/test_execution_order_lists.py::TestABatchResponseIsReadPerItem::test_an_order_the_response_never_mentioned_stays_in_flight` | — |

## Execution: Group 9 — Lifecycle and reconciliation

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E80 | Open position on start | implemented | The submission half is TC-E01/TC-E02; opening a position is the `ExecTester`'s own start-up action | Nothing offline opens a position from a start-up market order |
| TC-E81 | Cancel orders on stop | offline-harness | `tests/test_execution_cancel.py::TestCancelAllReachesPlainOrders::test_cancel_all_cancels_the_resting_limits_as_well_as_the_armed_trigger`, `tests/test_execution_orders.py::TestCancelAllLocalCheck::test_a_configured_product_still_cancels`, `tests/test_execution_orders.py::TestCancelAllLocalCheck::test_an_unconfigured_product_warns_and_emits_no_event` | — |
| TC-E82 | Close positions on stop | implemented | As TC-E06: the closing order is the tester's | Reachable only in a live `ExecTester` session |
| TC-E83 | Unsubscribe on stop (execution) | unit-tested | `tests/test_execution_events.py::test_a_socket_that_fails_to_close_still_releases_the_transport`, `tests/test_execution_events.py::test_a_cancelled_teardown_still_releases_the_transport`, `tests/test_execution_events.py::test_a_frame_arriving_during_teardown_cannot_start_a_venue_sweep`, `tests/test_execution_events.py::test_the_account_poll_leaves_its_loop_when_the_gate_shuts` | These assert that teardown releases its transports and generates no venue traffic, not that each private channel was individually unsubscribed |
| TC-E84 | Reconcile open orders | offline-harness | `tests/test_execution_reports.py::TestOrderReportPagination::test_futures_open_orders_are_paged`, `tests/test_execution_reports.py::TestOrderReportPagination::test_spot_open_orders_are_paged`, `tests/test_execution_reports.py::TestMissingInstrumentHandling::test_unknown_instrument_is_loaded_rather_than_dropped`, `tests/test_execution_reports.py::TestMissingInstrumentHandling::test_order_reports_also_load_the_instrument`, `tests/test_execution_reports.py::TestUnreadableContractOrderFields::test_an_unreadable_deciding_field_fails_the_listing`, `tests/test_execution_reports.py::TestUnreadableSpotOrderFields::test_an_unreadable_deciding_field_fails_the_listing` | — |
| TC-E85 | Reconcile filled orders | offline-harness | `tests/test_execution_reports.py::TestSpotBaseFeeInReports::test_report_filled_qty_matches_the_event_stream`, `tests/test_execution_reports.py::TestSpotBaseFeeInReports::test_fully_filled_report_can_close_the_order`, `tests/test_execution_reports.py::TestFillReportPagination::test_futures_fills_are_paged_beyond_the_first_hundred`, `tests/test_execution_reports.py::TestFillReportPagination::test_spot_fills_are_paged` | — |
| TC-E86 | Reconcile open long position | offline-harness | `tests/test_execution_reports.py::TestStalePositionAnswersAfterRecovery::test_a_row_containing_the_booked_trades_answers_and_clears_the_memory` (LONG with the venue's quantity), `tests/test_execution_reports.py::TestUnreadablePositionSizes::test_an_unreadable_size_fails_the_per_instrument_query`, `tests/test_execution_reports.py::TestUnansweredPositionQueries::test_a_failed_per_instrument_query_raises_instead_of_answering_flat` | — |
| TC-E87 | Reconcile open short position | offline-harness | `tests/test_execution_reports.py::TestUnreadablePositionSizes::test_a_stringified_size_reads_exactly` (SHORT, quantity 4), plus the guards named under TC-E86 | — |

## Execution: Group 10 — Options

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-E90 | Limit BUY option | implemented | Options appear in refusals only: `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_options_limit_rejects_fok`, `tests/test_execution_orders.py::TestDisplayQuantity::test_options_hidden_order_is_refused_not_inverted`, `tests/test_execution_orders.py::TestPostOnlyWithImmediateTimeInForce::test_options_limit_is_refused` | No accepted option limit BUY body, and no accept event |
| TC-E91 | Limit SELL option | unit-tested | `tests/test_execution_modify.py::TestReduceOnlyOnRegularOrders::test_an_options_order_carries_reduce_only_too` — an option limit SELL reaches the options namespace with `size: -3` | The rest of the body, and the accept event |
| TC-E92 | Limit with alternative pricing | skipped | The specification routes this through `order_params`, which the client reads on no path (`gateio_nt/execution.py`, `_submit_order`), as recorded for TC-E63 | — |
| TC-E94 | Unsupported order type denied for options | offline-harness | `tests/test_execution_orders.py::TestDeniedVersusRejectedBoundary::test_a_globally_unsupported_type_is_denied_on_an_option_too`, `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_options_market_rejects_fok` | — |
| TC-E96 | Conditional order rejected for options | offline-harness | `tests/test_execution_orders.py::TestOrderRouting::test_options_have_no_conditional_endpoint` — Gate.io publishes no price-triggered endpoint for options, so the order is denied and nothing is sent | — |
| TC-E99 | FOK limit option | offline-harness | `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_options_limit_rejects_fok`, `tests/test_execution_orders.py::TestFillOrKillPerProduct::test_options_market_rejects_fok` — both assert the denial names "fill-or-kill" and that nothing left for the venue | — |
| TC-E100 | Cancel option order | implemented | The cancel round trip is asserted for spot, perpetual and delivery only (`tests/test_execution_cancel.py::TestCancelSucceeds::test_a_plain_limit_cancel_emits_exactly_one_order_canceled`); options are reached only in the finished listing, by `tests/test_execution_reports.py::TestClientOrderIdLookup::test_options_are_found_in_the_finished_listing` | The options cancel endpoint and its event |
| TC-E101 | Reconcile option position | unit-tested | `tests/test_execution_accounting.py::TestUnrealisedPnlIsCountedOnce::test_options_equity_is_not_reported_as_the_balance`, `tests/test_execution_reports.py::TestClientOrderIdLookup::test_options_are_found_in_the_finished_listing` | No option position status report is built from a venue row |

## Data: Group 1 — Instruments

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D01 | Request instruments | unit-tested | `tests/test_providers.py::TestUsesTypedHttpNamespaces::test_every_product_is_loaded_through_its_namespace`, `tests/test_providers.py::TestSpotFilters::test_tradable_pair_is_published`, `tests/test_providers.py::TestContractFilters::test_perpetual_is_published`, `tests/test_providers.py::TestOptionFilters::test_active_option_is_published`, `tests/test_instruments.py::TestPrecisionDerivation::test_spot_scales_come_from_precision_fields` | — |
| TC-D02 | Subscribe instrument | implemented | `_subscribe_instrument` and `_unsubscribe_instrument` exist (`gateio_nt/data.py:893`, `:1385`) | No test issues a `SubscribeInstrument` command or asserts an instrument reaching `on_instrument` |
| TC-D03 | Load specific instrument | unit-tested | `tests/test_providers.py::TestUsesTypedHttpNamespaces::test_single_instrument_loads_go_through_the_namespaces`, `tests/test_providers.py::TestSpotFilters::test_explicit_single_load_applies_the_same_filters` | — |

## Data: Group 2 — Order book

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D10 | Subscribe book deltas | unit-tested | `tests/test_data_client.py::test_book_deltas_published_for_every_level_container_shape`, `tests/test_data_client.py::test_updates_are_buffered_until_the_snapshot_arrives`, `tests/test_data_client.py::test_seeding_replays_the_buffer_and_publishes_a_clean_snapshot`, `tests/test_books.py::test_full_sequence_buffers_then_applies_from_the_straddling_update` | — |
| TC-D11 | Subscribe book at interval | unit-tested | `tests/test_data_client.py::test_per_product_intervals_match_the_venue`, `tests/test_data_client.py::test_configured_interval_is_clamped_per_product`, `tests/test_data_client.py::test_config_book_intervals_are_the_union_of_the_authoritative_table` | — |
| TC-D12 | Subscribe book depth (`OrderBookDepth10`) | unit-tested | `tests/test_data_book_depth.py::test_spot_depth_subscribes_the_snapshot_channel_at_ten_levels`, `tests/test_data_book_depth.py::test_a_spot_snapshot_publishes_one_depth_with_the_venue_levels`, `tests/test_data_book_depth.py::test_asymmetric_sides_are_padded_rather_than_dropped`, `tests/test_data_book_depth.py::test_a_depth_subscription_now_reaches_the_venue_and_publishes` | — |
| TC-D13 | Request book snapshot | unit-tested | `tests/test_data_client.py::test_options_snapshot_request_uses_the_options_depth_table`, `tests/test_data_client.py::test_nearest_snapshot_limit_is_per_product` | The depth argument is asserted, not the snapshot. One test reaches `_request_order_book_snapshot`, and it patches the delivery path away to isolate the depth table; nothing asserts that the request answers with a book, or what it answers with when the venue refuses |
| TC-D14 | Managed book from deltas | unit-tested | `tests/test_books.py::test_gap_raises_order_book_sequence_error_and_unsyncs`, `tests/test_books.py::test_stale_snapshot_is_rejected_and_leaves_the_book_untouched`, `tests/test_data_client.py::test_a_gap_resyncs_and_republishes_a_clean_snapshot`, `tests/test_data_client.py::test_a_stale_rest_snapshot_does_not_roll_the_book_back` | — |
| TC-D15 | Request historical book deltas | skipped | Gate.io publishes no historical order-book endpoint on any product; `GateioDataClient` declares no `_request_order_book_deltas` hook (`gateio_nt/data.py`), so the request type is not offered | — |

## Data: Group 3 — Quotes

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D20 | Subscribe quotes | unit-tested | `tests/test_data_client.py::test_book_ticker_publishes_a_quote_with_both_sizes`, `tests/test_data_client.py::test_book_ticker_with_an_unrepresentable_size_is_skipped` | The subscription command itself: no test drives `_subscribe_quote_ticks` and reads back the venue channel |
| TC-D21 | Request historical quotes | skipped | Gate.io publishes no bid/ask history on any product — quotes are the live `*.book_ticker` stream, and `GET /*/tickers` is a single current row, not a series. The client refuses the request in as many words rather than inventing history: `gateio_nt/data.py:2854-2877`, pinned by `tests/test_data_custom_types.py::test_a_historical_quote_request_is_refused_without_raising` and `tests/test_data_custom_types.py::test_the_refusal_never_touches_the_ticker_endpoint` | — |

## Data: Group 4 — Trades

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D30 | Subscribe trades | unit-tested | `tests/test_data_client.py::test_trade_with_an_id_keeps_the_venue_id_verbatim`, `tests/test_data_client.py::test_trade_without_an_id_is_dropped`, `tests/test_data_client.py::test_futures_trade_below_one_contract_is_dropped` | The subscription command itself, as TC-D20 |
| TC-D31 | Request historical trades | implemented | `_request_trade_ticks` is implemented and filters the venue window (`gateio_nt/data.py:2878-2917`) | No test calls it: nothing asserts the endpoint per product, the window filter or the published ticks |

## Data: Group 5 — Bars

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D40 | Subscribe bars | unit-tested | `tests/test_data_client.py::test_closed_bucket_is_published_on_the_clock_without_a_window_flag`, `tests/test_data_client.py::test_an_open_bucket_is_not_published_early`, `tests/test_data_client.py::test_a_newer_bucket_still_releases_the_previous_one`, `tests/test_data_client.py::test_a_bar_is_published_once` | — |
| TC-D41 | Request historical bars | unit-tested | `tests/test_data_client.py::test_one_malformed_candle_does_not_abort_the_whole_bar_request` | Ascending timestamps and OHLCV fidelity across a well-formed page are not asserted; the only test of this path feeds it a malformed row |

## Data: Group 6 — Derivatives data

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D50 | Subscribe mark prices | unit-tested | `tests/test_data_client.py::test_a_mark_price_keeps_the_venue_scale_instead_of_the_order_tick`, `tests/test_data_client.py::test_a_mark_price_scale_does_not_wobble_between_updates`, `tests/test_data_client.py::test_an_option_may_subscribe_to_mark_and_index_prices`, `tests/test_data_client.py::test_spot_may_not_subscribe_to_any_of_the_three` | — |
| TC-D51 | Subscribe index prices | unit-tested | `tests/test_data_client.py::test_an_index_price_keeps_the_venue_scale`, `tests/test_data_client.py::test_an_unparseable_reference_price_is_not_published_as_zero`, `tests/test_data_client.py::test_mark_and_index_prices_are_published_for_options` | — |
| TC-D52 | Subscribe funding rates | unit-tested | `tests/test_data_client.py::test_a_stale_next_funding_time_is_rolled_onto_the_funding_grid`, `tests/test_data_client.py::test_a_next_funding_time_still_ahead_is_published_verbatim`, `tests/test_data_client.py::test_next_funding_time_is_omitted_when_the_grid_step_is_unknown`, `tests/test_data_client.py::test_an_option_may_not_subscribe_to_funding_rates` | — |
| TC-D53 | Request historical funding rates | unit-tested | `tests/test_data_client.py::test_funding_rate_history_is_requested_and_published`, `tests/test_data_client.py::test_funding_rate_history_is_refused_for_a_product_without_funding` | — |

## Data: Group 7 — Instrument status

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D60 | Subscribe instrument status | unit-tested | `tests/test_instrument_lifecycle.py::test_subscribing_reports_the_current_status_at_once`, `tests/test_instrument_lifecycle.py::test_a_delisting_transition_is_emitted_exactly_once`, `tests/test_instrument_lifecycle.py::test_absence_from_a_listing_that_was_not_read_is_not_a_delisting`, `tests/test_instrument_lifecycle.py::test_unsubscribing_stops_emissions_for_that_instrument_only` | — |
| TC-D61 | Subscribe instrument close | unit-tested | `tests/test_instrument_lifecycle.py::test_a_settled_delivery_contract_publishes_its_close`, `tests/test_instrument_lifecycle.py::test_an_unsettled_delivery_contract_publishes_nothing`, `tests/test_instrument_lifecycle.py::test_an_option_close_is_its_own_value_not_the_underlying_price`, `tests/test_instrument_lifecycle.py::test_a_close_is_published_at_most_once` | — |

## Data: Group 8 — Option greeks

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D62 | Subscribe option greeks | unit-tested | `tests/test_data_custom_types.py::test_a_platform_greeks_subscription_now_delivers_on_gateio`, `tests/test_data_custom_types.py::test_each_venue_number_lands_on_the_field_that_names_it`, `tests/test_data_custom_types.py::test_a_row_that_omits_one_greek_publishes_nothing_rather_than_a_zero`, `tests/test_data_custom_types.py::test_a_row_of_zeros_is_published_because_zero_is_what_it_says`, `tests/test_data_custom_types.py::test_greeks_are_refused_on_a_product_that_has_no_greeks` | — |
| TC-D63 | Subscribe option chain | skipped | The platform declares no option-chain subscription for an adapter to serve, and Gate.io publishes no chain channel: greeks arrive per contract on `options.tickers` (`gateio_nt/data.py`, the options ticker handler) | — |

## Data: Group 9 — Lifecycle

| Case | Name | Rung | Evidence | Not yet asserted |
|---|---|---|---|---|
| TC-D70 | Unsubscribe on stop (data) | unit-tested | `tests/test_data_book_depth.py::test_unsubscribe_repeats_the_payload_that_was_subscribed`, `tests/test_data_book_depth.py::test_unsubscribing_depth_clears_the_sequence_watermark`, `tests/test_data_custom_types.py::test_a_refused_ticker_subscription_drops_the_registry_entry`, `tests/test_data_client.py::test_disconnect_settles_its_tasks_before_releasing_the_transports` | Unsubscribe is asserted for depth, tickers and greeks; quotes, trades and bars are not driven through their unsubscribe hooks |
| TC-D71 | Custom subscribe params | unit-tested | The adapter does accept subscription parameters: `tests/test_data_book_depth.py::test_a_spot_push_interval_may_be_chosen_through_command_params` (a spot push interval chosen through `params`) and `tests/test_data_book_depth.py::test_an_interval_the_product_rejects_falls_back` (a parameter the product cannot serve falls back instead of failing) | Only the book-depth interval is parametrizable; no other subscription reads `params` |
| TC-D72 | Custom request params | skipped | No request path reads `request.params`; the one hook that receives them refuses the request itself, and does so without choking on the dict: `tests/test_data_custom_types.py::test_the_refusal_reads_request_params_without_choking`. The specification marks this case *N/A (adapter-specific)* | — |

---

## Summary

| Rung | Cases |
|---|---|
| offline-harness | 28 |
| unit-tested | 42 |
| implemented | 11 |
| skipped | 14 |
| mainnet-confirmed | 0 |
| **Total** | **95** |

The eleven rows at *implemented* are the open gaps: TC-E22, TC-E23, TC-E27,
TC-E33, TC-E90, TC-E100 and TC-D02, TC-D31 are offline-closable and are the work
this page exists to make visible; TC-E06, TC-E80 and TC-E82 are start-up and
shutdown behaviours of the `ExecTester` rather than of this adapter, and are
reachable only in a live session.

Nothing on this page is a claim about Gate.io. Every rung below
*mainnet-confirmed* means the venue has never seen that path; what it has seen
is recorded on [validation.md](validation.md).
