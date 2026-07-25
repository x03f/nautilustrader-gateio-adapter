# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a vulnerability

Please report security vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/x03f/nautilustrader-gateio-adapter/security/advisories/new)
on this repository. Do **not** open a public issue for security problems.

You can expect an initial response within a few days. Once a fix is available,
a security advisory will be published together with a patched release.

## Scope

- Vulnerabilities in **this adapter** (signing, credential handling, request
  construction, validation, etc.): report here as described above.
- Vulnerabilities in the **Gate.io exchange or API** itself: report to the
  [Gate.io security program](https://www.gate.io/), not here.
- Vulnerabilities in **NautilusTrader**: report upstream to the
  [NautilusTrader project](https://github.com/nautechsystems/nautilus_trader).

## API key safety for users

This adapter handles exchange credentials; please follow these practices:

- **Least privilege**: create an API key with spot trading permission only.
  Do **not** grant withdrawal permission — the adapter never needs it.
- **IP allowlist**: restrict the API key to the IP addresses of the machines
  that run your trading node.
- **Testnet first**: validate your setup against the Gate.io testnet
  (`GATE_TESTNET_API_KEY` / `GATE_TESTNET_API_SECRET`) before using a
  mainnet key.
- **Environment variables, not code**: supply credentials via environment
  variables (`GATE_API_KEY` / `GATE_API_SECRET`); never hardcode them, commit
  them, or paste them into logs, issues, or fixtures.
- **Rotate on suspicion**: if you suspect a key has been exposed, revoke and
  rotate it immediately in your Gate.io account settings.
