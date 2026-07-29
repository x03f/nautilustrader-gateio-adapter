"""WebSocket transport and channel helpers for the Gate.io adapter.

* :class:`GateioWebSocketClient` — one resilient connection to one endpoint.
* :class:`GateioPublicWebSocket` — public market data channels for one product.
* :class:`GateioPrivateWebSocket` — authenticated order, fill, balance and
  position channels for one product.
"""

from __future__ import annotations

from nautilus_gateio.websocket.client import GateioWebSocketClient
from nautilus_gateio.websocket.private import GateioPrivateWebSocket
from nautilus_gateio.websocket.public import GateioPublicWebSocket

__all__ = [
    "GateioPrivateWebSocket",
    "GateioPublicWebSocket",
    "GateioWebSocketClient",
]
