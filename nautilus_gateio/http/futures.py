"""Typed REST namespace for Gate.io perpetual and delivery futures.

Routes to ``/api/v4/futures/{settle}/*`` or ``/api/v4/delivery/{settle}/*``
depending on the ``delivery`` flag given to :class:`GateioFuturesHttpAPI`.

Order size is expressed in CONTRACTS as a SIGNED INTEGER
------------------------------------------------------
``size`` is never a coin amount. A positive ``size`` buys (opens or increases a
long), a negative ``size`` sells (opens or increases a short), and ``size=0``
together with ``close=true`` closes the whole position. The contract's
``quanto_multiplier`` converts contracts to base units::

    base_units     = size_contracts * quanto_multiplier
    size_contracts = base_units / quanto_multiplier
    notional_quote = size_contracts * quanto_multiplier * price

For example ``BTC_USDT`` has ``quanto_multiplier = "0.0001"``, so one contract
is 0.0001 BTC and buying 0.05 BTC means sending ``size = 500``. Parse the
multiplier with :class:`decimal.Decimal`, never with a binary float, and reject
non-integral conversions unless the contract sets ``enable_decimal = true``.
Inverse contracts report ``quanto_multiplier = "0"`` as a null sentinel — their
face value is one unit of the quote currency per contract, so never use that
field as a divisor without guarding against zero.

There is no ``type`` field on a futures order. A MARKET order is encoded as
``price = "0"`` with ``tif = "ioc"``; ``price = "0"`` with any other time in
force is not a market order and will be rejected or behave unexpectedly.

Sizes elsewhere in the API follow the same unit: ``left`` on an order, ``size``
on a position and on a fill, ``s`` on an order book level, and ``v`` on a
candlestick are all contract counts.

Why one class serves both product lines
---------------------------------------
Delivery futures are a strict subset of perpetual futures: identical path shape
after the product segment, identical request and response models for contracts,
order books, trades, candlesticks, orders, fills, positions and the account
ledger. Duplicating the surface into two classes would duplicate every
docstring and every payload contract for no behavioural gain, so the product is
a constructor flag and the handful of genuinely perpetual-only endpoints raise
instead of silently returning something else. Those are: funding rates (delivery
contracts have no funding at all), order amendment, and dual-position mode.

One consequence is worth stating, because it is a promise this namespace cannot
keep on both product lines. Gate.io declares the ``x-gate-exptime`` submission
deadline on the *perpetual* order endpoints (``/futures/{settle}/orders`` and
its batch, cancel, cancel-all and amend siblings) and declares it nowhere under
``/delivery``. The methods below ask for the deadline unconditionally, so a
delivery order does carry the header — the request is identical in shape and an
undeclared header cannot make it worse — but whether the venue enforces it there
is undocumented. Treat the deadline as guaranteed on perpetuals and best-effort
on dated contracts. It is never attached to the price-order endpoints of either
product, nor to the countdown switch, because Gate.io declares it for none of
them; see ``docs/configuration.md`` for the complete list.
"""

from __future__ import annotations

from typing import Any

from nautilus_gateio.common.errors import UnsupportedOrderError
from nautilus_gateio.http.client import GateioHttpClient


class GateioFuturesHttpAPI:
    """Perpetual (``/futures``) or delivery (``/delivery``) REST endpoints.

    Parameters
    ----------
    client : GateioHttpClient
        Shared transport handling signing, pacing and error translation.
    settle : str
        Settlement currency path segment. Perpetuals serve ``usdt`` (linear) and
        ``btc`` (inverse); delivery serves ``usdt`` only.
    delivery : bool
        Route to the dated delivery contracts instead of the perpetuals.
    """

    def __init__(
        self,
        client: GateioHttpClient,
        settle: str = "usdt",
        delivery: bool = False,
    ) -> None:
        self._client = client
        self._settle = settle.lower()
        self._delivery = delivery
        self._base = f"/{'delivery' if delivery else 'futures'}/{self._settle}"

    @property
    def client(self) -> GateioHttpClient:
        return self._client

    @property
    def settle(self) -> str:
        return self._settle

    @property
    def is_delivery(self) -> bool:
        return self._delivery

    @property
    def base_path(self) -> str:
        """The product-and-settlement path prefix, e.g. ``/futures/usdt``."""
        return self._base

    def _require_perpetual(self, what: str) -> None:
        if self._delivery:
            raise ValueError(
                f"{what} is not available for delivery futures "
                f"(Gate.io only exposes it under /futures/{{settle}})"
            )

    # -- public market data ------------------------------------------------

    async def contracts(
        self,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/contracts`` — every listed contract.

        Perpetual contracts carry the funding block (``funding_rate``,
        ``funding_interval``, ``funding_next_apply``, ...) and ``enable_decimal``;
        delivery contracts instead carry ``underlying``, ``cycle``,
        ``expire_time``, ``settle_price``, ``settle_fee_rate`` and the basis
        fields, and have no funding fields at all. ``settle_price`` stays
        ``"0"`` until the contract settles.

        ``limit``/``offset`` are honoured by the perpetual endpoint only; the
        delivery endpoint ignores them and always returns the full list.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        return await self._client.get(f"{self._base}/contracts", params=params)

    async def contract(self, name: str) -> dict[str, Any]:
        """``GET {base}/contracts/{name}`` — one contract definition.

        The fields that drive instrument construction are ``quanto_multiplier``
        (contracts to base units), ``order_price_round`` (price tick),
        ``order_size_min``/``order_size_max`` (contract counts),
        ``maintenance_rate`` and the maker/taker rates.
        """
        return await self._client.get(f"{self._base}/contracts/{name}")

    async def order_book(
        self,
        contract: str,
        interval: str | None = None,
        limit: int | None = None,
        with_id: bool = True,
    ) -> dict[str, Any]:
        """``GET {base}/order_book`` — an order book snapshot.

        Levels are objects ``{"p": <price str>, "s": <contracts int>}``. With
        ``with_id=True`` the response carries ``id``, the book sequence number
        used to align incremental WebSocket updates against the snapshot;
        ``current`` and ``update`` are float Unix seconds.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "interval": interval,
            "limit": limit,
            "with_id": with_id,
        }
        return await self._client.get(f"{self._base}/order_book", params=params)

    async def trades(
        self,
        contract: str,
        limit: int | None = None,
        last_id: str | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/trades`` — recent public trades.

        ``size`` is a signed contract count whose sign is the aggressor's side.
        ``create_time_ms`` is misnamed: the venue returns Unix *seconds* there,
        identical to ``create_time`` — do not rescale it.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "limit": limit,
            "last_id": last_id,
            "from": frm,
            "to": to,
        }
        return await self._client.get(f"{self._base}/trades", params=params)

    async def candlesticks(
        self,
        contract: str,
        interval: str = "1m",
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/candlesticks`` — OHLCV bars as objects.

        Each element is ``{"t": <unix s>, "o", "h", "l", "c": <price str>,
        "v": <contracts int>}``. Unlike the spot endpoint the elements are
        objects, not positional arrays. Perpetual bars carry an extra ``sum``
        (turnover in the quote currency); delivery bars do not.

        Prefixing the contract with ``mark_`` or ``index_`` (for example
        ``mark_BTC_USDT``) returns mark-price or index-price candles; those come
        back with ``v = 0`` and ``sum = "0"``.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "interval": interval,
            "limit": limit,
            "from": frm,
            "to": to,
        }
        return await self._client.get(f"{self._base}/candlesticks", params=params)

    async def tickers(self, contract: str | None = None) -> list[dict[str, Any]]:
        """``GET {base}/tickers`` — 24h statistics, mark/index price and BBO.

        Always a list. The perpetual ticker includes ``funding_rate`` and
        ``funding_rate_indicative``; the delivery ticker replaces those with
        ``basis_rate``, ``basis_value`` and ``settle_price``. Volumes are
        published in several units at once (``volume_24h`` in contracts,
        ``volume_24h_base``, ``volume_24h_quote``, ``volume_24h_settle``), and
        the response repeats ``quanto_multiplier``.
        """
        return await self._client.get(f"{self._base}/tickers", params={"contract": contract})

    async def funding_rate(
        self,
        contract: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /futures/{settle}/funding_rate`` — historical funding rates.

        Perpetual contracts only; delivery futures have no funding mechanism and
        calling this on a delivery namespace raises ``ValueError``.

        Records are ``{"t": <unix s>, "r": <decimal ratio str>}``, most recent
        first, spaced by the contract's ``funding_interval``. ``r`` is a ratio,
        not a percentage. Realised funding payments are not in this feed: they
        appear as ``type="fund"`` rows in :meth:`account_book`.
        """
        self._require_perpetual("funding rate history")
        params: dict[str, Any] = {"contract": contract, "limit": limit}
        return await self._client.get(f"{self._base}/funding_rate", params=params)

    async def risk_limit_tiers(
        self,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/risk_limit_tiers`` — the tiered margin table.

        Tiers give ``tier``, ``risk_limit``, ``initial_rate``,
        ``maintenance_rate`` and ``leverage_max``. Perpetual tiers add
        ``deduction``, the fast-path offset in
        ``MM = position_value * maintenance_rate - deduction``; delivery tiers
        omit it. This is the supported replacement for the deprecated
        ``risk_limit_base``/``_step``/``_max`` fields on the contract.

        ``limit``/``offset`` paginate over markets and only take effect when
        ``contract`` is omitted.
        """
        params: dict[str, Any] = {"contract": contract, "limit": limit, "offset": offset}
        return await self._client.get(f"{self._base}/risk_limit_tiers", params=params)

    # -- private: account --------------------------------------------------

    async def accounts(self) -> dict[str, Any]:
        """``GET {base}/accounts`` — the futures wallet for this settlement currency.

        Returns a single object (not a list) with ``total``, ``available``,
        ``position_margin``, ``order_margin``, ``unrealised_pnl``, the
        ``history`` block of lifetime totals, and the position-mode flags
        ``in_dual_mode`` / ``position_mode``.

        The wallet does not exist until funds are first transferred into it;
        until then Gate.io answers ``USER_NOT_FOUND``. That is a provisioning
        state, not an authentication failure — see
        :class:`nautilus_gateio.common.errors.WalletNotProvisionedError`.
        """
        return await self._client.get(f"{self._base}/accounts", signed=True)

    async def account_book(
        self,
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
        type: str | None = None,
        contract: str | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/account_book`` — the settlement-currency cash ledger.

        Rows are ``{time, change, balance, type, text, contract, trade_id, id}``.
        ``type`` values: ``dnw`` (transfers), ``pnl``, ``fee``, ``refr``,
        ``fund`` (funding payments — the authoritative record of every funding
        charge or credit), ``point_dnw``, ``point_fee``, ``point_refr`` and
        ``bonus_offset``. Treat unknown values as non-fatal. Filtering by
        ``contract`` only matches rows written after 2023-10-30; older rows carry
        no contract.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
            "type": type,
        }
        return await self._client.get(f"{self._base}/account_book", params=params, signed=True)

    # -- private: positions ------------------------------------------------

    async def positions(
        self,
        holding: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/positions`` — every position on this settlement currency.

        ``holding=True`` returns only non-zero positions. ``size`` is a signed
        contract count, ``leverage="0"`` means the position runs on cross margin
        (with ``cross_leverage_limit`` holding the effective cap), and
        ``average_maintenance_rate`` — not ``maintenance_rate`` — is the rate
        actually applied to the position. ``update_id`` increments on every
        change and is the correct key for ordering REST against WebSocket
        updates.
        """
        params: dict[str, Any] = {"holding": holding, "limit": limit, "offset": offset}
        return await self._client.get(f"{self._base}/positions", params=params, signed=True)

    async def position(self, contract: str) -> dict[str, Any]:
        """``GET {base}/positions/{contract}`` — one position."""
        return await self._client.get(f"{self._base}/positions/{contract}", signed=True)

    async def update_position_leverage(
        self,
        contract: str,
        leverage: str,
        cross_leverage_limit: str | None = None,
    ) -> dict[str, Any]:
        """``POST {base}/positions/{contract}/leverage`` — set position leverage.

        The parameters are sent as query arguments; there is no request body.
        A positive ``leverage`` selects ISOLATED margin at that multiple.
        ``leverage="0"`` selects CROSS margin, in which case
        ``cross_leverage_limit`` carries the leverage cap actually used. The
        value must lie within the contract's ``leverage_min``/``leverage_max``
        and within the current risk tier's ``leverage_max``.
        """
        params: dict[str, Any] = {
            "leverage": leverage,
            "cross_leverage_limit": cross_leverage_limit,
        }
        return await self._client.post(
            f"{self._base}/positions/{contract}/leverage",
            params=params,
        )

    async def update_position_margin(self, contract: str, change: str) -> dict[str, Any]:
        """``POST {base}/positions/{contract}/margin`` — add or remove margin.

        ``change`` is a signed decimal string: positive adds margin, negative
        withdraws it. Only meaningful under isolated margin; Gate.io answers
        ``POSITION_CROSS_MARGIN`` for a cross-margin position.
        """
        return await self._client.post(
            f"{self._base}/positions/{contract}/margin",
            params={"change": change},
        )

    async def update_position_risk_limit(self, contract: str, risk_limit: str) -> dict[str, Any]:
        """``POST {base}/positions/{contract}/risk_limit`` — set the risk limit.

        The value must be a multiple of the contract's risk-limit step, else the
        venue answers ``RISK_LIMIT_NOT_MULTIPLE``.
        """
        return await self._client.post(
            f"{self._base}/positions/{contract}/risk_limit",
            params={"risk_limit": risk_limit},
        )

    async def set_position_cross_mode(self, contract: str, mode: str) -> dict[str, Any]:
        """``POST {base}/positions/cross_mode`` — switch a position's margin mode.

        ``mode`` is the upper-case ``"ISOLATED"`` or ``"CROSS"``. This is the
        explicit replacement for the legacy convention of setting
        ``leverage="0"`` to mean cross margin.
        """
        body: dict[str, Any] = {"mode": mode, "contract": contract}
        return await self._client.post(f"{self._base}/positions/cross_mode", body=body)

    async def dual_mode(self) -> dict[str, Any]:
        """Report whether hedge (dual) position mode is active.

        Gate.io has no dedicated getter: the state is a property of the futures
        account, exposed as ``in_dual_mode`` (legacy boolean) and
        ``position_mode`` (``single`` / ``dual`` / ``dual_plus``). This method
        reads :meth:`accounts` and returns that account object.

        Delivery futures have no hedge mode and raise ``ValueError``.
        """
        self._require_perpetual("dual (hedge) position mode")
        return await self.accounts()

    async def set_dual_mode(self, dual_mode: bool) -> dict[str, Any]:
        """``POST /futures/{settle}/dual_mode`` — enable or disable hedge mode.

        Only permitted when the account holds no positions and no pending orders
        on this settlement currency; otherwise Gate.io answers
        ``POSITION_HOLDING`` or ``ORDER_PENDING``. Returns the futures account.

        Delivery futures have no hedge mode and raise ``ValueError``.
        """
        self._require_perpetual("dual (hedge) position mode")
        return await self._client.post(
            f"{self._base}/dual_mode",
            params={"dual_mode": dual_mode},
        )

    # -- private: orders ---------------------------------------------------

    async def list_orders(
        self,
        status: str,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        last_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/orders`` — orders by ``status``.

        ``status`` is required and is either ``open`` or ``finished``. Only the
        last six months of finished orders are queryable here. A zero-fill order
        stays invisible for ten minutes after cancellation, so reconciliation
        must not treat "absent" as "never existed" immediately after a cancel.
        """
        params: dict[str, Any] = {
            "status": status,
            "contract": contract,
            "limit": limit,
            "offset": offset,
            "last_id": last_id,
        }
        return await self._client.get(f"{self._base}/orders", params=params, signed=True)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """``GET {base}/orders/{order_id}`` — a single order.

        ``order_id`` accepts the numeric venue id or the client id in ``text``,
        but the client id resolves only while the order rests and for about 60
        seconds after it finishes. Persist the numeric id on acceptance.
        """
        return await self._client.get(f"{self._base}/orders/{order_id}", signed=True)

    async def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST {base}/orders`` — submit a futures order (HTTP 201 on success).

        Body fields: ``contract``, ``size`` (signed contract count),
        ``price`` (string; ``"0"`` with ``tif="ioc"`` means market), ``tif``
        (``gtc``/``ioc``/``poc``/``fok``), ``text`` (client id, ``t-`` prefixed),
        ``iceberg``, ``close``, ``reduce_only``, ``auto_size``, ``stp_act`` and
        ``action_mode``.

        Canonical bodies::

            limit buy 500 contracts, post-only
            {"contract": "BTC_USDT", "size": 500, "price": "64000", "tif": "poc"}

            market sell 500 contracts
            {"contract": "BTC_USDT", "size": -500, "price": "0", "tif": "ioc"}

            close the whole position (one-way mode)
            {"contract": "BTC_USDT", "size": 0, "price": "0", "tif": "ioc", "close": true}

            close the whole long leg (hedge mode)
            {"contract": "BTC_USDT", "size": 0, "price": "0", "tif": "ioc",
             "auto_size": "close_long", "reduce_only": true}

        ``close=true`` is legal only in one-way mode; hedge mode uses
        ``auto_size`` instead. In hedge mode the sign convention inverts for
        reducing orders: with ``reduce_only=true`` a positive ``size`` reduces
        the short leg and a negative ``size`` reduces the long leg.

        **This request is never replayed.** A retry could open the position
        twice. If the transport raises
        :class:`~nautilus_gateio.http.client.GateioRequestAmbiguousError` the
        submission outcome is unknown: resolve it with :meth:`list_orders` or
        :meth:`get_order` on the client id rather than resubmitting.
        """
        return await self._client.post(f"{self._base}/orders", body=body, expiring=True)

    async def create_batch_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``POST /futures/{settle}/batch_orders`` — submit up to 10 orders.

        A malformed item rejects the whole request with HTTP 400; once validated
        every item is executed independently, so a business failure on one order
        does not stop the others. Results are index-aligned with the request and
        each carries ``succeeded``, ``label`` and ``detail``. Each order counts
        separately against the rate limit.

        Delivery futures have no batch endpoints and raise ``ValueError``.

        Never replayed, for the same reason as :meth:`create_order`.
        """
        self._require_perpetual("batch order submission")
        return await self._client.post(f"{self._base}/batch_orders", body=orders, expiring=True)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """``DELETE {base}/orders/{order_id}`` — cancel one order.

        Cancelling an unknown or already finished order returns HTTP 404 rather
        than succeeding silently.
        """
        return await self._client.delete(f"{self._base}/orders/{order_id}", expiring=True)

    async def cancel_all(
        self,
        contract: str,
        side: str | None = None,
        exclude_reduce_only: bool | None = None,
    ) -> list[dict[str, Any]]:
        """``DELETE {base}/orders`` — cancel every resting order on ``contract``.

        ``contract`` is required by the venue: futures has no cancel-everything
        call, so callers must iterate contracts. ``side`` uses the book-side
        vocabulary ``bid`` (cancel buys) and ``ask`` (cancel sells), *not*
        ``buy``/``sell``; omit it to cancel both sides.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "side": side,
            "exclude_reduce_only": exclude_reduce_only,
        }
        return await self._client.delete(f"{self._base}/orders", params=params, expiring=True)

    async def amend_order(self, order_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``PUT /futures/{settle}/orders/{order_id}`` — amend size and/or price.

        Body fields: ``size``, ``price``, ``text``, ``amend_text``. The side
        cannot change and a close order's size cannot change. Amending to a size
        at or below the filled size cancels the order. Reducing the size at an
        unchanged price keeps queue priority; anything else loses it. Orders with
        an attached stop cannot be amended (``AMEND_WITH_STOP``).

        Delivery futures have no amend endpoint; calling this on a delivery
        namespace raises
        :class:`nautilus_gateio.common.errors.UnsupportedOrderError` so the
        execution client can reject the modification explicitly.
        """
        if self._delivery:
            raise UnsupportedOrderError(
                "Gate.io delivery futures do not support order amendment; "
                "cancel and resubmit instead"
            )
        return await self._client.put(f"{self._base}/orders/{order_id}", body=body, expiring=True)

    async def my_trades(
        self,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        last_id: str | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/my_trades`` — the account's own fills.

        Only the last six months are available. Fields: ``id`` (venue trade id),
        ``order_id`` (a *string* here, while the order object reports an integer
        ``id`` — normalise at the parser boundary), ``size`` (signed contracts),
        ``close_size``, ``price``, ``role`` and ``fee`` in the settlement
        currency (negative means a maker rebate).

        ``close_size`` decodes what the fill did to the position: zero means it
        opened, and a magnitude smaller than ``size`` means the fill closed one
        side *and* opened the other, which a position-aware consumer must split
        into two events.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "order": order,
            "limit": limit,
            "offset": offset,
            "last_id": last_id,
        }
        return await self._client.get(f"{self._base}/my_trades", params=params, signed=True)

    async def position_close_history(
        self,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/position_close`` — realised PnL per closed position.

        Rows carry ``side`` (``long``/``short``) and the PnL split
        ``pnl = pnl_pnl + pnl_fund + pnl_fee`` (position result, funding, fees).
        ``long_price``/``short_price`` are direction-dependent: for a long the
        first is the average open and the second the average close, and the
        other way round for a short.
        """
        params: dict[str, Any] = {
            "contract": contract,
            "limit": limit,
            "offset": offset,
            "from": frm,
            "to": to,
            "side": side,
        }
        return await self._client.get(f"{self._base}/position_close", params=params, signed=True)

    async def countdown_cancel_all(
        self,
        timeout: int,
        contract: str | None = None,
    ) -> dict[str, Any]:
        """``POST /futures/{settle}/countdown_cancel_all`` — arm the dead-man switch.

        Gate.io cancels the covered orders unless the countdown is refreshed
        within ``timeout`` seconds (minimum 5; ``0`` disarms).

        Delivery futures have no countdown endpoint and raise ``ValueError``.
        """
        self._require_perpetual("the countdown cancel-all dead-man switch")
        body: dict[str, Any] = {"timeout": timeout}
        if contract is not None:
            body["contract"] = contract
        return await self._client.post(f"{self._base}/countdown_cancel_all", body=body)

    # -- private: price-triggered orders -----------------------------------

    async def create_price_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST {base}/price_orders`` — arm a price-triggered order.

        Body shape::

            {"initial": {"contract": "BTC_USDT", "size": -500, "price": "0",
                         "tif": "ioc", "reduce_only": true},
             "trigger": {"strategy_type": 0, "price_type": 1,
                         "price": "60000", "rule": 2, "expiration": 86400},
             "order_type": "close-long-position"}

        ``trigger.price_type`` selects the reference price: ``0`` last trade,
        ``1`` mark, ``2`` index. ``trigger.rule`` is ``1`` for "trigger when the
        price rises to or above ``trigger.price``" (which must sit above the
        current last price at submission) and ``2`` for the mirror condition
        below it; violating that constraint is rejected on submit.
        ``initial.tif`` accepts only ``gtc`` and ``ioc`` here, and
        ``initial.price = "0"`` means the fired order is a market order.

        Only four ``order_type`` values are submittable —
        ``close-long-position``, ``close-short-position``,
        ``plan-close-long-position``, ``plan-close-short-position``. The
        ``close-long-order`` / ``close-short-order`` pair is read-only.

        The armed order has its own id space: the id returned here is not the
        eventual futures order id, which appears as ``trade_id`` on the price
        order once it fires.

        **No submission deadline.** Gate.io declares ``x-gate-exptime`` on the
        plain futures order endpoints and not on the price-order ones (see
        ``docs/configuration.md`` for the full list), so this request carries
        none: delayed in flight, it arms whenever it lands.
        """
        return await self._client.post(f"{self._base}/price_orders", body=body)

    async def list_price_orders(
        self,
        status: str,
        contract: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET {base}/price_orders`` — armed or finished price-triggered orders.

        ``status`` is required (``open`` or ``finished``).
        """
        params: dict[str, Any] = {
            "status": status,
            "contract": contract,
            "limit": limit,
            "offset": offset,
        }
        return await self._client.get(f"{self._base}/price_orders", params=params, signed=True)

    async def get_price_order(self, order_id: str) -> dict[str, Any]:
        """``GET {base}/price_orders/{order_id}`` — one price-triggered order."""
        return await self._client.get(f"{self._base}/price_orders/{order_id}", signed=True)

    async def cancel_price_order(self, order_id: str) -> dict[str, Any]:
        """``DELETE {base}/price_orders/{order_id}`` — disarm one price order.

        Carries no submission deadline: Gate.io declares ``x-gate-exptime`` for
        the plain-order cancels but not for the price-order ones, so a disarm
        delayed in flight still applies when it lands, however late.
        """
        return await self._client.delete(f"{self._base}/price_orders/{order_id}")

    async def cancel_price_orders(self, contract: str | None = None) -> list[dict[str, Any]]:
        """``DELETE {base}/price_orders`` — disarm price orders.

        ``contract`` is the only filter this endpoint accepts. Carries no
        submission deadline, for the same reason as :meth:`cancel_price_order`.
        """
        return await self._client.delete(
            f"{self._base}/price_orders",
            params={"contract": contract},
        )
