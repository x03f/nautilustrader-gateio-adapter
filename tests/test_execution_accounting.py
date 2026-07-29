"""Account state, balances and margins against what the platform means by each.

Every assertion here is about a number the platform will act on, not about the
shape of a particular implementation:

* ``Portfolio.equity()`` for a margin account is
  ``balances_total + sum(unrealized_pnl(open positions))``
  (portfolio/portfolio.pyx:1176-1243, concepts/portfolio.md "Equity formula"), so
  what an adapter puts in ``AccountBalance.total`` decides whether unrealised PnL
  is counted once or twice.
* ``MarginAccount`` keeps per-instrument and account-wide margins in separate
  stores and *replaces* both from every event
  (accounting/accounts/margin.pyx:505-521; concepts/accounting.md "Margin
  scopes"), so scope decides which query answers at all, and a partial snapshot
  deletes what it leaves out.
* A ``PositionStatusReport`` is a claim the venue made. The absence of one is
  read as flatness on an account-wide answer, and the FLAT fallback makes it a
  claim on a per-instrument one, so a query the venue *refused* must raise.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.common.component import MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OrderSide, PositionSide
from nautilus_trader.model.identifiers import PositionId, Venue
from nautilus_trader.model.objects import Currency, Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode
from nautilus_gateio.common.errors import GateioClientError, GateioError
from nautilus_gateio.execution import PositionStatusUnavailable
from tests.test_execution_orders import PERP_BTC_USDT, ExecHarness

GATEIO_VENUE = Venue("GATE_IO")
USDT = Currency.from_str("USDT")
BTC = Currency.from_str("BTC")

#: One cross-margined BTC_USDT perpetual, as `GET /futures/usdt/positions` returns
#: it. `leverage: "0"` is Gate.io's cross-margin marker; the cap lives in
#: `cross_leverage_limit`.
CROSS_POSITION = {
    "contract": "BTC_USDT",
    "size": 10000,
    "leverage": "0",
    "cross_leverage_limit": "10",
    "initial_margin": "300",
    "maintenance_margin": "15",
    "value": "3000",
    "entry_price": "30000",
}

#: The same position held isolated at 10x.
ISOLATED_POSITION = dict(CROSS_POSITION, leverage="10", cross_leverage_limit="0")


def _usdt(state):
    return next(b for b in state.balances if b.currency.code == "USDT")


def _futures_wallet(total: str, available: str, unrealised: str) -> dict:
    return {
        "currency": "USDT",
        "total": total,
        "available": available,
        "unrealised_pnl": unrealised,
    }


# -- unrealised PnL belongs to the Portfolio, not to the balance --------------


class TestUnrealisedPnlIsCountedOnce:
    """`AccountBalance.total` is the wallet balance; the Portfolio adds the PnL.

    Gate.io states of the futures `total`: "does not include upl of positions".
    That is the figure the platform wants, and the figure in-tree Binance reports
    (`walletBalance`, adapters/binance/futures/schemas/account.py:75-88). Adding
    `unrealised_pnl` to it makes `Portfolio.equity()` add the same PnL twice.
    """

    @staticmethod
    def _funded() -> ExecHarness:
        env = ExecHarness(products=(GateioProductType.PERP,))
        env.perp.responses["accounts"] = _futures_wallet("1000", "1000", "100")
        env.perp.responses["positions"] = []
        return env

    def test_total_is_the_wallet_balance(self):
        env = self._funded()
        try:
            env.run(env.client._update_account_state())

            assert _usdt(env.account_states[-1]).total.as_decimal() == Decimal("1000")
        finally:
            env.close()

    def test_unrealised_profit_is_not_published_as_locked_collateral(self):
        """Nothing is reserved here: no position margin, no open orders."""
        env = self._funded()
        try:
            env.run(env.client._update_account_state())

            usdt = _usdt(env.account_states[-1])
            assert usdt.locked.as_decimal() == Decimal(0)
            assert usdt.free.as_decimal() == Decimal("1000")
        finally:
            env.close()

    def test_portfolio_equity_adds_the_unrealised_pnl_once(self):
        """The end-to-end damage: a 1000 USDT account shows 1100, not 1200."""
        env = self._funded()
        try:
            env.run(env.client._update_account_state())

            portfolio = Portfolio(
                msgbus=MessageBus(trader_id=env.trader_id, clock=env.clock),
                cache=env.cache,
                clock=env.clock,
            )
            portfolio.update_account(env.account_states[-1])

            instrument = env.cache.instrument(PERP_BTC_USDT)
            order = env.order_factory.market(
                instrument_id=PERP_BTC_USDT,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(10000),  # x0.0001 multiplier = 1 BTC
            )
            fill = TestEventStubs.order_filled(
                order,
                instrument=instrument,
                last_px=Price.from_str("30000.0"),
                account_id=env.client.account_id,
                position_id=PositionId("P-1"),
            )
            env.cache.add_position(Position(instrument=instrument, fill=fill), oms_type=2)

            quote = QuoteTick(
                instrument_id=PERP_BTC_USDT,
                bid_price=Price.from_str("30100.0"),
                ask_price=Price.from_str("30100.0"),
                bid_size=Quantity.from_int(1),
                ask_size=Quantity.from_int(1),
                ts_event=0,
                ts_init=0,
            )
            env.cache.add_quote_tick(quote)
            portfolio.update_quote_tick(quote)

            # 1 BTC bought at 30000, marked at 30100 -> 100 USDT unrealised.
            assert portfolio.unrealized_pnls(GATEIO_VENUE)[USDT].as_decimal() == Decimal("100")
            assert portfolio.equity(GATEIO_VENUE)[USDT].as_decimal() == Decimal("1100")
        finally:
            env.close()

    def test_the_poll_and_the_balance_stream_state_the_same_wallet(self):
        """`futures.balances` carries the wallet balance; the poll must agree.

        Otherwise the published equity jumps by the unrealised PnL on every
        stream tick and snaps back on the next poll, and the account history is
        a sawtooth of a balance that never changed.
        """
        env = self._funded()
        try:
            env.run(env.client._update_account_state())
            after_poll = _usdt(env.account_states[-1]).total.as_decimal()

            env.client._handle_balance_payload(
                GateioProductType.PERP,
                {"currency": "USDT", "balance": "1000", "time_ms": 1_700_000_000_000},
            )
            after_tick = _usdt(env.account_states[-1]).total.as_decimal()

            assert after_poll == after_tick == Decimal("1000")
        finally:
            env.close()

    def test_options_equity_is_not_reported_as_the_balance(self):
        """`OptionsAccount.equity` is "balance + position value" — the same trap."""
        env = ExecHarness(products=(GateioProductType.OPT,))
        try:
            env.options.responses["account"] = {
                "currency": "USDT",
                "total": "5000",
                "equity": "5400",
                "unrealised_pnl": "400",
                "available": "5000",
            }
            env.options.responses["positions"] = []
            env.run(env.client._update_account_state())

            assert _usdt(env.account_states[-1]).total.as_decimal() == Decimal("5000")
        finally:
            env.close()


# -- a partial read is not a snapshot -----------------------------------------


class TestPartialWalletReads:
    """A poll that could not read every wallet must not restate the whole account."""

    @staticmethod
    def _unified() -> ExecHarness:
        env = ExecHarness(
            products=(GateioProductType.SPOT, GateioProductType.PERP),
            spot_account_mode=GateioSpotAccountMode.UNIFIED,
        )
        env.spot.responses["accounts"] = [
            {"currency": "USDT", "available": "1000", "locked": "0"},
        ]
        env.margin.responses["unified_accounts"] = {
            "balances": {
                "USDT": {
                    "available": "1000",
                    "freeze": "0",
                    "borrowed": "0",
                    "interest": "0",
                },
            },
        }
        env.perp.responses["accounts"] = _futures_wallet("1000", "1000", "0")
        env.perp.responses["positions"] = []
        return env

    def test_a_failed_unified_read_does_not_reinflate_the_aggregate(self):
        """The unified ledger is what stops the wallets being summed.

        Lose it, and the spot wallet and the futures wallet — both echoing the
        same 1000 USDT — are added together into 2000 USDT of reported equity,
        published as `reported=True` for the RiskEngine to size against.
        """
        env = self._unified()
        try:
            env.run(env.client._update_account_state())
            assert env.client._balances["USDT"][0] == Decimal("1000")

            env.margin.responses["unified_accounts"] = GateioError(
                502,
                "SERVER_ERROR",
                "bad gateway",
            )
            env.run(env.client._update_account_state())

            assert env.client._balances["USDT"][0] == Decimal("1000")
            assert _usdt(env.account_states[-1]).total.as_decimal() == Decimal("1000")
        finally:
            env.close()

    def test_an_unreadable_unified_ledger_on_the_first_poll_states_nothing(self):
        """With no unified snapshot ever taken, no aggregate can be stated."""
        env = self._unified()
        try:
            env.margin.responses["unified_accounts"] = GateioError(
                502,
                "SERVER_ERROR",
                "bad gateway",
            )
            env.run(env.client._update_account_state())

            assert env.account_states == []
        finally:
            env.close()

    def test_a_wallet_that_could_not_be_read_keeps_the_margin_it_last_reported(self):
        """`MarginAccount.apply` replaces its stores, so omission is deletion."""
        env = ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP))
        try:
            env.spot.responses["accounts"] = [
                {"currency": "USDT", "available": "1000", "locked": "0"},
            ]
            env.perp.responses["accounts"] = _futures_wallet("1000", "700", "0")
            env.perp.responses["positions"] = [CROSS_POSITION]
            env.run(env.client._update_account_state())
            assert env.account_states[-1].margins != []

            env.perp.responses["accounts"] = GateioError(502, "SERVER_ERROR", "bad gateway")
            env.run(env.client._update_account_state())

            margins = env.account_states[-1].margins
            assert margins != [], "the futures margin was deleted by a poll that never read it"
            assert margins[0].initial.as_decimal() == Decimal("300")
        finally:
            env.close()


# -- margin scope --------------------------------------------------------------


class TestMarginScope:
    """Per-instrument means isolated; account-wide means cross (concepts/accounting.md)."""

    @staticmethod
    def _account(env):
        """Build the real `MarginAccount` the platform would build from our event."""
        state = env.account_states[-1]
        assert state.account_type == AccountType.MARGIN
        return AccountFactory.create(state)

    def test_a_cross_margined_position_is_reported_account_wide(self):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.perp.responses["accounts"] = _futures_wallet("1000", "700", "0")
            env.perp.responses["positions"] = [CROSS_POSITION]
            env.run(env.client._update_account_state())

            account = self._account(env)
            margin = account.margin_init_for_currency(USDT)
            assert margin is not None, "a cross-margin venue reports account-wide collateral"
            assert margin.as_decimal() == Decimal("300")
            assert account.margin_init(PERP_BTC_USDT) is None
        finally:
            env.close()

    def test_an_isolated_position_is_reported_per_instrument(self):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.perp.responses["accounts"] = _futures_wallet("1000", "700", "0")
            env.perp.responses["positions"] = [ISOLATED_POSITION]
            env.run(env.client._update_account_state())

            account = self._account(env)
            assert account.margin_init(PERP_BTC_USDT).as_decimal() == Decimal("300")
            assert account.margin_init_for_currency(USDT) is None
        finally:
            env.close()

    def test_two_collateral_currencies_both_survive(self):
        """Account-wide entries are keyed by currency, so USDT and BTC coexist.

        Keyed under a single ``None`` instead, the second wallet read silently
        overwrites the first and one whole collateral disappears.
        """
        env = ExecHarness(products=(GateioProductType.PERP, GateioProductType.INVERSE))
        try:
            env.perp.responses["accounts"] = _futures_wallet("1000", "700", "0")
            env.perp.responses["positions"] = [CROSS_POSITION]
            env.inverse.responses["accounts"] = {
                "currency": "BTC",
                "total": "2",
                "available": "1.5",
                "unrealised_pnl": "0",
            }
            env.inverse.responses["positions"] = [
                dict(
                    CROSS_POSITION,
                    contract="BTC_USD",
                    initial_margin="0.5",
                    maintenance_margin="0.02",
                ),
            ]
            env.run(env.client._update_account_state())

            account = self._account(env)
            assert account.margin_init_for_currency(USDT).as_decimal() == Decimal("300")
            assert account.margin_init_for_currency(BTC).as_decimal() == Decimal("0.5")
        finally:
            env.close()

    def test_two_products_sharing_one_collateral_are_summed_not_replaced(self):
        """A USDT perpetual and the USDT options wallet are one collateral.

        The platform keys account-wide margin by currency, so publishing one
        entry per product for the same currency does not add them up — the
        second silently replaces the first, and half the account's reserved
        collateral disappears from every query.
        """
        env = ExecHarness(products=(GateioProductType.PERP, GateioProductType.OPT))
        try:
            env.perp.responses["accounts"] = _futures_wallet("1000", "700", "0")
            env.perp.responses["positions"] = [CROSS_POSITION]
            env.options.responses["account"] = {
                "currency": "USDT",
                "total": "500",
                "available": "300",
                "init_margin": "150",
                "order_margin": "50",
                "maint_margin": "20",
            }
            env.options.responses["positions"] = []
            env.run(env.client._update_account_state())

            account = self._account(env)
            assert account.margin_init_for_currency(USDT).as_decimal() == Decimal("500")
            assert account.margin_maint_for_currency(USDT).as_decimal() == Decimal("35")
        finally:
            env.close()

    def test_cross_positions_in_one_collateral_are_summed_not_replaced(self):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.perp.responses["accounts"] = _futures_wallet("1000", "400", "0")
            env.perp.responses["positions"] = [
                CROSS_POSITION,
                dict(
                    CROSS_POSITION,
                    contract="ETH_USDT",
                    initial_margin="200",
                    maintenance_margin="10",
                ),
            ]
            env.run(env.client._update_account_state())

            account = self._account(env)
            assert account.margin_init_for_currency(USDT).as_decimal() == Decimal("500")
            assert account.margin_maint_for_currency(USDT).as_decimal() == Decimal("25")
        finally:
            env.close()


# -- position reports state venue truth ---------------------------------------


REFUSAL_LABELS = ["FORBIDDEN", "INVALID_UNIFIED_ACCOUNT", "UNIFIED_ACCOUNT_NOT_ACTIVATED"]


def _refuse(env, label: str) -> None:
    env.perp.responses["position"] = GateioClientError(403, label, "not permitted")
    env.perp.responses["positions"] = GateioClientError(403, label, "not permitted")


def _ask(env, instrument_id):
    return env.run(
        env.client.generate_position_status_reports(
            GeneratePositionStatusReports(
                instrument_id=instrument_id,
                start=None,
                end=None,
                command_id=UUID4(),
                ts_init=env.clock.timestamp_ns(),
            ),
        ),
    )


class TestPositionQueryRefusal:
    """A refusal is not an answer, and never a FLAT position.

    Only ``USER_NOT_FOUND`` is a statement about positions — the wallet does not
    exist, so nothing is in it. ``FORBIDDEN`` and the unified-account labels say
    the key or the account mode may not look, which says nothing at all about
    what is open. Reading them as "no position" reaches the FLAT fallback and
    hands the engine a claim nobody made; the engine then squares the book
    against it with a RECONCILIATION order and an inferred fill, closing a
    position that is still open at the venue.

    ``require_wallet`` separates the two at the source, so the distinction is a
    type rather than a rule each catch site has to remember. These tests hold the
    behaviour at the client; ``tests/test_errors.py`` holds the typing that makes
    it the default.
    """

    @pytest.mark.parametrize("label", REFUSAL_LABELS)
    def test_a_refused_query_is_not_reported_as_flat(self, label):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            _refuse(env, label)

            with pytest.raises(PositionStatusUnavailable) as excinfo:
                _ask(env, PERP_BTC_USDT)

            assert label in str(excinfo.value)
        finally:
            env.close()

    @pytest.mark.parametrize("label", REFUSAL_LABELS)
    def test_a_refused_account_wide_sweep_is_not_reported_as_flat(self, label):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            _refuse(env, label)

            with pytest.raises(PositionStatusUnavailable):
                _ask(env, None)
        finally:
            env.close()

    def test_an_unprovisioned_wallet_still_answers_flat(self):
        """USER_NOT_FOUND is a definite absence: Gate.io has not created the wallet."""
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.perp.responses["position"] = GateioClientError(
                400,
                "USER_NOT_FOUND",
                "user has no futures account",
            )

            reports = _ask(env, PERP_BTC_USDT)

            assert [r.position_side for r in reports] == [PositionSide.FLAT]
        finally:
            env.close()

    def test_a_position_the_venue_holds_is_still_reported(self):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.perp.responses["position"] = dict(CROSS_POSITION, size=10000)

            reports = _ask(env, PERP_BTC_USDT)

            assert [r.position_side for r in reports] == [PositionSide.LONG]
            assert reports[0].quantity == Quantity.from_int(10000)
        finally:
            env.close()


# -- account state timestamps --------------------------------------------------


class TestAccountStateTimestamps:
    """`ts_event` is when the change happened; `ts_init` is when we saw it."""

    def test_a_stream_balance_carries_the_venue_timestamp(self):
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.client._handle_balance_payload(
                GateioProductType.PERP,
                {"currency": "USDT", "balance": "1000", "time_ms": 1_700_000_000_123},
            )

            state = env.account_states[-1]
            assert state.ts_event == 1_700_000_000_123_000_000
            assert state.ts_init > state.ts_event
        finally:
            env.close()

    def test_a_rest_snapshot_uses_the_local_clock(self):
        """A poll is a reading taken now; the venue attaches no time to it."""
        env = ExecHarness(products=(GateioProductType.PERP,))
        try:
            env.perp.responses["accounts"] = _futures_wallet("1000", "1000", "0")
            env.perp.responses["positions"] = []
            before = env.clock.timestamp_ns()
            env.run(env.client._update_account_state())

            state = env.account_states[-1]
            assert before <= state.ts_event <= state.ts_init
        finally:
            env.close()
