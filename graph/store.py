"""SQLite-backed identity graph store (M3).

SQLite rather than Neo4j on purpose. The committed baseline runs on a 16 GB laptop,
the graph for a single case is thousands of nodes at most, and a store that opens
with the standard library is one a marker can run. The trade-off is real and it is
named in the report: this is a single-writer store, not a multi-analyst graph
database.

**Nothing in this module merges on its own.** ``add_edge`` writes a proposal.
``merge`` is the only operation that moves a node, it demands an actor and a
rationale, and it writes a complete before-state so ``reverse_merge`` can restore
the pair exactly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from graph.model import (
    Artefact,
    Edge,
    EdgeStatus,
    Entity,
    Evidence,
    GraphError,
    MergeNotReversible,
    MergeRecord,
    NoSuchEntity,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS entity (
    entity_id  TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    case_id    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    merged_into TEXT
);

CREATE TABLE IF NOT EXISTS artefact (
    artefact_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    value           TEXT NOT NULL,
    service_id      TEXT NOT NULL,
    path            TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    observed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artefact_value ON artefact(kind, value);

CREATE TABLE IF NOT EXISTS entity_service (
    entity_id  TEXT NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    service_id TEXT NOT NULL,
    PRIMARY KEY (entity_id, service_id)
);

CREATE TABLE IF NOT EXISTS edge (
    edge_id     TEXT PRIMARY KEY,
    a_entity    TEXT NOT NULL,
    b_entity    TEXT NOT NULL,
    method      TEXT NOT NULL,
    score       REAL NOT NULL,
    status      TEXT NOT NULL,
    supporting  TEXT NOT NULL,
    counter     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (a_entity, b_entity)
);

CREATE TABLE IF NOT EXISTS merge_log (
    merge_id     TEXT PRIMARY KEY,
    survivor     TEXT NOT NULL,
    absorbed     TEXT NOT NULL,
    actor        TEXT NOT NULL,
    rationale    TEXT NOT NULL,
    before_state TEXT NOT NULL,
    after_state  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    reversed_at  TEXT,
    reversed_by  TEXT
);

CREATE TABLE IF NOT EXISTS finding (
    finding_id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    evidence   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS finding_service ON finding(service_id);

CREATE TABLE IF NOT EXISTS note (
    note_id    TEXT PRIMARY KEY,
    entity_id  TEXT,
    case_id    TEXT NOT NULL,
    actor      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(a: str, b: str) -> tuple[str, str]:
    """Order a pair so (a,b) and (b,a) are the same edge and cannot be stored twice."""

    return (a, b) if a <= b else (b, a)


def _digest(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class GraphStore:
    """One case's identity graph on disk."""

    def __init__(self, path: str | Path, *, case_id: str) -> None:
        self.path = Path(path)
        self.case_id = case_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- lifecycle ------------------------------------------------------- #
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- entities -------------------------------------------------------- #
    def add_entity(self, entity_id: str, label: str, *, created_at: str | None = None) -> Entity:
        row = Entity(
            entity_id=entity_id,
            label=label,
            case_id=self.case_id,
            status="candidate",
            created_at=created_at or utc_now(),
        )
        self._conn.execute(
            "INSERT INTO entity (entity_id, label, case_id, status, created_at)"
            " VALUES (?,?,?,?,?)",
            (row.entity_id, row.label, row.case_id, row.status, row.created_at),
        )
        self._conn.commit()
        return row

    def entity(self, entity_id: str) -> Entity:
        r = self._conn.execute(
            "SELECT * FROM entity WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if r is None:
            raise NoSuchEntity(entity_id)
        services = [
            x["service_id"]
            for x in self._conn.execute(
                "SELECT service_id FROM entity_service WHERE entity_id = ?"
                " ORDER BY service_id",
                (entity_id,),
            )
        ]
        return Entity(
            entity_id=r["entity_id"],
            label=r["label"],
            case_id=r["case_id"],
            status=r["status"],
            service_ids=services,
            created_at=r["created_at"],
            merged_into=r["merged_into"],
        )

    def entities(self, *, include_merged: bool = False) -> list[Entity]:
        if include_merged:
            rows = self._conn.execute(
                "SELECT entity_id FROM entity WHERE case_id = ? ORDER BY entity_id",
                (self.case_id,),
            )
        else:
            rows = self._conn.execute(
                "SELECT entity_id FROM entity WHERE case_id = ? AND merged_into IS NULL"
                " ORDER BY entity_id",
                (self.case_id,),
            )
        return [self.entity(r["entity_id"]) for r in rows]

    def attach_service(self, entity_id: str, service_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO entity_service (entity_id, service_id) VALUES (?,?)",
            (entity_id, service_id),
        )
        self._conn.commit()

    # -- artefacts ------------------------------------------------------- #
    def add_artefact(self, art: Artefact) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO artefact"
            " (artefact_id, kind, value, service_id, path, evidence_sha256, observed_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                art.artefact_id,
                art.kind,
                art.value,
                art.service_id,
                art.path,
                art.evidence_sha256,
                art.observed_at,
            ),
        )
        self._conn.commit()

    def artefacts(self, *, service_id: str | None = None) -> list[Artefact]:
        sql = "SELECT * FROM artefact"
        args: tuple[Any, ...] = ()
        if service_id:
            sql += " WHERE service_id = ?"
            args = (service_id,)
        sql += " ORDER BY service_id, kind"
        return [
            Artefact(
                artefact_id=r["artefact_id"],
                kind=r["kind"],
                value=r["value"],
                service_id=r["service_id"],
                path=r["path"],
                evidence_sha256=r["evidence_sha256"],
                observed_at=r["observed_at"],
            )
            for r in self._conn.execute(sql, args)
        ]

    # -- edges ----------------------------------------------------------- #
    def add_edge(self, edge: Edge) -> Edge:
        a, b = _canonical(edge.a_entity, edge.b_entity)
        self._conn.execute(
            "INSERT OR REPLACE INTO edge"
            " (edge_id, a_entity, b_entity, method, score, status, supporting, counter, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                edge.edge_id,
                a,
                b,
                edge.method,
                edge.score,
                edge.status,
                json.dumps([e.to_dict() for e in edge.supporting], sort_keys=True),
                json.dumps([e.to_dict() for e in edge.counter], sort_keys=True),
                edge.created_at or utc_now(),
            ),
        )
        self._conn.commit()
        edge.a_entity, edge.b_entity = a, b
        return edge

    def edges(self, *, status: str | None = None) -> list[Edge]:
        sql = "SELECT * FROM edge"
        args: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY score DESC, edge_id"
        out: list[Edge] = []
        for r in self._conn.execute(sql, args):
            out.append(
                Edge(
                    edge_id=r["edge_id"],
                    a_entity=r["a_entity"],
                    b_entity=r["b_entity"],
                    method=r["method"],
                    score=r["score"],
                    status=r["status"],
                    supporting=[Evidence(**e) for e in json.loads(r["supporting"])],
                    counter=[Evidence(**e) for e in json.loads(r["counter"])],
                    created_at=r["created_at"],
                )
            )
        return out

    def set_edge_status(self, edge_id: str, status: str, *, actor: str) -> None:
        if status not in {s.value for s in EdgeStatus}:
            raise GraphError(f"unknown edge status: {status}")
        cur = self._conn.execute(
            "UPDATE edge SET status = ? WHERE edge_id = ?", (status, edge_id)
        )
        if cur.rowcount == 0:
            raise GraphError(f"no such edge: {edge_id}")
        self._conn.commit()
        self._note(f"edge {edge_id} -> {status} by {actor}", actor=actor)

    # -- merges (the only operation that moves a node) -------------------- #
    def merge(self, survivor: str, absorbed: str, *, actor: str, rationale: str) -> MergeRecord:
        """Combine two entities into one, reversibly.

        Refuses when the pair is not linked by an edge, because a merge with no
        recorded evidence is exactly the assertion this system exists not to make.
        """

        if not actor.strip():
            raise GraphError("a merge requires a named actor")
        if not rationale.strip():
            raise GraphError("a merge requires a rationale")
        if survivor == absorbed:
            raise GraphError("cannot merge an entity into itself")

        # Both sides must still be live. Without this, merging the same pair twice wrote
        # a second merge record that changed nothing: the log then claimed two decisions
        # where an analyst made one, and reversing either record restored a state the
        # other record had already superseded.
        sur = self.entity(survivor)
        abs_ = self.entity(absorbed)
        for ent in (sur, abs_):
            if ent.merged_into:
                raise GraphError(
                    f"{ent.entity_id} is already merged into {ent.merged_into}; "
                    "reverse that merge if the decision was wrong"
                )

        a, b = _canonical(survivor, absorbed)
        edge = self._conn.execute(
            "SELECT edge_id FROM edge WHERE a_entity = ? AND b_entity = ?", (a, b)
        ).fetchone()
        if edge is None:
            raise GraphError(
                "no evidence links these entities; propose an edge before merging"
            )

        before = {
            "survivor": sur.to_dict(),
            "absorbed": abs_.to_dict(),
            "edge_status": self._conn.execute(
                "SELECT status FROM edge WHERE edge_id = ?", (edge["edge_id"],)
            ).fetchone()["status"],
        }

        self._conn.execute(
            "UPDATE entity_service SET entity_id = ? WHERE entity_id = ?",
            (survivor, absorbed),
        )
        self._conn.execute(
            "UPDATE entity SET merged_into = ?, status = 'merged' WHERE entity_id = ?",
            (survivor, absorbed),
        )
        self._conn.execute(
            "UPDATE edge SET status = ? WHERE edge_id = ?",
            (EdgeStatus.CONFIRMED.value, edge["edge_id"]),
        )

        after = {
            "survivor": self.entity(survivor).to_dict(),
            "absorbed": self.entity(absorbed).to_dict(),
            "edge_status": EdgeStatus.CONFIRMED.value,
        }
        # The id must be unique per *record*, not per pair-and-second. It was a digest of
        # (survivor, absorbed, now) at second resolution, and `reverse_merge` keeps the record it
        # reverses -- deliberately, because "we merged these and changed our mind" is evidence.
        # So merge, undo, merge again inside one second collided on the primary key and the second
        # merge died with an IntegrityError instead of working. That is not a rare sequence: it is
        # what an analyst does when they are unsure, and it is what this project's own console
        # walkthrough does. Counting the pair's prior merges keeps the id stable for a given state
        # and distinct for every actual decision.
        prior_merges = self._conn.execute(
            "SELECT COUNT(*) AS n FROM merge_log WHERE survivor = ? AND absorbed = ?",
            (survivor, absorbed),
        ).fetchone()["n"]
        rec = MergeRecord(
            merge_id=f"mrg-{_digest([survivor, absorbed, utc_now(), str(prior_merges)])[:16]}",
            survivor=survivor,
            absorbed=absorbed,
            actor=actor,
            rationale=rationale,
            before_state=before,
            after_state=after,
            created_at=utc_now(),
        )
        self._conn.execute(
            "INSERT INTO merge_log (merge_id, survivor, absorbed, actor, rationale,"
            " before_state, after_state, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                rec.merge_id,
                rec.survivor,
                rec.absorbed,
                rec.actor,
                rec.rationale,
                json.dumps(rec.before_state, sort_keys=True),
                json.dumps(rec.after_state, sort_keys=True),
                rec.created_at,
            ),
        )
        self._conn.commit()
        return rec

    def reverse_merge(self, merge_id: str, *, actor: str, reason: str = "") -> None:
        """Undo a merge by restoring the recorded before-state exactly."""

        r = self._conn.execute(
            "SELECT * FROM merge_log WHERE merge_id = ?", (merge_id,)
        ).fetchone()
        if r is None:
            raise GraphError(f"no such merge: {merge_id}")
        if r["reversed_at"]:
            raise MergeNotReversible(f"{merge_id} was already reversed")

        before = json.loads(r["before_state"])
        survivor = before["survivor"]["entity_id"]
        absorbed = before["absorbed"]["entity_id"]

        # Restore the absorbed entity and hand back exactly the services the
        # before-state says it had, rather than the ones it has now.
        self._conn.execute(
            "UPDATE entity SET merged_into = NULL, status = ? WHERE entity_id = ?",
            (before["absorbed"]["status"], absorbed),
        )
        self._conn.execute(
            "DELETE FROM entity_service WHERE entity_id IN (?,?)", (survivor, absorbed)
        )
        for sid in before["survivor"]["service_ids"]:
            self._conn.execute(
                "INSERT INTO entity_service (entity_id, service_id) VALUES (?,?)",
                (survivor, sid),
            )
        for sid in before["absorbed"]["service_ids"]:
            self._conn.execute(
                "INSERT INTO entity_service (entity_id, service_id) VALUES (?,?)",
                (absorbed, sid),
            )

        a, b = _canonical(survivor, absorbed)
        self._conn.execute(
            "UPDATE edge SET status = ? WHERE a_entity = ? AND b_entity = ?",
            (before["edge_status"], a, b),
        )
        self._conn.execute(
            "UPDATE merge_log SET reversed_at = ?, reversed_by = ? WHERE merge_id = ?",
            (utc_now(), actor, merge_id),
        )
        self._conn.commit()
        self._note(f"merge {merge_id} reversed by {actor}: {reason}".strip(), actor=actor)

    def merge_log(self) -> list[MergeRecord]:
        out: list[MergeRecord] = []
        for r in self._conn.execute("SELECT * FROM merge_log ORDER BY created_at"):
            out.append(
                MergeRecord(
                    merge_id=r["merge_id"],
                    survivor=r["survivor"],
                    absorbed=r["absorbed"],
                    actor=r["actor"],
                    rationale=r["rationale"],
                    before_state=json.loads(r["before_state"]),
                    after_state=json.loads(r["after_state"]),
                    created_at=r["created_at"],
                    reversed_at=r["reversed_at"],
                    reversed_by=r["reversed_by"],
                )
            )
        return out

    # -- findings -------------------------------------------------------- #
    def add_finding(
        self, *, service_id: str, kind: str, path: str, evidence: str
    ) -> None:
        """Record one M2 detection against a service.

        Findings live next to the graph rather than in a separate store because an
        analyst reading a link needs to see what was found on the services behind it.
        Two stores would drift the first time only one of them was updated.
        """

        fid = f"fnd-{_digest([service_id, kind, path])[:16]}"
        self._conn.execute(
            "INSERT OR REPLACE INTO finding"
            " (finding_id, service_id, kind, path, evidence, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (fid, service_id, kind, path, evidence, utc_now()),
        )
        self._conn.commit()

    def findings(self, *, service_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM finding"
        args: tuple[Any, ...] = ()
        if service_id:
            sql += " WHERE service_id = ?"
            args = (service_id,)
        sql += " ORDER BY service_id, kind"
        return [dict(r) for r in self._conn.execute(sql, args)]

    # -- notes ----------------------------------------------------------- #
    def _note(self, body: str, *, actor: str, entity_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO note (note_id, entity_id, case_id, actor, body, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                f"note-{_digest([body, utc_now()])[:16]}",
                entity_id,
                self.case_id,
                actor,
                body,
                utc_now(),
            ),
        )
        self._conn.commit()

    def add_note(self, body: str, *, actor: str, entity_id: str | None = None) -> None:
        if entity_id is not None:
            self.entity(entity_id)  # raises if unknown
        self._note(body, actor=actor, entity_id=entity_id)

    def notes(self, *, entity_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM note WHERE case_id = ?"
        args: list[Any] = [self.case_id]
        if entity_id:
            sql += " AND entity_id = ?"
            args.append(entity_id)
        sql += " ORDER BY created_at"
        return [dict(r) for r in self._conn.execute(sql, tuple(args))]

    # -- export ---------------------------------------------------------- #
    def export(self) -> dict[str, Any]:
        """A JSON view of the whole graph, for the dashboard and the evidence pack.

        Merged entities are included with a ``merged_into`` pointer rather than
        dropped, so the history stays visible to whoever reads the export later.
        """

        ents = self._conn.execute(
            "SELECT * FROM entity WHERE case_id = ? ORDER BY entity_id", (self.case_id,)
        )
        nodes = []
        for r in ents:
            services = [
                x["service_id"]
                for x in self._conn.execute(
                    "SELECT service_id FROM entity_service WHERE entity_id = ?"
                    " ORDER BY service_id",
                    (r["entity_id"],),
                )
            ]
            arts = [
                dict(a)
                for a in self._conn.execute(
                    "SELECT a.kind, a.value, a.service_id, a.path, a.evidence_sha256"
                    " FROM artefact a JOIN entity_service es ON es.service_id = a.service_id"
                    " WHERE es.entity_id = ? ORDER BY a.kind",
                    (r["entity_id"],),
                )
            ]
            nodes.append(
                {
                    "entity_id": r["entity_id"],
                    "label": r["label"],
                    "status": r["status"],
                    "merged_into": r["merged_into"],
                    "services": services,
                    "artefacts": arts,
                }
            )

        return {
            "case_id": self.case_id,
            "entities": nodes,
            "edges": [e.to_dict() for e in self.edges()],
            "merges": [
                {
                    "merge_id": m.merge_id,
                    "survivor": m.survivor,
                    "absorbed": m.absorbed,
                    "actor": m.actor,
                    "rationale": m.rationale,
                    "created_at": m.created_at,
                    "reversed_at": m.reversed_at,
                }
                for m in self.merge_log()
            ],
            "findings": self.findings(),
            "notes": self.notes(),
            "counts": {
                "entities": len([n for n in nodes if n["merged_into"] is None]),
                "merged": len([n for n in nodes if n["merged_into"]]),
                "edges": len(self.edges()),
                "conflicts": len(
                    [e for e in self.edges() if e.status == EdgeStatus.CONFLICT.value]
                ),
                "findings": len(self.findings()),
            },
        }
