"""What a base-denominated spot MARKET BUY actually pays.

Gate.io's native spot market buy spends a **quote** amount, so it cannot say
"buy exactly this many base units". ``_build_aggressive_spot_buy`` answers that
instruction by sending a *limit* order priced through the book by the pair's own
published slippage cap. The quantity the caller asked for survives that
translation; the price is invented by this client, from data this client chose.

That makes the price the money-state field on this path, and every test here is
about the number that reaches the venue in the request body:

* too low and nothing fills — an IOC limit under the market is cancelled on
  arrival, so a strategy that believes it is long is flat;
* too high and the cap that bounds the fill is not the cap the pair publishes,
  which is the whole reason the rewrite is defensible in the first place;
* absent and the order must be refused rather than priced from a guess, because
  a guessed price is spent money.

The reference is read through a chain — the cached quote's **ask**, then the
last trade, then the venue's ticker — and each link is asserted by the number
that leaves for the venue when only that link can answer. The bodies are read
off the recorded ``POST /spot/orders`` call, not off the order object: the order
object never carries this price at all, so an assertion there would prove
nothing about what Gate.io was told.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity

from gateio_nt.common.errors import GateioServerError
from gateio_nt.instruments import parse_spot_instrument

try:  # pytest inserts the tests directory on the path; support both layouts
    from tests.test_execution_orders import (
        SPOT_BTC_USDT,
        SPOT_PAIR_PAYLOAD,
        ExecHarness,
        _assert_denied,
        _submit,
        build_instruments,
    )
except ImportError:  # pragma: no cover - depends on the pytest import mode
    from test_execution_orders import (  # type: ignore[no-redef]
        SPOT_BTC_USDT,
        SPOT_PAIR_PAYLOAD,
        ExecHarness,
        _assert_denied,
        _submit,
        build_instruments,
    )

#: The book, the tape and the venue ticker deliberately disagree in every test
#: that has more than one of them, so the number in the request body names the
#: link that answered instead of being consistent with all three.
BEST_BID = "59000.00"
BEST_ASK = "60000.00"
LAST_TRADE = "50000.00"
TICKER_LAST = "40000"

#: `SPOT_PAIR_PAYLOAD` publishes `slippage: "0.05"`, so the pair's own cap and
#: the fallback constant agree on BTC_USDT. Any test that has to tell the two
#: apart uses a pair built with a different cap.
ASK_PLUS_PAIR_CAP = "63000.00"  # 60000.00 * 1.05

_ABSENT = object()


# -- fixtures -----------------------------------------------------------------


def _pair(pair_id: str, slippage: Any = _ABSENT) -> Instrument:
    """Build one spot pair, optionally restating (or dropping) its slippage cap."""
    payload = {**SPOT_PAIR_PAYLOAD, "id": pair_id, "base": pair_id.split("_")[0]}
    if slippage is _ABSENT:
        payload.pop("slippage", None)
    else:
        payload["slippage"] = slippage
    instrument = parse_spot_instrument(payload)
    assert instrument is not None, f"the fixture pair {pair_id} did not parse"
    return instrument


@pytest.fixture()
def make_env():
    """Build execution harnesses that answer a submission, and close them after."""
    created: list[ExecHarness] = []

    def _make(extra_instruments: tuple[Instrument, ...] = ()) -> ExecHarness:
        env = ExecHarness(instruments=[*build_instruments(), *extra_instruments])
        # An empty ack is what the venue answers a submission with here; the
        # tests are about the request, so the reply carries nothing to parse.
        env.spot.responses["create_order"] = {}
        created.append(env)
        return env

    yield _make
    for env in created:
        env.close()


def _quote(env: ExecHarness, instrument_id: InstrumentId, bid: str, ask: str) -> None:
    """Put one top-of-book quote in the platform cache."""
    env.cache.add_quote_ticks(
        [
            QuoteTick(
                instrument_id=instrument_id,
                bid_price=Price.from_str(bid),
                ask_price=Price.from_str(ask),
                bid_size=Quantity.from_str("1.000000"),
                ask_size=Quantity.from_str("1.000000"),
                ts_event=1,
                ts_init=1,
            ),
        ],
    )


def _trade(env: ExecHarness, instrument_id: InstrumentId, price: str) -> None:
    """Put one trade on the tape in the platform cache."""
    env.cache.add_trade_ticks(
        [
            TradeTick(
                instrument_id=instrument_id,
                price=Price.from_str(price),
                size=Quantity.from_str("1.000000"),
                aggressor_side=AggressorSide.BUYER,
                trade_id=TradeId("T-1"),
                ts_event=1,
                ts_init=1,
            ),
        ],
    )


def _market_buy(
    env: ExecHarness,
    instrument_id: InstrumentId = SPOT_BTC_USDT,
    quantity: str = "0.010000",
    time_in_force: TimeInForce = TimeInForce.GTC,
    quote_quantity: bool = False,
) -> Any:
    """Submit a spot market buy denominated in base units unless told otherwise."""
    order = env.order_factory.market(
        instrument_id,
        OrderSide.BUY,
        Quantity.from_str(quantity),
        time_in_force=time_in_force,
        quote_quantity=quote_quantity,
    )
    _submit(env, order)
    return order


def _sent_body(env: ExecHarness) -> dict[str, Any]:
    """Return the one ``POST /spot/orders`` body this client actually sent."""
    calls = env.spot.calls_named("create_order")
    assert len(calls) == 1, f"expected exactly one submission, got {calls}"
    return calls[0].body


# -- the price the venue is told ---------------------------------------------


class TestThePriceIsTheAskLiftedByThePairsCap:
    """The reference is the side being crossed, and the cap is the pair's own."""

    def test_the_whole_body_is_a_limit_at_the_ask_plus_the_pairs_cap(self, make_env):
        """The book answers, and the number is the ask lifted by the cap.

        The tape and the venue ticker are both loaded with different prices, so
        this asserts *which* input priced the order and not merely that some
        price was written: the bid would send 61950.00, the mid 62475.00, the
        last trade 52500.00 and the venue's ticker 42000.00 — every one of them
        a price a taker buy would not have paid.

        The body is asserted whole because the price is not the only field that
        changes meaning here: `type` has to become `limit` (a `market` body with
        `amount: 0.010000` would spend 0.01 **USDT**, four orders of magnitude
        of instruction lost in one word) and `amount` has to stay the base
        quantity the caller asked for.
        """
        env = make_env()
        _quote(env, SPOT_BTC_USDT, BEST_BID, BEST_ASK)
        _trade(env, SPOT_BTC_USDT, LAST_TRADE)
        env.spot.responses["tickers"] = [{"last": TICKER_LAST}]

        order = _market_buy(env)

        assert _sent_body(env) == {
            "currency_pair": "BTC_USDT",
            "side": "buy",
            "account": "spot",
            "text": f"t-{order.client_order_id.value}",
            "type": "limit",
            "amount": "0.010000",
            "price": ASK_PLUS_PAIR_CAP,
            "time_in_force": "ioc",
        }
        assert not env.spot.called("tickers"), (
            "the book answered, so the venue must not be asked for a price as well"
        )

    def test_the_cap_is_the_one_the_pair_publishes(self, make_env):
        """A pair with its own cap is priced by that cap, not by the constant.

        Each case is a different order: at 0.005 the 5% fallback would pay
        2700.00 more than the pair allows, and at 0.10 it would refuse 3000.00
        of headroom the pair grants — the second is the one that shows up as an
        unexplained non-fill.
        """
        pairs = {
            "0.005": ("AAA_USDT", "60300.00"),
            "0.02": ("BBB_USDT", "61200.00"),
            "0.1": ("CCC_USDT", "66000.00"),
        }
        instruments = tuple(_pair(pair_id, cap) for cap, (pair_id, _) in pairs.items())
        env = make_env(instruments)

        for sent, (cap, (pair_id, expected)) in enumerate(pairs.items(), start=1):
            instrument_id = InstrumentId.from_str(f"{pair_id}.GATE_IO")
            _quote(env, instrument_id, BEST_BID, BEST_ASK)
            _market_buy(env, instrument_id)
            calls = env.spot.calls_named("create_order")
            assert len(calls) == sent, f"{pair_id} was not submitted"
            body = calls[-1].body
            assert body["currency_pair"] == pair_id
            assert body["price"] == expected, f"cap {cap} on {pair_id}"

    @pytest.mark.parametrize(
        "slippage",
        [
            pytest.param(_ABSENT, id="field-missing"),
            pytest.param(None, id="null"),
            pytest.param("", id="empty"),
            pytest.param("0", id="zero"),
            pytest.param("-0.01", id="negative"),
            pytest.param("wide", id="unreadable"),
        ],
    )
    def test_a_pair_with_no_usable_cap_falls_back_to_five_percent(self, make_env, slippage):
        """A cap that cannot bound anything is replaced, not applied.

        `0`, a negative cap and an unreadable one all arrive as a `Decimal` this
        client would otherwise multiply by: `0` prices the buy *at* the ask
        (60000.00, an order that only fills if nothing moved between the quote
        and the request) and `-0.01` prices it 600.00 **below** the ask, which
        is an aggressive order that cannot be aggressive.
        """
        instrument = _pair("DDD_USDT", slippage)
        env = make_env((instrument,))
        instrument_id = InstrumentId.from_str("DDD_USDT.GATE_IO")
        _quote(env, instrument_id, BEST_BID, BEST_ASK)

        _market_buy(env, instrument_id)

        assert _sent_body(env)["price"] == ASK_PLUS_PAIR_CAP

    def test_a_fill_or_kill_buy_is_capped_and_still_all_or_nothing(self, make_env):
        """The substitution is in the price and stops there.

        A spot limit takes `fok` as readily as `ioc`, so downgrading it would
        turn "fill in full or not at all" into "fill whatever is available" —
        a partial position at a price the strategy never chose to hold.
        """
        env = make_env()
        _quote(env, SPOT_BTC_USDT, BEST_BID, BEST_ASK)

        _market_buy(env, time_in_force=TimeInForce.FOK)

        body = _sent_body(env)
        assert body["price"] == ASK_PLUS_PAIR_CAP
        assert body["time_in_force"] == "fok"


# -- the chain that finds a reference price ----------------------------------


class TestTheFallbackChain:
    """Each link prices the order only when the ones before it cannot."""

    def test_the_tape_prices_the_order_when_the_book_is_silent(self, make_env):
        """No quote cached: the last trade answers, and the venue is not asked.

        The venue ticker is loaded with a different price, so a client that
        skipped the tape would send 42000.00 — 8500.00 below the last print,
        and an IOC buy at that price is a cancel, not a trade.
        """
        env = make_env()
        _trade(env, SPOT_BTC_USDT, LAST_TRADE)
        env.spot.responses["tickers"] = [{"last": TICKER_LAST}]

        _market_buy(env)

        assert _sent_body(env)["price"] == "52500.00"  # 50000.00 * 1.05
        assert not env.spot.called("tickers"), "the cache answered; no venue read was needed"

    def test_the_venue_ticker_is_the_last_resort(self, make_env):
        """Nothing cached: the pair's own ticker is read, and it prices the order."""
        env = make_env()
        env.spot.responses["tickers"] = [{"last": TICKER_LAST}]

        _market_buy(env)

        assert _sent_body(env)["price"] == "42000.00"  # 40000 * 1.05
        ticker_calls = env.spot.calls_named("tickers")
        assert len(ticker_calls) == 1
        assert ticker_calls[0].args[0] == "BTC_USDT", (
            "a ticker read for another pair would price this order off another market"
        )

    @pytest.mark.parametrize(
        "tickers",
        [
            pytest.param([], id="no-ticker-row"),
            pytest.param([{}], id="row-without-last"),
            pytest.param([{"last": ""}], id="empty-last"),
            pytest.param([{"last": "0"}], id="zero-last"),
            pytest.param([{"last": "-1"}], id="negative-last"),
            pytest.param([{"last": "n/a"}], id="unreadable-last"),
            pytest.param(GateioServerError(500, "SERVER_ERROR", "ticker unavailable"), id="error"),
        ],
    )
    def test_an_unpriceable_buy_is_refused_rather_than_priced_from_nothing(
        self,
        make_env,
        tickers,
    ):
        """With no reference anywhere, the order is denied and nothing is sent.

        Every case here is a way the last link can fail to state a price, and
        each one has a wrong answer that is easy to reach: `to_decimal` answers
        `0` for a missing, empty or unreadable `last`, and a `0` carried through
        this method is a limit buy priced at zero — an order the venue refuses
        at best, and on the venue's *futures* wire the encoding for "market",
        i.e. the unbounded fill this rewrite exists to avoid. The refusal is
        also the only outcome that keeps the caller's own remedy visible, which
        is why the message has to name it.
        """
        env = make_env()
        env.spot.responses["tickers"] = tickers

        order = _market_buy(env)

        _assert_denied(env, order, "cannot price a base-denominated spot market buy on BTC_USDT")
        assert "quote_quantity=True" in env.events[-1].reason
        assert not env.spot.called("create_order"), "no price was found, so nothing may be sent"


# -- the rewrite applies to exactly one instruction ---------------------------


class TestOnlyABaseDenominatedBuyIsRewritten:
    """A cached quote must not turn the other two spot market orders into limits."""

    def test_a_quote_denominated_buy_stays_a_native_market_order(self, make_env):
        """Gate.io can express this one directly; pricing it would change it.

        `amount` here is 500 USDT of cash. Rewriting it as a limit would send a
        limit order for 500 **BTC**, which is the same field read in the other
        currency: a request roughly seven orders of magnitude larger than the
        one the strategy issued.
        """
        env = make_env()
        _quote(env, SPOT_BTC_USDT, BEST_BID, BEST_ASK)

        _market_buy(env, quantity="500.000000", quote_quantity=True)

        body = _sent_body(env)
        assert body["type"] == "market"
        assert body["amount"] == "500.000000"
        assert "price" not in body, "Gate.io's native market buy carries no price"

    def test_a_market_sell_stays_a_native_market_order(self, make_env):
        """A sell is already base-denominated on this venue, so nothing is rewritten."""
        env = make_env()
        _quote(env, SPOT_BTC_USDT, BEST_BID, BEST_ASK)
        order = env.order_factory.market(
            SPOT_BTC_USDT,
            OrderSide.SELL,
            Quantity.from_str("0.010000"),
        )
        _submit(env, order)

        body = _sent_body(env)
        assert body["type"] == "market"
        assert body["amount"] == "0.010000"
        assert "price" not in body


# -- a zero in the cache is not a price ---------------------------------------


class TestAZeroPriceInTheCacheIsNotAPrice:
    """A cached zero must leave the order exactly as an empty cache would.

    These are the two open defects in this method, reported rather than fixed
    (the source is out of scope for this change), and they are stated as tests
    so that the fix has an executable definition of done:

    * a **one-sided book** (`ask = 0`, the shape a venue publishes when the
      offer side is empty or halted) is skipped by `_reference_price` — which
      is correct — and then averaged by `_last_price` into `(bid + 0) / 2`, so
      the buy is priced at *half the bid*;
    * a **zero anywhere in the cache** (both quote sides, or a trade printed at
      zero) is returned as a reference of `0` and reaches the venue as
      `price: "0.00"`, even though the identical zero coming from the venue's
      own ticker is refused by the same method one branch away.

    The invariant is not a threshold anyone has to argue about: a price of zero
    carries no information, so the request built with it must equal the request
    built without it. Both are `xfail(strict=True)`: when the source stops
    fabricating these prices, the marker fails the run and has to be removed.
    """

    @staticmethod
    def _baseline(make_env) -> dict[str, Any]:
        """The body sent when the cache holds nothing at all."""
        env = make_env()
        env.spot.responses["tickers"] = [{"last": TICKER_LAST}]
        _market_buy(env)
        return _sent_body(env)

    @pytest.mark.xfail(
        strict=True,
        reason="F-02a: a zero ask is averaged into the reference, pricing the buy at half the bid",
    )
    def test_a_one_sided_book_does_not_price_the_buy_at_half_the_bid(self, make_env):
        env = make_env()
        _quote(env, SPOT_BTC_USDT, BEST_BID, "0.00")
        env.spot.responses["tickers"] = [{"last": TICKER_LAST}]

        _market_buy(env)

        body = _sent_body(env)
        assert Decimal(body["price"]) != Decimal(BEST_BID) / 2 * Decimal("1.05")
        assert body == self._baseline(make_env)

    @pytest.mark.xfail(
        strict=True,
        reason="F-02b: a zero cached price is sent as `price: 0.00` instead of being refused",
    )
    @pytest.mark.parametrize("source", ["quote", "trade"])
    def test_a_zero_cached_price_does_not_reach_the_venue(self, make_env, source):
        env = make_env()
        if source == "quote":
            _quote(env, SPOT_BTC_USDT, "0.00", "0.00")
        else:
            _trade(env, SPOT_BTC_USDT, "0.00")
        env.spot.responses["tickers"] = [{"last": TICKER_LAST}]

        _market_buy(env)

        body = _sent_body(env)
        assert Decimal(body["price"]) > 0, "a limit buy at zero is not the order that was asked for"
        assert body == self._baseline(make_env)
