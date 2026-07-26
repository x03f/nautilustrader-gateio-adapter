"""Tests for the Gate.io payload -> NautilusTrader instrument parsers.

No network and no credentials: every payload below is a static copy of the
shape ``GET /spot/currency_pairs``, ``GET /futures/{settle}/contracts``,
``GET /delivery/{settle}/contracts`` and ``GET /options/contracts`` return.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.accounting.margin_models import LeveragedMarginModel
from nautilus_trader.model.enums import OptionKind, PositionSide
from nautilus_trader.model.instruments import (
    CryptoFuture,
    CryptoOption,
    CryptoPerpetual,
    CurrencyPair,
)
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.tick_scheme.base import get_tick_scheme, list_tick_schemes

import nautilus_gateio.instruments as instruments
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.instruments import (
    CONTRACT_MARGIN_INIT,
    INVERSE_CONTRACT_FACE_VALUE,
    MAX_PRECISION,
    parse_delivery_instrument,
    parse_instrument,
    parse_option_instrument,
    parse_perpetual_instrument,
    parse_spot_instrument,
)

# -- payload fixtures ----------------------------------------------------------


@pytest.fixture
def spot_payload() -> dict[str, Any]:
    """``BTC_USDT`` spot pair (price scale 1, size scale 6)."""
    return {
        "id": "BTC_USDT",
        "base": "BTC",
        "quote": "USDT",
        "fee": "0.2",  # PERCENT, deprecated per-pair field
        "min_base_amount": "0.000001",
        "min_quote_amount": "3",
        "max_base_amount": "100",
        "max_quote_amount": "5000000",
        "amount_precision": 6,
        "precision": 1,
        "trade_status": "tradable",
        "sell_start": 0,
        "buy_start": 0,
    }


@pytest.fixture
def high_precision_spot_payload() -> dict[str, Any]:
    """A live-shaped pair quoting 14 decimals, unrepresentable on standard builds."""
    return {
        "id": "PEPE2_USDT",
        "base": "PEPE2",
        "quote": "USDT",
        "fee": "0.2",
        "min_base_amount": "1",
        "min_quote_amount": "3",
        "amount_precision": 0,
        "precision": 14,
        "trade_status": "tradable",
        "sell_start": 0,
        "buy_start": 0,
    }


@pytest.fixture
def perp_payload() -> dict[str, Any]:
    """``BTC_USDT`` linear perpetual: 0.0001 BTC per contract, 200x cap."""
    return {
        "name": "BTC_USDT",
        "type": "direct",
        "quanto_multiplier": "0.0001",
        "order_price_round": "0.1",
        "mark_price_round": "0.1",
        "order_size_min": 1,
        "order_size_max": 1000000,
        "leverage_min": "1",
        "leverage_max": "200",
        "cross_leverage_default": "10",
        "maintenance_rate": "0.003",
        "maker_fee_rate": "0.00015",
        "taker_fee_rate": "0.0005",
        "in_delisting": False,
        "status": "trading",
        "config_change_time": 1782119555,
        "create_time": 1577404800,
    }


@pytest.fixture
def perp_odd_tick_payload(perp_payload: dict[str, Any]) -> dict[str, Any]:
    """``BNB_USDT`` perpetual: the tick (0.05) is not a power of ten."""
    return {
        **perp_payload,
        "name": "BNB_USDT",
        "quanto_multiplier": "0.001",
        "order_price_round": "0.05",
        "leverage_max": "75",
        "maintenance_rate": "0.0066",
    }


@pytest.fixture
def inverse_payload() -> dict[str, Any]:
    """``BTC_USD`` inverse perpetual: ``quanto_multiplier`` is the "0" sentinel."""
    return {
        "name": "BTC_USD",
        "type": "inverse",
        "quanto_multiplier": "0",
        "order_price_round": "0.1",
        "order_size_min": 1,
        "order_size_max": 530000,
        "leverage_min": "1",
        "leverage_max": "100",
        "cross_leverage_default": "10",
        "maintenance_rate": "0.005",
        "maker_fee_rate": "-0.0002",
        "taker_fee_rate": "0.00075",
        "in_delisting": False,
        "status": "trading",
        "config_change_time": 1776852838,
        "create_time": 1545235200,
    }


@pytest.fixture
def delivery_payload() -> dict[str, Any]:
    """``SOL_USDT_20260731`` dated future: 1 SOL per contract."""
    return {
        "name": "SOL_USDT_20260731",
        "underlying": "SOL_USDT",
        "type": "direct",
        "cycle": "BI-WEEKLY",
        "quanto_multiplier": "1",
        "order_price_round": "0.001",
        "order_size_min": 1,
        "order_size_max": 1000000,
        "leverage_min": "1",
        "leverage_max": "50",
        "maintenance_rate": "0.01",
        "maker_fee_rate": "-0.00015",
        "taker_fee_rate": "0.00025",
        "expire_time": 1785484800,
        "settle_price": "0",
        "in_delisting": False,
        "config_change_time": 1784274301,
    }


@pytest.fixture
def option_payload() -> dict[str, Any]:
    """``ETH_USDT-20260729-2150-P``: European put, 0.01 ETH per contract."""
    return {
        "name": "ETH_USDT-20260729-2150-P",
        "underlying": "ETH_USDT",
        "is_call": False,
        "is_active": True,
        "strike_price": "2150",
        "multiplier": "0.01",
        "order_price_round": "0.1",
        "mark_price_round": "0.01",
        "order_size_min": 1,
        "order_size_max": 30000,
        "maker_fee_rate": "0.0003",
        "taker_fee_rate": "0.0003",
        "init_margin_high": "0.15",
        "init_margin_low": "0.1",
        "maint_margin_base": "0.075",
        "create_time": 1784965682,
        "expiration_time": 1785312000,
    }


# -- precision and increment derivation ---------------------------------------


class TestPrecisionDerivation:
    def test_spot_scales_come_from_precision_fields(self, spot_payload):
        instrument = parse_spot_instrument(spot_payload)

        assert isinstance(instrument, CurrencyPair)
        assert instrument.price_precision == 1
        assert instrument.size_precision == 6
        assert instrument.price_increment == Price.from_str("0.1")
        assert instrument.size_increment == Quantity.from_str("0.000001")

    def test_perpetual_tick_comes_from_order_price_round(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)

        assert instrument.price_precision == 1
        assert instrument.price_increment == Price.from_str("0.1")

    def test_perpetual_tick_is_used_verbatim_when_not_a_power_of_ten(
        self,
        perp_odd_tick_payload,
    ):
        instrument = parse_perpetual_instrument(perp_odd_tick_payload, GateioProductType.PERP)

        assert instrument.price_precision == 2
        assert instrument.price_increment == Price.from_str("0.05")

    def test_delivery_tick_comes_from_order_price_round(self, delivery_payload):
        instrument = parse_delivery_instrument(delivery_payload)

        assert instrument.price_precision == 3
        assert instrument.price_increment == Price.from_str("0.001")

    def test_option_tick_comes_from_order_price_round(self, option_payload):
        instrument = parse_option_instrument(option_payload)

        assert instrument.price_precision == 1
        assert instrument.price_increment == Price.from_str("0.1")

    @pytest.mark.parametrize("product", ["perp", "inverse", "delivery", "option"])
    def test_contract_sizes_are_whole_contracts(
        self,
        product,
        perp_payload,
        inverse_payload,
        delivery_payload,
        option_payload,
    ):
        instrument = {
            "perp": lambda: parse_perpetual_instrument(perp_payload, GateioProductType.PERP),
            "inverse": lambda: parse_perpetual_instrument(
                inverse_payload,
                GateioProductType.INVERSE,
            ),
            "delivery": lambda: parse_delivery_instrument(delivery_payload),
            "option": lambda: parse_option_instrument(option_payload),
        }[product]()

        assert instrument.size_precision == 0
        assert instrument.size_increment == Quantity.from_int(1)
        assert instrument.min_quantity == Quantity.from_int(1)


# -- tick schemes -------------------------------------------------------------


class TestTickSchemes:
    """Every instrument must carry a tick scheme, and it must be the right grid.

    ``Instrument.next_bid_price`` / ``next_ask_price`` raise ``ValueError`` when
    an instrument has no ``tick_scheme_name``, and the platform's stock
    ``FIXED_PRECISION_{n}`` scheme walks the price in ``10**-n`` steps — wrong for
    the Gate.io contracts that quote two decimals but tick in ``0.05``.
    """

    def _build(self, product, spot_payload, perp_payload, delivery_payload, option_payload):
        return {
            "spot": lambda: parse_spot_instrument(spot_payload),
            "perp": lambda: parse_perpetual_instrument(perp_payload, GateioProductType.PERP),
            "delivery": lambda: parse_delivery_instrument(delivery_payload),
            "option": lambda: parse_option_instrument(option_payload),
        }[product]()

    @pytest.mark.parametrize("product", ["spot", "perp", "delivery", "option"])
    def test_every_product_carries_a_tick_scheme(
        self,
        product,
        spot_payload,
        perp_payload,
        delivery_payload,
        option_payload,
    ):
        instrument = self._build(
            product, spot_payload, perp_payload, delivery_payload, option_payload
        )

        assert instrument.tick_scheme_name is not None
        # Regression: without a scheme both of these raise ValueError.
        assert instrument.next_bid_price(100.0) is not None
        assert instrument.next_ask_price(100.0) is not None
        assert len(instrument.next_bid_prices(100.0, num_ticks=3)) == 3

    @pytest.mark.parametrize("product", ["spot", "perp", "delivery", "option"])
    def test_a_power_of_ten_grid_uses_the_platform_scheme(
        self,
        product,
        spot_payload,
        perp_payload,
        delivery_payload,
        option_payload,
    ):
        """No bespoke scheme is registered where the platform already has one."""
        instrument = self._build(
            product, spot_payload, perp_payload, delivery_payload, option_payload
        )

        assert instrument.tick_scheme_name == f"FIXED_PRECISION_{instrument.price_precision}"

    def test_an_off_decimal_tick_gets_its_own_registered_scheme(self, perp_odd_tick_payload):
        """Regression: BNB_USDT ticks in 0.05 with a price precision of 2.

        ``FIXED_PRECISION_2`` would step the price by 0.01 and hand back prices
        Gate.io refuses, so the venue increment needs a scheme of its own.
        """
        instrument = parse_perpetual_instrument(perp_odd_tick_payload, GateioProductType.PERP)

        assert instrument.tick_scheme_name == "GATEIO_TICK_0.05_P2"
        assert instrument.tick_scheme_name.startswith(instruments.TICK_SCHEME_PREFIX)
        assert get_tick_scheme(instrument.tick_scheme_name).increment == Price.from_str("0.05")

    def test_an_off_decimal_grid_snaps_prices_to_the_venue_tick(self, perp_odd_tick_payload):
        instrument = parse_perpetual_instrument(perp_odd_tick_payload, GateioProductType.PERP)

        # 612.33 is a legal 2-decimal price and an illegal Gate.io price.
        assert instrument.next_bid_price(612.33) == Price.from_str("612.30")
        assert instrument.next_ask_price(612.33) == Price.from_str("612.35")
        assert instrument.next_bid_prices(612.33, num_ticks=3) == [
            Price.from_str("612.30"),
            Price.from_str("612.25"),
            Price.from_str("612.20"),
        ]

    def test_the_bespoke_scheme_keeps_the_bounds_of_the_stock_one(self, perp_odd_tick_payload):
        """Only the grid differs from ``FIXED_PRECISION_2``, not the price range."""
        instrument = parse_perpetual_instrument(perp_odd_tick_payload, GateioProductType.PERP)

        scheme = get_tick_scheme(instrument.tick_scheme_name)
        stock = get_tick_scheme("FIXED_PRECISION_2")
        assert scheme.min_price == stock.min_price
        assert scheme.max_price == stock.max_price

    def test_reparsing_does_not_re_register_the_scheme(self, perp_odd_tick_payload):
        """Providers re-parse every instrument on each load; registration is global."""
        first = parse_perpetual_instrument(perp_odd_tick_payload, GateioProductType.PERP)
        second = parse_perpetual_instrument(perp_odd_tick_payload, GateioProductType.PERP)

        assert first is not None
        assert second is not None
        assert first.tick_scheme_name == second.tick_scheme_name
        assert list_tick_schemes().count(second.tick_scheme_name) == 1

    def test_a_delivery_contract_can_tick_off_decimal_too(self, delivery_payload):
        """ETH_USDT_20261225 and ETH_USDT_20260925 tick in 0.05 as well."""
        payload = {**delivery_payload, "name": "ETH_USDT_20261225", "order_price_round": "0.05"}

        instrument = parse_delivery_instrument(payload)

        assert instrument.tick_scheme_name == "GATEIO_TICK_0.05_P2"
        assert instrument.next_ask_price(3011.02) == Price.from_str("3011.05")


# -- GIO-DOM-3: the standard-precision guard ----------------------------------


class TestStandardPrecisionGuard:
    """A price scale the running build cannot represent must never be published.

    On a stock NautilusTrader wheel ``FIXED_PRECISION`` is 9, and Gate.io lists
    spot pairs quoting up to 14 decimals. Clamping the scale silently produced a
    pair whose every price quantises to ``0.000000000``.
    """

    @pytest.fixture(autouse=True)
    def _standard_precision_build(self, monkeypatch):
        monkeypatch.setattr(instruments, "MAX_PRECISION", 9)

    def test_unrepresentable_spot_pair_is_rejected(self, high_precision_spot_payload):
        assert parse_spot_instrument(high_precision_spot_payload) is None

    def test_unrepresentable_spot_size_scale_is_rejected(self, spot_payload):
        payload = {**spot_payload, "amount_precision": 12}

        assert parse_spot_instrument(payload) is None

    def test_unrepresentable_contract_tick_is_rejected(self, perp_payload):
        payload = {**perp_payload, "order_price_round": "0.00000000001"}

        assert parse_perpetual_instrument(payload, GateioProductType.PERP) is None

    def test_unrepresentable_option_tick_is_rejected(self, option_payload):
        payload = {**option_payload, "order_price_round": "0.00000000001"}

        assert parse_option_instrument(payload) is None

    def test_representable_pairs_still_load(self, spot_payload, perp_payload):
        assert parse_spot_instrument(spot_payload) is not None
        assert parse_perpetual_instrument(perp_payload, GateioProductType.PERP) is not None

    @pytest.mark.parametrize("precision", [10, 11, 12, 13, 14])
    def test_no_instrument_is_ever_built_with_a_zero_price_increment(
        self,
        spot_payload,
        perp_payload,
        option_payload,
        precision,
    ):
        """The invariant, stated directly.

        A published instrument always carries a positive price increment at the
        venue's own scale; a scale this build cannot represent yields ``None``,
        never a silently clamped instrument.
        """
        tick = "0." + "0" * (precision - 1) + "1"
        candidates = [
            parse_spot_instrument({**spot_payload, "precision": precision}),
            parse_perpetual_instrument(
                {**perp_payload, "order_price_round": tick},
                GateioProductType.PERP,
            ),
            parse_option_instrument({**option_payload, "order_price_round": tick}),
        ]

        for instrument in candidates:
            if instrument is None:
                continue
            assert instrument.price_increment.as_decimal() > 0
            assert instrument.price_precision == precision  # never silently clamped

    def test_positive_size_bound_that_rounds_to_zero_is_dropped(self, spot_payload):
        payload = {**spot_payload, "amount_precision": 2, "max_base_amount": "0.0001"}

        instrument = parse_spot_instrument(payload)

        assert instrument is not None
        assert instrument.max_quantity is None


def test_high_precision_pair_is_rejected_on_this_build_too(high_precision_spot_payload):
    """Same guard without monkeypatching, whichever build runs the suite."""
    payload = {**high_precision_spot_payload, "precision": MAX_PRECISION + 1}

    assert parse_spot_instrument(payload) is None


# -- GIO-DOM-2: margins --------------------------------------------------------


class TestContractMargins:
    """``margin_init`` must not come from ``leverage_max``.

    ``leverage_max`` is the venue's maximum permitted leverage, so deriving
    ``margin_init = 1 / leverage_max`` reserved the smallest initial margin the
    venue could ever require (1/200 of notional on ``BTC_USDT``).
    """

    def test_perpetual_margin_values(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)

        assert instrument.margin_init == Decimal(1) == CONTRACT_MARGIN_INIT
        assert instrument.margin_maint == Decimal("0.003")  # payload maintenance_rate

    def test_delivery_margin_values(self, delivery_payload):
        instrument = parse_delivery_instrument(delivery_payload)

        assert instrument.margin_init == Decimal(1)
        assert instrument.margin_maint == Decimal("0.01")  # payload maintenance_rate

    def test_margin_init_is_not_derived_from_leverage_max(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)

        leverage_max = Decimal(perp_payload["leverage_max"])
        assert instrument.margin_init != Decimal(1) / leverage_max

    def test_leverage_fields_remain_available_in_info(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)

        assert instrument.info["leverage_max"] == "200"
        assert instrument.info["cross_leverage_default"] == "10"
        assert instrument.info["maintenance_rate"] == "0.003"

    def test_initial_margin_reserved_matches_the_venue_at_the_declared_leverage(
        self,
        perp_payload,
    ):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)
        model = LeveragedMarginModel()
        quantity = Quantity.from_int(100)
        price = Price.from_str("100000.0")

        notional = instrument.notional_value(quantity, price).as_decimal()
        assert notional == Decimal("1000")  # 100 contracts x 0.0001 BTC x 100000

        at_1x = model.calculate_margin_init(instrument, quantity, price, Decimal(1))
        at_10x = model.calculate_margin_init(instrument, quantity, price, Decimal(10))

        assert at_1x.as_decimal() == notional
        assert at_10x.as_decimal() == notional / 10  # Gate.io's 10x cross default

    def test_maintenance_margin_uses_the_first_tier_rate(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)
        model = LeveragedMarginModel()
        quantity = Quantity.from_int(100)
        price = Price.from_str("100000.0")

        maintenance = model.calculate_margin_maint(
            instrument,
            PositionSide.LONG,
            quantity,
            price,
            Decimal(1),
        )

        assert maintenance.as_decimal() == Decimal("1000") * Decimal("0.003")

    def test_spot_carries_no_margin(self, spot_payload):
        instrument = parse_spot_instrument(spot_payload)

        assert instrument.margin_init == Decimal(0)
        assert instrument.margin_maint == Decimal(0)


# -- notional arithmetic per product ------------------------------------------


class TestNotionalArithmetic:
    """``notional = contracts x multiplier x price`` for every contract product."""

    def test_linear_perpetual(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)

        assert instrument.multiplier == Quantity.from_str("0.0001")
        assert instrument.is_inverse is False
        assert instrument.settlement_currency.code == "USDT"

        notional = instrument.notional_value(Quantity.from_int(250), Price.from_str("64000.0"))
        assert notional.as_decimal() == Decimal(250) * Decimal("0.0001") * Decimal(64000)
        assert notional.currency.code == "USDT"

    def test_inverse_perpetual_uses_the_documented_face_value(self, inverse_payload):
        instrument = parse_perpetual_instrument(inverse_payload, GateioProductType.INVERSE)

        # `quanto_multiplier: "0"` is a null sentinel, never a multiplier.
        assert instrument.multiplier == Quantity.from_int(1)
        assert instrument.is_inverse is True
        assert instrument.settlement_currency.code == "BTC"

        quantity = Quantity.from_int(100)
        price = Price.from_str("64000.0")

        in_quote = instrument.notional_value(quantity, price, use_quote_for_inverse=True)
        assert in_quote.as_decimal() == Decimal(100)  # 100 contracts x 1 USD
        assert in_quote.currency.code == "USD"

        in_base = instrument.notional_value(quantity, price, use_quote_for_inverse=False)
        assert in_base.as_decimal() == Decimal(100) / Decimal(64000)
        assert in_base.currency.code == "BTC"

    def test_inverse_face_value_prefers_a_populated_payload_value(self, inverse_payload):
        """GIO-DOM-4: a real ``quanto_multiplier`` must win over the fallback."""
        payload = {**inverse_payload, "name": "ETH_USD", "quanto_multiplier": "10"}

        instrument = parse_perpetual_instrument(payload, GateioProductType.INVERSE)

        assert instrument.multiplier == Quantity.from_int(10)
        assert instrument.multiplier != Quantity.from_str(str(INVERSE_CONTRACT_FACE_VALUE))

        # The base-denominated notional honours the face value.
        notional = instrument.notional_value(
            Quantity.from_int(7),
            Price.from_str("64000.0"),
            use_quote_for_inverse=False,
        )
        assert notional.as_decimal() == pytest.approx(Decimal(7) * Decimal(10) / Decimal(64000))

        # NautilusTrader's quote-denominated branch for inverse instruments treats
        # one contract as one unit of quote currency and ignores the multiplier,
        # which is why the parser logs a warning for such a contract.
        in_quote = instrument.notional_value(
            Quantity.from_int(7),
            Price.from_str("64000.0"),
            use_quote_for_inverse=True,
        )
        assert in_quote.as_decimal() == Decimal(7)

    def test_delivery_future(self, delivery_payload):
        instrument = parse_delivery_instrument(delivery_payload)

        assert isinstance(instrument, CryptoFuture)
        assert instrument.multiplier == Quantity.from_int(1)
        assert instrument.underlying.code == "SOL"
        assert instrument.settlement_currency.code == "USDT"

        notional = instrument.notional_value(Quantity.from_int(10), Price.from_str("74.500"))
        assert notional.as_decimal() == Decimal(10) * Decimal(1) * Decimal("74.5")

    def test_option_premium(self, option_payload):
        instrument = parse_option_instrument(option_payload)

        assert isinstance(instrument, CryptoOption)
        assert instrument.multiplier == Quantity.from_str("0.01")

        notional = instrument.notional_value(Quantity.from_int(10), Price.from_str("278.2"))
        assert notional.as_decimal() == Decimal(10) * Decimal("0.01") * Decimal("278.2")
        assert notional.currency.code == "USDT"

    def test_linear_multiplier_sentinel_is_rejected(self, perp_payload):
        """The "0" sentinel is only valid on inverse contracts."""
        payload = {**perp_payload, "quanto_multiplier": "0"}

        assert parse_perpetual_instrument(payload, GateioProductType.PERP) is None


# -- option symbol decomposition ----------------------------------------------


class TestOptionParsing:
    def test_put_strike_kind_and_expiry(self, option_payload):
        instrument = parse_option_instrument(option_payload)

        assert str(instrument.id) == "ETH_USDT-20260729-2150-P.GATE_IO"
        assert str(instrument.raw_symbol) == "ETH_USDT-20260729-2150-P"
        assert instrument.option_kind == OptionKind.PUT
        assert instrument.strike_price == Price.from_str("2150.0")
        assert instrument.expiration_ns == 1785312000 * 1_000_000_000
        assert instrument.activation_ns == 1784965682 * 1_000_000_000

    def test_call(self, option_payload):
        payload = {
            **option_payload,
            "name": "ETH_USDT-20260729-2150-C",
            "is_call": True,
        }

        instrument = parse_option_instrument(payload)

        assert instrument.option_kind == OptionKind.CALL

    def test_fractional_strike_keeps_its_scale(self, option_payload):
        payload = {
            **option_payload,
            "name": "SOL_USDT-20260729-137.5-C",
            "underlying": "SOL_USDT",
            "is_call": True,
            "strike_price": "137.5",
        }

        instrument = parse_option_instrument(payload)

        assert instrument.strike_price == Price.from_str("137.5")

    def test_kind_disagreement_is_rejected_not_guessed(self, option_payload):
        payload = {**option_payload, "is_call": True}  # symbol says P

        assert parse_option_instrument(payload) is None

    def test_missing_expiration_is_rejected(self, option_payload):
        payload = {k: v for k, v in option_payload.items() if k != "expiration_time"}

        assert parse_option_instrument(payload) is None

    def test_settlement_is_the_quote_currency(self, option_payload):
        instrument = parse_option_instrument(option_payload)

        assert instrument.underlying.code == "ETH"
        assert instrument.quote_currency.code == "USDT"
        assert instrument.settlement_currency.code == "USDT"
        assert instrument.is_inverse is False


# -- delivery specifics --------------------------------------------------------


class TestDeliveryParsing:
    def test_symbol_expiry_and_underlying(self, delivery_payload):
        instrument = parse_delivery_instrument(delivery_payload)

        assert str(instrument.id) == "SOL_USDT_20260731.GATE_IO"
        assert str(instrument.raw_symbol) == "SOL_USDT_20260731"
        assert instrument.expiration_ns == 1785484800 * 1_000_000_000

    def test_missing_expire_time_is_rejected(self, delivery_payload):
        payload = {k: v for k, v in delivery_payload.items() if k != "expire_time"}

        assert parse_delivery_instrument(payload) is None


# -- symbology and dispatch ----------------------------------------------------


class TestSymbologyAndDispatch:
    def test_perp_suffix_only_on_perpetuals(
        self,
        spot_payload,
        perp_payload,
        inverse_payload,
        delivery_payload,
        option_payload,
    ):
        assert str(parse_spot_instrument(spot_payload).id) == "BTC_USDT.GATE_IO"
        assert (
            str(parse_perpetual_instrument(perp_payload, GateioProductType.PERP).id)
            == "BTC_USDT-PERP.GATE_IO"
        )
        assert (
            str(parse_perpetual_instrument(inverse_payload, GateioProductType.INVERSE).id)
            == "BTC_USD-PERP.GATE_IO"
        )
        assert str(parse_delivery_instrument(delivery_payload).id) == "SOL_USDT_20260731.GATE_IO"
        assert str(parse_option_instrument(option_payload).id) == "ETH_USDT-20260729-2150-P.GATE_IO"

    def test_raw_symbol_is_always_the_venue_string(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)

        assert str(instrument.raw_symbol) == "BTC_USDT"

    @pytest.mark.parametrize(
        ("product", "fixture", "expected"),
        [
            (GateioProductType.SPOT, "spot_payload", CurrencyPair),
            (GateioProductType.PERP, "perp_payload", CryptoPerpetual),
            (GateioProductType.INVERSE, "inverse_payload", CryptoPerpetual),
            (GateioProductType.FUT, "delivery_payload", CryptoFuture),
            (GateioProductType.OPT, "option_payload", CryptoOption),
        ],
    )
    def test_dispatch_by_product(self, request, product, fixture, expected):
        payload = request.getfixturevalue(fixture)

        assert isinstance(parse_instrument(payload, product), expected)

    def test_contract_type_must_agree_with_the_product(self, inverse_payload):
        assert parse_perpetual_instrument(inverse_payload, GateioProductType.PERP) is None

    def test_delivery_product_is_not_a_perpetual(self, delivery_payload):
        with pytest.raises(ValueError, match="not a perpetual"):
            parse_perpetual_instrument(delivery_payload, GateioProductType.FUT)


# -- fees ----------------------------------------------------------------------


class TestSpotFees:
    def test_pair_fee_is_a_percent_and_is_converted(self, spot_payload):
        instrument = parse_spot_instrument(spot_payload)

        assert instrument.maker_fee == Decimal("0.002")  # "0.2" percent
        assert instrument.taker_fee == Decimal("0.002")

    def test_account_fee_tier_wins(self, spot_payload):
        instrument = parse_spot_instrument(
            spot_payload,
            fee_maker=Decimal("0.00098"),
            fee_taker=Decimal("0.00098"),
        )

        assert instrument.maker_fee == Decimal("0.00098")
        assert instrument.taker_fee == Decimal("0.00098")

    def test_contract_fees_are_fractions(self, perp_payload):
        instrument = parse_perpetual_instrument(perp_payload, GateioProductType.PERP)

        assert instrument.maker_fee == Decimal("0.00015")
        assert instrument.taker_fee == Decimal("0.0005")


# -- malformed payloads never abort a batch ------------------------------------


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"id": "BTC_USDT"},
            {"id": "NOTAPAIR", "base": "", "quote": "USDT", "precision": 2},
        ],
    )
    def test_bad_spot_payload_returns_none(self, payload):
        assert parse_spot_instrument(payload) is None

    def test_bad_contract_payload_returns_none(self):
        assert parse_perpetual_instrument({"name": "NOPAIR"}, GateioProductType.PERP) is None

    def test_unknown_product_raises(self, spot_payload):
        unknown = SimpleNamespace(
            is_spot=False,
            is_perpetual=False,
            is_delivery=False,
            is_option=False,
        )

        with pytest.raises(ValueError, match="unknown product"):
            parse_instrument(spot_payload, unknown)  # type: ignore[arg-type]
