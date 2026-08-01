"""Typed REST namespace for Gate.io margin ledgers: isolated, cross and unified.

Margin is not a separate market on Gate.io. Orders are still placed through the
spot endpoints (:mod:`gateio_nt.http.spot`) with the ``account`` field set
to ``margin``, ``cross_margin`` or ``unified``; what lives here are the balance,
borrow, repay and account-mode endpoints backing those ledgers.

Three ledgers, three endpoint families
--------------------------------------
* **Isolated margin** — ``/margin/*`` for balances and settings, ``/margin/uni/*``
  for borrowing. Positions and debt are scoped to a single currency pair.
* **Cross margin** — ``/margin/cross/*``. Gate.io has retired most of this
  family: only the historical loan and repayment queries remain in its own SDK,
  and cross-margin state is now reported through the unified account.
* **Unified account** — ``/unified/*``. Requires the account to be upgraded out
  of ``classic`` mode; every endpoint here fails until then.

Debt and capability gating
--------------------------
Every method that can create a liability says so explicitly in its docstring.
Borrowing accrues interest from the moment it is drawn, and Gate.io can
liquidate collateral to recover it.

The endpoints in this module are the ones most likely to fail for reasons that
are *configuration*, not error: an unprovisioned wallet, an account still in
classic mode, or an API key without unified permission. :func:`require_wallet`
turns those responses into
:class:`~gateio_nt.common.errors.WalletNotProvisionedError` — or, where the
venue refused the query rather than reporting an empty ledger, its subclass
:class:`~gateio_nt.common.errors.WalletQueryRefusedError` — with an
actionable message, so a caller trading only spot can degrade gracefully instead
of failing to start.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from gateio_nt.common.errors import (
    ACCOUNT_MODE_LABELS,
    WALLET_NOT_PROVISIONED_LABELS,
    GateioClientError,
    WalletNotProvisionedError,
    WalletQueryRefusedError,
)
from gateio_nt.http.client import GateioHttpClient

#: What a caller can actually do about each capability-gating error label.
_REMEDIES: dict[str, str] = {
    "USER_NOT_FOUND": (
        "the wallet has not been created yet; Gate.io creates it on the first "
        "internal transfer into it (POST /wallet/transfers)"
    ),
    "INVALID_UNIFIED_ACCOUNT": (
        "the account is not a unified account; upgrade it in the Gate.io "
        "interface or use the isolated-margin endpoints instead"
    ),
    "UNIFIED_ACCOUNT_NOT_ACTIVATED": (
        "the account is not a unified account; upgrade it in the Gate.io "
        "interface or use the isolated-margin endpoints instead"
    ),
    "FORBIDDEN": (
        "the API key lacks the permission this ledger needs; grant it in the "
        "key's permission settings"
    ),
}


async def require_wallet[T](awaitable: Awaitable[T], description: str) -> T:
    """Await ``awaitable``, converting capability-gating errors into a clear one.

    Gate.io reports "this wallet does not exist yet", "this account is not in the
    required mode" and "this key lacks permission" as ordinary 4xx errors with
    distinctive labels. All three are configuration states rather than failures,
    and for a caller that only wants to read a balance they have the same
    consequence: the ledger is unavailable and the previous figures stand.

    They are **not** the same fact, and the two are raised as two types, because
    for a caller that turns silence into a claim the difference decides whether
    an execution is invented. ``USER_NOT_FOUND`` says the wallet does not exist,
    so it holds nothing; that is an absence, and
    :class:`WalletNotProvisionedError` states it. The account-mode labels say the
    venue rejected the question, so nothing at all is known about the ledger;
    :class:`WalletQueryRefusedError` states that, and being a subclass it is
    still caught by every handler that legitimately treats both alike.

    Deciding this here rather than at the catch sites is the point. The label is
    the only place the two are distinguishable, a caller that re-derived the
    distinction from ``__cause__`` would be one call site away from getting it
    wrong, and a new call site would inherit "absence" as its default reading —
    which is the wrong way round for the one consequence that cannot be undone.

    Parameters
    ----------
    awaitable : Awaitable
        The pending API call.
    description : str
        Human-readable name of what was being read, used in the message.

    Raises
    ------
    WalletQueryRefusedError
        If the venue refused the query (an account-mode or permission label), so
        that nothing is known about the ledger.
    WalletNotProvisionedError
        If the venue reported the wallet as not yet created, which is a statement
        that it holds nothing.
    GateioError
        Unchanged, for every other failure.
    """
    try:
        return await awaitable
    except GateioClientError as exc:
        remedy = _REMEDIES.get(exc.label, "the account is not configured for this ledger")
        if exc.label in ACCOUNT_MODE_LABELS:
            raise WalletQueryRefusedError(
                f"{description} could not be read ({exc.label}): {remedy}"
            ) from exc
        if exc.label in WALLET_NOT_PROVISIONED_LABELS:
            raise WalletNotProvisionedError(
                f"{description} is unavailable ({exc.label}): {remedy}"
            ) from exc
        raise


class GateioMarginHttpAPI:
    """Isolated, cross and unified margin REST endpoints.

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
    async def guarded[T](awaitable: Awaitable[T], description: str) -> T:
        """Alias of :func:`require_wallet`, for use through an API instance."""
        return await require_wallet(awaitable, description)

    # -- isolated margin: balances and settings ----------------------------

    async def accounts(self, currency_pair: str | None = None) -> list[dict[str, Any]]:
        """``GET /margin/accounts`` — isolated margin balances, one entry per pair.

        Each entry has a ``base`` and a ``quote`` block of
        ``{currency, available, locked, borrowed, interest}``, plus ``leverage``,
        ``mmr`` (the pair's current maintenance margin rate) and ``locked``
        (whether the pair is frozen).

        ``borrowed`` is outstanding principal and ``interest`` is accrued but
        unpaid interest: both are liabilities, and ``available`` already includes
        borrowed funds (``available = margin + borrowed``), so treating
        ``available`` as owned equity overstates the account.

        An empty list means margin is enabled but no pair has been activated
        yet, which is not an error.
        """
        return await self._client.get(
            "/margin/accounts",
            params={"currency_pair": currency_pair},
            signed=True,
        )

    async def account_book(
        self,
        currency: str | None = None,
        currency_pair: str | None = None,
        limit: int | None = None,
        page: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /margin/account_book`` — the isolated margin ledger.

        Rows are ``{id, time, time_ms, currency, currency_pair, change, balance,
        type}``, where ``change`` is positive for transfers in and negative for
        transfers out and ``balance`` is the balance after the change.
        """
        params: dict[str, Any] = {
            "currency": currency,
            "currency_pair": currency_pair,
            "from": frm,
            "to": to,
            "page": page,
            "limit": limit,
        }
        return await self._client.get("/margin/account_book", params=params, signed=True)

    async def funding_accounts(self, currency: str | None = None) -> list[dict[str, Any]]:
        """``GET /margin/funding_accounts`` — the lending side of margin.

        Fields per currency: ``available`` (assets that could be lent),
        ``locked`` (committed to open loan offers), ``lent`` (outstanding
        principal lent out) and ``total_lent = lent + locked``. This reports what
        the account has *lent*, the mirror image of the borrowing endpoints.
        """
        return await self._client.get(
            "/margin/funding_accounts",
            params={"currency": currency},
            signed=True,
        )

    async def transferable(self, currency: str, currency_pair: str | None = None) -> dict[str, Any]:
        """``GET /margin/transferable`` — the maximum transferable out of margin.

        Returns ``{currency, currency_pair, amount}``. The ceiling accounts for
        outstanding debt, so it is smaller than ``available`` whenever the pair
        carries a loan.
        """
        params: dict[str, Any] = {"currency": currency, "currency_pair": currency_pair}
        return await self._client.get("/margin/transferable", params=params, signed=True)

    async def auto_repay(self) -> dict[str, Any]:
        """``GET /margin/auto_repay`` — the isolated-margin auto-repay setting.

        Returns ``{"status": "on" | "off"}``. This is an account-level switch:
        isolated margin ignores the per-order ``auto_repay`` field entirely, so
        this setting alone decides whether proceeds from a filled margin order
        are applied to outstanding debt.
        """
        return await self._client.get("/margin/auto_repay", signed=True)

    async def set_auto_repay(self, status: str) -> dict[str, Any]:
        """``POST /margin/auto_repay`` — set the isolated-margin auto-repay switch.

        ``status`` is ``"on"`` or ``"off"``. Turning it off leaves borrowed
        principal outstanding and accruing interest after a position is closed,
        so debt persists until repaid explicitly.
        """
        return await self._client.post("/margin/auto_repay", params={"status": status})

    async def loan_margin_tiers(self, currency_pair: str | None = None) -> list[dict[str, Any]]:
        """``GET /margin/loan_margin_tiers`` — the public tiered margin table.

        Each tier gives ``upper_limit`` (the borrowing ceiling at that tier),
        ``mmr`` and ``leverage``: the more debt is carried, the lower the maximum
        permissible leverage. This endpoint needs no credentials — for the tiers
        as they apply to this account use :meth:`user_loan_margin_tiers`.
        """
        return await self._client.get(
            "/margin/loan_margin_tiers",
            params={"currency_pair": currency_pair},
        )

    async def user_loan_margin_tiers(
        self,
        currency_pair: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /margin/user/loan_margin_tiers`` — the account's own margin tiers.

        Same shape as :meth:`loan_margin_tiers`, resolved against the account's
        current leverage settings.
        """
        return await self._client.get(
            "/margin/user/loan_margin_tiers",
            params={"currency_pair": currency_pair},
            signed=True,
        )

    # -- isolated margin: borrowing ----------------------------------------

    async def currency_pairs(self) -> list[dict[str, Any]]:
        """``GET /margin/uni/currency_pairs`` — margin-enabled pairs.

        Each entry gives ``currency_pair``, ``leverage`` and the per-side
        minimum borrow amounts ``base_min_borrow_amount`` /
        ``quote_min_borrow_amount``. This is the current endpoint; the older
        ``GET /margin/currency_pairs`` is deprecated.
        """
        return await self._client.get("/margin/uni/currency_pairs")

    async def currency_pair(self, currency_pair: str) -> dict[str, Any]:
        """``GET /margin/uni/currency_pairs/{currency_pair}`` — one margin pair."""
        return await self._client.get(f"/margin/uni/currency_pairs/{currency_pair}")

    async def estimate_rate(self, currencies: list[str] | str) -> dict[str, str]:
        """``GET /margin/uni/estimate_rate`` — estimated hourly borrow rates.

        At most ten currencies per call. Gate.io recalculates rates hourly from
        lending depth, so the returned figures are estimates and the rate
        actually charged may differ.
        """
        value = ",".join(currencies) if isinstance(currencies, list) else currencies
        return await self._client.get(
            "/margin/uni/estimate_rate",
            params={"currencies": value},
            signed=True,
        )

    async def borrowable(self, currency: str, currency_pair: str) -> dict[str, Any]:
        """``GET /margin/uni/borrowable`` — maximum borrowable for an isolated pair.

        Both parameters are required. Returns
        ``{currency, currency_pair, borrowable}``. This is a quote of available
        credit; it does not create debt on its own.
        """
        params: dict[str, Any] = {"currency": currency, "currency_pair": currency_pair}
        return await self._client.get("/margin/uni/borrowable", params=params, signed=True)

    async def loans(
        self,
        currency: str | None = None,
        currency_pair: str | None = None,
        page: int | None = None,
        limit: int | None = None,
        type: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /margin/uni/loans`` — outstanding isolated-margin loans.

        ``amount`` on each row is the amount still to repay. ``type`` filters
        between ``platform`` and ``margin`` borrowing.
        """
        params: dict[str, Any] = {
            "currency": currency,
            "currency_pair": currency_pair,
            "page": page,
            "limit": limit,
            "type": type,
        }
        return await self._client.get("/margin/uni/loans", params=params, signed=True)

    async def borrow_or_repay(self, body: dict[str, Any]) -> Any:
        """``POST /margin/uni/loans`` — **borrows or repays**; borrowing creates debt.

        Body fields: ``currency``, ``currency_pair`` (isolated margin is
        per-pair), ``type`` (the loan type, ``"margin"``), ``amount`` (the
        borrow or repayment amount) and ``repaid_all`` (repayment only — when
        true it overrides ``amount`` and clears the position's whole debt).
        The endpoint returns no body.

        **This call can create a liability.** A borrow accrues interest from the
        moment it is drawn, is reported as ``borrowed`` plus ``interest`` on
        :meth:`accounts`, and Gate.io may liquidate the pair's collateral to
        recover it. Repayment is either automatic — governed by the
        account-level :meth:`auto_repay` switch, not by any per-order field — or
        explicit through this endpoint.

        Note that Gate.io's schema carries no explicit borrow-versus-repay
        discriminator here: unlike the unified-account variant, ``type`` names
        the loan kind rather than the direction. Confirm the exact request the
        venue expects before wiring an automated repayment path.
        """
        return await self._client.post("/margin/uni/loans", body=body)

    async def loan_records(
        self,
        currency: str | None = None,
        currency_pair: str | None = None,
        type: str | None = None,
        page: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /margin/uni/loan_records`` — isolated borrow and repay history.

        Rows are ``{type: "borrow" | "repay", currency_pair, currency, amount,
        create_time}``.
        """
        params: dict[str, Any] = {
            "currency": currency,
            "currency_pair": currency_pair,
            "type": type,
            "page": page,
            "limit": limit,
        }
        return await self._client.get("/margin/uni/loan_records", params=params, signed=True)

    async def interest_records(
        self,
        currency: str | None = None,
        currency_pair: str | None = None,
        page: int | None = None,
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /margin/uni/interest_records`` — interest charged on isolated loans.

        Rows carry ``currency``, ``currency_pair``, ``actual_rate``,
        ``interest``, ``status`` (``1`` success, ``0`` failure) and
        ``create_time``. This is the authoritative record of the cost of
        borrowing.
        """
        params: dict[str, Any] = {
            "currency": currency,
            "currency_pair": currency_pair,
            "page": page,
            "limit": limit,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/margin/uni/interest_records", params=params, signed=True)

    # -- cross margin ------------------------------------------------------

    async def cross_accounts(self) -> dict[str, Any]:
        """``GET /margin/cross/accounts`` — the classic cross-margin account.

        Gate.io has largely retired classic cross margin: this path is absent
        from the venue's own SDK and from the current documentation, yet the
        production server still routes it — it answers with an account-mode
        error on accounts that have not been upgraded, rather than with a
        routing error. Treat the payload as unstable and prefer
        :meth:`unified_accounts` where the account supports it.

        Wrap the call in :func:`require_wallet` to turn "not a unified account"
        into an actionable message rather than a hard failure.
        """
        return await self._client.get("/margin/cross/accounts", signed=True)

    async def cross_loans(
        self,
        status: str,
        currency: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        reverse: bool | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /margin/cross/loans`` — cross-margin borrow history.

        ``status`` is required. Rows carry ``amount`` (principal borrowed),
        ``repaid``, ``repaid_interest`` and ``unpaid_interest``; the timestamps
        here are milliseconds, unlike most of the API. Gate.io notes that
        ``status`` is effectively deprecated and now reports ``2`` for every
        record.
        """
        params: dict[str, Any] = {
            "status": status,
            "currency": currency,
            "limit": limit,
            "offset": offset,
            "reverse": reverse,
        }
        return await self._client.get("/margin/cross/loans", params=params, signed=True)

    async def cross_repayments(
        self,
        currency: str | None = None,
        loan_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /margin/cross/repayments`` — cross-margin repayment history.

        Rows carry ``principal``, ``interest`` and ``repayment_type``
        (``none``, ``manual_repay`` or ``auto_repay``, the last meaning an
        automatic repayment after a cancellation).
        """
        params: dict[str, Any] = {
            "currency": currency,
            "loan_id": loan_id,
            "limit": limit,
            "offset": offset,
        }
        return await self._client.get("/margin/cross/repayments", params=params, signed=True)

    # -- unified account ---------------------------------------------------

    async def unified_accounts(self, currency: str | None = None) -> dict[str, Any]:
        """``GET /unified/accounts`` — the unified (portfolio) account.

        Returns one object with a ``balances`` map keyed by currency, plus the
        account-level aggregates ``unified_account_total``,
        ``unified_account_total_equity``, ``unified_account_total_liab``,
        ``total_initial_margin``, ``total_maintenance_margin`` and
        ``total_available_margin``.

        Field validity depends on ``mode``: the borrowing and margin aggregates
        are only meaningful in ``multi_currency`` and ``portfolio`` modes and
        report zero in ``single_currency``, and ``options_order_loss`` is
        populated only in ``portfolio`` mode. Read ``mode`` first and interpret
        the rest accordingly.

        Per-currency liabilities are ``borrowed``, ``negative_liab`` (debt
        created by a negative balance) and ``total_liab``.
        """
        return await self._client.get(
            "/unified/accounts",
            params={"currency": currency},
            signed=True,
        )

    async def unified_account_mode(self) -> dict[str, Any]:
        """``GET /unified/unified_mode`` — the account's unified mode.

        Returns ``{"mode": ..., "settings": {...}}`` where ``mode`` is
        ``classic``, ``single_currency``, ``multi_currency`` or ``portfolio``.
        ``classic`` means the account has not been upgraded, so orders must not
        be routed with ``account="unified"`` and the rest of the ``/unified/*``
        tree is unavailable.

        This endpoint is the correct capability probe at start-up. Note the path
        is ``unified_mode``, not ``account_mode``.
        """
        return await self._client.get("/unified/unified_mode", signed=True)

    async def set_unified_account_mode(
        self,
        mode: str,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """``PUT /unified/unified_mode`` — switch the account's unified mode.

        ``mode`` is one of ``classic``, ``single_currency``, ``multi_currency``
        or ``portfolio``; ``settings`` carries the optional switches
        ``usdt_futures``, ``spot_hedge``, ``use_funding`` and ``options``.

        **This changes how the whole account is margined and can create debt
        capacity that did not exist before.** In cross-currency margin mode the
        ``usdt_futures`` and ``options`` switches can be enabled but not
        disabled again. Changing mode is an account-wide operation and should
        never be a side effect of connecting a trading client.
        """
        body: dict[str, Any] = {"mode": mode}
        if settings is not None:
            body["settings"] = settings
        return await self._client.put("/unified/unified_mode", body=body)

    async def unified_borrowable(self, currency: str) -> dict[str, Any]:
        """``GET /unified/borrowable`` — maximum borrowable in the unified account.

        Returns ``{currency, amount}``. A quote of available credit; it does not
        create debt on its own.
        """
        return await self._client.get(
            "/unified/borrowable",
            params={"currency": currency},
            signed=True,
        )

    async def unified_transferable(self, currency: str) -> dict[str, Any]:
        """``GET /unified/transferable`` — maximum transferable out of the unified account."""
        return await self._client.get(
            "/unified/transferable",
            params={"currency": currency},
            signed=True,
        )

    async def unified_loans(
        self,
        currency: str | None = None,
        page: int | None = None,
        limit: int | None = None,
        type: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /unified/loans`` — outstanding unified-account loans.

        ``limit`` defaults to 100 and is capped at 100. ``type`` filters between
        ``platform`` and ``margin`` borrowing.
        """
        params: dict[str, Any] = {
            "currency": currency,
            "page": page,
            "limit": limit,
            "type": type,
        }
        return await self._client.get("/unified/loans", params=params, signed=True)

    async def unified_borrow_or_repay(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /unified/loans`` — **borrows or repays**; borrowing creates debt.

        Body fields: ``currency``, ``type`` (``"borrow"`` or ``"repay"`` — the
        unified variant does carry an explicit direction), ``amount``,
        ``repaid_all`` (repayment only; overrides ``amount`` and clears the whole
        debt) and ``text``. Returns ``{"tran_id": ...}``.

        **This call can create a liability.** Interest is deducted from the
        account automatically at intervals, the borrowed amount must stay within
        the platform and user borrowing limits, and outstanding debt counts
        against the account's maintenance margin — a large enough position can
        be liquidated to recover it. Borrowing is only available in
        ``multi_currency`` and ``portfolio`` modes.

        Gate.io rate-limits this endpoint far more tightly than ordinary private
        calls (about 15 requests per 10 seconds), so it is not a path to poll.
        """
        return await self._client.post("/unified/loans", body=body)

    async def unified_loan_records(
        self,
        currency: str | None = None,
        type: str | None = None,
        page: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /unified/loan_records`` — unified borrow and repay history.

        Rows carry ``type`` (``borrow``/``repay``), ``borrow_type``
        (``manual_borrow``/``auto_borrow``) and ``repayment_type``, which
        distinguishes a manual repayment from an automatic one and from a
        cross-currency repayment.
        """
        params: dict[str, Any] = {
            "currency": currency,
            "type": type,
            "page": page,
            "limit": limit,
        }
        return await self._client.get("/unified/loan_records", params=params, signed=True)

    async def unified_interest_records(
        self,
        currency: str | None = None,
        page: int | None = None,
        limit: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /unified/interest_records`` — interest charged on unified loans."""
        params: dict[str, Any] = {
            "currency": currency,
            "page": page,
            "limit": limit,
            "from": frm,
            "to": to,
        }
        return await self._client.get("/unified/interest_records", params=params, signed=True)

    async def unified_risk_units(self) -> dict[str, Any]:
        """``GET /unified/risk_units`` — portfolio-margin risk units.

        Only meaningful in ``portfolio`` mode, where Gate.io groups instruments
        sharing an underlying into a single risk unit and margins them together.
        """
        return await self._client.get("/unified/risk_units", signed=True)
