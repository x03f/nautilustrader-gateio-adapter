"""Constants for the Gate.io adapter: venue identifiers, endpoints, and interval maps."""

from typing import Final

from nautilus_trader.model.identifiers import ClientId, Venue

GATEIO: Final[str] = "GATEIO"
GATEIO_VENUE: Final[Venue] = Venue(GATEIO)
GATEIO_CLIENT_ID: Final[ClientId] = ClientId(GATEIO)

# REST endpoints (Gate.io API v4).
GATEIO_HTTP_MAINNET: Final[str] = "https://api.gateio.ws"
# Gate.io testnet host. Futures testnet is fully public; spot endpoints on this host
# are used for authenticated spot *testnet* accounts. There is NO public spot testnet
# market-data feed — public market data always comes from mainnet.
GATEIO_HTTP_TESTNET: Final[str] = "https://api-testnet.gateapi.io"
GATEIO_API_PREFIX: Final[str] = "/api/v4"

# WebSocket endpoints (spot).
GATEIO_WS_MAINNET: Final[str] = "wss://api.gateio.ws/ws/v4/"

# Candlestick interval maps.
# Nautilus BarType aggregation string -> Gate.io interval.
NAUTILUS_TO_GATEIO_INTERVAL: Final[dict[str, str]] = {
    "1-MINUTE": "1m",
    "5-MINUTE": "5m",
    "15-MINUTE": "15m",
    "30-MINUTE": "30m",
    "1-HOUR": "1h",
    "4-HOUR": "4h",
    "8-HOUR": "8h",
    "1-DAY": "1d",
}

# Gate.io interval -> milliseconds (used for gap detection).
GATEIO_INTERVAL_MS: Final[dict[str, int]] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "1d": 86_400_000,
}

# Client order id constraints for the Gate.io ``text`` field:
# must start with "t-", total length <= 28, charset [0-9A-Za-z_.-].
CLIENT_ORDER_ID_PREFIX: Final[str] = "t-"
CLIENT_ORDER_ID_MAX_LEN: Final[int] = 28
DEFAULT_CLIENT_ORDER_ID_TAG: Final[str] = "ng"
