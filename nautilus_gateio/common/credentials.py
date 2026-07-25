"""Credential resolution from explicit configuration or environment variables.

Environment variables (following the NautilusTrader adapter convention):

* mainnet: ``GATE_API_KEY`` / ``GATE_API_SECRET``
* testnet: ``GATE_TESTNET_API_KEY`` / ``GATE_TESTNET_API_SECRET``

Values are stripped of surrounding whitespace, because keys pasted with a
trailing newline otherwise produce signatures the venue silently rejects.
Credentials are never logged; :func:`mask` renders a safe fingerprint for
diagnostics.
"""

from __future__ import annotations

import os

ENV_API_KEY = "GATE_API_KEY"
ENV_API_SECRET = "GATE_API_SECRET"
ENV_TESTNET_API_KEY = "GATE_TESTNET_API_KEY"
ENV_TESTNET_API_SECRET = "GATE_TESTNET_API_SECRET"


def resolve_credentials(
    api_key: str | None,
    api_secret: str | None,
    testnet: bool = False,
) -> tuple[str, str]:
    """Return ``(api_key, api_secret)``, falling back to environment variables.

    Empty strings mean "no credentials"; public market data still works without
    them. On testnet the testnet variables take precedence and fall back to the
    mainnet ones.
    """
    if api_key is None:
        if testnet:
            api_key = os.getenv(ENV_TESTNET_API_KEY) or os.getenv(ENV_API_KEY, "")
        else:
            api_key = os.getenv(ENV_API_KEY, "")
    if api_secret is None:
        if testnet:
            api_secret = os.getenv(ENV_TESTNET_API_SECRET) or os.getenv(ENV_API_SECRET, "")
        else:
            api_secret = os.getenv(ENV_API_SECRET, "")
    return (api_key or "").strip(), (api_secret or "").strip()


def mask(secret: str) -> str:
    """Render a credential as a short fingerprint that is safe to log."""
    if not secret:
        return "<unset>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-2:]}"
