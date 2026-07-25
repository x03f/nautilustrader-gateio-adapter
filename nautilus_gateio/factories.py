"""Factories registering the Gate.io clients with a Nautilus ``TradingNode``.

Usage:

.. code-block:: python

    from nautilus_trader.live.node import TradingNode
    from nautilus_gateio import (
        GATEIO,
        GateioDataClientConfig,
        GateioExecClientConfig,
        GateioLiveDataClientFactory,
        GateioLiveExecClientFactory,
    )

    node = TradingNode(config=node_config)  # config with data/exec client sections
    node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
    node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
    node.build()
"""

from __future__ import annotations

import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory
from nautilus_trader.model.identifiers import ClientId

from nautilus_gateio.config import GateioDataClientConfig, GateioExecClientConfig
from nautilus_gateio.data import GateioDataClient
from nautilus_gateio.execution import GateioExecutionClient
from nautilus_gateio.http import GateioHttpClient
from nautilus_gateio.providers import GateioInstrumentProvider


class GateioLiveDataClientFactory(LiveDataClientFactory):
    """Creates :class:`~nautilus_gateio.data.GateioDataClient` instances."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: GateioDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> GateioDataClient:
        if not isinstance(config, GateioDataClientConfig):
            config = GateioDataClientConfig()
        provider = GateioInstrumentProvider(
            http_client=GateioHttpClient(base_url=config.base_url_http),
            config=config.instrument_provider,
            venue=config.venue,
        )
        return GateioDataClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
        )


class GateioLiveExecClientFactory(LiveExecClientFactory):
    """Creates :class:`~nautilus_gateio.execution.GateioExecutionClient` instances."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: GateioExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> GateioExecutionClient:
        if not isinstance(config, GateioExecClientConfig):
            config = GateioExecClientConfig()
        return GateioExecutionClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
