"""Typed REST namespace for the Gate.io spot API (``/api/v4/spot/*``).

Every method returns the decoded JSON payload exactly as Gate.io sent it;
translating payloads into NautilusTrader objects is the job of the parsing and
client layers. All methods are asynchronous and share the transport passed to
:class:`GateioSpotHttpAPI`.

Spot margin is not a separate REST namespace on Gate.io: isolated margin, cross
margin and unified-account trading are all driven through these same endpoints
by setting the ``account`` field/parameter (``spot``, ``margin``,
``cross_margin``, ``unified``). The balance, borrow and repay endpoints for
those ledgers live in :mod:`nautilus_gateio.http.margin`.

Two naming asymmetries in Gate.io's own API are preserved verbatim rather than
smoothed over, because the venue validates them:

* regular orders use ``account="spot"`` while price-triggered orders use
  ``put.account="normal"`` for the same concept;
* ``from``/``to`` query parameters are exposed here as ``frm``/``to`` because
  ``from`` is a Python keyword.
"""

from __future__ import annotations

from typing import Any

from nautilus_gateio.http.client import GateioHttpClient


class GateioSpotHttpAPI:
    """Spot (and spot-margin) REST endpoints.

    Parameters
    ----------
    client : GateioHttpClient
        Shared transport handling signing, pacing and error translation.
    """

    def __init__(self, client: GateioHttpClient) -> None:
        self._client = client

    @property
    def client(self) -> GateioHttpClient:
        return self._client

    # -- public market data ------------------------------------------------

    async def server_time(self) -> dict[str, Any]:
        """``GET /spot/time`` — venue clock as ``{"server_time": <unix ms>}``."""
        return await self._client.get("/spot/time")

    async def currency_pairs(self) -> list[dict[str, Any]]:
        """``GET /spot/currency_pairs`` — every listed spot pair.

        Key fields per pair: ``id``, ``base``, ``quote``, ``precision`` (price
        scale), ``amount_precision`` (base quantity scale), ``min_base_amount``,
        ``min_quote_amount``, ``trade_status`` and ``slippage``.
        """
        return await self._client.get("/spot/currency_pairs")

    async def currency_pair(self, pair: str) -> dict[str, Any]:
        """``GET /spot/currency_pairs/{pair}`` — a single pair definition."""
        return await self._client.get(f"/spot/currency_pairs/{pair}")

    async def currencies(self) -> list[dict[str, Any]]:
        """``GET /spot/currencies`` — per-currency chains and trading flags.

        Fields are ``currency``, ``name``, ``category``, ``chain``, ``chains``,
        ``delisted``, ``trade_disabled``, ``deposit_disabled``,
        ``withdraw_disabled``, ``withdraw_delayed``, ``fixed_rate``,
        ``market_cap`` and ``total_supply``.

        There is no decimal-precision field here. Every quantity and price scale
        comes from the currency *pair* (``precision`` and ``amount_precision``),
        and Gate.io publishes no scale at all for the quote-denominated amount of
        a spot market buy — only the ``min_quote_amount`` floor.
        """
        return await self._client.get("/spot/currencies")

    async def currency(self, ccy: str) -> dict[str, Any]:
        """``GET /spot/currencies/{ccy}`` — a single currency definition."""
        return await self._client.get(f"/spot/currencies/{ccy}")

    async def candlesticks(
        self,
        pair: str,
        interval: str = "1m",
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[list[str]]:
        """``GET /spot/candlesticks`` — OHLCV bars, oldest first.

        Each element is a positional array
        ``[timestamp_s, quote_volume, close, high, low, open, base_volume, closed]``.
        Note the unusual ordering: the *close* precedes high/low/open, the second
        element is turnover in the **quote** currency and the seventh is volume in
        the **base** currency. ``frm``/``to`` are Unix seconds.
        """
        params: dict[str, Any] = {
            "currency_pair": pair,
            "interval": interval,
            "limit": limit,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/spot/candlesticks", params=params)

    async def trades(
        self,
        pair: str,
        limit: int | None = None,
        last_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /spot/trades`` — recent public trades.

        The public form omits the private-only keys (``role``, ``order_id``,
        ``fee``, ``fee_currency``, ``point_fee``, ``gt_fee``, ``text``); do not
        assume they are present.
        """
        params: dict[str, Any] = {"currency_pair": pair, "limit": limit, "last_id": last_id}
        return await self._client.get("/spot/trades", params=params)

    async def order_book(
        self,
        pair: str,
        limit: int | None = None,
        with_id: bool = True,
        interval: str | None = None,
    ) -> dict[str, Any]:
        """``GET /spot/order_book`` — an order book snapshot.

        With ``with_id=True`` the response carries ``id``, the book sequence
        number required to align an incremental WebSocket stream against the
        snapshot. ``interval`` requests price aggregation (``"0"`` = none).
        Levels are ``[price, size]`` string pairs.
        """
        params: dict[str, Any] = {
            "currency_pair": pair,
            "interval": interval,
            "limit": limit,
            "with_id": with_id,
        }
        return await self._client.get("/spot/order_book", params=params)

    async def tickers(self, pair: str | None = None) -> list[dict[str, Any]]:
        """``GET /spot/tickers`` — 24h statistics and best bid/ask.

        Always returns a list, including when ``pair`` selects a single market.
        """
        return await self._client.get("/spot/tickers", params={"currency_pair": pair})

    async def fee(self, pair: str | None = None) -> dict[str, Any]:
        """``GET /spot/fee`` — the account's spot fee tier. **Requires credentials.**

        Despite living under the otherwise public ``/spot`` tree this endpoint is
        signed. It is also the cheapest source of ``user_id``, which the private
        futures and options WebSocket channels require in their subscription
        payloads.

        Gate.io marks this endpoint deprecated in favor of ``GET /wallet/fee``
        (see :meth:`nautilus_gateio.http.wallet.GateioWalletHttpAPI.fee`), which
        returns spot, perpetual and delivery rates in one call.
        """
        return await self._client.get("/spot/fee", params={"currency_pair": pair}, signed=True)

    # -- private: accounts -------------------------------------------------

    async def accounts(self, currency: str | None = None) -> list[dict[str, Any]]:
        """``GET /spot/accounts`` — spot balances as ``{currency, available, locked}``."""
        return await self._client.get("/spot/accounts", params={"currency": currency}, signed=True)

    async def account_book(
        self,
        currency: str | None = None,
        limit: int | None = None,
        page: int | None = None,
        frm: int | None = None,
        to: int | None = None,
        type: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /spot/account_book`` — the spot ledger of balance changes."""
        params: dict[str, Any] = {
            "currency": currency,
            "from": frm,
            "to": to,
            "page": page,
            "limit": limit,
            "type": type,
        }
        return await self._client.get("/spot/account_book", params=params, signed=True)

    # -- private: orders ---------------------------------------------------

    async def open_orders(
        self,
        page: int | None = None,
        limit: int | None = None,
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /spot/open_orders`` — all resting orders, grouped by pair.

        Returns ``[{currency_pair, total, orders: [...]}, ...]``. Pagination
        applies *within* each pair, not across pairs: every pair holding open
        orders is always returned. This is the one-call source for an
        unfiltered order status report.

        Without ``account`` the query spans the spot, unified and isolated
        margin ledgers.
        """
        params: dict[str, Any] = {"page": page, "limit": limit, "account": account}
        return await self._client.get("/spot/open_orders", params=params, signed=True)

    async def list_orders(
        self,
        pair: str,
        status: str,
        page: int | None = None,
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
        side: str | None = None,
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /spot/orders`` — orders for one pair by ``status``.

        ``status`` is required and is either ``open`` or ``finished``. Gate.io
        restricts the other parameters by status: with ``open`` only ``page`` and
        ``limit`` apply (``limit`` capped at 100) and ``currency_pair`` is
        mandatory; with ``finished`` the time range (``frm``/``to``, matched on
        the order's *end* time) and ``side`` filters become available.
        """
        params: dict[str, Any] = {
            "currency_pair": pair,
            "status": status,
            "page": page,
            "limit": limit,
            "account": account,
            "from": frm,
            "to": to,
            "side": side,
        }
        return await self._client.get("/spot/orders", params=params, signed=True)

    async def get_order(
        self,
        order_id: str,
        pair: str,
        account: str | None = None,
    ) -> dict[str, Any]:
        """``GET /spot/orders/{order_id}`` — a single order.

        ``order_id`` may be the venue order id or the client id carried in
        ``text``, but the client id only resolves while the order is still
        resting: once an order is filled or canceled Gate.io accepts the venue
        id alone. Persist the venue id on acceptance and use it for every
        post-terminal lookup.
        """
        params: dict[str, Any] = {"currency_pair": pair, "account": account}
        return await self._client.get(f"/spot/orders/{order_id}", params=params, signed=True)

    async def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /spot/orders`` — submit a spot, margin or unified-account order.

        Body fields: ``currency_pair``, ``side`` (``buy``/``sell``), ``amount``,
        ``type`` (``limit`` default, or ``market``), ``price`` (required for
        ``limit``), ``time_in_force`` (``gtc``/``ioc``/``poc``/``fok``),
        ``account``, ``text`` (client id, ``t-`` prefixed), ``iceberg``,
        ``auto_borrow``, ``auto_repay``, ``stp_act`` and ``slippage``.

        **Market-order amount semantics — the critical Gate.io quirk.**
        ``amount`` changes denomination with ``type`` and ``side``:

        =========  ======  ==================================================
        ``type``   side    ``amount`` is denominated in
        =========  ======  ==================================================
        ``limit``  buy     base currency (BTC in ``BTC_USDT``)
        ``limit``  sell    base currency
        ``market`` **buy** **quote currency** (USDT in ``BTC_USDT``)
        ``market`` sell    base currency
        =========  ======  ==================================================

        So a market buy spends a cash amount, it does not purchase a quantity.
        The floor for a market buy is therefore ``min_quote_amount`` and the cap
        is ``market_order_max_money``, while a market sell is bounded by
        ``min_base_amount`` / ``market_order_max_stock``. Callers must convert
        deliberately: a base-denominated buy has to be expressed either as an
        aggressive immediate-or-cancel limit order or by pre-multiplying by a
        reference price, and the resulting fill quantity must be read back from
        ``filled_amount`` (base) rather than inferred from ``amount``.
        ``left`` is denominated the same way as the submitted ``amount``.

        Only ``ioc`` and ``fok`` are legal with ``type="market"``; send the
        time in force explicitly rather than relying on a default.

        **This request is never replayed.** A retry could submit the order
        twice, and Gate.io's duplicate-``text`` rejection does not cover an
        order that already filled and closed. If the transport raises
        :class:`~nautilus_gateio.http.client.GateioRequestAmbiguousError` the
        submission outcome is unknown: resolve it with :meth:`get_order` on the
        client id rather than resubmitting.
        """
        return await self._client.post("/spot/orders", body=body, expiring=True)

    async def create_batch_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``POST /spot/batch_orders`` — submit up to 4 pairs x 10 orders.

        ``text`` is mandatory on every item, all items must share one ``account``
        value, and HTTP 200 does not mean every order was accepted: inspect the
        per-item ``succeeded``, ``label`` and ``message`` fields.

        Never replayed, for the same reason as :meth:`create_order`; a partially
        applied batch cannot be reconstructed from a replay either.
        """
        return await self._client.post("/spot/batch_orders", body=orders, expiring=True)

    async def cancel_order(
        self,
        order_id: str,
        pair: str,
        account: str | None = None,
    ) -> dict[str, Any]:
        """``DELETE /spot/orders/{order_id}`` — cancel one order.

        Idempotent at the venue (a second cancel reports ``ORDER_NOT_FOUND`` or
        ``ORDER_CLOSED``), so the transport may replay it on a transient
        failure.
        """
        params: dict[str, Any] = {"currency_pair": pair, "account": account}
        return await self._client.delete(f"/spot/orders/{order_id}", params=params, expiring=True)

    async def cancel_all(
        self,
        pair: str,
        side: str | None = None,
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        """``DELETE /spot/orders`` — cancel every resting order on ``pair``.

        ``pair`` is required here by deliberate choice: Gate.io treats
        ``currency_pair`` as optional and cancels *every* order across *every*
        pair and ledger when it is omitted, which no caller of this adapter ever
        wants implicitly.
        """
        params: dict[str, Any] = {"currency_pair": pair, "side": side, "account": account}
        return await self._client.delete("/spot/orders", params=params, expiring=True)

    async def cancel_batch(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``POST /spot/cancel_batch_orders`` — cancel up to 20 orders at once.

        Each item is ``{"currency_pair": ..., "id": ..., "account": ...}`` where
        ``id`` is a venue order id (client ids are accepted only within 30
        minutes of creation). Partial failure is normal: check ``succeeded`` and
        ``label`` on every returned element.

        Canceling is idempotent at the venue, but this endpoint is a ``POST``,
        so the transport does not replay it; re-issue it explicitly if the
        outcome is unknown. Gate.io documents the ``x-gate-exptime`` submission
        deadline here, so a re-issued batch that crawls to the venue is refused
        rather than applied against a book that has moved on.
        """
        return await self._client.post("/spot/cancel_batch_orders", body=orders, expiring=True)

    async def amend_order(
        self,
        order_id: str,
        pair: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """``PATCH /spot/orders/{order_id}`` — amend price and/or quantity.

        Only ``amount``, ``price``, ``amend_text`` and the attached
        ``stop_profit``/``stop_loss`` may change; side, type, time in force,
        iceberg and account cannot. Reducing the quantity below the already
        filled amount cancels the order, and any price change (or quantity
        increase) loses queue priority. Amendments share the order-submission
        rate-limit budget.

        An amendment changes live order state, so it is never replayed; an
        ambiguous failure must be resolved with :meth:`get_order`.
        """
        params: dict[str, Any] = {"currency_pair": pair}
        return await self._client.patch(
            f"/spot/orders/{order_id}", body=body, params=params, expiring=True
        )

    async def my_trades(
        self,
        pair: str | None = None,
        limit: int | None = None,
        page: int | None = None,
        order_id: str | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /spot/my_trades`` — the account's own fills.

        ``limit`` is capped at 1000. Without a time range only the last 7 days
        are returned, a supplied range may not exceed 30 days, and the range is
        matched against the order's *end* time. ``order_id`` requires ``pair``.
        Each fill carries ``id`` (the venue trade id), ``role``
        (``maker``/``taker``), ``fee`` and ``fee_currency``.
        """
        params: dict[str, Any] = {
            "currency_pair": pair,
            "limit": limit,
            "page": page,
            "order_id": order_id,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/spot/my_trades", params=params, signed=True)

    async def countdown_cancel_all(
        self,
        timeout: int,
        pair: str | None = None,
    ) -> dict[str, Any]:
        """``POST /spot/countdown_cancel_all`` — arm the dead-man switch.

        Gate.io cancels the covered orders unless the countdown is refreshed
        within ``timeout`` seconds (minimum 5; ``0`` disarms). Omitting ``pair``
        arms it for every market.
        """
        body: dict[str, Any] = {"timeout": timeout}
        if pair is not None:
            body["currency_pair"] = pair
        return await self._client.post("/spot/countdown_cancel_all", body=body)

    # -- private: price-triggered orders -----------------------------------

    async def create_price_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /spot/price_orders`` — arm a price-triggered (stop) order.

        Body shape::

            {"trigger": {"price": "...", "rule": ">=" | "<=", "expiration": <secs>},
             "put": {"type": "limit"|"market", "side": "buy"|"sell", "price": "...",
                     "amount": "...", "account": "normal"|"margin"|"unified",
                     "time_in_force": "gtc"|"ioc"},
             "market": "BTC_USDT"}

        ``rule`` is the literal two-character string ``">="`` or ``"<="``.
        ``put.account`` uses ``normal`` where a regular order would say ``spot``,
        and ``put.time_in_force`` accepts only ``gtc`` and ``ioc``.

        The response is ``{"id": <int>, "id_string": "<int>"}``; prefer
        ``id_string`` to avoid 64-bit precision loss. The triggered order is a
        *separate* object with its own id, surfaced later as ``fired_order_id``
        on the price order, so callers need a two-stage identity map.

        **No submission deadline.** Arming a stop is as money-significant as
        submitting a plain order, but Gate.io declares ``x-gate-exptime`` only
        for the endpoints listed in ``docs/configuration.md``, and the
        price-order endpoints are not among them, so this request carries
        none: a submission delayed in
        flight is armed whenever it lands, at a price that may no longer be
        the one the caller reasoned about.
        """
        return await self._client.post("/spot/price_orders", body=body)

    async def list_price_orders(
        self,
        status: str,
        market: str | None = None,
        account: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /spot/price_orders`` — armed or finished price-triggered orders.

        ``status`` is required (``open`` or ``finished``). Note this endpoint
        paginates with ``limit``/``offset`` rather than the ``page``/``limit``
        scheme used elsewhere in the spot API.
        """
        params: dict[str, Any] = {
            "status": status,
            "market": market,
            "account": account,
            "limit": limit,
            "offset": offset,
        }
        return await self._client.get("/spot/price_orders", params=params, signed=True)

    async def get_price_order(self, order_id: str) -> dict[str, Any]:
        """``GET /spot/price_orders/{order_id}`` — one price-triggered order.

        ``order_id`` is the auto-order id returned at creation, not the id of the
        spot order it fires.
        """
        return await self._client.get(f"/spot/price_orders/{order_id}", signed=True)

    async def cancel_price_order(self, order_id: str) -> dict[str, Any]:
        """``DELETE /spot/price_orders/{order_id}`` — disarm one price order.

        Like every price-order endpoint, this one carries no submission
        deadline: Gate.io declares ``x-gate-exptime`` for the plain-order
        cancels but not for these, so a disarm delayed in flight still applies
        when it lands, however late.
        """
        return await self._client.delete(f"/spot/price_orders/{order_id}")

    async def cancel_price_orders(
        self,
        market: str | None = None,
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        """``DELETE /spot/price_orders`` — disarm every price order in scope.

        Omitting ``market`` cancels the account's price orders on all markets.
        Carries no submission deadline, for the same reason as
        :meth:`cancel_price_order`.
        """
        params: dict[str, Any] = {"market": market, "account": account}
        return await self._client.delete("/spot/price_orders", params=params)
