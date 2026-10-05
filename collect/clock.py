"""Time, in one place, with one way to drive both the collector and the gate.

The gate takes ``clock: Callable[[], str]``; the collector's throttle needs real
``datetime`` arithmetic to compute how long is left on a host delay. ``FrozenClock``
satisfies both, and ``as_gate_clock()`` hands the gate a bound string clock over the *same
moment*, so one ``advance()`` moves collection, retention and permit expiry together.

That matters for testing the interaction rather than each half: the interesting failure is
a permit that expires *between* being issued and being used, which needs the clock to move
for the gate and the collector simultaneously. Two separate clocks could not express it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    """A clock that only moves when told to."""

    _moment: datetime = field(
        default_factory=lambda: datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    )

    @classmethod
    def at(cls, iso: str) -> "FrozenClock":
        stamp = datetime.fromisoformat(iso)
        return cls(_moment=stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc))

    def now(self) -> datetime:
        return self._moment

    def iso(self) -> str:
        return self._moment.isoformat(timespec="seconds")

    def advance(self, *, seconds: float = 0.0, minutes: float = 0.0, days: float = 0.0) -> datetime:
        if seconds < 0 or minutes < 0 or days < 0:
            raise ValueError("this clock does not run backwards; retention is a deadline")
        self._moment += timedelta(seconds=seconds, minutes=minutes, days=days)
        return self._moment

    def as_gate_clock(self) -> Callable[[], str]:
        """The same moment, in the form ``CollectionGate`` and ``AuditLog`` accept."""
        return self.iso


@dataclass(frozen=True)
class SystemClock:
    """The real clock, wrapped so it satisfies the same protocol as ``FrozenClock``.

    The CLI uses this rather than a frozen moment, because "when did we look" has to be
    when we looked: a capture stamped with a fixture's epoch would make a report claim a
    collection date that never happened. Tests use ``FrozenClock`` instead, since a real
    clock makes retention and backoff untestable.
    """

    def now(self) -> datetime:
        return utc_now()

    def iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def as_gate_clock(self) -> Callable[[], str]:
        return self.iso
