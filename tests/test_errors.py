"""Tests for error typing, message formatting and retry classification."""

from __future__ import annotations

import pytest

from nautilus_gateio.common.errors import (
    ACCOUNT_MODE_LABELS,
    WALLET_NOT_PROVISIONED_LABELS,
    GateioClientError,
    GateioError,
    GateioServerError,
    OrderValidationError,
    UnsupportedOrderError,
    WalletNotProvisionedError,
    error_from_response,
    should_retry,
)


class TestErrorFromResponse:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 499])
    def test_4xx_maps_to_a_client_error(self, status):
        error = error_from_response(status, "SOME_LABEL", "bad request")
        assert isinstance(error, GateioClientError)
        assert isinstance(error, GateioError)
        assert not isinstance(error, GateioServerError)

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_maps_to_a_server_error(self, status):
        error = error_from_response(status, "SERVER_ERROR", "boom")
        assert isinstance(error, GateioServerError)
        assert isinstance(error, GateioError)
        assert not isinstance(error, GateioClientError)

    def test_all_three_fields_are_preserved(self):
        error = error_from_response(400, "INVALID_PARAM_VALUE", "amount too small")
        assert error.status == 400
        assert error.label == "INVALID_PARAM_VALUE"
        assert error.message == "amount too small"

    def test_message_carries_status_label_and_text(self):
        error = error_from_response(429, "TOO_MANY_REQUESTS", "slow down")
        text = str(error)
        assert "429" in text
        assert "TOO_MANY_REQUESTS" in text
        assert "slow down" in text

    def test_errors_are_raisable_exceptions(self):
        with pytest.raises(GateioError):
            raise error_from_response(400, "LABEL", "message")


class TestShouldRetry:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retryable(self, status):
        assert should_retry(error_from_response(status, "SERVER_ERROR", "boom")) is True

    def test_rate_limiting_is_retryable(self):
        assert should_retry(error_from_response(429, "TOO_MANY_REQUESTS", "slow down")) is True

    @pytest.mark.parametrize(
        "label",
        ["TOO_MANY_REQUESTS", "SERVER_ERROR", "INTERNAL", "TIMEOUT", "REQUEST_EXPIRED"],
    )
    def test_transient_labels_are_retryable_whatever_the_status(self, label):
        assert should_retry(error_from_response(400, label, "transient")) is True

    @pytest.mark.parametrize(
        "label",
        [
            "INVALID_KEY",
            "INVALID_SIGNATURE",
            "INVALID_PARAM_VALUE",
            "BALANCE_NOT_ENOUGH",
            "ORDER_NOT_FOUND",
            "READ_ONLY",
            "IP_FORBIDDEN",
        ],
    )
    def test_permanent_client_errors_are_not_retryable(self, label):
        assert should_retry(error_from_response(400, label, "nope")) is False

    @pytest.mark.parametrize(
        "error",
        [ValueError("not a venue error"), RuntimeError("boom"), Exception("generic")],
    )
    def test_unrelated_exceptions_are_not_retryable(self, error):
        assert should_retry(error) is False

    def test_wallet_not_provisioned_is_not_retryable(self):
        assert should_retry(WalletNotProvisionedError("no futures wallet")) is False


class TestLabelSets:
    def test_wallet_not_provisioned_labels(self):
        assert "USER_NOT_FOUND" in WALLET_NOT_PROVISIONED_LABELS

    @pytest.mark.parametrize(
        "label",
        ["INVALID_UNIFIED_ACCOUNT", "UNIFIED_ACCOUNT_NOT_ACTIVATED", "FORBIDDEN"],
    )
    def test_account_mode_labels(self, label):
        assert label in ACCOUNT_MODE_LABELS

    def test_the_label_sets_do_not_overlap(self):
        assert not WALLET_NOT_PROVISIONED_LABELS & ACCOUNT_MODE_LABELS


class TestAdapterLevelErrors:
    @pytest.mark.parametrize(
        "error_class",
        [WalletNotProvisionedError, OrderValidationError, UnsupportedOrderError],
    )
    def test_adapter_errors_are_plain_exceptions_not_venue_errors(self, error_class):
        """They describe local conditions, so they carry no HTTP status."""
        error = error_class("explanation")
        assert isinstance(error, Exception)
        assert not isinstance(error, GateioError)
        assert "explanation" in str(error)

    def test_unsupported_order_is_distinct_from_order_validation(self):
        """One means "the venue would reject it", the other "we cannot express it"."""
        assert not issubclass(UnsupportedOrderError, OrderValidationError)
        assert not issubclass(OrderValidationError, UnsupportedOrderError)
