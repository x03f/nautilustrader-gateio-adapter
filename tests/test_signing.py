"""Unit tests for request signing and client order id helpers.

All vectors are synthetic; no credentials or network access involved.
"""

from __future__ import annotations

import re
import time

from nautilus_gateio.signing import (
    generate_client_order_id,
    sanitize_client_order_id,
    sign_request,
)

API_KEY = "test-key"
API_SECRET = "test-secret"
FIXED_TS = "1700000000"

# Pre-computed HMAC-SHA512 vector for the fixed inputs below. Guards against
# regressions in the signature-string layout (method/path/query/body-hash/ts).
EXPECTED_GET_SIGN = (
    "dba754d4310283ce9f0614ff46ae304e5c35bd2937409c05bd4ad17ea38a6ff8"
    "68e77dee5bfcc21477ba38a174147a164256fc21f48bffb33814071d55fd1f5a"
)
EXPECTED_POST_SIGN = (
    "a6c0be96901f6c9e11f60d202930a4e07c551b75da7987de285d11aab9efc392"
    "26ecf52b3f29fb118a07f7b4169ecbc58fc20adf4ce8a47a499c847630f6a8d9"
)

CLIENT_ID_RE = re.compile(r"^t-[0-9A-Za-z_.\-]*$")


def _sign(**overrides):
    kwargs = dict(
        method="get",
        url_path="/api/v4/spot/accounts",
        query="currency=BTC",
        body="",
        api_key=API_KEY,
        api_secret=API_SECRET,
        timestamp=FIXED_TS,
    )
    kwargs.update(overrides)
    return sign_request(**kwargs)


class TestSignRequest:
    def test_deterministic_get_vector(self):
        headers = _sign()
        assert headers["SIGN"] == EXPECTED_GET_SIGN
        assert headers["KEY"] == API_KEY
        assert headers["Timestamp"] == FIXED_TS
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"

    def test_deterministic_post_vector_with_body(self):
        headers = _sign(
            method="post",
            url_path="/api/v4/spot/orders",
            query="",
            body='{"currency_pair":"BTC_USDT"}',
        )
        assert headers["SIGN"] == EXPECTED_POST_SIGN

    def test_signature_is_128_hex_chars(self):
        sign = _sign()["SIGN"]
        assert len(sign) == 128
        assert re.fullmatch(r"[0-9a-f]{128}", sign)

    def test_method_is_upper_cased(self):
        assert _sign(method="GET")["SIGN"] == _sign(method="get")["SIGN"]

    def test_body_changes_signature(self):
        assert _sign(body="")["SIGN"] != _sign(body='{"a":1}')["SIGN"]

    def test_query_changes_signature(self):
        assert _sign(query="currency=BTC")["SIGN"] != _sign(query="currency=ETH")["SIGN"]

    def test_path_changes_signature(self):
        assert (
            _sign(url_path="/api/v4/spot/accounts")["SIGN"]
            != _sign(url_path="/api/v4/spot/orders")["SIGN"]
        )

    def test_secret_changes_signature(self):
        assert _sign(api_secret="secret-a")["SIGN"] != _sign(api_secret="secret-b")["SIGN"]

    def test_timestamp_changes_signature(self):
        assert _sign(timestamp="1700000000")["SIGN"] != _sign(timestamp="1700000001")["SIGN"]

    def test_default_timestamp_is_current_unix_seconds(self):
        before = int(time.time())
        headers = sign_request("GET", "/api/v4/spot/time", api_key=API_KEY, api_secret=API_SECRET)
        after = int(time.time())
        ts = int(headers["Timestamp"])
        assert before <= ts <= after


class TestGenerateClientOrderId:
    def test_prefix_length_and_charset(self):
        cid = generate_client_order_id()
        assert cid.startswith("t-")
        assert len(cid) <= 28
        assert CLIENT_ID_RE.fullmatch(cid)

    def test_custom_tag_included(self):
        cid = generate_client_order_id(tag="algo")
        assert cid.startswith("t-algo-")
        assert len(cid) <= 28

    def test_uniqueness_over_rapid_generation(self):
        ids = {generate_client_order_id() for _ in range(1000)}
        assert len(ids) == 1000


class TestSanitizeClientOrderId:
    def test_adds_prefix(self):
        assert sanitize_client_order_id("abc123") == "t-abc123"

    def test_strips_illegal_characters(self):
        result = sanitize_client_order_id("O-2026/07/25#1")
        assert result == "t-O-202607251"
        assert CLIENT_ID_RE.fullmatch(result)

    def test_truncates_to_28_chars(self):
        result = sanitize_client_order_id("x" * 100)
        assert len(result) == 28
        assert result == "t-" + "x" * 26

    def test_idempotent_on_valid_input(self):
        valid = "t-ng-1234567890"
        assert sanitize_client_order_id(valid) == valid

    def test_idempotent_when_applied_twice(self):
        once = sanitize_client_order_id("O-2026/07/25#1")
        assert sanitize_client_order_id(once) == once

    def test_generated_ids_survive_sanitize_unchanged(self):
        cid = generate_client_order_id()
        assert sanitize_client_order_id(cid) == cid
