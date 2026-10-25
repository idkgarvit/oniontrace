"""Resumable checkpoints, stored where the purge will reach them.

A checkpoint records *where a walk got to*, so an interrupted run resumes instead of
restarting. It is case data: it reveals which channels are being watched and how far
back, which is exactly the kind of thing retention is supposed to end.

So it lives under the case's ``derived/`` directory, which is inside the scope the gate's
purge already deletes. That placement is the whole point -- a checkpoint that survived a
purge would leave a residue describing collection that no longer exists. Nothing in
``collect/`` writes outside a case directory, and there is a test for that.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import CheckpointError


@dataclass
class Checkpoint:
    """Progress of one backward walk through one source."""

    case_id: str
    source: str
    cursor: str | None = None
    pages_fetched: int = 0
    completed: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Checkpoint":
        if payload.get("case_id") and payload["case_id"] != payload.get("_expected_case"):
            raise CheckpointError(
                f"checkpoint claims case {payload['case_id']!r} but was read from "
                f"{payload.get('_expected_case')!r}; checkpoints are not portable between cases"
            )
        return cls(
            case_id=str(payload.get("case_id", "")),
            source=str(payload.get("source", "")),
            cursor=payload.get("cursor"),
            pages_fetched=int(payload.get("pages_fetched", 0)),
            completed=bool(payload.get("completed", False)),
            updated_at=str(payload.get("updated_at", "")),
        )


class CheckpointStore:
    """Per-case, per-source cursor storage inside the case's derived directory."""

    def __init__(self, case_dir: Path, *, clock) -> None:
        self._dir = Path(case_dir)
        self._clock = clock

    @property
    def root(self) -> Path:
        return self._dir

    def _path(self, source: str) -> Path:
        if not source or "/" in source or source in {".", ".."}:
            raise CheckpointError(f"invalid checkpoint source {source!r}")
        return self._dir / "checkpoints" / f"{source}.json"

    def load(self, case_id: str, source: str) -> Checkpoint | None:
        path = self._path(source)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["_expected_case"] = case_id
        checkpoint = Checkpoint.from_dict(payload)
        if checkpoint.case_id != case_id:
            raise CheckpointError(
                f"checkpoint for {source!r} belongs to {checkpoint.case_id!r}, not {case_id!r}"
            )
        return checkpoint

    def save(self, checkpoint: Checkpoint) -> Checkpoint:
        path = self._path(checkpoint.source)
        path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.updated_at = self._clock.now().isoformat()
        path.write_text(
            json.dumps(checkpoint.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return checkpoint

    def sources(self) -> tuple[str, ...]:
        folder = self._dir / "checkpoints"
        if not folder.exists():
            return ()
        return tuple(sorted(p.stem for p in folder.glob("*.json")))


class ProposalStore:
    """Seed *proposals* the collector found, waiting on an analyst's decision.

    Stored beside the checkpoints for the same reason: a proposal names an address or
    channel the case was interested in, so it is case data and must die with the case.

    The interface is the discipline. There is no method here that turns a proposal into a
    seed, and there must not be one: E2.2 puts that behind ``CaseStore.add_seed`` and an
    analyst's permission. A store with an ``accept()`` on it is one refactor away from a
    crawler that promotes its own findings, and the audit trail would show collection
    with no decision in front of it.
    """

    def __init__(self, case_dir: Path, *, clock) -> None:
        self._dir = Path(case_dir)
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._dir / "proposals.json"

    def pending(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return tuple(payload.get("proposals", ()))

    def record(self, case_id: str, proposals: Iterable[dict[str, Any]]) -> int:
        """Merge proposals by seed, keeping the first discovery note. Return new count.

        Idempotent by seed: a crawler that finds the same address in three runs must not
        produce three identical review items, or the queue an analyst works through stops
        being a list of things they have not decided yet.
        """
        known = {str(item.get("seed")) for item in self.pending()}
        merged = list(self.pending())
        added = 0
        for item in proposals:
            seed = str(item.get("seed", ""))
            if not seed or seed in known:
                continue
            known.add(seed)
            merged.append(
                {
                    **item,
                    "case_id": case_id,
                    "proposed_at": self._clock.now().isoformat(),
                }
            )
            added += 1
        if added:
            self._dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"case_id": case_id, "proposals": merged}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return added

