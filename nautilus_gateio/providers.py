"""Instrument provider for Gate.io.

The provider loads instrument definitions for the products the adapter is
configured to trade. Each Gate.io product family has its own REST namespace and
its own wallet, so they are loaded independently and a failure on one product
(most often a wallet that has not been provisioned yet, or an API key without
permission for that product) is logged and skipped rather than aborting the
whole load.

Every request goes through the typed REST namespaces in
:mod:`nautilus_gateio.http`, so the venue's path layout — in particular the
``/futures/{settle}`` versus ``/delivery/{settle}`` split — is expressed in
exactly one place:

======================  ===============================================
Product                 Namespace call
======================  ===============================================
Spot                    ``GateioSpotHttpAPI.currency_pairs()``
Perpetual (linear)      ``GateioFuturesHttpAPI(settle="usdt").contracts()``
Perpetual (inverse)     ``GateioFuturesHttpAPI(settle="btc").contracts()``
Delivery future         ``GateioFuturesHttpAPI(settle="usdt",
                        delivery=True).contracts()``
Option                  ``GateioOptionsHttpAPI.underlyings() /
                        .expirations() / .contracts()``
======================  ===============================================

Those endpoints are public. When credentials are configured the account's fee
tier is read once from ``GateioWalletHttpAPI.fee()`` (``GET /wallet/fee``), with
the deprecated ``GET /spot/fee`` as a fallback, so spot instruments carry the
account's real fees instead of the deprecated per-pair percentage.

Instruments that cannot be traded normally are not published: spot pairs that
are ``untradable`` or in one of the one-sided states, futures and delivery
contracts that are delisting or inactive, expired delivery contracts, and
options outside an active expiration.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any, Final

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument

from nautilus_gateio.common.constants import GATEIO_VENUE
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import (
    ACCOUNT_MODE_LABELS,
    WALLET_NOT_PROVISIONED_LABELS,
    GateioError,
    WalletNotProvisionedError,
)
from nautilus_gateio.common.parsing import to_decimal
from nautilus_gateio.common.symbols import instrument_id_to_gateio
from nautilus_gateio.http.client import GateioHttpClient
from nautilus_gateio.http.futures import GateioFuturesHttpAPI
from nautilus_gateio.http.options import GateioOptionsHttpAPI
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.http.wallet import GateioWalletHttpAPI
from nautilus_gateio.instruments import (
    parse_delivery_instrument,
    parse_option_instrument,
    parse_perpetual_instrument,
    parse_spot_instrument,
)

#: Spot pairs in this state cannot be traded at all and are not published.
UNTRADABLE_SPOT_STATUS: Final[str] = "untradable"

#: Spot ``trade_status`` values that allow only one side of the market.
#:
#: Gate.io uses these around listing and delisting windows: ``buyable`` accepts
#: buys only, ``sellable`` sells only. A ``CurrencyPair`` cannot express a
#: one-sided market, so such a pair is not published as tradable — publishing it
#: would let a strategy send the disallowed side and collect an opaque venue
#: rejection.
ONE_SIDED_SPOT_STATUSES: Final[tuple[str, ...]] = ("buyable", "sellable")

#: Loading every listed option across every underlying yields thousands of
#: instruments; past this count the provider suggests narrowing the load.
OPTIONS_COUNT_WARNING_THRESHOLD: Final[int] = 1_000


class GateioInstrumentProvider(InstrumentProvider):
    """Loads Gate.io instrument definitions for the configured products.

    Parameters
    ----------
    http_client : GateioHttpClient
        The REST client used for the (public) instrument endpoints.
    config : InstrumentProviderConfig, optional
        Standard NautilusTrader provider configuration. ``config.filters`` may
        carry ``{"options_underlyings": [...]}`` to override the underlyings
        loaded for options.
    products : Sequence[GateioProductType], optional
        The product families to load. Defaults to spot only.
    options_underlyings : Sequence[str], optional
        Restricts option loading to these underlyings (for example
        ``["BTC_USDT"]``). When omitted, every underlying Gate.io lists is
        loaded, which is a large number of instruments.

    """

    def __init__(
        self,
        http_client: GateioHttpClient,
        config: InstrumentProviderConfig | None = None,
        products: Sequence[GateioProductType] | None = None,
        options_underlyings: Sequence[str] | None = None,
    ) -> None:
        super().__init__(config=config)
        self._http = http_client
        self._spot_http = GateioSpotHttpAPI(http_client)
        self._options_http = GateioOptionsHttpAPI(http_client)
        self._wallet_http = GateioWalletHttpAPI(http_client)
        self._futures_http: dict[GateioProductType, GateioFuturesHttpAPI] = {
            GateioProductType.PERP: GateioFuturesHttpAPI(http_client, settle="usdt"),
            GateioProductType.INVERSE: GateioFuturesHttpAPI(http_client, settle="btc"),
            GateioProductType.FUT: GateioFuturesHttpAPI(
                http_client,
                settle="usdt",
                delivery=True,
            ),
        }
        self._products: tuple[GateioProductType, ...] = tuple(
            products if products else (GateioProductType.SPOT,)
        )
        self._options_underlyings: tuple[str, ...] | None = (
            tuple(u.upper() for u in options_underlyings) if options_underlyings else None
        )
        self._spot_fees: tuple[Decimal | None, Decimal | None] | None = None

    @property
    def products(self) -> tuple[GateioProductType, ...]:
        """The product families this provider loads."""
        return self._products

    @property
    def options_underlyings(self) -> tuple[str, ...] | None:
        """The option underlyings this provider is restricted to (``None`` = all)."""
        return self._options_underlyings

    # -- loading -----------------------------------------------------------

    async def load_all_async(self, filters: dict | None = None) -> None:
        """Load every instrument of every configured product."""
        underlyings = self._resolve_options_underlyings(filters)

        for product in self._products:
            try:
                if product.is_spot:
                    await self._load_spot()
                elif product.is_perpetual:
                    await self._load_perpetuals(product)
                elif product.is_delivery:
                    await self._load_delivery()
                elif product.is_option:
                    await self._load_options(underlyings)
            except WalletNotProvisionedError as exc:
                self._log.warning(f"Skipping {product.value}: {exc}")
            except GateioError as exc:
                if _is_recoverable(exc):
                    self._log.warning(
                        f"Skipping {product.value}: Gate.io returned {exc.label} ({exc.message})"
                    )
                else:
                    raise

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """Load exactly the given instruments, one venue request each."""
        for instrument_id in instrument_ids:
            await self.load_async(instrument_id, filters)

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict | None = None,
    ) -> None:
        """Load a single instrument, resolving its product from the instrument id."""
        if instrument_id.venue != GATEIO_VENUE:
            self._log.error(
                f"Cannot load {instrument_id}: venue is not {GATEIO_VENUE}",
            )
            return

        try:
            product, raw_symbol = instrument_id_to_gateio(instrument_id)
        except ValueError as exc:
            self._log.error(f"Cannot load {instrument_id}: {exc}")
            return

        ts_init = _now_ns()
        try:
            if product.is_spot:
                payload = await self._spot_http.currency_pair(raw_symbol)
                status = str(payload.get("trade_status", "")).lower()
                if status == UNTRADABLE_SPOT_STATUS or _is_spot_one_sided(payload, ts_init / 1e9):
                    self._log.warning(
                        f"Cannot load {instrument_id}: the venue reports it as "
                        f"{status or 'not fully tradable'}"
                    )
                    return
                instrument = parse_spot_instrument(payload, *(await self._fees()), ts_init)
            elif product.is_perpetual:
                payload = await self._futures_http[product].contract(raw_symbol)
                instrument = parse_perpetual_instrument(payload, product, ts_init)
            elif product.is_delivery:
                payload = await self._futures_http[product].contract(raw_symbol)
                instrument = parse_delivery_instrument(payload, ts_init)
            else:
                payload = await self._options_http.contract(raw_symbol)
                instrument = parse_option_instrument(payload, ts_init)
        except WalletNotProvisionedError as exc:
            self._log.warning(f"Cannot load {instrument_id}: {exc}")
            return
        except GateioError as exc:
            self._log.error(f"Cannot load {instrument_id}: {exc}")
            return

        if instrument is None:
            self._log.warning(f"Cannot load {instrument_id}: unsupported instrument definition")
            return

        self._add(instrument)

    # -- per-product loaders -----------------------------------------------

    async def _load_spot(self) -> None:
        payloads = await self._spot_http.currency_pairs()
        fee_maker, fee_taker = await self._fees()
        ts_init = _now_ns()
        now_secs = ts_init / 1e9

        loaded = skipped_untradable = skipped_one_sided = failed = 0
        for payload in _as_list(payloads):
            if str(payload.get("trade_status", "")).lower() == UNTRADABLE_SPOT_STATUS:
                skipped_untradable += 1
                continue
            if _is_spot_one_sided(payload, now_secs):
                skipped_one_sided += 1
                continue
            instrument = parse_spot_instrument(payload, fee_maker, fee_taker, ts_init)
            if instrument is None:
                failed += 1
                continue
            self._add(instrument)
            loaded += 1

        self._log.info(
            f"Loaded {loaded} SPOT instruments (skipped {skipped_untradable} untradable, "
            f"{skipped_one_sided} one-sided, {failed} unsupported)"
        )

    async def _load_perpetuals(self, product: GateioProductType) -> None:
        payloads = await self._futures_http[product].contracts()
        ts_init = _now_ns()

        loaded = skipped = failed = 0
        for payload in _as_list(payloads):
            if _is_contract_inactive(payload):
                skipped += 1
                continue
            instrument = parse_perpetual_instrument(payload, product, ts_init)
            if instrument is None:
                failed += 1
                continue
            self._add(instrument)
            loaded += 1

        self._log.info(
            f"Loaded {loaded} {product.value} instruments "
            f"(skipped {skipped} delisting/inactive, {failed} unsupported)"
        )

    async def _load_delivery(self) -> None:
        product = GateioProductType.FUT
        payloads = await self._futures_http[product].contracts()
        ts_init = _now_ns()
        now_secs = ts_init / 1e9

        loaded = skipped = failed = 0
        for payload in _as_list(payloads):
            expire_time = float(to_decimal(payload.get("expire_time")))
            if _is_contract_inactive(payload) or (expire_time and expire_time <= now_secs):
                skipped += 1
                continue
            instrument = parse_delivery_instrument(payload, ts_init)
            if instrument is None:
                failed += 1
                continue
            self._add(instrument)
            loaded += 1

        self._log.info(
            f"Loaded {loaded} {product.value} instruments "
            f"(skipped {skipped} delisting/expired, {failed} unsupported)"
        )

    async def _load_options(self, underlyings: Sequence[str] | None) -> None:
        product = GateioProductType.OPT
        if underlyings is None:
            listed = await self._options_http.underlyings()
            names = tuple(str(item["name"]).upper() for item in _as_list(listed))
            self._log.warning(
                f"No option underlying filter configured: loading all {len(names)} "
                f"underlyings Gate.io lists. Pass `options_underlyings` to narrow this"
            )
        else:
            names = tuple(underlyings)

        total = failed = 0
        for underlying in names:
            try:
                count, bad = await self._load_options_for(underlying)
            except GateioError as exc:
                if _is_recoverable(exc):
                    self._log.warning(
                        f"Skipping options on {underlying}: {exc.label} ({exc.message})"
                    )
                    continue
                raise
            total += count
            failed += bad
            self._log.info(f"Loaded {count} {product.value} instruments on {underlying}")

        if total > OPTIONS_COUNT_WARNING_THRESHOLD:
            self._log.warning(
                f"Loaded {total} option instruments; consider restricting "
                f"`options_underlyings` to the underlyings actually traded"
            )
        self._log.info(
            f"Loaded {total} {product.value} instruments across "
            f"{len(names)} underlyings ({failed} unsupported)"
        )

    async def _load_options_for(self, underlying: str) -> tuple[int, int]:
        """Load the active expirations of one option underlying."""
        expirations = await self._options_http.expirations(underlying)
        ts_init = _now_ns()
        now_secs = ts_init / 1e9
        active = {int(e) for e in _as_list(expirations) if float(e) > now_secs}
        if not active:
            self._log.info(f"No active option expirations on {underlying}")
            return 0, 0

        payloads = await self._options_http.contracts(underlying)

        loaded = failed = 0
        for payload in _as_list(payloads):
            if not payload.get("is_active", True):
                continue
            expiration = payload.get("expiration_time")
            if expiration is None or int(float(expiration)) not in active:
                continue
            instrument = parse_option_instrument(payload, ts_init)
            if instrument is None:
                failed += 1
                continue
            self._add(instrument)
            loaded += 1

        return loaded, failed

    # -- helpers -----------------------------------------------------------

    def _add(self, instrument: Instrument) -> None:
        """Register an instrument together with every currency it references."""
        for attribute in ("base_currency", "quote_currency", "settlement_currency", "underlying"):
            currency = getattr(instrument, attribute, None)
            if currency is not None:
                self.add_currency(currency)
        self.add(instrument)

    async def _fees(self) -> tuple[Decimal | None, Decimal | None]:
        """The account's spot maker/taker fees, or ``(None, None)`` without credentials.

        ``GET /wallet/fee`` is the current source and returns spot, perpetual and
        delivery rates in one call; ``GET /spot/fee``, which Gate.io marks
        deprecated, is only a fallback. Both report fractions. When neither is
        available the spot parser falls back to the pair's deprecated percentage
        field.
        """
        if self._spot_fees is not None:
            return self._spot_fees
        if not self._http.has_credentials:
            self._spot_fees = (None, None)
            return self._spot_fees

        try:
            payload = await self._wallet_http.fee()
        except GateioError as exc:
            self._log.warning(f"Cannot read /wallet/fee ({exc.label}); falling back to /spot/fee")
            try:
                payload = await self._spot_http.fee()
            except GateioError as inner:
                self._log.warning(
                    f"Cannot read the account fee tier ({inner.label}); "
                    f"falling back to the published pair fees"
                )
                self._spot_fees = (None, None)
                return self._spot_fees

        maker = to_decimal(payload.get("maker_fee"))
        taker = to_decimal(payload.get("taker_fee"))
        self._spot_fees = (maker, taker)
        self._log.info(f"Spot fee tier: maker {maker}, taker {taker}")
        return self._spot_fees

    def _resolve_options_underlyings(self, filters: dict | None) -> tuple[str, ...] | None:
        override = (filters or {}).get("options_underlyings")
        if override:
            return tuple(str(u).upper() for u in override)
        return self._options_underlyings


# -- module helpers ------------------------------------------------------------


def _now_ns() -> int:
    return time.time_ns()


def _as_list(payload: Any) -> Iterable[Any]:
    """Normalise a venue response into an iterable of entries."""
    if payload is None:
        return ()
    if isinstance(payload, list):
        return payload
    return (payload,)


def _is_spot_one_sided(payload: dict[str, Any], now_secs: float) -> bool:
    """Whether only one side of a spot pair is currently open.

    Two independent venue signals say so: the ``buyable`` / ``sellable``
    ``trade_status`` values, and the ``buy_start`` / ``sell_start`` Unix-second
    timestamps that gate each side around a listing.
    """
    status = str(payload.get("trade_status", "")).lower()
    if status in ONE_SIDED_SPOT_STATUSES:
        return True

    for field in ("buy_start", "sell_start"):
        start = float(to_decimal(payload.get(field)))
        if start > now_secs:
            return True
    return False


def _is_contract_inactive(payload: dict[str, Any]) -> bool:
    """Whether a futures/delivery contract should not be published as tradable."""
    if payload.get("in_delisting"):
        return True
    status = str(payload.get("status", "")).lower()
    return status in ("delisting", "delisted")


def _is_recoverable(error: GateioError) -> bool:
    """Whether a product can simply be skipped instead of failing the load."""
    return error.label in WALLET_NOT_PROVISIONED_LABELS or error.label in ACCOUNT_MODE_LABELS
