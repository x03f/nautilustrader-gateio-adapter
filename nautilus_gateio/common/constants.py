"""Venue identifiers, endpoints and interval maps for the Gate.io adapter."""

from __future__ import annotations

from typing import Final

from nautilus_trader.model.identifiers import ClientId, Venue

from nautilus_gateio.common.enums import GateioProductType

# NautilusTrader already identifies this venue as ``GATE_IO`` (see the Tardis
# integration's venue mapping), so the adapter uses the same string to stay
# interoperable with data loaded through other NautilusTrader tooling.
GATEIO: Final[str] = "GATE_IO"
GATEIO_VENUE: Final[Venue] = Venue(GATEIO)
GATEIO_CLIENT_ID: Final[ClientId] = ClientId(GATEIO)

# -- REST ---------------------------------------------------------------------

GATEIO_HTTP_MAINNET: Final[str] = "https://api.gateio.ws"
#: Gate.io testnet. Verified to serve both ``/api/v4/spot/*`` and ``/api/v4/futures/*``.
GATEIO_HTTP_TESTNET: Final[str] = "https://api-testnet.gateapi.io"
GATEIO_API_PREFIX: Final[str] = "/api/v4"

# -- WebSocket ----------------------------------------------------------------
#
# Verified by connecting to each endpoint. Note that the options host published in
# some Gate.io documentation (``op-ws.gateio.ws``) does not resolve; the working
# host is ``op-ws.gateio.live``.

GATEIO_WS_SPOT: Final[str] = "wss://api.gateio.ws/ws/v4/"
GATEIO_WS_PERP_USDT: Final[str] = "wss://fx-ws.gateio.ws/v4/ws/usdt"
GATEIO_WS_PERP_BTC: Final[str] = "wss://fx-ws.gateio.ws/v4/ws/btc"
GATEIO_WS_DELIVERY_USDT: Final[str] = "wss://fx-ws.gateio.ws/v4/ws/delivery/usdt"
#: Documented options endpoint. The settle-suffixed variants below also work and
#: can be selected through configuration, but the documented URL is the default.
GATEIO_WS_OPTIONS: Final[str] = "wss://op-ws.gateio.live/v4/ws"
GATEIO_WS_OPTIONS_USDT: Final[str] = "wss://op-ws.gateio.live/v4/ws/usdt"
GATEIO_WS_OPTIONS_BTC: Final[str] = "wss://op-ws.gateio.live/v4/ws/btc"

GATEIO_WS_SPOT_TESTNET: Final[str] = "wss://ws-testnet.gate.com/v4/ws/spot"
GATEIO_WS_PERP_USDT_TESTNET: Final[str] = "wss://fx-ws-testnet.gateio.ws/v4/ws/usdt"

WS_URLS: Final[dict[GateioProductType, str]] = {
    GateioProductType.SPOT: GATEIO_WS_SPOT,
    GateioProductType.PERP: GATEIO_WS_PERP_USDT,
    GateioProductType.INVERSE: GATEIO_WS_PERP_BTC,
    GateioProductType.FUT: GATEIO_WS_DELIVERY_USDT,
    GateioProductType.OPT: GATEIO_WS_OPTIONS,
}


def ws_url(product: GateioProductType, testnet: bool = False) -> str:
    """Return the WebSocket endpoint for ``product``."""
    if testnet:
        if product is GateioProductType.SPOT:
            return GATEIO_WS_SPOT_TESTNET
        if product is GateioProductType.PERP:
            return GATEIO_WS_PERP_USDT_TESTNET
        raise ValueError(f"Gate.io has no testnet WebSocket endpoint for {product.value}")
    return WS_URLS[product]


# -- channel prefixes ---------------------------------------------------------
#
# Futures and delivery share the ``futures.*`` channel namespace; they are told
# apart by the endpoint they are subscribed on.

CHANNEL_PREFIX: Final[dict[GateioProductType, str]] = {
    GateioProductType.SPOT: "spot",
    GateioProductType.PERP: "futures",
    GateioProductType.INVERSE: "futures",
    GateioProductType.FUT: "futures",
    GateioProductType.OPT: "options",
}

# -- candlestick intervals ----------------------------------------------------

#: Nautilus bar aggregation token -> Gate.io interval string.
NAUTILUS_TO_GATEIO_INTERVAL: Final[dict[str, str]] = {
    "1-SECOND": "1s",
    "10-SECOND": "10s",
    "1-MINUTE": "1m",
    "5-MINUTE": "5m",
    "15-MINUTE": "15m",
    "30-MINUTE": "30m",
    "1-HOUR": "1h",
    "4-HOUR": "4h",
    "8-HOUR": "8h",
    "1-DAY": "1d",
    "7-DAY": "7d",
}

GATEIO_INTERVAL_MS: Final[dict[str, int]] = {
    "1s": 1_000,
    "10s": 10_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "1d": 86_400_000,
    "7d": 604_800_000,
}

# -- client order ids ---------------------------------------------------------
#
# Gate.io ``text`` field rules: must start with ``t-``; at most 28 characters
# after that prefix; charset ``[0-9A-Za-z_.-]``.

CLIENT_ORDER_ID_PREFIX: Final[str] = "t-"
CLIENT_ORDER_ID_MAX_BODY: Final[int] = 28
DEFAULT_CLIENT_ORDER_ID_TAG: Final[str] = "ng"

# -- request pacing -----------------------------------------------------------

DEFAULT_MAX_REQUESTS_PER_SECOND: Final[float] = 8.0
DEFAULT_HTTP_TIMEOUT_SECS: Final[float] = 20.0
DEFAULT_WS_RECV_TIMEOUT_SECS: Final[float] = 30.0
DEFAULT_WS_MAX_BACKOFF_SECS: Final[float] = 30.0

#: Depth values Gate.io accepts for order book snapshots.
ORDER_BOOK_SNAPSHOT_LIMITS: Final[tuple[int, ...]] = (1, 5, 10, 20, 50, 100)
