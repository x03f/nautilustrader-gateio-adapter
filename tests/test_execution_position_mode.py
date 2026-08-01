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
act.

The refusal is also *fail-closed*, and that is a separate claim needing its own
tests: the client refuses everything it cannot read as one-way, rather than
matching the answer against a list of hedge spellings it happens to know. The
first version of this module asserted that property in prose and tested only
the documented spellings, so rewriting the check as a whitelist left all of it
green while ``position_mode: "dual_side"`` connected and opened private streams
against a hedge account. `TestTheRefusalIsFailClosed` is what actually holds it.

The last class documents what happens when the venue's answer does not settle
the question at all — including two cases that are neither a refusal nor a
diagnosable error, one of which is a genuine fail-open gap; see the module notes
on them there.
"""

from __future__ import annotations

from typing import Any

import pytest

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import (
    GateioClientError,
    GateioServerError,
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


def _start_and_capture_refusal(env: ExecHarness) -> BaseException | None:
    """Drive the real connect sequence and return the refusal, if any.

    The refusal is *returned* rather than expected, so that a client which no
    longer refuses is judged on what it went on to do — the stream it opened and
    the account it published — instead of only on a missing exception.
    """
    try:
        _connect(env)
    except RuntimeError as e:
        return e
    return None


def _assert_nothing_was_started(env: ExecHarness, refusal: BaseException | None) -> None:
    """The damage assertion: a hedged account leaves the client inert.

    This is the state the refutation of the first version of this module
    measured on a surviving mutant — connect completed and the private streams
    were open against a two-legged account — so it is the state every
    fail-closed case below asserts, not the wording of an exception.
    """
    assert env.opened_streams == [], (
        "a private stream was opened against an account whose position mode was "
        "not established as one-way; this client cannot represent two venue legs "
        "as one netted position"
    )
    assert env.account_states == [], (
        "an account this client cannot trade was published to the platform"
    )
    assert refusal is not None, "connect completed against an account that is not one-way"


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


# -- the refusal is fail-closed, not a list of known hedge spellings ----------


#: Position-mode answers this client has no basis for calling one-way.
#:
#: None of these appear in Gate.io's documented ``single`` / ``dual`` /
#: ``dual_plus`` set, and that is the point: the venue's own vocabulary has
#: already grown once (``dual_plus`` postdates ``dual``), spellings differ
#: between the futures and the unified endpoints, and a mode this client has
#: never heard of is a mode whose netting behaviour it cannot vouch for. The
#: first five are hedge in plain words; the rest are near-misses and shape
#: accidents — a separator swapped, a case, stray whitespace, a suffixed
#: variant, a mode invented after this file was written.
_MODES_THAT_ARE_NOT_ONE_WAY = [
    "dual_side",  # the exact value that traded a hedge account under a whitelist
    "double_side",
    "hedge",
    "both",
    "two_way",
    "dual-mode",
    "DUAL_SIDE",
    " dual ",
    "dual_plus_v2",
    "portfolio_margin_hedge",
]


class TestTheRefusalIsFailClosed:
    """Only the venue's one-way answer starts the client; everything else stops it.

    The first version of this module asserted that it pinned this property and
    did not: its parametrisation listed ``dual``, ``dual_plus`` and two casings
    of them, so replacing ``mode != "single"`` with the whitelist
    ``mode in ("dual", "dual_plus")`` passed every test while
    ``position_mode: "dual_side"`` connected and opened private streams against
    a hedge account. A refusal built from a list of known bad values is a
    refusal that is silently wrong about every value the venue adds next, and
    the direction of that error is the unrecoverable one: the client keeps
    trading, nets two venue legs into one platform position and reconciles
    against a model of the account that does not exist.

    So these tests say nothing about *which* spellings mean hedge. They say the
    check must treat an answer it cannot read as one-way exactly as it treats
    hedge — and each one asserts the damage, driving the real connect sequence
    and checking that no stream opened and no account was published.
    """

    @pytest.mark.parametrize("mode", _MODES_THAT_ARE_NOT_ONE_WAY)
    def test_a_mode_that_is_not_the_one_way_answer_refuses_the_whole_start(self, mode: str):
        """An unrecognised mode stops the start, with the legacy flag saying nothing.

        ``in_dual_mode`` is pinned to ``False`` deliberately: it is the other
        hedge signal, and leaving it out would let a check that reads only the
        legacy boolean — or one that reads neither field and refuses on
        something else entirely — pass this test for the wrong reason. The
        `position_mode` field alone must carry the refusal.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(
                position_mode=mode,
                in_dual_mode=False,
            )

            refusal = _start_and_capture_refusal(env)

            _assert_nothing_was_started(env, refusal)
            assert env.perp.called("accounts"), (
                "the start was refused without the position mode ever being read; "
                "this test would then pass against a check that refuses everything"
            )
            assert mode.lower() in str(refusal), (
                "the operator is not told which value the venue reported"
            )
        finally:
            env.close()

    @pytest.mark.parametrize("flag", [True, 1, "true", "dual"])
    def test_any_truthy_legacy_flag_refuses_whatever_shape_it_arrives_in(self, flag: Any):
        """The older field decides on truthiness, not on being exactly ``True``.

        ``position_mode`` here says ``single``, so the legacy flag is the only
        thing standing between the client and a hedge account. Gate.io serves
        this field as a JSON boolean today, but a check written as
        ``in_dual_mode is True`` would admit a hedge account the moment the
        field arrives stringly typed — which is how the same account is
        described elsewhere in the same API — and admitting it is the direction
        that costs money. Truthiness is the fail-closed reading.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(
                position_mode="single",
                in_dual_mode=flag,
            )

            refusal = _start_and_capture_refusal(env)

            _assert_nothing_was_started(env, refusal)
            assert "hedge/dual" in str(refusal)
        finally:
            env.close()

    @pytest.mark.parametrize(
        ("product", "settle"),
        [(GateioProductType.PERP, "usdt"), (GateioProductType.INVERSE, "btc")],
    )
    def test_each_perpetual_is_checked_on_its_own_wallet_and_no_other(
        self,
        product: GateioProductType,
        settle: str,
    ):
        """The gate must reach both perpetual products, each through its own wallet.

        Two failures hide behind one passing test otherwise: a check that runs
        only for the USDT-margined product clears a hedged BTC-margined account
        it never asked about, and a check that reads the mode off a hard-coded
        USDT wallet clears the same account by asking the wrong endpoint. Both
        end with the private stream open against a hedged account, so the
        product's own namespace being the one queried is asserted here, not
        inferred from the message.
        """
        env = _perp_harness(products=(product,))
        stub = {GateioProductType.PERP: env.perp, GateioProductType.INVERSE: env.inverse}
        try:
            stub[product].responses["accounts"] = _futures_wallet(
                position_mode="dual_side",
                in_dual_mode=False,
            )

            refusal = _start_and_capture_refusal(env)

            _assert_nothing_was_started(env, refusal)
            assert stub[product].called("accounts"), (
                f"the {product.value} wallet was never asked for its position mode"
            )
            for other, other_stub in stub.items():
                if other is not product:
                    assert not other_stub.called("accounts"), (
                        f"the {other.value} wallet was read while checking {product.value}"
                    )
            assert product.value in str(refusal)
            assert f"/futures/{settle}/dual_mode" in str(refusal)
        finally:
            env.close()

    def test_a_product_with_no_hedge_mode_skips_its_turn_and_not_the_rest(self):
        """Spot is exempt from the check; it does not exempt what comes after it.

        Spot has no position mode, so the loop steps over it — and a step that
        ends the loop instead of skipping one iteration leaves every product
        configured behind spot unchecked. The client would then start against a
        hedged perpetual wallet purely because of the order the operator listed
        products in, which is why the hedged perpetual sits second here.
        """
        env = _perp_harness(products=(GateioProductType.SPOT, GateioProductType.PERP))
        try:
            env.perp.responses["accounts"] = _futures_wallet(
                position_mode="dual_side",
                in_dual_mode=False,
            )

            refusal = _start_and_capture_refusal(env)

            _assert_nothing_was_started(env, refusal)
            assert env.perp.called("accounts"), (
                "the perpetual wallet behind the exempt spot product was never checked"
            )
            assert "PERP" in str(refusal)
        finally:
            env.close()

    @pytest.mark.parametrize(
        "failure",
        [
            GateioServerError(500, "SERVER_ERROR", "internal error"),
            TimeoutError("the wallet query timed out"),
        ],
        ids=["a-5xx", "a-timeout"],
    )
    def test_a_wallet_the_client_could_not_read_at_all_stops_the_start(
        self,
        failure: BaseException,
    ):
        """Only the two wallet-gating answers are allowed to skip the check.

        `_assert_one_way_position_mode` steps over a wallet that does not exist
        and one the venue refused to talk about, because those are statements
        about the account rather than failures — and both are pinned in
        `TestTheModeCannotBeDetermined` below. Nothing else is a statement about
        anything. A 5xx or a timed-out request means the mode is simply unknown,
        and widening that `except` to swallow it — the natural shape of a
        "startup should be resilient" change — clears the check on every account
        the venue happened to be slow about, hedged ones included.

        The assertion is therefore that the start does not survive it: no stream
        is opened, no account is published, and the error reaches the caller.

        The wallet fails **once** and answers normally afterwards, which is what
        a blip looks like and is the only version of this test that can fail.
        A wallet that failed on every call would raise the same error again at
        `_update_account_state` a moment later, so the start would abort either
        way and a check that had silently skipped itself would still look
        healthy here.
        """
        env = _perp_harness()
        try:
            attempts: list[int] = []

            def _fails_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
                attempts.append(1)
                if len(attempts) == 1:
                    raise failure
                return _futures_wallet()

            env.perp.responses["accounts"] = _fails_once

            aborted: BaseException | None = None
            try:
                _connect(env)
            except type(failure) as e:
                aborted = e

            assert env.opened_streams == [], (
                "the private stream was opened after the position mode check was "
                "skipped over an error that said nothing about the account"
            )
            assert env.account_states == []
            assert aborted is not None, (
                "a wallet the client could not read cleared the position mode check "
                "and the start continued"
            )
        finally:
            env.close()

    def test_a_hedged_wallet_last_in_the_tuple_still_refuses(self):
        """The loop must finish, not stop at the first product that looks fine.

        The USDT-margined wallet answers one-way and the BTC-margined one does
        not; a check that returns on the first clean answer starts a client
        whose inverse leg is hedged. This is the unrecognised spelling on
        purpose: the same case with a documented spelling is asserted above,
        and one test should not need two defects to fail.
        """
        env = _perp_harness(products=(GateioProductType.PERP, GateioProductType.INVERSE))
        try:
            env.perp.responses["accounts"] = _futures_wallet()
            env.inverse.responses["accounts"] = _futures_wallet(
                currency="BTC",
                position_mode="dual_side",
                in_dual_mode=False,
            )

            refusal = _start_and_capture_refusal(env)

            _assert_nothing_was_started(env, refusal)
            assert "INVERSE" in str(refusal)
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

    @pytest.mark.parametrize("blank", [None, "", 0, False])
    def test_a_blank_position_mode_is_read_as_one_way_too(self, blank: Any):
        """The one place the check is **not** fail-closed, stated so it is visible.

        Everywhere else in this module an answer the client cannot read as
        one-way stops the start. Here it does not: the ``or "single"`` in
        `_assert_one_way_position_mode` turns "the venue said nothing about the
        position mode" into "the venue said one-way", and a wallet that answers
        ``position_mode: null`` — which is what a partially provisioned futures
        account and a proxy that drops unknown fields both produce — starts a
        client that never established the mode at all. The legacy flag is left
        false here because it is absent in exactly the same situations.

        This test records the hole; it does not endorse it. Closing it means
        refusing on a blank answer, which is a behaviour change in
        `nautilus_gateio/execution.py` and belongs in a commit with a CHANGELOG
        entry. When that lands, **delete this test** — do not weaken it into
        accepting either outcome, because a test that passes both ways is how
        the property got lost the first time.
        """
        env = _perp_harness()
        try:
            env.perp.responses["accounts"] = _futures_wallet(
                position_mode=blank,
                in_dual_mode=False,
            )

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
