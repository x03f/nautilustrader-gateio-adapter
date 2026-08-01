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
#: This *is* ``nautilus_trader.common.secure.mask_api_key``, exported under the
#: name this package documents. Two maskers exist in NautilusTrader 1.230 and
#: they disagree at the edges; this is the pure-Python one, which the OKX and
#: Deribit adapters log through (four call sites between them). The other lives
#: in ``core.nautilus_pyo3`` and is what Binance uses; it renders an absent
#: credential as the empty string and pads a short one to its own length, so it
#: would lose both properties this adapter wants — a *named* absent case, since
#: running with no credentials at all is a supported state for public market
#: data, and a fixed ``***`` that does not publish how long a short secret is.
#:
#: One thing did get wider, and it is stated here rather than left to be found.
#: For a credential longer than eight characters the hand-written version this
#: replaces showed four leading and **two** trailing characters
#: (``abcd...yz``); this one shows four and **four** (``abcd...wxyz``). A
#: Gate.io key is 32 hex characters and a secret 64, so the fingerprint now
#: discloses eight of them instead of six. That is the whole of the change in
#: what reaches a log, and ``tests/test_config.py`` pins it so it cannot widen
#: again without a test failing.
mask = mask_api_key
