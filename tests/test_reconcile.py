"""Tests for the diagnostic reconciliation helper (no network - mock exchange)."""

from __future__ import annotations

from typing import Any

from nautilus_gateio.reconcile import reconcile


class _MockExec:
    """Test double exposing the minimal exchange surface ``reconcile`` needs."""

    def __init__(
        self,
        balances: dict[str, dict[str, float]] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        unreachable: bool = False,
    ) -> None:
        self._balances = balances or {}
        self._open_orders = open_orders or []
        self._unreachable = unreachable

    def balances(self) -> dict[str, dict[str, float]]:
        if self._unreachable:
            raise ConnectionError("connection refused")
        return self._balances

    def open_orders(self, pair: str) -> list[dict[str, Any]]:
        return list(self._open_orders)

    def my_trades(self, pair: str, limit: int = 100) -> list[dict[str, Any]]:
        return []


class TestReconcile:
    def test_in_sync(self):
        exchange = _MockExec(
            balances={"BTC": {"available": 0.5, "locked": 0.0}},
        )
        local_state = {"positions": {"BTC_USDT": 0.5}, "known_order_ids": []}

        report = reconcile(exchange, local_state, ["BTC_USDT"])

        assert report["exchange_reachable"] is True
        assert report["in_sync"] is True
        assert report["discrepancies"] == []
        assert report["actions"] == []

    def test_unknown_open_order_flagged_for_adoption(self):
        exchange = _MockExec(
            balances={},
            open_orders=[{"id": "999", "side": "buy", "left": "0.1"}],
        )
        local_state = {"positions": {}, "known_order_ids": []}

        report = reconcile(exchange, local_state, ["BTC_USDT"])

        assert report["in_sync"] is False
        assert any("999" in d for d in report["discrepancies"])
        actions = [a for a in report["actions"] if a["type"] == "adopt_or_cancel"]
        assert len(actions) == 1
        assert actions[0]["order_id"] == "999"
        assert actions[0]["pair"] == "BTC_USDT"

    def test_known_open_order_is_fine(self):
        exchange = _MockExec(
            balances={},
            open_orders=[{"id": "999", "side": "buy", "left": "0.1"}],
        )
        local_state = {"positions": {}, "known_order_ids": ["999"]}

        report = reconcile(exchange, local_state, ["BTC_USDT"])

        assert report["in_sync"] is True
        assert report["discrepancies"] == []
        assert report["actions"] == []

    def test_exchange_unreachable(self):
        exchange = _MockExec(unreachable=True)
        local_state = {"positions": {}, "known_order_ids": []}

        report = reconcile(exchange, local_state, ["BTC_USDT"])

        assert report["exchange_reachable"] is False
        assert len(report["discrepancies"]) == 1
        assert "unreachable" in report["discrepancies"][0]

    def test_local_position_without_exchange_base_asset(self):
        exchange = _MockExec(balances={})  # no BTC on the exchange
        local_state = {"positions": {"BTC_USDT": 0.5}, "known_order_ids": []}

        report = reconcile(exchange, local_state, ["BTC_USDT"])

        assert report["in_sync"] is False
        actions = [a for a in report["actions"] if a["type"] == "resync_position"]
        assert len(actions) == 1
        assert actions[0]["pair"] == "BTC_USDT"
        assert actions[0]["local"] == 0.5
        assert actions[0]["exchange_base"] == 0.0

    def test_report_includes_exchange_snapshot(self):
        balances = {"USDT": {"available": 100.0, "locked": 0.0}}
        exchange = _MockExec(balances=balances)
        report = reconcile(exchange, {"positions": {}, "known_order_ids": []}, [])
        assert report["exchange_balances"] == balances
        assert report["in_sync"] is True
