# Validation status

This page records what has actually been exercised, and where. It is the only
place that may promote a feature to **Stable** in the
[feature support matrix](../README.md#feature-support-matrix).

## The status vocabulary

| Status | Meaning |
|---|---|
| **Stable** | Covered by unit tests **and** exercised against Gate.io mainnet, with the result recorded on this page |
| **Experimental** | Implemented, but the shape of the API or the behaviour may still change |
| **Partial** | Implemented for some cases only; the table row states which |
| **Implemented — not mainnet-validated** | Complete and unit-tested, but never run against the real venue with real funds |
| **Unsupported** | Not implemented. The venue may or may not offer it |

The rule is deliberately strict: a unit test proves the adapter does what its
author expected, not that the venue agrees. Only a real round trip on mainnet
settles the second question, so nothing reaches **Stable** without one.

## Current state

> **No mainnet validation has been performed for 0.2.0 yet.**
>
> Every execution row in the README matrix is therefore
> **Implemented — not mainnet-validated**, and no row anywhere is **Stable**.
> Treat the adapter as alpha software: start on the testnet, then start small.

<!-- VALIDATION RESULTS PLACEHOLDER
     Fill this section in as validation runs complete. One row per exercised
     path, with the date, the environment, the instrument, and what was
     observed. Only after a row appears here may the corresponding README
     matrix entry be promoted to Stable.
-->

### Mainnet validation results

| Date | Product | Path exercised | Instrument | Result |
|---|---|---|---|---|
| — | — | *nothing recorded yet* | — | — |

### Testnet validation results

| Date | Product | Path exercised | Instrument | Result |
|---|---|---|---|---|
| — | — | *nothing recorded yet* | — | — |

## Known validation limits

Some paths cannot be validated without account states that are outside the
adapter's control. They are listed here so that "not validated" is never
mistaken for "not attempted":

| Path | Why it is hard to validate |
|---|---|
| Cross margin, unified account | Requires the account to be upgraded out of classic mode; only the account owner can do that |
| Unified `multi_currency` mode | Gate.io requires an account balance above 500 USDT |
| Unified `portfolio` mode | Gate.io requires an account balance above 1000 USDT |
| Inverse (BTC-settled) perpetuals | No testnet endpoint; mainnet validation needs a funded BTC-margined wallet |
| Delivery futures, options | No testnet endpoint; the wallets are created by a first internal transfer |
| Hedge (dual) position mode | The adapter refuses it by design and never switches it on, so the refusal is what gets tested, not the mode |
| Liquidation and auto-deleveraging paths | Cannot be provoked safely |

## Reporting a validation result

If you exercise a path against the real venue, a pull request adding a row here
is genuinely useful. Include the date, the product, the instrument, what was
submitted, and what the venue did — including anything that surprised you.
Please do not include account identifiers, order ids, keys or balances.
