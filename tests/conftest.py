"""Shared test fixtures.

The whole suite runs offline: no test opens a socket, and no test reads or
needs Gate.io credentials. Two autouse fixtures enforce that:

* :func:`_isolate_credential_env` removes the Gate.io credential variables from
  the environment, so a developer machine that exports real keys produces the
  same results as CI.
* :func:`_clear_factory_caches` resets the ``lru_cache`` on the client
  factories, so a cached HTTP client or instrument provider never leaks from
  one test into the next.

:func:`block_network` is opt-in and raises on any attempt to open a TCP
connection; it is used by tests that assert construction is network-free.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from nautilus_gateio import factories
from nautilus_gateio.common.credentials import (
    ENV_API_KEY,
    ENV_API_SECRET,
    ENV_TESTNET_API_KEY,
    ENV_TESTNET_API_SECRET,
)

#: Environment variables the adapter reads credentials from.
CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    ENV_API_KEY,
    ENV_API_SECRET,
    ENV_TESTNET_API_KEY,
    ENV_TESTNET_API_SECRET,
)


@pytest.fixture(autouse=True)
def _isolate_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient Gate.io credentials so tests never depend on the machine."""
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clear_factory_caches() -> Iterator[None]:
    """Reset the factory caches around every test."""
    factories.get_cached_gateio_http_client.cache_clear()
    factories.get_cached_gateio_instrument_provider.cache_clear()
    yield
    factories.get_cached_gateio_http_client.cache_clear()
    factories.get_cached_gateio_instrument_provider.cache_clear()


class NetworkAccessError(AssertionError):
    """Raised when a test tries to open a network connection."""


@pytest.fixture
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outbound TCP connection attempt fail loudly."""

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise NetworkAccessError("the test attempted to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
