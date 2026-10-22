"""Command-line interface for the identity graph (M3).

    python -m graph build  --farm ./out/farm.json --db ./casework/CASE-1/graph.db
    python -m graph show   --db ./casework/CASE-1/graph.db
    python -m graph export --db ./casework/CASE-1/graph.db --out ./out/graph.json

    # the analyst workflow, in the order the doctrine requires
    python -m graph confirm --db ... --edge edg-abc --actor a.sharma
    python -m graph merge   --db ... --survivor ent-A --absorbed ent-B \
        --actor a.sharma --rationale "shared unique favicon, both pages self-describe as Parts Bin"
    python -m graph reverse --db ... --merge mrg-123 --actor a.sharma --reason "favicon re-used across markets"

Every mutating command demands ``--actor`` because an unattributed change to an
investigative graph is not auditable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from graph.extract import extract_artefacts, summarise
from graph.model import EdgeStatus, GraphError
from graph.resolve import DEFAULT_FREQUENCY_CAP, default_cohorts, resolve, to_edges
from graph.store import GraphStore, utc_now


def _load_farm(path: Path):
    """Read a farm manifest from disk.

    Reading the written ``farm.json`` rather than re-running the generator is
    deliberate: the graph is then reproducible from the artefact a reviewer holds,
    not from whatever the generator would emit today. Only the fields the extractor
    consumes are rebuilt, because a fuller loader would be a second implementation
    of the farm model and two sources of truth is how they start to disagree.
    """

    from types import SimpleNamespace

    data = json.loads(path.read_text())
    services = [
        SimpleNamespace(
            service_id=s["service_id"],
            cluster_id=s.get("cluster_id"),
            shared_artifacts=s.get("shared_artifacts", {}),
        )
        for s in data["services"]
    ]
    return SimpleNamespace(services=services, config=data.get("config", {}))


def cmd_build(args) -> int:
    farm = _load_farm(Path(args.farm))
    now = utc_now()

    with GraphStore(args.db, case_id=args.case) as store:
        arts = extract_artefacts(farm, observed_at=now)
        for art in arts:
            store.add_artefact(art)

        # One entity per service. The graph starts maximally split and only ever
        # closes that gap when an analyst says so, which is the opposite of starting
        # from a guess and confirming it.
        for svc in farm.services:
            eid = f"ent-{svc.service_id}"
            store.add_entity(eid, f"operator of {svc.service_id}", created_at=now)
            store.attach_service(eid, svc.service_id)

        ents = store.entities()
        proposals = resolve(ents, arts, cap=args.cap)
        edges = to_edges(proposals)
        for edge in edges:
            store.add_edge(edge)

        counts: dict[str, int] = {}
        for e in edges:
            counts[e.status] = counts.get(e.status, 0) + 1

        payload = {
            "case": args.case,
            "db": str(args.db),
            "artefacts": summarise(arts),
            "entities": len(ents),
            "edges": len(edges),
            "edges_by_status": dict(sorted(counts.items())),
            "conflicts": counts.get(EdgeStatus.CONFLICT.value, 0),
            "frequency_cap": args.cap,
            "default_cohorts": default_cohorts(arts),
        }
        print(json.dumps(payload, indent=2))
    return 0


def cmd_show(args) -> int:
    with GraphStore(args.db, case_id=args.case) as store:
        data = store.export()
        print(json.dumps(data["counts"], indent=2))
        if args.edges:
            for e in data["edges"][: args.limit]:
                flag = "CONFLICT" if e["has_conflict"] else e["status"]
                print(
                    f"  {e['edge_id']}  {e['a_entity']:<22} {e['b_entity']:<22}"
                    f" {e['method']:<14} {e['score']:.3f}  {flag}"
                )
                for ev in e["supporting"]:
                    print(f"      + {ev['kind']}: {ev['value'][:40]}  ({ev['note']})")
                for ev in e["counter"]:
                    print(f"      - {ev['kind']}: {ev['value'][:40]}  ({ev['note']})")
    return 0


def cmd_export(args) -> int:
    with GraphStore(args.db, case_id=args.case) as store:
        data = store.export()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2))
    print(json.dumps({"written": str(out), "counts": data["counts"]}, indent=2))
    return 0


def cmd_merge(args) -> int:
    with GraphStore(args.db, case_id=args.case) as store:
        rec = store.merge(
            args.survivor, args.absorbed, actor=args.actor, rationale=args.rationale
        )
        print(
            json.dumps(
                {"merge_id": rec.merge_id, "survivor": rec.survivor, "absorbed": rec.absorbed},
                indent=2,
            )
        )
    return 0


def cmd_reverse(args) -> int:
    with GraphStore(args.db, case_id=args.case) as store:
        store.reverse_merge(args.merge, actor=args.actor, reason=args.reason)
        print(json.dumps({"reversed": args.merge}, indent=2))
    return 0


def cmd_status(args) -> int:
    with GraphStore(args.db, case_id=args.case) as store:
        store.set_edge_status(args.edge, args.status, actor=args.actor)
        print(json.dumps({"edge": args.edge, "status": args.status}, indent=2))
    return 0


def cmd_note(args) -> int:
    with GraphStore(args.db, case_id=args.case) as store:
        store.add_note(args.body, actor=args.actor, entity_id=args.entity)
        print(json.dumps({"noted": True}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m graph", description=__doc__)
    p.add_argument("--db", required=True, help="path to the case graph database")
    p.add_argument("--case", default="CASE-1", help="case id this graph belongs to")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="extract artefacts from a farm and propose edges")
    b.add_argument("--farm", required=True)
    b.add_argument("--cap", type=int, default=DEFAULT_FREQUENCY_CAP)
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("show", help="print counts and optionally the edges")
    s.add_argument("--edges", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_show)

    e = sub.add_parser("export", help="write the whole graph as JSON")
    e.add_argument("--out", required=True)
    e.set_defaults(func=cmd_export)

    m = sub.add_parser("merge", help="combine two entities (reversible)")
    m.add_argument("--survivor", required=True)
    m.add_argument("--absorbed", required=True)
    m.add_argument("--actor", required=True)
    m.add_argument("--rationale", required=True)
    m.set_defaults(func=cmd_merge)

    r = sub.add_parser("reverse", help="undo a merge")
    r.add_argument("--merge", required=True)
    r.add_argument("--actor", required=True)
    r.add_argument("--reason", default="")
    r.set_defaults(func=cmd_reverse)

    st = sub.add_parser("status", help="confirm or reject an edge")
    st.add_argument("--edge", required=True)
    st.add_argument("--status", required=True, choices=["confirmed", "rejected", "possible"])
    st.add_argument("--actor", required=True)
    st.set_defaults(func=cmd_status)

    n = sub.add_parser("note", help="attach an analyst note")
    n.add_argument("--body", required=True)
    n.add_argument("--actor", required=True)
    n.add_argument("--entity", default=None)
    n.set_defaults(func=cmd_note)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
