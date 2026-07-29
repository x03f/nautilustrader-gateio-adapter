"""Local L2 order book assembly for Gate.io incremental depth streams.

Gate.io publishes depth as a REST snapshot plus an incremental WebSocket stream
(``spot.order_book_update`` / ``futures.order_book_update`` /
``options.order_book_update``). The snapshot carries an ``id``; every update
carries the range of update ids it covers, ``U`` (first) to ``u`` (last). The
documented synchronisation algorithm is:

1. subscribe to the incremental channel and buffer the notifications;
2. fetch the REST snapshot with ``with_id=true`` and remember its ``id``;
3. discard every buffered notification whose ``u`` is older than the snapshot;
4. start applying from the notification that straddles the snapshot, i.e.
   ``U <= id + 1 <= u``;
5. if the snapshot is older than the whole buffer (``id + 1 < U`` of the first
   notification), fetch a newer snapshot;
6. if a later notification has ``U > previous u + 1``, updates were lost and the
   book must be rebuilt from a new snapshot;
7. a snapshot that is not newer than the state the book already holds is
   discarded rather than applied, so a slow REST response cannot roll a book
   that has meanwhile resynced itself backwards.

Spot and perpetual streams may also push a ``full: true`` message, which *is* a
complete snapshot of the subscribed depth. The venue documents it as re-pushable
at any time, so it is placed in the stream the same way a REST snapshot is: one
that is not newer than the local state is superseded depth and is discarded, and
one carrying no update id cannot be placed at all and is a sequence break.

Level amounts are absolute, not deltas: a level with size ``0`` is a deletion.
Messages with empty ``a``/``b`` arrays still advance the update id and must not
be skipped.

One thing this module deliberately does **not** do is bound the book to the
subscribed depth. The venue describes the channel's ``level`` parameter as
"optional depth level interested, only updates within are notified", which leaves
open whether a price level that falls out of the top-N window because a better
level appeared is reported as removed. If it is, the book stays at N levels by
itself; if it is not, levels below the window survive carrying the last size the
venue confirmed for them. Trimming would answer the question the other way and
drop depth the venue may still be maintaining, so neither reading is assumed
here — :attr:`GateioOrderBook.depth` is what an operator watches to settle it.

This module is deliberately free of any framework dependency — it deals in
:class:`~decimal.Decimal` prices and sizes only, so it can be unit tested
without a trading environment. Converting the returned changes into venue data
types is the data client's job.

Payload shapes differ per product and both are accepted transparently:

* spot uses ``[["price", "amount"], ...]``;
* futures, delivery and options use ``[{"p": "price", "s": size}, ...]``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

#: Side markers used in the change tuples returned by this module.
BID: Final[str] = "bid"
ASK: Final[str] = "ask"

#: ``(side, price, size)``; a size of zero means the level was removed.
BookChange = tuple[str, Decimal, Decimal]

#: Unix timestamps above this many milliseconds are already in milliseconds.
_MS_THRESHOLD: Final[int] = 100_000_000_000

_ZERO: Final[Decimal] = Decimal(0)


class OrderBookSequenceError(Exception):
    """Raised when the incremental stream is no longer contiguous.

    The local book is left unsynchronised; the caller must fetch a fresh REST
    snapshot (and, for a venue-driven resync, may also resubscribe) before
    publishing further deltas.
    """


class SnapshotStaleError(Exception):
    """Raised when a REST snapshot is older than the state the book already holds.

    Deliberately **not** a subclass of :class:`OrderBookSequenceError`: a stale
    snapshot is not a sequence break. The book is still synchronised and needs
    nothing at all; the snapshot is simply discarded. This happens when a REST
    request that was already in flight returns after the stream has resynced the
    book on its own (a ``full: true`` message, for example). Applying it would
    roll the book backwards and republish depth the venue has already replaced.
    """


def _to_decimal(value: Any) -> Decimal | None:
    """Convert a Gate.io numeric field to ``Decimal``, or ``None`` if unusable."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_levels(levels: Iterable[Any] | None) -> list[tuple[Decimal, Decimal]]:
    """Normalise either supported level container form into ``(price, size)`` pairs."""
    parsed: list[tuple[Decimal, Decimal]] = []
    if not levels:
        return parsed
    for level in levels:
        if isinstance(level, dict):
            raw_price = level.get("p")
            raw_size = level.get("s")
        elif isinstance(level, Sequence) and not isinstance(level, str | bytes):
            if len(level) < 2:
                continue
            raw_price, raw_size = level[0], level[1]
        else:  # pragma: no cover - defensive
            continue
        price = _to_decimal(raw_price)
        size = _to_decimal(raw_size)
        if price is None or size is None:
            continue
        parsed.append((price, size))
    return parsed


def _normalise_ms(value: Any) -> int:
    """Return a millisecond timestamp from a Gate.io ``t``/``current``/``update`` field.

    WebSocket messages always use milliseconds, while the REST snapshot reports
    seconds on the futures and options endpoints and milliseconds on spot, so
    the magnitude decides the unit.
    """
    number = _to_decimal(value)
    if number is None:
        return 0
    if number >= _MS_THRESHOLD:
        return int(number)
    return int(number * 1000)


class GateioOrderBook:
    """Maintains one instrument's L2 book from a snapshot plus incremental updates.

    Parameters
    ----------
    symbol : str
        The Gate.io symbol this book tracks; used only for error messages.
    max_buffer : int
        How many pre-snapshot notifications to retain. The buffer covers the
        latency of the REST snapshot request; older entries are discarded.

    Notes
    -----
    Instances are not thread safe. All methods are synchronous and cheap: a
    100-level book applies an update in microseconds.
    """

    def __init__(self, symbol: str, max_buffer: int = 2048) -> None:
        self.symbol = symbol
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_buffer)
        self._last_update_id: int = 0
        self._last_update_ms: int = 0
        self._synced: bool = False
        # counters
        self._gaps_detected: int = 0
        self._updates_applied: int = 0
        self._updates_dropped: int = 0
        self._updates_buffered: int = 0
        self._overlaps_applied: int = 0
        self._snapshots_applied: int = 0
        self._snapshots_stale: int = 0
        self._last_apply_was_snapshot: bool = False

    # -- introspection -----------------------------------------------------

    @property
    def is_synced(self) -> bool:
        """Whether the book is currently synchronised with the venue."""
        return self._synced

    @property
    def gaps_detected(self) -> int:
        """How many sequence gaps have been detected since construction."""
        return self._gaps_detected

    @property
    def last_update_id(self) -> int:
        """The venue update id the local book currently reflects."""
        return self._last_update_id

    @property
    def last_update_ms(self) -> int:
        """Venue timestamp of the last applied message, in milliseconds."""
        return self._last_update_ms

    @property
    def updates_applied(self) -> int:
        return self._updates_applied

    @property
    def updates_dropped(self) -> int:
        """Notifications discarded as older than the snapshot."""
        return self._updates_dropped

    @property
    def updates_buffered(self) -> int:
        """Notifications parked while waiting for a snapshot."""
        return self._updates_buffered

    @property
    def overlaps_applied(self) -> int:
        """Notifications whose range straddled the local update id.

        Expected exactly once per resynchronisation (the message that straddles
        the snapshot id); a persistently growing counter means the venue is
        replaying ranges.
        """
        return self._overlaps_applied

    @property
    def snapshots_applied(self) -> int:
        """Full replacements applied, whether from REST or a ``full`` message."""
        return self._snapshots_applied

    @property
    def snapshots_stale(self) -> int:
        """Snapshots rejected as older than the state the book already held.

        Counts both REST snapshots (which raise :class:`SnapshotStaleError`) and
        superseded ``full`` pushes (which are discarded silently, because the
        stream needs nothing from the caller).
        """
        return self._snapshots_stale

    @property
    def last_apply_was_snapshot(self) -> bool:
        """Whether the most recent :meth:`apply_update` replaced the whole book.

        Gate.io may push a ``full: true`` message on the incremental channel at
        any time. The returned changes still describe the transition exactly, so
        callers may ignore this; those that prefer to republish a cleared
        snapshot can branch on it.
        """
        return self._last_apply_was_snapshot

    @property
    def depth(self) -> tuple[int, int]:
        """The number of bid and ask levels currently held."""
        return len(self._bids), len(self._asks)

    def __len__(self) -> int:
        return len(self._bids) + len(self._asks)

    # -- state -------------------------------------------------------------

    def reset(self) -> None:
        """Drop all state, including buffered notifications."""
        self._bids.clear()
        self._asks.clear()
        self._buffer.clear()
        self._last_update_id = 0
        self._last_update_ms = 0
        self._synced = False
        self._last_apply_was_snapshot = False

    def bids(self) -> list[tuple[Decimal, Decimal]]:
        """Bid levels, best (highest) price first."""
        return sorted(self._bids.items(), key=lambda item: item[0], reverse=True)

    def asks(self) -> list[tuple[Decimal, Decimal]]:
        """Ask levels, best (lowest) price first."""
        return sorted(self._asks.items(), key=lambda item: item[0])

    def best_bid(self) -> tuple[Decimal, Decimal] | None:
        """The highest bid as ``(price, size)``, or ``None`` if there are no bids."""
        if not self._bids:
            return None
        price = max(self._bids)
        return price, self._bids[price]

    def best_ask(self) -> tuple[Decimal, Decimal] | None:
        """The lowest ask as ``(price, size)``, or ``None`` if there are no asks."""
        if not self._asks:
            return None
        price = min(self._asks)
        return price, self._asks[price]

    def spread(self) -> Decimal | None:
        """Best ask minus best bid, or ``None`` if either side is empty."""
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return ask[0] - bid[0]

    # -- ingestion ---------------------------------------------------------

    def apply_snapshot(self, payload: dict[str, Any]) -> list[BookChange]:
        """Replace the book with a REST snapshot and replay buffered updates.

        ``payload`` is the body of ``GET /spot/order_book``,
        ``GET /futures/{settle}/order_book`` or ``GET /options/order_book``,
        which must be requested with ``with_id=true`` so that it carries the
        ``id`` field the incremental stream is keyed on.

        Returns the changes produced by replaying the buffered notifications.
        Callers that publish a full snapshot from :meth:`bids` / :meth:`asks`
        after this call should ignore the return value: the state already
        includes those changes.

        Raises
        ------
        ValueError
            If the payload carries no update id.
        SnapshotStaleError
            If the book is already synchronised at an update id at least as new
            as the snapshot's. The book is left untouched and the caller has
            nothing to do.
        OrderBookSequenceError
            If the snapshot is already older than the buffered notifications.
            Gate.io's REST snapshot can lag its own stream by a few hundred
            milliseconds, so this is an expected condition: fetch a newer
            snapshot and call this method again. Notifications that were not
            consumed are kept for that retry.
        """
        book_id = payload.get("id")
        if book_id is None:
            book_id = payload.get("u")
        if book_id is None:
            raise ValueError(
                f"order book snapshot for {self.symbol!r} has no 'id'; request it with with_id=true"
            )

        snapshot_id = int(book_id)
        if self._synced and snapshot_id <= self._last_update_id:
            # The stream has already carried the book past this snapshot (for
            # example through a ``full: true`` message that arrived while the
            # REST request was in flight). Replacing the book would roll it
            # backwards and republish superseded depth.
            self._snapshots_stale += 1
            raise SnapshotStaleError(
                f"order book snapshot for {self.symbol!r} is stale: snapshot id {snapshot_id} "
                f"is not newer than the local update id {self._last_update_id}"
            )

        self._bids = {
            price: size for price, size in _parse_levels(payload.get("bids")) if size > _ZERO
        }
        self._asks = {
            price: size for price, size in _parse_levels(payload.get("asks")) if size > _ZERO
        }
        self._last_update_id = snapshot_id
        self._last_update_ms = _normalise_ms(payload.get("update") or payload.get("current"))
        self._synced = True
        self._snapshots_applied += 1
        self._last_apply_was_snapshot = True

        return self._drain_buffer()

    def apply_update(self, payload: dict[str, Any]) -> list[BookChange]:
        """Apply one incremental notification and return the resulting changes.

        ``payload`` may be either the full WebSocket message or just its
        ``result`` object. Before the book is synchronised the notification is
        buffered and an empty list is returned.

        Returns
        -------
        list[tuple[str, Decimal, Decimal]]
            ``(side, price, size)`` per changed level, where a size of zero
            means the level was removed. Sizes are absolute, not deltas.

        Raises
        ------
        OrderBookSequenceError
            If the notification starts after the local update id plus one, i.e.
            updates were lost. The book is marked unsynchronised and the caller
            must resnapshot.
        """
        result = payload.get("result") if "result" in payload else payload
        if not isinstance(result, dict):
            return []

        self._last_apply_was_snapshot = False

        # A ``full: true`` message is itself a complete snapshot of the
        # subscribed depth and resynchronises the book without a REST call.
        if result.get("full") is True:
            return self._apply_full(result)

        if not self._synced:
            self._buffer.append(result)
            self._updates_buffered += 1
            return []

        return self._apply_incremental(result)

    # -- internals ---------------------------------------------------------

    def _drain_buffer(self) -> list[BookChange]:
        """Replay buffered notifications onto a freshly applied snapshot.

        If the snapshot turns out to be older than the buffered notifications,
        the unconsumed remainder is put back so that a newer snapshot can still
        make use of it, and the sequence error is propagated.
        """
        if not self._buffer:
            return []
        buffered = list(self._buffer)
        self._buffer.clear()
        changes: list[BookChange] = []
        for index, result in enumerate(buffered):
            try:
                if result.get("full") is True:
                    # Unreachable through `apply_update`, which handles a full
                    # push before it ever buffers; kept so that a future caller
                    # feeding the buffer directly cannot skip the sequencing.
                    changes.extend(self._apply_full(result))
                    continue
                last_id = _int_or_none(result.get("u"))
                if last_id is not None and last_id <= self._last_update_id:
                    self._updates_dropped += 1
                    continue
                changes.extend(self._apply_incremental(result))
            except OrderBookSequenceError:
                self._buffer.extend(buffered[index:])
                raise
        return changes

    def _apply_full(self, result: dict[str, Any]) -> list[BookChange]:
        """Replace both sides wholesale, reporting the transition as changes.

        Raises
        ------
        OrderBookSequenceError
            If the message carries no update id. The depth is real, but without
            an id it cannot be placed in the stream: keeping the previous id
            would leave the book holding one state while claiming another, so
            every later notification is measured against the wrong expectation
            and is either discarded as old or reported as a gap — with the local
            book already wrong in between. A resnapshot is the only way back to a
            known position, and only the caller can order one.
        """
        last_id = _int_or_none(result.get("u"))
        if last_id is None:
            self._gaps_detected += 1
            self._synced = False
            raise OrderBookSequenceError(
                f"full order book push for {self.symbol!r} carries no update id 'u'; "
                f"the book cannot be placed in the stream and must be resnapshotted"
            )

        if self._synced and last_id <= self._last_update_id:
            # Gate.io documents that a full snapshot may be re-pushed at any
            # time. One that is not newer than the local state describes depth
            # the stream has already replaced; applying it would roll the book
            # backwards and, because callers republish on
            # `last_apply_was_snapshot`, send that superseded depth downstream as
            # a fresh snapshot. Same reasoning as `SnapshotStaleError` on the
            # REST path (step 7 of the module docstring), but the caller has
            # nothing to do about it, so it is counted rather than raised.
            self._snapshots_stale += 1
            return []

        bids = {price: size for price, size in _parse_levels(result.get("b")) if size > _ZERO}
        asks = {price: size for price, size in _parse_levels(result.get("a")) if size > _ZERO}

        changes: list[BookChange] = []
        for side, new, old in ((BID, bids, self._bids), (ASK, asks, self._asks)):
            for price in old:
                if price not in new:
                    changes.append((side, price, _ZERO))
            for price, size in new.items():
                if old.get(price) != size:
                    changes.append((side, price, size))

        self._bids = bids
        self._asks = asks
        self._last_update_id = last_id
        self._last_update_ms = _normalise_ms(result.get("t"))
        self._synced = True
        self._snapshots_applied += 1
        self._last_apply_was_snapshot = True
        return changes

    def _apply_incremental(self, result: dict[str, Any]) -> list[BookChange]:
        first_id = _int_or_none(result.get("U"))
        last_id = _int_or_none(result.get("u"))
        if first_id is None or last_id is None:
            # Without a range there is nothing to validate against; such a
            # message cannot be sequenced and is ignored rather than guessed at.
            self._updates_dropped += 1
            return []

        expected = self._last_update_id + 1

        if last_id < expected:
            # Entirely older than the local state.
            self._updates_dropped += 1
            return []

        if first_id > expected:
            self._gaps_detected += 1
            self._synced = False
            raise OrderBookSequenceError(
                f"order book gap for {self.symbol!r}: expected first update id {expected}, "
                f"received range {first_id}-{last_id} (missing {first_id - expected} updates)"
            )

        if first_id < expected:
            # Straddles the local update id: happens once per resynchronisation
            # and is safe because level sizes are absolute.
            self._overlaps_applied += 1

        changes = self._apply_levels(BID, result.get("b"))
        changes.extend(self._apply_levels(ASK, result.get("a")))

        # The id and timestamp advance even when both sides are empty, so that
        # the local state stays aligned with the venue's.
        self._last_update_id = last_id
        self._last_update_ms = _normalise_ms(result.get("t"))
        self._updates_applied += 1
        return changes

    def _apply_levels(self, side: str, levels: Iterable[Any] | None) -> list[BookChange]:
        book = self._bids if side == BID else self._asks
        changes: list[BookChange] = []
        for price, size in _parse_levels(levels):
            if size <= _ZERO:
                if book.pop(price, None) is not None:
                    changes.append((side, price, _ZERO))
                continue
            if book.get(price) != size:
                book[price] = size
                changes.append((side, price, size))
        return changes


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
