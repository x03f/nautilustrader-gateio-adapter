"""Error types for the Gate.io adapter and translation from API responses.

Gate.io API v4 returns errors as JSON bodies of the form ``{"label": ..., "message": ...}``
with an HTTP status code. ``GateioError`` preserves all three fields so callers can
branch on either the HTTP status or the Gate.io error label. Error messages never
include request signatures or credentials.
"""

from __future__ import annotations


class GateioError(Exception):
    """Base error raised for Gate.io API failures."""

    def __init__(self, status: int, label: str, message: str) -> None:
        self.status = status
        self.label = label
        self.message = message
        super().__init__(f"Gate.io {status} {label}: {message}")


class GateioClientError(GateioError):
    """A 4xx error: the request was rejected by Gate.io (bad params, auth, limits)."""


class GateioServerError(GateioError):
    """A 5xx error: Gate.io reported an internal failure; usually retryable."""


class LiveOrdersDisabledError(Exception):
    """Raised when a mutating call is attempted while live orders are disabled.

    The HTTP client requires an explicit ``live_orders=True`` switch before any
    order-mutating endpoint can be called. The presence of API credentials alone
    is never sufficient to place or cancel orders.
    """


class OrderValidationError(Exception):
    """The order violates exchange constraints (min amount, min notional, precision,
    or the pair is not currently tradable)."""


# Gate.io labels that indicate a transient condition worth retrying.
_RETRYABLE_LABELS = frozenset(
    {
        "TOO_MANY_REQUESTS",
        "SERVER_ERROR",
        "INTERNAL",
        "TIMEOUT",
    }
)


def error_from_response(status: int, label: str, message: str) -> GateioError:
    """Build the appropriate typed error for an HTTP status and Gate.io label."""
    if status >= 500:
        return GateioServerError(status, label, message)
    return GateioClientError(status, label, message)


def should_retry(error: Exception) -> bool:
    """Return whether the request that produced ``error`` is safe/sensible to retry."""
    if isinstance(error, GateioServerError):
        return True
    if isinstance(error, GateioError):
        return error.status == 429 or error.label in _RETRYABLE_LABELS
    return False
