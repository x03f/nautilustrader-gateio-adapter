"""Reconciliation-report tests for ``GateioExecutionClient``.

Covers the report builders, their pagination, and the instrument resolution that
decides whether venue state is reconciled or lost.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
)
from nautilus_trader.model.enums import (
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import PositionId, TradeId, VenueOrderId
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.position import Position

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioError, WalletNotProvisionedError
from nautilus_gateio.execution import (
    REPORT_PAGE_LIMIT,
    FillReportsUnavailable,
    PositionStatusUnavailable,
)

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        FUT_BTC_USDT,
        OPT_BTC_USDT,
        PERP_BTC_USDT,
        PERP_CONTRACT_PAYLOAD,
        SPOT_BTC_USDT,
        SPOT_PAIR_PAYLOAD,
        ExecHarness,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
        FUT_BTC_USDT,
        OPT_BTC_USDT,
        PERP_BTC_USDT,
        PERP_CONTRACT_PAYLOAD,
        SPOT_BTC_USDT,
        SPOT_PAIR_PAYLOAD,
        ExecHarness,
    )

from nautilus_gateio.instruments import parse_perpetual_instrument, parse_spot_instrument

WINDOW_START = datetime(2026, 7, 25, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 27, tzinfo=UTC)
INSIDE_SECS = int(datetime(2026, 7, 26, tzinfo=UTC).timestamp())
BEFORE_SECS = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp())


def _fill_reports_command(instrument_id: Any = None) -> GenerateFillReports:
    return GenerateFillReports(
        instrument_id=instrument_id,
        venue_order_id=None,
        start=WINDOW_START,
        end=WINDOW_END,
        command_id=UUID4(),
        ts_init=0,
    )


def _order_reports_command(open_only: bool = False) -> GenerateOrderStatusReports:
    return GenerateOrderStatusReports(
        instrument_id=None,
        start=WINDOW_START,
        end=WINDOW_END,
        open_only=open_only,
        command_id=UUID4(),
        ts_init=0,
    )


def _paged(rows: list[dict[str, Any]], cursor_key: str) -> Any:
    """Return a stub responder that serves ``rows`` one page at a time."""

    def _fetch(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        cursor = kwargs.get(cursor_key, 1 if cursor_key == "page" else 0)
        start = (cursor - 1) * REPORT_PAGE_LIMIT if cursor_key == "page" else cursor
        return rows[start : start + REPORT_PAGE_LIMIT]

    return _fetch


def _futures_fills(count: int, create_time: int = INSIDE_SECS) -> list[dict[str, Any]]:
    return [
        {
            "id": f"T-{index}",
            "contract": "BTC_USDT",
            "order_id": "900001",
            "size": -1,
            "price": "59000.0",
            "role": "taker",
            "fee": "0.01",
            "create_time": create_time,
        }
        for index in range(count)
    ]


# -- MANDATORY-ADJACENT: the report and the fills must state the same order ---


class TestSpotBaseFeeInReports:
    """The report states the venue's own quantities, exactly as the fills do.

    A spot BUY is commissioned in the currency being bought, and it is the
    platform that takes that off the position (``Position.apply``,
    model/position.pyx:591-612) — neither the fill nor the report nets it.
    They have to agree, and on the venue's own numbers: ``_should_update``
    (live/execution_engine.py:3307) restates the order to ``report.quantity``
    whenever it differs, and ``_handle_fill_quantity_mismatch`` (:3164) makes up
    an inferred fill for whatever ``report.filled_qty`` claims beyond the fills
    on the order. A report netted of a fee the fills are not would be undone on
    one reconciliation pass and inflated on the next.
    """

    PAYLOAD: dict[str, Any] = {
        "id": "778899",
        "currency_pair": "BTC_USDT",
        "type": "limit",
        "side": "buy",
        "amount": "0.010000",
        "left": "0",
        "filled_amount": "0.010000",
        "filled_total": "600",
        "avg_deal_price": "60000",
        "fee": "0.000010",
        "fee_currency": "BTC",
        "status": "closed",
        "finish_as": "filled",
        "time_in_force": "gtc",
        "create_time": INSIDE_SECS,
        "update_time": INSIDE_SECS,
    }

    def test_report_filled_qty_matches_the_event_stream(self, spot_env):
        env = spot_env
        report = env.client._parse_order_status_report(
            GateioProductType.SPOT,
            self.PAYLOAD,
            env.instruments[0],
        )
        assert report is not None

        fill_qty, commission = env.client._fill_quantity_and_commission(
            GateioProductType.SPOT,
            {
                "amount": "0.010000",
                "fee": "0.000010",
                "fee_currency": "BTC",
                "price": "60000.00",
            },
            env.instruments[0],
        )
        assert report.filled_qty == fill_qty
        assert report.filled_qty == Quantity.from_str("0.010000")
        # The fee is a fact of its own, on the fill and nowhere else.
        assert commission.as_decimal() == Decimal("0.000010")

    def test_fully_filled_report_can_close_the_order(self, spot_env):
        env = spot_env
        report = env.client._parse_order_status_report(
            GateioProductType.SPOT,
            self.PAYLOAD,
            env.instruments[0],
        )
        assert report is not None
        assert report.order_status == OrderStatus.FILLED
        # A FILLED report whose filled_qty is short of its quantity leaves the
        # order permanently open.
        assert report.filled_qty == report.quantity

    def test_partial_fill_report_states_the_venues_own_quantities(self, spot_env):
        """A partially filled report must agree with the order it describes.

        Netting the fee off ``filled_qty`` only while the order is still working
        was the other half of the double count: the report claimed less filled
        than the order had, so every startup reconciliation restated the
        quantity back and published a quantity change the venue never made.
        """
        env = spot_env
        payload = dict(
            self.PAYLOAD,
            left="0.004000",
            filled_amount="0.006000",
            fee="0.000006",
            status="open",
            finish_as=None,
        )
        report = env.client._parse_order_status_report(
            GateioProductType.SPOT,
            payload,
            env.instruments[0],
        )
        assert report is not None
        assert report.quantity == Quantity.from_str("0.010000")
        assert report.filled_qty == Quantity.from_str("0.006000")
        assert report.order_status == OrderStatus.PARTIALLY_FILLED

    def test_quote_currency_fee_is_not_netted(self, spot_env):
        env = spot_env
        payload = dict(self.PAYLOAD, side="sell", fee="6", fee_currency="USDT")
        report = env.client._parse_order_status_report(
            GateioProductType.SPOT,
            payload,
            env.instruments[0],
        )
        assert report is not None
        assert report.filled_qty == Quantity.from_str("0.010000")
        assert report.quantity == Quantity.from_str("0.010000")


# -- EXEC-6 / DP-8: pagination ------------------------------------------------


class TestFillReportPagination:
    def test_futures_fills_are_paged_beyond_the_first_hundred(self, perp_env):
        env = perp_env
        rows = _futures_fills(REPORT_PAGE_LIMIT + 50)
        env.perp.responses["my_trades"] = _paged(rows, "offset")

        reports = env.run(env.client.generate_fill_reports(_fill_reports_command()))

        assert len(reports) == REPORT_PAGE_LIMIT + 50
        offsets = [call.kwargs["offset"] for call in env.perp.calls_named("my_trades")]
        assert offsets == [0, REPORT_PAGE_LIMIT]

    def test_futures_paging_stops_once_the_window_start_is_passed(self, perp_env):
        env = perp_env
        first_page = _futures_fills(REPORT_PAGE_LIMIT - 1)
        first_page.append(_futures_fills(1, create_time=BEFORE_SECS)[0])
        second_page = _futures_fills(REPORT_PAGE_LIMIT)
        pages = {0: first_page, REPORT_PAGE_LIMIT: second_page}
        env.perp.responses["my_trades"] = lambda **kwargs: pages.get(kwargs.get("offset", 0), [])

        reports = env.run(env.client.generate_fill_reports(_fill_reports_command()))

        # The out-of-window row proves every further page is older still.
        assert len(env.perp.calls_named("my_trades")) == 1
        assert len(reports) == REPORT_PAGE_LIMIT - 1

    def test_spot_fills_are_paged(self, spot_env):
        env = spot_env
        rows = [
            {
                "id": f"S-{index}",
                "currency_pair": "BTC_USDT",
                "order_id": "778899",
                "side": "buy",
                "amount": "0.001000",
                "price": "60000.00",
                "fee": "0.0000001",
                "fee_currency": "USDT",
                "role": "taker",
                "create_time": INSIDE_SECS,
            }
            for index in range(REPORT_PAGE_LIMIT + 5)
        ]
        env.spot.responses["my_trades"] = _paged(rows, "page")

        reports = env.run(
            env.client.generate_fill_reports(_fill_reports_command(SPOT_BTC_USDT)),
        )

        assert len(reports) == REPORT_PAGE_LIMIT + 5
        pages = [call.kwargs["page"] for call in env.spot.calls_named("my_trades")]
        assert pages == [1, 2]

    def test_paging_is_capped(self, perp_env):
        """A venue that always answers a full page must not loop forever."""
        env = perp_env
        env.perp.responses["my_trades"] = lambda **kwargs: _futures_fills(REPORT_PAGE_LIMIT)

        env.run(env.client.generate_fill_reports(_fill_reports_command()))

        from nautilus_gateio.execution import MAX_REPORT_PAGES

        assert len(env.perp.calls_named("my_trades")) == MAX_REPORT_PAGES


class TestOrderReportPagination:
    def test_futures_open_orders_are_paged(self, perp_env):
        env = perp_env
        rows = [
            {
                "id": 1000 + index,
                "id_string": str(1000 + index),
                "contract": "BTC_USDT",
                "size": -1,
                "left": -1,
                "price": "59000.0",
                "tif": "gtc",
                "status": "open",
                "create_time": INSIDE_SECS,
            }
            for index in range(REPORT_PAGE_LIMIT + 3)
        ]
        env.perp.responses["list_orders"] = lambda **kwargs: (
            _paged(rows, "offset")(**kwargs) if kwargs.get("status") == "open" else []
        )

        reports = env.run(env.client.generate_order_status_reports(_order_reports_command(True)))

        assert len(reports) == REPORT_PAGE_LIMIT + 3
        offsets = [
            call.kwargs["offset"]
            for call in env.perp.calls_named("list_orders")
            if call.kwargs.get("status") == "open"
        ]
        assert offsets == [0, REPORT_PAGE_LIMIT]

    def test_spot_open_orders_are_paged(self, spot_env):
        env = spot_env
        groups = [
            {
                "currency_pair": "BTC_USDT",
                "total": 1,
                "orders": [
                    {
                        "id": str(2000 + index),
                        "currency_pair": "BTC_USDT",
                        "type": "limit",
                        "side": "buy",
                        "amount": "0.010000",
                        "left": "0.010000",
                        "filled_amount": "0",
                        "price": "59000.00",
                        "status": "open",
                        "time_in_force": "gtc",
                        "create_time": INSIDE_SECS,
                    },
                ],
            }
            for index in range(REPORT_PAGE_LIMIT + 1)
        ]
        env.spot.responses["open_orders"] = _paged(groups, "page")

        reports = env.run(env.client.generate_order_status_reports(_order_reports_command(True)))

        assert len(reports) == REPORT_PAGE_LIMIT + 1
        pages = [call.kwargs["page"] for call in env.spot.calls_named("open_orders")]
        assert pages == [1, 2]


# -- GIO-DOM-1: never discard venue state because of a missing instrument -----


class TestMissingInstrumentHandling:
    @staticmethod
    def _spot_only_env() -> ExecHarness:
        spot = parse_spot_instrument(SPOT_PAIR_PAYLOAD)
        env = ExecHarness(
            products=(GateioProductType.SPOT, GateioProductType.PERP),
            instruments=[spot],
        )
        return env

    def test_unknown_instrument_is_loaded_rather_than_dropped(self):
        env = self._spot_only_env()
        try:
            perp = parse_perpetual_instrument(PERP_CONTRACT_PAYLOAD, GateioProductType.PERP)
            env.provider.loadable[PERP_BTC_USDT] = perp
            env.perp.responses["my_trades"] = lambda **kwargs: (
                _futures_fills(1) if kwargs.get("offset", 0) == 0 else []
            )

            reports = env.run(env.client.generate_fill_reports(_fill_reports_command()))

            assert env.provider.load_requests == [PERP_BTC_USDT]
            assert len(reports) == 1
            assert reports[0].instrument_id == PERP_BTC_USDT
        finally:
            env.close()

    def test_unloadable_instrument_is_dropped_after_an_attempt(self):
        env = self._spot_only_env()
        try:
            env.perp.responses["my_trades"] = lambda **kwargs: (
                _futures_fills(1) if kwargs.get("offset", 0) == 0 else []
            )

            reports = env.run(env.client.generate_fill_reports(_fill_reports_command()))

            # The loss is explicit: the client asked the venue before giving up.
            assert env.provider.load_requests == [PERP_BTC_USDT]
            assert reports == []
        finally:
            env.close()

    def test_order_reports_also_load_the_instrument(self):
        env = self._spot_only_env()
        try:
            perp = parse_perpetual_instrument(PERP_CONTRACT_PAYLOAD, GateioProductType.PERP)
            env.provider.loadable[PERP_BTC_USDT] = perp
            env.perp.responses["list_orders"] = lambda **kwargs: (
                [
                    {
                        "id": 1001,
                        "id_string": "1001",
                        "contract": "BTC_USDT",
                        "size": -1,
                        "left": -1,
                        "price": "59000.0",
                        "tif": "gtc",
                        "status": "open",
                        "create_time": INSIDE_SECS,
                    },
                ]
                if kwargs.get("status") == "open"
                else []
            )

            reports = env.run(
                env.client.generate_order_status_reports(_order_reports_command(True)),
            )

            assert env.provider.load_requests == [PERP_BTC_USDT]
            assert [report.instrument_id for report in reports] == [PERP_BTC_USDT]
        finally:
            env.close()

    def test_position_reports_also_load_the_instrument(self):
        env = self._spot_only_env()
        try:
            perp = parse_perpetual_instrument(PERP_CONTRACT_PAYLOAD, GateioProductType.PERP)
            env.provider.loadable[PERP_BTC_USDT] = perp
            env.perp.responses["positions"] = [
                {
                    "contract": "BTC_USDT",
                    "size": -20,
                    "entry_price": "59000",
                    "update_time": INSIDE_SECS,
                },
            ]

            reports = env.run(
                env.client.generate_position_status_reports(
                    GeneratePositionStatusReports(
                        instrument_id=None,
                        start=None,
                        end=None,
                        command_id=UUID4(),
                        ts_init=0,
                    ),
                ),
            )

            assert [report.instrument_id for report in reports] == [PERP_BTC_USDT]
            assert reports[0].position_side == PositionSide.SHORT
            assert reports[0].quantity == Quantity.from_int(20)
        finally:
            env.close()


# -- positions the venue does not keep ----------------------------------------


def _position_command(instrument_id: Any) -> GeneratePositionStatusReports:
    return GeneratePositionStatusReports(
        instrument_id=instrument_id,
        start=None,
        end=None,
        command_id=UUID4(),
        ts_init=0,
    )


def _open_futures_orders() -> list[dict[str, Any]]:
    """One resting perpetual order, as the venue lists it."""
    return [
        {
            "id": 900001,
            "contract": "BTC_USDT",
            "size": -10,
            "left": -10,
            "price": "60000",
            "status": "open",
            "create_time": INSIDE_SECS,
            "text": "t-O-1",
        },
    ]


def _cache_open_position(env: ExecHarness, instrument_id: Any, quantity: Quantity) -> Position:
    """Leave an open Nautilus position on ``instrument_id`` in the cache.

    Nautilus opens a position from a fill on any instrument, spot included, so
    this is the state the engine's periodic position check walks — the reason the
    account-wide answer is read as a claim about every instrument it omits.
    """
    instrument = env.cache.instrument(instrument_id)
    order = env.order_factory.market(instrument_id, OrderSide.BUY, quantity)
    env.accepted(order, "P-1")
    env.client.generate_order_filled(
        strategy_id=order.strategy_id,
        instrument_id=instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("P-1"),
        venue_position_id=PositionId(f"{instrument_id}-POS"),
        trade_id=TradeId("P-T-1"),
        order_side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        last_qty=quantity,
        last_px=instrument.make_price(Decimal("60000")),
        quote_currency=instrument.quote_currency,
        commission=Money(0, instrument.quote_currency),
        liquidity_side=LiquiditySide.TAKER,
        ts_event=env.clock.timestamp_ns(),
    )
    env.drain(order)
    fill = env.events_of(OrderFilled)[-1]
    position = Position(instrument=instrument, fill=fill)
    env.cache.add_position(position, OmsType.NETTING)
    return position


class TestSpotPositionsAreNotReported:
    """Regression: a FLAT report is a claim about the venue, and spot has none.

    Startup reconciliation asks this client, one instrument at a time, about
    every open position the cache holds — and Nautilus opens a position from a
    spot fill like any other. Answering FLAT made the engine square the book with
    a reconciliation order and an inferred fill: an execution that never
    happened. Gate.io has no spot position endpoint and a spot balance is not a
    position, so there is nothing to report and nothing to be flat about.
    """

    def test_a_spot_position_query_is_answered_as_not_applicable(self, spot_env):
        env = spot_env

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(SPOT_BTC_USDT)),
        )

        assert reports == []

    def test_a_futures_position_query_is_still_answered_flat(self, perp_env):
        """The FLAT answer stays where the venue really can say "no position".

        Without it a futures position closed at the venue could never be closed
        locally, which is why the fallback exists at all. The row here is the one
        Gate.io sends for a contract whose position is closed: it names the
        contract and reports zero size, so it is a statement about the ledger and
        must square the book. A row the client could not read is the opposite
        case, and is covered by ``TestUnreadablePositionRows``.
        """
        env = perp_env
        env.perp.responses["position"] = {
            "contract": "BTC_USDT",
            "size": 0,
            "entry_price": "0",
        }

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert [report.position_side for report in reports] == [PositionSide.FLAT]
        assert reports[0].instrument_id == PERP_BTC_USDT

    def test_an_account_wide_query_reports_no_spot_position(self, spot_env):
        """The same rule with no instrument named: spot is skipped, not flattened."""
        env = spot_env

        reports = env.run(env.client.generate_position_status_reports(_position_command(None)))

        assert reports == []

    def test_an_instrument_this_client_does_not_route_is_not_reported_flat(self, spot_env):
        """A FLAT answer needs a question, and an unrouted product was never asked.

        The product loop only queries the configured products, so for anything
        else the fallback would answer FLAT off the back of no venue call at all
        — the same claim-without-an-answer as the spot case, one product wider.
        """
        env = spot_env  # spot only; the perpetual is not routed here

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert reports == []


class TestUnansweredPositionQueries:
    """Regression: "the venue did not answer" may not become "the venue is flat".

    A raise is the only way this client can tell ``LiveExecutionEngine`` that a
    position query went unanswered — ``_did_position_status_query_fail`` skips a
    venue whose query raised, and the startup path counts the raise as a failed
    reconciliation. Swallowing the error and falling through to FLAT closes a
    position that is still open at the venue, with a RECONCILIATION order and an
    inferred fill: an execution that never happened, on the product where it does
    the most damage.
    """

    def test_a_failed_per_instrument_query_raises_instead_of_answering_flat(self, perp_env):
        env = perp_env
        env.perp.responses["position"] = RuntimeError("502 Bad Gateway from the venue")

        with pytest.raises(PositionStatusUnavailable):
            env.run(
                env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
            )

    def test_a_failed_account_wide_query_raises_instead_of_answering_nothing(self, perp_env):
        """An incomplete sweep is not an answer either.

        The engine's periodic check reads omission from an account-wide answer as
        flatness, so half a sweep is a claim about the half that is missing.
        """
        env = perp_env
        env.perp.responses["positions"] = RuntimeError("502 Bad Gateway from the venue")

        with pytest.raises(PositionStatusUnavailable):
            env.run(env.client.generate_position_status_reports(_position_command(None)))

    def test_an_unprovisioned_wallet_is_an_answer_and_stays_one(self, perp_env):
        """Gate.io says USER_NOT_FOUND until a product wallet exists.

        That is a definite "there is no position here", not an unanswered
        question, so it must not be turned into a failure — an account trading
        only spot has to be able to start.
        """
        env = perp_env
        env.perp.responses["position"] = WalletNotProvisionedError("no futures wallet yet")

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert [report.position_side for report in reports] == [PositionSide.FLAT]

    def test_an_account_wide_sweep_refuses_to_omit_a_position_it_cannot_report(self, spot_env):
        """The periodic check builds the FLAT report itself for anything omitted.

        ``LiveExecutionEngine._process_cached_position_discrepancies`` walks its
        own cached open positions and, for each one the account-wide answer did
        not mention, calls ``_create_flat_position_report`` and squares the book
        against it. To that caller an empty answer and a FLAT report are the same
        answer, so silence is only honest while there is nothing to be silent
        about.
        """
        env = spot_env
        _cache_open_position(env, SPOT_BTC_USDT, Quantity.from_str("0.010000"))

        with pytest.raises(PositionStatusUnavailable):
            env.run(env.client.generate_position_status_reports(_position_command(None)))

    def test_an_account_wide_sweep_answers_when_it_can_speak_for_every_position(self, perp_env):
        """The refusal is scoped to what this client cannot report on.

        A derivatives-only account keeps a fully working periodic check: the
        venue really can answer for every open position, so an empty answer means
        flat and the engine is right to act on it.
        """
        env = perp_env
        _cache_open_position(env, PERP_BTC_USDT, Quantity.from_int(4))

        reports = env.run(env.client.generate_position_status_reports(_position_command(None)))

        assert reports == []

    def test_the_startup_mass_status_survives_an_unanswerable_position_query(self, perp_env):
        """One 502 on the position endpoint may not cost the order recovery.

        The inherited ``generate_mass_status`` returns ``None`` — no orders, no
        fills, no reconciliation at all — if any of its three queries raises, and
        the position query now raises by design. Positions lose nothing by being
        left out: startup reconciliation queries them per instrument straight
        afterwards, where "the venue did not answer" is handled correctly.
        """
        env = perp_env
        env.perp.responses["positions"] = RuntimeError("502 Bad Gateway from the venue")
        env.perp.responses["list_orders"] = lambda **kwargs: (
            _open_futures_orders() if kwargs.get("status") == "open" else []
        )

        mass_status = env.run(env.client.generate_mass_status())

        assert mass_status is not None
        assert [str(key) for key in mass_status.order_reports] == ["900001"]
        assert mass_status.position_reports == {}


class TestUnreadablePositionRows:
    """Regression (REC-02): a row this client cannot parse is not an empty answer.

    The venue said something; the client failed to read it. Dropping the row
    leaves the query answering with fewer reports than the venue sent, and both
    callers above read that as flatness — the per-instrument route builds the
    FLAT report here, and the engine's account-wide check builds it itself for
    any cached open position the answer omitted. Either way a live position is
    squared with a RECONCILIATION order and an inferred fill.

    The distinction being defended is between a row that *reports* zero and a row
    that could not be read; ``TestSpotPositionsAreNotReported`` holds the first
    half, so a fix that simply stopped reporting positions would fail there.
    """

    @pytest.mark.parametrize(
        ("answer", "why"),
        [
            ({"size": -4, "entry_price": "59000"}, "no contract field"),
            (None, "an empty 200 body, which the HTTP client returns as None"),
            ("BTC_USDT", "a row that is not an object"),
            ([{"size": -4}], "a list whose only row carries no contract"),
        ],
    )
    def test_a_row_that_cannot_be_read_fails_the_per_instrument_query(
        self,
        perp_env,
        answer,
        why,
    ):
        env = perp_env
        env.perp.responses["position"] = answer

        with pytest.raises(PositionStatusUnavailable):
            env.run(
                env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
            )

    @pytest.mark.parametrize(
        ("answer", "why"),
        [
            ([{"size": -4, "entry_price": "59000"}], "no contract field"),
            (None, "an empty 200 body, which the HTTP client returns as None"),
            (["BTC_USDT"], "a row that is not an object"),
        ],
    )
    def test_a_row_that_cannot_be_read_fails_the_account_wide_sweep(self, perp_env, answer, why):
        """The account-wide route is the one the periodic position check uses.

        It never sees this client's FLAT fallback: the engine manufactures the
        flat report for anything the answer omitted, so a dropped row and an
        answer that never mentioned the instrument are the same claim to it.
        """
        env = perp_env
        env.perp.responses["positions"] = answer

        with pytest.raises(PositionStatusUnavailable):
            env.run(env.client.generate_position_status_reports(_position_command(None)))

    def test_an_unloadable_instrument_fails_the_query_rather_than_vanishing(self, perp_env):
        """A row about a contract this client cannot resolve is still a row.

        It names a position the venue is reporting. Dropping it silently answers
        for a ledger that was never read, which is the same claim by a different
        route.
        """
        env = perp_env
        env.perp.responses["positions"] = [
            {"contract": "ETH_USDT", "size": -4, "entry_price": "3000"},
        ]

        with pytest.raises(PositionStatusUnavailable):
            env.run(env.client.generate_position_status_reports(_position_command(None)))


class TestUnreadablePositionSizes:
    """Regression (REC-02 remainder): the field that decides the answer is read strictly.

    The row-shape cases above catch a row that is not an object or cannot be
    attributed to an instrument. This class holds the field that decides the
    answer itself: ``size`` was read with a forgiving helper that returns 0 for
    a missing key, null, an empty string, a non-numeric string and any
    magnitude truncating below one lot — and 0 is not a default here, it is the
    affirmative claim FLAT, which the engine squares a live book against with a
    reconciliation order and an invented fill. Gate.io moved every futures size
    field from integer to string in v4.106.0, which is what makes an unreadable
    shape reachable rather than hypothetical.
    """

    UNREADABLE = [
        ({"contract": "BTC_USDT", "entry_price": "59000"}, "the size key is absent"),
        ({"contract": "BTC_USDT", "size": None}, "size is null"),
        ({"contract": "BTC_USDT", "size": ""}, "size is an empty string"),
        ({"contract": "BTC_USDT", "size": "abc"}, "size is a non-numeric string"),
        ({"contract": "BTC_USDT", "size": {"long": -4}}, "size is an object"),
        ({"contract": "BTC_USDT", "size": [-4]}, "size is a list"),
        ({"contract": "BTC_USDT", "size": "-0.5"}, "size truncates below one lot"),
        ({"contract": "BTC_USDT", "size": True}, "size is a boolean"),
        ({"contract": "BTC_USDT", "size": "NaN"}, "size is not a number at all"),
    ]

    @pytest.mark.parametrize(("row", "why"), UNREADABLE)
    def test_an_unreadable_size_fails_the_per_instrument_query(self, perp_env, row, why):
        env = perp_env
        env.perp.responses["position"] = [row]

        with pytest.raises(PositionStatusUnavailable) as excinfo:
            env.run(
                env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
            )

        # The raise names the row and the field, so an operator is sent to the
        # venue payload rather than left with a phantom flat position and no log.
        assert "size" in str(excinfo.value)
        assert "1 of 1" in str(excinfo.value)

    @pytest.mark.parametrize(("row", "why"), UNREADABLE)
    def test_an_unreadable_size_fails_the_account_wide_sweep(self, perp_env, row, why):
        env = perp_env
        env.perp.responses["positions"] = [row]

        with pytest.raises(PositionStatusUnavailable):
            env.run(env.client.generate_position_status_reports(_position_command(None)))

    @pytest.mark.parametrize("zero", [0, "0", "0.0"])
    def test_a_size_that_reads_zero_is_still_an_answer(self, perp_env, zero):
        """The venue stated the position is gone; the book must still square."""
        env = perp_env
        env.perp.responses["position"] = [
            {"contract": "BTC_USDT", "size": zero, "entry_price": "0", "update_time": INSIDE_SECS},
        ]

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert [report.position_side for report in reports] == [PositionSide.FLAT]

    def test_a_stringified_size_reads_exactly(self, perp_env):
        """v4.106.0 sends sizes as strings: "-4" is four lots short, not flat."""
        env = perp_env
        env.perp.responses["position"] = [
            {
                "contract": "BTC_USDT",
                "size": "-4",
                "entry_price": "59000",
                "update_time": INSIDE_SECS,
            },
        ]

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert [report.position_side for report in reports] == [PositionSide.SHORT]
        assert reports[0].quantity == Quantity.from_int(4)


class TestStalePositionAnswersAfterRecovery:
    """Regression (REC-01, position half): a row recovery outran is not an answer.

    Recovery reads the trade listing and the position listing at separate
    instants, so a position answer can predate a venue trade recovery just
    booked. The engine takes any position report as current truth and squares
    the book against it — which would delete the venue trade id, price and fee
    just booked and replace them with an invented execution. While the answer
    is exactly the pre-trade book and cannot be shown to postdate the trades,
    the only honest answer is that the query is not answered yet.
    """

    @staticmethod
    def _after_recovery_booked(env: ExecHarness, lots: int, booked_ts_ns: int) -> None:
        """Leave the state the recovery sweep leaves behind.

        A position opened by the booked trades, and the client's memory of
        having booked them in this recovery pass.
        """
        _cache_open_position(env, PERP_BTC_USDT, Quantity.from_int(lots))
        env.client._recovery_booked[PERP_BTC_USDT] = (Decimal(lots), booked_ts_ns)

    def test_a_row_equal_to_the_pre_booking_book_is_withheld(self, perp_env):
        env = perp_env
        self._after_recovery_booked(env, 4, booked_ts_ns=2**62)
        env.perp.responses["position"] = [
            {"contract": "BTC_USDT", "size": 0, "entry_price": "0", "update_time": INSIDE_SECS},
        ]

        with pytest.raises(PositionStatusUnavailable):
            env.run(
                env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
            )

    def test_an_absent_row_equal_to_the_pre_booking_book_is_withheld(self, perp_env):
        """An absent row carries no timestamp at all, so it can never postdate
        the booked trades; while it matches the pre-trade book it is a stale
        read, not the venue saying flat."""
        env = perp_env
        self._after_recovery_booked(env, 4, booked_ts_ns=2**62)
        env.perp.responses["position"] = []

        with pytest.raises(PositionStatusUnavailable):
            env.run(
                env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
            )

    def test_a_row_stamped_after_the_booked_trades_stands_whatever_it_says(self, perp_env):
        """A fresher row wins even when it reads flat: the venue has spoken since."""
        env = perp_env
        self._after_recovery_booked(env, 4, booked_ts_ns=1)
        env.perp.responses["position"] = [
            {"contract": "BTC_USDT", "size": 0, "entry_price": "0", "update_time": INSIDE_SECS},
        ]

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert [report.position_side for report in reports] == [PositionSide.FLAT]
        assert PERP_BTC_USDT not in env.client._recovery_booked

    def test_a_row_containing_the_booked_trades_answers_and_clears_the_memory(self, perp_env):
        env = perp_env
        self._after_recovery_booked(env, 4, booked_ts_ns=2**62)
        env.perp.responses["position"] = [
            {
                "contract": "BTC_USDT",
                "size": 4,
                "entry_price": "60000",
                "update_time": INSIDE_SECS,
            },
        ]

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert [report.position_side for report in reports] == [PositionSide.LONG]
        assert PERP_BTC_USDT not in env.client._recovery_booked

    def test_a_disagreement_the_bookings_do_not_explain_is_reported(self, perp_env):
        """A difference beyond the booked trades is a genuine discrepancy: the
        engine must see it, or position reconciliation would be blinded."""
        env = perp_env
        self._after_recovery_booked(env, 4, booked_ts_ns=2**62)
        env.perp.responses["position"] = [
            {
                "contract": "BTC_USDT",
                "size": 9,
                "entry_price": "60000",
                "update_time": INSIDE_SECS,
            },
        ]

        reports = env.run(
            env.client.generate_position_status_reports(_position_command(PERP_BTC_USDT)),
        )

        assert [report.position_side for report in reports] == [PositionSide.LONG]
        assert reports[0].quantity == Quantity.from_int(9)


class TestFailedFillQueriesAreSurfaced:
    """Regression (REC-03): a failed trade listing may not be reported as no trades.

    ``LiveExecutionEngine`` keeps one brake against squaring a position to flat:
    ``_process_cached_position_discrepancies`` does it only when
    ``had_fill_query_errors`` is False, and ``_query_and_find_missing_fills``
    sets that flag from a ``generate_fill_reports`` that *raises* and from
    nothing else. A client that logs each per-product failure and returns what it
    collected reports "no fills", the brake never engages, and the position is
    closed with a synthetic trade id and no commission — permanently, because a
    closed position is never queried again and the venue's real closing trade is
    never applied.
    """

    def test_a_failed_product_makes_the_query_raise(self, perp_env):
        env = perp_env
        env.perp.responses["my_trades"] = RuntimeError("500 from the trade listing")

        with pytest.raises(FillReportsUnavailable):
            env.run(env.client.generate_fill_reports(_fill_reports_command()))

    def test_the_failure_carries_what_the_other_products_did_answer(self, spot_and_perp_env):
        """Recovery works from what the venue did answer, so it is not thrown away.

        The exception is what the position paths need; the reports are what the
        restart and reconnect recoveries need. Raising a bare error would let one
        5xx on one product's trade listing cost the order and fill recovery a
        restart exists to perform.
        """
        env = spot_and_perp_env
        env.spot.responses["my_trades"] = RuntimeError("500 from the spot trade listing")
        env.perp.responses["my_trades"] = lambda **kwargs: (
            _futures_fills(1) if kwargs.get("offset", 0) == 0 else []
        )

        with pytest.raises(FillReportsUnavailable) as excinfo:
            env.run(env.client.generate_fill_reports(_fill_reports_command()))

        assert [report.instrument_id for report in excinfo.value.reports] == [PERP_BTC_USDT]

    def test_an_unprovisioned_wallet_is_an_answer_of_none(self, perp_env):
        """USER_NOT_FOUND means the ledger does not exist, so it holds no trades.

        That is a definite answer and must not raise: an account that trades only
        spot has to be able to reconcile.
        """
        env = perp_env
        env.perp.responses["my_trades"] = WalletNotProvisionedError("no futures wallet yet")

        assert env.run(env.client.generate_fill_reports(_fill_reports_command())) == []

    def test_the_startup_mass_status_survives_a_failed_trade_listing(self, spot_and_perp_env):
        """One 5xx on one product's trade listing may not cost the order recovery."""
        env = spot_and_perp_env
        env.spot.responses["my_trades"] = RuntimeError("500 from the spot trade listing")
        env.perp.responses["list_orders"] = lambda **kwargs: (
            _open_futures_orders() if kwargs.get("status") == "open" else []
        )
        # `generate_mass_status` with no lookback uses the client's own default
        # window, so the trade has to be inside it rather than at the module's
        # fixed epoch.
        recent = int(env.clock.timestamp_ns() // 1_000_000_000) - 60
        env.perp.responses["my_trades"] = lambda **kwargs: (
            _futures_fills(1, create_time=recent) if kwargs.get("offset", 0) == 0 else []
        )

        mass_status = env.run(env.client.generate_mass_status())

        assert mass_status is not None
        assert [str(key) for key in mass_status.order_reports] == ["900001"]
        assert [
            report.trade_id.value
            for reports in mass_status.fill_reports.values()
            for report in reports
        ] == ["T-0"]


# -- contract report parsing --------------------------------------------------


class TestContractOrderReports:
    def test_signed_size_decodes_the_side(self, perp_env):
        env = perp_env
        report = env.client._parse_order_status_report(
            GateioProductType.PERP,
            {
                "id": 4242,
                "id_string": "4242",
                "contract": "BTC_USDT",
                "size": 40,
                "left": 10,
                "price": "59000.0",
                "tif": "gtc",
                "status": "open",
                "create_time": INSIDE_SECS,
            },
            env.instruments[1],
        )
        assert report is not None
        assert report.order_side == OrderSide.BUY
        assert report.quantity == Quantity.from_int(40)
        assert report.filled_qty == Quantity.from_int(30)
        assert report.order_status == OrderStatus.PARTIALLY_FILLED

    def test_zero_price_reports_a_market_order(self, perp_env):
        env = perp_env
        report = env.client._parse_order_status_report(
            GateioProductType.PERP,
            {
                "id": 4243,
                "id_string": "4243",
                "contract": "BTC_USDT",
                "size": -40,
                "left": 0,
                "price": "0",
                "tif": "ioc",
                "status": "finished",
                "finish_as": "filled",
                "create_time": INSIDE_SECS,
            },
            env.instruments[1],
        )
        assert report is not None
        assert report.price is None
        assert report.order_side == OrderSide.SELL
        assert report.order_status == OrderStatus.FILLED


# -- MANDATORY: resolving an order the venue never gave us an id for ----------


def _single_report_command(instrument_id: Any, order: Any) -> GenerateOrderStatusReport:
    """Build the command the engine builds for an order that is still in flight.

    ``LiveExecutionEngine._check_inflight_orders`` passes ``order.venue_order_id``,
    and a ``SUBMITTED`` order has none — the venue id is only assigned by
    ``OrderAccepted``. This is the exact shape of the query that has to be
    answered for an ambiguous submit to be resolved.
    """
    return GenerateOrderStatusReport(
        instrument_id=instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=None,
        command_id=UUID4(),
        ts_init=0,
    )


def _in_flight(env: ExecHarness, instrument_id: Any, quantity: Quantity) -> Any:
    """Leave a limit order SUBMITTED, as an unconfirmed submit leaves it."""
    order = env.order_factory.limit(
        instrument_id,
        OrderSide.BUY,
        quantity,
        Price.from_str("59000.0"),
    )
    env.add_order(order)
    env.client.generate_order_submitted(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        ts_event=env.clock.timestamp_ns(),
    )
    env.drain(order)
    assert order.status == OrderStatus.SUBMITTED
    assert order.venue_order_id is None
    return order


class TestClientOrderIdLookup:
    """A submit whose outcome Gate.io never confirmed must stay resolvable.

    ``_outcome_unresolved`` deliberately leaves such an order ``SUBMITTED`` so
    that the engine can settle it, and the engine settles it in exactly one way:
    ``_check_inflight_orders`` (installed live/execution_engine.py:701-765) issues
    ``QueryOrder`` with ``order.venue_order_id``, which is ``None``, and
    ``_resolve_inflight_order`` (:767-795) emits
    ``OrderRejected(reason="UNKNOWN")`` once ``inflight_check_retries`` queries
    have come back empty. That rejection is terminal on 1.230.0, so an order
    Gate.io is holding live would never be representable again.

    The client used to answer that query with ``None`` on every product except
    spot, which is to say it answered ``None`` for the whole derivatives half of
    the adapter.
    """

    def test_the_platform_query_path_answers_for_a_perpetual(self, perp_env):
        """Driven through the platform's own ``query_order`` plumbing, not ours."""
        from nautilus_trader.execution.messages import QueryOrder

        env = perp_env
        order = _in_flight(env, PERP_BTC_USDT, Quantity.from_int(10))
        env.perp.responses["get_order"] = {
            "id": 900001,
            "id_string": "900001",
            "contract": "BTC_USDT",
            "size": 10,
            "left": 10,
            "price": "59000.0",
            "tif": "gtc",
            "status": "open",
            "text": f"t-{order.client_order_id.value}",
            "create_time": INSIDE_SECS,
        }

        env.client.query_order(
            QueryOrder(
                trader_id=env.trader_id,
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=None,
                command_id=UUID4(),
                ts_init=0,
            ),
        )
        env.run(_drain_tasks(env))

        # The venue's statement has to reach the engine; without it the order is
        # rejected as unknown while it is live at Gate.io.
        assert [type(report).__name__ for report in env.reports] == ["OrderStatusReport"]
        assert env.reports[0].client_order_id == order.client_order_id
        assert env.reports[0].venue_order_id == VenueOrderId("900001")
        # Gate.io takes the client id in place of the venue id on this endpoint.
        assert [call.args[0] for call in env.perp.calls_named("get_order")] == [
            f"t-{order.client_order_id.value}",
        ]

    def test_delivery_is_found_in_the_resting_listing(self, delivery_env):
        """Delivery's single-order endpoint takes the venue id only, so it is listed."""
        env = delivery_env
        order = _in_flight(env, FUT_BTC_USDT, Quantity.from_int(3))
        env.delivery.responses["list_orders"] = lambda **kwargs: (
            [
                {
                    "id": 550055,
                    "id_string": "550055",
                    "contract": "BTC_USDT_20260807",
                    "size": 3,
                    "left": 3,
                    "price": "59000.0",
                    "tif": "gtc",
                    "status": "open",
                    "text": f"t-{order.client_order_id.value}",
                    "create_time": INSIDE_SECS,
                },
            ]
            if kwargs.get("status") == "open"
            else []
        )

        report = env.run(
            env.client.generate_order_status_report(
                _single_report_command(FUT_BTC_USDT, order),
            ),
        )

        assert report is not None
        assert report.client_order_id == order.client_order_id
        assert report.venue_order_id == VenueOrderId("550055")
        # No pointless request to an endpoint Gate.io documents as venue-id only.
        assert not env.delivery.called("get_order")

    def test_options_are_found_in_the_finished_listing(self, options_env):
        """An order that filled before the answer was lost is still resolvable."""
        env = options_env
        order = _in_flight(env, OPT_BTC_USDT, Quantity.from_int(2))
        env.options.responses["list_orders"] = lambda **kwargs: (
            [
                {
                    "id": 660066,
                    "id_string": "660066",
                    "contract": "BTC_USDT-20260729-70000-C",
                    "size": 2,
                    "left": 0,
                    "price": "120.0",
                    "tif": "gtc",
                    "status": "finished",
                    "finish_as": "filled",
                    "text": f"t-{order.client_order_id.value}",
                    "create_time": INSIDE_SECS,
                },
            ]
            if kwargs.get("status") == "finished"
            else []
        )

        report = env.run(
            env.client.generate_order_status_report(
                _single_report_command(OPT_BTC_USDT, order),
            ),
        )

        assert report is not None
        assert report.client_order_id == order.client_order_id
        assert report.venue_order_id == VenueOrderId("660066")
        assert report.order_status == OrderStatus.FILLED
        assert not env.options.called("get_order")

    def test_a_direct_miss_falls_through_to_the_listing(self, perp_env):
        """Gate.io stops resolving the custom id a minute after the order finishes."""
        env = perp_env
        order = _in_flight(env, PERP_BTC_USDT, Quantity.from_int(10))
        env.perp.responses["get_order"] = GateioError(404, "ORDER_NOT_FOUND", "not found")
        # A finished order is windowed, and the window this lookup uses is its
        # own: an order queried without a venue id has just been submitted.
        just_now = int(env.clock.timestamp_ns() // 1_000_000_000)
        env.perp.responses["list_orders"] = lambda **kwargs: (
            [
                {
                    "id": 900002,
                    "id_string": "900002",
                    "contract": "BTC_USDT",
                    "size": 10,
                    "left": 0,
                    "price": "59000.0",
                    "tif": "gtc",
                    "status": "finished",
                    "finish_as": "filled",
                    "text": f"t-{order.client_order_id.value}",
                    "create_time": just_now,
                },
            ]
            if kwargs.get("status") == "finished"
            else []
        )

        report = env.run(
            env.client.generate_order_status_report(
                _single_report_command(PERP_BTC_USDT, order),
            ),
        )

        assert report is not None
        assert report.venue_order_id == VenueOrderId("900002")
        assert report.order_status == OrderStatus.FILLED

    def test_a_foreign_answer_is_not_adopted(self, perp_env):
        """The identity is checked, not assumed, before the report is handed on.

        This report is about to become the venue's statement on an in-flight
        order; adopting the wrong venue order id for it would address every later
        cancel and amend to somebody else's order.
        """
        env = perp_env
        order = _in_flight(env, PERP_BTC_USDT, Quantity.from_int(10))
        env.perp.responses["get_order"] = {
            "id": 999999,
            "id_string": "999999",
            "contract": "BTC_USDT",
            "size": 10,
            "left": 10,
            "price": "59000.0",
            "tif": "gtc",
            "status": "open",
            "text": "t-SOMEBODY-ELSE",
            "create_time": INSIDE_SECS,
        }

        report = env.run(
            env.client.generate_order_status_report(
                _single_report_command(PERP_BTC_USDT, order),
            ),
        )

        assert report is None
        assert order.venue_order_id is None

    def test_an_order_the_venue_never_took_is_still_reported_missing(self, perp_env):
        """Nothing is invented for an order Gate.io does not hold."""
        env = perp_env
        order = _in_flight(env, PERP_BTC_USDT, Quantity.from_int(10))

        report = env.run(
            env.client.generate_order_status_report(
                _single_report_command(PERP_BTC_USDT, order),
            ),
        )

        assert report is None

    def test_neither_identifier_is_a_caller_error(self, perp_env):
        """The platform documents this as a ``ValueError``, not as "not found"."""
        env = perp_env
        with pytest.raises(ValueError, match="were `None`"):
            env.run(
                env.client.generate_order_status_report(
                    GenerateOrderStatusReport(
                        instrument_id=PERP_BTC_USDT,
                        client_order_id=None,
                        venue_order_id=None,
                        command_id=UUID4(),
                        ts_init=0,
                    ),
                ),
            )


# -- the report of an order a trigger of ours fired ---------------------------


def _fired_stop_limit(env: ExecHarness, *, apply_triggered: bool) -> Any:
    """Arm a perpetual STOP_LIMIT at ``777`` and fire it onto order ``900001``."""
    order = env.order_factory.stop_limit(
        PERP_BTC_USDT,
        OrderSide.SELL,
        Quantity.from_int(10),
        Price.from_str("59000.0"),
        Price.from_str("59500.0"),
    )
    env.accepted(order, "777")
    if apply_triggered:
        # Driven through the client's own rebase, which is the only sequence
        # `Order.apply` accepts: `OrderUpdated` carries the new venue order id,
        # `OrderTriggered` follows it.
        env.client._register_trigger_link(GateioProductType.PERP, "777", order.client_order_id)
        env.client._maybe_swap_trigger_venue_order_id(
            order,
            VenueOrderId("900001"),
            env.clock.timestamp_ns(),
        )
        env.drain(order)
        assert order.status == OrderStatus.TRIGGERED
    else:
        # The trigger fired while this client was down: the venue knows, the
        # local order does not.
        env.client._register_trigger_link(
            GateioProductType.PERP,
            "777",
            order.client_order_id,
            fired_id="900001",
        )
    return order


FIRED_ORDER_PAYLOAD: dict[str, Any] = {
    "id": 900001,
    "id_string": "900001",
    "contract": "BTC_USDT",
    "size": -10,
    "left": 10,
    "price": "59000.0",
    "tif": "gtc",
    "status": "open",
    "create_time": INSIDE_SECS,
}


class TestFiredConditionalOrderReports:
    """The order a trigger created is still the conditional order, and reads so.

    Gate.io keeps the trigger on the armed price order and none of it on the
    order that fires, so the venue's own statement about the fired object is
    "an accepted limit order with no trigger". Reported that way it collides with
    the local order twice on every reconciliation pass: the engine tries
    ``TRIGGERED -> ACCEPTED``, which the state table refuses, and ``_should_update``
    (live/execution_engine.py:3307-3318) sees ``None`` where the order has a
    trigger price and publishes an ``OrderUpdated`` for an amendment nobody made.
    """

    def test_a_resting_fired_order_reports_triggered(self, perp_env):
        env = perp_env
        order = _fired_stop_limit(env, apply_triggered=True)

        report = env.client._parse_order_status_report(
            GateioProductType.PERP,
            FIRED_ORDER_PAYLOAD,
            env.instruments[1],
        )

        assert report is not None
        assert report.order_status == OrderStatus.TRIGGERED
        # `_should_update` reads exactly these three for a STOP_LIMIT; all three
        # must match, or a phantom amendment is published on every pass.
        assert report.trigger_price == order.trigger_price
        assert report.price == order.price
        assert report.quantity == order.quantity
        # The trigger is already on the order, so restating it would only be
        # dropped by the FSM.
        assert report.ts_triggered == 0

    def test_accepted_would_be_refused_by_the_state_table(self, perp_env):
        """Why TRIGGERED and not ACCEPTED, proved against the installed FSM."""
        from nautilus_trader.core.fsm import InvalidStateTrigger
        from nautilus_trader.model.events import OrderAccepted

        env = perp_env
        order = _fired_stop_limit(env, apply_triggered=True)

        with pytest.raises(InvalidStateTrigger):
            order.apply(
                OrderAccepted(
                    trader_id=order.trader_id,
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=VenueOrderId("900001"),
                    account_id=env.client.account_id,
                    event_id=UUID4(),
                    ts_event=0,
                    ts_init=0,
                ),
            )

    def test_a_trigger_missed_while_down_is_carried_on_the_report(self, perp_env):
        """``ts_triggered`` is how the engine recovers an ``OrderTriggered`` it missed."""
        env = perp_env
        _fired_stop_limit(env, apply_triggered=False)

        report = env.client._parse_order_status_report(
            GateioProductType.PERP,
            dict(FIRED_ORDER_PAYLOAD, status="finished", finish_as="cancelled", left=10),
            env.instruments[1],
        )

        assert report is not None
        assert report.order_status == OrderStatus.CANCELED
        # The engine gates the recovered `OrderTriggered` on this being non-zero
        # (live/execution_engine.py:3281).
        assert report.ts_triggered > 0

    def test_an_unrelated_order_carries_no_trigger(self, perp_env):
        env = perp_env
        _fired_stop_limit(env, apply_triggered=True)

        report = env.client._parse_order_status_report(
            GateioProductType.PERP,
            dict(FIRED_ORDER_PAYLOAD, id=123456, id_string="123456"),
            env.instruments[1],
        )

        assert report is not None
        assert report.trigger_price is None
        assert report.order_status == OrderStatus.ACCEPTED
        assert report.ts_triggered == 0


# -- fixtures -----------------------------------------------------------------


@pytest.fixture()
def spot_env():
    env = ExecHarness()
    yield env
    env.close()


@pytest.fixture()
def perp_env():
    env = ExecHarness(products=(GateioProductType.PERP,))
    yield env
    env.close()


@pytest.fixture()
def spot_and_perp_env():
    """Two products, so a failure on one can be told from a failure on all."""
    env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
    yield env
    env.close()


@pytest.fixture()
def delivery_env():
    env = ExecHarness(products=(GateioProductType.FUT,))
    yield env
    env.close()


@pytest.fixture()
def options_env():
    env = ExecHarness(products=(GateioProductType.OPT,))
    yield env
    env.close()


async def _drain_tasks(env: ExecHarness) -> None:
    """Await the tasks the client's own fire-and-forget entry points create."""
    import asyncio

    pending = [task for task in asyncio.all_tasks(env.loop) if task is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def test_fill_reports_honour_the_venue_order_id_filter(perp_env):
    """Asked about one order, the client must not answer with every fill.

    ``GenerateFillReports.venue_order_id`` (installed
    execution/messages.pyx:338-382) narrows the answer to a single order, and a
    caller that groups the answer under that order's status report — which is
    what ``ExecutionMassStatus`` does, keying trades by venue order id — would
    otherwise attach executions Gate.io booked against other orders.
    """
    env = perp_env
    mine = _futures_fills(1, create_time=INSIDE_SECS + 1)[0]
    mine["id"] = "T-mine"
    mine["order_id"] = "900001"
    other = _futures_fills(1, create_time=INSIDE_SECS + 2)[0]
    other["id"] = "T-other"
    other["order_id"] = "900002"
    env.perp.responses["my_trades"] = lambda **kwargs: (
        [mine, other] if kwargs.get("offset", 0) == 0 else []
    )

    reports = env.run(
        env.client.generate_fill_reports(
            GenerateFillReports(
                instrument_id=None,
                venue_order_id=VenueOrderId("900001"),
                start=WINDOW_START,
                end=WINDOW_END,
                command_id=UUID4(),
                ts_init=0,
            ),
        ),
    )

    assert [report.trade_id.value for report in reports] == ["T-mine"]
    # Gate.io narrows this server-side, so the whole window is not walked either.
    assert [call.kwargs.get("order") for call in env.perp.calls_named("my_trades")] == ["900001"]


def test_fill_reports_are_sorted_for_replay(perp_env):
    """Reconciliation applies fills in list order, so ordering is load-bearing."""
    env = perp_env
    rows = [
        _futures_fills(1, create_time=INSIDE_SECS + 5)[0],
        _futures_fills(1, create_time=INSIDE_SECS + 1)[0],
    ]
    rows[0]["id"] = "T-late"
    rows[1]["id"] = "T-early"
    env.perp.responses["my_trades"] = lambda **kwargs: rows if kwargs.get("offset", 0) == 0 else []

    reports = env.run(env.client.generate_fill_reports(_fill_reports_command()))

    assert [report.trade_id.value for report in reports] == ["T-early", "T-late"]
    assert reports[0].commission.as_decimal() == Decimal("0.01")
