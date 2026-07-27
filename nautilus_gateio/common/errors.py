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


class WalletNotProvisionedError(Exception):
    """The requested Gate.io wallet does not exist yet for this account.

    Gate.io creates the futures, delivery and options wallets on the first
    internal transfer into them, and reports ``USER_NOT_FOUND`` until then. The
    adapter treats this as a recoverable configuration state rather than a
    failure, so that an account trading only spot can still start cleanly.

    This is a statement about the ledger: a wallet the venue has not created
    holds nothing, so a caller may read it as an absence. The venue *declining
    to answer* is a different fact and carries the subclass
    :class:`WalletQueryRefusedError`; a caller for which the difference matters
    must catch that one first.
    """


class WalletQueryRefusedError(WalletNotProvisionedError):
    """Gate.io would not answer the query, and said nothing about the ledger.

    ``FORBIDDEN`` means the API key lacks the permission the endpoint needs;
    ``INVALID_UNIFIED_ACCOUNT`` and ``UNIFIED_ACCOUNT_NOT_ACTIVATED`` mean the
    account is not in the mode the endpoint needs. In all three the venue
    rejected the *question*. Nothing follows about what the wallet holds — it may
    hold an open position of any size.

    The distinction is not cosmetic, because for one class of caller the two
    facts have opposite consequences. Balances may treat both alike: a ledger
    that cannot be read keeps its previous figures either way, which is why this
    derives from :class:`WalletNotProvisionedError` rather than standing beside
    it — every existing handler stays correct. A position query may not, because
    NautilusTrader reads the absence of a position report as flatness
    (``LiveExecutionEngine._create_flat_position_report``) and squares the local
    book against it with a reconciliation order and an inferred fill. Filing a
    refusal as an absence there closes a live position with an execution nobody
    made.
    """


class OrderValidationError(Exception):
    """The order violates exchange constraints (min amount, min notional, precision,
    or the instrument is not currently tradable)."""


class UnsupportedOrderError(Exception):
    """The order cannot be represented on Gate.io without changing its meaning.

    Raised instead of silently substituting a different order type, time in
    force or flag; the execution client turns it into an explicit rejection.
    """


#: Gate.io labels that indicate a transient condition worth retrying.
_RETRYABLE_LABELS = frozenset(
    {
        "TOO_MANY_REQUESTS",
        "SERVER_ERROR",
        "INTERNAL",
        "TIMEOUT",
        "REQUEST_EXPIRED",
    }
)

#: Labels meaning "this wallet has not been created for the account yet".
WALLET_NOT_PROVISIONED_LABELS = frozenset({"USER_NOT_FOUND"})

#: Labels meaning "the account is not in the mode this endpoint needs".
ACCOUNT_MODE_LABELS = frozenset(
    {"INVALID_UNIFIED_ACCOUNT", "UNIFIED_ACCOUNT_NOT_ACTIVATED", "FORBIDDEN"}
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
