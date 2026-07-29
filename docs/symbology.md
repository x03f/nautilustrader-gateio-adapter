# Symbology

The adapter follows a **minimum normalization** rule: a Gate.io symbol is used
verbatim wherever it is already unique, and a suffix is added only where the
exchange genuinely reuses one symbol for two different instruments.

All conversion lives in `nautilus_gateio.common.symbols`. No other module
constructs or takes apart an instrument id.

## Venue

The venue string is **`GATE_IO`** (with an underscore).

NautilusTrader already identifies this exchange as `GATE_IO` in its own tooling,
so using the same string keeps adapter instruments interchangeable with data
loaded through other NautilusTrader components. Version 0.1.0 of this adapter
used `GATEIO`; see the [migration guide](migration-0.1-to-0.2.md).

```python
from nautilus_gateio import GATEIO, GATEIO_CLIENT_ID, GATEIO_VENUE

GATEIO           # "GATE_IO"
GATEIO_VENUE     # Venue("GATE_IO")
GATEIO_CLIENT_ID # ClientId("GATE_IO")
```

The three constants are the same string wearing the three types the platform
asks for it in:

* `GATEIO` is the registration key — the `name` you pass to
  `node.add_data_client_factory(GATEIO, ...)` and
  `node.add_exec_client_factory(GATEIO, ...)`.
* `GATEIO_VENUE` is the `Venue`, for anything venue-scoped: building an
  `InstrumentId` by hand, filtering cache queries.
* `GATEIO_CLIENT_ID` is the `ClientId`, for commands that address a specific
  client. The factories create each client with `ClientId(name)` — the id *is*
  the registration name — so `GATEIO_CLIENT_ID` matches whenever you registered
  under the conventional `GATEIO` key. Register under a different name and the
  client id follows that name instead.

## Instrument id per product

| Product | Instrument id | `raw_symbol` |
|---|---|---|
| Spot | `BTC_USDT.GATE_IO` | `BTC_USDT` |
| Perpetual (linear, USDT-margined) | `BTC_USDT-PERP.GATE_IO` | `BTC_USDT` |
| Perpetual (inverse, BTC-margined) | `BTC_USD-PERP.GATE_IO` | `BTC_USD` |
| Delivery future | `BTC_USDT_20260807.GATE_IO` | `BTC_USDT_20260807` |
| Option | `BTC_USDT-20260729-70000-C.GATE_IO` | `BTC_USDT-20260729-70000-C` |

`raw_symbol` on every instrument is always the exact string Gate.io uses, so a
round trip back to the API never has to reverse a transformation.

## Why perpetuals need a suffix and nothing else does

Measured against the live venue:

| Comparison | Colliding symbols |
|---|---|
| Spot vs USDT perpetual | **527** |
| Spot vs delivery future | 0 |
| Spot vs BTC-settled perpetual | 0 |
| USDT perpetual vs BTC-settled perpetual | 0 |

`BTC_USDT` is both a spot market and a perpetual contract, and 526 other
contracts share that problem, so a perpetual instrument id must carry something
the spot id does not. Delivery contracts (`BTC_USDT_20260807`) and options
(`BTC_USDT-20260729-70000-C`) carry the expiry inside the symbol and collide with
nothing: no spot pair contains a dash, and none has a second underscore.

`-PERP` is the established NautilusTrader convention for exactly this situation
(the Binance adapter maps its perpetuals to `BTCUSDT-PERP`, and the Tardis
integration documents `BTCUSDT-PERP.BINANCE` as the canonical form). The adapter
therefore adds `-PERP` to perpetuals and invents no other suffix — there is no
`-SPOT`, `-FUT`, `-OPT` or `-INVERSE`.

## Product inference

The product is inferred from the shape of the symbol, plus the `-PERP` marker:

```text
<PAIR>-<YYYYMMDD>-<STRIKE>-<C|P>   ->  OPT       (option)
<PAIR>_<YYYYMMDD>                  ->  FUT       (delivery future)
<PAIR>-PERP, quote == USD          ->  INVERSE   (BTC-settled perpetual)
<PAIR>-PERP, any other quote       ->  PERP      (USDT-margined perpetual)
anything else                      ->  SPOT
```

The settlement currency of a perpetual follows from the quote currency: a `USD`
quote is BTC-settled (`settle=btc`), everything else is USDT-settled
(`settle=usdt`). `GateioProductType.settle` returns exactly that path
parameter.

## API

```python
from nautilus_gateio import (
    GateioProductType,
    gateio_to_instrument_id,
    instrument_id_to_gateio,
    parse_delivery_symbol,
    parse_option_symbol,
    product_of,
    raw_symbol_of,
)

gateio_to_instrument_id(GateioProductType.PERP, "BTC_USDT")
# InstrumentId("BTC_USDT-PERP.GATE_IO")

instrument_id_to_gateio("BTC_USDT-PERP.GATE_IO")
# (GateioProductType.PERP, "BTC_USDT")
instrument_id_to_gateio("BTC_USD-PERP.GATE_IO")
# (GateioProductType.INVERSE, "BTC_USD")

product_of("BTC_USDT_20260807.GATE_IO")     # GateioProductType.FUT
raw_symbol_of("BTC_USDT-PERP.GATE_IO")      # "BTC_USDT"

parse_option_symbol("BTC_USDT-20260729-70000-C")
# ("BTC_USDT", "20260729", 70000.0, True)   -- True means a call

parse_delivery_symbol("BTC_USDT_20260807")
# ("BTC_USDT", "20260807")
```

`gateio_to_instrument_id(product, raw_symbol)` builds the canonical id: the
venue symbol upper-cased, `-PERP` appended when the product is a perpetual, and
the `GATE_IO` venue unless you pass another. `instrument_id_to_gateio` is its
inverse and accepts either an `InstrumentId` or its string: it strips the
venue, removes the `-PERP` marker if present, and infers the product from the
symbol's shape — which is why the two directions never disagree. `product_of`
and `raw_symbol_of` are the two halves of that answer individually.

The `parse_*` symbol helpers decompose the two symbol shapes that carry
structure: `parse_option_symbol` returns
`(underlying, expiry, strike, is_call)` and `parse_delivery_symbol` returns
`(pair, expiry)`, and both raise `ValueError` on a symbol that is not in the
stated form. `instrument_id_to_gateio` (and through it `product_of` and
`raw_symbol_of`) raises `ValueError` on an empty symbol; a non-empty symbol
that matches none of the structured shapes is a spot pair by the inference rule
above, not an error.

Two more helpers live in `nautilus_gateio.common.symbols` without a top-level
export, because ordinary code goes through the pair above: `nautilus_symbol`
(the symbol string alone, without the venue) and `product_from_raw_symbol` (the
inference rule, with a `perpetual` flag to disambiguate the one case the
symbol alone cannot).

The similarly named `parse_instrument` / `parse_spot_instrument` /
`parse_perpetual_instrument` / `parse_delivery_instrument` /
`parse_option_instrument` are a different layer: they build full Nautilus
instrument definitions from Gate.io API payloads, applying
`gateio_to_instrument_id` for the id, and belong to instrument loading (see
[products.md](products.md)) rather than symbology.

## Quantity semantics

Symbology decides *what* an instrument is; the product decides what a
`Quantity` means on it:

* **Spot** — a `Quantity` is an amount of the base currency, exactly like
  Gate.io's `amount` field.
* **Perpetual, inverse perpetual, delivery future, option** — a `Quantity` is a
  **number of contracts**, matching the venue's `size` field. The face value is
  carried by the instrument's `multiplier`, so
  `notional = quantity x multiplier x price`. `size_precision` is `0` and
  `size_increment` is `1`: fractional contracts do not exist on this venue.
