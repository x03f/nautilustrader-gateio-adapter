"""Tests for credential resolution and client configuration defaults."""

from __future__ import annotations

import pytest

from nautilus_gateio.config import (
    GateioDataClientConfig,
    GateioExecClientConfig,
    resolve_credentials,
)
from nautilus_gateio.constants import GATEIO_HTTP_MAINNET, GATEIO_HTTP_TESTNET

ENV_VARS = (
    "GATE_API_KEY",
    "GATE_API_SECRET",
    "GATE_TESTNET_API_KEY",
    "GATE_TESTNET_API_SECRET",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate every test from ambient credential environment variables."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestResolveCredentials:
    def test_explicit_args_win_over_env(self, monkeypatch):
        monkeypatch.setenv("GATE_API_KEY", "env-key")
        monkeypatch.setenv("GATE_API_SECRET", "env-secret")
        assert resolve_credentials("arg-key", "arg-secret", testnet=False) == (
            "arg-key",
            "arg-secret",
        )

    def test_env_fallback_mainnet(self, monkeypatch):
        monkeypatch.setenv("GATE_API_KEY", "env-key")
        monkeypatch.setenv("GATE_API_SECRET", "env-secret")
        assert resolve_credentials(None, None, testnet=False) == ("env-key", "env-secret")

    def test_testnet_prefers_testnet_vars(self, monkeypatch):
        monkeypatch.setenv("GATE_API_KEY", "main-key")
        monkeypatch.setenv("GATE_API_SECRET", "main-secret")
        monkeypatch.setenv("GATE_TESTNET_API_KEY", "test-key")
        monkeypatch.setenv("GATE_TESTNET_API_SECRET", "test-secret")
        assert resolve_credentials(None, None, testnet=True) == ("test-key", "test-secret")

    def test_testnet_falls_back_to_mainnet_vars(self, monkeypatch):
        monkeypatch.setenv("GATE_API_KEY", "main-key")
        monkeypatch.setenv("GATE_API_SECRET", "main-secret")
        assert resolve_credentials(None, None, testnet=True) == ("main-key", "main-secret")

    def test_whitespace_and_newlines_stripped(self):
        assert resolve_credentials("  key\n", "\tsecret \n", testnet=False) == (
            "key",
            "secret",
        )

    def test_stripped_from_env_too(self, monkeypatch):
        monkeypatch.setenv("GATE_API_KEY", "env-key\n")
        monkeypatch.setenv("GATE_API_SECRET", " env-secret ")
        assert resolve_credentials(None, None, testnet=False) == ("env-key", "env-secret")

    def test_empty_when_nothing_set(self):
        assert resolve_credentials(None, None, testnet=False) == ("", "")
        assert resolve_credentials(None, None, testnet=True) == ("", "")


class TestGateioExecClientConfig:
    def test_defaults_target_testnet(self):
        config = GateioExecClientConfig()
        assert config.environment == "testnet"
        assert config.is_testnet is True
        assert config.resolve_base_url() == "https://api-testnet.gateapi.io"
        assert config.resolve_base_url() == GATEIO_HTTP_TESTNET

    def test_mainnet_opt_in(self):
        config = GateioExecClientConfig(environment="mainnet")
        assert config.is_testnet is False
        assert config.resolve_base_url() == "https://api.gateio.ws"
        assert config.resolve_base_url() == GATEIO_HTTP_MAINNET

    def test_environment_case_insensitive(self):
        assert GateioExecClientConfig(environment="MAINNET").is_testnet is False
        assert GateioExecClientConfig(environment="Testnet").is_testnet is True

    def test_base_url_override_wins(self):
        override = "https://example.invalid"
        config = GateioExecClientConfig(environment="mainnet", base_url_http=override)
        assert config.resolve_base_url() == override
        config = GateioExecClientConfig(base_url_http=override)
        assert config.resolve_base_url() == override

    def test_misc_defaults(self):
        config = GateioExecClientConfig()
        assert config.api_key is None
        assert config.api_secret is None
        assert config.venue == "GATEIO"
        assert config.account_poll_interval_secs == 5.0
        assert config.client_order_id_tag == "ng"


class TestGateioDataClientConfig:
    def test_defaults(self):
        config = GateioDataClientConfig()
        assert config.use_websocket is True
        assert config.emit_synthetic_quotes is True
        assert config.poll_interval_secs == 5.0
        assert config.venue == "GATEIO"
        assert config.base_url_http == GATEIO_HTTP_MAINNET
