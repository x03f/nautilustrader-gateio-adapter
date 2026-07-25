"""Tests for REST/WebSocket request signing and client order id generation.

All vectors are synthetic. No credentials and no network access are involved.

The REST signature specification (Gate.io API v4) is::

    s    = METHOD \\n URL_PATH \\n QUERY_STRING \\n HEX(SHA512(BODY)) \\n TIMESTAMP
    SIGN = HEX(HMAC_SHA512(secret, s))

The WebSocket subscription signature uses a different string::

    s    = "channel=<channel>&event=<event>&time=<unix_seconds>"
    SIGN = HEX(HMAC_SHA512(secret, s))

Each family is checked twice: against a frozen hex vector (catches a regression
in the layout) and against the documented formula rebuilt from its parts inside
the test (catches the layout being wrong in the first place).
"""

from __future__ import annotations

import hashlib
import hmac
import re

import pytest

from nautilus_gateio.common.constants import (
    CLIENT_ORDER_ID_MAX_BODY,
    CLIENT_ORDER_ID_PREFIX,
    DEFAULT_CLIENT_ORDER_ID_TAG,
)
from nautilus_gateio.common.signing import (
    generate_client_order_id,
    sanitize_client_order_id,
    sign_request,
    sign_ws_request,
    ws_auth_payload,
)

API_KEY = "test-key"
API_SECRET = "test-secret"
FIXED_TS = "1700000000"

ORDER_BODY = '{"currency_pair":"BTC_USDT","side":"buy","amount":"0.001","price":"20000"}'

#: SHA-512 of the empty string, the body hash used by every GET request.
EMPTY_BODY_SHA512 = (
    "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
    "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"
)

#: Frozen HMAC-SHA512 vectors for the fixed inputs above.
EXPECTED_GET_SIGN = (
    "d468582a134a0ba79cd1a4be803141dfbd0b67478363e0c2aa109fb6c18c83a2"
    "4370696bd515ca0a6734d109de6841ec205f8b7a73f1cc3cb468ba8ec8663dfc"
)
EXPECTED_POST_SIGN = (
    "c33848a280767d6c438a29d8a13a284f158ec8eb8736f26025448c3ddb7c5cde"
    "3ee167fec4d5bd1059853961609e28c6d21f6c70ade77bdbcc636f7fe771956c"
)
EXPECTED_WS_SPOT_SIGN = (
    "48c67de39ba434f1c9dc1e664cb9e13b32b42f10356e65405b79ee975e5c1fde"
    "cd7277bc4b7603bc67ff8d4f099362ac4313b90e176d749e5c79d721ebaa96e6"
)


def reference_rest_signature(
    method: str,
    url_path: str,
    query: str,
    body: str,
    timestamp: str,
    secret: str,
) -> str:
    """Rebuild the documented REST signature from its five components."""
    body_hash = hashlib.sha512(body.encode()).hexdigest()
    payload = "\n".join([method, url_path, query, body_hash, timestamp])
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()


class TestRestSignatureVectors:
    def test_get_request_matches_the_frozen_vector(self):
        headers = sign_request(
            "GET",
            "/api/v4/spot/accounts",
            query="currency=USDT",
            body="",
            api_key=API_KEY,
            api_secret=API_SECRET,
            timestamp=FIXED_TS,
        )
        assert headers["SIGN"] == EXPECTED_GET_SIGN

    def test_get_request_matches_the_documented_formula(self):
        headers = sign_request(
            "GET",
            "/api/v4/spot/accounts",
            query="currency=USDT",
            body="",
            api_key=API_KEY,
            api_secret=API_SECRET,
            timestamp=FIXED_TS,
        )
        assert headers["SIGN"] == reference_rest_signature(
            "GET",
            "/api/v4/spot/accounts",
            "currency=USDT",
            "",
            FIXED_TS,
            API_SECRET,
        )

    def test_post_request_matches_the_frozen_vector(self):
        headers = sign_request(
            "POST",
            "/api/v4/spot/orders",
            query="",
            body=ORDER_BODY,
            api_key=API_KEY,
            api_secret=API_SECRET,
            timestamp=FIXED_TS,
        )
        assert headers["SIGN"] == EXPECTED_POST_SIGN

    def test_post_request_matches_the_documented_formula(self):
        headers = sign_request(
            "POST",
            "/api/v4/spot/orders",
            query="",
            body=ORDER_BODY,
            api_key=API_KEY,
            api_secret=API_SECRET,
            timestamp=FIXED_TS,
        )
        assert headers["SIGN"] == reference_rest_signature(
            "POST",
            "/api/v4/spot/orders",
            "",
            ORDER_BODY,
            FIXED_TS,
            API_SECRET,
        )

    def test_empty_body_hashes_to_the_sha512_of_the_empty_string(self):
        """An empty body must hash, not be omitted, or every GET fails to verify."""
        signed = sign_request(
            "GET",
            "/api/v4/spot/accounts",
            api_key=API_KEY,
            api_secret=API_SECRET,
            timestamp=FIXED_TS,
        )["SIGN"]
        rebuilt = hmac.new(
            API_SECRET.encode(),
            "\n".join(["GET", "/api/v4/spot/accounts", "", EMPTY_BODY_SHA512, FIXED_TS]).encode(),
            hashlib.sha512,
        ).hexdigest()
        assert signed == rebuilt


class TestRestSignatureBehaviour:
    def _sign(self, **overrides: str) -> str:
        kwargs: dict[str, str] = {
            "method": "GET",
            "url_path": "/api/v4/spot/accounts",
            "query": "currency=USDT",
            "body": "",
            "api_key": API_KEY,
            "api_secret": API_SECRET,
            "timestamp": FIXED_TS,
        }
        kwargs.update(overrides)
        return sign_request(**kwargs)["SIGN"]

    def test_headers_carry_key_timestamp_and_signature(self):
        headers = sign_request(
            "GET",
            "/api/v4/spot/accounts",
            api_key=API_KEY,
            api_secret=API_SECRET,
            timestamp=FIXED_TS,
        )
        assert headers["KEY"] == API_KEY
        assert headers["Timestamp"] == FIXED_TS
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"

    def test_signature_is_lowercase_hex_sha512(self):
        assert re.fullmatch(r"[0-9a-f]{128}", self._sign())

    def test_the_secret_never_leaks_into_the_headers(self):
        headers = sign_request(
            "POST",
            "/api/v4/spot/orders",
            body=ORDER_BODY,
            api_key=API_KEY,
            api_secret=API_SECRET,
            timestamp=FIXED_TS,
        )
        assert API_SECRET not in "".join(headers.values())

    def test_method_is_upper_cased(self):
        assert self._sign(method="get") == self._sign(method="GET")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"method": "POST"},
            {"url_path": "/api/v4/spot/orders"},
            {"query": "currency=BTC"},
            {"body": ORDER_BODY},
            {"timestamp": "1700000001"},
            {"api_secret": "another-secret"},
        ],
        ids=["method", "path", "query", "body", "timestamp", "secret"],
    )
    def test_every_signed_component_changes_the_signature(self, overrides):
        assert self._sign(**overrides) != self._sign()

    def test_api_key_is_not_part_of_the_signature(self):
        """Only the secret keys the HMAC; the key travels in its own header."""
        assert self._sign(api_key="a-different-key") == self._sign()

    def test_default_timestamp_is_current_unix_seconds(self, monkeypatch):
        monkeypatch.setattr("nautilus_gateio.common.signing.time.time", lambda: 1700000000.75)
        headers = sign_request(
            "GET",
            "/api/v4/spot/accounts",
            api_key=API_KEY,
            api_secret=API_SECRET,
        )
        assert headers["Timestamp"] == FIXED_TS

    def test_missing_credentials_still_produce_a_well_formed_header_set(self):
        """Public endpoints reach the same code path with empty credentials."""
        headers = sign_request("GET", "/api/v4/spot/tickers", timestamp=FIXED_TS)
        assert headers["KEY"] == ""
        assert re.fullmatch(r"[0-9a-f]{128}", headers["SIGN"])


class TestWebSocketSignature:
    def test_matches_the_frozen_vector(self):
        assert (
            sign_ws_request("spot.orders", "subscribe", 1700000000, API_SECRET)
            == EXPECTED_WS_SPOT_SIGN
        )

    def test_matches_the_documented_formula(self):
        signature = sign_ws_request("futures.orders", "subscribe", 1700000000, API_SECRET)
        expected = hmac.new(
            API_SECRET.encode(),
            b"channel=futures.orders&event=subscribe&time=1700000000",
            hashlib.sha512,
        ).hexdigest()
        assert signature == expected

    def test_signature_is_lowercase_hex_sha512(self):
        signature = sign_ws_request("spot.usertrades", "subscribe", 1700000000, API_SECRET)
        assert re.fullmatch(r"[0-9a-f]{128}", signature)

    @pytest.mark.parametrize(
        ("channel", "event", "timestamp"),
        [
            ("spot.balances", "subscribe", 1700000000),
            ("spot.orders", "unsubscribe", 1700000000),
            ("spot.orders", "subscribe", 1700000001),
        ],
        ids=["channel", "event", "timestamp"],
    )
    def test_every_signed_component_changes_the_signature(self, channel, event, timestamp):
        baseline = sign_ws_request("spot.orders", "subscribe", 1700000000, API_SECRET)
        assert sign_ws_request(channel, event, timestamp, API_SECRET) != baseline

    def test_auth_payload_shape(self):
        payload = ws_auth_payload("spot.orders", "subscribe", 1700000000, API_KEY, API_SECRET)
        assert payload == {
            "method": "api_key",
            "KEY": API_KEY,
            "SIGN": EXPECTED_WS_SPOT_SIGN,
        }

    def test_auth_payload_never_carries_the_secret(self):
        payload = ws_auth_payload("spot.orders", "subscribe", 1700000000, API_KEY, API_SECRET)
        assert API_SECRET not in "".join(str(value) for value in payload.values())


class TestClientOrderIdConstraints:
    """Gate.io ``text`` rules: ``t-`` prefix, <= 28 bytes after it, ``[0-9A-Za-z_.-]``."""

    CHARSET = re.compile(r"^t-[0-9A-Za-z_.\-]*$")

    def test_constants_match_the_documented_limits(self):
        assert CLIENT_ORDER_ID_PREFIX == "t-"
        assert CLIENT_ORDER_ID_MAX_BODY == 28

    def test_generated_id_has_the_required_prefix(self):
        assert generate_client_order_id().startswith(CLIENT_ORDER_ID_PREFIX)

    def test_generated_id_body_is_within_the_length_limit(self):
        body = generate_client_order_id()[len(CLIENT_ORDER_ID_PREFIX) :]
        assert 0 < len(body) <= CLIENT_ORDER_ID_MAX_BODY

    def test_generated_id_uses_only_the_allowed_charset(self):
        assert self.CHARSET.fullmatch(generate_client_order_id())

    def test_generated_ids_are_unique(self):
        """The ``text`` field doubles as the venue's idempotency key."""
        ids = {generate_client_order_id() for _ in range(2000)}
        assert len(ids) == 2000

    @pytest.mark.parametrize("tag", [DEFAULT_CLIENT_ORDER_ID_TAG, "abc", "x1"])
    def test_generated_id_matches_the_pattern_the_execution_client_recognises(self, tag):
        """The execution client recognises its own ids with ``^t-<tag>-\\d+$``."""
        pattern = re.compile(rf"^{re.escape(CLIENT_ORDER_ID_PREFIX + tag)}-\d+$")
        for _ in range(50):
            assert pattern.fullmatch(generate_client_order_id(tag))

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("t-abc123", "t-abc123"),
            ("abc123", "t-abc123"),
            ("O-1.2_3", "t-O-1.2_3"),
        ],
    )
    def test_sanitize_preserves_valid_values(self, value, expected):
        assert sanitize_client_order_id(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("t-a b c", "t-abc"),
            ("t-a/b:c", "t-abc"),
            ("t-order#1", "t-order1"),
        ],
    )
    def test_sanitize_strips_characters_outside_the_charset(self, value, expected):
        assert sanitize_client_order_id(value) == expected

    def test_sanitize_truncates_an_over_long_body(self):
        sanitized = sanitize_client_order_id("t-" + "A" * 100)
        assert sanitized == "t-" + "A" * CLIENT_ORDER_ID_MAX_BODY
        assert len(sanitized) == len(CLIENT_ORDER_ID_PREFIX) + CLIENT_ORDER_ID_MAX_BODY

    def test_sanitize_does_not_double_the_prefix(self):
        assert sanitize_client_order_id("t-t-abc") == "t-t-abc"

    @pytest.mark.parametrize(
        "value",
        [
            "plain",
            "t-already",
            "with space",
            "u" * 200,
            "!@#$%^&*()",
            "t-" + "z" * 200,
        ],
    )
    def test_sanitize_always_returns_a_venue_valid_value(self, value):
        sanitized = sanitize_client_order_id(value)
        body = sanitized[len(CLIENT_ORDER_ID_PREFIX) :]
        assert sanitized.startswith(CLIENT_ORDER_ID_PREFIX)
        assert len(body) <= CLIENT_ORDER_ID_MAX_BODY
        assert self.CHARSET.fullmatch(sanitized)

    def test_sanitize_is_idempotent(self):
        for value in ("t-abc", "abc", "t-" + "A" * 100, "a b c"):
            once = sanitize_client_order_id(value)
            assert sanitize_client_order_id(once) == once

    def test_generated_ids_survive_sanitization_unchanged(self):
        for _ in range(50):
            generated = generate_client_order_id()
            assert sanitize_client_order_id(generated) == generated
