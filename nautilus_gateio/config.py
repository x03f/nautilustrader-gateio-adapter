"""Configuration classes for the Gate.io adapter.

Credentials resolution follows the NautilusTrader adapter convention: if
``api_key`` / ``api_secret`` are ``None`` they are read from environment
variables at client-creation time:

* mainnet: ``GATE_API_KEY`` / ``GATE_API_SECRET``
* testnet: ``GATE_TESTNET_API_KEY`` / ``GATE_TESTNET_API_SECRET``
  (falling back to the mainnet variables if unset)

Safety defaults: the execution client targets the Gate.io *testnet* host by
default. Trading against mainnet requires explicitly setting
``environment="mainnet"`` in :class:`GateioExecClientConfig`.
"""

from __future__ import annotations

import os

from nautilus_trader.config import NautilusConfig
from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig

from nautilus_gateio.constants import (
    GATEIO,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
    GATEIO_WS_MAINNET,
)


def resolve_credentials(
    api_key: str | None,
    api_secret: str | None,
    testnet: bool,
) -> tuple[str, str]:
    """Resolve credentials from explicit values or environment variables.

    Returns ``(api_key, api_secret)``; empty strings mean "no credentials"
    (public data still works). Values are stripped of surrounding whitespace
    so keys pasted with trailing newlines do not break request signing.
    """
    if api_key is None:
        if testnet:
            api_key = os.getenv("GATE_TESTNET_API_KEY") or os.getenv("GATE_API_KEY", "")
        else:
            api_key = os.getenv("GATE_API_KEY", "")
    if api_secret is None:
        if testnet:
            api_secret = os.getenv("GATE_TESTNET_API_SECRET") or os.getenv("GATE_API_SECRET", "")
        else:
            api_secret = os.getenv("GATE_API_SECRET", "")
    return (api_key or "").strip(), (api_secret or "").strip()


class GateioDataClientConfig(LiveDataClientConfig, frozen=True):
    """Configuration for :class:`~nautilus_gateio.data.GateioDataClient`.

    Parameters
    ----------
    venue : str
        Venue string used in instrument ids (default ``GATEIO``).
    base_url_http : str
        REST endpoint for market data (public; mainnet by default — Gate.io
        has no public spot testnet market data).
    base_url_ws : str
        WebSocket endpoint for market data.
    use_websocket : bool
        WebSocket is the primary transport; when ``False`` the client uses
        REST polling only.
    poll_interval_secs : float
        REST polling cadence for the fallback transport.
    emit_synthetic_quotes : bool
        When ``True`` (default) the client synthesizes a ``QuoteTick`` around
        each closed bar (close +/- 0.5 bp, unit sizes). This exists so
        sandbox/backtest-style execution clients that fill on quotes receive
        top-of-book updates. These are NOT real quotes — disable if your
        strategy consumes quote ticks as market truth.
    """

    venue: str = GATEIO
    base_url_http: str = GATEIO_HTTP_MAINNET
    base_url_ws: str = GATEIO_WS_MAINNET
    use_websocket: bool = True
    poll_interval_secs: float = 5.0
    emit_synthetic_quotes: bool = True


class GateioExecClientConfig(LiveExecClientConfig, frozen=True):
    """Configuration for :class:`~nautilus_gateio.execution.GateioExecutionClient`.

    Parameters
    ----------
    api_key, api_secret : str, optional
        Credentials; ``None`` reads environment variables (see module docs).
    environment : str
        ``"testnet"`` (default) or ``"mainnet"``. The default deliberately
        targets Gate.io's testnet host; mainnet must be opted into explicitly.
    base_url_http : str, optional
        Override the REST endpoint; when ``None`` it derives from
        ``environment``.
    venue : str
        Venue string (default ``GATEIO``).
    account_poll_interval_secs : float
        Cadence of the order-fill/balance polling loop (the adapter has no
        private WebSocket yet; fills are detected via REST polling).
    client_order_id_tag : str
        Short tag embedded in generated Gate.io ``text`` client order ids.
    """

    api_key: str | None = None
    api_secret: str | None = None
    environment: str = "testnet"
    base_url_http: str | None = None
    venue: str = GATEIO
    account_poll_interval_secs: float = 5.0
    client_order_id_tag: str = "ng"

    @property
    def is_testnet(self) -> bool:
        return self.environment.lower() != "mainnet"

    def resolve_base_url(self) -> str:
        if self.base_url_http:
            return self.base_url_http
        return GATEIO_HTTP_TESTNET if self.is_testnet else GATEIO_HTTP_MAINNET


class GateioPaperConfig(NautilusConfig, frozen=True):
    """Configuration for the local paper-fill simulator (no exchange orders)."""

    starting_balances: dict[str, float] | None = None
    taker_fee: float = 0.0016
    maker_fee: float = 0.00075
