"""Gate.io product types and venue enums, with conversions to NautilusTrader enums."""

from __future__ import annotations

from enum import Enum, unique
from typing import Final

from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    time_in_force_to_str,
)


@unique
class GateioProductType(Enum):
    """A tradable Gate.io product family.

    Each product has its own REST namespace, WebSocket endpoint and wallet on
    Gate.io, so the product is part of an instrument's identity (see
    :mod:`nautilus_gateio.common.symbols`).
    """

    SPOT = "SPOT"
    PERP = "PERP"  # USDT-margined perpetual futures (linear), settle=usdt
    INVERSE = "INVERSE"  # BTC-margined perpetual futures (inverse), settle=btc
    FUT = "FUT"  # USDT-margined delivery (dated) futures, settle=usdt
    OPT = "OPT"  # USDT-settled options

    @property
    def is_spot(self) -> bool:
        return self is GateioProductType.SPOT

    @property
    def is_futures(self) -> bool:
        """Perpetual or delivery futures (i.e. the ``/futures`` and ``/delivery`` APIs)."""
        return self in (GateioProductType.PERP, GateioProductType.INVERSE, GateioProductType.FUT)

    @property
    def is_perpetual(self) -> bool:
        return self in (GateioProductType.PERP, GateioProductType.INVERSE)

    @property
    def is_delivery(self) -> bool:
        return self is GateioProductType.FUT

    @property
    def is_option(self) -> bool:
        return self is GateioProductType.OPT

    @property
    def is_inverse(self) -> bool:
        return self is GateioProductType.INVERSE

    @property
    def settle(self) -> str:
        """The ``settle`` path parameter used by the futures/delivery/options APIs."""
        if self is GateioProductType.INVERSE:
            return "btc"
        return "usdt"


GATEIO_ALL_PRODUCTS: Final[tuple[GateioProductType, ...]] = tuple(GateioProductType)


@unique
class GateioSpotAccountMode(Enum):
    """The ``account`` field sent with spot orders — selects which ledger trades.

    ``SPOT`` is a plain cash trade. The margin modes borrow against collateral and
    require the corresponding account type to be provisioned on Gate.io.
    """

    SPOT = "spot"
    MARGIN = "margin"  # isolated margin
    CROSS_MARGIN = "cross_margin"
    UNIFIED = "unified"

    @property
    def is_margin(self) -> bool:
        return self is not GateioSpotAccountMode.SPOT


@unique
class GateioOrderStatus(Enum):
    """Order ``status`` values returned by Gate.io (spot and futures share these)."""

    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FINISHED = "finished"


@unique
class GateioFinishAs(Enum):
    """Terminal reason (``finish_as``) reported once an order leaves the book."""

    FILLED = "filled"
    CANCELLED = "cancelled"
    LIQUIDATED = "liquidated"
    IOC = "ioc"
    FOK = "fok"
    AUTO_DELEVERAGED = "auto_deleveraged"
    REDUCE_ONLY = "reduce_only"
    POSITION_CLOSED = "position_closed"
    REDUCE_OUT = "reduce_out"
    STP = "stp"
    EXPIRED = "expired"
    UNKNOWN = "_unknown"

    @classmethod
    def parse(cls, value: str | None) -> GateioFinishAs:
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


@unique
class GateioTimeInForce(Enum):
    """Gate.io time-in-force values. ``POC`` is post-only ("pending or cancelled")."""

    GTC = "gtc"
    IOC = "ioc"
    POC = "poc"
    FOK = "fok"


@unique
class GateioTriggerRule(Enum):
    """Price-trigger comparison rule: 1 = price >= trigger, 2 = price <= trigger."""

    GREATER_OR_EQUAL = 1
    LESS_OR_EQUAL = 2


# -- conversions to Nautilus enums --------------------------------------------


def order_side_to_gateio(side: OrderSide) -> str:
    """Nautilus order side -> Gate.io spot ``side`` string."""
    return "buy" if side == OrderSide.BUY else "sell"


def order_side_from_gateio(side: str) -> OrderSide:
    return OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL


def time_in_force_to_gateio(
    time_in_force: TimeInForce,
    post_only: bool = False,
) -> GateioTimeInForce:
    """Map a Nautilus time in force onto Gate.io's vocabulary.

    Gate.io has no separate post-only flag: the constraint is expressed by the
    time-in-force value ``poc``, a maker-only order that *rests* until it is
    cancelled. A post-only GTC order is therefore ``poc``, and nothing is lost.

    IOC and FOK are a different matter. NautilusTrader models post-only as a
    liquidity constraint orthogonal to the time in force (concepts/orders,
    "Post-only" and "Time in force"), so ``LIMIT``/``IOC``/``post_only`` asks for
    an order that is maker-only *and* gone within milliseconds. Sending ``poc``
    would keep the first half and discard the second, leaving a resting order the
    caller expects to have self-cancelled — the kind of substitution the platform
    tells an adapter not to make ("If an order includes an instruction or option
    the target venue does not support, the system does not submit it"). Both
    combinations therefore raise, like every other value Gate.io cannot express.
    """
    if post_only:
        if time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
            raise ValueError(
                f"post-only cannot be combined with {time_in_force_to_str(time_in_force)} on "
                f"Gate.io: post-only is the `poc` time in force, a maker-only order that rests "
                f"until cancelled, so the immediacy of "
                f"{time_in_force_to_str(time_in_force)} cannot survive. Submit the order either "
                f"post-only (GTC) or immediate, not both"
            )
        return GateioTimeInForce.POC
    if time_in_force == TimeInForce.GTC:
        return GateioTimeInForce.GTC
    if time_in_force == TimeInForce.IOC:
        return GateioTimeInForce.IOC
    if time_in_force == TimeInForce.FOK:
        return GateioTimeInForce.FOK
    raise ValueError(
        f"time in force {time_in_force_to_str(time_in_force)} is not supported by Gate.io "
        f"(supported: GTC, IOC, FOK, and post-only via POC)"
    )


def time_in_force_from_gateio(value: str | None) -> TimeInForce:
    mapping = {
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
        "poc": TimeInForce.GTC,  # post-only is GTC with a maker-only constraint
    }
    return mapping.get(str(value).lower(), TimeInForce.GTC)


def liquidity_side_from_gateio(role: str | None) -> LiquiditySide:
    if role is None:
        return LiquiditySide.NO_LIQUIDITY_SIDE
    return LiquiditySide.MAKER if str(role).lower() == "maker" else LiquiditySide.TAKER


def order_type_from_gateio(order_type: str | None) -> OrderType:
    return OrderType.MARKET if str(order_type).lower() == "market" else OrderType.LIMIT


def order_status_from_gateio(
    status: str | None,
    finish_as: str | None = None,
    filled: float = 0.0,
    amount: float = 0.0,
) -> OrderStatus:
    """Map Gate.io order state onto a Nautilus :class:`OrderStatus`.

    Gate.io reports a coarse ``status`` plus a terminal ``finish_as`` reason.
    The two answer different questions and only one of them is authoritative
    about completion: **the quantities say whether the order finished, the
    reason says why it stopped.** The order of the branches below is therefore
    load-bearing — completion is decided from ``filled``/``amount`` first, and
    ``finish_as`` only selects the flavour of a *non*-completion.

    Consulting the reason first looks equivalent and is not; it produces a wrong
    terminal state in both directions, and this ordering closes both at once:

    * ``FILLED``, ``EXPIRED`` and ``REJECTED`` are all terminal, but only
      ``CANCELED`` has a transition left for a late fill (installed
      model/orders/base.pyx:132-133 holds ``(CANCELED, PARTIALLY_FILLED)`` and
      ``(CANCELED, FILLED)``, annotated "Real world possibility", and nothing
      else). Reporting a completed order as ``EXPIRED`` therefore does more than
      mislabel it: Gate.io does not order ``*.orders`` against ``*.usertrades``,
      so the fill that completed the order routinely arrives after the message
      that closed it, and against an ``EXPIRED`` order that execution is
      discarded instead of booked.
    * The mirror case is a reason that implies completion when nothing filled.
      ``liquidated`` and ``auto_deleveraged`` are **cancellations** in Gate.io's
      own words — "cancelled because of liquidation" and "finished by ADL" on
      ``FuturesOrder.finish_as`` — so calling them ``FILLED`` with a zero fill
      leaves the order open in the cache indefinitely: nothing closes an order
      the client believes filled (the fill stream is meant to do that), and
      reconciliation finds no fill to infer either.

    ``expired`` is worth naming explicitly because it does not belong to a
    regular order at all: it is absent from the ``finish_as`` enum of every
    order object (``Order``, ``FuturesOrder``, ``OptionsOrder``) and appears
    only on ``FuturesPriceTriggeredOrder`` (``cancelled`` / ``succeeded`` /
    ``failed`` / ``expired``), where it means the *trigger* lapsed unfired. It
    stays mapped here because the venue is free to widen a documented enum and a
    GTD-style lapse is what ``EXPIRED`` exists for — but only for an order that
    did not complete.

    Reasons Gate.io documents and this enum does not name — ``poc``, ``small``,
    ``depth_not_enough``, ``trader_not_enough``, ``liquidate_cancelled``,
    ``price_protect_cancelled``, ``mmp_cancelled`` — parse to ``UNKNOWN`` and
    fall through to the state, which resolves every one of them to the same
    ``CANCELED`` an explicit branch would.

    ``poc`` is the one whose fallback is not the whole answer. A post-only
    order the venue would not rest is a *rejection*, and the flag that says so —
    ``due_post_only`` — lives on ``OrderRejected``, which no ``OrderStatus`` can
    carry. The live path does not depend on this mapping for it: the order
    stream reads ``finish_as`` itself, ahead of any use of the status returned
    here, and answers ``poc`` with a rejection where the order is still in a
    state the platform allows one from.

    The reconciliation path has no such override — an order status report built
    from a REST listing has only this mapping — so a post-only refusal recovered
    after a restart is reported ``CANCELED`` where ``REJECTED`` would be
    faithful. That gap is open, and it is not closable by naming ``poc`` here:
    the same value would then map to ``REJECTED`` for an order the venue had
    already partly filled, and the platform has no ``PARTIALLY_FILLED ->
    REJECTED`` transition to receive it.
    """
    state = str(status).lower() if status else ""
    reason = GateioFinishAs.parse(finish_as)

    if state == "open":
        return OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.ACCEPTED

    # Completion is a fact about quantities, so it outranks every reason. The
    # `amount > 0` guard keeps out a payload that states no comparable quantity
    # — a spot market buy, whose `amount` is a quote cash amount.
    if amount > 0 and filled >= amount:
        return OrderStatus.FILLED

    if reason is GateioFinishAs.FILLED:
        # No usable quantities, so the venue's own word is all there is.
        return OrderStatus.FILLED
    if reason is GateioFinishAs.EXPIRED:
        return OrderStatus.EXPIRED
    if reason is not GateioFinishAs.UNKNOWN:
        # Everything else Gate.io names ends the order without completing it:
        # user cancels, the unfilled remainder of an IOC/FOK order, self-trade
        # prevention, reduce-only and position-closed refusals, liquidation and
        # ADL. A partial fill among them is still a cancellation — the platform
        # reads the filled quantity off the fills, not off this status.
        return OrderStatus.CANCELED

    # Fall back on the state when the venue omits or renames the reason.
    if state in ("cancelled", "closed", "finished"):
        return OrderStatus.CANCELED
    return OrderStatus.ACCEPTED
