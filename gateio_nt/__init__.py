"""Gate.io adapter for NautilusTrader (community-maintained, unofficial).

Connects NautilusTrader to Gate.io spot, margin, perpetual futures (linear and
inverse), delivery futures and options: real market data over WebSocket and
REST, order execution, position and balance tracking, and start-up
reconciliation.

Quick start
-----------
.. code-block:: python

    from nautilus_trader.live.node import TradingNode
    from gateio_nt import (
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

Venue-native data
-----------------
:class:`~gateio_nt.types.GateioTicker` carries the ticker fields
NautilusTrader has no type of its own for: 24-hour statistics, implied
volatilities, the greeks and the delivery basis. Subscribe to it with the
metadata form, which is the topic the published rows are addressed to:

.. code-block:: python

    from nautilus_trader.model.data import DataType
    from gateio_nt import GATEIO_CLIENT_ID, GateioTicker

    self.subscribe_data(
        DataType(GateioTicker, metadata={"instrument_id": instrument_id}),
        client_id=GATEIO_CLIENT_ID,
    )

Importing this package registers ``GateioTicker`` with the platform's msgpack
and Arrow serializers, so it can be persisted and sent over an external message
bus. The registration is not called here, unlike the in-tree adapters that call
``register_serializable_type`` in their own ``__init__``: their types are plain
``Data`` subclasses, while ``GateioTicker`` is built with ``@customdataclass``,
which performs both registrations itself at class definition
(``model/custom.py:159-160``). Registering a type twice raises ``KeyError``, so
repeating the in-tree call here would stop the package importing at all.
"""

from gateio_nt.books import GateioOrderBook, OrderBookSequenceError
from gateio_nt.common.constants import (
    GATEIO,
    GATEIO_CLIENT_ID,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
    GATEIO_VENUE,
    GATEIO_WS_OPTIONS,
    GATEIO_WS_SPOT,
)
from gateio_nt.common.credentials import resolve_credentials
from gateio_nt.common.enums import (
    GateioProductType,
    GateioSpotAccountMode,
    GateioTimeInForce,
)
from gateio_nt.common.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
    OrderValidationError,
    UnsupportedOrderError,
    WalletNotProvisionedError,
    WalletQueryRefusedError,
    should_retry,
)
from gateio_nt.common.signing import (
    generate_client_order_id,
    sanitize_client_order_id,
    sign_request,
    sign_ws_request,
)
from gateio_nt.common.symbols import (
    gateio_to_instrument_id,
    instrument_id_to_gateio,
    parse_delivery_symbol,
    parse_option_symbol,
    product_of,
    raw_symbol_of,
)
from gateio_nt.config import GateioDataClientConfig, GateioExecClientConfig
from gateio_nt.data import GateioDataClient
from gateio_nt.execution import GateioExecutionClient
from gateio_nt.factories import (
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
)
from gateio_nt.http import (
    GateioFuturesHttpAPI,
    GateioHttpClient,
    GateioMarginHttpAPI,
    GateioOptionsHttpAPI,
    GateioSpotHttpAPI,
    GateioWalletHttpAPI,
)
from gateio_nt.instruments import (
    parse_delivery_instrument,
    parse_instrument,
    parse_option_instrument,
    parse_perpetual_instrument,
    parse_spot_instrument,
)
from gateio_nt.providers import GateioInstrumentProvider
from gateio_nt.types import TICKER_FIELDS, GateioTicker
from gateio_nt.websocket import (
    GateioPrivateWebSocket,
    GateioPublicWebSocket,
    GateioWebSocketClient,
)

__version__ = "0.2.0a2.dev0"

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
    "GateioTicker",
    "GateioTimeInForce",
    "GateioWalletHttpAPI",
    "GateioWebSocketClient",
    "OrderBookSequenceError",
    "OrderValidationError",
    "TICKER_FIELDS",
    "UnsupportedOrderError",
    "WalletNotProvisionedError",
    "WalletQueryRefusedError",
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
