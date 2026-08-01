"""The connect-time refusal of a hedge (dual) position-mode account.

Nautilus nets positions per instrument: ``OmsType.NETTING`` keeps one net
position per ``InstrumentId``, so a venue that holds a separate long leg and
short leg for the same contract has state the platform has no way to represent.
The remedy is a venue-side account setting, and this client never changes an
operator's account settings, so the only correct behaviour left is to refuse to
start and say what to change.

That refusal is a *documented capability* — README "Position mode is one-way
(`OmsType.NETTING`). Hedge mode is detected at connect and refused with an
explanatory error, never switched off for you", `docs/execution.md` "unsupported
— detected at connect and refused with an explanatory error" — and until this
module it was the only such claim resting on nothing but a reading of the
source. A refusal nobody tests is a refusal a refactor deletes silently, and the
damage is not a stack trace: it is a node that starts, trades, and reconciles a
two-legged account against a one-legged model.

So the assertions here are about the damage, not about the shape of the check:
a hedge account must leave the client with **no private stream open and no
account state published**, and the message must carry enough for an operator to
act. The last class documents what happens when the venue's answer does not
settle the question at all — including one case that is not a refusal and not a
diagnosable error either; see the module notes on it there.
"""

from __future__ import annotations

from typing import Any

import pytest

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import (
    GateioClientError,
    WalletNotProvisionedError,
    WalletQueryRefusedError,
)
from tests.test_execution_orders import ExecHarness


def _futures_wallet(**overrides: Any) -> dict[str, Any]:
    """A futures wallet in the shape `GET /futures/{settle}/accounts` returns.

    Only the two fields carrying the position mode matter here; the rest is the
    minimum `_update_account_state` needs to publish a balance. Structural and
    sanitised — round figures, nothing that identifies an account.
    """
    wallet = {
        "currency": "USDT",
        "total": "1000",
        "available": "1000",
        "unrealised_pnl": "0",
        "position_mode": "single",
        "in_dual_mode": False,
    }
    wallet.update(overrides)
    return wallet


def _perp_harness(
    products: tuple[GateioProductType, ...] = (GateioProductType.PERP,),
) -> ExecHarness:
    """A client whose connect can be driven offline up to the websocket step.

    Only the two steps that follow the position-mode gate and genuinely need the
    outside world are replaced: opening the private stream (a socket) and the
    platform's own wait for the account to appear in a cache no exec engine is
    feeding here (`live/execution_client.py:534-567`, a 30 s poll). Everything
    the gate itself depends on — the products, the futures REST namespace, the
    order of the connect sequence — is the real client.
    """
    env = ExecHarness(products=products)
    env.opened_streams: list[GateioProductType] = []  # type: ignore[attr-defined]

    async def _record_stream(product: GateioProductType) -> None:
        env.opened_streams.append(product)  # type: ignore[attr-defined]

    async def _account_registered(*args: Any, **kwargs: Any) -> None:
        return None

    env.client._connect_private_websocket = _record_stream  # type: ignore[method-assign]
    env.client._await_account_registered = _account_registered  # type: ignore[method-assign]
    env.wallet.responses["fee"] = {"user_id": 1}
    for api in (env.perp, env.inverse, env.delivery):
        api.responses["positions"] = []
    return env


def _connect(env: ExecHarness) -> None:
    env.run(env.client._connect())
    env.run(env.client._disconnect())  # cancels the account poll task


# -- a hedge-mode account is refused, on every product that has one -----------


class TestHedgeModeAccountIsRefused:
    """Start fails, nothing is opened, and the operator is told what to change."""

    def test_connect_refuses_a_dual_mode_perpetual_account(self):
        """The damage assertion: no stream, no account state, no trading.

        Without the refusal this connect completes, the private stream opens and
        the node goes on to net two venue legs into one platform position.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(
                position_mode="dual",
                in_dual_mode=True,
            )

            # The damage is asserted before the exception is, so that a client
            # which no longer refuses fails this test by naming what it did.
            refusal: BaseException | None = None
            try:
                _connect(env)
            except RuntimeError as e:
                refusal = e

            assert env.opened_streams == [], (
                "the private stream was opened against a hedge-mode account; this "
                "client cannot represent its two legs as one netted position"
            )
            assert env.account_states == [], (
                "an account this client declared it cannot trade was published to the platform"
            )
            assert refusal is not None, "connect completed against a hedge-mode account"
            assert "position mode" in str(refusal)
        finally:
            env.close()

    def test_the_refusal_happens_before_any_account_state_is_published(self):
        """Order matters: the gate sits ahead of `_update_account_state`.

        A refusal that ran after the account had been published would leave the
        platform holding a balance from an account this client just declared it
        cannot trade.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(position_mode="dual")

            with pytest.raises(RuntimeError):
                _connect(env)

            assert env.account_states == []
            assert env.client._futures_http[GateioProductType.PERP].called("accounts")
        finally:
            env.close()

    @pytest.mark.parametrize("mode", ["dual", "dual_plus", "DUAL", "Dual_Plus"])
    def test_every_non_single_position_mode_is_refused(self, mode: str):
        """Gate.io reports `single`, `dual` and `dual_plus`, and case is not promised.

        `GateioFuturesHttpAPI.dual_mode` documents the field as
        ``single`` / ``dual`` / ``dual_plus``; anything that is not one-way is
        refused rather than matched against a list of known hedge spellings.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(position_mode=mode)

            with pytest.raises(RuntimeError) as excinfo:
                env.run(env.client._assert_one_way_position_mode())

            assert mode.lower() in str(excinfo.value)
        finally:
            env.close()

    def test_the_legacy_boolean_alone_is_enough_to_refuse(self):
        """`in_dual_mode` is the older field and it is not ignored.

        An account answering ``position_mode: "single"`` while the legacy flag
        says dual is a contradiction, and the safe reading of a contradiction
        about hedge mode is hedge mode.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(
                position_mode="single",
                in_dual_mode=True,
            )

            with pytest.raises(RuntimeError) as excinfo:
                env.run(env.client._assert_one_way_position_mode())

            assert "hedge/dual" in str(excinfo.value)
        finally:
            env.close()

    def test_inverse_perpetuals_are_checked_on_their_own_wallet(self):
        """Each perpetual product has its own settlement wallet and its own mode.

        BTC-margined perpetuals settle under ``btc``; a check that only ever read
        the USDT wallet would clear an inverse account it never asked about, so
        the refusal has to name that product's own `dual_mode` endpoint.
        """
        env = _perp_harness(products=(GateioProductType.INVERSE,))
        try:
            env.inverse.responses["accounts"] = _futures_wallet(
                currency="BTC",
                position_mode="dual",
            )

            with pytest.raises(RuntimeError) as excinfo:
                env.run(env.client._assert_one_way_position_mode())

            message = str(excinfo.value)
            assert "INVERSE" in message
            assert "/futures/btc/dual_mode" in message
        finally:
            env.close()

    def test_one_hedged_wallet_refuses_a_multi_product_start(self):
        """A client trading several products is refused if any of them is hedged.

        There is no partial start: the platform would otherwise be told about an
        account whose futures leg this client cannot represent.
        """
        env = _perp_harness(products=(GateioProductType.PERP, GateioProductType.INVERSE))
        try:
            env.perp.responses["accounts"] = _futures_wallet()
            env.inverse.responses["accounts"] = _futures_wallet(
                currency="BTC",
                position_mode="dual",
            )

            refusal: BaseException | None = None
            try:
                _connect(env)
            except RuntimeError as e:
                refusal = e

            assert env.opened_streams == [], (
                "a hedged inverse wallet did not stop the USDT-margined half of "
                "the same client from starting"
            )
            assert refusal is not None
            assert "INVERSE" in str(refusal)
        finally:
            env.close()

    def test_the_message_says_what_to_change_and_the_client_changes_nothing(self):
        """The promise: "refused with an explanatory error, never switched off for you".

        Both halves are asserted together because they are one promise: the
        message must be actionable *because* the client will not act itself.
        `POST /futures/{settle}/dual_mode` exists on the REST layer
        (`GateioFuturesHttpAPI.set_dual_mode`) and must stay unused here.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(position_mode="dual")

            with pytest.raises(RuntimeError) as excinfo:
                env.run(env.client._assert_one_way_position_mode())

            message = str(excinfo.value)
            assert "one-way" in message  # what the client needs instead
            assert "Close all positions and pending orders" in message  # the precondition
            assert "/futures/usdt/dual_mode?dual_mode=false" in message  # how to change it
            assert not env.perp.called("set_dual_mode")
            assert not env.inverse.called("set_dual_mode")
        finally:
            env.close()


# -- a one-way account starts, and the products without a hedge mode are exempt


class TestOneWayAccountStarts:
    """The gate must let the supported configuration through untouched."""

    def test_connect_completes_for_a_single_mode_account(self):
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet()

            _connect(env)

            assert env.opened_streams == [GateioProductType.PERP]
            assert len(env.account_states) == 1
        finally:
            env.close()

    def test_an_account_omitting_the_legacy_flag_still_starts(self):
        """Only `position_mode` is documented as current; its absence is not hedge."""
        env = _perp_harness()
        try:
            wallet = _futures_wallet()
            del wallet["in_dual_mode"]
            env.perp.responses["accounts"] = wallet

            env.run(env.client._assert_one_way_position_mode())  # must not raise
        finally:
            env.close()

    @pytest.mark.parametrize(
        "product",
        [GateioProductType.SPOT, GateioProductType.FUT, GateioProductType.OPT],
    )
    def test_products_without_a_hedge_mode_are_not_queried(
        self,
        product: GateioProductType,
    ):
        """Spot, delivery futures and options have no dual mode to check.

        Asserting the wallet is never read, rather than only that nothing raises:
        the exemption is documented in `docs/products.md` as "Not applicable",
        and a check that queried a delivery wallet would fail a perfectly valid
        start on an unprovisioned one.
        """
        env = _perp_harness(products=(product,))
        try:
            for api in (env.perp, env.inverse, env.delivery, env.spot, env.options):
                api.responses["accounts"] = _futures_wallet(position_mode="dual")

            env.run(env.client._assert_one_way_position_mode())  # must not raise

            assert not env.perp.called("accounts")
            assert not env.inverse.called("accounts")
            assert not env.delivery.called("accounts")
            assert not env.options.called("accounts")
        finally:
            env.close()


# -- when the venue's answer does not settle the question ---------------------


class TestTheModeCannotBeDetermined:
    """What the current code *does* when the answer is missing or malformed.

    These tests state observed behaviour, not endorsed behaviour. Two of the
    three outcomes below let a start proceed without the position mode ever
    having been established; that is recorded here so a change to it is a
    deliberate change with a failing test, rather than an accident.
    """

    def test_an_unprovisioned_wallet_skips_the_check_and_starts(self):
        """A wallet that does not exist holds no positions, so there is nothing to hedge."""
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = GateioClientError(
                400,
                "USER_NOT_FOUND",
                "user not found",
            )

            env.run(env.client._assert_one_way_position_mode())  # must not raise
        finally:
            env.close()

    def test_a_refused_wallet_query_also_skips_the_check_and_starts(self, log_capture):
        """A *refusal* is not an absence, and here the two are handled alike.

        `require_wallet` deliberately splits them —
        `WalletQueryRefusedError` ("the venue rejected the question, so nothing
        at all is known about the ledger") is raised for the account-mode and
        permission labels and is a *subclass* of `WalletNotProvisionedError`
        ("the wallet does not exist, so it holds nothing"). Catching the base
        class here therefore catches both, and a `FORBIDDEN` on the futures
        wallet lets the client start against an account whose position mode was
        never established. The one thing the operator does get is the warning
        line asserted below.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = GateioClientError(403, "FORBIDDEN", "forbidden")
            assert issubclass(WalletQueryRefusedError, WalletNotProvisionedError)

            log_capture.mark()
            env.run(env.client._assert_one_way_position_mode())  # must not raise
            lines = log_capture.wait_for("Skipping position mode check")

            assert any("Skipping position mode check for PERP" in line for line in lines)
        finally:
            env.close()

    def test_an_empty_account_object_is_read_as_one_way(self):
        """An answer carrying neither field starts the client as if it were one-way.

        `str(None or "single").lower()` is `"single"`, so a response that says
        nothing about the position mode is indistinguishable here from one that
        says one-way. Documented, not endorsed.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = {}

            env.run(env.client._assert_one_way_position_mode())  # must not raise
        finally:
            env.close()

    def test_a_response_that_is_not_an_object_aborts_the_start_undiagnosably(self):
        """A non-object answer fails the start with `AttributeError`, not a message.

        Aborting is the safe direction — nothing starts against an unknown
        position mode — but the operator gets `'list' object has no attribute
        'get'` out of `_connect` rather than a sentence naming the wallet and the
        endpoint, which is what every other unreadable-wallet path in this client
        produces.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = []  # a list where the venue returns an object

            with pytest.raises(AttributeError):
                env.run(env.client._assert_one_way_position_mode())
        finally:
            env.close()
