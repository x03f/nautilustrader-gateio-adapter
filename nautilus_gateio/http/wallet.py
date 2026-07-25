"""Typed REST namespace for Gate.io wallet operations the adapter needs.

Gate.io keeps a separate wallet per product: spot, isolated margin, perpetual
futures, delivery futures and options each hold their own balances, and funds do
not flow between them implicitly. Moving money between them is what
:meth:`GateioWalletHttpAPI.transfer` is for, and it is also how the derivative
wallets come into existence — Gate.io creates the futures, delivery and options
accounts on the first transfer into them, answering ``USER_NOT_FOUND`` until
that has happened.

This class NEVER moves funds out of the account
-----------------------------------------------
It performs internal transfers between the *same* account's own trading wallets
and nothing else. There is no withdrawal method here, no sub-account transfer
method, and no method that accepts a blockchain address or another user's UID —
those endpoints are not implemented anywhere in this package, so no caller can
reach them through it. :data:`ALLOWED_TRANSFER_ACCOUNTS` is enforced on both
ends of every transfer, and it contains only internal trading wallets: no
external destination can be expressed.

That is a property of this module, not a security boundary. The authoritative
control over what an API key may do is the key's own permission set on Gate.io;
a key used with this adapter needs no withdrawal permission at all.
"""

from __future__ import annotations

from typing import Any, Final

from nautilus_gateio.http.client import GateioHttpClient

#: The account names accepted on either end of an internal transfer.
#:
#: Every member is one of the account's own trading wallets. ``spot``,
#: ``margin``, ``futures``, ``delivery`` and ``options`` are the values Gate.io's
#: own SDK validates against; ``cross_margin`` and ``unified`` appear in older
#: documentation and in the ``/wallet/total_balance`` breakdown, and are accepted
#: here for accounts that use those ledgers. External destinations — withdrawal
#: addresses, other users, sub-accounts — are deliberately absent and cannot be
#: expressed through this API.
ALLOWED_TRANSFER_ACCOUNTS: Final[frozenset[str]] = frozenset(
    {
        "spot",
        "margin",
        "futures",
        "delivery",
        "options",
        "cross_margin",
        "unified",
    }
)

#: Wallets whose transfers must name a settlement currency.
_SETTLE_REQUIRED: Final[frozenset[str]] = frozenset({"futures", "delivery", "options"})

#: Wallets whose transfers must name a currency pair (isolated margin is per-pair).
_PAIR_REQUIRED: Final[frozenset[str]] = frozenset({"margin"})


class GateioWalletHttpAPI:
    """Internal transfers and account-wide fee/balance reads.

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

    @staticmethod
    def _validate_account(name: str, role: str) -> str:
        if not isinstance(name, str) or name not in ALLOWED_TRANSFER_ACCOUNTS:
            allowed = ", ".join(sorted(ALLOWED_TRANSFER_ACCOUNTS))
            raise ValueError(
                f"transfer {role} account {name!r} is not an internal trading wallet; "
                f"allowed values are: {allowed}"
            )
        return name

    async def transfer(
        self,
        currency: str,
        from_: str,
        to: str,
        amount: str,
        settle: str | None = None,
        currency_pair: str | None = None,
    ) -> dict[str, Any]:
        """``POST /wallet/transfers`` — move funds between this account's wallets.

        Parameters
        ----------
        currency : str
            Asset to move, e.g. ``"USDT"``.
        from_ : str
            Source wallet. Named with a trailing underscore because ``from`` is
            a Python keyword; it is sent as ``from`` on the wire.
        to : str
            Destination wallet.
        amount : str
            Decimal amount as a string, to avoid binary floating point.
        settle : str, optional
            Settlement currency of the contract wallet, lower case
            (``"usdt"``, ``"btc"``). Required whenever either end is
            ``futures``, ``delivery`` or ``options``.
        currency_pair : str, optional
            Isolated margin pair. Required whenever either end is ``margin``.

        Returns
        -------
        dict
            ``{"tx_id": <int>}``. Poll :meth:`transfer_status` with that id to
            confirm completion.

        Raises
        ------
        ValueError
            If either end is not in :data:`ALLOWED_TRANSFER_ACCOUNTS`, if the
            two ends are the same wallet, or if a required ``settle`` /
            ``currency_pair`` companion is missing.

        Notes
        -----
        Gate.io routes every internal transfer through the spot wallet, so there
        is no direct hop between two derivative wallets: moving from futures to
        options means futures to spot, then spot to options.

        A transfer into a derivative wallet is also what creates it. An account
        that has never funded futures, delivery or options will see
        ``USER_NOT_FOUND`` from those product endpoints until the first transfer
        lands.

        This method cannot send funds anywhere outside the account: the request
        has no address and no recipient field, and both endpoints are validated
        against an allow-list of internal wallets before anything is signed.

        **This request is never replayed.** Gate.io offers no idempotency token
        for transfers — the ``tx_id`` is assigned by the venue, not supplied by
        the caller — so a transparent retry could move the funds twice. A
        failure whose outcome is unknown surfaces as
        :class:`~nautilus_gateio.http.client.GateioRequestAmbiguousError`;
        resolve it by listing the wallet ledger rather than transferring again.
        """
        source = self._validate_account(from_, "source")
        destination = self._validate_account(to, "destination")
        if source == destination:
            raise ValueError(f"transfer source and destination are the same wallet: {source!r}")

        needs_settle = source in _SETTLE_REQUIRED or destination in _SETTLE_REQUIRED
        if needs_settle and not settle:
            raise ValueError(
                "transfers involving a contract wallet (futures, delivery, options) "
                "require the 'settle' currency, e.g. settle='usdt'"
            )
        needs_pair = source in _PAIR_REQUIRED or destination in _PAIR_REQUIRED
        if needs_pair and not currency_pair:
            raise ValueError(
                "transfers involving the isolated margin wallet require "
                "'currency_pair', because isolated margin balances are per pair"
            )

        body: dict[str, Any] = {
            "currency": currency,
            "from": source,
            "to": destination,
            "amount": amount,
        }
        if settle is not None:
            body["settle"] = settle
        if currency_pair is not None:
            body["currency_pair"] = currency_pair
        return await self._client.post("/wallet/transfers", body=body)

    async def transfer_status(self, tx_id: str) -> dict[str, Any]:
        """``GET /wallet/order_status`` — the outcome of a transfer.

        Returns ``{"tx_id": "...", "status": ...}`` where the status is
        ``PENDING``, ``SUCCESS`` or ``FAIL``. Poll by ``tx_id``: the transfer
        request itself carries no client-supplied identifier.
        """
        return await self._client.get(
            "/wallet/order_status",
            params={"tx_id": tx_id},
            signed=True,
        )

    async def total_balance(self, currency: str | None = None) -> dict[str, Any]:
        """``GET /wallet/total_balance`` — an estimated total across all wallets.

        ``currency`` is the valuation unit (``BTC``, ``CNY``, ``USD`` or the
        default ``USDT``). The response is ``{"total": ..., "details": {...}}``
        with the breakdown keyed by wallet name; treat ``details`` as an open
        map, since Gate.io's documented key list is not exhaustive.

        **This is a cached estimate**, refreshed at most once a minute, and
        Gate.io explicitly advises against using it for real-time calculations.
        Account state must come from the per-product balance endpoints instead:
        spot accounts, margin accounts, futures accounts per settlement
        currency, and the options account.
        """
        return await self._client.get(
            "/wallet/total_balance",
            params={"currency": currency},
            signed=True,
        )

    async def fee(
        self,
        currency_pair: str | None = None,
        settle: str | None = None,
    ) -> dict[str, Any]:
        """``GET /wallet/fee`` — the account's maker/taker rates.

        One call returns spot (``maker_fee`` / ``taker_fee``), perpetual
        (``futures_maker_fee`` / ``futures_taker_fee``) and delivery
        (``delivery_maker_fee`` / ``delivery_taker_fee``) rates, which makes it
        the right source for populating fees across a multi-product instrument
        set. It also returns ``user_id``, needed by the private futures and
        options WebSocket subscriptions.

        There are no options fee fields here: option maker and taker rates come
        from each contract's own ``maker_fee_rate`` / ``taker_fee_rate``.

        Interpret the GT-discount fields carefully. ``gt_taker_fee`` and
        ``gt_maker_fee`` are zero when GT deduction is disabled, and there zero
        means "not applicable", not "free" — check ``gt_discount`` before
        preferring them, and read ``debit_fee`` to learn which discount scheme
        is actually active.

        ``settle`` narrows contract-fee accuracy to one settlement currency.
        """
        params: dict[str, Any] = {"currency_pair": currency_pair, "settle": settle}
        return await self._client.get("/wallet/fee", params=params, signed=True)
