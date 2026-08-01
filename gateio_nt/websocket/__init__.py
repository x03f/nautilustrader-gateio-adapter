"""WebSocket transport and channel helpers for the Gate.io adapter.

* :class:`GateioWebSocketClient` — one resilient connection to one endpoint.
* :class:`GateioPublicWebSocket` — public market data channels for one product.
* :class:`GateioPrivateWebSocket` — authenticated order, fill, balance and
  position channels for one product.
"""

from __future__ import annotations

from gateio_nt.websocket.client import GateioWebSocketClient
from gateio_nt.websocket.private import GateioPrivateWebSocket
from gateio_nt.websocket.public import GateioPublicWebSocket

__all__ = [
    "GateioPrivateWebSocket",
    "GateioPublicWebSocket",
    "GateioWebSocketClient",
]
