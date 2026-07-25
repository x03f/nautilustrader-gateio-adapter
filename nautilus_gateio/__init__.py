"""Gate.io adapter for NautilusTrader (community-maintained, unofficial).

Provides Gate.io spot market data and execution for NautilusTrader live
trading nodes, plus reusable REST/WebSocket clients, a local paper-fill
simulator and experimental futures clients.
"""

from nautilus_gateio.config import (
    GateioDataClientConfig,
    GateioExecClientConfig,
    GateioPaperConfig,
    resolve_credentials,
)
from nautilus_gateio.constants import (
    GATEIO,
    GATEIO_CLIENT_ID,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
    GATEIO_VENUE,
    GATEIO_WS_MAINNET,
)
from nautilus_gateio.data import GateioDataClient
from nautilus_gateio.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
    LiveOrdersDisabledError,
    OrderValidationError,
    should_retry,
)
from nautilus_gateio.execution import GateioExecutionClient
from nautilus_gateio.factories import (
    GateioLiveDataClientFactory,
    GateioLiveExecClientFactory,
)
from nautilus_gateio.http import GateioHttpClient, RateLimiter
from nautilus_gateio.paper import PaperExecution, PaperFill
from nautilus_gateio.providers import (
    GateioInstrumentProvider,
    StaticInstrumentProvider,
    build_currency_pair,
)
from nautilus_gateio.reconcile import reconcile
from nautilus_gateio.signing import (
    generate_client_order_id,
    sanitize_client_order_id,
    sign_request,
)
from nautilus_gateio.symbols import (
    gate_pair_to_instrument_id,
    instrument_id_to_gate_pair,
)
from nautilus_gateio.websocket import GateioWebSocketClient

__version__ = "0.1.0"

__all__ = [
    "GATEIO",
    "GATEIO_CLIENT_ID",
    "GATEIO_HTTP_MAINNET",
    "GATEIO_HTTP_TESTNET",
    "GATEIO_VENUE",
    "GATEIO_WS_MAINNET",
    "GateioClientError",
    "GateioDataClient",
    "GateioDataClientConfig",
    "GateioError",
    "GateioExecClientConfig",
    "GateioExecutionClient",
    "GateioHttpClient",
    "GateioInstrumentProvider",
    "GateioLiveDataClientFactory",
    "GateioLiveExecClientFactory",
    "GateioPaperConfig",
    "GateioServerError",
    "GateioWebSocketClient",
    "LiveOrdersDisabledError",
    "OrderValidationError",
    "PaperExecution",
    "PaperFill",
    "RateLimiter",
    "StaticInstrumentProvider",
    "build_currency_pair",
    "gate_pair_to_instrument_id",
    "generate_client_order_id",
    "instrument_id_to_gate_pair",
    "reconcile",
    "resolve_credentials",
    "sanitize_client_order_id",
    "should_retry",
    "sign_request",
    "__version__",
]
