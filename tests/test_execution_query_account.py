"""The ``QueryAccount`` command, and what an incomplete answer to it costs.

Three facts about the platform decide everything asserted here, and all three
were read from installed 1.230.0 rather than from documentation:

* ``LiveExecutionClient`` overrides the public ``query_account`` and calls
  ``self._query_account(command)`` from it, but defines no ``_query_account`` of
  its own and does not list one among the coroutines a subclass must implement.
  An adapter that omits the method therefore does not "ignore the command": the
  attribute lookup raises ``AttributeError`` **synchronously**, inside
  ``query_account``, before a task exists, and ``ExecutionEngine`` calls that
  method without a guard. So the regression test has to go through the *public*
  method — a test that awaits the private coroutine cannot see the defect.
* ``generate_account_state(..., reported=True)`` asserts the balances "are
  reported directly from the exchange". Under a wallet that could not be read
  this client republishes that wallet's previous figures, deliberately (see
  ``_update_account_state``: dropping them would delete the product's margins,
  because ``MarginAccount.apply`` replaces rather than merges). For the
  background poll that trade is right. For a user command it means the caller
  cannot tell "nothing changed" from "nothing was read", so the client has to
  say which it was.
* ``WalletQueryRefusedError`` and ``WalletNotProvisionedError`` are not the same
  fact even though one derives from the other. The venue refusing the *question*
  leaves the ledger unknown; the venue reporting that the wallet does not exist
  is a complete answer that it holds nothing. Only the first is an unread wallet.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import QueryAccount

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioClientError, GateioServerError
from tests.test_execution_orders import ExecHarness

SPOT_WALLET = [{"currency": "USDT", "available": "1000", "locked": "0"}]
PERP_WALLET = {"currency": "USDT", "total": "500", "available": "500"}


def _funded(env: ExecHarness) -> ExecHarness:
    env.spot.responses["accounts"] = SPOT_WALLET
    env.perp.responses["accounts"] = PERP_WALLET
    env.perp.responses["positions"] = []
    return env


def _query(env: ExecHarness) -> None:
    """Issue the command the way the execution engine issues it.

    Through the public ``query_account``, which is where the missing coroutine
    is fatal, and then one turn of the loop so the task it created can run.
    """
    env.client.query_account(
        QueryAccount(
            trader_id=env.trader_id,
            account_id=env.client.account_id,
            command_id=UUID4(),
            ts_init=env.clock.timestamp_ns(),
        ),
    )
    env.run(asyncio.sleep(0))


def _usdt_total(state) -> Decimal:
    return next(b for b in state.balances if b.currency.code == "USDT").total.as_decimal()


class TestTheCommandIsServed:
    def test_a_query_publishes_an_account_state(self):
        env = _funded(ExecHarness(products=(GateioProductType.SPOT,)))
        try:
            _query(env)

            assert len(env.account_states) == 1
            assert _usdt_total(env.account_states[-1]) == Decimal("1000")
        finally:
            env.close()

    def test_a_query_reads_the_venue_rather_than_replaying_the_last_state(self):
        """The command means "ask Gate.io now", not "repeat what you last said"."""
        env = _funded(ExecHarness(products=(GateioProductType.SPOT,)))
        try:
            _query(env)
            env.spot.responses["accounts"] = [
                {"currency": "USDT", "available": "1234", "locked": "0"},
            ]
            _query(env)

            assert len(env.spot.calls_named("accounts")) == 2
            assert _usdt_total(env.account_states[-1]) == Decimal("1234")
        finally:
            env.close()


class TestAnIncompleteAnswerSaysSo:
    """The published state is unchanged; what changes is what the caller is told.

    Asserting that the ``AccountState`` is still published is as much the point
    as asserting the error: suppressing it would drop the margins of every
    product that *did* answer, which is the trade ``_update_account_state``
    argues out in full. The test encodes the trade actually chosen.
    """

    def test_a_wallet_that_could_not_be_read_is_named(self, log_capture):
        env = _funded(ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP)))
        env.perp.responses["accounts"] = GateioServerError(502, "SERVER_ERROR", "bad gateway")
        try:
            log_capture.mark()
            _query(env)

            assert len(env.account_states) == 1, "the state that could be stated is still published"
            lines = log_capture.wait_for("is incomplete")
            assert any(
                "[ERROR]" in line and "PERP wallet(s) could not be read" in line for line in lines
            ), lines
        finally:
            env.close()

    def test_a_wallet_the_venue_refused_to_answer_about_is_unread(self, log_capture):
        """``FORBIDDEN`` says nothing about the ledger, so the ledger is unknown.

        It arrives as ``WalletQueryRefusedError``, which derives from
        ``WalletNotProvisionedError`` — catching only the base class files a
        refusal as an absence and reports a complete sweep that never happened.
        """
        env = _funded(ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP)))
        env.perp.responses["accounts"] = GateioClientError(403, "FORBIDDEN", "no permission")
        try:
            log_capture.mark()
            _query(env)

            lines = log_capture.wait_for("is incomplete")
            assert any(
                "[ERROR]" in line and "PERP wallet(s) could not be read" in line for line in lines
            ), lines
        finally:
            env.close()

    def test_an_unprovisioned_wallet_is_not_reported_as_unread(self, log_capture):
        """``USER_NOT_FOUND`` is a complete answer: the wallet holds nothing.

        Counting it would make a normal account — spot funded, futures wallet
        never created — report an incomplete sweep on every single query.
        """
        env = _funded(ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP)))
        env.perp.responses["accounts"] = GateioClientError(400, "USER_NOT_FOUND", "no wallet")
        try:
            log_capture.mark()
            _query(env)

            assert len(env.account_states) == 1
            assert not any("is incomplete" in line for line in log_capture.since())
        finally:
            env.close()

    def test_a_total_outage_still_publishes_and_still_reports_it(self, log_capture):
        """Every wallet unreadable: the last figures stand, and the caller is told.

        Deliberately does *not* assert that publication is suppressed. The
        alternative was argued and rejected in ``_update_account_state``, and a
        test asserting the nicer-sounding behaviour would encode a decision this
        client did not make.
        """
        env = _funded(ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP)))
        try:
            _query(env)
            assert len(env.account_states) == 1

            env.spot.responses["accounts"] = GateioServerError(503, "SERVER_ERROR", "down")
            env.perp.responses["accounts"] = GateioServerError(503, "SERVER_ERROR", "down")
            log_capture.mark()
            _query(env)

            assert len(env.account_states) == 2
            assert _usdt_total(env.account_states[-1]) == Decimal("1500")
            lines = log_capture.wait_for("is incomplete")
            assert any(
                "[ERROR]" in line and "PERP, SPOT wallet(s) could not be read" in line
                for line in lines
            ), lines
        finally:
            env.close()


class TestTheSweepReportsWhatItCouldNotRead:
    """The return value the poll ignores and the query needs.

    Every existing caller of ``_update_account_state`` discards it, which is why
    the sweep may keep publishing exactly as before; the value exists so that one
    caller can distinguish an answer from a restatement.
    """

    def test_a_complete_sweep_reports_nothing_unread(self):
        env = _funded(ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP)))
        try:
            assert env.run(env.client._update_account_state()) == frozenset()
        finally:
            env.close()

    @pytest.mark.parametrize(
        "error",
        [
            GateioServerError(502, "SERVER_ERROR", "bad gateway"),
            GateioClientError(403, "FORBIDDEN", "no permission"),
        ],
        ids=["transport-failure", "query-refused"],
    )
    def test_an_unreadable_wallet_is_reported(self, error: Exception):
        env = _funded(ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP)))
        env.perp.responses["accounts"] = error
        try:
            unread = env.run(env.client._update_account_state())

            assert unread == frozenset({GateioProductType.PERP})
        finally:
            env.close()

    def test_an_unprovisioned_wallet_is_not_reported(self):
        env = _funded(ExecHarness(products=(GateioProductType.SPOT, GateioProductType.PERP)))
        env.perp.responses["accounts"] = GateioClientError(400, "USER_NOT_FOUND", "no wallet")
        try:
            assert env.run(env.client._update_account_state()) == frozenset()
        finally:
            env.close()
