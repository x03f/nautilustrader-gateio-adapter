"""Gate.io API v4 HTTP client: public market data and private spot endpoints.

Design notes
------------
* Public endpoints need no credentials; private endpoints are signed with
  HMAC-SHA512 (see :mod:`nautilus_gateio.signing`).
* Order-mutating calls (place/cancel) additionally require the explicit
  ``live_orders=True`` switch. Credentials alone never enable order flow —
  this is a deliberate, layered safety model.
* A simple rate limiter paces requests and backs off exponentially on
  HTTP 429; errors are translated into typed exceptions
  (:class:`~nautilus_gateio.errors.GateioError` hierarchy) that never
  contain credentials or signatures.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from nautilus_gateio.constants import GATEIO_API_PREFIX, GATEIO_HTTP_MAINNET
from nautilus_gateio.errors import (
    GateioError,
    LiveOrdersDisabledError,
    error_from_response,
)
from nautilus_gateio.schemas import (
    parse_balances,
    parse_candle,
    parse_currency_pair,
    parse_fill,
    parse_order,
    parse_order_book,
    parse_public_trade,
    validate_order,
)
from nautilus_gateio.signing import generate_client_order_id, sign_request


class RateLimiter:
    """Minimum-interval pacing with exponential backoff on HTTP 429."""

    def __init__(self, max_per_sec: float = 8.0) -> None:
        self.min_interval = 1.0 / max_per_sec
        self._last = 0.0
        self._backoff = 0.0

    def wait(self) -> None:
        now = time.time()
        delay = max(0.0, self.min_interval - (now - self._last)) + self._backoff
        if delay > 0:
            time.sleep(delay)
        self._last = time.time()

    def on_429(self) -> None:
        self._backoff = min(self._backoff * 2 + 0.5, 10.0)

    def on_ok(self) -> None:
        self._backoff = self._backoff * 0.5
        if self._backoff < 1e-3:
            self._backoff = 0.0


class GateioHttpClient:
    """Synchronous Gate.io API v4 client (spot).

    Parameters
    ----------
    api_key, api_secret : str, optional
        Credentials for private endpoints. Public market data works without them.
    live_orders : bool
        Hard switch for order-mutating endpoints. Defaults to ``False``:
        ``place_order``/``cancel_order`` raise ``LiveOrdersDisabledError``
        until explicitly enabled.
    base_url : str
        REST host; defaults to mainnet. Use ``GATEIO_HTTP_TESTNET`` for a
        Gate.io testnet account.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        live_orders: bool = False,
        base_url: str = GATEIO_HTTP_MAINNET,
        max_per_sec: float = 8.0,
        timeout: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self.live_orders = bool(live_orders)
        self.base_url = base_url
        self._limiter = RateLimiter(max_per_sec)
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, headers={"Accept": "application/json"}
        )
        self._time_offset_ms = 0

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GateioHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    # -- time --------------------------------------------------------------

    def server_time_ms(self) -> int:
        response = self._client.get(f"{GATEIO_API_PREFIX}/spot/time")
        response.raise_for_status()
        return int(response.json()["server_time"])

    def sync_time(self) -> int:
        """Synchronize the local clock offset with the exchange.

        Returns the measured offset in milliseconds; subsequent signed
        requests use it when computing the signature timestamp.
        """
        self._time_offset_ms = self.server_time_ms() - int(time.time() * 1000)
        return self._time_offset_ms

    def _signed_timestamp(self) -> str:
        return str(int((time.time() * 1000 + self._time_offset_ms) / 1000))

    # -- public market data ------------------------------------------------

    def ping(self) -> dict[str, Any]:
        started = time.time()
        server_ms = self.server_time_ms()
        return {
            "ok": True,
            "server_time_ms": server_ms,
            "latency_ms": round((time.time() - started) * 1000),
        }

    def currency_pairs(self) -> list[dict[str, Any]]:
        """All spot pairs with trading constraints (precision, minimums, status)."""
        response = self._client.get(f"{GATEIO_API_PREFIX}/spot/currency_pairs")
        response.raise_for_status()
        return [parse_currency_pair(p) for p in response.json()]

    def currency_pair(self, pair: str) -> dict[str, Any]:
        """Trading constraints for a single pair (e.g. ``BTC_USDT``)."""
        response = self._client.get(f"{GATEIO_API_PREFIX}/spot/currency_pairs/{pair.upper()}")
        response.raise_for_status()
        return parse_currency_pair(response.json())

    def candles(
        self,
        pair: str,
        interval: str = "1h",
        limit: int = 500,
        start: int | None = None,
        end: int | None = None,
    ) -> list[dict[str, Any]]:
        """OHLCV candles, oldest first. ``start``/``end`` are Unix seconds."""
        params: dict[str, Any] = {
            "currency_pair": pair.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start:
            params["from"] = int(start)
        if end:
            params["to"] = int(end)
        response = self._client.get(f"{GATEIO_API_PREFIX}/spot/candlesticks", params=params)
        response.raise_for_status()
        candles = [parse_candle(row) for row in response.json()]
        candles.sort(key=lambda c: c["ts"])
        return candles

    def trades(self, pair: str, limit: int = 100) -> list[dict[str, Any]]:
        response = self._client.get(
            f"{GATEIO_API_PREFIX}/spot/trades",
            params={"currency_pair": pair.upper(), "limit": limit},
        )
        response.raise_for_status()
        return [parse_public_trade(t) for t in response.json()]

    def order_book(self, pair: str, limit: int = 20) -> dict[str, Any]:
        response = self._client.get(
            f"{GATEIO_API_PREFIX}/spot/order_book",
            params={"currency_pair": pair.upper(), "limit": limit},
        )
        response.raise_for_status()
        return parse_order_book(response.json())

    def ticker_last(self, pair: str) -> float:
        response = self._client.get(
            f"{GATEIO_API_PREFIX}/spot/tickers", params={"currency_pair": pair.upper()}
        )
        response.raise_for_status()
        return float(response.json()[0]["last"])

    # -- private core ------------------------------------------------------

    def _require_credentials(self) -> None:
        if not self.has_credentials:
            raise ValueError(
                "Gate.io API credentials required (set GATE_API_KEY / GATE_API_SECRET)"
            )

    def _require_live_orders(self) -> None:
        self._require_credentials()
        if not self.live_orders:
            raise LiveOrdersDisabledError(
                "live orders are disabled: construct the client with live_orders=True "
                "to allow order-mutating calls (credentials alone are not sufficient)"
            )

    def _private(
        self,
        method: str,
        path: str,
        query: str = "",
        body: str = "",
        retries: int = 3,
    ) -> Any:
        self._require_credentials()
        url_path = GATEIO_API_PREFIX + path
        for _ in range(retries):
            self._limiter.wait()
            headers = sign_request(
                method,
                url_path,
                query,
                body,
                self._api_key,
                self._api_secret,
                self._signed_timestamp(),
            )
            url = url_path + (f"?{query}" if query else "")
            response = self._client.request(method, url, headers=headers, content=body or None)
            if response.status_code == 429:
                self._limiter.on_429()
                continue
            self._limiter.on_ok()
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except Exception:
                    payload = {"label": "HTTP", "message": response.text[:200]}
                raise error_from_response(
                    response.status_code,
                    payload.get("label", ""),
                    payload.get("message", ""),
                )
            return response.json()
        raise GateioError(429, "TOO_MANY_REQUESTS", "rate-limit retries exhausted")

    # -- private account state (read-only) ---------------------------------

    def balances(self) -> dict[str, dict[str, float]]:
        return parse_balances(self._private("GET", "/spot/accounts"))

    def open_orders(self, pair: str | None = None) -> list[dict[str, Any]]:
        query = f"currency_pair={pair}" if pair else ""
        data = self._private("GET", "/spot/open_orders", query=query)
        orders: list[dict[str, Any]] = []
        for group in data if isinstance(data, list) else []:
            # The endpoint may return grouped entries ({currency_pair, orders: [...]})
            # or plain order dicts — handle both shapes.
            if isinstance(group, dict) and "orders" in group:
                entries = group["orders"]
            else:
                entries = [group]
            orders.extend(parse_order(o) for o in entries if isinstance(o, dict))
        return orders

    def order(self, order_id: str, pair: str) -> dict[str, Any]:
        raw = self._private("GET", f"/spot/orders/{order_id}", query=f"currency_pair={pair}")
        return parse_order(raw)

    def my_trades(self, pair: str, limit: int = 100) -> list[dict[str, Any]]:
        data = self._private("GET", "/spot/my_trades", query=f"currency_pair={pair}&limit={limit}")
        return [parse_fill(t) for t in data]

    def find_by_client_id(self, pair: str, client_id: str) -> dict[str, Any] | None:
        """Idempotency helper: find an open order by client id before re-submitting."""
        for order in self.open_orders(pair):
            if order.get("client_id") == client_id:
                return order
        return None

    # -- private order flow (requires live_orders=True) ---------------------

    def place_order(
        self,
        pair: str,
        side: str,
        amount: str | float,
        price: str | float | None = None,
        order_type: str = "limit",
        client_id: str | None = None,
        time_in_force: str | None = None,
    ) -> dict[str, Any]:
        """Submit a spot order.

        Note: Gate.io spot market-buy orders interpret ``amount`` as QUOTE
        currency; this method passes parameters through verbatim. The Nautilus
        execution client avoids the quirk by emulating market orders with
        aggressive IOC limit orders (see :mod:`nautilus_gateio.execution`).
        """
        self._require_live_orders()
        client_id = client_id or generate_client_order_id()
        tif = time_in_force or ("ioc" if order_type == "market" else "gtc")
        payload: dict[str, Any] = {
            "currency_pair": pair,
            "side": side,
            "amount": str(amount),
            "type": order_type,
            "text": client_id,
            "time_in_force": tif,
        }
        if order_type == "limit" and price is not None:
            payload["price"] = str(price)
        raw = self._private("POST", "/spot/orders", body=json.dumps(payload))
        return parse_order(raw)

    def place_order_validated(
        self,
        pair: str,
        side: str,
        amount: float,
        price: float | None = None,
        order_type: str = "limit",
        client_id: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validating, idempotent wrapper over :meth:`place_order`.

        Rounds and checks the order against instrument constraints, and if an
        open order with the same client id already exists returns it instead
        of creating a duplicate.
        """
        self._require_live_orders()
        client_id = client_id or generate_client_order_id()
        spec = spec or self.currency_pair(pair)
        amount, price = validate_order(spec, amount, price if order_type == "limit" else None)
        existing = self.find_by_client_id(pair, client_id)
        if existing:
            return {**existing, "idempotent": True}
        return self.place_order(pair, side, amount, price, order_type, client_id)

    def cancel_order(self, order_id: str, pair: str) -> dict[str, Any]:
        self._require_live_orders()
        raw = self._private("DELETE", f"/spot/orders/{order_id}", query=f"currency_pair={pair}")
        return parse_order(raw)

    def cancel_all(self, pair: str | None = None) -> list[dict[str, Any]]:
        """Cancel all open orders (optionally for one pair)."""
        self._require_live_orders()
        results: list[dict[str, Any]] = []
        for order in self.open_orders(pair):
            try:
                results.append(self.cancel_order(order["id"], order["pair"]))
            except Exception as exc:  # keep cancelling the rest
                results.append({"id": order["id"], "error": str(exc)[:80]})
        return results

    def emergency_stop(self, pair: str | None = None) -> dict[str, Any]:
        """Cancel all open orders and hard-disable live order flow.

        After this call any mutating request raises ``LiveOrdersDisabledError``
        until a new client is constructed with ``live_orders=True``.
        """
        result: dict[str, Any] = {"cancelled": [], "live_orders_disabled": True}
        try:
            if self.live_orders:
                result["cancelled"] = self.cancel_all(pair)
        finally:
            self.live_orders = False
        return result
