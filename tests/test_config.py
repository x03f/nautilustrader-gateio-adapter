"""Tests for configuration defaults, validation helpers and URL resolution.

Covers :mod:`nautilus_gateio.config` and the credential resolution in
:mod:`nautilus_gateio.common.credentials`. The credential environment variables
are cleared for every test by an autouse fixture in ``conftest.py``.
"""

from __future__ import annotations

import pytest

from nautilus_gateio.common.constants import (
    DEFAULT_HTTP_TIMEOUT_SECS,
    GATEIO_HTTP_MAINNET,
    GATEIO_HTTP_TESTNET,
    GATEIO_WS_DELIVERY_USDT,
    GATEIO_WS_OPTIONS,
    GATEIO_WS_PERP_BTC,
    GATEIO_WS_PERP_USDT,
    GATEIO_WS_PERP_USDT_TESTNET,
    GATEIO_WS_SPOT,
    GATEIO_WS_SPOT_TESTNET,
    ORDER_BOOK_SNAPSHOT_LIMITS,
)
from nautilus_gateio.common.credentials import (
    ENV_API_KEY,
    ENV_API_SECRET,
    ENV_TESTNET_API_KEY,
    ENV_TESTNET_API_SECRET,
    mask,
    resolve_credentials,
)
from nautilus_gateio.common.enums import GateioProductType, GateioSpotAccountMode
from nautilus_gateio.config import (
    MAINNET,
    ORDER_BOOK_UPDATE_INTERVALS_MS,
    TESTNET,
    TESTNET_PRODUCTS,
    GateioDataClientConfig,
    GateioExecClientConfig,
    is_testnet,
    resolve_http_url,
    resolve_ws_url,
    validate_book_interval_ms,
    validate_products,
    validate_snapshot_limit,
)

CONFIG_CLASSES = [GateioDataClientConfig, GateioExecClientConfig]


class TestEnvironmentPredicate:
    @pytest.mark.parametrize("value", ["testnet", "TESTNET", " testnet ", "TestNet"])
    def test_testnet_is_recognised_case_and_space_insensitively(self, value):
        assert is_testnet(value) is True

    @pytest.mark.parametrize("value", ["mainnet", "MAINNET", "", "prod", "live"])
    def test_anything_else_is_mainnet(self, value):
        assert is_testnet(value) is False

    def test_environment_constants(self):
        assert MAINNET == "mainnet"
        assert TESTNET == "testnet"


class TestResolveHttpUrl:
    def test_mainnet(self):
        assert resolve_http_url(MAINNET) == GATEIO_HTTP_MAINNET

    def test_testnet(self):
        assert resolve_http_url(TESTNET) == GATEIO_HTTP_TESTNET

    @pytest.mark.parametrize("environment", [MAINNET, TESTNET])
    def test_override_wins(self, environment):
        assert resolve_http_url(environment, "https://proxy.invalid") == "https://proxy.invalid"

    @pytest.mark.parametrize("environment", [MAINNET, TESTNET])
    def test_empty_override_is_ignored(self, environment):
        assert resolve_http_url(environment, "") == resolve_http_url(environment)

    def test_the_two_environments_differ(self):
        assert resolve_http_url(MAINNET) != resolve_http_url(TESTNET)


class TestResolveWsUrl:
    @pytest.mark.parametrize(
        ("product", "expected"),
        [
            (GateioProductType.SPOT, GATEIO_WS_SPOT),
            (GateioProductType.PERP, GATEIO_WS_PERP_USDT),
            (GateioProductType.INVERSE, GATEIO_WS_PERP_BTC),
            (GateioProductType.FUT, GATEIO_WS_DELIVERY_USDT),
            (GateioProductType.OPT, GATEIO_WS_OPTIONS),
        ],
    )
    def test_mainnet_endpoint_per_product(self, product, expected):
        assert resolve_ws_url(product, MAINNET) == expected

    def test_every_product_has_a_distinct_mainnet_endpoint(self):
        urls = {resolve_ws_url(p, MAINNET) for p in GateioProductType}
        assert len(urls) == len(list(GateioProductType))

    @pytest.mark.parametrize(
        ("product", "expected"),
        [
            (GateioProductType.SPOT, GATEIO_WS_SPOT_TESTNET),
            (GateioProductType.PERP, GATEIO_WS_PERP_USDT_TESTNET),
        ],
    )
    def test_testnet_endpoint_for_the_supported_products(self, product, expected):
        assert resolve_ws_url(product, TESTNET) == expected

    @pytest.mark.parametrize(
        "product",
        [GateioProductType.INVERSE, GateioProductType.FUT, GateioProductType.OPT],
    )
    def test_testnet_rejects_products_the_venue_does_not_serve(self, product):
        with pytest.raises(ValueError, match="no testnet WebSocket endpoint"):
            resolve_ws_url(product, TESTNET)

    @pytest.mark.parametrize("product", list(GateioProductType))
    def test_override_wins_for_every_product(self, product):
        assert resolve_ws_url(product, MAINNET, "wss://proxy.invalid") == "wss://proxy.invalid"

    @pytest.mark.parametrize(
        "product",
        [GateioProductType.INVERSE, GateioProductType.FUT, GateioProductType.OPT],
    )
    def test_override_bypasses_the_testnet_restriction(self, product):
        """An explicit URL is the operator's decision and is not second-guessed."""
        assert resolve_ws_url(product, TESTNET, "wss://proxy.invalid") == "wss://proxy.invalid"

    def test_endpoints_are_websocket_urls(self):
        for product in GateioProductType:
            assert resolve_ws_url(product, MAINNET).startswith("wss://")


class TestValidateProducts:
    def test_empty_tuple_is_rejected(self):
        with pytest.raises(ValueError, match="at least one Gate.io product"):
            validate_products((), MAINNET)

    @pytest.mark.parametrize("bad", ["SPOT", "spot", 1, None])
    def test_non_product_members_are_rejected(self, bad):
        with pytest.raises(ValueError, match="GateioProductType members"):
            validate_products((bad,), MAINNET)

    def test_duplicates_are_removed_and_order_preserved(self):
        products = (
            GateioProductType.PERP,
            GateioProductType.SPOT,
            GateioProductType.PERP,
            GateioProductType.SPOT,
        )
        assert validate_products(products, MAINNET) == (
            GateioProductType.PERP,
            GateioProductType.SPOT,
        )

    def test_every_product_is_allowed_on_mainnet(self):
        assert validate_products(tuple(GateioProductType), MAINNET) == tuple(GateioProductType)

    @pytest.mark.parametrize("product", list(TESTNET_PRODUCTS))
    def test_testnet_products_are_allowed_on_testnet(self, product):
        assert validate_products((product,), TESTNET) == (product,)

    @pytest.mark.parametrize(
        "product",
        [GateioProductType.INVERSE, GateioProductType.FUT, GateioProductType.OPT],
    )
    def test_testnet_rejects_the_unsupported_products_by_name(self, product):
        with pytest.raises(ValueError, match=product.value):
            validate_products((product,), TESTNET)

    def test_testnet_rejects_a_mixed_tuple(self):
        with pytest.raises(ValueError, match="no testnet endpoint"):
            validate_products((GateioProductType.SPOT, GateioProductType.OPT), TESTNET)

    def test_testnet_product_set_matches_the_ws_endpoints(self):
        for product in GateioProductType:
            supported = product in TESTNET_PRODUCTS
            try:
                resolve_ws_url(product, TESTNET)
            except ValueError:
                assert not supported
            else:
                assert supported


class TestValidateBookInterval:
    @pytest.mark.parametrize("interval", list(ORDER_BOOK_UPDATE_INTERVALS_MS))
    def test_accepted_intervals(self, interval):
        assert validate_book_interval_ms(interval) == interval

    @pytest.mark.parametrize("interval", [0, 10, 50, 200, 500, 2000, -100])
    def test_rejected_intervals(self, interval):
        with pytest.raises(ValueError, match="order_book_update_interval_ms"):
            validate_book_interval_ms(interval)

    def test_documented_interval_set(self):
        assert ORDER_BOOK_UPDATE_INTERVALS_MS == (20, 100, 1000)


class TestValidateSnapshotLimit:
    @pytest.mark.parametrize("limit", list(ORDER_BOOK_SNAPSHOT_LIMITS))
    def test_accepted_limits(self, limit):
        assert validate_snapshot_limit(limit) == limit

    @pytest.mark.parametrize("limit", [0, 2, 15, 30, 200, 1000, -1])
    def test_rejected_limits(self, limit):
        with pytest.raises(ValueError, match="order_book_snapshot_limit"):
            validate_snapshot_limit(limit)

    def test_documented_limit_set(self):
        assert ORDER_BOOK_SNAPSHOT_LIMITS == (1, 5, 10, 20, 50, 100)


class TestDataClientConfigDefaults:
    def test_defaults(self):
        config = GateioDataClientConfig()
        assert config.api_key is None
        assert config.api_secret is None
        assert config.environment == MAINNET
        assert config.products == (GateioProductType.SPOT,)
        assert config.options_underlyings is None
        assert config.base_url_http is None
        assert config.base_url_ws is None
        assert config.update_instruments_interval_mins == 60
        assert config.http_timeout_secs == DEFAULT_HTTP_TIMEOUT_SECS
        assert config.max_retries == 3
        assert config.order_book_snapshot_limit == 100
        assert config.order_book_update_interval_ms == 100
        assert config.bars_timestamp_on_close is True

    def test_defaults_pass_their_own_validators(self):
        config = GateioDataClientConfig()
        assert validate_products(config.products, config.environment) == config.products
        assert validate_book_interval_ms(config.order_book_update_interval_ms)
        assert validate_snapshot_limit(config.order_book_snapshot_limit)

    def test_is_testnet_property(self):
        assert GateioDataClientConfig().is_testnet is False
        assert GateioDataClientConfig(environment=TESTNET).is_testnet is True

    def test_resolve_http_url_method(self):
        assert GateioDataClientConfig().resolve_http_url() == GATEIO_HTTP_MAINNET
        assert GateioDataClientConfig(environment=TESTNET).resolve_http_url() == GATEIO_HTTP_TESTNET
        assert (
            GateioDataClientConfig(base_url_http="https://proxy.invalid").resolve_http_url()
            == "https://proxy.invalid"
        )

    def test_resolve_ws_url_method(self):
        config = GateioDataClientConfig()
        assert config.resolve_ws_url(GateioProductType.SPOT) == GATEIO_WS_SPOT
        assert config.resolve_ws_url(GateioProductType.OPT) == GATEIO_WS_OPTIONS

    def test_ws_override_applies_to_every_product(self):
        config = GateioDataClientConfig(base_url_ws="wss://proxy.invalid")
        for product in GateioProductType:
            assert config.resolve_ws_url(product) == "wss://proxy.invalid"


class TestExecClientConfigDefaults:
    def test_defaults(self):
        config = GateioExecClientConfig()
        assert config.api_key is None
        assert config.api_secret is None
        assert config.environment == MAINNET
        assert config.products == (GateioProductType.SPOT,)
        assert config.options_underlyings is None
        assert config.base_url_http is None
        assert config.base_url_ws is None
        assert config.spot_account_mode is GateioSpotAccountMode.SPOT
        assert config.client_order_id_tag == "ng"
        assert config.account_polling_interval_secs == 30.0
        assert config.max_retries == 3
        assert config.http_timeout_secs == DEFAULT_HTTP_TIMEOUT_SECS

    def test_execution_defaults_to_mainnet(self):
        """A default that silently pointed at another environment would be worse."""
        assert GateioExecClientConfig().environment == MAINNET
        assert GateioExecClientConfig().is_testnet is False

    def test_resolve_urls(self):
        config = GateioExecClientConfig(environment=TESTNET)
        assert config.resolve_http_url() == GATEIO_HTTP_TESTNET
        assert config.resolve_ws_url(GateioProductType.PERP) == GATEIO_WS_PERP_USDT_TESTNET


class TestConfigImmutability:
    @pytest.mark.parametrize("config_class", CONFIG_CLASSES)
    def test_configs_are_frozen(self, config_class):
        config = config_class()
        with pytest.raises(AttributeError):
            config.environment = TESTNET

    @pytest.mark.parametrize("config_class", CONFIG_CLASSES)
    def test_configs_are_hashable_so_they_can_key_a_cache(self, config_class):
        assert isinstance(hash(config_class()), int)


class TestResolveCredentials:
    def test_explicit_values_win_over_the_environment(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, "env-key")
        monkeypatch.setenv(ENV_API_SECRET, "env-secret")
        assert resolve_credentials("explicit-key", "explicit-secret") == (
            "explicit-key",
            "explicit-secret",
        )

    def test_environment_is_read_when_nothing_is_configured(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, "env-key")
        monkeypatch.setenv(ENV_API_SECRET, "env-secret")
        assert resolve_credentials(None, None) == ("env-key", "env-secret")

    def test_missing_environment_yields_empty_strings(self):
        """Public market data works without credentials."""
        assert resolve_credentials(None, None) == ("", "")

    def test_testnet_variables_take_precedence_on_testnet(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, "main-key")
        monkeypatch.setenv(ENV_API_SECRET, "main-secret")
        monkeypatch.setenv(ENV_TESTNET_API_KEY, "test-key")
        monkeypatch.setenv(ENV_TESTNET_API_SECRET, "test-secret")
        assert resolve_credentials(None, None, testnet=True) == ("test-key", "test-secret")

    def test_testnet_falls_back_to_the_mainnet_variables(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, "main-key")
        monkeypatch.setenv(ENV_API_SECRET, "main-secret")
        assert resolve_credentials(None, None, testnet=True) == ("main-key", "main-secret")

    def test_mainnet_never_reads_the_testnet_variables(self, monkeypatch):
        monkeypatch.setenv(ENV_TESTNET_API_KEY, "test-key")
        monkeypatch.setenv(ENV_TESTNET_API_SECRET, "test-secret")
        assert resolve_credentials(None, None, testnet=False) == ("", "")

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        """A key pasted with a trailing newline otherwise signs unverifiably."""
        monkeypatch.setenv(ENV_API_KEY, "  env-key\n")
        monkeypatch.setenv(ENV_API_SECRET, "\tenv-secret  ")
        assert resolve_credentials(None, None) == ("env-key", "env-secret")

    def test_explicit_values_are_stripped_too(self):
        assert resolve_credentials(" key ", "secret\n") == ("key", "secret")

    def test_explicit_empty_string_is_not_replaced_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(ENV_API_KEY, "env-key")
        monkeypatch.setenv(ENV_API_SECRET, "env-secret")
        assert resolve_credentials("", "") == ("", "")


class TestMaskCredential:
    def test_unset_credential(self):
        assert mask("") == "<unset>"

    def test_short_credential_is_fully_hidden(self):
        assert mask("12345678") == "*" * 8

    def test_long_credential_shows_only_a_fingerprint(self):
        secret = "abcdefghijklmnopqrstuvwxyz"
        masked = mask(secret)
        assert masked == "abcd...yz"
        assert secret not in masked
