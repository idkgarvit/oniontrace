"""Cases, per-case seeds, retention and purge (Part E2.2).

The rule this module exists to enforce is **no case, no collection**. A case carries four
things that make a collection run accountable after the fact:

  * an **owning analyst** -- a person, not a team;
  * an **authorisation reference** -- the document that makes this lawful, which is
    required and cannot be blank, because "we had authority" is not a reference;
  * **per-case seeds** -- there is no global crawl list anywhere in this package, so a
    run cannot inherit targets from a previous investigation;
  * a **retention expiry** -- set at creation, so retention is a decision made before the
    data exists rather than a negotiation made after.

Purge deletes snapshots and derived data and leaves a tombstone carrying the
authorisation record, because deleting the evidence that authority existed would make the
deletion unaccountable. The audit log is in a separate tree and is untouched by a purge.
"""

from __future__ import annotations

from . import permissions

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gates.errors import CaseError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class LinkDecision:
    """An analyst's recorded action on one candidate link.

    Persisted with the case rather than held in memory, so two gate instances cannot
    disagree about whether a link was confirmed, and a restart does not silently un-decide
    something a person decided.
    """

    action: str  # "confirmed" | "rejected"
    decided_by: str
    recorded_at: str
    reason: str | None = None
    audit_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Case:
    """A named investigation with an owner, an authorisation reference and an expiry."""
    case_id: str
    title: str
    owner: str
    authorisation_ref: str
    opened_at: str
    retention_until: str
    sealed: bool = False
    sealed_at: str | None = None
    purged_at: str | None = None
    #: How many snapshots the purge removed. Carried on the tombstone so "we deleted it"
    #: is a number in the record rather than an unverifiable claim.
    purged_snapshots: int | None = None
    seeds: list[str] = field(default_factory=list)

    @property
    def purged(self) -> bool:
        return self.purged_at is not None

    def is_expired(self, now: str) -> bool:
        """Retention is a deadline, not a suggestion: at the expiry instant it is over."""
        return _parse(now) >= _parse(self.retention_until)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CaseStore:
    """Case-scoped storage: `<root>/cases/<case_id>/`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.cases_root = self.root / "cases"
        permissions.secure_dir(self.cases_root)

    def _dir(self, case_id: str) -> Path:
        if not case_id or "/" in case_id or case_id in {".", ".."}:
            raise CaseError("E2.2", f"invalid case id {case_id!r}")
        return self.cases_root / case_id

    def _record(self, case_id: str) -> Path:
        return self._dir(case_id) / "case.json"

    def snapshots_dir(self, case_id: str) -> Path:
        return self._dir(case_id) / "snapshots"

    def derived_dir(self, case_id: str) -> Path:
        """Where data *computed from* the snapshots lives.

        Public because it is inside the scope ``purge`` deletes, so anything a module
        derives from case content belongs here or it outlives the retention decision.
        Acquisition puts its resumable checkpoints here for exactly that reason.
        """
        return self._dir(case_id) / "derived"

    def read_snapshots(self, case_id: str) -> tuple[dict[str, Any], ...]:
        """Every snapshot currently held for the case, in stored-name order.

        Read-only, and the only way to see collected bytes after they are written --
        which is what lets the collector rebuild its notion of what it already holds
        from the record rather than from a counter it keeps for itself.
        """
        directory = self.snapshots_dir(case_id)
        if not directory.exists():
            return ()
        payloads: list[dict[str, Any]] = []
        for name in self.snapshot_names(case_id):
            payloads.append(json.loads((directory / name).read_text(encoding="utf-8")))
        return tuple(payloads)

    # -- lifecycle --------------------------------------------------------- #

    def exists(self, case_id: str) -> bool:
        return self._record(case_id).exists()

    def create(
        self,
        *,
        case_id: str,
        title: str,
        owner: str,
        authorisation_ref: str,
        retention_days: int,
        now: str | None = None,
    ) -> Case:
        """Open a case. Refuses a case that could not be accounted for later."""
        if self.exists(case_id):
            raise CaseError("E2.2", f"case {case_id!r} already exists")
        if not owner.strip():
            raise CaseError("E2.2", "a case needs a named owning analyst")
        if not authorisation_ref.strip():
            raise CaseError(
                "E2.2",
                "a case needs a recorded authorisation reference; collection without one "
                "is the thing this gate exists to prevent",
            )
        if retention_days <= 0:
            raise CaseError(
                "E2.2",
                f"retention must be a positive number of days, got {retention_days}; an "
                "unlimited case is a case nobody has decided to end",
            )

        opened = now or utc_now()
        case = Case(
            case_id=case_id,
            title=title.strip(),
            owner=owner.strip(),
            authorisation_ref=authorisation_ref.strip(),
            opened_at=opened,
            retention_until=(
                _parse(opened) + timedelta(days=retention_days)
            ).isoformat(timespec="seconds"),
        )
        self._write(case)
        return case

    def get(self, case_id: str) -> Case:
        path = self._record(case_id)
        if not path.exists():
            raise CaseError("E2.2", f"no such case {case_id!r}")
        return Case(**json.loads(path.read_text(encoding="utf-8")))

    def _write(self, case: Case) -> None:
        permissions.secure_dir(self._dir(case.case_id))
        permissions.secure_write_text(
            self._record(case.case_id), json.dumps(case.to_dict(), indent=2, sort_keys=True)
        )

    def seal(self, case_id: str, now: str | None = None) -> Case:
        case = self.get(case_id)
        if case.purged:
            raise CaseError("E2.2", f"case {case_id!r} is purged")
        case.sealed = True
        case.sealed_at = now or utc_now()
        self._write(case)
        return case

    # -- seeds ------------------------------------------------------------- #

    def add_seed(self, case_id: str, seed: str) -> Case:
        """Add a seed to one case. Seeds are never global, so nothing else can see it."""
        case = self.require_collectable(case_id)
        cleaned = seed.strip()
        if not cleaned:
            raise CaseError("E2.2", "an empty seed is not a seed")
        if cleaned not in case.seeds:
            case.seeds.append(cleaned)
            self._write(case)
        return case

    def require_collectable(self, case_id: str) -> Case:
        """Fetch a case that may still receive data, or refuse with the reason."""
        case = self.get(case_id)
        if case.purged:
            raise CaseError(
                "E2.2", f"case {case_id!r} was purged at {case.purged_at}"
            )
        if case.sealed:
            raise CaseError(
                "E2.2",
                f"case {case_id!r} was sealed at {case.sealed_at}; sealed cases do not "
                "accept further collection",
            )
        return case

    # -- snapshots --------------------------------------------------------- #

    def store_snapshot(self, case_id: str, payload: dict[str, Any]) -> str:
        """Persist one captured snapshot under the case, returning its name."""
        self.require_collectable(case_id)
        directory = self.snapshots_dir(case_id)
        permissions.secure_dir(directory)
        name = f"snapshot-{len(list(directory.glob('snapshot-*.json'))):04d}.json"
        # A snapshot holds a target's page. It is the last file in this tree that should be
        # readable by whoever happens to be logged in.
        permissions.secure_write_text(
            directory / name, json.dumps(payload, indent=2, sort_keys=True, default=str)
        )
        return name

    def snapshot_names(self, case_id: str) -> tuple[str, ...]:
        directory = self.snapshots_dir(case_id)
        if not directory.exists():
            return ()
        return tuple(sorted(p.name for p in directory.glob("snapshot-*.json")))

    # -- analyst decisions on candidate links ------------------------------ #

    def _decisions_path(self, case_id: str) -> Path:
        return self._dir(case_id) / "decisions.json"

    @staticmethod
    def link_key(left: str, right: str) -> str:
        """Order-independent key for a pair, so a link and its reverse share a decision."""
        return "|".join(sorted((left, right)))

    def decisions(self, case_id: str) -> dict[str, LinkDecision]:
        path = self._decisions_path(case_id)
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {key: LinkDecision(**value) for key, value in raw.items()}

    def decision_for(
        self, case_id: str, left: str, right: str
    ) -> LinkDecision | None:
        return self.decisions(case_id).get(self.link_key(left, right))

    def record_decision(
        self, case_id: str, left: str, right: str, decision: LinkDecision
    ) -> None:
        self.require_collectable(case_id)
        current = self.decisions(case_id)
        current[self.link_key(left, right)] = decision
        permissions.secure_write_text(
            self._decisions_path(case_id),
            json.dumps(
                {key: value.to_dict() for key, value in current.items()},
                indent=2,
                sort_keys=True,
            ),
        )

    def clear_decision(self, case_id: str, left: str, right: str) -> LinkDecision | None:
        """Remove a decision, returning what was removed.

        This is what makes a merge reversible: the decision is deleted and the link
        returns to the unconfirmed state it had before an analyst touched it. The record
        of the decision itself survives in the audit trail, which is where it belongs.
        """
        current = self.decisions(case_id)
        removed = current.pop(self.link_key(left, right), None)
        if removed is None:
            return None
        permissions.secure_write_text(
            self._decisions_path(case_id),
            json.dumps(
                {key: value.to_dict() for key, value in current.items()},
                indent=2,
                sort_keys=True,
            ),
        )
        return removed

    # -- retention and purge ----------------------------------------------- #

    def all(self) -> tuple[Case, ...]:
        return tuple(
            Case(**json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self.cases_root.glob("*/case.json"))
        )

    def expired(self, now: str | None = None) -> tuple[str, ...]:
        """Case ids whose retention has run out and which are not yet purged."""
        moment = now or utc_now()
        return tuple(
            sorted(c.case_id for c in self.all() if not c.purged and c.is_expired(moment))
        )

    def purge(self, case_id: str, now: str | None = None) -> Case:
        """Delete snapshots and derived data; keep the tombstone.

        The tombstone is not a leftover. Once the bytes are gone the only remaining
        evidence that this collection was *authorised* is the case record, so the record
        is what makes the deletion reviewable instead of merely final.
        """
        case = self.get(case_id)
        if case.purged:
            return case

        directory = self.snapshots_dir(case_id)
        removed = len(self.snapshot_names(case_id))
        if directory.exists():
            shutil.rmtree(directory)
        # Decisions are derived data about case content, so they go with it. The record
        # that a human made them survives in the audit trail, which the purge cannot
        # reach -- content deleted, actions still accountable.
        decisions = self._decisions_path(case_id)
        if decisions.exists():
            decisions.unlink()
        for other in (self.derived_dir(case_id),):
            if other.exists():
                shutil.rmtree(other)

        case.purged_at = now or utc_now()
        case.purged_snapshots = removed
        case.seeds = []
        self._write(case)
        return case
