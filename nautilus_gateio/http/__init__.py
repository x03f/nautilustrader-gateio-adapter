"""Gate.io REST layer: one shared transport plus a typed namespace per product.

:class:`GateioHttpClient` owns signing, pacing, retries and error translation.
The namespace classes are thin, composable wrappers over it — each takes the
client in its constructor and returns decoded payloads unchanged, leaving
translation into NautilusTrader objects to the layers above.

Perpetual and delivery futures share :class:`GateioFuturesHttpAPI`, selected by
its ``settle`` and ``delivery`` constructor arguments::

    client = GateioHttpClient(api_key, api_secret)
    spot = GateioSpotHttpAPI(client)
    perp = GateioFuturesHttpAPI(client, settle="usdt")
    inverse = GateioFuturesHttpAPI(client, settle="btc")
    dated = GateioFuturesHttpAPI(client, settle="usdt", delivery=True)
    options = GateioOptionsHttpAPI(client)
    margin = GateioMarginHttpAPI(client)
    wallet = GateioWalletHttpAPI(client)
"""

from __future__ import annotations

from nautilus_gateio.http.client import GateioHttpClient
from nautilus_gateio.http.futures import GateioFuturesHttpAPI
from nautilus_gateio.http.margin import GateioMarginHttpAPI, require_wallet
from nautilus_gateio.http.options import GateioOptionsHttpAPI
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.http.wallet import ALLOWED_TRANSFER_ACCOUNTS, GateioWalletHttpAPI

__all__ = [
    "ALLOWED_TRANSFER_ACCOUNTS",
    "GateioFuturesHttpAPI",
    "GateioHttpClient",
    "GateioMarginHttpAPI",
    "GateioOptionsHttpAPI",
    "GateioSpotHttpAPI",
    "GateioWalletHttpAPI",
    "require_wallet",
]
