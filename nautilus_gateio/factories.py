"""Factories registering the Gate.io clients with a NautilusTrader ``TradingNode``.

.. code-block:: python

    from nautilus_trader.live.node import TradingNode

    from nautilus_gateio import (
        GATEIO,
        GateioDataClientConfig,
        GateioExecClientConfig,
        GateioLiveDataClientFactory,
        GateioLiveExecClientFactory,
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
    node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)
    node.build()

The HTTP transport and the instrument provider are cached so that a data client
and an execution client sharing the same credentials, environment and product
set also share one connection pool and one instrument load, as the official
adapters do. Caching is keyed on those values, so differing configurations
still get their own instances.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import TYPE_CHECKING

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory
from nautilus_trader.model.identifiers import ClientId

from nautilus_gateio.common.constants import DEFAULT_HTTP_TIMEOUT_SECS, GATEIO_HTTP_MAINNET
from nautilus_gateio.common.credentials import resolve_credentials
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.config import GateioDataClientConfig, GateioExecClientConfig
from nautilus_gateio.data import GateioDataClient
from nautilus_gateio.http.client import GateioHttpClient
from nautilus_gateio.providers import GateioInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.live.execution_client import LiveExecutionClient


@lru_cache(1)
def get_cached_gateio_http_client(
    api_key: str = "",
    api_secret: str = "",
    base_url: str = GATEIO_HTTP_MAINNET,
    timeout_secs: float = DEFAULT_HTTP_TIMEOUT_SECS,
    max_retries: int = 3,
) -> GateioHttpClient:
    """Return a cached :class:`GateioHttpClient`.

    Parameters
    ----------
    api_key : str
        The Gate.io API key (empty for public data only).
    api_secret : str
        The Gate.io API secret (empty for public data only).
    base_url : str
        The REST base URL.
    timeout_secs : float
        The per-request timeout.
    max_retries : int
        The attempts for rate-limited or transient server errors.

    Returns
    -------
    GateioHttpClient

    """
    return GateioHttpClient(
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
        timeout_secs=timeout_secs,
        max_retries=max_retries,
    )


@lru_cache(1)
def get_cached_gateio_instrument_provider(
    http_client: GateioHttpClient,
    products: tuple[GateioProductType, ...],
    options_underlyings: tuple[str, ...] | None = None,
    config: InstrumentProviderConfig | None = None,
) -> GateioInstrumentProvider:
    """Return a cached :class:`GateioInstrumentProvider`.

    Parameters
    ----------
    http_client : GateioHttpClient
        The REST transport used to load instrument definitions.
    products : tuple[GateioProductType, ...]
        The products to load.
    options_underlyings : tuple[str, ...], optional
        Restricts option loading to these underlyings.
    config : InstrumentProviderConfig, optional
        The provider configuration (hashable, so it takes part in the key).

    Returns
    -------
    GateioInstrumentProvider

    """
    return GateioInstrumentProvider(
        http_client=http_client,
        config=config,
        products=products,
        options_underlyings=options_underlyings,
    )


def _build_http_client(config: GateioDataClientConfig | GateioExecClientConfig):
    api_key, api_secret = resolve_credentials(
        config.api_key,
        config.api_secret,
        testnet=config.is_testnet,
    )
    return get_cached_gateio_http_client(
        api_key=api_key,
        api_secret=api_secret,
        base_url=config.resolve_http_url(),
        timeout_secs=config.http_timeout_secs,
        max_retries=config.max_retries,
    )


class GateioLiveDataClientFactory(LiveDataClientFactory):
    """Provides a Gate.io live data client factory."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: GateioDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> GateioDataClient:
        """Create a new Gate.io data client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The clients name (used as the client id).
        config : GateioDataClientConfig
            The client configuration.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock : LiveClock
            The clock for the client.

        Returns
        -------
        GateioDataClient

        """
        http_client = _build_http_client(config)
        provider = get_cached_gateio_instrument_provider(
            http_client=http_client,
            products=tuple(config.products),
            options_underlyings=config.options_underlyings,
            config=config.instrument_provider,
        )
        return GateioDataClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            http_client=http_client,
            config=config,
        )


class GateioLiveExecClientFactory(LiveExecClientFactory):
    """Provides a Gate.io live execution client factory."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: GateioExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveExecutionClient:
        """Create a new Gate.io execution client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The clients name (used as the client id).
        config : GateioExecClientConfig
            The client configuration.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock : LiveClock
            The clock for the client.

        Returns
        -------
        GateioExecutionClient

        """
        # Imported here so that this module remains importable (and the data
        # client usable) independently of the execution module.
        from nautilus_gateio.execution import GateioExecutionClient

        http_client = _build_http_client(config)
        provider = get_cached_gateio_instrument_provider(
            http_client=http_client,
            products=tuple(config.products),
            options_underlyings=config.options_underlyings,
            config=config.instrument_provider,
        )
        return GateioExecutionClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            http_client=http_client,
            config=config,
        )


__all__ = [
    "GateioLiveDataClientFactory",
    "GateioLiveExecClientFactory",
    "get_cached_gateio_http_client",
    "get_cached_gateio_instrument_provider",
]
