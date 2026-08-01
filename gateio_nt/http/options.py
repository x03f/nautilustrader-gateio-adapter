"""Typed REST namespace for the Gate.io options API (``/api/v4/options/*``).

Gate.io options are cash-settled European options quoted and settled in USDT.
Unlike futures there is no ``{settle}`` path segment: the options wallet is a
single USDT account covering every underlying.

Size, price and premium
-----------------------
``size`` is a SIGNED INTEGER count of contracts: positive buys, negative sells,
and ``size=0`` with ``close=true`` closes the position. Prices (``price``,
``mark_price``, ``last_price``, candlestick OHLC, ``strike_price``) are quoted
in USDT *per unit of the underlying*, so cash amounts require the contract's
``multiplier``::

    premium_usdt = price * multiplier * size

The multiplier is the units of underlying represented by one contract (0.01 for
BTC and ETH, 1 for LTC and SOL, 100 for ADA, 1000 for DOGE). The same factor
converts settlement intrinsic value into cash, which is why a settlement record
reports ``profit = max(0, +-(settle_price - strike)) * multiplier`` per contract.

There is no ``type`` field on an options order. A MARKET order is encoded as
``price = "0"`` with ``tif = "ioc"``, and options accept only
``gtc``/``ioc``/``poc`` — there is no ``fok`` here, unlike futures.

Contract payload caveat
-----------------------
Gate.io's published OpenAPI model for ``/options/contracts`` is incomplete: the
live response carries fields the schema omits, including ``strike_price`` and
``is_active``. Parse the raw JSON and keep it, rather than filtering it through
the documented model. Tradability is ``is_active`` — there is no options
equivalent of the futures ``in_delisting`` field.
"""

from __future__ import annotations

from typing import Any

from gateio_nt.http.client import GateioHttpClient


class GateioOptionsHttpAPI:
    """Options REST endpoints.

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

    async def underlyings(self) -> list[dict[str, Any]]:
        """``GET /options/underlyings`` — the tradable underlyings.

        Elements are ``{"name": "BTC_USDT", "index_price": "...",
        "index_time": <unix s>}``. Every underlying is quoted against USDT.
        """
        return await self._client.get("/options/underlyings")

    async def expirations(self, underlying: str) -> list[int]:
        """``GET /options/expirations`` — expiry timestamps for an underlying.

        A bare array of Unix seconds, returned **unsorted**; every expiry falls
        at 08:00:00 UTC.
        """
        return await self._client.get("/options/expirations", params={"underlying": underlying})

    async def contracts(
        self,
        underlying: str,
        expiration: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/contracts`` — option contracts for an underlying.

        ``underlying`` is required; omitting ``expiration`` returns every
        expiry. Contract names follow
        ``<UNDERLYING>-<YYYYMMDD>-<STRIKE>-<C|P>``, for example
        ``BTC_USDT-20260729-70000-C``. The date in the name is only a label —
        take the exact instant from ``expiration_time``.

        Beyond the documented schema the live payload includes
        ``strike_price``, ``is_active``, ``position_limit``,
        ``market_order_slip_ratio``, the ``mark_price_up``/``mark_price_down``
        band and an embedded top of book (``bid1_price``, ``bid1_size``,
        ``ask1_price``, ``ask1_size``).
        """
        params: dict[str, Any] = {"underlying": underlying, "expiration": expiration}
        return await self._client.get("/options/contracts", params=params)

    async def contract(self, name: str) -> dict[str, Any]:
        """``GET /options/contracts/{name}`` — one contract, same field set."""
        return await self._client.get(f"/options/contracts/{name}")

    async def settlements(
        self,
        underlying: str,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/settlements`` — historical settlements for an underlying.

        Rows are ``{time, contract, strike_price, settle_price, profit, fee}``
        where ``profit`` is the per-contract cash intrinsic value (already
        multiplied by the contract multiplier) and ``settle_price`` is Gate.io's
        averaged underlying price at the 08:00 UTC expiry.
        """
        params: dict[str, Any] = {
            "underlying": underlying,
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/options/settlements", params=params)

    async def settlement(self, contract: str, underlying: str, at: int) -> dict[str, Any]:
        """``GET /options/settlements/{contract}`` — one settlement record.

        All three arguments are required; ``at`` is the settlement ``time``.
        """
        params: dict[str, Any] = {"underlying": underlying, "at": at}
        return await self._client.get(f"/options/settlements/{contract}", params=params)

    async def order_book(
        self,
        contract: str,
        interval: str | None = None,
        limit: int | None = None,
        with_id: bool = True,
    ) -> dict[str, Any]:
        """``GET /options/order_book`` — an order book snapshot.

        Shares the futures book shape: levels are
        ``{"p": <price str>, "s": <contracts int>}``, ``id`` is present only
        with ``with_id=True``, and ``current``/``update`` are float Unix seconds.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "interval": interval,
            "limit": limit,
            "with_id": with_id,
        }
        return await self._client.get("/options/order_book", params=params)

    async def tickers(self, underlying: str) -> list[dict[str, Any]]:
        """``GET /options/tickers`` — every contract of an underlying at once.

        Each row carries ``name``, best bid/ask, ``mark_price``,
        ``index_price``, ``underlying_price``, ``position_size`` and the Greeks
        (``delta``, ``gamma``, ``vega``, ``theta``, ``rho``) plus
        ``mark_iv``/``bid_iv``/``ask_iv``. This is the cheapest way to refresh a
        whole option chain.
        """
        return await self._client.get("/options/tickers", params={"underlying": underlying})

    async def underlying_ticker(self, underlying: str) -> dict[str, Any]:
        """``GET /options/underlying/tickers/{underlying}`` — index price and flow.

        Returns ``{"trade_put": <int>, "trade_call": <int>, "index_price": "..."}``
        where the two counts are 24-hour put and call volumes.
        """
        return await self._client.get(f"/options/underlying/tickers/{underlying}")

    async def candlesticks(
        self,
        contract: str,
        interval: str = "5m",
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/candlesticks`` — option-premium bars.

        Elements are ``{"t": <unix s>, "v": <contracts int>, "o", "h", "l", "c"}``
        with prices in USDT per unit of underlying. Illiquid contracts legally
        return an empty array — handle that as "no data", not as an error.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "interval": interval,
            "limit": limit,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/options/candlesticks", params=params)

    async def underlying_candlesticks(
        self,
        underlying: str,
        interval: str = "5m",
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/underlying/candlesticks`` — index bars for an underlying.

        Elements are ``{"t", "o", "h", "l", "c"}``; there is no volume on the
        index series.
        """
        params: dict[str, Any] = {
            "underlying": underlying,
            "interval": interval,
            "limit": limit,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/options/underlying/candlesticks", params=params)

    async def trades(
        self,
        contract: str | None = None,
        type: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/trades`` — recent public trades.

        ``type`` filters by option kind: ``C`` for calls, ``P`` for puts.
        ``limit`` is capped at 1000. Elements are
        ``{"id", "create_time", "contract", "size", "price"}``; ``size`` follows
        the futures convention where the sign is the aggressor's side.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "type": type,
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/options/trades", params=params)

    # -- private: account and positions ------------------------------------

    async def account(self) -> dict[str, Any]:
        """``GET /options/accounts`` — the single USDT options wallet.

        Note the endpoint is plural even though it returns one object. Fields
        include ``total``, ``available``, ``equity``, ``position_value``,
        ``unrealised_pnl``, ``init_margin``, ``maint_margin``, ``order_margin``
        and the capability flags ``short_enabled``, ``mmp_enabled`` and
        ``liq_triggered``.

        The wallet does not exist until funds are first transferred into it and
        the venue answers with a not-found response until then; treat that as a
        provisioning state rather than an authentication failure.
        """
        return await self._client.get("/options/accounts", signed=True)

    async def account_book(
        self,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
        type: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/account_book`` — the USDT ledger of the options wallet.

        Rows are ``{time, change, balance, type, text}``. Observed ``type``
        values include ``dnw`` (transfers), ``prem`` (premium paid or received),
        ``fee``, ``refr`` and ``set`` (settlement result). Gate.io's own
        documents disagree on the complete set, so accept any string.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
            "type": type,
        }
        return await self._client.get("/options/account_book", params=params, signed=True)

    async def positions(
        self,
        underlying: str | None = None,
        contract: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Open option positions.

        With ``contract`` this calls ``GET /options/positions/{contract}`` and
        returns a single object; otherwise it calls ``GET /options/positions``
        and returns a list, optionally narrowed by ``underlying``.

        ``size`` is a signed contract count. Positions also carry
        ``entry_price``, ``mark_price``, ``mark_iv``, realized and unrealized
        PnL, the per-position Greeks, and ``close_order``, which is ``null``
        when no closing order is working.
        """
        if contract is not None:
            return await self._client.get(f"/options/positions/{contract}", signed=True)
        return await self._client.get(
            "/options/positions",
            params={"underlying": underlying},
            signed=True,
        )

    async def position_close_history(
        self,
        underlying: str,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/position_close`` — realized PnL per closed position.

        ``underlying`` is required. Rows are
        ``{time, contract, side, pnl, text, settle_size}`` with ``side`` being
        ``long`` or ``short``.
        """
        params: dict[str, Any] = {
            "underlying": underlying,
            "contract": contract,
            "limit": limit,
            "offset": offset,
        }
        return await self._client.get("/options/position_close", params=params, signed=True)

    # -- private: orders ---------------------------------------------------

    async def list_orders(
        self,
        status: str,
        contract: str | None = None,
        underlying: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/orders`` — orders by ``status``.

        ``status`` is required and is either ``open`` or ``finished``.
        ``frm``/``to`` are Unix seconds.
        """
        params: dict[str, Any] = {
            "status": status,
            "contract": contract,
            "underlying": underlying,
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/options/orders", params=params, signed=True)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """``GET /options/orders/{order_id}`` — one order by its numeric venue id."""
        return await self._client.get(f"/options/orders/{order_id}", signed=True)

    async def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /options/orders`` — submit an options order (HTTP 201 on success).

        Body fields: ``contract``, ``size`` (signed contract count), ``price``
        (string), ``tif`` (``gtc``, ``ioc`` or ``poc`` — options have no
        ``fok``), ``text`` (client id, ``t-`` prefixed), ``iceberg``, ``close``,
        ``reduce_only`` and ``mmp``.

        A market order is ``price="0"`` with ``tif="ioc"``; the contract's
        ``market_order_slip_ratio`` bounds how far it may slip. Any other
        combination with a zero price is undocumented and should be rejected
        before submission.

        Price validity is constrained twice and the two rules disagree in width:
        the documented band is ``mark_price +- order_price_deviate *
        underlying_price``, while the live-only ``mark_price_up`` /
        ``mark_price_down`` fields describe a much tighter window. Validating
        against the intersection is the safe reading.

        Selling opens a short option position with unbounded loss. Gate.io
        publishes ``short_enabled`` on the options account, which is the
        authoritative permission for that; a long-only caller should assert it.

        **This request is never replayed.** A retry could open the position
        twice, and Gate.io documents no submission-deadline header for the
        options endpoints, so the transport cannot even bound how late a
        delayed request may land. On
        :class:`~gateio_nt.http.client.GateioRequestAmbiguousError`,
        resolve the outcome with :meth:`list_orders` before resubmitting.
        """
        return await self._client.post("/options/orders", body=body)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """``DELETE /options/orders/{order_id}`` — cancel one order.

        Gate.io documents no ``x-gate-exptime`` header anywhere in the options
        API, so this cancel carries no submission deadline: delayed in flight,
        it still applies whenever it lands.
        """
        return await self._client.delete(f"/options/orders/{order_id}")

    async def cancel_all(
        self,
        contract: str | None = None,
        underlying: str | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        """``DELETE /options/orders`` — cancel resting orders in scope.

        Returns the canceled orders. At least one of ``contract`` or
        ``underlying`` is required by deliberate choice: Gate.io treats both as
        optional and cancels *every* resting option order in the account when
        they are omitted, which no caller of this adapter ever wants
        implicitly. This matches the scope policy of
        :meth:`gateio_nt.http.spot.GateioSpotHttpAPI.cancel_all` and of
        :meth:`gateio_nt.http.futures.GateioFuturesHttpAPI.cancel_all`.
        To cancel the whole account, iterate the underlyings explicitly.

        ``side`` is documented only as "all bids or all asks" with no published
        value set; by analogy with futures it is expected to be ``bid``/``ask``,
        but that is unverified, so prefer omitting it and scoping the cancel
        with ``contract`` or ``underlying``.

        Raises
        ------
        ValueError
            If neither ``contract`` nor ``underlying`` is given.
        """
        if not contract and not underlying:
            raise ValueError(
                "options cancel_all requires a scope: pass 'contract' or 'underlying'. "
                "Gate.io would otherwise cancel every resting option order in the account"
            )
        params: dict[str, Any] = {
            "contract": contract,
            "underlying": underlying,
            "side": side,
        }
        return await self._client.delete("/options/orders", params=params)

    async def countdown_cancel_all(
        self,
        timeout: int,
        contract: str | None = None,
        underlying: str | None = None,
    ) -> dict[str, Any]:
        """``POST /options/countdown_cancel_all`` — arm the dead-man switch.

        Gate.io cancels the covered orders unless the countdown is refreshed
        within ``timeout`` seconds (minimum 5; ``0`` disarms).
        """
        body: dict[str, Any] = {"timeout": timeout}
        if contract is not None:
            body["contract"] = contract
        if underlying is not None:
            body["underlying"] = underlying
        return await self._client.post("/options/countdown_cancel_all", body=body)

    async def my_trades(
        self,
        underlying: str,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/my_trades`` — the account's own fills.

        ``underlying`` is required. Fields are ``id`` (venue trade id),
        ``create_time``, ``contract``, ``order_id``, ``size`` (signed
        contracts), ``price``, ``underlying_price`` and ``role``.

        There is no fee field on an options fill: commission has to come from
        the order's ``tkfr``/``mkfr`` rates or from the ``fee`` rows of
        :meth:`account_book`.
        """
        params: dict[str, Any] = {
            "underlying": underlying,
            "contract": contract,
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/options/my_trades", params=params, signed=True)

    async def my_settlements(
        self,
        underlying: str,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /options/my_settlements`` — the account's settlement results.

        ``underlying`` is required. Rows carry ``size``, ``strike_price``,
        ``settle_price``, ``settle_profit``, ``fee`` and ``realised_pnl``, the
        last being the cumulative result including premium and fees.

        Settlement closes positions without producing an order or a fill, so
        reconciliation must read this endpoint rather than inferring the close
        from trade history.
        """
        params: dict[str, Any] = {
            "underlying": underlying,
            "contract": contract,
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/options/my_settlements", params=params, signed=True)
