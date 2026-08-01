"""The events this execution client may, must and must not generate.

An adapter's event surface is defined by the platform, not by the venue: an
``ExecutionClient`` publishes order and account events through a fixed set of
``generate_*`` methods, and everything else in the event model is produced
somewhere else in NautilusTrader. This module pins that surface against the
**installed** platform, so the answer to "which events do we never emit, and is
that correct?" is checkable rather than remembered.

Two kinds of failure are wanted here:

* the platform grows an event generator this client does not handle
  (``OrderFillVoided`` is the one already known to be coming), and
* this client starts producing an event that is not its to produce.

Sources: ``nautilus_trader/execution/client.pyx`` (the generator list),
``nautilus_trader/model/orders/base.pyx`` (the state table), and
``docs/concepts/events/`` for what each event means.
"""

from __future__ import annotations

import ast
import functools
import inspect
import textwrap

import pytest
from nautilus_trader.execution.client import ExecutionClient
from nautilus_trader.live.execution_client import LiveExecutionClient

from gateio_nt.execution import GateioExecutionClient

#: ``generate_*`` methods that build execution **reports** for reconciliation
#: rather than publishing an event. They answer a request from the engine and
#: are covered by the reconciliation tests, not by this module.
REPORT_GENERATORS: frozenset[str] = frozenset(
    {
        "generate_fill_reports",
        "generate_mass_status",
        "generate_order_status_report",
        "generate_order_status_reports",
        "generate_position_status_reports",
    },
)

#: Every event an ``ExecutionClient`` can publish in the installed 1.230.0, and
#: what makes this client emit it. The list is the platform's, in full: if a
#: generator is missing from here the platform has changed and the omission is a
#: decision somebody has to make, not a detail to be discovered later.
EVENT_GENERATORS: dict[str, str] = {
    "generate_account_state": "balance stream and the REST account poll",
    "generate_order_denied": "a refusal decided from the order alone, before submission",
    "generate_order_submitted": "the request is about to go to Gate.io",
    "generate_order_rejected": "Gate.io refused the submission, or finished it as `poc`",
    "generate_order_accepted": "the order rests at the venue, or a trigger was armed",
    "generate_order_modify_rejected": "an amend Gate.io refused, or a local pre-flight failure",
    "generate_order_cancel_rejected": "a cancel Gate.io refused, or a local pre-flight failure",
    "generate_order_updated": "an amend the venue applied, and the fired-order id rebase",
    "generate_order_canceled": "the order or the armed trigger is gone at the venue",
    "generate_order_triggered": "a conditional order fired",
    "generate_order_expired": "the venue expired the order",
    "generate_order_filled": "a trade on the `usertrades` stream",
}

#: Events in the platform's model that an execution client must never construct,
#: with the component that owns each. Emitting any of them from here would
#: either double-apply a transition the owner has already applied, or race the
#: engine's own derivation.
FOREIGN_EVENTS: dict[str, str] = {
    "OrderInitialized": "OrderFactory, when the strategy creates the order",
    "OrderEmulated": "OrderEmulator",
    "OrderReleased": "OrderEmulator",
    "OrderPendingUpdate": "Strategy._generate_order_pending_update, before the command is sent",
    "OrderPendingCancel": "Strategy._generate_order_pending_cancel, before the command is sent",
    "PositionOpened": "ExecutionEngine, derived from fills",
    "PositionChanged": "ExecutionEngine, derived from fills",
    "PositionClosed": "ExecutionEngine, derived from fills",
    "PositionAdjusted": "Position.apply_adjustment, inside the model",
}


def _platform_event_generators() -> set[str]:
    names = {name for name in dir(ExecutionClient) if name.startswith("generate_")}
    return names - REPORT_GENERATORS


@functools.lru_cache(maxsize=1)
def _client_calls() -> tuple[frozenset[str], frozenset[str]]:
    """Return what the client actually calls: ``(self.<method>, bare names)``.

    The client's source is parsed rather than searched. Every event named here
    is discussed in a docstring somewhere in that file — ``PositionAdjusted``
    has a paragraph explaining why the platform, and not this client, raises it
    — so a text search answers the wrong question and would pass or fail on
    prose.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(GateioExecutionClient)))
    self_calls: set[str] = set()
    bare_calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            bare_calls.add(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id == "self":
                self_calls.add(func.attr)
            else:
                bare_calls.add(func.attr)
    return frozenset(self_calls), frozenset(bare_calls)


class TestGeneratorCoverage:
    def test_the_platform_generator_list_is_the_one_we_reviewed(self):
        """A new generator upstream is a decision, not a silent gap.

        ``OrderFillVoided`` is the known candidate: it is documented on
        ``develop`` with three explicit adapter requirements and does not exist
        in 1.230.0. When the pinned build changes, this test is what says so.
        """
        assert _platform_event_generators() == set(EVENT_GENERATORS)

    def test_the_live_client_adds_no_event_generators(self):
        """``LiveExecutionClient`` only adds report generators, never events."""
        live_only = {name for name in dir(LiveExecutionClient) if name.startswith("generate_")}
        assert live_only - REPORT_GENERATORS == set(EVENT_GENERATORS)

    @pytest.mark.parametrize("generator", sorted(EVENT_GENERATORS))
    def test_every_platform_event_is_generated_somewhere(self, generator):
        """Nothing in the client's event surface is left unimplemented.

        A venue with no equivalent for an event would be a legitimate reason to
        skip one — Gate.io has an equivalent for all twelve, so an absence here
        is an omission rather than a decision.
        """
        self_calls, _ = _client_calls()
        assert generator in self_calls, EVENT_GENERATORS[generator]


class TestEventsThisClientMustNotProduce:
    @pytest.mark.parametrize("event", sorted(FOREIGN_EVENTS))
    def test_a_foreign_event_is_never_constructed(self, event):
        self_calls, bare_calls = _client_calls()
        assert event not in bare_calls, FOREIGN_EVENTS[event]
        assert event not in self_calls, FOREIGN_EVENTS[event]

    def test_no_pending_generators_exist_to_call(self):
        """The platform does not even offer them, which is the real guarantee.

        ``OrderPendingUpdate`` and ``OrderPendingCancel`` are applied by
        ``Strategy`` before the command reaches this client, so an adapter that
        emitted them would drive the transition twice.
        """
        assert not hasattr(ExecutionClient, "generate_order_pending_update")
        assert not hasattr(ExecutionClient, "generate_order_pending_cancel")

    def test_position_events_are_the_engines_to_derive(self):
        assert not any(
            name.startswith("generate_position_") and not name.endswith("reports")
            for name in dir(ExecutionClient)
        )


class TestVersionFloorForOrderFillVoided:
    """``OrderFillVoided`` is documented upstream and absent from 1.230.0.

    Gate.io does void trades — self-trade-prevention reversals and erroneous
    trade cancellations — so a voided trade currently leaves an uncorrectable
    ``OrderFilled`` in the order and position history. Nothing can be done about
    that from the adapter until the platform ships the event; this pins the
    floor so the day it lands is not missed.
    """

    def test_the_installed_platform_cannot_express_a_voided_fill(self):
        import nautilus_trader.model.events as events
        from nautilus_trader.model.enums import OrderStatus

        assert not hasattr(events, "OrderFillVoided")
        assert "VOIDED" not in {status.name for status in OrderStatus}
        assert not hasattr(ExecutionClient, "generate_order_fill_voided")
