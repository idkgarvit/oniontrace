"""Score the demo case against the farm's ground truth, item by item.

The console shows a case. A case with 12 links on it looks the same whether those 12 are the
12 true links or 12 plausible-looking wrong ones, and the walkthrough can only report counts.
This reads the same case the console reads, joins it to the ground truth the farm was built
from, and says which individual links and findings are right.

Four questions, and each one names the wrong answers rather than only counting them:

1. Links. Every proposed pair is scored against the real operator clusters. False proposals are
   printed with the artefacts that supported them, and true pairs the run missed are printed by
   address.
2. Support. A link held up only by a widespread artefact is the failure this project measures,
   so each proposal is labelled by whether any of its support is a distinctive artefact or all
   of it is hard-negative class.
3. Findings. Each injected flaw is looked for in the findings, by service and kind. Missed flaws
   are listed. Findings that no injected flaw explains are listed too -- a detector that fires
   on a service with nothing wrong with it is a false positive, not a bonus.
4. Singletons. A service with no cluster-mate has no true link. Any link touching one is a false
   merge, which is the check the abstention cases exist for.

Usage:
    python3 scripts/qa/demo_accuracy.py                  # scores CASE-DEMO
    python3 scripts/qa/demo_accuracy.py --case CASE-X    # scores another case
    python3 scripts/qa/demo_accuracy.py --json out.json  # also writes the numbers

Exit code is 0 whenever the scoring ran. This is a measurement, not a gate: it reports what the
case says, including when that is bad news. `tests/test_demo_accuracy.py` pins the headline
numbers so the demo cannot quietly stop being the demo.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FARM = ROOT / "out" / "farm.json"


def load_farm(path: Path) -> dict:
    """The ground truth the case was generated from."""

    if not path.exists():
        raise SystemExit(
            f"no farm at {path}. The demo case is generated from one, so scoring it needs the "
            "same farm: run `python -m farm generate --out out` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_graph(case: str) -> sqlite3.Connection:
    """The case store the console reads."""

    db = ROOT / "casework" / case / "graph.db"
    if not db.exists():
        raise SystemExit(
            f"no graph store for {case} at {db}. Build the case the way the walkthrough does:\n"
            f"  python -m oniontrace run --seed 20260925 --case {case}"
        )
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    return connection


def service_of(connection: sqlite3.Connection) -> dict[str, str]:
    """entity id -> service id, from the store rather than from the naming convention."""

    return {r["entity_id"]: r["service_id"] for r in connection.execute("select * from entity_service")}


def true_pairs(farm: dict) -> set[frozenset[str]]:
    """Every pair of services that really is one operator."""

    pairs: set[frozenset[str]] = set()
    for cluster in farm["clusters"]:
        members = list(cluster["service_ids"])
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pairs.add(frozenset((a, b)))
    return pairs


def cluster_of(farm: dict) -> dict[str, str | None]:
    return {s["service_id"]: s.get("cluster_id") for s in farm["services"]}


def hard_negative_kinds(farm: dict) -> set[str]:
    """Artefact kinds whose agreement means nothing, by the farm's own catalogue."""

    return {h["flaw_kind"] for h in farm["hard_negatives"]}


def score_links(connection: sqlite3.Connection, farm: dict) -> dict:
    entity_service = service_of(connection)
    truth = true_pairs(farm)
    clusters = cluster_of(farm)
    useless = hard_negative_kinds(farm)

    proposed: list[dict] = []
    for row in connection.execute("select * from edge"):
        a = entity_service.get(row["a_entity"])
        b = entity_service.get(row["b_entity"])
        if a is None or b is None:
            continue
        support = json.loads(row["supporting"] or "[]")
        kinds = sorted({item.get("kind") for item in support if item.get("kind")})
        proposed.append(
            {
                "edge_id": row["edge_id"],
                "pair": frozenset((a, b)),
                "a": a,
                "b": b,
                "method": row["method"],
                "score": row["score"],
                "status": row["status"],
                "support_kinds": kinds,
                "distinctive_support": sorted(set(kinds) - useless),
                "useless_support": sorted(set(kinds) & useless),
                "counter": json.loads(row["counter"] or "[]"),
            }
        )

    called = {p["pair"] for p in proposed}
    false_proposals = [p for p in proposed if p["pair"] not in truth]
    missed = sorted(
        (tuple(sorted(pair)) for pair in truth - called),
        key=lambda pair: (clusters.get(pair[0]) or "", pair[0]),
    )
    singletons = {sid for sid, cid in clusters.items() if cid is None}
    singleton_links = [p for p in proposed if (p["a"] in singletons or p["b"] in singletons)]

    return {
        "proposed": len(proposed),
        "true": len(truth),
        "correct": len(proposed) - len(false_proposals),
        "precision": (len(proposed) - len(false_proposals)) / len(proposed) if proposed else 0.0,
        "recall": (len(proposed) - len(false_proposals)) / len(truth) if truth else 0.0,
        "false_proposals": false_proposals,
        "missed": missed,
        "held_by_widespread_only": [
            p for p in proposed if p["support_kinds"] and not p["distinctive_support"]
        ],
        "singleton_links": singleton_links,
        "by_status": _counts(p["status"] for p in proposed),
        "by_method": _counts(p["method"] for p in proposed),
    }


def score_findings(connection: sqlite3.Connection, farm: dict) -> dict:
    recorded = [
        {"service_id": r["service_id"], "kind": r["kind"], "path": r["path"]}
        for r in connection.execute("select * from finding")
    ]
    seen = {(f["service_id"], f["kind"]) for f in recorded}

    # A cluster-scope entry is a *shared artefact*, and the run's own name for it is linking
    # evidence rather than a finding: `shared_favicon` on three services is what makes them look
    # like one operator, and reporting it as three findings would both inflate the count and
    # confuse a reader about what the detector found. Counting those as missing findings would
    # publish a recall of 19/37 for a run that found every flaw it was asked to find.
    expected: list[dict] = []
    shared: list[dict] = []
    for flaw in farm["flaws"]:
        target = expected if flaw.get("scope") == "single" else shared
        for service_id in flaw.get("service_ids", []):
            target.append(
                {
                    "flaw_id": flaw["flaw_id"],
                    "kind": flaw["kind"],
                    "service_id": service_id,
                    "path": flaw.get("path"),
                    "scope": flaw.get("scope"),
                }
            )

    caught = [e for e in expected if (e["service_id"], e["kind"]) in seen]
    missed = [e for e in expected if (e["service_id"], e["kind"]) not in seen]
    explained = {(e["service_id"], e["kind"]) for e in expected}
    unexplained = [f for f in recorded if (f["service_id"], f["kind"]) not in explained]

    return {
        "recorded": len(recorded),
        "expected": len(expected),
        "caught": len(caught),
        "missed": missed,
        "unexplained": unexplained,
        "shared_artefacts": len(shared),
        "shared_kinds": sorted({s["kind"] for s in shared}),
        "kinds_recorded": sorted({f["kind"] for f in recorded}),
    }


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def report(case: str, links: dict, findings: dict, farm: dict) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"demo accuracy: {case} against the farm it was generated from")
    add(f"farm: {len(farm['services'])} services in {len(farm['clusters'])} clusters, "
        f"{len(farm['flaws'])} injected flaws, {len(farm['hard_negatives'])} hard negatives, "
        f"{len(farm['abstention_cases'])} abstention cases")
    add("")
    add("links")
    add(f"  proposed {links['proposed']}, of which true {links['correct']}, "
        f"false {len(links['false_proposals'])}")
    add(f"  true pairs in the farm {links['true']}, of which found {links['correct']}, "
        f"missed {len(links['missed'])}")
    add(f"  precision {links['precision']:.3f}   recall {links['recall']:.3f}")
    add(f"  by status {links['by_status']}   by method {links['by_method']}")

    if links["held_by_widespread_only"]:
        add(f"  {len(links['held_by_widespread_only'])} proposal(s) supported only by "
            "hard-negative class artefacts:")
        for p in links["held_by_widespread_only"]:
            add(f"    {p['a']} -- {p['b']}  support {p['support_kinds']}")
    else:
        add("  0 proposals are supported only by hard-negative class artefacts")

    if links["false_proposals"]:
        add(f"  false proposals ({len(links['false_proposals'])}):")
        for p in links["false_proposals"]:
            add(f"    {p['a']} -- {p['b']}  score {p['score']:.3f}  support {p['support_kinds']}")
    else:
        add("  no false proposals")

    if links["missed"]:
        add(f"  true pairs missed ({len(links['missed'])}):")
        for a, b in links["missed"]:
            add(f"    {a} -- {b}")
    else:
        add("  no true pair was missed")

    if links["singleton_links"]:
        add(f"  FALSE MERGES: {len(links['singleton_links'])} link(s) touch a service with no "
            "cluster-mate, so no link there can be true:")
        for p in links["singleton_links"]:
            add(f"    {p['a']} -- {p['b']}  score {p['score']:.3f}")
    else:
        add("  no link touches a singleton service, which is what the abstention cases test")

    add("")
    add("findings")
    add(f"  recorded {findings['recorded']}, explained by a single-service injected flaw "
        f"{findings['caught']} of {findings['expected']}")
    add(f"  {findings['shared_artefacts']} further injected entries are cluster-scope shared "
        "artefacts, which are linking evidence and are scored above, not findings")
    if findings["missed"]:
        add(f"  injected flaws with no finding ({len(findings['missed'])}):")
        for m in findings["missed"]:
            add(f"    {m['flaw_id']} {m['kind']} on {m['service_id']} ({m['scope']} scope)")
    else:
        add("  every injected flaw has a finding")
    if findings["unexplained"]:
        add(f"  findings no injected flaw explains ({len(findings['unexplained'])}):")
        for f in findings["unexplained"]:
            add(f"    {f['kind']} on {f['service_id']} ({f['path']})")
    else:
        add("  no finding fires on a service with nothing injected")

    add("")
    add("what this is not: the farm's correlation ground truth is synthetic. These numbers say the "
        "pipeline is right about a world that was built to be scored, not about a world nobody "
        "has labelled.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="CASE-DEMO")
    parser.add_argument("--farm", type=Path, default=FARM)
    parser.add_argument("--json", type=Path, default=None, help="also write the numbers here")
    args = parser.parse_args()

    farm = load_farm(args.farm)
    connection = load_graph(args.case)
    try:
        links = score_links(connection, farm)
        findings = score_findings(connection, farm)
    finally:
        connection.close()

    print(report(args.case, links, findings, farm))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "case": args.case,
                    "links": {k: v for k, v in links.items() if k != "false_proposals"},
                    "false_proposals": [
                        {k: v for k, v in p.items() if k != "pair"} for p in links["false_proposals"]
                    ],
                    "findings": findings,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nnumbers written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
