"""Credential resolution from explicit configuration or environment variables.

Environment variables (following the NautilusTrader adapter convention):

* mainnet: ``GATE_API_KEY`` / ``GATE_API_SECRET``
* testnet: ``GATE_TESTNET_API_KEY`` / ``GATE_TESTNET_API_SECRET``

Values are stripped of surrounding whitespace, because keys pasted with a
trailing newline otherwise produce signatures the venue silently rejects.
Credentials are never logged; :data:`mask` renders a safe fingerprint for
diagnostics.
"""

from __future__ import annotations

import os

from nautilus_trader.common.secure import mask_api_key

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


#: Render a credential as a short fingerprint that is safe to log.
#:
#: This *is* NautilusTrader's own helper, exported under the name this package
#: documents. It is not wrapped, because no Gate.io requirement survives that it
#: fails to express: it renders ``<empty>`` for an absent credential — which
#: this adapter needs, since running with no credentials at all is a supported
#: state for public market data — and a fixed ``***`` for a secret short enough
#: that a fingerprint would give it away. Four in-tree adapters log through it.
#:
#: The hand-written version this replaces differed only in ways that were worse:
#: it padded a short secret to its own length, disclosing how long the credential
#: was, and it named the absent case ``<unset>``, a spelling nothing else in the
#: platform uses.
mask = mask_api_key
