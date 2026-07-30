"""Regression tests for ``InstrumentStatus`` and ``InstrumentClose``.

Gate.io publishes no status channel on any product, so the status feed is
derived from the instrument listing endpoints and rides the instrument reload
cadence. ``InstrumentClose`` exists only where a contract actually settles:
delivery futures and options. Spot, perpetual and inverse perpetual markets
trade continuously and are refused with the reason.

Every assertion here is on **published data**, never on
``subscribed_instrument_status()``. The base class records a subscription before
creating the task that would raise, so a test asserting on the subscription list
passes on a tree where neither hook exists at all.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import (
    SubscribeInstrumentClose,
    SubscribeInstrumentStatus,
    UnsubscribeInstrumentClose,
    UnsubscribeInstrumentStatus,
)
from nautilus_trader.model.data import InstrumentClose, InstrumentStatus
from nautilus_trader.model.enums import InstrumentCloseType, MarketStatusAction
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from nautilus_gateio import data as data_module
from nautilus_gateio.common.constants import GATEIO_CLIENT_ID, GATEIO_VENUE
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioClientError
from nautilus_gateio.common.status import diff_and_emit_statuses, market_status_action
from nautilus_gateio.config import GateioDataClientConfig
from nautilus_gateio.data import GateioDataClient
from nautilus_gateio.instruments import parse_delivery_instrument
from tests.test_data_client import (
    OPTION_ID,
    OPTION_SYMBOL,
    PERP_ID,
    SPOT_ID,
    Harness,
    StubProvider,
    build_instruments,
)

DELIVERY_SYMBOL = "BTC_USDT_20260925"
DELIVERY_ID = InstrumentId.from_str(f"{DELIVERY_SYMBOL}.GATE_IO")

#: A settlement instant safely in the past, so no watcher has to wait for it.
EXPIRY_SECS = 1_600_000_000


def delivery_instrument(settle_price: str = "0") -> Any:
    instrument = parse_delivery_instrument(
        {
            "name": DELIVERY_SYMBOL,
            "quanto_multiplier": "0.0001",
            "order_price_round": "0.1",
            "mark_price_round": "0.1",
            "maker_fee_rate": "0.0002",
            "taker_fee_rate": "0.0005",
            "maintenance_rate": "0.003",
            "leverage_min": "1",
            "leverage_max": "100",
            "order_size_min": 1,
            "order_size_max": 1_000_000,
            "expire_time": EXPIRY_SECS,
            "create_time": 1_500_000_000,
            "settle_price": settle_price,
            "in_delisting": False,
        },
    )
    assert instrument is not None
    return instrument


@pytest.fixture
def lifecycle() -> Harness:
    """A client serving every product, with a delivery contract in the provider."""
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    provider: InstrumentProvider = StubProvider()
    for instrument in [*build_instruments(), delivery_instrument()]:
        provider.add(instrument)
    client = GateioDataClient(
        loop=asyncio.new_event_loop(),
        client_id=GATEIO_CLIENT_ID,
        msgbus=msgbus,
        cache=Cache(),
        clock=clock,
        instrument_provider=provider,
        http_client=object(),
        config=GateioDataClientConfig(
            products=(
                GateioProductType.SPOT,
                GateioProductType.PERP,
                GateioProductType.FUT,
                GateioProductType.OPT,
            ),
        ),
    )
    return Harness(client)


def statuses(harness: Harness) -> list[InstrumentStatus]:
    return [item for item in harness.published if isinstance(item, InstrumentStatus)]


def closes(harness: Harness) -> list[InstrumentClose]:
    return [item for item in harness.published if isinstance(item, InstrumentClose)]


class StubListings:
    """A stand-in for one product's REST namespace.

    Each method may be handed an exception to raise instead of a payload, which
    is how the partial-failure guard is exercised: a listing that failed must
    not be read as an empty listing.
    """

    def __init__(self, single: Any = None, listing: Any = None) -> None:
        self.single = single
        self.listing = listing
        self.calls: list[str] = []

    def _answer(self, name: str, value: Any) -> Any:
        self.calls.append(name)
        if isinstance(value, Exception):
            raise value
        return value

    async def currency_pair(self, pair: str) -> Any:
        return self._answer(f"currency_pair:{pair}", self.single)

    async def currency_pairs(self) -> Any:
        return self._answer("currency_pairs", self.listing)

    async def contract(self, name: str) -> Any:
        return self._answer(f"contract:{name}", self.single)

    async def contracts(self, *args: Any, **kwargs: Any) -> Any:
        return self._answer("contracts", self.listing)


# -- the mapping table -------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "payload", "expected", "reason"),
    [
        (
            GateioProductType.SPOT,
            {"trade_status": "tradable"},
            MarketStatusAction.TRADING,
            "trade_status=tradable",
        ),
        (
            GateioProductType.SPOT,
            {"trade_status": "untradable"},
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            "trade_status=untradable",
        ),
        (
            GateioProductType.SPOT,
            {"trade_status": "sellable", "buy_start": 4_000_000_000},
            MarketStatusAction.PRE_OPEN,
            "buy_start=4000000000",
        ),
        (
            GateioProductType.SPOT,
            {"trade_status": "buyable", "sell_start": 4_000_000_000},
            MarketStatusAction.PRE_OPEN,
            "sell_start=4000000000",
        ),
        (
            # A one-sided pair with both starts in the past. It is genuinely
            # trading on one side and genuinely absent from the instrument set
            # this adapter publishes, because `CurrencyPair` cannot express
            # "buys only"; status must agree with instrument availability.
            GateioProductType.SPOT,
            {"trade_status": "sellable", "buy_start": 1, "sell_start": 1},
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            "trade_status=sellable",
        ),
        (
            GateioProductType.PERP,
            {"in_delisting": False},
            MarketStatusAction.TRADING,
            "in_delisting=false",
        ),
        (
            GateioProductType.PERP,
            {"in_delisting": True},
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            "in_delisting=true",
        ),
        (
            GateioProductType.PERP,
            {"in_delisting": False, "status": "delisted"},
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            "status=delisted",
        ),
        (
            GateioProductType.FUT,
            {"in_delisting": False, "expire_time": 4_000_000_000},
            MarketStatusAction.TRADING,
            "in_delisting=false",
        ),
        (
            GateioProductType.FUT,
            {"in_delisting": False, "expire_time": 1_000},
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            "expire_time=1000 elapsed",
        ),
        (
            GateioProductType.OPT,
            {"is_active": True, "expiration_time": 4_000_000_000},
            MarketStatusAction.TRADING,
            "is_active=true",
        ),
        (
            GateioProductType.OPT,
            {"is_active": False, "expiration_time": 4_000_000_000},
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            "is_active=false",
        ),
        (
            GateioProductType.OPT,
            {"is_active": True, "expiration_time": 1_000},
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            "expiration_time=1000 elapsed",
        ),
        (
            # A contract payload that carries none of the deciding fields. The
            # action is unchanged — nothing the venue said states a halt — but
            # the reason may not quote `in_delisting=false` for a payload that
            # never carried `in_delisting`, which is the one thing a reason
            # naming a venue field verbatim exists to rule out.
            GateioProductType.PERP,
            {"name": "BTC_USDT"},
            MarketStatusAction.TRADING,
            "no venue field states a halt",
        ),
        (
            GateioProductType.PERP,
            {"status": "trading"},
            MarketStatusAction.TRADING,
            "status=trading",
        ),
        (
            GateioProductType.FUT,
            {"expire_time": 4_000_000_000},
            MarketStatusAction.TRADING,
            "expire_time=4000000000 ahead",
        ),
    ],
)
def test_the_listing_payload_decides_the_action(
    product: GateioProductType,
    payload: dict[str, Any],
    expected: MarketStatusAction,
    reason: str,
) -> None:
    action, actual_reason = market_status_action(product, payload, now_secs=2_000_000_000)

    assert action == expected
    assert actual_reason == reason


def test_only_three_actions_are_ever_produced() -> None:
    """Gate.io distinguishes tradable, one-sided-with-a-start, and not tradable.

    Every other ``MarketStatusAction`` — ``HALT``, ``PAUSE``, ``CROSS``,
    ``PRE_CLOSE`` and the rest — would be an invention of venue intent.
    """
    payloads = [
        (GateioProductType.SPOT, {"trade_status": status})
        for status in ("tradable", "untradable", "buyable", "sellable", "")
    ]
    payloads += [(GateioProductType.PERP, {"in_delisting": flag}) for flag in (True, False)]
    payloads += [(GateioProductType.OPT, {"is_active": flag}) for flag in (True, False)]

    produced = {market_status_action(product, payload, 1)[0] for product, payload in payloads}

    assert produced <= {
        MarketStatusAction.TRADING,
        MarketStatusAction.PRE_OPEN,
        MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
    }


# -- the diff ----------------------------------------------------------------


def test_absence_from_a_read_listing_is_a_delisting() -> None:
    cache = {SPOT_ID: MarketStatusAction.TRADING}
    emitted: list[tuple[InstrumentId, MarketStatusAction, str]] = []

    diff_and_emit_statuses(
        new_statuses={},
        cached_statuses=cache,
        subscriptions={SPOT_ID},
        emit=lambda *args: emitted.append(args),
        removable=lambda _: True,
    )

    assert cache[SPOT_ID] == MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
    assert [item[1] for item in emitted] == [MarketStatusAction.NOT_AVAILABLE_FOR_TRADING]


def test_absence_from_a_listing_that_was_not_read_is_not_a_delisting() -> None:
    """The guard Kraken carries in-tree, after the bug was found there first."""
    cache = {SPOT_ID: MarketStatusAction.TRADING}
    emitted: list[Any] = []

    diff_and_emit_statuses(
        new_statuses={},
        cached_statuses=cache,
        subscriptions={SPOT_ID},
        emit=lambda *args: emitted.append(args),
        removable=lambda _: False,
    )

    assert cache[SPOT_ID] == MarketStatusAction.TRADING
    assert emitted == []


def test_the_cache_tracks_unsubscribed_instruments_without_emitting() -> None:
    """A subscription taken out later must have something to replay."""
    cache: dict[InstrumentId, MarketStatusAction] = {}
    emitted: list[Any] = []

    diff_and_emit_statuses(
        new_statuses={
            SPOT_ID: (MarketStatusAction.TRADING, "trade_status=tradable"),
            PERP_ID: (MarketStatusAction.TRADING, "in_delisting=false"),
        },
        cached_statuses=cache,
        subscriptions={SPOT_ID},
        emit=lambda *args: emitted.append(args),
        removable=lambda _: True,
    )

    assert cache[PERP_ID] == MarketStatusAction.TRADING
    assert [item[0] for item in emitted] == [SPOT_ID]


# -- the hooks ---------------------------------------------------------------


def status_command(instrument_id: InstrumentId) -> SubscribeInstrumentStatus:
    return SubscribeInstrumentStatus(
        instrument_id,
        GATEIO_CLIENT_ID,
        GATEIO_VENUE,
        UUID4(),
        0,
    )


async def test_subscribing_reports_the_current_status_at_once(lifecycle: Harness) -> None:
    """Fails on the pre-change tree: the base coroutine raises instead.

    Without this the diff would only speak on a *change*, so a strategy that
    subscribes to a healthy instrument in a quiet market would learn nothing at
    all until it stopped being healthy.
    """
    lifecycle.client._spot_http = StubListings(
        single={"id": "BTC_USDT", "trade_status": "tradable"}
    )

    await lifecycle.client._subscribe_instrument_status(status_command(SPOT_ID))

    published = statuses(lifecycle)
    assert len(published) == 1
    assert published[0].instrument_id == SPOT_ID
    assert published[0].action == MarketStatusAction.TRADING
    assert published[0].is_trading is True
    # Gate.io publishes neither, and the fields are tri-state so that an adapter
    # can decline to guess.
    assert published[0].is_quoting is None
    assert published[0].is_short_sell_restricted is None


async def test_the_status_timestamp_comes_from_the_clock(lifecycle: Harness) -> None:
    """``create_time`` is the listing instant and never moves."""
    lifecycle.client._spot_http = StubListings(
        single={"id": "BTC_USDT", "trade_status": "tradable", "create_time": 1_500_000_000},
    )
    before = lifecycle.client._clock.timestamp_ns()

    await lifecycle.client._subscribe_instrument_status(status_command(SPOT_ID))

    published = statuses(lifecycle)[0]
    assert published.ts_event >= before
    assert published.ts_event == published.ts_init


async def test_a_delisting_transition_is_emitted_exactly_once(lifecycle: Harness) -> None:
    client = lifecycle.client
    perp = StubListings(
        single={"name": "BTC_USDT", "in_delisting": False},
        listing=[{"name": "BTC_USDT", "in_delisting": False}],
    )
    client._futures_http[GateioProductType.PERP] = perp  # type: ignore[assignment]

    await client._subscribe_instrument_status(status_command(PERP_ID))
    await client._poll_instrument_statuses()  # unchanged
    perp.listing = [{"name": "BTC_USDT", "in_delisting": True}]
    await client._poll_instrument_statuses()  # the transition
    await client._poll_instrument_statuses()  # unchanged again

    published = statuses(lifecycle)
    assert [item.action for item in published] == [
        MarketStatusAction.TRADING,
        MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
    ]
    # `is_trading` is the field a strategy reads to decide whether to send an
    # order; asserting it only in the TRADING case leaves a delisted instrument
    # published as tradable indistinguishable from a correct event.
    assert [item.is_trading for item in published] == [True, False]


async def test_a_product_whose_listing_failed_delists_nothing(lifecycle: Harness) -> None:
    """A skipped listing must not look like a mass delisting."""
    client = lifecycle.client
    client._spot_http = StubListings(  # type: ignore[assignment]
        single={"id": "BTC_USDT", "trade_status": "tradable"},
        listing=GateioClientError(500, "SERVER_ERROR", "listing unavailable"),
    )
    perp = StubListings(
        single={"name": "BTC_USDT", "in_delisting": False},
        listing=[{"name": "BTC_USDT", "in_delisting": True}],
    )
    client._futures_http[GateioProductType.PERP] = perp  # type: ignore[assignment]

    await client._subscribe_instrument_status(status_command(SPOT_ID))
    await client._subscribe_instrument_status(status_command(PERP_ID))
    lifecycle.published.clear()

    await client._poll_instrument_statuses()

    reported = {item.instrument_id: item.action for item in statuses(lifecycle)}
    assert SPOT_ID not in reported, "a failed spot listing was read as a delisting"
    assert reported[PERP_ID] == MarketStatusAction.NOT_AVAILABLE_FOR_TRADING


async def test_a_poll_that_read_nothing_emits_nothing(lifecycle: Harness) -> None:
    client = lifecycle.client
    client._spot_http = StubListings(  # type: ignore[assignment]
        single={"id": "BTC_USDT", "trade_status": "tradable"},
        listing=GateioClientError(500, "SERVER_ERROR", "listing unavailable"),
    )

    await client._subscribe_instrument_status(status_command(SPOT_ID))
    lifecycle.published.clear()
    await client._poll_instrument_statuses()

    assert statuses(lifecycle) == []
    assert client._status_cache[SPOT_ID] == MarketStatusAction.TRADING


async def test_an_option_listing_is_polled_per_underlying(lifecycle: Harness) -> None:
    """``GET /options/contracts`` is queried one underlying at a time."""
    client = lifecycle.client
    options = StubListings(
        single={"name": OPTION_SYMBOL, "is_active": True, "expiration_time": 4_000_000_000},
        listing=[{"name": OPTION_SYMBOL, "is_active": False}],
    )
    client._options_http = options  # type: ignore[assignment]

    await client._subscribe_instrument_status(status_command(OPTION_ID))
    lifecycle.published.clear()
    await client._poll_instrument_statuses()

    assert statuses(lifecycle)[0].action == MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
    assert client._status_scope(OPTION_ID) == "opt:BTC_USDT"


async def test_a_status_a_filtered_instrument_would_hide_is_still_reported(
    lifecycle: Harness,
) -> None:
    """The provider drops untradable pairs before an instrument is ever built.

    Deriving status from the provider's output would therefore make the one
    transition that matters — tradable to untradable — invisible twice over.
    """
    client = lifecycle.client
    client._spot_http = StubListings(  # type: ignore[assignment]
        single={"id": "BTC_USDT", "trade_status": "untradable"},
    )

    await client._subscribe_instrument_status(status_command(SPOT_ID))

    assert statuses(lifecycle)[0].action == MarketStatusAction.NOT_AVAILABLE_FOR_TRADING


async def test_unsubscribing_stops_emissions_for_that_instrument_only(
    lifecycle: Harness,
) -> None:
    client = lifecycle.client
    client._spot_http = StubListings(  # type: ignore[assignment]
        single={"id": "BTC_USDT", "trade_status": "tradable"},
        listing=[{"id": "BTC_USDT", "trade_status": "untradable"}],
    )
    perp = StubListings(
        single={"name": "BTC_USDT", "in_delisting": False},
        listing=[{"name": "BTC_USDT", "in_delisting": True}],
    )
    client._futures_http[GateioProductType.PERP] = perp  # type: ignore[assignment]

    await client._subscribe_instrument_status(status_command(SPOT_ID))
    await client._subscribe_instrument_status(status_command(PERP_ID))
    await client._unsubscribe_instrument_status(
        UnsubscribeInstrumentStatus(SPOT_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    lifecycle.published.clear()

    await client._poll_instrument_statuses()

    assert [item.instrument_id for item in statuses(lifecycle)] == [PERP_ID]


async def test_a_status_subscription_for_an_unconfigured_product_is_refused() -> None:
    clock = LiveClock()
    provider: InstrumentProvider = StubProvider()
    for instrument in build_instruments():
        provider.add(instrument)
    client = GateioDataClient(
        loop=asyncio.new_event_loop(),
        client_id=GATEIO_CLIENT_ID,
        msgbus=MessageBus(trader_id=TraderId("TESTER-000"), clock=clock),
        cache=Cache(),
        clock=clock,
        instrument_provider=provider,
        http_client=object(),
        config=GateioDataClientConfig(products=(GateioProductType.SPOT,)),
    )
    harness = Harness(client)

    await client._subscribe_instrument_status(status_command(PERP_ID))

    assert statuses(harness) == []
    assert PERP_ID not in client._instrument_status_subs


# -- instrument close --------------------------------------------------------


def close_command(instrument_id: InstrumentId) -> SubscribeInstrumentClose:
    return SubscribeInstrumentClose(instrument_id, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0)


class StubDeliveryHttp:
    """The two independent public sources of a delivery ``settle_price``."""

    def __init__(self, contract_price: str, ticker_price: str) -> None:
        self.contract_price = contract_price
        self.ticker_price = ticker_price

    async def contract(self, name: str) -> dict[str, Any]:
        return {"name": name, "settle_price": self.contract_price}

    async def tickers(self, contract: str | None = None) -> list[dict[str, Any]]:
        return [{"contract": contract, "settle_price": self.ticker_price}]


class StubSettlementsHttp:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[tuple[Any, ...]] = []

    async def settlements(
        self,
        underlying: str,
        limit: int | None = None,
        offset: int | None = None,
        frm: int | None = None,
        to: int | None = None,
    ) -> list[dict[str, Any]]:
        self.queries.append((underlying, frm, to))
        return self.rows


async def test_a_settled_delivery_contract_publishes_its_close(lifecycle: Harness) -> None:
    """Fails on the pre-change tree: the base coroutine raises instead."""
    client = lifecycle.client
    client._futures_http[GateioProductType.FUT] = StubDeliveryHttp(  # type: ignore[assignment]
        "115000.5",
        "115000.5",
    )

    await client._watch_instrument_close(DELIVERY_ID)

    published = closes(lifecycle)
    assert len(published) == 1
    assert str(published[0].close_price) == "115000.5"
    assert published[0].close_type == InstrumentCloseType.CONTRACT_EXPIRED
    assert published[0].ts_event == EXPIRY_SECS * 1_000_000_000


async def test_an_unsettled_delivery_contract_publishes_nothing(
    lifecycle: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``settle_price`` stays ``"0"`` until the contract settles."""
    monkeypatch.setattr(data_module, "INSTRUMENT_CLOSE_TIMEOUT_SECS", 0.0)
    client = lifecycle.client
    client._futures_http[GateioProductType.FUT] = StubDeliveryHttp("0", "0")  # type: ignore[assignment]

    await client._watch_instrument_close(DELIVERY_ID)

    assert closes(lifecycle) == []


async def test_two_disagreeing_settlement_sources_publish_nothing(
    lifecycle: Harness,
) -> None:
    """A cross-check with only an agreeing fixture is decoration."""
    client = lifecycle.client
    client._futures_http[GateioProductType.FUT] = StubDeliveryHttp(  # type: ignore[assignment]
        "115000.5",
        "114000.0",
    )

    await client._watch_instrument_close(DELIVERY_ID)

    assert closes(lifecycle) == []
    assert DELIVERY_ID not in client._instrument_close_emitted


async def test_an_option_close_is_its_own_value_not_the_underlying_price(
    lifecycle: Harness,
) -> None:
    """``settle_price`` on a settlement row is the *underlying*'s averaged price.

    The option is quoted in USDT per unit of that underlying, so publishing
    ``settle_price`` would be wrong by orders of magnitude for anything but a
    deep in-the-money contract. The option's own value is ``profit`` — the
    per-contract cash intrinsic value — divided back out by the multiplier.
    """
    client = lifecycle.client
    # multiplier 0.01, strike 70000, underlying settles at 115000:
    # intrinsic per unit is 45000, so profit per contract is 450.
    client._options_http = StubSettlementsHttp(  # type: ignore[assignment]
        [
            {
                "time": 1_785_000_000,
                "contract": OPTION_SYMBOL,
                "strike_price": "70000",
                "settle_price": "115000",
                "profit": "450",
                "fee": "0.1",
            },
        ],
    )

    await client._watch_instrument_close(OPTION_ID)

    published = closes(lifecycle)
    assert len(published) == 1
    assert str(published[0].close_price) == "45000"
    assert published[0].close_type == InstrumentCloseType.CONTRACT_EXPIRED


async def test_an_option_close_reads_the_contract_from_the_instrument(
    lifecycle: Harness,
) -> None:
    """The terms are on the instrument; ``info`` is a copy of the venue payload.

    ``CryptoOption`` carries the multiplier, the strike and the call/put kind as
    first-class fields. Reading them out of ``info`` instead made an instrument
    whose payload did not survive a round trip settle as a put with no
    multiplier — silently, because ``bool(None)`` is ``False``.
    """
    client = lifecycle.client
    instrument = client._instrument(OPTION_ID)
    assert instrument is not None
    instrument.info.clear()
    client._options_http = StubSettlementsHttp(  # type: ignore[assignment]
        [
            {
                "time": 1_785_000_000,
                "contract": OPTION_SYMBOL,
                "strike_price": "70000",
                "settle_price": "115000",
                "profit": "450",
                "fee": "0.1",
            },
        ],
    )

    await client._watch_instrument_close(OPTION_ID)

    assert str(closes(lifecycle)[0].close_price) == "45000"


async def test_an_out_of_the_money_option_closes_at_zero(lifecycle: Harness) -> None:
    """Zero here is the venue's own number, not a stand-in for "unknown"."""
    client = lifecycle.client
    client._options_http = StubSettlementsHttp(  # type: ignore[assignment]
        [
            {
                "time": 1_785_000_000,
                "contract": OPTION_SYMBOL,
                "strike_price": "70000",
                "settle_price": "50000",
                "profit": "0",
                "fee": "0",
            },
        ],
    )

    await client._watch_instrument_close(OPTION_ID)

    assert str(closes(lifecycle)[0].close_price) == "0"


async def test_an_option_settlement_that_contradicts_its_own_row_publishes_nothing(
    lifecycle: Harness,
) -> None:
    client = lifecycle.client
    client._options_http = StubSettlementsHttp(  # type: ignore[assignment]
        [
            {
                "time": 1_785_000_000,
                "contract": OPTION_SYMBOL,
                "strike_price": "70000",
                "settle_price": "115000",
                # 450 would be right; 900 implies 90000 per unit against an
                # intrinsic value of 45000.
                "profit": "900",
                "fee": "0.1",
            },
        ],
    )

    await client._watch_instrument_close(OPTION_ID)

    assert closes(lifecycle) == []


async def test_a_settlement_that_never_appears_publishes_nothing(
    lifecycle: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no null ``close_price``, so silence is the only honest answer."""
    monkeypatch.setattr(data_module, "INSTRUMENT_CLOSE_TIMEOUT_SECS", 0.0)
    client = lifecycle.client
    client._options_http = StubSettlementsHttp([])  # type: ignore[assignment]

    await client._watch_instrument_close(OPTION_ID)

    assert closes(lifecycle) == []


async def test_the_settlement_window_brackets_the_stated_expiry(lifecycle: Harness) -> None:
    client = lifecycle.client
    settlements = StubSettlementsHttp([])
    client._options_http = settlements  # type: ignore[assignment]

    await client._fetch_instrument_close(OPTION_ID, client._instrument(OPTION_ID))

    underlying, frm, to = settlements.queries[0]
    expiry_secs = client._instrument(OPTION_ID).expiration_ns // 1_000_000_000
    assert underlying == "BTC_USDT"
    assert frm < expiry_secs < to


@pytest.mark.parametrize("instrument_id", [SPOT_ID, PERP_ID])
async def test_a_continuous_market_close_subscription_is_refused(
    lifecycle: Harness,
    instrument_id: InstrumentId,
) -> None:
    """Spot and perpetual markets never settle, so there is no close price."""
    client = lifecycle.client

    await client._subscribe_instrument_close(close_command(instrument_id))

    assert client._instrument_close_tasks == {}
    assert closes(lifecycle) == []


async def test_unsubscribing_cancels_the_close_watcher(lifecycle: Harness) -> None:
    client = lifecycle.client
    client._loop = asyncio.get_running_loop()
    client._options_http = StubSettlementsHttp([])  # type: ignore[assignment]

    await client._subscribe_instrument_close(close_command(OPTION_ID))
    assert OPTION_ID in client._instrument_close_tasks
    task = client._instrument_close_tasks[OPTION_ID]

    await client._unsubscribe_instrument_close(
        UnsubscribeInstrumentClose(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    await asyncio.sleep(0)

    assert task.cancelled() or task.cancelling()
    assert client._instrument_close_tasks == {}
    assert closes(lifecycle) == []


async def test_a_close_is_published_at_most_once(lifecycle: Harness) -> None:
    client = lifecycle.client
    client._futures_http[GateioProductType.FUT] = StubDeliveryHttp(  # type: ignore[assignment]
        "115000.5",
        "115000.5",
    )

    await client._watch_instrument_close(DELIVERY_ID)
    await client._watch_instrument_close(DELIVERY_ID)

    assert len(closes(lifecycle)) == 1
