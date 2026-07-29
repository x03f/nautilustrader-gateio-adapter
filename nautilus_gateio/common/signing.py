"""Request signing for Gate.io API v4 (HMAC-SHA512) and client order id generation.

The signature specification (Gate.io API v4):

.. code-block:: text

    s = METHOD \\n URL_PATH \\n QUERY_STRING \\n HEX(SHA512(BODY)) \\n TIMESTAMP
    SIGN = HEX(HMAC_SHA512(secret, s))

Authenticated requests send the ``KEY``, ``Timestamp`` and ``SIGN`` headers.
Public endpoints require no signature. These are pure functions and are unit
tested without keys or network access.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import re
import time

from nautilus_gateio.common.constants import (
    CLIENT_ORDER_ID_MAX_BODY,
    CLIENT_ORDER_ID_PREFIX,
    DEFAULT_CLIENT_ORDER_ID_TAG,
)


def sign_request(
    method: str,
    url_path: str,
    query: str = "",
    body: str = "",
    api_key: str = "",
    api_secret: str = "",
    timestamp: str | None = None,
) -> dict[str, str]:
    """Return authentication headers for a Gate.io API v4 request.

    ``timestamp`` may be fixed for deterministic tests; it defaults to the
    current Unix time in seconds.
    """
    ts = timestamp if timestamp is not None else str(int(time.time()))
    hashed_payload = hashlib.sha512((body or "").encode()).hexdigest()
    signature_string = f"{method.upper()}\n{url_path}\n{query}\n{hashed_payload}\n{ts}"
    signature = hmac.new(
        (api_secret or "").encode(), signature_string.encode(), hashlib.sha512
    ).hexdigest()
    return {
        "KEY": api_key,
        "Timestamp": ts,
        "SIGN": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


_TEXT_CHARSET = re.compile(r"[^0-9A-Za-z_.\-]")
_counter = itertools.count()


def generate_client_order_id(tag: str = DEFAULT_CLIENT_ORDER_ID_TAG) -> str:
    """Generate a unique client order id for the Gate.io ``text`` field.

    Gate.io requires the ``text`` field to start with ``t-``, contain only
    ``[0-9A-Za-z_.-]`` and be at most 28 characters. Repeated submission with
    the same ``text`` is rejected by the exchange, which makes the id usable
    as an idempotency key. Uniqueness combines a nanosecond timestamp with a
    process-local counter so ids generated in the same nanosecond still differ.
    """
    ns = time.time_ns() % 10**12
    seq = next(_counter) % 1000
    return sanitize_client_order_id(f"{CLIENT_ORDER_ID_PREFIX}{tag}-{ns}{seq:03d}")


def sanitize_client_order_id(value: str) -> str:
    """Coerce ``value`` into a valid Gate.io ``text`` field.

    Ensures the ``t-`` prefix, strips characters outside the allowed charset,
    and truncates to the 28-character limit. Returns a valid ``text`` value.
    """
    cleaned = _TEXT_CHARSET.sub("", value)
    if not cleaned.startswith(CLIENT_ORDER_ID_PREFIX):
        cleaned = CLIENT_ORDER_ID_PREFIX + cleaned
    return (
        CLIENT_ORDER_ID_PREFIX + cleaned[len(CLIENT_ORDER_ID_PREFIX) :][:CLIENT_ORDER_ID_MAX_BODY]
    )


def sign_ws_request(channel: str, event: str, timestamp: int, api_secret: str) -> str:
    """Sign a private WebSocket subscription.

    Gate.io signs the literal string ``channel=<channel>&event=<event>&time=<ts>``
    with HMAC-SHA512. The resulting hex digest goes into the ``auth.SIGN`` field
    alongside ``auth.KEY`` and ``auth.method = "api_key"``.
    """
    message = f"channel={channel}&event={event}&time={timestamp}"
    return hmac.new(api_secret.encode(), message.encode(), hashlib.sha512).hexdigest()


def ws_auth_payload(
    channel: str, event: str, timestamp: int, api_key: str, api_secret: str
) -> dict:
    """Build the ``auth`` object for a private WebSocket subscription."""
    return {
        "method": "api_key",
        "KEY": api_key,
        "SIGN": sign_ws_request(channel, event, timestamp, api_secret),
    }
