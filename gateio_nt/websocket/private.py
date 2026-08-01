"""Typed helpers for Gate.io's private (authenticated) WebSocket channels.

Every request on these channels carries an ``auth`` object signed over
``channel=<channel>&event=<event>&time=<time>``; the transport in
:mod:`gateio_nt.websocket.client` attaches it to subscribes and
unsubscribes alike.

Payload conventions differ per product, which is the main reason for this class:

* spot channels take a list of currency pairs, or ``["!all"]`` for every pair,
  and the balance channels take no payload at all;
* futures, delivery and options channels take ``[user_id, contract]``, where the
  contract may be ``"!all"``; the balance channels take ``[user_id]``.

The user id is the numeric account id, obtainable from ``GET /spot/fee``. The
venue does not validate it at subscribe time — a wrong id is acknowledged
successfully and then silently delivers nothing — so it must be fetched rather
than guessed.

Payload shapes delivered to the handler
---------------------------------------
``spot.orders``
    Array of order objects with an ``event`` discriminator: ``put`` (accepted),
    ``update`` (partially filled) and ``finish`` (left the book). ``finish_as``
    gives the terminal reason, ``filled`` among them. Amounts are strings and
    ``side`` is explicit.

``futures.orders`` / ``options.orders``
    Array of order objects with no lifecycle event: the discriminators are
    ``status`` (``open`` or ``finished``), ``finish_as`` and ``left``. ``size``
    is signed — positive is a buy — and is expressed in contracts.

``spot.usertrades`` / ``futures.usertrades`` / ``options.usertrades``
    Array of fills. The venue trade ``id`` is the only safe deduplication key
    across the WebSocket and REST reconciliation paths. Options name the parent
    order field ``order`` rather than ``order_id``.

``spot.balances`` / ``futures.balances`` / ``options.balances``
    Array of balance change events. Spot reports per currency with a
    ``change_type``; futures and options report the settle-currency wallet with
    a ``type`` such as ``dnw``, ``pnl``, ``fee`` or ``fund``.

``futures.positions`` / ``options.positions``
    Array of position states, ``size`` signed and in contracts. Futures carry an
    ``update_id`` sequence number and report ``leverage: 0`` for cross margin.
    Positions have no equivalent on spot.

Gate.io provides no replay or resume on any private channel, so a reconnect must
be followed by REST reconciliation — see the execution client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Final

from gateio_nt.common.constants import CHANNEL_PREFIX, ws_url
from gateio_nt.common.enums import GateioProductType
from gateio_nt.websocket.client import GateioWebSocketClient

#: Wildcard accepted by the private channels in place of a symbol or contract.
ALL: Final[str] = "!all"


class GateioPrivateWebSocket:
    """Authenticated order, fill, balance and position streams for one product.

    Parameters
    ----------
    product : GateioProductType
        The product family to stream. Perpetual, delivery and options each have
        their own endpoint and wallet.
    handler : Callable[[dict], Any]
        Receives every data message; see the module docstring for the payloads.
    api_key, api_secret : str
        Credentials with at least read permission for the product.
    user_id : str | int, optional
        Numeric account id, required by the futures, delivery and options
        channels. May also be supplied per call.
    url : str, optional
        Override the endpoint.
    testnet : bool
        Use the testnet endpoint where Gate.io publishes one.
    loop : asyncio.AbstractEventLoop, optional
        Event loop for the connection's background tasks.
    on_reconnect : Callable[[], Any], optional
        Invoked after subscriptions have been replayed; the right place to
        trigger REST reconciliation.
    """

    def __init__(
        self,
        product: GateioProductType,
        handler: Callable[[dict[str, Any]], Any],
        api_key: str,
        api_secret: str,
        user_id: str | int | None = None,
        url: str | None = None,
        testnet: bool = False,
        loop: asyncio.AbstractEventLoop | None = None,
        **client_kwargs: Any,
    ) -> None:
        self.product = product
        self.user_id = str(user_id) if user_id is not None else None
        self.client = GateioWebSocketClient(
            url=url or ws_url(product, testnet),
            product=product,
            handler=handler,
            api_key=api_key,
            api_secret=api_secret,
            loop=loop,
            **client_kwargs,
        )

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        await self.client.connect()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected

    @property
    def prefix(self) -> str:
        """The channel namespace for this product."""
        return CHANNEL_PREFIX[self.product]

    # -- orders ------------------------------------------------------------

    async def subscribe_orders(
        self,
        user_id: str | int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Subscribe to order lifecycle updates.

        ``symbols`` defaults to every symbol of the product. On spot a single
        subscription covers the whole list; on futures, delivery and options the
        venue accepts one contract per request, so one subscription is issued
        per symbol and the acknowledgements are returned together.
        """
        return await self._subscribe_contract_scoped(f"{self.prefix}.orders", user_id, symbols)

    async def unsubscribe_orders(
        self,
        user_id: str | int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._unsubscribe_contract_scoped(f"{self.prefix}.orders", user_id, symbols)

    # -- fills -------------------------------------------------------------

    async def subscribe_user_trades(
        self,
        user_id: str | int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Subscribe to this account's own fills."""
        return await self._subscribe_contract_scoped(f"{self.prefix}.usertrades", user_id, symbols)

    async def unsubscribe_user_trades(
        self,
        user_id: str | int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._unsubscribe_contract_scoped(
            f"{self.prefix}.usertrades", user_id, symbols
        )

    # -- balances ----------------------------------------------------------

    async def subscribe_balances(self, user_id: str | int | None = None) -> dict[str, Any]:
        """Subscribe to wallet balance changes for this product's wallet.

        Gate.io keeps a separate wallet per product, so a session trading
        several products subscribes on each of their connections.
        """
        channel = f"{self.prefix}.balances"
        return await self.client.subscribe(channel, self._balances_payload(user_id), auth=True)

    async def unsubscribe_balances(self, user_id: str | int | None = None) -> dict[str, Any]:
        channel = f"{self.prefix}.balances"
        return await self.client.unsubscribe(channel, self._balances_payload(user_id), auth=True)

    async def subscribe_margin_balances(self) -> dict[str, Any]:
        """Subscribe to isolated margin balances (spot only).

        Reports ``borrowed`` and ``interest`` per currency and pair, which the
        plain spot balance channel does not carry.
        """
        self._require_spot("spot.margin_balances")
        return await self.client.subscribe("spot.margin_balances", [], auth=True)

    async def subscribe_cross_balances(self) -> dict[str, Any]:
        """Subscribe to cross margin balances (spot only)."""
        self._require_spot("spot.cross_balances")
        return await self.client.subscribe("spot.cross_balances", [], auth=True)

    async def subscribe_funding_balances(self) -> dict[str, Any]:
        """Subscribe to the lending (funding) wallet balances (spot only)."""
        self._require_spot("spot.funding_balances")
        return await self.client.subscribe("spot.funding_balances", [], auth=True)

    # -- positions ---------------------------------------------------------

    async def subscribe_positions(
        self,
        user_id: str | int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Subscribe to position updates (futures, delivery and options only)."""
        if self.product.is_spot:
            raise ValueError("spot has no position channel; positions exist on derivatives only")
        return await self._subscribe_contract_scoped(f"{self.prefix}.positions", user_id, symbols)

    async def unsubscribe_positions(
        self,
        user_id: str | int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.product.is_spot:
            raise ValueError("spot has no position channel; positions exist on derivatives only")
        return await self._unsubscribe_contract_scoped(f"{self.prefix}.positions", user_id, symbols)

    # -- internals ---------------------------------------------------------

    def _require_spot(self, channel: str) -> None:
        if not self.product.is_spot:
            raise ValueError(
                f"{channel} is a spot channel and is unavailable on {self.product.value}"
            )

    def _resolve_user_id(self, user_id: str | int | None) -> str:
        resolved = str(user_id) if user_id is not None else self.user_id
        if not resolved:
            raise ValueError(
                f"{self.product.value} private channels require the account's numeric user id "
                f"(available from GET /spot/fee)"
            )
        return resolved

    def _balances_payload(self, user_id: str | int | None) -> list[str]:
        # Spot balance channels take no payload; the derivative ones are scoped
        # to the account.
        if self.product.is_spot:
            return []
        return [self._resolve_user_id(user_id)]

    def _payloads(
        self,
        user_id: str | int | None,
        symbols: Sequence[str] | None,
    ) -> list[Any]:
        """Build the per-request payloads for a contract-scoped channel."""
        selected = list(symbols) if symbols else [ALL]
        if self.product.is_spot:
            # Spot takes the whole list in one request.
            return [selected]
        resolved = self._resolve_user_id(user_id)
        return [[resolved, symbol] for symbol in selected]

    async def _subscribe_contract_scoped(
        self,
        channel: str,
        user_id: str | int | None,
        symbols: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        acks = []
        for payload in self._payloads(user_id, symbols):
            acks.append(await self.client.subscribe(channel, payload, auth=True))
        return acks

    async def _unsubscribe_contract_scoped(
        self,
        channel: str,
        user_id: str | int | None,
        symbols: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        acks = []
        for payload in self._payloads(user_id, symbols):
            acks.append(await self.client.unsubscribe(channel, payload, auth=True))
        return acks
