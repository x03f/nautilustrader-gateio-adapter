"""Translation of Gate.io instrument definitions into NautilusTrader instruments.

Every function here is a pure payload transformation: it takes the JSON object
Gate.io returns for one instrument and produces the matching NautilusTrader
instrument. Nothing performs I/O, and a payload that cannot be represented
faithfully yields ``None`` rather than an exception, so a single malformed entry
never aborts a batch load (see :mod:`nautilus_gateio.providers`).

Quantity semantics
------------------
For **spot** pairs a Nautilus ``Quantity`` is an amount of the base currency,
exactly as Gate.io's ``amount`` field is.

For **every contract product** (perpetual, inverse perpetual, delivery future and
option) a Nautilus ``Quantity`` is a **number of contracts**, matching the venue's
own ``size`` field. The contract's face value is carried by ``multiplier``
(Gate.io ``quanto_multiplier`` for futures, ``multiplier`` for options), so::

    notional = quantity x multiplier x price

which is precisely how Gate.io computes it, and how
``Instrument.notional_value()`` computes it. Consequently ``size_precision`` is
``0`` and ``size_increment`` is ``1`` on all contract instruments: fractional
contracts do not exist on this venue.

Fee conventions
---------------
Gate.io publishes futures and options fees as **fractions** (``"0.0005"`` means
5 bps) but the ``fee`` field on a spot currency pair is a deprecated **percent**
string (``"0.2"`` means 0.2%, i.e. a fraction of ``0.002``). The spot parser
converts it, and prefers the account's real fee tier when the caller supplies it.
Maker fees are negative on every Gate.io perpetual and delivery contract — a
rebate — and the sign is carried through unchanged, which is what the platform
documents the field to mean.

Rates that are never assumed
----------------------------
``maker_fee``, ``taker_fee`` and ``margin_maint`` are refused rather than
defaulted when the payload does not carry them: zero is a valid rate for all
three, so a substituted zero cannot be told apart afterwards from one the venue
published, and it understates both commission and margin. See
:func:`_required_rate`.

Precision limits
----------------
NautilusTrader's fixed-point types support ``FIXED_PRECISION`` decimal places,
which is 9 on standard builds and 16 on high-precision builds. A handful of
Gate.io spot pairs quote prices with up to 14 decimals, which a standard build
cannot represent: quantising such a price yields ``0.000000000``, and both
``Price`` and the tick/book types accept that silently. Publishing the pair
anyway would mean publishing zeroes as if they were venue prices, so every
parser here **rejects** an instrument whose price scale the running build cannot
represent (``None`` plus a warning) instead of clamping it. The same guard
covers contract products, whose tick comes from ``order_price_round``: a tick
that rounds away to zero is rejected rather than published.

Tick schemes
------------
Every instrument built here carries a ``tick_scheme_name``, so
``Instrument.next_bid_price`` / ``next_ask_price`` work instead of raising. Most
Gate.io grids are a plain power of ten and use the platform's pre-registered
``FIXED_PRECISION_{n}``. A few contracts do not: ``BNB_USDT`` perpetuals and the
longer-dated ``ETH_USDT`` delivery contracts publish ``order_price_round`` of
``0.05``, so their valid prices are 2-decimal *and* multiples of ``0.05``.
Precision alone cannot express that, and ``make_price`` only quantises to the
precision, so those grids get a registered ``FixedTickScheme`` with the venue
increment (see :func:`_tick_scheme_name`).

Margins
-------
Gate.io's contract payloads publish no initial-margin *rate*. ``leverage_max``
is the venue's maximum permitted leverage (a cap: 200x on ``BTC_USDT``), not a
requirement, and the per-tier ``initial_rate`` lives on a separate endpoint
(``GET /futures/{settle}/risk_limit_tiers``). NautilusTrader's default
``LeveragedMarginModel`` computes ``initial margin = notional / leverage *
margin_init``, so contract instruments carry ``margin_init = 1`` — the full
notional at 1x — and the caller expresses the position's leverage through
``MarginAccount.set_leverage()``. This matches NautilusTrader's own crypto
futures convention. ``margin_maint`` comes from the payload's
``maintenance_rate`` field, which the venue documents as the maintenance margin
rate of the **first** risk-limit tier; larger positions fall into higher tiers,
so reconcile the real number from the position's ``average_maintenance_rate``.

The initial-margin half of that arrangement is exact: ``LeveragedMarginModel``
computes ``notional / leverage x 1``, which is how Gate.io sizes initial margin
at any leverage. The maintenance half is not, and the reason is worth stating
precisely because the error is silent and scales with leverage. Gate.io applies
``maintenance_rate`` to the notional and does **not** divide it by leverage;
``LeveragedMarginModel.calculate_margin_maint`` does, so at 50x the locally
computed maintenance requirement is 1/50 of the venue's and the portfolio
understates liquidation risk by exactly the leverage factor. Switching the
account to ``StandardMarginModel`` fixes the maintenance figure and breaks the
initial one in the same proportion, so neither shipped model is right for this
venue. ``MarginModel`` is a public base class and ``MarginAccount`` takes one
through ``set_margin_model``, so a venue-shaped model — leverage division on
initial margin, none on maintenance — is the complete answer; note that
``MarginModelConfig`` reaches only the backtest engine in 1.230.0, so a live
system has to set it programmatically. Until then the maintenance figure the
framework reports is advisory.
"""

from __future__ import annotations

import decimal
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Final, NamedTuple

from nautilus_trader.common.component import Logger
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import (
    CryptoFuture,
    CryptoOption,
    CryptoPerpetual,
    CurrencyPair,
)
from nautilus_trader.model.objects import (
    FIXED_PRECISION,
    Currency,
    Money,
    Price,
    Quantity,
)
from nautilus_trader.model.tick_scheme import (
    FixedTickScheme,
    get_tick_scheme,
    register_tick_scheme,
)
from nautilus_trader.model.tick_scheme.base import list_tick_schemes

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.parsing import (
    precision_from_increment,
    secs_to_nanos,
    to_decimal,
)
from nautilus_gateio.common.symbols import (
    gateio_to_instrument_id,
    parse_delivery_symbol,
    parse_option_symbol,
)

#: Maximum number of decimal places the running NautilusTrader build can represent.
MAX_PRECISION: Final[int] = int(FIXED_PRECISION)

#: Working precision for intermediate ``Decimal`` arithmetic (well above any venue value).
_DECIMAL_CONTEXT_PRECISION: Final[int] = 60

#: Fallback face value of one inverse (coin-margined) contract, in quote currency units.
#:
#: Gate.io returns ``quanto_multiplier: "0"`` for inverse contracts; that is a null
#: sentinel, not a multiplier, and it is used only when the payload carries no
#: usable value. The real face value of the single live inverse contract
#: (``BTC_USD``) is 1 USD, confirmed by its ticker where the 24h contract volume
#: equals the 24h USD volume.
INVERSE_CONTRACT_FACE_VALUE: Final[Decimal] = Decimal(1)

#: Initial margin ratio carried by every contract instrument.
#:
#: See the "Margins" section of the module docstring: the venue publishes no
#: initial-margin rate, and NautilusTrader's ``LeveragedMarginModel`` divides by
#: the account leverage, so the full notional is reserved at 1x.
CONTRACT_MARGIN_INIT: Final[Decimal] = Decimal(1)

#: Prefix of the tick schemes this module registers for Gate.io's non-decimal grids.
TICK_SCHEME_PREFIX: Final[str] = "GATEIO_TICK"

_LOG: Final[Logger] = Logger(name="GateioInstruments")


class _ContractSpec(NamedTuple):
    """Fields shared by every Gate.io contract product."""

    price_precision: int
    price_increment: Price
    size_increment: Quantity
    multiplier: Quantity
    min_quantity: Quantity | None
    max_quantity: Quantity | None
    maker_fee: Decimal
    taker_fee: Decimal
    margin_init: Decimal
    margin_maint: Decimal


# -- tick schemes --------------------------------------------------------------


def _tick_scheme_name(price_precision: int, price_increment: Price) -> str:
    """Return the registered tick scheme describing this instrument's price grid.

    NautilusTrader pre-registers a ``FIXED_PRECISION_{n}`` scheme for every
    representable precision (``model/tick_scheme/implementations/fixed.pyx``),
    whose increment is ``10**-n``. That is the correct scheme for the ~3,100
    Gate.io instruments whose ``order_price_round`` is a power of ten, and it is
    what the in-tree Tardis adapter uses for every instrument it builds.

    It is the *wrong* scheme for a contract that ticks in ``0.05``: it would walk
    the price in ``0.01`` steps and hand back prices Gate.io refuses. Naming the
    precision alone cannot express such a grid, so those instruments get their
    own ``FixedTickScheme`` carrying the venue increment — the platform type that
    exists for exactly this, rather than a rounding helper of our own. The bounds
    are borrowed from the stock scheme of the same precision so that the grid is
    the only thing that differs.

    Lookup precedes registration because registration is process-global and
    ``register_tick_scheme`` refuses a duplicate name: the providers re-parse
    every instrument on each load, and a reload must not raise.
    """
    increment = price_increment.as_decimal()
    if increment == Decimal(1).scaleb(-price_precision):
        return f"FIXED_PRECISION_{price_precision}"

    # The precision belongs in the name as well as the increment: a scheme
    # returns its prices at its own precision, so two grids sharing an increment
    # but not a precision are not the same scheme.
    name = f"{TICK_SCHEME_PREFIX}_{increment:f}_P{price_precision}"
    if name not in list_tick_schemes():
        stock = get_tick_scheme(f"FIXED_PRECISION_{price_precision}")
        register_tick_scheme(
            FixedTickScheme(
                name=name,
                price_precision=price_precision,
                min_tick=stock.min_price,
                max_tick=stock.max_price,
                # `increment` is documented as a float, and the scheme parses it
                # back with `Price.from_str(str(increment))`. A `Decimal` happens
                # to survive that too and would avoid the binary round-trip, but
                # relying on an unenforced annotation is not worth it: every tick
                # the venue publishes has few enough significant digits that the
                # float representation is exact.
                increment=float(increment),
            ),
        )
    return name


# -- public parsers ------------------------------------------------------------


def parse_spot_instrument(
    payload: dict[str, Any],
    fee_maker: Decimal | None = None,
    fee_taker: Decimal | None = None,
    ts_init: int = 0,
) -> CurrencyPair | None:
    """Build a ``CurrencyPair`` from a ``GET /spot/currency_pairs`` entry.

    Parameters
    ----------
    payload : dict
        One currency pair object as returned by Gate.io.
    fee_maker, fee_taker : Decimal, optional
        The account's maker/taker fees as **fractions** (from ``GET /wallet/fee``,
        or the deprecated ``GET /spot/fee`` as a fallback). When omitted, the
        pair's deprecated ``fee`` field is used, converted from percent to a
        fraction.
    ts_init : int
        UNIX timestamp (nanoseconds) when the instrument object was initialised.

    Returns
    -------
    CurrencyPair or ``None``
        ``None`` if the payload cannot be represented. In particular a pair whose
        ``precision`` or ``amount_precision`` exceeds what this NautilusTrader
        build can represent is rejected, because its prices would quantise to
        zero (see the module docstring).

    """
    try:
        raw_symbol = str(payload["id"]).upper()
        base = _currency(payload["base"])
        quote = _currency(payload["quote"])

        price_precision = _representable_precision(payload["precision"], "price precision")
        size_precision = _representable_precision(payload["amount_precision"], "size precision")

        maker, taker = _spot_fees(payload, fee_maker, fee_taker)
        price_increment = _increment_price(price_precision, "price precision")

        return CurrencyPair(
            instrument_id=gateio_to_instrument_id(GateioProductType.SPOT, raw_symbol),
            raw_symbol=_symbol(raw_symbol),
            base_currency=base,
            quote_currency=quote,
            price_precision=price_precision,
            size_precision=size_precision,
            price_increment=price_increment,
            size_increment=_increment_quantity(size_precision),
            tick_scheme_name=_tick_scheme_name(price_precision, price_increment),
            ts_event=ts_init,
            ts_init=ts_init,
            lot_size=None,
            max_quantity=_optional_quantity(
                payload.get("max_base_amount"), size_precision, ROUND_FLOOR
            ),
            min_quantity=_optional_quantity(
                payload.get("min_base_amount"), size_precision, ROUND_CEILING
            ),
            max_notional=_optional_money(payload.get("max_quote_amount"), quote, ROUND_FLOOR),
            min_notional=_optional_money(payload.get("min_quote_amount"), quote, ROUND_CEILING),
            margin_init=Decimal(0),
            margin_maint=Decimal(0),
            maker_fee=maker,
            taker_fee=taker,
            info=dict(payload),
        )
    except Exception as exc:  # noqa: BLE001 - a bad entry must not abort the batch
        _LOG.warning(f"Cannot parse spot pair {payload.get('id')!r}: {exc}")
        return None


def parse_perpetual_instrument(
    contract_payload: dict[str, Any],
    product: GateioProductType,
    ts_init: int = 0,
) -> CryptoPerpetual | None:
    """Build a ``CryptoPerpetual`` from a ``GET /futures/{settle}/contracts`` entry.

    ``product`` selects the settlement convention:

    * ``GateioProductType.PERP`` - linear, USDT-margined. Settlement currency is
      the quote currency and ``is_inverse`` is ``False``.
    * ``GateioProductType.INVERSE`` - coin-margined. Settlement currency is the
      base currency, ``is_inverse`` is ``True`` and the contract face value is
      1 unit of the quote currency (Gate.io reports ``quanto_multiplier: "0"``
      for these contracts, which is a null sentinel).

    Returns ``None`` if the payload cannot be represented.
    """
    if not product.is_perpetual:
        raise ValueError(f"{product.value} is not a perpetual product")

    name = str(contract_payload.get("name", ""))
    try:
        raw_symbol = name.upper()
        base, quote = _split_pair(raw_symbol)
        is_inverse = _is_inverse(contract_payload, product)
        settlement = base if is_inverse else quote
        spec = _contract_spec(contract_payload, is_inverse=is_inverse)

        return CryptoPerpetual(
            instrument_id=gateio_to_instrument_id(product, raw_symbol),
            raw_symbol=_symbol(raw_symbol),
            base_currency=base,
            quote_currency=quote,
            settlement_currency=settlement,
            is_inverse=is_inverse,
            price_precision=spec.price_precision,
            size_precision=0,
            price_increment=spec.price_increment,
            size_increment=spec.size_increment,
            tick_scheme_name=_tick_scheme_name(spec.price_precision, spec.price_increment),
            ts_event=_contract_ts_event(contract_payload, ts_init),
            ts_init=ts_init,
            multiplier=spec.multiplier,
            lot_size=spec.size_increment,
            max_quantity=spec.max_quantity,
            min_quantity=spec.min_quantity,
            margin_init=spec.margin_init,
            margin_maint=spec.margin_maint,
            maker_fee=spec.maker_fee,
            taker_fee=spec.taker_fee,
            info=dict(contract_payload),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(f"Cannot parse perpetual contract {name!r}: {exc}")
        return None


def parse_delivery_instrument(
    contract_payload: dict[str, Any],
    ts_init: int = 0,
) -> CryptoFuture | None:
    """Build a ``CryptoFuture`` from a ``GET /delivery/{settle}/contracts`` entry.

    The underlying currency is taken from the contract symbol
    (``SOL_USDT_20260731`` -> ``SOL``), and ``expiration_ns`` from the contract's
    ``expire_time``. ``GET /delivery/{settle}/contracts`` carries no listing time
    — unlike the perpetual payload it has no ``create_time`` — so ``activation_ns``
    resolves to ``0``, meaning no activation constraint, rather than being
    invented from a timestamp that means something else.

    Returns ``None`` if the payload cannot be represented.
    """
    name = str(contract_payload.get("name", ""))
    try:
        raw_symbol = name.upper()
        pair, _ = parse_delivery_symbol(raw_symbol)
        base, quote = _split_pair(pair)
        is_inverse = str(contract_payload.get("type", "direct")).lower() == "inverse"
        settlement = base if is_inverse else quote
        spec = _contract_spec(contract_payload, is_inverse=is_inverse)

        expire_time = contract_payload.get("expire_time")
        if not expire_time:
            raise ValueError("contract has no 'expire_time'")

        return CryptoFuture(
            instrument_id=gateio_to_instrument_id(GateioProductType.FUT, raw_symbol),
            raw_symbol=_symbol(raw_symbol),
            underlying=base,
            quote_currency=quote,
            settlement_currency=settlement,
            is_inverse=is_inverse,
            activation_ns=secs_to_nanos(contract_payload.get("create_time", 0)),
            expiration_ns=secs_to_nanos(expire_time),
            price_precision=spec.price_precision,
            size_precision=0,
            price_increment=spec.price_increment,
            size_increment=spec.size_increment,
            tick_scheme_name=_tick_scheme_name(spec.price_precision, spec.price_increment),
            ts_event=_contract_ts_event(contract_payload, ts_init),
            ts_init=ts_init,
            multiplier=spec.multiplier,
            lot_size=spec.size_increment,
            max_quantity=spec.max_quantity,
            min_quantity=spec.min_quantity,
            margin_init=spec.margin_init,
            margin_maint=spec.margin_maint,
            maker_fee=spec.maker_fee,
            taker_fee=spec.taker_fee,
            info=dict(contract_payload),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(f"Cannot parse delivery contract {name!r}: {exc}")
        return None


def parse_option_instrument(
    contract_payload: dict[str, Any],
    ts_init: int = 0,
) -> CryptoOption | None:
    """Build a ``CryptoOption`` from a ``GET /options/contracts`` entry.

    Gate.io options are European, USDT-settled and quoted in USDT **per unit of
    underlying**; the cash premium of an order is ``price x multiplier x size``,
    so ``multiplier`` carries the venue's ``multiplier`` field (``0.01`` for BTC
    and ETH, ``1`` for LTC and SOL, and so on) and quantities are contracts.

    The option kind parsed from the symbol is cross-checked against the payload's
    ``is_call`` flag; a mismatch is rejected rather than guessed.

    Returns ``None`` if the payload cannot be represented.
    """
    name = str(contract_payload.get("name", ""))
    try:
        raw_symbol = name.upper()
        underlying_pair, _, _, symbol_is_call = parse_option_symbol(raw_symbol)
        base, quote = _split_pair(underlying_pair)

        payload_is_call = bool(contract_payload["is_call"])
        if payload_is_call != symbol_is_call:
            raise ValueError(
                f"symbol says {'call' if symbol_is_call else 'put'} but the payload "
                f"reports is_call={payload_is_call}"
            )

        price_precision = _representable_precision(
            precision_from_increment(contract_payload["order_price_round"]),
            "order_price_round",
        )
        price_increment = _tick_from_value(
            contract_payload["order_price_round"],
            price_precision,
            "order_price_round",
        )

        strike_raw = to_decimal(contract_payload["strike_price"])
        strike_precision = _representable_precision(
            max(price_precision, _exponent(strike_raw)),
            "strike_price",
        )
        strike_price = _tick_from_value(strike_raw, strike_precision, "strike_price")

        multiplier = _quantity_from_value(contract_payload["multiplier"])
        size_increment = Quantity.from_int(1)

        expiration = contract_payload.get("expiration_time")
        if not expiration:
            raise ValueError("contract has no 'expiration_time'")

        return CryptoOption(
            instrument_id=gateio_to_instrument_id(GateioProductType.OPT, raw_symbol),
            raw_symbol=_symbol(raw_symbol),
            underlying=base,
            quote_currency=quote,
            settlement_currency=quote,
            is_inverse=False,
            option_kind=OptionKind.CALL if payload_is_call else OptionKind.PUT,
            strike_price=strike_price,
            activation_ns=secs_to_nanos(contract_payload.get("create_time", 0)),
            expiration_ns=secs_to_nanos(expiration),
            price_precision=price_precision,
            size_precision=0,
            price_increment=price_increment,
            size_increment=size_increment,
            tick_scheme_name=_tick_scheme_name(price_precision, price_increment),
            ts_event=_contract_ts_event(contract_payload, ts_init),
            ts_init=ts_init,
            multiplier=multiplier,
            lot_size=size_increment,
            max_quantity=_optional_quantity(contract_payload.get("order_size_max"), 0, ROUND_FLOOR),
            min_quantity=_optional_quantity(
                contract_payload.get("order_size_min"), 0, ROUND_CEILING
            ),
            # Gate.io's option margin coefficients (`init_margin_high`/`_low`,
            # `maint_margin_base`) are ratios of the *underlying* price applied to
            # short positions, not of the premium notional, so they cannot be
            # expressed as Nautilus margin ratios. They remain available in `info`.
            margin_init=Decimal(0),
            margin_maint=Decimal(0),
            maker_fee=_required_rate(contract_payload, "maker_fee_rate"),
            taker_fee=_required_rate(contract_payload, "taker_fee_rate"),
            info=dict(contract_payload),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(f"Cannot parse option contract {name!r}: {exc}")
        return None


def parse_instrument(
    payload: dict[str, Any],
    product: GateioProductType,
    ts_init: int = 0,
    fee_maker: Decimal | None = None,
    fee_taker: Decimal | None = None,
) -> CurrencyPair | CryptoPerpetual | CryptoFuture | CryptoOption | None:
    """Dispatch a Gate.io payload to the parser for ``product``."""
    if product.is_spot:
        return parse_spot_instrument(payload, fee_maker, fee_taker, ts_init)
    if product.is_perpetual:
        return parse_perpetual_instrument(payload, product, ts_init)
    if product.is_delivery:
        return parse_delivery_instrument(payload, ts_init)
    if product.is_option:
        return parse_option_instrument(payload, ts_init)
    raise ValueError(f"unknown product {product!r}")


# -- shared contract handling --------------------------------------------------


def _contract_spec(payload: dict[str, Any], is_inverse: bool) -> _ContractSpec:
    """Derive the fields common to every Gate.io contract product.

    Sizes are contract counts, so ``size_increment`` is always ``1`` and
    ``size_precision`` always ``0``. The price tick comes from
    ``order_price_round``, which is not always a power of ten (``BNB_USDT``
    perpetuals tick in ``0.05``), so the published increment is used verbatim;
    a tick this build cannot represent is rejected rather than rounded to zero.

    Margin source fields: ``margin_init`` is **not** venue-derived (the payload
    has no initial-margin rate — see the module docstring), and ``margin_maint``
    is the payload's ``maintenance_rate``, the first risk-limit tier's rate.
    """
    price_precision = _representable_precision(
        precision_from_increment(payload["order_price_round"]),
        "order_price_round",
    )
    price_increment = _tick_from_value(
        payload["order_price_round"],
        price_precision,
        "order_price_round",
    )

    multiplier = _contract_multiplier(payload, is_inverse)

    return _ContractSpec(
        price_precision=price_precision,
        price_increment=price_increment,
        size_increment=Quantity.from_int(1),
        multiplier=multiplier,
        min_quantity=_optional_quantity(payload.get("order_size_min"), 0, ROUND_CEILING),
        max_quantity=_optional_quantity(payload.get("order_size_max"), 0, ROUND_FLOOR),
        maker_fee=_required_rate(payload, "maker_fee_rate"),
        taker_fee=_required_rate(payload, "taker_fee_rate"),
        margin_init=CONTRACT_MARGIN_INIT,
        margin_maint=_quantize_ratio(_required_rate(payload, "maintenance_rate")),
    )


def _contract_multiplier(payload: dict[str, Any], is_inverse: bool) -> Quantity:
    """Face value of one contract, as a Nautilus ``Quantity`` multiplier.

    The venue field is ``quanto_multiplier``. Inverse (coin-margined) contracts
    report the ``"0"`` sentinel instead of a value, in which case the documented
    face value of one quote-currency unit is used; a populated value always wins
    so a future inverse contract with a different face value is not silently
    mispriced.
    """
    value = to_decimal(payload.get("quanto_multiplier"))

    if is_inverse:
        if value <= 0:
            value = INVERSE_CONTRACT_FACE_VALUE
        elif value != INVERSE_CONTRACT_FACE_VALUE:
            # NautilusTrader's `use_quote_for_inverse` path assumes one contract
            # is one unit of quote currency, so such a contract needs review.
            _LOG.warning(
                f"Inverse contract {payload.get('name')!r} reports "
                f"quanto_multiplier={value}, not {INVERSE_CONTRACT_FACE_VALUE}; "
                f"quote-denominated notionals may be wrong"
            )
        return _quantity_from_value(value)

    if value <= 0:
        raise ValueError(f"invalid quanto_multiplier {payload.get('quanto_multiplier')!r}")
    return _quantity_from_value(value)


def _is_inverse(payload: dict[str, Any], product: GateioProductType) -> bool:
    """Resolve the inverse flag, requiring the payload and the product to agree."""
    declared = str(payload.get("type", "")).lower()
    if declared not in ("", "direct", "inverse"):
        raise ValueError(f"unknown contract type {declared!r}")
    payload_inverse = declared == "inverse"
    if declared and payload_inverse != product.is_inverse:
        raise ValueError(
            f"contract type {declared!r} contradicts product {product.value} "
            f"(settle={product.settle})"
        )
    return product.is_inverse


def _contract_ts_event(payload: dict[str, Any], ts_init: int) -> int:
    """Timestamp of the definition itself.

    Gate.io stamps futures and delivery contracts with ``config_change_time`` (the
    last time the contract specification changed) and options with ``create_time``;
    when neither is present the initialisation time is used.
    """
    for field in ("config_change_time", "create_time"):
        value = payload.get(field)
        if value:
            return secs_to_nanos(value)
    return ts_init


def _spot_fees(
    payload: dict[str, Any],
    fee_maker: Decimal | None,
    fee_taker: Decimal | None,
) -> tuple[Decimal, Decimal]:
    """Resolve spot fees, converting the pair's percent field when needed.

    Raises
    ------
    ValueError
        If neither side is supplied by the caller and the pair carries no usable
        ``fee`` field — see :func:`_required_rate` for why zero is not a fallback.
    """
    if fee_maker is not None and fee_taker is not None:
        return fee_maker, fee_taker

    # The per-pair `fee` field is a PERCENT string ("0.2" == 0.2% == 0.002).
    fallback = _required_rate(payload, "fee") / Decimal(100)
    return (
        fallback if fee_maker is None else fee_maker,
        fallback if fee_taker is None else fee_taker,
    )


# -- value helpers -------------------------------------------------------------


def _required_rate(payload: dict[str, Any], field: str) -> Decimal:
    """Read a venue rate that must not be assumed.

    :func:`to_decimal` answers zero for a value that is missing, empty or not a
    number, and every field routed through here is one where zero is itself a
    meaningful rate. A ``margin_maint`` of zero tells ``MarginAccount`` the
    position needs no maintenance margin at all (``accounting/margin_models.pyx``
    multiplies the notional by it), and a fee of zero tells
    ``Account.calculate_commission`` that trading this instrument is free. Once
    the instrument is built, neither is distinguishable from a rate the venue
    really published.

    Gate.io publishes all of them on every contract and option it lists, so a
    payload without one is a payload this parser does not understand. It is
    refused for the same reason an unrepresentable price scale is refused (see
    the "Precision limits" section of the module docstring): the caller turns the
    ``ValueError`` into a skipped instrument plus a warning naming the field,
    which is loud, rather than a published instrument carrying a number nobody
    chose, which is silent.
    """
    raw = payload.get(field)
    if raw is None or raw == "":
        raise ValueError(f"payload has no {field!r}, and it cannot be assumed to be zero")
    try:
        return Decimal(str(raw))
    except (decimal.InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} {raw!r} is not a number") from exc


def _clamp_precision(precision: int) -> int:
    """Clamp a venue precision to what this NautilusTrader build can represent.

    Only for values whose loss of scale is harmless (a ``multiplier``, whose
    magnitude is what matters). Anything that participates in price arithmetic
    must go through :func:`_representable_precision` instead.
    """
    return max(0, min(int(precision), MAX_PRECISION))


def _representable_precision(precision: Any, field: str) -> int:
    """Validate a venue precision against this NautilusTrader build.

    Clamping a price scale silently would make every price on the instrument
    quantise to zero, so an unrepresentable scale is rejected: the caller turns
    the ``ValueError`` into a skipped instrument plus a warning.
    """
    value = int(precision)
    if value < 0:
        raise ValueError(f"{field} {value} is negative")
    if value > MAX_PRECISION:
        raise ValueError(
            f"{field} {value} exceeds the {MAX_PRECISION} decimal places this "
            f"NautilusTrader build can represent; the instrument would carry "
            f"zero prices, so it is not published"
        )
    return value


def _tick_from_value(value: Any, precision: int, field: str) -> Price:
    """Build a price tick, rejecting one that quantises away to zero.

    ``Price`` accepts ``0`` without complaint, and so do ``QuoteTick``,
    ``TradeTick`` and ``BookOrder``; an instrument with a zero tick would
    therefore publish zeroes as if they were venue prices.
    """
    tick = _price_from_value(value, precision)
    if tick.as_decimal() <= 0:
        raise ValueError(
            f"{field} {value!r} is not representable at {precision} decimal "
            f"places (this NautilusTrader build supports {MAX_PRECISION}); "
            f"the instrument would carry a zero price increment"
        )
    return tick


def _currency(code: Any) -> Currency:
    """Resolve a currency code, defaulting unknown codes to 8-decimal crypto."""
    text = str(code).upper()
    if not text:
        raise ValueError("empty currency code")
    return Currency.from_str(text)


def _symbol(raw_symbol: str) -> Symbol:
    return Symbol(raw_symbol)


def _split_pair(pair: str) -> tuple[Currency, Currency]:
    """Split ``BTC_USDT`` into its base and quote currencies."""
    base, sep, quote = pair.upper().partition("_")
    if not sep or not base or not quote:
        raise ValueError(f"symbol {pair!r} is not in '<BASE>_<QUOTE>' form")
    return _currency(base), _currency(quote)


def _exponent(value: Decimal) -> int:
    """Number of decimal places carried by ``value``."""
    exponent = value.as_tuple().exponent
    return max(0, -int(exponent)) if isinstance(exponent, int) else 0


def _format(value: Decimal, precision: int, rounding: str = ROUND_HALF_UP) -> str:
    with decimal.localcontext() as ctx:
        ctx.prec = _DECIMAL_CONTEXT_PRECISION
        quantized = value.quantize(Decimal(1).scaleb(-precision), rounding=rounding)
    return f"{quantized:.{precision}f}"


def _increment_price(precision: int, field: str) -> Price:
    """``10 ** -precision`` as a ``Price``, guarded against a zero tick."""
    return _tick_from_value(Decimal(1).scaleb(-precision), precision, field)


def _increment_quantity(precision: int) -> Quantity:
    """``10 ** -precision`` as a ``Quantity``."""
    return Quantity.from_str(_format(Decimal(1).scaleb(-precision), precision))


def _price_from_value(value: Any, precision: int) -> Price:
    return Price.from_str(_format(to_decimal(value), precision))


def _quantity_from_value(value: Any) -> Quantity:
    """Build a ``Quantity`` preserving the venue's own decimal places."""
    parsed = to_decimal(value)
    precision = _clamp_precision(_exponent(parsed))
    return Quantity.from_str(_format(parsed, precision, ROUND_DOWN))


def _quantize_ratio(value: Decimal) -> Decimal:
    """Round a derived ratio (such as ``1 / leverage_max``) to a sane scale."""
    with decimal.localcontext() as ctx:
        ctx.prec = _DECIMAL_CONTEXT_PRECISION
        return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _optional_quantity(value: Any, precision: int, rounding: str) -> Quantity | None:
    """Build an optional size bound, dropping it when it is absent or unusable.

    Bounds are rounded *inwards* (minimums up, maximums down) so a rounded value
    never permits an order the venue would reject.
    """
    if value is None or value == "":
        return None
    try:
        parsed = to_decimal(value)
        if parsed <= 0:
            return None
        bound = Quantity.from_str(_format(parsed, precision, rounding))
    except Exception as exc:  # noqa: BLE001 - an unusable bound is dropped, not fatal
        _LOG.warning(f"Ignoring unusable size bound {value!r}: {exc}")
        return None

    if bound.as_decimal() <= 0:
        # A positive bound that rounds inwards to zero would forbid every order;
        # dropping it leaves the venue to enforce the real limit.
        _LOG.warning(f"Ignoring size bound {value!r}: it rounds to zero at precision {precision}")
        return None
    return bound


def _optional_money(value: Any, currency: Currency, rounding: str) -> Money | None:
    """Build an optional notional bound in ``currency``, dropping an unusable one.

    A ``Money`` carries the *currency's* precision, not the venue's, so a bound
    finer than the quote currency can round inwards to zero — and the platform's
    ``Instrument`` constructor requires a positive ``max_notional``
    (``model/instruments/base.pyx``, ``Condition.positive``). Letting that reach
    the constructor would raise inside the parser's blanket handler and discard
    the whole instrument over an optional field, which is out of all proportion
    to what is lost. The size bounds have refused that trade since they were
    written; this is the same guard, for the same reason.
    """
    if value is None or value == "":
        return None
    try:
        parsed = to_decimal(value)
        if parsed <= 0:
            return None
        bound = Money(_format(parsed, currency.precision, rounding), currency)
    except Exception as exc:  # noqa: BLE001 - an unusable bound is dropped, not fatal
        _LOG.warning(f"Ignoring unusable notional bound {value!r}: {exc}")
        return None

    if bound.as_decimal() <= 0:
        _LOG.warning(
            f"Ignoring notional bound {value!r}: it rounds to zero at "
            f"{currency.code}'s precision of {currency.precision}"
        )
        return None
    return bound
