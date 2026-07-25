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
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
)
from nautilus_trader.model.enums import OrderSide, OrderStatus, PositionSide
from nautilus_trader.model.objects import Quantity

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.execution import REPORT_PAGE_LIMIT

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        PERP_BTC_USDT,
        PERP_CONTRACT_PAYLOAD,
        SPOT_BTC_USDT,
        SPOT_PAIR_PAYLOAD,
        ExecHarness,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
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


# -- MANDATORY-ADJACENT: EXEC-4, the spot base-fee netting must agree ---------


class TestSpotBaseFeeNettingInReports:
    """Regression for EXEC-4.

    ``_fill_quantity_and_commission`` nets a base-currency fee off every fill, so
    a fully filled spot BUY produces fills summing to ``amount - fee``. If the
    order status report keeps stating the raw ``filled_amount`` the order can
    never reconcile as closed, and NautilusTrader synthesises a phantom fill for
    the difference.
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

        fill_qty, _ = env.client._fill_quantity_and_commission(
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
        assert report.filled_qty == Quantity.from_str("0.009990")

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

    def test_partial_fill_report_nets_the_fee_but_keeps_the_quantity(self, spot_env):
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
        assert report.filled_qty == Quantity.from_str("0.005994")
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
