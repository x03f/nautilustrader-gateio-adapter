"""Gate.io USDT-margined perpetual futures — EXPERIMENTAL.

Status: raw REST clients only. This module is **not** integrated with the
Nautilus live clients (no futures instrument provider, data client or
execution client yet). It is published because the public-data half is useful
on its own and the private half demonstrates the safety model.

Safety model for the private client:

* the private client refuses to operate on any host except the Gate.io
  futures **testnet** (``PermissionError`` otherwise) — mainnet futures
  trading is deliberately out of scope for this experimental module;
* order-mutating calls additionally require ``live_orders=True``;
* a distinct client-order-id tag separates testnet futures orders from any
  other order flow.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from nautilus_gateio.constants import GATEIO_API_PREFIX, GATEIO_HTTP_TESTNET
from nautilus_gateio.schemas import parse_futures_contract
from nautilus_gateio.signing import sign_request

FUTURES_TESTNET_HOSTS = frozenset({"api-testnet.gateapi.io"})
FUTURES_CLIENT_ORDER_ID_TAG = "t-tn"


def assert_testnet(base_url: str) -> None:
    """Raise ``PermissionError`` unless ``base_url`` is a known futures testnet host."""
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    if host not in FUTURES_TESTNET_HOSTS:
        raise PermissionError(
            f"the experimental futures private client only operates on Gate.io "
            f"testnet hosts, got: {host}"
        )


class GateioFuturesPublicClient:
    """Public USDT-perpetual futures data (no credentials). Defaults to testnet."""

    def __init__(self, base_url: str = GATEIO_HTTP_TESTNET, timeout: float = 15.0) -> None:
        self.base_url = base_url
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, headers={"Accept": "application/json"}
        )

    def close(self) -> None:
        self._client.close()

    def availability(self) -> dict[str, Any]:
        """Probe public endpoints with real requests and report availability."""
        checks = {
            "contracts": "/futures/usdt/contracts?limit=1",
            "tickers": "/futures/usdt/tickers?contract=BTC_USDT",
            "order_book": "/futures/usdt/order_book?contract=BTC_USDT&limit=1",
            "candlesticks": "/futures/usdt/candlesticks?contract=BTC_USDT&interval=1h&limit=1",
            "funding_rate": "/futures/usdt/funding_rate?contract=BTC_USDT&limit=1",
        }
        results: dict[str, Any] = {}
        for name, path in checks.items():
            try:
                response = self._client.get(GATEIO_API_PREFIX + path)
                results[name] = {"status": response.status_code, "ok": response.status_code == 200}
            except Exception as exc:
                results[name] = {"status": 0, "ok": False, "error": str(exc)[:80]}
        return {
            "host": self.base_url,
            "endpoints": results,
            "public_ready": all(entry["ok"] for entry in results.values()),
        }

    def contract(self, name: str = "BTC_USDT") -> dict[str, Any] | None:
        """Contract specification for one USDT-perp (tick size, leverage, funding)."""
        response = self._client.get(f"{GATEIO_API_PREFIX}/futures/usdt/contracts")
        response.raise_for_status()
        for raw in response.json():
            if raw.get("name") == name:
                return parse_futures_contract(raw)
        return None


class GateioFuturesPrivateClient:
    """Private USDT-perpetual futures REST — TESTNET ONLY (see module docs)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = GATEIO_HTTP_TESTNET,
        live_orders: bool = False,
    ) -> None:
        assert_testnet(base_url)
        if not (api_key and api_secret):
            raise ValueError("Gate.io testnet API credentials required")
        self._api_key = api_key
        self._api_secret = api_secret
        self.base_url = base_url
        self.live_orders = bool(live_orders)
        self._client = httpx.Client(base_url=base_url, timeout=20.0)

    def close(self) -> None:
        self._client.close()

    def _require_live_orders(self) -> None:
        if not self.live_orders:
            raise PermissionError(
                "futures testnet orders are disabled: pass live_orders=True explicitly"
            )

    def _private(self, method: str, path: str, query: str = "", body: str = "") -> Any:
        headers = sign_request(
            method, GATEIO_API_PREFIX + path, query, body, self._api_key, self._api_secret
        )
        url = GATEIO_API_PREFIX + path + (f"?{query}" if query else "")
        response = self._client.request(method, url, headers=headers, content=body or None)
        response.raise_for_status()
        return response.json()

    # -- read-only ---------------------------------------------------------

    def accounts(self) -> Any:
        return self._private("GET", "/futures/usdt/accounts")

    def positions(self) -> Any:
        return self._private("GET", "/futures/usdt/positions")

    def position(self, contract: str) -> Any:
        return self._private("GET", f"/futures/usdt/positions/{contract}")

    def list_orders(self, status: str = "open", contract: str | None = None) -> Any:
        query = f"status={status}" + (f"&contract={contract}" if contract else "")
        return self._private("GET", "/futures/usdt/orders", query=query)

    def get_order(self, order_id: str) -> Any:
        return self._private("GET", f"/futures/usdt/orders/{order_id}")

    def my_trades(self, contract: str, limit: int = 20) -> Any:
        return self._private(
            "GET", "/futures/usdt/my_trades", query=f"contract={contract}&limit={limit}"
        )

    # -- mutating (requires live_orders=True, testnet host enforced) ---------

    def place_order(
        self,
        contract: str,
        size: int,
        price: str | float | None = None,
        tif: str = "gtc",
        reduce_only: bool = False,
        client_id: str | None = None,
    ) -> Any:
        self._require_live_orders()
        payload: dict[str, Any] = {
            "contract": contract,
            "size": int(size),
            "tif": tif,
            "reduce_only": reduce_only,
            "text": client_id or f"{FUTURES_CLIENT_ORDER_ID_TAG}-{time.time_ns() % 10**10}",
        }
        if price is not None:
            payload["price"] = str(price)
        return self._private("POST", "/futures/usdt/orders", body=json.dumps(payload))

    def set_leverage(self, contract: str, leverage: int) -> Any:
        self._require_live_orders()
        return self._private(
            "POST",
            f"/futures/usdt/positions/{contract}/leverage",
            query=f"leverage={int(leverage)}",
        )

    def cancel_order(self, order_id: str) -> Any:
        self._require_live_orders()
        return self._private("DELETE", f"/futures/usdt/orders/{order_id}")

    def cancel_all(self, contract: str) -> Any:
        self._require_live_orders()
        return self._private("DELETE", "/futures/usdt/orders", query=f"contract={contract}")
