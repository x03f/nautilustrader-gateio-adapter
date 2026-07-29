"""Tests for :class:`GateioInstrumentProvider` — no network, no credentials.

The provider is driven by a recording stand-in for ``GateioHttpClient`` that
serves canned payloads and remembers every request, so the tests can assert both
*what* was loaded and *how* it was requested.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioError, WalletNotProvisionedError
from nautilus_gateio.http.futures import GateioFuturesHttpAPI
from nautilus_gateio.http.options import GateioOptionsHttpAPI
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.http.wallet import GateioWalletHttpAPI
from nautilus_gateio.providers import GateioInstrumentProvider

FAR_FUTURE = 4_102_444_800  # 2100-01-01, comfortably beyond any test clock
LONG_PAST = 1_500_000_000  # 2017-07-14

# -- payloads ------------------------------------------------------------------

SPOT_BTC: dict[str, Any] = {
    "id": "BTC_USDT",
    "base": "BTC",
    "quote": "USDT",
    "fee": "0.2",
    "min_base_amount": "0.000001",
    "min_quote_amount": "3",
    "amount_precision": 6,
    "precision": 1,
    "trade_status": "tradable",
    "buy_start": 0,
    "sell_start": 0,
}

PERP_BTC: dict[str, Any] = {
    "name": "BTC_USDT",
    "type": "direct",
    "quanto_multiplier": "0.0001",
    "order_price_round": "0.1",
    "order_size_min": 1,
    "order_size_max": 1000000,
    "leverage_max": "200",
    "maintenance_rate": "0.003",
    "maker_fee_rate": "0.00015",
    "taker_fee_rate": "0.0005",
    "in_delisting": False,
    "status": "trading",
    "config_change_time": 1782119555,
}

INVERSE_BTC: dict[str, Any] = {
    **PERP_BTC,
    "name": "BTC_USD",
    "type": "inverse",
    "quanto_multiplier": "0",
}

DELIVERY_SOL: dict[str, Any] = {
    "name": "SOL_USDT_20260731",
    "underlying": "SOL_USDT",
    "type": "direct",
    "quanto_multiplier": "1",
    "order_price_round": "0.001",
    "order_size_min": 1,
    "order_size_max": 1000000,
    "leverage_max": "50",
    "maintenance_rate": "0.01",
    "maker_fee_rate": "-0.00015",
    "taker_fee_rate": "0.00025",
    "expire_time": FAR_FUTURE,
    "in_delisting": False,
    "config_change_time": 1784274301,
}

OPTION_ETH: dict[str, Any] = {
    "name": "ETH_USDT-20260729-2150-P",
    "underlying": "ETH_USDT",
    "is_call": False,
    "is_active": True,
    "strike_price": "2150",
    "multiplier": "0.01",
    "order_price_round": "0.1",
    "order_size_min": 1,
    "order_size_max": 30000,
    "maker_fee_rate": "0.0003",
    "taker_fee_rate": "0.0003",
    "create_time": 1784965682,
    "expiration_time": FAR_FUTURE,
}


class RecordingHttpClient:
    """Stands in for ``GateioHttpClient``, recording every request it serves."""

    def __init__(
        self,
        routes: dict[str, Any] | None = None,
        has_credentials: bool = False,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.errors = errors or {}
        self.has_credentials = has_credentials
        self.calls: list[tuple[str, str, dict[str, Any] | None, bool]] = []

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _, _ in self.calls]

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        self.calls.append(("GET", path, params, signed))
        if path in self.errors:
            raise self.errors[path]
        if path not in self.routes:
            raise GateioError(404, "NOT_FOUND", f"no canned response for {path}")
        return self.routes[path]


def spy_on(monkeypatch, cls: type, name: str, record: list[str]) -> None:
    """Wrap a namespace coroutine so calls through it are observable."""
    original = getattr(cls, name)

    async def wrapper(self, *args, **kwargs):
        record.append(f"{cls.__name__}.{name}")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, wrapper)


def make_provider(
    routes: dict[str, Any],
    products: tuple[GateioProductType, ...],
    options_underlyings: list[str] | None = None,
    **kwargs: Any,
) -> tuple[GateioInstrumentProvider, RecordingHttpClient]:
    http = RecordingHttpClient(routes, **kwargs)
    provider = GateioInstrumentProvider(
        http_client=http,  # type: ignore[arg-type]
        products=products,
        options_underlyings=options_underlyings,
    )
    return provider, http


ALL_ROUTES: dict[str, Any] = {
    "/spot/currency_pairs": [SPOT_BTC],
    "/futures/usdt/contracts": [PERP_BTC],
    "/futures/btc/contracts": [INVERSE_BTC],
    "/delivery/usdt/contracts": [DELIVERY_SOL],
    "/options/underlyings": [{"name": "ETH_USDT"}],
    "/options/expirations": [FAR_FUTURE],
    "/options/contracts": [OPTION_ETH],
}


# -- seam-06: the provider must go through the typed HTTP namespaces -----------


class TestUsesTypedHttpNamespaces:
    """The venue's path layout lives in ``nautilus_gateio.http``, not here.

    In particular the ``/futures/{settle}`` versus ``/delivery/{settle}`` split
    is what ``GateioFuturesHttpAPI(settle=..., delivery=...)`` exists to
    encapsulate; hand-rolled paths in the provider duplicate it silently.
    """

    def test_every_product_is_loaded_through_its_namespace(self, monkeypatch):
        seen: list[str] = []
        spy_on(monkeypatch, GateioSpotHttpAPI, "currency_pairs", seen)
        spy_on(monkeypatch, GateioFuturesHttpAPI, "contracts", seen)
        spy_on(monkeypatch, GateioOptionsHttpAPI, "underlyings", seen)
        spy_on(monkeypatch, GateioOptionsHttpAPI, "expirations", seen)
        spy_on(monkeypatch, GateioOptionsHttpAPI, "contracts", seen)

        provider, _ = make_provider(ALL_ROUTES, tuple(GateioProductType))
        asyncio.run(provider.load_all_async())

        assert seen == [
            "GateioSpotHttpAPI.currency_pairs",
            "GateioFuturesHttpAPI.contracts",  # PERP
            "GateioFuturesHttpAPI.contracts",  # INVERSE
            "GateioFuturesHttpAPI.contracts",  # FUT (delivery)
            "GateioOptionsHttpAPI.underlyings",
            "GateioOptionsHttpAPI.expirations",
            "GateioOptionsHttpAPI.contracts",
        ]

    def test_single_instrument_loads_go_through_the_namespaces(self, monkeypatch):
        seen: list[str] = []
        spy_on(monkeypatch, GateioSpotHttpAPI, "currency_pair", seen)
        spy_on(monkeypatch, GateioFuturesHttpAPI, "contract", seen)
        spy_on(monkeypatch, GateioOptionsHttpAPI, "contract", seen)

        routes = {
            "/spot/currency_pairs/BTC_USDT": SPOT_BTC,
            "/futures/usdt/contracts/BTC_USDT": PERP_BTC,
            "/futures/btc/contracts/BTC_USD": INVERSE_BTC,
            "/delivery/usdt/contracts/SOL_USDT_20260731": DELIVERY_SOL,
            "/options/contracts/ETH_USDT-20260729-2150-P": OPTION_ETH,
        }
        provider, http = make_provider(routes, (GateioProductType.SPOT,))

        asyncio.run(
            provider.load_ids_async(
                [
                    InstrumentId.from_str("BTC_USDT.GATE_IO"),
                    InstrumentId.from_str("BTC_USDT-PERP.GATE_IO"),
                    InstrumentId.from_str("BTC_USD-PERP.GATE_IO"),
                    InstrumentId.from_str("SOL_USDT_20260731.GATE_IO"),
                    InstrumentId.from_str("ETH_USDT-20260729-2150-P.GATE_IO"),
                ],
            ),
        )

        assert seen == [
            "GateioSpotHttpAPI.currency_pair",
            "GateioFuturesHttpAPI.contract",
            "GateioFuturesHttpAPI.contract",
            "GateioFuturesHttpAPI.contract",
            "GateioOptionsHttpAPI.contract",
        ]
        assert provider.count == 5
        assert http.paths == list(routes)

    def test_delivery_routes_to_the_delivery_base_path(self):
        provider, http = make_provider(ALL_ROUTES, (GateioProductType.FUT,))

        asyncio.run(provider.load_all_async())

        assert http.paths == ["/delivery/usdt/contracts"]

    def test_inverse_routes_to_the_btc_settle(self):
        provider, http = make_provider(ALL_ROUTES, (GateioProductType.INVERSE,))

        asyncio.run(provider.load_all_async())

        assert http.paths == ["/futures/btc/contracts"]

    def test_namespace_query_parameters_are_used(self):
        """The namespaces pass their documented parameter set, hand-rolled paths do not."""
        provider, http = make_provider(ALL_ROUTES, (GateioProductType.PERP,))

        asyncio.run(provider.load_all_async())

        assert http.calls == [
            ("GET", "/futures/usdt/contracts", {"limit": None, "offset": None}, False)
        ]


# -- SEAM-01: the account fee tier comes from /wallet/fee ----------------------


class TestFeeTier:
    def test_wallet_fee_is_preferred(self, monkeypatch):
        seen: list[str] = []
        spy_on(monkeypatch, GateioWalletHttpAPI, "fee", seen)

        routes = {
            **ALL_ROUTES,
            "/wallet/fee": {"maker_fee": "0.0009", "taker_fee": "0.0012", "user_id": 1},
        }
        provider, http = make_provider(
            routes,
            (GateioProductType.SPOT,),
            has_credentials=True,
        )

        asyncio.run(provider.load_all_async())

        assert seen == ["GateioWalletHttpAPI.fee"]
        assert "/wallet/fee" in http.paths
        assert "/spot/fee" not in http.paths
        instrument = provider.find(InstrumentId.from_str("BTC_USDT.GATE_IO"))
        assert instrument.maker_fee == Decimal("0.0009")
        assert instrument.taker_fee == Decimal("0.0012")

    def test_spot_fee_is_the_fallback(self):
        routes = {**ALL_ROUTES, "/spot/fee": {"maker_fee": "0.001", "taker_fee": "0.001"}}
        provider, http = make_provider(
            routes,
            (GateioProductType.SPOT,),
            has_credentials=True,
            errors={"/wallet/fee": GateioError(404, "NOT_FOUND", "gone")},
        )

        asyncio.run(provider.load_all_async())

        assert http.paths.index("/wallet/fee") < http.paths.index("/spot/fee")
        instrument = provider.find(InstrumentId.from_str("BTC_USDT.GATE_IO"))
        assert instrument.maker_fee == Decimal("0.001")

    def test_pair_percentage_is_used_when_both_endpoints_fail(self):
        provider, http = make_provider(
            ALL_ROUTES,
            (GateioProductType.SPOT,),
            has_credentials=True,
            errors={
                "/wallet/fee": GateioError(404, "NOT_FOUND", "gone"),
                "/spot/fee": GateioError(403, "FORBIDDEN", "no permission"),
            },
        )

        asyncio.run(provider.load_all_async())

        instrument = provider.find(InstrumentId.from_str("BTC_USDT.GATE_IO"))
        assert instrument.maker_fee == Decimal("0.002")  # "0.2" percent

    def test_no_signed_request_without_credentials(self):
        provider, http = make_provider(ALL_ROUTES, (GateioProductType.SPOT,))

        asyncio.run(provider.load_all_async())

        assert all(not signed for _, _, _, signed in http.calls)
        assert "/wallet/fee" not in http.paths


# -- GIO-DOM-5 and the other tradability filters ------------------------------


class TestSpotFilters:
    def _load(self, pairs: list[dict[str, Any]]) -> GateioInstrumentProvider:
        provider, _ = make_provider(
            {**ALL_ROUTES, "/spot/currency_pairs": pairs},
            (GateioProductType.SPOT,),
        )
        asyncio.run(provider.load_all_async())
        return provider

    def test_tradable_pair_is_published(self):
        provider = self._load([SPOT_BTC])

        assert provider.find(InstrumentId.from_str("BTC_USDT.GATE_IO")) is not None

    def test_untradable_pair_is_skipped(self):
        pair = {**SPOT_BTC, "id": "ETH_USDT", "base": "ETH", "trade_status": "untradable"}

        provider = self._load([SPOT_BTC, pair])

        assert provider.find(InstrumentId.from_str("ETH_USDT.GATE_IO")) is None
        assert provider.count == 1

    @pytest.mark.parametrize("status", ["buyable", "sellable", "BUYABLE"])
    def test_one_sided_pair_is_skipped(self, status):
        """A ``CurrencyPair`` cannot express a market that only accepts one side."""
        pair = {**SPOT_BTC, "id": "ETH_USDT", "base": "ETH", "trade_status": status}

        provider = self._load([SPOT_BTC, pair])

        assert provider.find(InstrumentId.from_str("ETH_USDT.GATE_IO")) is None
        assert provider.count == 1

    @pytest.mark.parametrize("field", ["buy_start", "sell_start"])
    def test_pair_with_a_side_not_yet_open_is_skipped(self, field):
        pair = {**SPOT_BTC, "id": "ETH_USDT", "base": "ETH", field: FAR_FUTURE}

        provider = self._load([SPOT_BTC, pair])

        assert provider.find(InstrumentId.from_str("ETH_USDT.GATE_IO")) is None

    @pytest.mark.parametrize("field", ["buy_start", "sell_start"])
    def test_pair_whose_sides_already_opened_is_published(self, field):
        pair = {**SPOT_BTC, "id": "ETH_USDT", "base": "ETH", field: LONG_PAST}

        provider = self._load([SPOT_BTC, pair])

        assert provider.find(InstrumentId.from_str("ETH_USDT.GATE_IO")) is not None

    def test_unparsable_pair_does_not_abort_the_batch(self):
        provider = self._load([{"id": "BROKEN"}, SPOT_BTC])

        assert provider.count == 1

    @pytest.mark.parametrize(
        "override",
        [
            {"trade_status": "untradable"},
            {"trade_status": "buyable"},
            {"trade_status": "sellable"},
            {"buy_start": FAR_FUTURE},
        ],
    )
    def test_explicit_single_load_applies_the_same_filters(self, override):
        provider, _ = make_provider(
            {"/spot/currency_pairs/BTC_USDT": {**SPOT_BTC, **override}},
            (GateioProductType.SPOT,),
        )

        asyncio.run(provider.load_async(InstrumentId.from_str("BTC_USDT.GATE_IO")))

        assert provider.count == 0


class TestContractFilters:
    def _load_perps(self, contracts: list[dict[str, Any]]) -> GateioInstrumentProvider:
        provider, _ = make_provider(
            {**ALL_ROUTES, "/futures/usdt/contracts": contracts},
            (GateioProductType.PERP,),
        )
        asyncio.run(provider.load_all_async())
        return provider

    def _load_delivery(self, contracts: list[dict[str, Any]]) -> GateioInstrumentProvider:
        provider, _ = make_provider(
            {**ALL_ROUTES, "/delivery/usdt/contracts": contracts},
            (GateioProductType.FUT,),
        )
        asyncio.run(provider.load_all_async())
        return provider

    def test_perpetual_is_published(self):
        provider = self._load_perps([PERP_BTC])

        assert provider.find(InstrumentId.from_str("BTC_USDT-PERP.GATE_IO")) is not None

    def test_delisting_perpetual_is_skipped(self):
        contract = {**PERP_BTC, "name": "ETH_USDT", "in_delisting": True}

        provider = self._load_perps([PERP_BTC, contract])

        assert provider.find(InstrumentId.from_str("ETH_USDT-PERP.GATE_IO")) is None

    @pytest.mark.parametrize("status", ["delisting", "delisted"])
    def test_inactive_perpetual_is_skipped(self, status):
        contract = {**PERP_BTC, "name": "ETH_USDT", "status": status}

        provider = self._load_perps([PERP_BTC, contract])

        assert provider.find(InstrumentId.from_str("ETH_USDT-PERP.GATE_IO")) is None

    def test_delivery_contract_is_published(self):
        provider = self._load_delivery([DELIVERY_SOL])

        assert provider.find(InstrumentId.from_str("SOL_USDT_20260731.GATE_IO")) is not None

    def test_expired_delivery_contract_is_skipped(self):
        contract = {**DELIVERY_SOL, "name": "SOL_USDT_20200101", "expire_time": LONG_PAST}

        provider = self._load_delivery([DELIVERY_SOL, contract])

        assert provider.find(InstrumentId.from_str("SOL_USDT_20200101.GATE_IO")) is None
        assert provider.count == 1

    def test_delisting_delivery_contract_is_skipped(self):
        contract = {**DELIVERY_SOL, "name": "SOL_USDT_20260807", "in_delisting": True}

        provider = self._load_delivery([DELIVERY_SOL, contract])

        assert provider.find(InstrumentId.from_str("SOL_USDT_20260807.GATE_IO")) is None


class TestOptionFilters:
    def _load(self, contracts: list[dict[str, Any]], expirations: list[int]):
        provider, http = make_provider(
            {
                **ALL_ROUTES,
                "/options/contracts": contracts,
                "/options/expirations": expirations,
            },
            (GateioProductType.OPT,),
            options_underlyings=["ETH_USDT"],
        )
        asyncio.run(provider.load_all_async())
        return provider, http

    def test_active_option_is_published(self):
        provider, http = self._load([OPTION_ETH], [FAR_FUTURE])

        assert provider.find(InstrumentId.from_str("ETH_USDT-20260729-2150-P.GATE_IO")) is not None
        assert "/options/underlyings" not in http.paths  # underlying filter honoured

    def test_inactive_option_is_skipped(self):
        contract = {**OPTION_ETH, "is_active": False}

        provider, _ = self._load([contract], [FAR_FUTURE])

        assert provider.count == 0

    def test_option_outside_an_active_expiration_is_skipped(self):
        contract = {**OPTION_ETH, "expiration_time": LONG_PAST}

        provider, _ = self._load([contract], [FAR_FUTURE])

        assert provider.count == 0

    def test_expired_underlying_is_not_requested(self):
        provider, http = self._load([OPTION_ETH], [LONG_PAST])

        assert provider.count == 0
        assert "/options/contracts" not in http.paths


# -- resilience ----------------------------------------------------------------


class TestPartialFailures:
    def test_unprovisioned_wallet_skips_only_that_product(self):
        provider, _ = make_provider(
            ALL_ROUTES,
            (GateioProductType.PERP, GateioProductType.SPOT),
            errors={
                "/futures/usdt/contracts": WalletNotProvisionedError(
                    "futures wallet not provisioned",
                ),
            },
        )

        asyncio.run(provider.load_all_async())

        assert provider.find(InstrumentId.from_str("BTC_USDT.GATE_IO")) is not None
        assert provider.find(InstrumentId.from_str("BTC_USDT-PERP.GATE_IO")) is None

    def test_recoverable_venue_label_skips_only_that_product(self):
        provider, _ = make_provider(
            ALL_ROUTES,
            (GateioProductType.PERP, GateioProductType.SPOT),
            errors={
                "/futures/usdt/contracts": GateioError(
                    400,
                    "USER_NOT_FOUND",
                    "please transfer funds first to create futures account",
                ),
            },
        )

        asyncio.run(provider.load_all_async())

        assert provider.find(InstrumentId.from_str("BTC_USDT.GATE_IO")) is not None
        assert provider.find(InstrumentId.from_str("BTC_USDT-PERP.GATE_IO")) is None

    def test_unexpected_error_is_not_swallowed(self):
        provider, _ = make_provider(
            ALL_ROUTES,
            (GateioProductType.SPOT,),
            errors={"/spot/currency_pairs": GateioError(500, "SERVER_ERROR", "boom")},
        )

        with pytest.raises(GateioError):
            asyncio.run(provider.load_all_async())

    def test_foreign_venue_is_refused(self):
        provider, http = make_provider(ALL_ROUTES, (GateioProductType.SPOT,))

        asyncio.run(provider.load_async(InstrumentId.from_str("BTC_USDT.BINANCE")))

        assert http.calls == []
        assert provider.count == 0

    def test_currencies_are_registered_with_the_instrument(self):
        provider, _ = make_provider(ALL_ROUTES, (GateioProductType.SPOT,))

        asyncio.run(provider.load_all_async())

        assert {"BTC", "USDT"} <= set(provider.currencies())
