"""Tests for the live client factories, wired the way ``TradingNode`` wires them.

Constructs real Nautilus ``MessageBus`` / ``Cache`` / ``LiveClock`` components
and asserts that the factories return fully constructed clients without any
network activity.
"""

from __future__ import annotations

import asyncio

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId

from nautilus_gateio.config import GateioDataClientConfig, GateioExecClientConfig
from nautilus_gateio.constants import GATEIO_HTTP_TESTNET
from nautilus_gateio.data import GateioDataClient
from nautilus_gateio.execution import GateioExecutionClient
from nautilus_gateio.factories import (
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
)


@pytest.fixture()
def node_components():
    """Fresh msgbus/cache/clock/loop per test, as a TradingNode would provide."""
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = Cache()
    loop = asyncio.new_event_loop()
    yield clock, msgbus, cache, loop
    loop.close()


class TestDataClientFactory:
    def test_create_returns_data_client(self, node_components):
        clock, msgbus, cache, loop = node_components
        client = GateioLiveDataClientFactory.create(
            loop=loop,
            name="GATEIO",
            config=GateioDataClientConfig(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        assert isinstance(client, GateioDataClient)
        assert str(client.id) == "GATEIO"

    def test_create_with_wrong_config_type_falls_back_to_defaults(self, node_components):
        clock, msgbus, cache, loop = node_components
        client = GateioLiveDataClientFactory.create(
            loop=loop,
            name="GATEIO",
            config=None,  # type: ignore[arg-type]
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        assert isinstance(client, GateioDataClient)


class TestExecClientFactory:
    def test_create_returns_execution_client(self, node_components):
        clock, msgbus, cache, loop = node_components
        client = GateioLiveExecClientFactory.create(
            loop=loop,
            name="GATEIO",
            config=GateioExecClientConfig(api_key="k", api_secret="s"),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        assert isinstance(client, GateioExecutionClient)

    def test_account_id_and_testnet_base_url(self, node_components):
        clock, msgbus, cache, loop = node_components
        client = GateioLiveExecClientFactory.create(
            loop=loop,
            name="GATEIO",
            config=GateioExecClientConfig(api_key="k", api_secret="s"),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        assert str(client.account_id) == "GATEIO-SPOT"
        # Default environment is testnet; the HTTP client must target it.
        assert client._http.base_url == GATEIO_HTTP_TESTNET
        # Order flow explicitly enabled for the live execution client.
        assert client._http.live_orders is True
