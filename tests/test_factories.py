"""Tests for the live client factories, wired the way ``TradingNode`` wires them.

Every test builds real NautilusTrader ``MessageBus`` / ``Cache`` / ``LiveClock``
components and asserts that the factories return fully constructed clients
without touching the network. The ``block_network`` fixture (see
``conftest.py``) turns any connection attempt into a test failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId

from nautilus_gateio.common.constants import GATEIO_HTTP_MAINNET, GATEIO_HTTP_TESTNET, GATEIO_VENUE
from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode
from nautilus_gateio.config import GateioDataClientConfig, GateioExecClientConfig
from nautilus_gateio.data import GateioDataClient
from nautilus_gateio.execution import GateioExecutionClient
from nautilus_gateio.factories import (
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
    get_cached_gateio_http_client,
    get_cached_gateio_instrument_provider,
)

CLIENT_NAME = "GATE_IO"


@pytest.fixture
def node_components() -> Iterator[tuple[LiveClock, MessageBus, Cache, asyncio.AbstractEventLoop]]:
    """Fresh clock/msgbus/cache/loop per test, as a ``TradingNode`` provides."""
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = Cache()
    loop = asyncio.new_event_loop()
    yield clock, msgbus, cache, loop
    loop.close()


def build_data_client(components, config: GateioDataClientConfig) -> GateioDataClient:
    clock, msgbus, cache, loop = components
    return GateioLiveDataClientFactory.create(
        loop=loop,
        name=CLIENT_NAME,
        config=config,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )


def build_exec_client(components, config: GateioExecClientConfig) -> GateioExecutionClient:
    clock, msgbus, cache, loop = components
    return GateioLiveExecClientFactory.create(
        loop=loop,
        name=CLIENT_NAME,
        config=config,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )


class TestDataClientFactory:
    def test_builds_a_data_client(self, node_components, block_network):
        client = build_data_client(node_components, GateioDataClientConfig())
        assert isinstance(client, GateioDataClient)
        assert str(client.id) == CLIENT_NAME
        assert client.venue == GATEIO_VENUE

    def test_construction_opens_no_websocket_connections(self, node_components, block_network):
        client = build_data_client(node_components, GateioDataClientConfig())
        assert client._ws_clients == {}

    def test_http_transport_targets_the_configured_environment(
        self, node_components, block_network
    ):
        mainnet = build_data_client(node_components, GateioDataClientConfig())
        assert mainnet._http_client.base_url == GATEIO_HTTP_MAINNET

        testnet = build_data_client(
            node_components,
            GateioDataClientConfig(environment="testnet"),
        )
        assert testnet._http_client.base_url == GATEIO_HTTP_TESTNET

    def test_configured_products_reach_the_client_and_the_provider(
        self,
        node_components,
        block_network,
    ):
        products = (GateioProductType.SPOT, GateioProductType.PERP)
        client = build_data_client(node_components, GateioDataClientConfig(products=products))
        assert client._products == products
        assert tuple(client._instrument_provider.products) == products

    def test_invalid_configuration_is_rejected_before_any_client_is_built(
        self,
        node_components,
        block_network,
    ):
        with pytest.raises(ValueError, match="no testnet endpoint"):
            build_data_client(
                node_components,
                GateioDataClientConfig(
                    environment="testnet",
                    products=(GateioProductType.OPT,),
                ),
            )

    @pytest.mark.parametrize("interval_ms", [0, 50, 250])
    def test_invalid_book_interval_is_rejected(self, node_components, block_network, interval_ms):
        with pytest.raises(ValueError, match="order_book_update_interval_ms"):
            build_data_client(
                node_components,
                GateioDataClientConfig(order_book_update_interval_ms=interval_ms),
            )


class TestExecClientFactory:
    def test_builds_an_execution_client(self, node_components, block_network):
        client = build_exec_client(node_components, GateioExecClientConfig())
        assert isinstance(client, GateioExecutionClient)
        assert str(client.id) == CLIENT_NAME
        assert client.venue == GATEIO_VENUE

    def test_account_id_is_the_single_master_account(self, node_components, block_network):
        """One Nautilus account aggregates Gate.io's segregated wallets."""
        client = build_exec_client(node_components, GateioExecClientConfig())
        assert str(client.account_id) == "GATE_IO-master"

    def test_oms_type_is_netting(self, node_components, block_network):
        client = build_exec_client(node_components, GateioExecClientConfig())
        assert client.oms_type == OmsType.NETTING

    def test_spot_only_plain_spot_is_a_cash_account(self, node_components, block_network):
        client = build_exec_client(
            node_components,
            GateioExecClientConfig(products=(GateioProductType.SPOT,)),
        )
        assert client.account_type == AccountType.CASH

    @pytest.mark.parametrize(
        "mode",
        [
            GateioSpotAccountMode.MARGIN,
            GateioSpotAccountMode.CROSS_MARGIN,
            GateioSpotAccountMode.UNIFIED,
        ],
    )
    def test_spot_margin_modes_produce_a_margin_account(
        self,
        node_components,
        block_network,
        mode,
    ):
        client = build_exec_client(
            node_components,
            GateioExecClientConfig(products=(GateioProductType.SPOT,), spot_account_mode=mode),
        )
        assert client.account_type == AccountType.MARGIN

    @pytest.mark.parametrize(
        "products",
        [
            (GateioProductType.PERP,),
            (GateioProductType.INVERSE,),
            (GateioProductType.FUT,),
            (GateioProductType.OPT,),
            (GateioProductType.SPOT, GateioProductType.PERP),
        ],
    )
    def test_any_derivative_product_produces_a_margin_account(
        self,
        node_components,
        block_network,
        products,
    ):
        client = build_exec_client(node_components, GateioExecClientConfig(products=products))
        assert client.account_type == AccountType.MARGIN

    def test_construction_opens_no_websocket_connections(self, node_components, block_network):
        client = build_exec_client(node_components, GateioExecClientConfig())
        assert client._ws_clients == {}

    def test_http_transport_targets_the_configured_environment(
        self, node_components, block_network
    ):
        client = build_exec_client(node_components, GateioExecClientConfig(environment="testnet"))
        assert client._http_client.base_url == GATEIO_HTTP_TESTNET

    def test_invalid_configuration_is_rejected_before_any_client_is_built(
        self,
        node_components,
        block_network,
    ):
        with pytest.raises(ValueError, match="no testnet endpoint"):
            build_exec_client(
                node_components,
                GateioExecClientConfig(
                    environment="testnet",
                    products=(GateioProductType.FUT,),
                ),
            )


class TestNoNetworkAtConstruction:
    """Constructing a client must never reach out to the venue."""

    def test_the_network_guard_itself_works(self, block_network):
        import socket

        with pytest.raises(AssertionError, match="attempted to open a network connection"):
            socket.create_connection(("127.0.0.1", 1))

    def test_building_both_clients_stays_offline(self, node_components, block_network):
        data = build_data_client(node_components, GateioDataClientConfig())
        execution = build_exec_client(node_components, GateioExecClientConfig())
        assert data is not None
        assert execution is not None

    def test_the_shared_transport_has_not_synchronised_the_venue_clock(
        self,
        node_components,
        block_network,
    ):
        client = build_data_client(node_components, GateioDataClientConfig())
        assert client._http_client._clock_synced is False


class TestCaching:
    def test_identical_arguments_return_the_same_transport(self, block_network):
        first = get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)
        second = get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)
        assert first is second

    def test_a_different_configuration_gets_its_own_transport(self, block_network):
        mainnet = get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)
        testnet = get_cached_gateio_http_client(base_url=GATEIO_HTTP_TESTNET)
        assert mainnet is not testnet
        assert mainnet.base_url == GATEIO_HTTP_MAINNET
        assert testnet.base_url == GATEIO_HTTP_TESTNET

    def test_different_credentials_get_their_own_transport(self, block_network):
        anonymous = get_cached_gateio_http_client()
        credentialed = get_cached_gateio_http_client(api_key="k", api_secret="s")
        assert anonymous is not credentialed

    def test_the_cache_holds_one_entry_so_an_alternating_call_evicts(self, block_network):
        """``lru_cache(1)``, matching the official adapters: last configuration wins."""
        first = get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)
        get_cached_gateio_http_client(base_url=GATEIO_HTTP_TESTNET)
        again = get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)
        assert again is not first

    def test_data_and_execution_clients_share_one_transport(self, node_components, block_network):
        data = build_data_client(node_components, GateioDataClientConfig())
        execution = build_exec_client(node_components, GateioExecClientConfig())
        assert data._http_client is execution._http_client

    def test_data_and_execution_clients_share_one_instrument_provider(
        self,
        node_components,
        block_network,
    ):
        """One instrument load serves both clients, as the official adapters do."""
        data = build_data_client(node_components, GateioDataClientConfig())
        execution = build_exec_client(node_components, GateioExecClientConfig())
        assert data._instrument_provider is execution._instrument_provider

    def test_differing_product_sets_get_their_own_provider(self, node_components, block_network):
        spot = build_data_client(
            node_components,
            GateioDataClientConfig(products=(GateioProductType.SPOT,)),
        )
        perp = build_data_client(
            node_components,
            GateioDataClientConfig(products=(GateioProductType.PERP,)),
        )
        assert spot._instrument_provider is not perp._instrument_provider

    def test_provider_cache_is_keyed_on_the_transport(self, block_network):
        transport = get_cached_gateio_http_client(base_url=GATEIO_HTTP_MAINNET)
        first = get_cached_gateio_instrument_provider(
            http_client=transport,
            products=(GateioProductType.SPOT,),
        )
        second = get_cached_gateio_instrument_provider(
            http_client=transport,
            products=(GateioProductType.SPOT,),
        )
        assert first is second

    def test_caches_are_cleared_between_tests(self, block_network):
        """Guards the autouse ``_clear_factory_caches`` fixture in conftest."""
        assert get_cached_gateio_http_client.cache_info().currsize == 0
        assert get_cached_gateio_instrument_provider.cache_info().currsize == 0


class TestSharedTransportLifecycle:
    """Regression for seam-08: nothing ever released the shared HTTP transport.

    The client was reference counted and closeable, but no caller acquired or
    released it, so the connection pool outlived every trading node in the
    process. Closing it naively was not enough either: the transport is cached,
    so a second node in the same process would have been handed a closed client.
    """

    def test_the_factory_registers_one_owner_per_client(self, block_network):
        from nautilus_gateio.factories import _build_http_client

        transport = _build_http_client(GateioDataClientConfig()).acquire()
        try:
            assert transport.owner_count == 1
            transport.acquire()
            assert transport.owner_count == 2
        finally:
            get_cached_gateio_http_client.cache_clear()

    def test_the_last_release_closes_the_transport(self, block_network):
        from nautilus_gateio.factories import _build_http_client

        transport = _build_http_client(GateioDataClientConfig()).acquire()
        transport.acquire()
        try:
            asyncio.run(transport.close())
            assert not transport.is_closed, "one owner still holds it"
            asyncio.run(transport.close())
            assert transport.is_closed
        finally:
            get_cached_gateio_http_client.cache_clear()

    def test_a_closed_cached_transport_is_rebuilt(self, block_network):
        """A second node in the same process must not receive a closed client."""
        first = get_cached_gateio_http_client(base_url="https://example.invalid")
        asyncio.run(first.close())
        assert first.is_closed

        second = get_cached_gateio_http_client(base_url="https://example.invalid")
        try:
            assert second is not first
            assert not second.is_closed
        finally:
            get_cached_gateio_http_client.cache_clear()
