# Security policy

## Supported versions

| Version        | Status                                                                                        |
|----------------|-----------------------------------------------------------------------------------------------|
| `0.2.0a2.dev0` | The default branch. A fix lands here first, and is released from here                         |
| `0.2.0a1`      | Current release, tag `v0.2.0a1`. A security fix is published as the next release on this line |
| `0.1.x`        | Superseded. No fixes; preserved unchanged at the `v0.1.0` tag for reference                   |

This is alpha software. Live validation reaches the market-data paths, spot
execution, one USDT perpetual and one option contract, and no further; see
[docs/validation.md](docs/validation.md). The absence of a published advisory is
not evidence of safety: the offline test suite is extensive, and it is evidence
about the code rather than about the exchange.

## Reporting a vulnerability

Report privately through
[GitHub private vulnerability reporting](https://github.com/x03f/gateio-nt-community/security/advisories/new).
Do **not** open a public issue for a security problem.

A useful report states the adapter build — the line `pip freeze | grep gateio`
prints, which names the commit on a git install — the product and the endpoint or
channel involved, what an attacker gains, and — if you have one — a minimal
reproduction.

A report must contain no API key or secret, no account identifier, no balance or
position, no venue or client order id, and no raw authenticated response.
Describe the shape of the request or reply and substitute placeholders for the
values. An advisory becomes public when it is published, and anything pasted
into it is published with it.

This is a spare-time project: expect a first response within a few days rather
than within hours. When a fix is ready, an advisory is published together with a
patched release.

## Scope

* Vulnerabilities in **this adapter** — signing, credential handling, request
  construction, validation — belong here.
* Vulnerabilities in the **Gate.io exchange or its API** belong to
  [Gate.io](https://www.gate.io/), not here.
* Vulnerabilities in **NautilusTrader** belong
  [upstream](https://github.com/nautechsystems/nautilus_trader).

## What the adapter does with your credentials

Each statement below describes the code as it currently stands, and names the
module so you can check it rather than take it on trust.

* **Resolution happens once, in `gateio_nt/common/credentials.py`.** An
  `api_key` / `api_secret` set explicitly on the config wins. When either is
  `None`, the environment is read: `GATE_API_KEY` and `GATE_API_SECRET` on
  mainnet, `GATE_TESTNET_API_KEY` and `GATE_TESTNET_API_SECRET` on the testnet.
  Those four names are the only environment variables the package reads
  anywhere. Surrounding whitespace is stripped, because a key pasted with a
  trailing newline otherwise produces a signature the venue rejects with no
  useful explanation.
* **They are used for signing and nothing else.** `common/signing.py` computes
  an HMAC-SHA512 over the canonical request string; REST calls carry `KEY`,
  `Timestamp` and `SIGN` headers, and private WebSocket subscriptions carry an
  `auth` object of `method`, `KEY` and `SIGN`. The secret itself is never
  transmitted — only the digest. Two tests exist specifically to hold that line:
  `test_the_secret_never_leaks_into_the_headers` and
  `test_auth_payload_never_carries_the_secret` in `tests/test_signing.py`.
* **Nothing is persisted.** The package opens no files, writes no configuration
  and sets no environment variables. Credentials live in the client objects for
  the lifetime of the process and go away with it.
* **No logging call in the package is passed the key or the secret.** The
  adapter logs a great deal, but no call site receives either value.
* **The only hosts contacted are Gate.io's.** The REST and WebSocket endpoints
  are fixed in `common/constants.py`, overridable only by the `base_url_http`
  and `base_url_ws` fields you set yourself. There is no telemetry and no
  reporting endpoint of any kind.

### What can still reach your logs

None of the following is a credential, but all of it identifies you, and it is
better to know where it comes from than to discover it after pasting a log into
an issue.

* Error messages are built from the venue's own reply: the `message` field of a
  JSON error payload is passed through, and a non-JSON error body is included up
  to 200 characters (`http/client.py`). A failure on an authenticated endpoint
  can therefore put account data into an exception message, and from there into
  a log.
* When a subscription cannot be replayed after a reconnect, the WebSocket client
  logs the subscription payload (`websocket/client.py`). On futures, delivery
  and options channels that payload contains the account's numeric user id.
* Execution logs name client order ids, venue order ids and instrument ids as a
  matter of routine.

Scrub logs before sharing them. For values you print yourself, the package
provides a fingerprint helper:

```python
from gateio_nt.common.credentials import mask

mask(api_key)  # 'abcd...wxyz'; '<empty>' when empty; '***' up to 8 characters
```

`mask` is NautilusTrader's own `mask_api_key`, not a copy of it. Since
`0.2.0a2` it discloses the first four and last four characters of a long
credential, where the package's previous hand-written version disclosed four and
two; against a 32-character Gate.io key that is eight characters instead of six.
In exchange, a credential of eight characters or fewer no longer discloses its
own length.

The adapter never calls `mask` internally — it has nothing to mask, because it
does not print credentials at all. The helper exists for your own diagnostics.

## Key hygiene

**Never grant withdrawal permission to a key used here.** No module in this
package implements a withdrawal, a sub-account transfer, or any method that
accepts a blockchain address or another user's identifier.
`GateioWalletHttpAPI.transfer` validates both ends of every transfer against a
fixed set of the account's own trading wallets, so an external destination
cannot be expressed at all. Treat that as a property of the code rather than as
a security boundary: the authoritative control over what a key can do is the
key's own permission set at the venue, and a key used with this adapter needs no
withdrawal permission whatsoever.

**Grant only the sections you actually configure.** The adapter calls
`/spot/*`, `/margin/*` and `/unified/*`, `/futures/{settle}/*`,
`/delivery/{settle}/*`, `/options/*`, and four `/wallet/*` endpoints (fee
schedule, total balance, internal transfer, transfer status). Which of them a
given run touches follows the `products` and the spot account mode you
configure: the margin and unified endpoints are reached only in the
corresponding modes. One call is unconditional — at start-up the execution
client reads `/wallet/fee`, falling back to `/spot/fee`, to obtain the numeric
account id that Gate.io's private derivative channels require. A spot-only key
that cannot read the account leaves the client running, with two warnings; a
client configured with any derivative product refuses to start.

**Use separate keys per environment, and set the testnet variables
explicitly.** On `environment="testnet"` the testnet variables take precedence
but fall back to the mainnet ones when unset, so a testnet run with only
`GATE_API_KEY` in the environment will sign with your mainnet key against the
testnet host. The reverse cannot happen: a mainnet run never reads the testnet
variables. Both behaviors are pinned by tests in `tests/test_config.py`.

**Supply credentials through the environment, never through source.** The
repository's `.gitignore` covers the usual credential file shapes: `.env`,
`credentials` and `secrets` files in the common formats, `*.pem`, `*.key`,
`*.p12`.

**Restrict the key to the addresses that will use it.** An IP allowlist at the
venue turns a leaked key into a much smaller problem. It does nothing if the
host itself is compromised, which is the argument for a dedicated key per
deployment rather than one key everywhere.

**Rehearse before committing funds.** Gate.io publishes testnet endpoints for
spot and USDT perpetuals only; inverse perpetuals, delivery futures and options
have no testnet, so the first real run for those products is on mainnet, and no
testnet run is recorded in [docs/validation.md](docs/validation.md) either.

**Know that `environment` defaults to `"mainnet"` on both clients, and that
there is no local order kill switch.** A misconfigured node holding valid
credentials will send real orders. The default is deliberate — a client that
silently traded somewhere other than where its configuration said would be the
more dangerous design — but it does mean the configuration is the only thing
standing between a rehearsal and the real venue. See
[docs/configuration.md](docs/configuration.md).

**Rotate on suspicion.** If a key may have been exposed, revoke it at Gate.io
first and investigate afterwards.
