"""Order translation tests for ``GateioExecutionClient``.

This module also carries the shared execution-test harness (:class:`ExecHarness`
and the stub REST namespaces) used by the other ``test_execution_*`` modules.

Everything is offline: the REST namespaces are replaced by recording stubs, the
private WebSocket is never connected, and no credentials are read. The
NautilusTrader side is real throughout — a real ``Cache``, ``MessageBus``,
``OrderFactory`` and real ``Order`` objects — so the finite state machine that
rejects malformed event sequences is genuinely exercised.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.enums import (
    OrderSide,
    OrderType,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    StrategyId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity

from nautilus_gateio.common.constants import GATEIO_CLIENT_ID
from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode
from nautilus_gateio.config import GateioExecClientConfig
from nautilus_gateio.execution import GateioExecutionClient
from nautilus_gateio.instruments import (
    parse_delivery_instrument,
    parse_option_instrument,
    parse_perpetual_instrument,
    parse_spot_instrument,
)

# -- instrument fixtures (payload shapes copied from the live venue) ----------

SPOT_PAIR_PAYLOAD: dict[str, Any] = {
    "id": "BTC_USDT",
    "base": "BTC",
    "quote": "USDT",
    "fee": "0.2",
    "min_base_amount": "0.0001",
    "min_quote_amount": "3",
    "amount_precision": 6,
    "precision": 2,
    "trade_status": "tradable",
    "slippage": "0.05",
}

PERP_CONTRACT_PAYLOAD: dict[str, Any] = {
    "name": "BTC_USDT",
    "type": "direct",
    "quanto_multiplier": "0.0001",
    "order_price_round": "0.1",
    "mark_price_round": "0.1",
    "order_size_min": 1,
    "order_size_max": 1000000,
    "maintenance_rate": "0.004",
    "maker_fee_rate": "0.0002",
    "taker_fee_rate": "0.00075",
    "create_time": 1600000000,
}

DELIVERY_CONTRACT_PAYLOAD: dict[str, Any] = {
    "name": "BTC_USDT_20260807",
    "type": "direct",
    "quanto_multiplier": "0.0001",
    "order_price_round": "0.1",
    "order_size_min": 1,
    "order_size_max": 1000000,
    "maintenance_rate": "0.004",
    "maker_fee_rate": "0.0002",
    "taker_fee_rate": "0.00075",
    "create_time": 1600000000,
    "expire_time": 1786000000,
}

OPTION_CONTRACT_PAYLOAD: dict[str, Any] = {
    "name": "BTC_USDT-20260729-70000-C",
    "is_call": True,
    "strike_price": "70000",
    "multiplier": "0.01",
    "order_price_round": "1",
    "order_size_min": 1,
    "order_size_max": 100,
    "maker_fee_rate": "0.0003",
    "taker_fee_rate": "0.0003",
    "create_time": 1600000000,
    "expiration_time": 1785000000,
}

SPOT_BTC_USDT = InstrumentId.from_str("BTC_USDT.GATE_IO")
PERP_BTC_USDT = InstrumentId.from_str("BTC_USDT-PERP.GATE_IO")
FUT_BTC_USDT = InstrumentId.from_str("BTC_USDT_20260807.GATE_IO")
OPT_BTC_USDT = InstrumentId.from_str("BTC_USDT-20260729-70000-C.GATE_IO")


def build_instruments() -> list[Instrument]:
    """Build one instrument per product through the adapter's own parsers."""
    instruments = [
        parse_spot_instrument(SPOT_PAIR_PAYLOAD),
        parse_perpetual_instrument(PERP_CONTRACT_PAYLOAD, GateioProductType.PERP),
        parse_delivery_instrument(DELIVERY_CONTRACT_PAYLOAD),
        parse_option_instrument(OPTION_CONTRACT_PAYLOAD),
    ]
    assert all(instrument is not None for instrument in instruments)
    return instruments  # type: ignore[return-value]


# -- stub REST layer ----------------------------------------------------------


class StubCall:
    """One recorded call to a stubbed REST namespace."""

    __slots__ = ("args", "kwargs", "name")

    def __init__(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.name = name
        self.args = args
        self.kwargs = kwargs

    @property
    def body(self) -> Any:
        """Return the request body a POST-style call was given."""
        return self.kwargs.get("body", self.args[0] if self.args else None)

    def __repr__(self) -> str:
        return f"StubCall({self.name!r}, args={self.args!r}, kwargs={self.kwargs!r})"


class StubApi:
    """A recording stand-in for one typed REST namespace.

    Every attribute resolves to an async method that records its arguments and
    answers from ``responses``. A response may be a value, an exception to raise,
    or a callable evaluated with the call's arguments (used to model pagination).
    Unconfigured methods answer with an empty list, which is what every listing
    endpoint returns when it has nothing to report.
    """

    def __init__(self, **responses: Any) -> None:
        self.calls: list[StubCall] = []
        self.responses: dict[str, Any] = dict(responses)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(StubCall(name, args, kwargs))
            value = self.responses.get(name, [])
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                return value(*args, **kwargs)
            return value

        return _call

    def calls_named(self, name: str) -> list[StubCall]:
        return [call for call in self.calls if call.name == name]

    def called(self, name: str) -> bool:
        return any(call.name == name for call in self.calls)


class StubInstrumentProvider(InstrumentProvider):
    """Instrument provider that can be asked to load a definition on demand.

    ``loadable`` holds instruments the venue would serve but which were not part
    of the initial load, which is what reconciliation hits when the provider was
    started with a narrower filter than the account actually traded.
    """

    def __init__(self, instruments: list[Instrument]) -> None:
        super().__init__()
        self.loadable: dict[InstrumentId, Instrument] = {}
        self.load_requests: list[InstrumentId] = []
        for instrument in instruments:
            self.add(instrument)

    async def load_async(self, instrument_id: InstrumentId, filters: dict | None = None) -> None:
        self.load_requests.append(instrument_id)
        instrument = self.loadable.pop(instrument_id, None)
        if instrument is not None:
            self.add(instrument)


# -- harness ------------------------------------------------------------------


class ExecHarness:
    """A ``GateioExecutionClient`` wired to real Nautilus components and stub REST."""

    def __init__(
        self,
        products: tuple[GateioProductType, ...] = (GateioProductType.SPOT,),
        spot_account_mode: GateioSpotAccountMode = GateioSpotAccountMode.SPOT,
        instruments: list[Instrument] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.clock = LiveClock()
        self.trader_id = TraderId("TESTER-000")
        self.strategy_id = StrategyId("S-001")
        self.msgbus = MessageBus(trader_id=self.trader_id, clock=self.clock)
        self.cache = Cache()
        self.loop = loop or asyncio.new_event_loop()

        self.instruments = instruments if instruments is not None else build_instruments()
        for instrument in self.instruments:
            self.cache.add_instrument(instrument)
        self.provider = StubInstrumentProvider(self.instruments)

        self.events: list[Any] = []
        self.reports: list[Any] = []
        self.account_states: list[Any] = []
        self.msgbus.register(endpoint="ExecEngine.process", handler=self.events.append)
        self.msgbus.register(
            endpoint="ExecEngine.reconcile_execution_report",
            handler=self.reports.append,
        )
        self.msgbus.register(
            endpoint="Portfolio.update_account",
            handler=self.account_states.append,
        )

        self.client = GateioExecutionClient(
            loop=self.loop,
            client_id=ClientId(GATEIO_CLIENT_ID.value),
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            instrument_provider=self.provider,
            http_client=StubApi(),
            config=GateioExecClientConfig(
                api_key="test-key",
                api_secret="test-secret",
                products=products,
                spot_account_mode=spot_account_mode,
                options_underlyings=("BTC_USDT",),
            ),
        )

        self.spot = StubApi()
        self.margin = StubApi()
        self.options = StubApi()
        self.wallet = StubApi()
        self.perp = StubApi()
        self.inverse = StubApi()
        self.delivery = StubApi()

        self.client._spot_http = self.spot
        self.client._margin_http = self.margin
        self.client._options_http = self.options
        self.client._wallet_http = self.wallet
        self.client._futures_http = {
            GateioProductType.PERP: self.perp,
            GateioProductType.INVERSE: self.inverse,
            GateioProductType.FUT: self.delivery,
        }

        self.order_factory = OrderFactory(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            clock=self.clock,
        )
        self._event_cursor = 0

    # -- driving ------------------------------------------------------------

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def close(self) -> None:
        self.loop.close()

    # -- events -------------------------------------------------------------

    def drain(self, order: Any = None) -> list[Any]:
        """Return the events generated since the last drain, applying them to ``order``.

        Applying through the real ``Order.apply`` is the point: an event sequence
        the venue could not produce, or one this client emits in the wrong order,
        raises out of the FSM instead of being silently accepted.
        """
        events = self.events[self._event_cursor :]
        self._event_cursor = len(self.events)
        if order is not None:
            for event in events:
                if event.client_order_id == order.client_order_id:
                    order.apply(event)
            self.cache.update_order(order)
        return events

    def events_of(self, event_type: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, event_type)]

    # -- orders -------------------------------------------------------------

    def add_order(self, order: Any) -> Any:
        self.cache.add_order(order)
        return order

    def accepted(self, order: Any, venue_order_id: str) -> Any:
        """Push an order through SUBMITTED -> ACCEPTED with the real FSM."""
        self.add_order(order)
        self.client.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self.clock.timestamp_ns(),
        )
        self.client.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId(venue_order_id),
            ts_event=self.clock.timestamp_ns(),
        )
        self.drain(order)
        self.cache.add_venue_order_id(
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId(venue_order_id),
            overwrite=True,
        )
        return order


@pytest.fixture()
def harness():
    env = ExecHarness()
    yield env
    env.close()


@pytest.fixture()
def multi_harness():
    env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
    yield env
    env.close()


def _submit(env: ExecHarness, order: Any) -> None:
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import SubmitOrder

    env.add_order(order)
    env.run(
        env.client._submit_order(
            SubmitOrder(
                trader_id=env.trader_id,
                strategy_id=order.strategy_id,
                order=order,
                command_id=UUID4(),
                ts_init=env.clock.timestamp_ns(),
            ),
        ),
    )


def _rejections(env: ExecHarness) -> list[Any]:
    from nautilus_trader.model.events import OrderRejected

    return env.events_of(OrderRejected)


# -- MANDATORY TEST 7: fill-or-kill across every supported product ------------


class TestFillOrKillPerProduct:
    """FOK must be honoured where Gate.io supports it and rejected where it does not.

    Regression for EXEC-5: FOK used to be silently downgraded to IOC on futures,
    delivery and options market orders, which turns "fill completely or not at
    all" into "fill whatever is available", a different execution guarantee.
    """

    def test_spot_limit_sends_fok(self, harness):
        order = harness.order_factory.limit(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("0.010000"),
            Price.from_str("60000.00"),
            time_in_force=TimeInForce.FOK,
        )
        body = harness.run(
            harness.client._build_spot_order(order, harness.instruments[0], "BTC_USDT"),
        )
        assert body["time_in_force"] == "fok"
        assert body == {
            "currency_pair": "BTC_USDT",
            "side": "buy",
            "account": "spot",
            "text": f"t-{order.client_order_id.value}",
            "type": "limit",
            "amount": "0.010000",
            "price": "60000.00",
            "time_in_force": "fok",
        }

    def test_spot_market_sell_sends_fok(self, harness):
        order = harness.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            time_in_force=TimeInForce.FOK,
        )
        body = harness.run(
            harness.client._build_spot_order(order, harness.instruments[0], "BTC_USDT"),
        )
        assert body["type"] == "market"
        assert body["time_in_force"] == "fok"

    def test_spot_market_quote_buy_sends_fok(self, harness):
        order = harness.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("500.000000"),
            time_in_force=TimeInForce.FOK,
            quote_quantity=True,
        )
        body = harness.run(
            harness.client._build_spot_order(order, harness.instruments[0], "BTC_USDT"),
        )
        assert body["type"] == "market"
        assert body["amount"] == "500.000000"
        assert body["time_in_force"] == "fok"

    def test_futures_market_sends_fok(self, harness):
        order = harness.order_factory.market(
            PERP_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_int(500),
            time_in_force=TimeInForce.FOK,
        )
        body = harness.client._build_futures_order(order, "BTC_USDT")
        assert body == {
            "contract": "BTC_USDT",
            "size": -500,
            "text": f"t-{order.client_order_id.value}",
            "price": "0",
            "tif": "fok",
        }

    def test_delivery_market_sends_fok(self, harness):
        order = harness.order_factory.market(
            FUT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_int(3),
            time_in_force=TimeInForce.FOK,
        )
        body = harness.client._build_futures_order(order, "BTC_USDT_20260807")
        assert body["tif"] == "fok"
        assert body["size"] == 3

    def test_options_market_rejects_fok(self, harness):
        env = ExecHarness(products=(GateioProductType.OPT,))
        try:
            order = env.order_factory.market(
                OPT_BTC_USDT,
                OrderSide.BUY,
                Quantity.from_int(1),
                time_in_force=TimeInForce.FOK,
            )
            _submit(env, order)

            rejected = _rejections(env)
            assert len(rejected) == 1
            assert "fill-or-kill" in rejected[0].reason
            assert not env.options.called("create_order")
        finally:
            env.close()

    def test_options_limit_rejects_fok(self, harness):
        env = ExecHarness(products=(GateioProductType.OPT,))
        try:
            order = env.order_factory.limit(
                OPT_BTC_USDT,
                OrderSide.BUY,
                Quantity.from_int(1),
                Price.from_str("25"),
                time_in_force=TimeInForce.FOK,
            )
            _submit(env, order)

            rejected = _rejections(env)
            assert len(rejected) == 1
            assert "fill-or-kill" in rejected[0].reason
        finally:
            env.close()


# -- MANDATORY TEST 6: spot market-buy quote amount is never a base quantity ---


class TestSpotMarketBuyQuoteSemantics:
    """Regression for EXEC-3/DP-3: ``amount`` on a spot market buy is quote cash."""

    PAYLOAD: dict[str, Any] = {
        "id": "778899",
        "currency_pair": "BTC_USDT",
        "type": "market",
        "side": "buy",
        "account": "spot",
        # 500 USDT of cash, NOT 500 BTC.
        "amount": "500",
        "left": "200",
        "filled_amount": "0.005",
        "filled_total": "300",
        "avg_deal_price": "60000",
        "status": "open",
        "text": "t-O-19700101-000000-001-001-1",
    }

    def test_payload_quantity_uses_filled_base_not_quote_amount(self, harness):
        quantity = harness.client._payload_quantity(
            SPOT_BTC_USDT,
            self.PAYLOAD,
            harness.instruments[0],
        )
        assert quantity == Quantity.from_str("0.005000")
        assert quantity != Quantity.from_str("500.000000")

    def test_unfilled_market_buy_reports_no_quantity(self, harness):
        payload = dict(self.PAYLOAD, filled_amount="0")
        assert (
            harness.client._payload_quantity(SPOT_BTC_USDT, payload, harness.instruments[0]) is None
        )

    def test_order_status_report_never_states_the_quote_amount(self, harness):
        report = harness.client._parse_order_status_report(
            GateioProductType.SPOT,
            self.PAYLOAD,
            harness.instruments[0],
        )
        assert report is not None
        assert report.quantity == Quantity.from_str("0.005000")
        assert report.filled_qty == Quantity.from_str("0.005000")
        assert report.quantity.as_decimal() < Decimal("1")

    def test_no_order_updated_for_a_market_order(self, harness):
        """A market order's payload quantity must never restate the order."""
        from nautilus_trader.model.events import OrderUpdated

        order = harness.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("500.000000"),
            quote_quantity=True,
            client_order_id=ClientOrderId("O-19700101-000000-001-001-1"),
        )
        harness.accepted(order, "778899")

        harness.client._handle_order_payload(GateioProductType.SPOT, self.PAYLOAD)
        harness.drain(order)

        assert harness.events_of(OrderUpdated) == []


# -- EXEC-7 / DP-5: conditional builders must reject what they cannot express --


class TestConditionalOrderRejections:
    def test_spot_price_order_rejects_post_only(self, harness):
        order = harness.order_factory.stop_limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
            post_only=True,
        )
        _submit(harness, order)

        rejected = _rejections(harness)
        assert len(rejected) == 1
        assert "post-only" in rejected[0].reason
        assert not harness.spot.called("create_price_order")

    def test_spot_price_order_rejects_display_qty(self, harness):
        order = harness.order_factory.stop_limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
            display_qty=Quantity.from_str("0.001000"),
        )
        _submit(harness, order)

        rejected = _rejections(harness)
        assert len(rejected) == 1
        assert "display_qty" in rejected[0].reason
        assert not harness.spot.called("create_price_order")

    def test_spot_price_order_rejects_reduce_only(self, harness):
        order = harness.order_factory.stop_market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            reduce_only=True,
        )
        _submit(harness, order)

        rejected = _rejections(harness)
        assert len(rejected) == 1
        assert "reduce-only" in rejected[0].reason
        assert not harness.spot.called("create_price_order")

    def test_spot_price_order_rejects_fok(self, harness):
        order = harness.order_factory.stop_limit(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
            time_in_force=TimeInForce.FOK,
        )
        _submit(harness, order)

        rejected = _rejections(harness)
        assert len(rejected) == 1
        assert "FOK" in rejected[0].reason
        assert not harness.spot.called("create_price_order")

    def test_futures_price_order_rejects_post_only(self, harness):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.order_factory.stop_limit(
                PERP_BTC_USDT,
                OrderSide.BUY,
                Quantity.from_int(10),
                Price.from_str("61000.0"),
                Price.from_str("60500.0"),
                post_only=True,
            )
            _submit(env, order)

            rejected = _rejections(env)
            assert len(rejected) == 1
            assert "post-only" in rejected[0].reason
            assert not env.perp.called("create_price_order")
        finally:
            env.close()

    def test_futures_price_order_rejects_display_qty(self, harness):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.order_factory.stop_limit(
                PERP_BTC_USDT,
                OrderSide.BUY,
                Quantity.from_int(10),
                Price.from_str("61000.0"),
                Price.from_str("60500.0"),
                display_qty=Quantity.from_int(2),
            )
            _submit(env, order)

            rejected = _rejections(env)
            assert len(rejected) == 1
            assert "display_qty" in rejected[0].reason
        finally:
            env.close()

    def test_futures_price_order_keeps_reduce_only(self, harness):
        """Reduce-only IS expressible on a futures conditional order."""
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.perp.responses["create_price_order"] = {"id": 4242, "id_string": "4242"}
            order = env.order_factory.stop_market(
                PERP_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(10),
                Price.from_str("59000.0"),
                reduce_only=True,
            )
            _submit(env, order)

            assert _rejections(env) == []
            body = env.perp.calls_named("create_price_order")[0].body
            assert body["initial"]["reduce_only"] is True
            assert body["initial"]["size"] == -10
            assert body["initial"]["price"] == "0"
            assert body["initial"]["tif"] == "ioc"
            assert body["trigger"]["price"] == "59000.0"
            assert body["trigger"]["price_type"] == 0
        finally:
            env.close()

    def test_spot_price_order_body_is_exact(self, harness):
        harness.spot.responses["create_price_order"] = {"id": 99, "id_string": "99"}
        harness.spot.responses["tickers"] = [{"last": "60000"}]
        order = harness.order_factory.limit_if_touched(
            SPOT_BTC_USDT,
            OrderSide.BUY,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00"),
            Price.from_str("59500.00"),
        )
        _submit(harness, order)

        assert _rejections(harness) == []
        body = harness.spot.calls_named("create_price_order")[0].body
        assert body == {
            "market": "BTC_USDT",
            "put": {
                "type": "limit",
                "side": "buy",
                "amount": "0.010000",
                "account": "normal",
                "time_in_force": "gtc",
                "price": "59000.00",
            },
            "trigger": {"price": "59500.00", "rule": "<="},
        }


# -- DP-6: futures amend must not truncate or guess the side ------------------


class TestFuturesAmend:
    def _modify(self, env: ExecHarness, order: Any, quantity: Quantity) -> None:
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.messages import ModifyOrder

        env.run(
            env.client._modify_order(
                ModifyOrder(
                    trader_id=env.trader_id,
                    strategy_id=env.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=VenueOrderId("5001"),
                    quantity=quantity,
                    price=None,
                    trigger_price=None,
                    command_id=UUID4(),
                    ts_init=env.clock.timestamp_ns(),
                ),
            ),
        )

    def test_fractional_contract_quantity_is_rejected_not_truncated(self):
        from nautilus_trader.model.events import OrderModifyRejected

        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.order_factory.limit(
                PERP_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(10),
                Price.from_str("61000.0"),
            )
            env.accepted(order, "5001")

            self._modify(env, order, Quantity.from_str("2.5"))

            rejected = env.events_of(OrderModifyRejected)
            assert len(rejected) == 1
            assert "whole contracts" in rejected[0].reason
            assert not env.perp.called("amend_order")
        finally:
            env.close()

    def test_unknown_order_is_rejected_instead_of_assuming_buy(self):
        from nautilus_trader.model.events import OrderModifyRejected

        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.order_factory.limit(
                PERP_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(10),
                Price.from_str("61000.0"),
            )
            # Deliberately NOT added to the cache: the side cannot be resolved.
            self._modify(env, order, Quantity.from_int(4))

            rejected = env.events_of(OrderModifyRejected)
            assert len(rejected) == 1
            assert "not in the cache" in rejected[0].reason
            assert not env.perp.called("amend_order")
        finally:
            env.close()

    def test_sell_amend_keeps_the_negative_sign(self):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.order_factory.limit(
                PERP_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(10),
                Price.from_str("61000.0"),
            )
            env.accepted(order, "5001")

            self._modify(env, order, Quantity.from_int(4))

            body = env.perp.calls_named("amend_order")[0].kwargs.get("body")
            if body is None:
                body = env.perp.calls_named("amend_order")[0].args[1]
            assert body["size"] == -4
        finally:
            env.close()


# -- DP-1: a side-scoped cancel-all must not disarm the other side ------------


class TestCancelAllSideScoping:
    def _cancel_all(self, env: ExecHarness, instrument_id: InstrumentId, side: OrderSide) -> None:
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.messages import CancelAllOrders

        env.run(
            env.client._cancel_all_orders(
                CancelAllOrders(
                    trader_id=env.trader_id,
                    strategy_id=env.strategy_id,
                    instrument_id=instrument_id,
                    client_id=ClientId(GATEIO_CLIENT_ID.value),
                    order_side=side,
                    command_id=UUID4(),
                    ts_init=env.clock.timestamp_ns(),
                ),
            ),
        )

    def _arm(self, env: ExecHarness, side: OrderSide, armed_id: str) -> Any:
        order = env.order_factory.stop_market(
            SPOT_BTC_USDT,
            side,
            Quantity.from_str("0.010000"),
            Price.from_str("59000.00") if side == OrderSide.SELL else Price.from_str("61000.00"),
        )
        env.accepted(order, armed_id)
        env.client._register_trigger_link(
            GateioProductType.SPOT,
            armed_id,
            order.client_order_id,
        )
        return order

    def test_side_scoped_cancel_does_not_bulk_disarm(self, harness):
        buy = self._arm(harness, OrderSide.BUY, "A-BUY")
        sell = self._arm(harness, OrderSide.SELL, "A-SELL")

        self._cancel_all(harness, SPOT_BTC_USDT, OrderSide.BUY)

        # The un-scoped bulk endpoint would have disarmed both sides.
        assert not harness.spot.called("cancel_price_orders")
        disarmed = [call.args[0] for call in harness.spot.calls_named("cancel_price_order")]
        assert disarmed == ["A-BUY"]
        assert harness.client.trigger_links.get(buy.client_order_id) is None
        assert harness.client.trigger_links[sell.client_order_id].armed_id == "A-SELL"

    def test_side_scoped_cancel_reports_the_disarmed_order(self, harness):
        from nautilus_trader.model.events import OrderCanceled

        buy = self._arm(harness, OrderSide.BUY, "A-BUY")
        self._arm(harness, OrderSide.SELL, "A-SELL")

        self._cancel_all(harness, SPOT_BTC_USDT, OrderSide.BUY)

        canceled = harness.events_of(OrderCanceled)
        assert [event.client_order_id for event in canceled] == [buy.client_order_id]
        assert canceled[0].venue_order_id == VenueOrderId("A-BUY")

    def test_unscoped_cancel_still_bulk_disarms(self, harness):
        harness.spot.responses["cancel_price_orders"] = [{"id": "A-BUY"}, {"id": "A-SELL"}]
        buy = self._arm(harness, OrderSide.BUY, "A-BUY")
        sell = self._arm(harness, OrderSide.SELL, "A-SELL")

        self._cancel_all(harness, SPOT_BTC_USDT, OrderSide.NO_ORDER_SIDE)

        assert harness.spot.called("cancel_price_orders")
        assert harness.client.trigger_links.get(buy.client_order_id) is None
        assert harness.client.trigger_links.get(sell.client_order_id) is None

    def test_futures_side_scoped_cancel_does_not_bulk_disarm(self):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.order_factory.stop_market(
                PERP_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(10),
                Price.from_str("59000.0"),
                trigger_type=TriggerType.MARK_PRICE,
            )
            env.accepted(order, "F-SELL")
            env.client._register_trigger_link(
                GateioProductType.PERP,
                "F-SELL",
                order.client_order_id,
            )

            self._cancel_all(env, PERP_BTC_USDT, OrderSide.BUY)

            assert not env.perp.called("cancel_price_orders")
            assert not env.perp.called("cancel_price_order")
            assert env.client.trigger_links[order.client_order_id].armed_id == "F-SELL"
        finally:
            env.close()


# -- order type routing -------------------------------------------------------


class TestOrderRouting:
    def test_unsupported_order_type_is_denied(self, harness):
        from nautilus_trader.model.events import OrderDenied

        order = harness.order_factory.trailing_stop_market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
            trailing_offset=Decimal("100"),
        )
        _submit(harness, order)

        denied = harness.events_of(OrderDenied)
        assert len(denied) == 1
        assert "TRAILING_STOP_MARKET" in denied[0].reason

    def test_options_have_no_conditional_endpoint(self):
        env = ExecHarness(products=(GateioProductType.OPT,))
        try:
            order = env.order_factory.stop_market(
                OPT_BTC_USDT,
                OrderSide.SELL,
                Quantity.from_int(1),
                Price.from_str("20"),
            )
            _submit(env, order)

            rejected = _rejections(env)
            assert len(rejected) == 1
            assert "price-triggered" in rejected[0].reason
        finally:
            env.close()

    def test_contract_quantity_must_be_whole(self, harness):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            order = env.order_factory.limit(
                PERP_BTC_USDT,
                OrderSide.BUY,
                Quantity.from_str("2.5"),
                Price.from_str("60000.0"),
            )
            _submit(env, order)

            rejected = _rejections(env)
            assert len(rejected) == 1
            assert "whole contracts" in rejected[0].reason
        finally:
            env.close()

    def test_account_id_uses_the_venue_constant(self, harness):
        assert harness.client.account_id == AccountId("GATE_IO-master")
        assert harness.client.venue.value == "GATE_IO"

    def test_conditional_types_route_to_the_price_order_endpoint(self, harness):
        from nautilus_gateio.execution import CONDITIONAL_ORDER_TYPES, SUPPORTED_ORDER_TYPES

        assert CONDITIONAL_ORDER_TYPES <= SUPPORTED_ORDER_TYPES
        assert OrderType.LIMIT_IF_TOUCHED in CONDITIONAL_ORDER_TYPES
        assert OrderType.MARKET not in CONDITIONAL_ORDER_TYPES
