"""Politeness, budgets and revisit scheduling.

Three separate limits, because they fail in different ways:

* **Per-host delay** protects the operator of the thing being fetched. It is the one
  limit a collector can quietly violate, so it is enforced in one place that every fetch
  must pass through, and it asks a ``Sleeper`` to wait rather than sleeping itself --
  which means a test can read back exactly how long each request waited, and a run that
  hammered a host would fail a test instead of merely being rude.
* **Run budgets** protect the collection operator from their own typo: a cursor bug that
  pages forever stops at the page or byte ceiling instead of filling the disk.
* **Revisit backoff** keeps a monitored set of services from being polled at a constant
  rate. Unchanged services back off geometrically to a ceiling and reset on change, so
  the collection rate tracks observed activity rather than wall-clock time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .errors import SchedulingError


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> float:
        """Wait, and report how long was actually waited."""


class RecordingSleeper:
    """Default sleeper: records requested waits and performs none.

    Used by the fixture path so tests run at full speed while still asserting the
    politeness arithmetic. The live path swaps in ``ClockSleeper``.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []

    def sleep(self, seconds: float) -> float:
        waited = max(0.0, float(seconds))
        self.waits.append(waited)
        return waited


class ClockSleeper:
    """Real sleeping, for the live transport only."""

    def sleep(self, seconds: float) -> float:
        import time  # stdlib; not a network or process capability

        waited = max(0.0, float(seconds))
        time.sleep(waited)
        return waited


@dataclass(frozen=True)
class Politeness:
    """The limits a single run may not cross."""

    min_host_delay_seconds: float = 1.0
    max_pages_per_source: int = 50
    max_bytes_per_run: int = 8 * 1024 * 1024

    def validated(self) -> "Politeness":
        if self.min_host_delay_seconds < 0:
            raise SchedulingError("host delay cannot be negative")
        if self.max_pages_per_source < 1:
            raise SchedulingError("a run must be allowed at least one page")
        return self


class Budget:
    """Page and byte ceilings for one run."""

    def __init__(self, politeness: Politeness, *, label: str = "run") -> None:
        self.politeness = politeness.validated()
        self.label = label
        self.pages = 0
        self.bytes = 0

    def spend(self, *, byte_length: int) -> None:
        self.pages += 1
        self.bytes += max(0, int(byte_length))
        if self.pages > self.politeness.max_pages_per_source:
            raise SchedulingError(
                f"{self.label}: page budget of "
                f"{self.politeness.max_pages_per_source} exhausted after {self.pages} pages. "
                "A cursor that never terminates looks exactly like this."
            )
        if self.bytes > self.politeness.max_bytes_per_run:
            raise SchedulingError(
                f"{self.label}: byte budget of "
                f"{self.politeness.max_bytes_per_run} exhausted after {self.bytes} bytes"
            )

    @property
    def exhausted(self) -> bool:
        """True once no further page may be requested.

        Both ceilings count. A byte ceiling that only stopped *recording* while the
        collector kept requesting pages would make the limit a filing rule instead of a
        politeness rule, and the requests it allowed after the limit are exactly the ones
        the limit exists to prevent.
        """
        return (
            self.pages >= self.politeness.max_pages_per_source
            or self.bytes >= self.politeness.max_bytes_per_run
        )


class HostThrottle:
    """Minimum interval between requests to the same host.

    Every fetch in this package goes through :meth:`gate`, so there is no route to a
    network that skips the delay -- the delay is not a courtesy the caller may opt out of.
    """

    def __init__(self, politeness: Politeness, *, clock, sleeper: Sleeper | None = None):
        self.politeness = politeness.validated()
        self._clock = clock
        self._sleeper = sleeper or RecordingSleeper()
        self._last: dict[str, datetime] = {}

    @property
    def sleeper(self) -> Sleeper:
        return self._sleeper

    def delay_for(self, host: str) -> float:
        last = self._last.get(host)
        if last is None:
            return 0.0
        elapsed = (self._clock.now() - last).total_seconds()
        return max(0.0, self.politeness.min_host_delay_seconds - elapsed)

    def gate(self, host: str) -> float:
        """Wait out the remaining interval for ``host``; return seconds waited."""
        waited = self._sleeper.sleep(self.delay_for(host))
        self._last[host] = self._clock.now()
        return waited


@dataclass
class VisitPlan:
    """When a monitored service is next worth revisiting."""

    target: str
    next_due: str
    interval_seconds: int
    unchanged_streak: int = 0

    def is_due(self, now: datetime) -> bool:
        return parse_instant(self.next_due) <= now


class RevisitPlanner:
    """Geometric backoff while content is unchanged, reset the moment it changes."""

    def __init__(
        self,
        *,
        min_interval_seconds: int = 3600,
        max_interval_seconds: int = 7 * 24 * 3600,
        factor: int = 2,
    ) -> None:
        if min_interval_seconds < 1 or max_interval_seconds < min_interval_seconds:
            raise SchedulingError("revisit interval bounds are inverted or empty")
        if factor < 2:
            raise SchedulingError("backoff factor must at least double")
        self.min_interval = min_interval_seconds
        self.max_interval = max_interval_seconds
        self.factor = factor

    def plan(
        self,
        *,
        target: str,
        changed: bool,
        previous: VisitPlan | None,
        now: datetime,
    ) -> VisitPlan:
        if changed or previous is None:
            interval, streak = self.min_interval, 0
        else:
            streak = previous.unchanged_streak + 1
            interval = min(self.max_interval, self.min_interval * (self.factor**streak))
        return VisitPlan(
            target=target,
            next_due=(now + timedelta(seconds=interval)).isoformat(),
            interval_seconds=interval,
            unchanged_streak=streak,
        )

    @staticmethod
    def due(plans: list[VisitPlan], now: datetime) -> list[VisitPlan]:
        """Targets worth revisiting, soonest-due first.

        Sorted explicitly: relying on insertion order here would let the revisit order
        depend on how a dictionary happened to iterate, which would make a run
        irreproducible from its seed.
        """
        overdue = [plan for plan in plans if plan.is_due(now)]
        return sorted(overdue, key=lambda p: (p.next_due, p.target))


def parse_instant(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

