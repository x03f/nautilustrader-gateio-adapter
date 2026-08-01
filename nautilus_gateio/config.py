"""Configuration for the Gate.io data and execution clients.

Credentials
-----------
``api_key`` / ``api_secret`` default to ``None``, in which case they are read
from the environment when the client is created (see
:mod:`nautilus_gateio.common.credentials`):

* mainnet: ``GATE_API_KEY`` / ``GATE_API_SECRET``
* testnet: ``GATE_TESTNET_API_KEY`` / ``GATE_TESTNET_API_SECRET``, falling back
  to the mainnet variables.

Public market data does not need credentials at all.

Environments
------------
``environment`` is ``"mainnet"`` (default) or ``"testnet"``. Gate.io's testnet
serves spot and USDT perpetual futures; there is no testnet endpoint for
BTC-settled perpetuals, delivery futures or options, so configuring those
products together with ``environment="testnet"`` is rejected by the client.

Validation
----------
Numeric fields carry NautilusTrader's constrained types (``PositiveInt``,
``PositiveFloat``, ``NonNegativeInt``, ``NonNegativeFloat``). Two fields are
non-negative rather than positive, because ``0`` already means something here:
it disables the instrument reload task and the account poll respectively, and
that is documented behavior, not a mistake to catch.

A constrained type alone is not enough, because it is a ``msgspec`` constraint
and ``msgspec`` applies those when it *decodes*. Writing
``GateioExecClientConfig(max_retries=0)`` in Python never decodes anything, and
that is the form the README, this package's examples and
``docs/configuration.md`` are written in; the declarative
``ImportableConfig`` form is the rarer one. So each class also runs
:func:`enforce_field_bounds` from ``__post_init__``, which ``msgspec`` calls
after construction *and* after decoding. A frozen struct can do this: it is
forbidden from assigning to itself, not from reading itself, and
``KrakenExecClientConfig`` in NautilusTrader 1.230 validates the same way.

``enforce_field_bounds`` does not restate the bounds. It reads the constraint
off the annotation and hands the value to ``msgspec`` itself, so the two doors
into a configuration cannot come to disagree about what ``max_retries=0`` means.
A class that reaches it carrying no constrained field at all is a programming
error and says so, rather than passing everything.

This keeps NautilusTrader's own contract intact. ``NautilusConfig.validate()``
is declared to return ``bool`` and is implemented as
``bool(self.parse(self.json()))``; because an out-of-range value is refused at
construction, no instance that could fail that round trip ever exists, and
``validate()`` still answers ``True`` rather than raising.

Constrained types express a range, not a set. Where Gate.io accepts only
particular values — the order book push interval and the REST snapshot depth
are each a short list the venue publishes — the range is not the constraint, so
those two fields keep an explicit check (:func:`validate_book_interval_ms`,
:func:`validate_snapshot_limit`).

Cross-field validation — which products may be combined with which environment
— still happens in the client constructors
(:class:`~nautilus_gateio.data.GateioDataClient` and the execution client),
which raise ``ValueError`` with an explicit message before any network
activity. The helper functions in this module (:func:`validate_products`,
:func:`validate_book_interval_ms`, :func:`validate_snapshot_limit`) are what
those constructors call, and they are public so that users can check a
configuration up front.
"""

from __future__ import annotations

import math
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

import msgspec
from nautilus_trader.config import NonNegativeFloat, NonNegativeInt, PositiveFloat, PositiveInt
from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig

from nautilus_gateio.common.constants import (
    DEFAULT_HTTP_TIMEOUT_SECS,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
    ORDER_BOOK_SNAPSHOT_LIMITS,
    ws_url,
)
from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode

#: Environment strings accepted by the ``environment`` field.
MAINNET = "mainnet"
TESTNET = "testnet"

#: Products for which Gate.io publishes a testnet WebSocket endpoint.
TESTNET_PRODUCTS: tuple[GateioProductType, ...] = (
    GateioProductType.SPOT,
    GateioProductType.PERP,
)

#: Every order book update interval (milliseconds) Gate.io accepts on some
#: product, used to validate a configured value before it reaches the venue.
#: Which of them a *given* product accepts differs: spot and the perpetuals take
#: ``20`` and ``100``, delivery and options take ``100`` and ``1000``. The
#: per-product table in ``nautilus_gateio.websocket.public.BOOK_INTERVALS_MS``
#: is authoritative and a configured interval is clamped against it.
ORDER_BOOK_UPDATE_INTERVALS_MS: tuple[int, ...] = (20, 100, 1000)


def is_testnet(environment: str) -> bool:
    """Return whether ``environment`` selects the Gate.io testnet."""
    return environment.strip().lower() == TESTNET


def resolve_http_url(environment: str, override: str | None = None) -> str:
    """Return the REST base URL for ``environment``, honoring an override."""
    if override:
        return override
    return GATEIO_HTTP_TESTNET if is_testnet(environment) else GATEIO_HTTP_MAINNET


def resolve_ws_url(
    product: GateioProductType,
    environment: str,
    override: str | None = None,
) -> str:
    """Return the WebSocket URL for ``product``, honoring an override.

    Raises
    ------
    ValueError
        If ``environment`` is the testnet and Gate.io publishes no testnet
        endpoint for ``product``.

    """
    if override:
        return override
    return ws_url(product, testnet=is_testnet(environment))


def validate_products(
    products: tuple[GateioProductType, ...],
    environment: str,
) -> tuple[GateioProductType, ...]:
    """Validate a configured product tuple and return it de-duplicated.

    Raises
    ------
    ValueError
        If the tuple is empty, contains a non-product, or names a product that
        Gate.io does not serve on the configured environment.

    """
    if not products:
        raise ValueError("`products` must name at least one Gate.io product")
    seen: list[GateioProductType] = []
    for product in products:
        if not isinstance(product, GateioProductType):
            raise ValueError(
                f"`products` must contain GateioProductType members, was {product!r}",
            )
        if product not in seen:
            seen.append(product)
    if is_testnet(environment):
        unsupported = [p.value for p in seen if p not in TESTNET_PRODUCTS]
        if unsupported:
            raise ValueError(
                f"Gate.io has no testnet endpoint for {', '.join(unsupported)}; "
                f"testnet supports {', '.join(p.value for p in TESTNET_PRODUCTS)}",
            )
    return tuple(seen)


def validate_book_interval_ms(interval_ms: int) -> int:
    """Validate the configured order book update interval.

    Venue-specific, and therefore not expressible as a constrained type: Gate.io
    publishes a *discrete* set of push intervals (20, 100 and 1000 ms) and
    rejects a subscription naming anything else. ``PositiveInt`` admits ``37``.
    """
    if interval_ms not in ORDER_BOOK_UPDATE_INTERVALS_MS:
        raise ValueError(
            f"`order_book_update_interval_ms` must be one of "
            f"{ORDER_BOOK_UPDATE_INTERVALS_MS}, was {interval_ms}",
        )
    return interval_ms


def validate_snapshot_limit(limit: int) -> int:
    """Validate the configured REST order book snapshot depth.

    Venue-specific, and therefore not expressible as a constrained type: Gate.io
    serves snapshots only at the depths in ``ORDER_BOOK_SNAPSHOT_LIMITS``, and
    the depth has to match the level of the stream it seeds. ``PositiveInt``
    admits ``73``, which the venue would answer at some other depth.
    """
    if limit not in ORDER_BOOK_SNAPSHOT_LIMITS:
        raise ValueError(
            f"`order_book_snapshot_limit` must be one of {ORDER_BOOK_SNAPSHOT_LIMITS}, was {limit}",
        )
    return limit


def _constraints(hint: Any) -> tuple[msgspec.Meta, ...]:
    """Return the ``msgspec`` constraints an annotation carries, if any.

    ``PositiveInt`` is ``Annotated[int, Meta(gt=0)]`` and
    ``NonNegativeInt | None`` wraps one of those in a union, so both shapes have
    to be unwrapped to find the constraint.
    """
    origin = get_origin(hint)
    if origin is Annotated:
        return tuple(m for m in get_args(hint)[1:] if isinstance(m, msgspec.Meta))
    if origin in (Union, UnionType):
        return tuple(m for arg in get_args(hint) for m in _constraints(arg))
    return ()


_BOUNDED_FIELDS: dict[type, tuple[tuple[str, Any], ...]] = {}


def bounded_fields(cls: type) -> tuple[tuple[str, Any], ...]:
    """Return ``(name, annotation)`` for every field of ``cls`` that is bounded.

    "Bounded" means the annotation carries a ``msgspec`` constraint — which is
    exactly what ``PositiveInt``, ``PositiveFloat``, ``NonNegativeInt`` and
    ``NonNegativeFloat`` are. Inherited fields are included, because
    ``get_type_hints`` walks the MRO.
    """
    cached = _BOUNDED_FIELDS.get(cls)
    if cached is not None:
        return cached

    hints = get_type_hints(cls, include_extras=True)
    fields = tuple(
        (name, hints[name])
        for name in cls.__struct_fields__
        if name in hints and _constraints(hints[name])
    )
    _BOUNDED_FIELDS[cls] = fields
    return fields


def enforce_field_bounds(config: Any) -> None:
    """Refuse a value outside the range its own annotation declares.

    Called from ``__post_init__``, which ``msgspec`` runs both after a direct
    Python construction and after a decode, so a nonsensical number is refused
    by whichever door it arrives through. The bound itself is never restated
    here: the annotation is handed back to ``msgspec.convert``, so this check
    and the decoder's check are the same check.

    Raises
    ------
    ValueError
        If a bounded field holds a value its annotation excludes, holds a type
        the annotation does not admit, or holds a float JSON cannot write. The
        message names the field and says which of the three it is.
        (``msgspec.ValidationError`` from the decode path is itself a
        ``ValueError``, so one ``except ValueError`` covers both.)
    RuntimeError
        If ``config``'s class declares no bounded field at all. That is a
        programming error — an annotation was dropped, or a class that has
        nothing to check was wired to this function — and it must not be
        allowed to read as "everything passed".
    """
    fields = bounded_fields(type(config))
    if not fields:
        raise RuntimeError(
            f"{type(config).__name__} declares no constrained field, so "
            "`enforce_field_bounds` would check nothing; restore the "
            "constrained annotations or stop calling it",
        )
    for name, hint in fields:
        value = getattr(config, name)
        # `inf` satisfies every ordering bound there is, so `msgspec.convert`
        # admits it — but JSON has no spelling for it, and `msgspec.json.encode`
        # writes `null` instead. The platform's `NautilusConfig.validate()` is
        # `bool(self.parse(self.json()))`, so an admitted `inf` would come back
        # as `null` and make a method declared `-> bool` raise instead. Refusing
        # it here is what keeps "an instance that exists round-trips" true.
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"`{name}` must be a finite number for {type(config).__name__}: JSON cannot "
                f"write it, so the configuration would not survive its own round trip, "
                f"was {value!r}",
            )
        try:
            msgspec.convert(value, type=hint)
        except msgspec.ValidationError as exc:
            # `convert` type-checks as well as range-checks, and the two failures
            # read very differently to whoever wrote the value: `60.0` for an
            # `int` field is the right magnitude in the wrong type, not a number
            # out of range. So this wrapper does not name the fault — msgspec's
            # own message already distinguishes "Expected `int` >= 1" from
            # "Expected `int`, got `float`", and a wrapper that guesses would
            # send the reader hunting for a bound that is not the problem.
            raise ValueError(
                f"`{name}` is not a value {type(config).__name__} accepts: {exc}, was {value!r}",
            ) from None


class GateioDataClientConfig(LiveDataClientConfig, frozen=True):
    """Configuration for :class:`~nautilus_gateio.data.GateioDataClient`.

    Parameters
    ----------
    api_key : str, optional
        Gate.io API key. ``None`` reads the environment (public data does not
        need credentials).
    api_secret : str, optional
        Gate.io API secret. ``None`` reads the environment.
    environment : str, default "mainnet"
        ``"mainnet"`` or ``"testnet"``.
    products : tuple[GateioProductType, ...], default ``(SPOT,)``
        Products to load instruments for and open public WebSocket streams on.
        One client multiplexes every configured product.
    options_underlyings : tuple[str, ...], optional
        Restricts option instrument loading to these underlyings (for example
        ``("BTC_USDT",)``). Gate.io lists thousands of option contracts, so
        loading them all is slow and rarely wanted. Ignored unless
        ``GateioProductType.OPT`` is configured.
    base_url_http : str, optional
        Overrides the REST base URL derived from ``environment``.
    base_url_ws : str, optional
        Overrides the WebSocket URL for **every** configured product. Intended
        for single-product setups or a local aggregating proxy.
    update_instruments_interval_mins : NonNegativeInt, optional, default 60
        Interval of the instrument reload task. ``None`` disables reloading.
    http_timeout_secs : PositiveFloat, default 20.0
        Per-request REST timeout.
    max_retries : PositiveInt, default 3
        REST attempts for rate-limited or transient server errors.
    order_book_snapshot_limit : PositiveInt, default 100
        Depth of the REST order book snapshot that seeds each local book. Gate
        requires the snapshot depth to match the subscribed stream level, so
        this is also the level requested on the WebSocket where the product
        allows one to be named.
    order_book_update_interval_ms : PositiveInt, default 100
        Push interval of the incremental order book stream. Gate.io accepts
        ``20``, ``100`` and ``1000``; ``20`` implies a 20-level stream and
        delivery/options accept ``100`` and ``1000`` only.
    bars_timestamp_on_close : bool, default True
        If bars are timestamped at the close of their interval (the Nautilus
        convention). ``False`` timestamps them at the open, matching the ``t``
        field Gate.io sends.

    Notes
    -----
    A value outside the range its type declares is refused here, whether the
    configuration is written in Python or decoded from a declarative one.
    Cross-field validation — which products go with which environment — runs in
    the client constructor. Call :func:`validate_products`,
    :func:`validate_book_interval_ms` and :func:`validate_snapshot_limit`
    directly to check a configuration without building a client.

    """

    api_key: str | None = None
    api_secret: str | None = None
    environment: str = MAINNET
    products: tuple[GateioProductType, ...] = (GateioProductType.SPOT,)
    options_underlyings: tuple[str, ...] | None = None
    base_url_http: str | None = None
    base_url_ws: str | None = None
    # Non-negative rather than positive: this package documents ``0`` as a
    # spelling of "no reload task", alongside ``None``. Refusing it would
    # break a configuration that is valid today for no gain -- what is
    # nonsense here is a negative period, and that is what is refused.
    update_instruments_interval_mins: NonNegativeInt | None = 60
    http_timeout_secs: PositiveFloat = DEFAULT_HTTP_TIMEOUT_SECS
    max_retries: PositiveInt = 3
    order_book_snapshot_limit: PositiveInt = 100
    order_book_update_interval_ms: PositiveInt = 100
    bars_timestamp_on_close: bool = True

    def __post_init__(self) -> None:
        enforce_field_bounds(self)

    @property
    def is_testnet(self) -> bool:
        """Return whether this configuration targets the Gate.io testnet."""
        return is_testnet(self.environment)

    def resolve_http_url(self) -> str:
        """Return the REST base URL for this configuration."""
        return resolve_http_url(self.environment, self.base_url_http)

    def resolve_ws_url(self, product: GateioProductType) -> str:
        """Return the WebSocket URL for ``product`` under this configuration."""
        return resolve_ws_url(product, self.environment, self.base_url_ws)


class GateioExecClientConfig(LiveExecClientConfig, frozen=True):
    """Configuration for the Gate.io execution client.

    Parameters
    ----------
    api_key : str, optional
        Gate.io API key. ``None`` reads the environment; trading always needs
        credentials.
    api_secret : str, optional
        Gate.io API secret. ``None`` reads the environment.
    environment : str, default "mainnet"
        ``"mainnet"`` or ``"testnet"``. Note this defaults to **mainnet**: an
        execution client that silently pointed at a different exchange
        environment would be more dangerous than one that requires the
        operator to state which venue they mean.
    products : tuple[GateioProductType, ...], default ``(SPOT,)``
        Products this client trades. Gate.io keeps a separate wallet per
        product, so enabling several products aggregates several wallets into
        one Nautilus account.
    options_underlyings : tuple[str, ...], optional
        Restricts option instrument loading, as for the data client.
    base_url_http : str, optional
        Overrides the REST base URL derived from ``environment``.
    base_url_ws : str, optional
        Overrides the private WebSocket URL for every configured product.
    spot_account_mode : GateioSpotAccountMode, default ``SPOT``
        Ledger spot orders trade against: plain spot, isolated margin, cross
        margin or the unified account. The margin modes require the
        corresponding account type to be provisioned on Gate.io, and select
        nothing unless ``SPOT`` is among the products; ``UNIFIED`` without
        ``SPOT`` is rejected outright (see Notes).
    client_order_id_tag : str, default "ng"
        Short tag embedded in generated Gate.io ``text`` client order ids.
    account_polling_interval_secs : NonNegativeFloat, default 30.0
        Interval of the account state poll that backs up the private
        WebSocket balance stream.
    max_retries : PositiveInt, default 3
        REST attempts for rate-limited or transient server errors.
    http_timeout_secs : PositiveFloat, default 20.0
        Per-request REST timeout.

    Notes
    -----
    A value outside the range its type declares is refused here, whether the
    configuration is written in Python or decoded from a declarative one.

    Cross-field validation runs in the execution client constructor instead,
    because it compares one field against another rather than checking a field
    on its own. It rejects a product set that is empty or not served by
    ``environment``, and ``spot_account_mode=UNIFIED`` without ``SPOT`` among the
    products — a combination Gate.io accepts but this client cannot serve,
    because it reads the unified ledger only while sweeping the spot wallet. The
    other margin modes without ``SPOT`` are merely inert and are logged as such.

    """

    api_key: str | None = None
    api_secret: str | None = None
    environment: str = MAINNET
    products: tuple[GateioProductType, ...] = (GateioProductType.SPOT,)
    options_underlyings: tuple[str, ...] | None = None
    base_url_http: str | None = None
    base_url_ws: str | None = None
    spot_account_mode: GateioSpotAccountMode = GateioSpotAccountMode.SPOT
    client_order_id_tag: str = "ng"
    # Non-negative rather than positive: ``0`` is this package's documented
    # spelling of "no account poll", and unlike the reload interval there is
    # no ``None`` to say it with.
    account_polling_interval_secs: NonNegativeFloat = 30.0
    max_retries: PositiveInt = 3
    http_timeout_secs: PositiveFloat = DEFAULT_HTTP_TIMEOUT_SECS

    def __post_init__(self) -> None:
        enforce_field_bounds(self)

    @property
    def is_testnet(self) -> bool:
        """Return whether this configuration targets the Gate.io testnet."""
        return is_testnet(self.environment)

    def resolve_http_url(self) -> str:
        """Return the REST base URL for this configuration."""
        return resolve_http_url(self.environment, self.base_url_http)

    def resolve_ws_url(self, product: GateioProductType) -> str:
        """Return the WebSocket URL for ``product`` under this configuration."""
        return resolve_ws_url(product, self.environment, self.base_url_ws)
