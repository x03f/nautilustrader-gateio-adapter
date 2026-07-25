"""Unit tests for the ``RateLimiter`` pacing and 429 backoff behavior.

``time.time``/``time.sleep`` are monkeypatched with a fake clock so the tests
are fully deterministic and never actually sleep.
"""

from __future__ import annotations

import pytest

import nautilus_gateio.http as http_module
from nautilus_gateio.http import RateLimiter


class FakeClock:
    """Deterministic replacement for ``time.time``/``time.sleep``.

    ``sleep`` records each requested delay and advances the clock by it,
    mimicking real wall-clock behavior without waiting.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(http_module.time, "time", fake.time)
    monkeypatch.setattr(http_module.time, "sleep", fake.sleep)
    return fake


class TestPacing:
    def test_first_call_is_immediate(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)
        limiter.wait()
        assert clock.sleeps == []

    def test_second_call_within_min_interval_sleeps(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)  # min_interval = 0.125s
        limiter.wait()
        limiter.wait()  # immediately after: clock has not advanced
        assert len(clock.sleeps) == 1
        assert clock.sleeps[0] == pytest.approx(0.125)

    def test_partial_elapsed_sleeps_the_remainder(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)
        limiter.wait()
        clock.advance(0.05)
        limiter.wait()
        assert clock.sleeps == [pytest.approx(0.075)]

    def test_no_sleep_after_min_interval_elapsed(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)
        limiter.wait()
        clock.advance(1.0)
        limiter.wait()
        assert clock.sleeps == []

    def test_min_interval_from_rate(self):
        assert RateLimiter(max_per_sec=4.0).min_interval == pytest.approx(0.25)
        assert RateLimiter(max_per_sec=10.0).min_interval == pytest.approx(0.1)


class TestBackoff:
    def _observed_backoff(self, limiter: RateLimiter, clock: FakeClock) -> float:
        """Measure the extra delay a call sleeps once pacing is satisfied."""
        clock.advance(60.0)  # far past min_interval: any sleep is pure backoff
        before = len(clock.sleeps)
        limiter.wait()
        new = clock.sleeps[before:]
        return sum(new)

    def test_on_429_grows_backoff_capped_at_10(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)
        limiter.wait()
        observed = []
        for _ in range(6):
            limiter.on_429()
            observed.append(self._observed_backoff(limiter, clock))
        assert observed == [
            pytest.approx(0.5),
            pytest.approx(1.5),
            pytest.approx(3.5),
            pytest.approx(7.5),
            pytest.approx(10.0),  # capped
            pytest.approx(10.0),  # stays capped
        ]

    def test_on_ok_halves_backoff(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)
        limiter.wait()
        for _ in range(5):
            limiter.on_429()  # grow to the 10.0 cap
        limiter.on_ok()
        assert self._observed_backoff(limiter, clock) == pytest.approx(5.0)
        limiter.on_ok()
        assert self._observed_backoff(limiter, clock) == pytest.approx(2.5)

    def test_on_ok_decays_toward_zero(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)
        limiter.wait()
        limiter.on_429()
        for _ in range(30):
            limiter.on_ok()
        assert self._observed_backoff(limiter, clock) < 1e-6

    def test_on_ok_without_backoff_is_noop(self, clock):
        limiter = RateLimiter(max_per_sec=8.0)
        limiter.wait()
        limiter.on_ok()
        assert self._observed_backoff(limiter, clock) == 0.0
        assert clock.sleeps == []
