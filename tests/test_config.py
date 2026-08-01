"""Tests for configuration defaults, validation helpers and URL resolution.

Covers :mod:`nautilus_gateio.config` and the credential resolution in
:mod:`nautilus_gateio.common.credentials`. The credential environment variables
are cleared for every test by an autouse fixture in ``conftest.py``.
"""

from __future__ import annotations

import httpx
import msgspec
import pytest
from nautilus_trader.common.config import ImportableConfig, NautilusConfig
from nautilus_trader.common.secure import mask_api_key

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
from nautilus_gateio.common.errors import GateioError
from nautilus_gateio.config import (
    MAINNET,
    ORDER_BOOK_UPDATE_INTERVALS_MS,
    TESTNET,
    TESTNET_PRODUCTS,
    GateioDataClientConfig,
    GateioExecClientConfig,
    bounded_fields,
    enforce_field_bounds,
    is_testnet,
    resolve_http_url,
    resolve_ws_url,
    validate_book_interval_ms,
    validate_products,
    validate_snapshot_limit,
)
from nautilus_gateio.http.client import GateioHttpClient

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
    """RN-1: the fingerprint helper is the platform's, not a copy of it."""

    def test_it_is_the_platform_helper_itself(self):
        """No second implementation exists to drift away from the first.

        The package used to carry its own four-plus-two masker. Asserting
        identity rather than equal output is deliberate: equal output today is
        what a copy also has.
        """
        assert mask is mask_api_key

    def test_masking_does_not_disclose_the_length_of_a_short_secret(self):
        """The damage the hand-written version did.

        It rendered one ``*`` per character, so the mask of a credential too
        short to fingerprint still published how long it was — and the length of
        a secret is the first thing an attacker would like to be told. Every
        secret below the fingerprint threshold must mask to the same string.
        """
        assert mask("1234") == mask("1234567") == mask("12345678")

    def test_unset_credential_is_named_as_such(self):
        """Running with no credentials is a supported state, not a blank field."""
        assert mask("") == "<empty>"

    def test_long_credential_shows_only_a_fingerprint(self):
        secret = "abcdefghijklmnopqrstuvwxyz"
        masked = mask(secret)
        assert masked == "abcd...wxyz"
        assert secret not in masked

    def test_the_fingerprint_discloses_at_most_eight_characters(self):
        """The one place this masker is *wider* than the one it replaced.

        The hand-written version showed four leading and two trailing
        characters; the platform's shows four and four. Against a 32-character
        Gate.io key that is eight disclosed characters instead of six, which is
        the whole of the change in what reaches a log -- and it is pinned here
        so it cannot widen further without this failing. ``visible_chars``
        raised to ``6`` would leak twelve and break this test.
        """
        secret = "0123456789abcdef0123456789abcdef"
        masked = mask(secret)

        disclosed = sum(1 for c in masked if c != ".")
        assert disclosed == 8
        assert masked == "0123...cdef"
        # Two of the venue's 32 hex characters were shown at each end before;
        # the middle of the credential is still never rendered.
        assert secret[4:-4] not in masked


DATA_CONFIG_PATH = "nautilus_gateio.config:GateioDataClientConfig"
EXEC_CONFIG_PATH = "nautilus_gateio.config:GateioExecClientConfig"
CONFIG_PATHS = {
    GateioDataClientConfig: DATA_CONFIG_PATH,
    GateioExecClientConfig: EXEC_CONFIG_PATH,
}

#: ``(cls, field, value)`` triples no configuration may survive, by any route.
#: Each value is nonsense the client would otherwise carry into production: a
#: retry budget that permits no attempt, a timeout that expires before the
#: request is written, a period counted backwards. ``0`` is absent for the two
#: timers that document it as "disabled" -- see the test that holds that
#: spelling open.
REFUSED_VALUES = [
    (GateioDataClientConfig, "max_retries", 0),
    (GateioDataClientConfig, "max_retries", -3),
    (GateioDataClientConfig, "http_timeout_secs", 0.0),
    (GateioDataClientConfig, "http_timeout_secs", -20.0),
    (GateioDataClientConfig, "order_book_snapshot_limit", 0),
    (GateioDataClientConfig, "order_book_snapshot_limit", -100),
    (GateioDataClientConfig, "order_book_update_interval_ms", 0),
    (GateioDataClientConfig, "order_book_update_interval_ms", -100),
    (GateioDataClientConfig, "update_instruments_interval_mins", -60),
    (GateioExecClientConfig, "max_retries", 0),
    (GateioExecClientConfig, "max_retries", -3),
    (GateioExecClientConfig, "http_timeout_secs", 0.0),
    (GateioExecClientConfig, "http_timeout_secs", -20.0),
    (GateioExecClientConfig, "account_polling_interval_secs", -30.0),
]

#: ``(cls, field, value)`` triples at the *legal* edge of each bounded field:
#: the smallest value the range still admits. They exist to pin the boundary
#: itself. A check moved one step in either direction fails here or in
#: :data:`REFUSED_VALUES`, and nowhere is a boundary asserted only from one side.
ADMITTED_AT_THE_EDGE = [
    (GateioDataClientConfig, "max_retries", 1),
    (GateioDataClientConfig, "http_timeout_secs", 0.001),
    (GateioDataClientConfig, "order_book_snapshot_limit", 1),
    (GateioDataClientConfig, "order_book_update_interval_ms", 1),
    (GateioDataClientConfig, "update_instruments_interval_mins", 0),
    (GateioExecClientConfig, "max_retries", 1),
    (GateioExecClientConfig, "http_timeout_secs", 0.001),
    (GateioExecClientConfig, "account_polling_interval_secs", 0.0),
]


class TestBoundedFieldDiscovery:
    """RN-3a: the check must have something to check.

    ``enforce_field_bounds`` reads the bounds off the annotations rather than
    restating them, which is what keeps the constructor and the decoder from
    drifting apart -- but it also means an annotation lost in a refactor would
    turn the check into a loop over nothing. These tests fail on an empty or
    shrunken discovery, so the vacuum cannot pass for a pass.
    """

    @pytest.mark.parametrize(
        ("config_cls", "expected"),
        [
            (
                GateioDataClientConfig,
                {
                    "update_instruments_interval_mins",
                    "http_timeout_secs",
                    "max_retries",
                    "order_book_snapshot_limit",
                    "order_book_update_interval_ms",
                },
            ),
            (
                GateioExecClientConfig,
                {"account_polling_interval_secs", "max_retries", "http_timeout_secs"},
            ),
        ],
    )
    def test_every_numeric_field_is_discovered_as_bounded(self, config_cls, expected):
        assert {name for name, _ in bounded_fields(config_cls)} == expected

    def test_a_class_with_nothing_to_check_is_an_error_not_a_pass(self):
        """``all([])`` is ``True``; this refuses to be that.

        A class reaching the checker with no constrained field means an
        annotation was dropped. Reporting "valid" would be the exact failure
        this whole check exists to prevent, so it is a hard error instead.
        """

        class Unbounded(NautilusConfig, frozen=True):
            plain: int = 3

        with pytest.raises(RuntimeError, match="no constrained field"):
            enforce_field_bounds(Unbounded())

    def test_the_refusal_table_exercises_every_bounded_field_of_both_classes(self):
        """A guard against a table that quietly stops covering something.

        Each class must appear, and within a class every field the discovery
        found must have at least one refused value -- otherwise a check that
        works for one class, or for four of five fields, would still show green.
        """
        for config_cls in CONFIG_CLASSES:
            discovered = {name for name, _ in bounded_fields(config_cls)}
            exercised = {field for cls, field, _ in REFUSED_VALUES if cls is config_cls}
            assert exercised == discovered
            edges = {field for cls, field, _ in ADMITTED_AT_THE_EDGE if cls is config_cls}
            assert edges == discovered


class TestOutOfRangeNumbersAreRefusedOnEveryPath:
    """RN-3: a nonsensical number is refused however the configuration is written.

    Two routes reach these classes and only one of them decodes. The direct
    Python constructor is the route the README, ``docs/configuration.md`` and
    every example in ``examples/`` are written in; ``ImportableConfig`` is the
    declarative route used to wire a pip-installed adapter into a node without
    importing it. A constrained ``msgspec`` type covers the second alone,
    because a constraint is applied when a struct is decoded and constructing
    one in Python decodes nothing.

    Left unchecked, each value below lands somewhere other than the field that
    caused it: ``max_retries=0`` was silently clamped up to one attempt, so a
    deployment that asked for no retries got them anyway; ``http_timeout_secs=0``
    produced a client whose every request expired; a negative reload period armed
    a timer counting backwards.
    """

    @pytest.mark.parametrize(("config_cls", "field", "value"), REFUSED_VALUES)
    def test_direct_construction_refuses_and_names_the_field(self, config_cls, field, value):
        """The main documented path: plain Python, no decoding anywhere."""
        with pytest.raises(ValueError) as excinfo:
            config_cls(**{field: value})

        assert field in str(excinfo.value)

    @pytest.mark.parametrize(("config_cls", "field", "value"), REFUSED_VALUES)
    def test_declarative_configuration_refuses_and_names_the_field(
        self,
        config_cls,
        field,
        value,
    ):
        """The decode path, which the constrained types already covered.

        ``msgspec.ValidationError`` is a subclass of ``ValueError``, so this
        asserts the same catchable type as the constructor path: one
        ``except ValueError`` handles a bad configuration whichever way it
        arrived.
        """
        importable = ImportableConfig(path=CONFIG_PATHS[config_cls], config={field: value})

        with pytest.raises(ValueError) as excinfo:
            importable.create()

        assert isinstance(excinfo.value, msgspec.ValidationError)
        assert field in str(excinfo.value)

    @pytest.mark.parametrize(("config_cls", "field", "value"), ADMITTED_AT_THE_EDGE)
    def test_the_smallest_legal_value_is_still_admitted_on_both_paths(
        self,
        config_cls,
        field,
        value,
    ):
        """The other side of every boundary asserted above.

        Without this, a check that refused everything -- or one shifted a step
        too far, refusing ``max_retries=1`` or ``account_polling_interval_secs=0``
        -- would satisfy every refusal test in this class.
        """
        constructed = config_cls(**{field: value})
        decoded = ImportableConfig(
            path=CONFIG_PATHS[config_cls],
            config={field: value},
        ).create()

        assert getattr(constructed, field) == value
        assert getattr(decoded, field) == value

    def test_a_whole_sensible_configuration_still_builds(self):
        """The constraints must not cost the documented path its usable values."""
        config = GateioDataClientConfig(
            max_retries=5,
            http_timeout_secs=10,
            order_book_snapshot_limit=20,
            order_book_update_interval_ms=20,
            update_instruments_interval_mins=None,
        )

        assert config.max_retries == 5
        assert config.http_timeout_secs == 10
        assert config.order_book_snapshot_limit == 20
        assert config.order_book_update_interval_ms == 20
        assert config.update_instruments_interval_mins is None

    @pytest.mark.parametrize(
        ("config_cls", "field"),
        [
            (GateioDataClientConfig, "update_instruments_interval_mins"),
            (GateioExecClientConfig, "account_polling_interval_secs"),
        ],
    )
    def test_zero_still_disables_the_timers_that_document_it(self, config_cls, field):
        """``0`` is a documented spelling here, so the bound admits it.

        Both clients read these two fields as "start no task at all" when they
        are falsy. A positive-only bound would have refused a configuration that
        is valid today, which is why these two are non-negative.
        """
        assert getattr(config_cls(**{field: 0}), field) == 0

    def test_the_venue_specific_sets_are_still_checked_separately(self):
        """A constrained type states a range; Gate.io publishes a set.

        ``37`` ms and ``73`` levels are positive, so they pass the bound and
        must still be refused by the explicit checks -- which is why those two
        validators are kept.
        """
        config = GateioDataClientConfig(
            order_book_update_interval_ms=37,
            order_book_snapshot_limit=73,
        )

        with pytest.raises(ValueError, match="order_book_update_interval_ms"):
            validate_book_interval_ms(config.order_book_update_interval_ms)
        with pytest.raises(ValueError, match="order_book_snapshot_limit"):
            validate_snapshot_limit(config.order_book_snapshot_limit)


class TestPlatformConfigContractIsIntact:
    """RN-3b: refusing early must not break what NautilusTrader promises.

    ``NautilusConfig.validate()`` is declared ``-> bool`` and implemented as
    ``bool(self.parse(self.json()))``. A caller -- a node, a tool, a user's
    pre-flight check -- expects an answer, not an exception. Refusing at
    construction is what keeps that true: an instance that would fail the round
    trip can no longer be built, so every instance that exists validates.
    """

    @pytest.mark.parametrize("config_cls", CONFIG_CLASSES)
    def test_validate_answers_true_instead_of_raising(self, config_cls):
        assert config_cls().validate() is True

    @pytest.mark.parametrize(("config_cls", "field", "value"), ADMITTED_AT_THE_EDGE)
    def test_validate_answers_true_at_every_boundary_value_too(self, config_cls, field, value):
        """The edge values are where a round trip would break if it were going to."""
        assert config_cls(**{field: value}).validate() is True

    @pytest.mark.parametrize(
        ("config_cls", "field", "value"),
        REFUSED_VALUES + ADMITTED_AT_THE_EDGE,
    )
    def test_no_instance_can_exist_that_raises_from_validate(self, config_cls, field, value):
        """The contract, stated as the only thing that must hold.

        For any value at all: either the configuration is refused when it is
        written, or it exists and ``validate()`` answers. There is no third
        outcome where an instance sits in memory and raises when a node asks it
        whether it is valid -- which is what carrying the bound *only* on the
        annotation produced, because a construction that skipped the constraint
        left an instance ``parse(self.json())`` then refused.
        """
        try:
            config = config_cls(**{field: value})
        except ValueError:
            return  # Refused at the door; no instance exists to misbehave.

        assert config.validate() is True

    @pytest.mark.parametrize("config_cls", CONFIG_CLASSES)
    def test_a_configuration_survives_its_own_serialisation(self, config_cls):
        """``parse(json())`` is what ``validate()`` runs; it must round-trip.

        ``__post_init__`` runs again on the way back in, so a check that
        rejected a value the encoder legitimately produces -- an ``int`` where a
        ``float`` was written, say -- would show up as a config that cannot
        survive being saved and reloaded.
        """
        config = config_cls(http_timeout_secs=10, max_retries=1)

        assert config_cls.parse(config.json()) == config


class TestRetryBudgetIsHonouredOrRefused:
    """RN-2: the transport no longer rewrites the operator's retry count.

    ``docs/configuration.md`` promised in both tables that a value below ``1``
    was "clamped to ``1``", and ``GateioHttpClient.__init__`` did exactly that
    with ``max(1, max_retries)``. That is now a refusal, in both places, because
    the attempt loop reads the number as a count of attempts: at ``0`` the
    request is never sent and the caller is handed ``TOO_MANY_REQUESTS`` about a
    request that never left the process.

    These tests count attempts against a mock transport rather than reading
    ``client.max_retries``, so a clamp restored anywhere between the constructor
    and the loop is caught by the number of requests actually made.
    """

    @pytest.mark.parametrize("max_retries", [1, 2, 5])
    async def test_the_configured_count_is_the_number_of_attempts_made(self, max_retries):
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, json={"label": "SERVER_ERROR", "message": "down"})

        client = _transport_client(handler, max_retries=max_retries)

        with pytest.raises(GateioError):
            await client.request("GET", "/spot/tickers")

        assert attempts == max_retries

    @pytest.mark.parametrize("max_retries", [0, -1, -3])
    def test_a_count_below_one_is_refused_rather_than_promoted(self, max_retries):
        """The clamp's replacement.

        Under ``max(1, max_retries)`` this constructor returned a client that
        made one attempt; the operator's ``0`` became a ``1`` and nothing said
        so.
        """
        with pytest.raises(ValueError, match="max_retries"):
            GateioHttpClient(max_retries=max_retries)

    async def test_the_config_default_reaches_the_transport_unchanged(self):
        """Ties the two layers together: the default is legal and is not rewritten."""
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, json={"label": "SERVER_ERROR", "message": "down"})

        client = _transport_client(handler, max_retries=GateioDataClientConfig().max_retries)

        with pytest.raises(GateioError):
            await client.request("GET", "/spot/tickers")

        assert attempts == 3


def _transport_client(handler, max_retries: int) -> GateioHttpClient:
    """Build a client whose transport is a mock and whose backoff never sleeps."""
    client = GateioHttpClient(max_retries=max_retries)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=GATEIO_HTTP_MAINNET,
    )
    client._limiter = _SilentLimiter()  # type: ignore[assignment]

    async def _no_delay(attempt: int) -> None:
        return None

    client._retry_delay = _no_delay  # type: ignore[assignment]
    return client


class _SilentLimiter:
    """Pacing stub: the retry count is under test here, not the rate limiter."""

    async def acquire(self) -> None:
        return None

    def on_rate_limited(self) -> None:
        return None

    def on_success(self) -> None:
        return None
