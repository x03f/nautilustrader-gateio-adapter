"""Unit tests for error typing, message format, and retry classification."""

from __future__ import annotations

import pytest

from nautilus_gateio.errors import (
    GateioClientError,
    GateioError,
    GateioServerError,
    error_from_response,
    should_retry,
)


class TestErrorFromResponse:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
    def test_4xx_maps_to_client_error(self, status):
        err = error_from_response(status, "SOME_LABEL", "bad request")
        assert isinstance(err, GateioClientError)
        assert isinstance(err, GateioError)
        assert not isinstance(err, GateioServerError)

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_maps_to_server_error(self, status):
        err = error_from_response(status, "SERVER_ERROR", "boom")
        assert isinstance(err, GateioServerError)
        assert isinstance(err, GateioError)
        assert not isinstance(err, GateioClientError)

    def test_fields_preserved(self):
        err = error_from_response(400, "INVALID_PARAM", "amount too small")
        assert err.status == 400
        assert err.label == "INVALID_PARAM"
        assert err.message == "amount too small"

    def test_message_format(self):
        err = error_from_response(400, "INVALID_PARAM", "amount too small")
        assert str(err) == "Gate.io 400 INVALID_PARAM: amount too small"

    def test_server_error_message_format(self):
        err = error_from_response(503, "SERVER_ERROR", "unavailable")
        assert str(err) == "Gate.io 503 SERVER_ERROR: unavailable"


class TestShouldRetry:
    def test_server_error_is_retryable(self):
        assert should_retry(error_from_response(500, "INTERNAL", "boom")) is True

    def test_429_is_retryable(self):
        assert should_retry(error_from_response(429, "RATE_LIMITED", "slow down")) is True

    def test_too_many_requests_label_is_retryable(self):
        err = error_from_response(403, "TOO_MANY_REQUESTS", "rate limit exceeded")
        assert should_retry(err) is True

    def test_400_invalid_param_not_retryable(self):
        assert should_retry(error_from_response(400, "INVALID_PARAM", "bad")) is False

    def test_auth_error_not_retryable(self):
        assert should_retry(error_from_response(401, "INVALID_KEY", "bad key")) is False

    @pytest.mark.parametrize(
        "error",
        [ValueError("nope"), RuntimeError("boom"), KeyError("k"), Exception("generic")],
    )
    def test_random_exceptions_not_retryable(self, error):
        assert should_retry(error) is False
