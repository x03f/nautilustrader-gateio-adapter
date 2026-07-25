"""Gate.io adapter for NautilusTrader (community-maintained, unofficial).

Connects NautilusTrader to Gate.io spot, margin, perpetual futures (linear and
inverse), delivery futures and options: real market data over WebSocket and
REST, order execution, position and balance tracking, and start-up
reconciliation.

Quick start
-----------
.. code-block:: python

    from nautilus_trader.live.node import TradingNode
    from nautilus_gateio import (
        GATEIO,
        GateioDataClientConfig,
        GateioExecClientConfig,
        GateioLiveDataClientFactory,
        GateioLiveExecClientFactory,
        GateioProductType,
    )

    node.add_data_client_factory(GATEIO, GateioLiveDataClientFactory)
    node.add_exec_client_factory(GATEIO, GateioLiveExecClientFactory)

Instrument ids use the exact Gate.io symbol, with the conventional ``-PERP``
suffix on perpetual contracts (the only case where Gate.io reuses a symbol
across products): ``BTC_USDT.GATE_IO``, ``BTC_USDT-PERP.GATE_IO``,
``BTC_USDT_20260807.GATE_IO``, ``BTC_USDT-20260729-70000-C.GATE_IO``.
"""

from nautilus_gateio.books import GateioOrderBook, OrderBookSequenceError
from nautilus_gateio.common.constants import (
    GATEIO,
    GATEIO_CLIENT_ID,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
    GATEIO_VENUE,
    GATEIO_WS_OPTIONS,
    GATEIO_WS_SPOT,
)
from nautilus_gateio.common.credentials import resolve_credentials
from nautilus_gateio.common.enums import (
    GateioProductType,
    GateioSpotAccountMode,
    GateioTimeInForce,
)
from nautilus_gateio.common.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
    OrderValidationError,
    UnsupportedOrderError,
    WalletNotProvisionedError,
    should_retry,
)
from nautilus_gateio.common.signing import (
    generate_client_order_id,
    sanitize_client_order_id,
    sign_request,
    sign_ws_request,
)
from nautilus_gateio.common.symbols import (
    gateio_to_instrument_id,
    instrument_id_to_gateio,
    parse_delivery_symbol,
    parse_option_symbol,
    product_of,
    raw_symbol_of,
)
from nautilus_gateio.config import GateioDataClientConfig, GateioExecClientConfig
from nautilus_gateio.data import GateioDataClient
from nautilus_gateio.execution import GateioExecutionClient
from nautilus_gateio.factories import (
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
)
from nautilus_gateio.http import (
    GateioFuturesHttpAPI,
    GateioHttpClient,
    GateioMarginHttpAPI,
    GateioOptionsHttpAPI,
    GateioSpotHttpAPI,
    GateioWalletHttpAPI,
)
from nautilus_gateio.instruments import (
    parse_delivery_instrument,
    parse_instrument,
    parse_option_instrument,
    parse_perpetual_instrument,
    parse_spot_instrument,
)
from nautilus_gateio.providers import GateioInstrumentProvider
from nautilus_gateio.websocket import (
    GateioPrivateWebSocket,
    GateioPublicWebSocket,
    GateioWebSocketClient,
)

__version__ = "0.2.0"

__all__ = [
    "GATEIO",
    "GATEIO_CLIENT_ID",
    "GATEIO_HTTP_MAINNET",
    "GATEIO_HTTP_TESTNET",
    "GATEIO_VENUE",
    "GATEIO_WS_OPTIONS",
    "GATEIO_WS_SPOT",
    "GateioClientError",
    "GateioDataClient",
    "GateioDataClientConfig",
    "GateioError",
    "GateioExecClientConfig",
    "GateioExecutionClient",
    "GateioFuturesHttpAPI",
    "GateioHttpClient",
    "GateioInstrumentProvider",
    "GateioLiveDataClientFactory",
    "GateioLiveExecClientFactory",
    "GateioMarginHttpAPI",
    "GateioOptionsHttpAPI",
    "GateioOrderBook",
    "GateioPrivateWebSocket",
    "GateioProductType",
    "GateioPublicWebSocket",
    "GateioServerError",
    "GateioSpotAccountMode",
    "GateioSpotHttpAPI",
    "GateioTimeInForce",
    "GateioWalletHttpAPI",
    "GateioWebSocketClient",
    "OrderBookSequenceError",
    "OrderValidationError",
    "UnsupportedOrderError",
    "WalletNotProvisionedError",
    "__version__",
    "gateio_to_instrument_id",
    "generate_client_order_id",
    "instrument_id_to_gateio",
    "parse_delivery_instrument",
    "parse_delivery_symbol",
    "parse_instrument",
    "parse_option_instrument",
    "parse_option_symbol",
    "parse_perpetual_instrument",
    "parse_spot_instrument",
    "product_of",
    "raw_symbol_of",
    "resolve_credentials",
    "sanitize_client_order_id",
    "should_retry",
    "sign_request",
    "sign_ws_request",
]
