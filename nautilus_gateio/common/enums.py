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

    Gate.io reports a coarse ``status`` plus a terminal ``finish_as`` reason; the
    filled/total amounts disambiguate partially filled cancellations.
    """
    state = str(status).lower() if status else ""
    reason = GateioFinishAs.parse(finish_as)

    if state == "open":
        return OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.ACCEPTED

    if reason is GateioFinishAs.FILLED:
        return OrderStatus.FILLED
    if reason is GateioFinishAs.EXPIRED:
        return OrderStatus.EXPIRED
    if reason in (GateioFinishAs.IOC, GateioFinishAs.FOK, GateioFinishAs.STP):
        # Unfilled remainder of an immediate-or-cancel style order.
        if amount > 0 and filled >= amount:
            return OrderStatus.FILLED
        return OrderStatus.CANCELED if filled == 0 else OrderStatus.CANCELED
    if reason in (
        GateioFinishAs.CANCELLED,
        GateioFinishAs.REDUCE_ONLY,
        GateioFinishAs.REDUCE_OUT,
        GateioFinishAs.POSITION_CLOSED,
    ):
        return OrderStatus.CANCELED
    if reason in (GateioFinishAs.LIQUIDATED, GateioFinishAs.AUTO_DELEVERAGED):
        return OrderStatus.FILLED

    # Fall back on the amounts when the venue omits a reason.
    if amount > 0 and filled >= amount:
        return OrderStatus.FILLED
    if state in ("cancelled", "closed", "finished"):
        return OrderStatus.CANCELED
    return OrderStatus.ACCEPTED
