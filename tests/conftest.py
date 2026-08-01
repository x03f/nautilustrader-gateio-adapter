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

from gateio_nt import factories
from gateio_nt.common.credentials import (
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


@pytest.fixture(autouse=True)
def _no_undispatchable_publish() -> Iterator[None]:
    """Fail any test whose client published a type the data engine cannot dispatch.

    ``DataEngine._handle_data`` dispatches on the concrete type and drops
    anything it does not recognise with a single error line, so a venue-native
    type published outside ``CustomData`` reaches no subscriber while every
    assertion in this suite still sees it — that is how one shipped through a
    green suite. The data tests replace ``_handle_data`` with a recorder that
    notes such an object (``tests/test_data_client.py``), and the reading happens
    here, after the test: most publishes run inside the client's WebSocket
    message handler, which catches per-message exceptions, so an assertion at the
    publish site would be swallowed exactly like the defect it looks for.
    """
    from tests.test_data_client import UNDISPATCHABLE_PUBLISHES

    UNDISPATCHABLE_PUBLISHES.clear()
    yield
    offenders = list(UNDISPATCHABLE_PUBLISHES)
    UNDISPATCHABLE_PUBLISHES.clear()
    assert not offenders, (
        f"published {offenders}, which `DataEngine._handle_data` would log as an "
        f"unrecognised type and drop; a venue-native type has to be published inside "
        f"`CustomData`"
    )


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


class _LogCapture:
    """Reads back what a client logged, through the platform's own subsystem.

    Some behaviour the platform prescribes *is* a log line and nothing else:
    "cancel, modify, cancel-all, and batch-cancel commands that fail local
    checks log warnings and do not produce rejection events" (concepts/live.md),
    and an ambiguous venue outcome is "logged" while the order is left in flight.
    A test that cannot read the line cannot tell the fixed behaviour from the
    bug, so the line is read where the platform writes it. ``Component._log`` is
    a Cython attribute and not writable, and ``init_logging`` is the documented
    way in — file output is its only machine-readable sink.
    """

    def __init__(self, path: Any) -> None:
        self._path = path
        self._mark = 0

    def _lines(self) -> list[str]:
        from nautilus_trader.common.component import flush_logger

        flush_logger()
        if not self._path.exists():
            return []
        return self._path.read_text(encoding="utf-8").splitlines()

    def mark(self) -> None:
        self._mark = len(self._lines())

    def since(self) -> list[str]:
        return self._lines()[self._mark :]

    def wait_for(self, fragment: str, timeout: float = 5.0) -> list[str]:
        """Return the new lines once one of them carries ``fragment``.

        The logging subsystem writes from its own thread, so a flush asks for the
        write rather than completing it; under a full suite the line lands a
        moment after the call returns.
        """
        import time

        deadline = time.monotonic() + timeout
        while True:
            lines = self.since()
            if any(fragment in line for line in lines) or time.monotonic() >= deadline:
                return lines
            time.sleep(0.02)


@pytest.fixture(scope="session")
def log_capture(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_LogCapture]:
    """Initialize the platform logger once per session and read its file sink.

    This lives in ``conftest`` rather than beside the tests that use it because
    ``init_logging`` can only be called once per process. A fixture defined in a
    test module is a separate fixture definition per module even when the same
    function object is imported, so a second module asking for it would find the
    subsystem already initialized and skip — silently disabling exactly the
    assertions that read a log line.
    """
    from nautilus_trader.common.component import init_logging, is_logging_initialized
    from nautilus_trader.common.enums import LogLevel

    if is_logging_initialized():
        pytest.skip("the logging subsystem was initialized elsewhere; its sink is unknown")

    directory = tmp_path_factory.mktemp("logs")
    guard = init_logging(
        level_stdout=LogLevel.OFF,
        level_file=LogLevel.DEBUG,
        directory=str(directory),
        file_name="capture",
    )
    yield _LogCapture(directory / "capture.log")
    del guard
