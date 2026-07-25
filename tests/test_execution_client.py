"""Tests for ``GateioExecutionClient`` internals with a stubbed HTTP client.

Strategy: construct the client with real Nautilus components (msgbus, cache,
clock) and a stub HTTP object, then drive the private coroutines directly on
the client's own event loop. Generated Nautilus events are captured by
monkeypatching the ``generate_*`` methods with recorders, which avoids needing
the full execution-engine event plumbing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    StrategyId,
    TraderId,
    VenueOrderId,
)

from nautilus_gateio.config import GateioExecClientConfig
from nautilus_gateio.execution import GateioExecutionClient, _PendingOrder

BTC_USDT = InstrumentId.from_str("BTC_USDT.GATEIO")
STRATEGY = StrategyId("S-001")


class StubHttp:
    """Implements the subset of ``GateioHttpClient`` used by the execution client."""

    def __init__(self) -> None:
        self.balances_data: dict[str, dict[str, float]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.last_price = 100.0
        self.spec: dict[str, Any] = {
            "pair": "BTC_USDT",
            "price_precision": 2,
            "amount_precision": 6,
        }
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.place_error: Exception | None = None
        self.next_order_id = "777"
        self.open_orders_data: list[dict[str, Any]] = []
        self.closed = False

    def balances(self) -> dict[str, dict[str, float]]:
        return {code: dict(entry) for code, entry in self.balances_data.items()}

    def order(self, order_id: str, pair: str) -> dict[str, Any]:
        return dict(self.orders[order_id])

    def ticker_last(self, pair: str) -> float:
        return self.last_price

    def currency_pair(self, pair: str) -> dict[str, Any]:
        return dict(self.spec)

    def place_order(
        self,
        pair: str,
        side: str,
        amount: str,
        price: str,
        order_type: str,
        client_id: str,
        time_in_force: str,
    ) -> dict[str, Any]:
        if self.place_error is not None:
            raise self.place_error
        self.placed.append(
            {
                "pair": pair,
                "side": side,
                "amount": amount,
                "price": price,
                "type": order_type,
                "text": client_id,
                "tif": time_in_force,
            }
        )
        return {"id": self.next_order_id}

    def cancel_order(self, order_id: str, pair: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        return {"id": order_id}

    def open_orders(self, pair: str | None = None) -> list[dict[str, Any]]:
        return list(self.open_orders_data)

    def close(self) -> None:
        self.closed = True


class Recorder:
    """Records positional/keyword arguments of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))

    def __len__(self) -> int:
        return len(self.calls)


@pytest.fixture()
def client_env():
    stub = StubHttp()
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = Cache()
    loop = asyncio.new_event_loop()
    client = GateioExecutionClient(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=GateioExecClientConfig(api_key="k", api_secret="s"),
        http_client=stub,
    )
    yield client, stub, loop
    loop.close()


def _seed_pending(client: GateioExecutionClient, order_id: str = "42") -> _PendingOrder:
    pending = _PendingOrder(
        BTC_USDT,
        STRATEGY,
        ClientOrderId("O-123"),
        "BTC_USDT",
        VenueOrderId(order_id),
    )
    client._pending[order_id] = pending
    return pending


async def _noop() -> None:
    return None


class TestExchangeText:
    def test_maps_client_order_id(self, client_env):
        client, _, _ = client_env
        assert client._exchange_text(ClientOrderId("O-123")) == "t-O-123"

    def test_long_id_sanitized_to_28_chars(self, client_env):
        client, _, _ = client_env
        text = client._exchange_text(ClientOrderId("O-" + "X" * 60))
        assert text.startswith("t-")
        assert len(text) <= 28

    def test_invalid_id_falls_back_to_generated(self, client_env):
        client, _, _ = client_env
        text = client._exchange_text(ClientOrderId("@@@"))
        assert text.startswith("t-ng-")
        assert len(text) <= 28


class TestFillDelta:
    def _wire(self, client) -> tuple[Recorder, Recorder]:
        fills = Recorder()
        cancels = Recorder()
        client.generate_order_filled = fills
        client.generate_order_canceled = cancels
        client._push_account_state = _noop
        return fills, cancels

    def test_partial_then_final_fill_deltas(self, client_env):
        client, stub, loop = client_env
        fills, _ = self._wire(client)
        pending = _seed_pending(client)
        stub.orders["42"] = {
            "amount": "1.0",
            "left": "0.6",
            "status": "open",
            "side": "buy",
            "avg_price": "100.0",
        }

        loop.run_until_complete(client._check_fills("42"))

        assert len(fills) == 1
        qty = fills.calls[0][0][8]
        assert abs(float(qty) - 0.4) < 1e-9
        assert pending.reported_fill == pytest.approx(0.4)
        assert "42" in client._pending  # still open

        stub.orders["42"] = {
            "amount": "1.0",
            "left": "0.0",
            "status": "closed",
            "side": "buy",
            "avg_price": "100.0",
        }
        loop.run_until_complete(client._check_fills("42"))

        assert len(fills) == 2
        qty = fills.calls[1][0][8]
        assert abs(float(qty) - 0.6) < 1e-9
        assert "42" not in client._pending  # popped on terminal status

    def test_duplicate_poll_emits_no_extra_fill(self, client_env):
        client, stub, loop = client_env
        fills, _ = self._wire(client)
        _seed_pending(client)
        stub.orders["42"] = {
            "amount": "1.0",
            "left": "0.6",
            "status": "open",
            "side": "buy",
            "avg_price": "100.0",
        }

        loop.run_until_complete(client._check_fills("42"))
        loop.run_until_complete(client._check_fills("42"))  # same exchange state

        assert len(fills) == 1

    def test_fill_price_and_side_propagated(self, client_env):
        client, stub, loop = client_env
        fills, _ = self._wire(client)
        _seed_pending(client)
        stub.orders["42"] = {
            "amount": "2.0",
            "left": "0.0",
            "status": "closed",
            "side": "sell",
            "avg_price": "123.45",
        }

        loop.run_until_complete(client._check_fills("42"))

        args = fills.calls[0][0]
        assert args[6] == OrderSide.SELL
        assert abs(float(args[9]) - 123.45) < 1e-9


class TestCancelDetection:
    def _wire(self, client) -> tuple[Recorder, Recorder]:
        fills = Recorder()
        cancels = Recorder()
        client.generate_order_filled = fills
        client.generate_order_canceled = cancels
        client._push_account_state = _noop
        return fills, cancels

    def test_exchange_cancel_generates_canceled_event(self, client_env):
        client, stub, loop = client_env
        _, cancels = self._wire(client)
        _seed_pending(client)
        stub.orders["42"] = {
            "amount": "1.0",
            "left": "1.0",
            "status": "cancelled",
            "side": "buy",
        }

        loop.run_until_complete(client._check_fills("42"))

        assert len(cancels) == 1
        assert "42" not in client._pending

    def test_our_own_cancel_suppresses_duplicate_event(self, client_env):
        client, stub, loop = client_env
        _, cancels = self._wire(client)
        _seed_pending(client)
        client._cancelling.add("42")
        stub.orders["42"] = {
            "amount": "1.0",
            "left": "1.0",
            "status": "cancelled",
            "side": "buy",
        }

        loop.run_until_complete(client._check_fills("42"))

        assert len(cancels) == 0
        assert "42" not in client._pending
        assert "42" not in client._cancelling


class TestAccountState:
    def test_builds_balances_and_skips_zero(self, client_env):
        client, stub, loop = client_env
        states = Recorder()
        client.generate_account_state = states
        stub.balances_data = {
            "BTC": {"available": 1.5, "locked": 0.5},
            "USDT": {"available": 0.0, "locked": 0.0},  # zero - must be skipped
        }

        loop.run_until_complete(client._push_account_state())

        assert len(states) == 1
        balances = states.calls[0][1]["balances"]
        assert len(balances) == 1
        balance = balances[0]
        assert balance.currency.code == "BTC"
        assert balance.total.as_double() == pytest.approx(2.0)
        assert balance.locked.as_double() == pytest.approx(0.5)
        assert balance.free.as_double() == pytest.approx(1.5)

    def test_no_event_when_all_balances_zero(self, client_env):
        client, stub, loop = client_env
        states = Recorder()
        client.generate_account_state = states
        stub.balances_data = {"USDT": {"available": 0.0, "locked": 0.0}}

        loop.run_until_complete(client._push_account_state())

        assert len(states) == 0

    def test_balance_fetch_failure_is_swallowed(self, client_env):
        client, stub, loop = client_env
        states = Recorder()
        client.generate_account_state = states

        def _boom():
            raise ConnectionError("unreachable")

        stub.balances = _boom
        loop.run_until_complete(client._push_account_state())
        assert len(states) == 0


class _FakeOrder:
    """Duck-typed stand-in for a Nautilus order inside a SubmitOrder command."""

    def __init__(self, order_type: OrderType, side: OrderSide, quantity: str, price=None):
        self.instrument_id = BTC_USDT
        self.strategy_id = STRATEGY
        self.client_order_id = ClientOrderId("O-1")
        self.order_type = order_type
        self.side = side
        self.quantity = quantity
        self.price = price


class _FakeCommand:
    def __init__(self, order: _FakeOrder):
        self.order = order


class TestSubmitMarketOrder:
    def _wire(self, client) -> dict[str, Recorder]:
        recorders = {
            name: Recorder()
            for name in (
                "generate_order_submitted",
                "generate_order_accepted",
                "generate_order_rejected",
                "generate_order_filled",
                "generate_order_canceled",
            )
        }
        for name, recorder in recorders.items():
            setattr(client, name, recorder)
        client._push_account_state = _noop
        return recorders

    def test_market_buy_emulated_as_aggressive_ioc_limit(self, client_env):
        client, stub, loop = client_env
        recorders = self._wire(client)
        stub.last_price = 100.0
        stub.orders["777"] = {
            "amount": "0.5",
            "left": "0.5",
            "status": "open",
            "side": "buy",
        }
        command = _FakeCommand(_FakeOrder(OrderType.MARKET, OrderSide.BUY, "0.5"))

        loop.run_until_complete(client._submit_order(command))

        assert len(recorders["generate_order_submitted"]) == 1
        assert len(recorders["generate_order_accepted"]) == 1
        assert len(recorders["generate_order_rejected"]) == 0
        assert len(stub.placed) == 1
        placed = stub.placed[0]
        assert placed["type"] == "limit"
        assert placed["tif"] == "ioc"
        assert placed["side"] == "buy"
        assert placed["amount"] == "0.5"
        # 100.0 * 1.01 rounded to price_precision=2 -> 101.0
        assert float(placed["price"]) == pytest.approx(101.0)
        assert placed["text"] == "t-O-1"
        assert "777" in client._pending

    def test_market_sell_crosses_down(self, client_env):
        client, stub, loop = client_env
        self._wire(client)
        stub.last_price = 100.0
        stub.orders["777"] = {
            "amount": "0.5",
            "left": "0.5",
            "status": "open",
            "side": "sell",
        }
        command = _FakeCommand(_FakeOrder(OrderType.MARKET, OrderSide.SELL, "0.5"))

        loop.run_until_complete(client._submit_order(command))

        assert float(stub.placed[0]["price"]) == pytest.approx(99.0)

    def test_place_order_exception_generates_rejected(self, client_env):
        client, stub, loop = client_env
        recorders = self._wire(client)
        stub.place_error = RuntimeError("BALANCE_NOT_ENOUGH")
        command = _FakeCommand(_FakeOrder(OrderType.MARKET, OrderSide.BUY, "0.5"))

        loop.run_until_complete(client._submit_order(command))

        assert len(recorders["generate_order_rejected"]) == 1
        assert len(recorders["generate_order_accepted"]) == 0
        assert client._pending == {}

    def test_unsupported_order_type_rejected(self, client_env):
        client, stub, loop = client_env
        recorders = self._wire(client)
        command = _FakeCommand(_FakeOrder(OrderType.STOP_MARKET, OrderSide.BUY, "0.5"))

        loop.run_until_complete(client._submit_order(command))

        assert len(recorders["generate_order_rejected"]) == 1
        assert stub.placed == []
