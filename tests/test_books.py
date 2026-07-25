"""Regression tests for the local order book assembly in ``nautilus_gateio.books``.

Everything here is synthetic: no sockets are opened, no credentials are read and
no venue is contacted. The payload shapes mirror what Gate.io actually pushes,
including the per-product difference in the level container:

* spot ``spot.order_book_update`` sends ``[["price", "amount"], ...]``;
* futures, delivery and options send ``[{"p": price, "s": size}, ...]``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from nautilus_gateio.books import (
    ASK,
    BID,
    GateioOrderBook,
    OrderBookSequenceError,
    SnapshotStaleError,
)

# -- payload builders --------------------------------------------------------


def spot_snapshot(
    book_id: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    update_ms: int = 1_700_000_000_000,
) -> dict[str, Any]:
    """``GET /spot/order_book?with_id=true`` — positional levels, ms timestamps."""
    return {
        "id": book_id,
        "current": update_ms,
        "update": update_ms,
        "bids": [[price, size] for price, size in bids],
        "asks": [[price, size] for price, size in asks],
    }


def futures_snapshot(
    book_id: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    update_secs: float = 1_700_000_000.0,
) -> dict[str, Any]:
    """``GET /futures/{settle}/order_book`` — object levels, *seconds* timestamps."""
    return {
        "id": book_id,
        "current": update_secs,
        "update": update_secs,
        "bids": [{"p": price, "s": size} for price, size in bids],
        "asks": [{"p": price, "s": size} for price, size in asks],
    }


def spot_update(
    first_id: int,
    last_id: int,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    ts_ms: int = 1_700_000_000_100,
    symbol: str = "BTC_USDT",
) -> dict[str, Any]:
    """A ``spot.order_book_update`` result object."""
    return {
        "t": ts_ms,
        "s": symbol,
        "U": first_id,
        "u": last_id,
        "b": [[price, size] for price, size in (bids or [])],
        "a": [[price, size] for price, size in (asks or [])],
    }


def futures_update(
    first_id: int,
    last_id: int,
    bids: list[tuple[str, Any]] | None = None,
    asks: list[tuple[str, Any]] | None = None,
    ts_ms: int = 1_700_000_000_100,
    symbol: str = "BTC_USDT",
) -> dict[str, Any]:
    """A ``futures.order_book_update`` / ``options.order_book_update`` result object."""
    return {
        "t": ts_ms,
        "s": symbol,
        "U": first_id,
        "u": last_id,
        "b": [{"p": price, "s": size} for price, size in (bids or [])],
        "a": [{"p": price, "s": size} for price, size in (asks or [])],
    }


BUILDERS = {"spot": (spot_snapshot, spot_update), "futures": (futures_snapshot, futures_update)}


# -- the documented synchronisation sequence, per product --------------------


@pytest.mark.parametrize("shape", ["spot", "futures"])
def test_full_sequence_buffers_then_applies_from_the_straddling_update(shape: str) -> None:
    """The whole documented algorithm, for both level container shapes.

    Subscribe, buffer, snapshot, discard the older notifications, apply the one
    that straddles the snapshot id, then apply the following ones in order.
    """
    make_snapshot, make_update = BUILDERS[shape]
    book = GateioOrderBook("BTC_USDT")

    # 1. notifications arrive before the snapshot and are buffered, not applied
    assert book.apply_update(make_update(90, 95, bids=[("99", "1")])) == []
    assert book.apply_update(make_update(96, 100, bids=[("98", "2")])) == []
    assert book.apply_update(make_update(101, 105, asks=[("101", "3")])) == []
    assert book.updates_buffered == 3
    assert not book.is_synced
    assert book.depth == (0, 0)

    # 2. the snapshot lands inside the buffered range (id 100)
    changes = book.apply_snapshot(
        make_snapshot(100, bids=[("99.5", "5"), ("99.0", "4")], asks=[("100.5", "6")]),
    )

    # 3./4. the two notifications ending at or before 100 are dropped; the one
    # straddling 100 (U=101 <= 101) is applied.
    assert book.is_synced
    assert book.updates_dropped == 2
    assert book.last_update_id == 105
    assert (ASK, Decimal("101"), Decimal("3")) in changes

    assert book.bids() == [(Decimal("99.5"), Decimal("5")), (Decimal("99.0"), Decimal("4"))]
    assert book.asks() == [(Decimal("100.5"), Decimal("6")), (Decimal("101"), Decimal("3"))]

    # 5. contiguous updates continue to apply; a zero size deletes the level
    changes = book.apply_update(make_update(106, 110, bids=[("99.0", "0")]))
    assert changes == [(BID, Decimal("99.0"), Decimal("0"))]
    assert book.bids() == [(Decimal("99.5"), Decimal("5"))]
    assert book.last_update_id == 110


@pytest.mark.parametrize("shape", ["spot", "futures"])
def test_gap_raises_order_book_sequence_error_and_unsyncs(shape: str) -> None:
    make_snapshot, make_update = BUILDERS[shape]
    book = GateioOrderBook("BTC_USDT")
    book.apply_snapshot(make_snapshot(100, bids=[("99", "1")], asks=[("101", "1")]))
    assert book.is_synced

    # 101 is expected; 105 means updates 101-104 were lost.
    with pytest.raises(OrderBookSequenceError) as excinfo:
        book.apply_update(make_update(105, 110, bids=[("99", "2")]))

    assert "missing 4 updates" in str(excinfo.value)
    assert not book.is_synced
    assert book.gaps_detected == 1


@pytest.mark.parametrize("shape", ["spot", "futures"])
def test_empty_delta_still_advances_the_sequence(shape: str) -> None:
    """A message with no levels advances ``u``; skipping it would fake a gap."""
    make_snapshot, make_update = BUILDERS[shape]
    book = GateioOrderBook("BTC_USDT")
    book.apply_snapshot(make_snapshot(100, bids=[("99", "1")], asks=[("101", "1")]))

    assert book.apply_update(make_update(101, 104)) == []
    assert book.last_update_id == 104

    # The next contiguous message must not be reported as a gap.
    assert book.apply_update(make_update(105, 106, bids=[("99", "2")])) != []


def test_snapshot_older_than_the_buffer_raises_and_keeps_the_buffer() -> None:
    """Gate.io's REST snapshot lags its own stream; the remedy is a newer one."""
    book = GateioOrderBook("BTC_USDT")
    book.apply_update(spot_update(200, 205, bids=[("99", "1")]))
    book.apply_update(spot_update(206, 210, bids=[("98", "1")]))

    with pytest.raises(OrderBookSequenceError):
        book.apply_snapshot(spot_snapshot(150, bids=[("99", "9")], asks=[("101", "9")]))

    # The unconsumed notifications survive for the retry with a newer snapshot.
    changes = book.apply_snapshot(spot_snapshot(204, bids=[("99", "9")], asks=[("101", "9")]))
    assert book.is_synced
    assert book.last_update_id == 210
    assert (BID, Decimal("98"), Decimal("1")) in changes


def test_stale_snapshot_is_rejected_and_leaves_the_book_untouched() -> None:
    """A REST snapshot older than the live book must never roll it backwards.

    Regression for md-02: a snapshot request in flight while the stream
    resynchronises the book (here through a ``full`` message) used to be applied
    unconditionally, republishing superseded depth at a lower update id.
    """
    book = GateioOrderBook("BTC_USDT")
    book.apply_snapshot(spot_snapshot(100, bids=[("99", "1")], asks=[("101", "1")]))

    # The venue pushes a complete snapshot on the incremental channel.
    book.apply_update(
        {
            "t": 1,
            "s": "BTC_USDT",
            "u": 500,
            "full": True,
            "b": [["99.9", "7"]],
            "a": [["100.1", "8"]],
        }
    )
    assert book.last_update_id == 500
    assert book.best_bid() == (Decimal("99.9"), Decimal("7"))

    # The REST snapshot that was already in flight now returns, older.
    with pytest.raises(SnapshotStaleError):
        book.apply_snapshot(spot_snapshot(400, bids=[("50", "1")], asks=[("150", "1")]))

    assert book.last_update_id == 500
    assert book.best_bid() == (Decimal("99.9"), Decimal("7"))
    assert book.best_ask() == (Decimal("100.1"), Decimal("8"))
    assert book.is_synced
    assert book.snapshots_stale == 1


def test_snapshot_with_the_same_id_is_also_stale() -> None:
    book = GateioOrderBook("BTC_USDT")
    book.apply_snapshot(spot_snapshot(100, bids=[("99", "1")], asks=[("101", "1")]))
    with pytest.raises(SnapshotStaleError):
        book.apply_snapshot(spot_snapshot(100, bids=[("1", "1")], asks=[("2", "1")]))
    assert book.best_bid() == (Decimal("99"), Decimal("1"))


def test_unsynced_book_accepts_any_snapshot() -> None:
    """After a reset the book has no state to protect, so no staleness applies."""
    book = GateioOrderBook("BTC_USDT")
    book.apply_snapshot(spot_snapshot(900, bids=[("99", "1")], asks=[("101", "1")]))
    book.reset()
    book.apply_snapshot(spot_snapshot(5, bids=[("98", "2")], asks=[("102", "2")]))
    assert book.is_synced
    assert book.last_update_id == 5


def test_fractional_futures_sizes_are_preserved_exactly() -> None:
    """The book itself is precision-free; it must not round venue sizes.

    Gate.io may push fractional contract sizes on the futures channels. Rounding
    them here would corrupt the source of truth used to rebuild snapshots; the
    quantisation to the instrument's precision belongs to the publishing layer.
    """
    book = GateioOrderBook("BTC_USDT")
    book.apply_snapshot(futures_snapshot(10, bids=[("99", "0.3")], asks=[("101", "1.7")]))
    assert book.best_bid() == (Decimal("99"), Decimal("0.3"))
    assert book.best_ask() == (Decimal("101"), Decimal("1.7"))

    book.apply_update(futures_update(11, 12, bids=[("99", "0.25")]))
    assert book.best_bid() == (Decimal("99"), Decimal("0.25"))


def test_snapshot_without_an_id_is_rejected() -> None:
    book = GateioOrderBook("BTC_USDT")
    with pytest.raises(ValueError, match="with_id=true"):
        book.apply_snapshot({"bids": [], "asks": []})


def test_seconds_and_millisecond_timestamps_are_both_normalised() -> None:
    """REST reports seconds on futures/options and milliseconds on spot."""
    spot_book = GateioOrderBook("BTC_USDT")
    spot_book.apply_snapshot(
        spot_snapshot(1, [("99", "1")], [("101", "1")], update_ms=1_700_000_000_000)
    )
    futures_book = GateioOrderBook("BTC_USDT")
    futures_book.apply_snapshot(
        futures_snapshot(1, [("99", "1")], [("101", "1")], update_secs=1_700_000_000.0),
    )
    assert spot_book.last_update_ms == futures_book.last_update_ms == 1_700_000_000_000
