# Products

Gate.io serves each product family from its own REST namespace, its own
WebSocket host and — importantly — its own **wallet**. The adapter models this
directly: `GateioProductType` is part of an instrument's identity, one data
client multiplexes every configured product, and one execution client
aggregates the wallets of the configured products into a single Nautilus
account.

```python
from nautilus_gateio import GateioProductType

GateioProductType.SPOT      # spot pairs
GateioProductType.PERP      # USDT-margined perpetual futures (linear)
GateioProductType.INVERSE   # BTC-margined perpetual futures (inverse)
GateioProductType.FUT       # USDT-margined delivery (dated) futures
GateioProductType.OPT       # USDT-settled options
```

Spot **margin** is not a product: it is an execution mode on spot instruments,
selected with `spot_account_mode`. See [Margin](#margin-isolated-cross-unified).

## Overview

| Product | `GateioProductType` | Instrument class | REST namespace | WebSocket host | Testnet |
|---|---|---|---|---|---|
| Spot | `SPOT` | `CurrencyPair` | `/spot`, `/margin`, `/unified` | `wss://api.gateio.ws/ws/v4/` | yes |
| Perpetual (linear) | `PERP` | `CryptoPerpetual` | `/futures/usdt` | `wss://fx-ws.gateio.ws/v4/ws/usdt` | yes |
| Perpetual (inverse) | `INVERSE` | `CryptoPerpetual` (`is_inverse=True`) | `/futures/btc` | `wss://fx-ws.gateio.ws/v4/ws/btc` | no |
| Delivery future | `FUT` | `CryptoFuture` | `/delivery/usdt` | `wss://fx-ws.gateio.ws/v4/ws/delivery/usdt` | no |
| Option | `OPT` | `CryptoOption` | `/options` | `wss://op-ws.gateio.live/v4/ws` | no |

Configuring a product Gate.io does not serve on the testnet together with
`environment="testnet"` raises `ValueError` from the client constructor, before
any network activity.

## Spot

* Instrument: `CurrencyPair`, id `BTC_USDT.GATE_IO`.
* `Quantity` is an amount of the **base** currency.
* `price_precision` comes from the pair's `precision`, `size_precision` from
  `amount_precision`, `min_quantity` from `min_base_amount` and `min_notional`
  from `min_quote_amount`.
* Fees come from the account's real fee tier (`GET /wallet/fee`) when
  credentials are configured, and from the pair's deprecated percent `fee` field
  otherwise.
* Pairs the venue reports as `untradable`, or that are currently one-sided
  (sell-only / buy-only before a listing), are not published as instruments.

**The market-order quirk.** Gate.io spot market orders interpret `amount`
differently per side: a market **buy** spends a *quote* amount, a market
**sell** delivers a *base* amount. The execution client handles this without
changing the order's meaning:

| Nautilus order | Sent to Gate.io |
|---|---|
| MARKET SELL | native `type=market`, `amount` = base quantity |
| MARKET BUY with `quote_quantity=True` | native `type=market`, `amount` = quote amount |
| MARKET BUY with a base quantity | aggressive IOC `limit` order, bounded by the pair's published `slippage` limit |

The third row is the only case where the encoding differs from the literal
order type, and it is documented on the order's rejection-free path because an
IOC limit at the venue's own slippage bound is the closest faithful expression
of "buy this many base units at market".

## Margin (isolated, cross, unified)

Margin orders are still spot orders: the same `/spot/orders` endpoint with a
different `account` field. Select the ledger with `spot_account_mode`:

| `GateioSpotAccountMode` | `account` field | Ledger |
|---|---|---|
| `SPOT` | `spot` | plain cash spot |
| `MARGIN` | `margin` | isolated margin, scoped to one currency pair |
| `CROSS_MARGIN` | `cross_margin` | cross margin (reported through the unified account) |
| `UNIFIED` | `unified` | unified account |

Consequences of choosing a margin mode:

* The Nautilus account becomes `AccountType.MARGIN` instead of `CASH`, and
  `AccountFactory.register_cash_borrowing(GATE_IO)` is called so borrowed
  (negative) balances can be held.
* Balances are read from the ledger's own endpoints (`/margin/accounts`,
  `/margin/cross/accounts`, `/unified/accounts`) instead of `/spot/accounts`.
* Price-triggered spot orders name the ledger `normal` where a regular order
  says `spot`; **cross margin has no representation on the price-trigger
  endpoint**, so conditional orders are rejected in that mode rather than
  silently routed to a different ledger.

Borrow and repay endpoints are exposed on `GateioMarginHttpAPI` — they are
required for correct isolated and cross margin support. Every method that can
create a liability says so in its docstring. Borrowing accrues interest from the
moment it is drawn, and Gate.io can liquidate collateral to recover it.

### Unified account modes and their venue-side minimum balances

A unified account has its own sub-mode, and Gate.io gates the richer modes on a
minimum account balance. These are venue facts, verified by attempting each
upgrade:

| Unified mode | Minimum balance | What it unlocks |
|---|---|---|
| `single_currency` | none (accepted with 50 USDT) | unified balances, cross-margin endpoints, futures under unified margin |
| `multi_currency` | **> 500 USDT** | cross-currency margin, unified borrow/repay (`/unified/loans`, `/unified/borrowable`) |
| `portfolio` | **> 1000 USDT** | portfolio margin, `/unified/risk_units` |

Below the threshold the venue answers `operation not support for single currency
mode` (borrow/repay) or `Please upgrade to portfolio margin mode first`
(risk units). Several unified response fields — `borrowed`,
`total_initial_margin`, `total_margin_balance`, `total_maintenance_margin`, the
four margin rates, `unified_account_total_liab`, `spot_order_loss` and `locked`
— are structurally zero outside `multi_currency`. Conversely `im`, `mm`, `imr`
and `mmr` are meaningful **only** in `single_currency` mode.

The adapter never changes an account's mode. `GateioMarginHttpAPI` exposes
`unified_account_mode()` and `set_unified_account_mode()` for callers who want
to do it deliberately.

### Graceful degradation

Gate.io reports "wallet not created yet" (`USER_NOT_FOUND`), "account not in the
required mode" (`INVALID_UNIFIED_ACCOUNT`, `UNIFIED_ACCOUNT_NOT_ACTIVATED`) and
"key lacks permission" (`FORBIDDEN`) as ordinary 4xx errors. The adapter
translates all three into `WalletNotProvisionedError` with an actionable
message, and skips that product during an instrument load or balance sweep
instead of failing to start.

## Perpetual futures (linear, USDT-margined)

* Instrument: `CryptoPerpetual(is_inverse=False)`, settlement currency USDT, id
  `BTC_USDT-PERP.GATE_IO`.
* `Quantity` is a **number of contracts**. `multiplier` is the venue's
  `quanto_multiplier`, so `notional = contracts x multiplier x price`.
* `size_precision = 0`, `size_increment = 1`.
* Order sizes are sent as a signed integer: positive for a buy, negative for a
  sell.
* Mark price, index price and funding rate are available as subscriptions, all
  sourced from the `futures.tickers` stream.
* Contracts that are delisting or inactive are not published as instruments.

## Perpetual futures (inverse, BTC-margined)

Everything above, with `is_inverse=True`, `settle=btc`, and a `USD` quote
currency (`BTC_USD-PERP.GATE_IO`). Gate.io lists exactly one such contract at
the time of writing. There is no testnet endpoint for it.

## Delivery futures

* Instrument: `CryptoFuture`, id `BTC_USDT_20260807.GATE_IO`.
* Symbol shape `PAIR_YYYYMMDD`; the expiry is part of the symbol, so no suffix
  is needed.
* `activation_ns` and `expiration_ns` come from the contract payload.
* Same contract-count quantity semantics as perpetuals.
* Shares the `futures.*` WebSocket channel namespace with perpetuals and is told
  apart only by the endpoint it is subscribed on — which is why the product is
  part of the connection's identity.
* Expired contracts are filtered out of instrument loads.

## Options

* Instrument: `CryptoOption`, id `BTC_USDT-20260729-70000-C.GATE_IO`.
* Symbol shape `PAIR-YYYYMMDD-STRIKE-C|P`; strike and option kind are parsed
  from the symbol, expiry and multiplier from the contract payload.
* USDT-settled; `Quantity` is a number of contracts.
* Gate.io lists thousands of option contracts. Restrict loading with
  `options_underlyings=("BTC_USDT",)` unless you really want all of them.
* The options WebSocket host is `wss://op-ws.gateio.live/v4/ws`. The
  `op-ws.gateio.ws` host published in some Gate.io documentation pages does not
  resolve; the settle-suffixed variants `.../v4/ws/usdt` and `.../v4/ws/btc`
  work and can be selected through `base_url_ws`.
* The per-contract candlestick channel is `options.contract_candlesticks` and
  the ticker channel is `options.contract_tickers`, not the `options.*` names
  the other products use.

## Wallet segregation and transfers

Funds do not move implicitly between product wallets. An account holding USDT in
the spot wallet cannot open a futures position until the balance is transferred,
and the futures, delivery and options wallets are **created by the first
transfer into them** — until then the venue answers `USER_NOT_FOUND`.

```python
await exec_client.transfer(currency="USDT", from_="spot", to="futures", amount="25", settle="usdt")
```

Every internal transfer is routed through the spot wallet by Gate.io, so moving
between two derivative wallets takes two calls. `transfer()` cannot send funds
outside the account: the request carries no address and no recipient. The
execution client logs a warning at startup so the segregation is never a
surprise.

## Not implemented

The adapter has no code for withdrawals, sub-account transfers, Earn, Gate Pay,
P2P, Copy Trading or Gate Bots. They are unrelated to trading. This is a scope
decision, not a safety mechanism: restricting what a key may do is what Gate.io
API key permissions are for.
