"""Score operator correlation against real certificate groupings, not generated ones.

The farm's correlation numbers come from ground truth the generator wrote down, which is the
only way to measure precision at all, and also the reason they cannot be a claim about the
world. Certificate Transparency offers one real ground truth that costs nothing to trust: a
certificate naming several onion services asserts that its holder controls all of them. That is
an assertion by the party best placed to know, it is signed, and it is unrelated to anything
this project generated.

So this module asks the question the farm cannot: given only public certificate fields, how well
can those real groupings be recovered? The features are the ones a passive observer actually
has -- the issuer and the issue date. Co-certificate is the *target*, never a feature, because
a linker that uses it would be reading the answer key.

Two things this module refuses to do. It does not report the best cap found on the data it is
scored on: the sweep is selected on one half of the clusters and reported on the other. And it
does not treat a high number as the goal: the farm's synthetic ceiling is 1.000, and if real
data reaches a third of that, saying so is the result.

Standard library only, like the rest of eval/.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Iterable, Sequence

Pair = tuple[str, str]


@dataclass(frozen=True)
class CertificateRecord:
    """The public fields of one certificate, reduced to what a passive observer can read."""

    onions: frozenset[str]
    issuer: str
    issued: str


def parse_sans(certificate: dict[str, Any]) -> list[str]:
    """Split a ``name_value`` field into names, tolerating any whitespace and newlines."""

    raw = str(certificate.get("name_value") or "")
    names: list[str] = []
    for line in raw.replace("\\n", "\n").splitlines():
        for piece in line.split(","):
            name = piece.strip().lower().lstrip("*.")
            if name:
                names.append(name)
    return names


def is_onion(name: str) -> bool:
    """A well-formed v3 (56 base32) or v2 (16 base32) onion address."""

    if not name.endswith(".onion"):
        return False
    label = name[: -len(".onion")]
    return len(label) in (16, 56) and all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in label)


def records(certificates: Iterable[dict[str, Any]]) -> list[CertificateRecord]:
    out: list[CertificateRecord] = []
    for certificate in certificates:
        onions = frozenset(name for name in parse_sans(certificate) if is_onion(name))
        if not onions:
            continue
        out.append(
            CertificateRecord(
                onions=onions,
                issuer=str(certificate.get("issuer_name") or ""),
                issued=str(certificate.get("not_before") or "")[:10],
            )
        )
    return out


def _pair(a: str, b: str) -> Pair:
    return (a, b) if a <= b else (b, a)


def pairs_of(members: Iterable[str]) -> set[Pair]:
    return {_pair(a, b) for a, b in combinations(sorted(set(members)), 2)}


def clusters(rows: Sequence[CertificateRecord]) -> dict[str, set[str]]:
    """True clusters: onions joined by appearing in the same certificate, transitively."""

    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(a: str, b: str) -> None:
        left, right = find(a), find(b)
        if left != right:
            parent[left] = right

    for row in rows:
        members = sorted(row.onions)
        for other in members[1:]:
            union(members[0], other)
        find(members[0])

    grouped: dict[str, set[str]] = defaultdict(set)
    for onion in parent:
        grouped[find(onion)].add(onion)
    return dict(grouped)


def group_by(rows: Sequence[CertificateRecord], key: str) -> dict[Any, set[str]]:
    """Group services by a public field, or by issuer and issue date together."""

    grouped: dict[Any, set[str]] = defaultdict(set)
    for row in rows:
        value = (row.issuer, row.issued) if key == "batch" else row.issuer
        grouped[value].update(row.onions)
    return dict(grouped)


def propose(grouped: dict[Any, set[str]], cap: int | None = None) -> set[Pair]:
    """Link every pair of services that shares a value, optionally dropping crowded values."""

    proposed: set[Pair] = set()
    for members in grouped.values():
        if cap is not None and len(members) > cap:
            continue
        proposed |= pairs_of(members)
    return proposed


def evaluate(proposed: set[Pair], truth: set[Pair]) -> dict[str, Any]:
    true_positive = len(proposed & truth)
    false_positive = len(proposed - truth)
    false_negative = len(truth - proposed)
    precision = true_positive / (true_positive + false_positive) if proposed else 0.0
    recall = true_positive / (true_positive + false_negative) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "proposed": len(proposed),
        "true_pairs": len(truth),
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _bucket(onion: str) -> int:
    return int(hashlib.sha256(onion.encode()).hexdigest(), 16) % 2


def split_services(rows: Sequence[CertificateRecord]) -> tuple[set[str], set[str]]:
    """Two disjoint halves, split by cluster so a cluster never straddles the boundary."""

    select: set[str] = set()
    report: set[str] = set()
    for members in clusters(rows).values():
        target = select if _bucket(sorted(members)[0]) == 0 else report
        target |= members
    return select, report


def _restrict(pairs: set[Pair], services: set[str]) -> set[Pair]:
    return {(a, b) for a, b in pairs if a in services and b in services}


def sweep(
    rows: Sequence[CertificateRecord],
    caps: Sequence[int],
) -> list[dict[str, Any]]:
    """Precision and recall as the frequency cap moves, plus the no-cap row."""

    grouped = group_by(rows, "batch")
    truth = set()
    for members in clusters(rows).values():
        truth |= pairs_of(members)

    results: list[dict[str, Any]] = []
    for cap in (None, *caps):
        measured = evaluate(propose(grouped, cap), truth)
        results.append({"cap": cap, **measured})
    return results


def held_out_cap(
    rows: Sequence[CertificateRecord], caps: Sequence[int]
) -> dict[str, Any]:
    """Choose the cap on one half of the clusters, then report it on the other half.

    Reporting the best cap found on the scored data would be a fit, not a result. The split is
    by whole cluster, so no cluster is learned on one half and scored on the other.
    """

    select, report = split_services(rows)
    grouped = group_by(rows, "batch")
    truth = set()
    for members in clusters(rows).values():
        truth |= pairs_of(members)
    truth_select = _restrict(truth, select)
    truth_report = _restrict(truth, report)

    best_cap: int | None = None
    best_f1 = -1.0
    for cap in caps:
        score = evaluate(_restrict(propose(grouped, cap), select), truth_select)
        if score["f1"] > best_f1:
            best_f1, best_cap = score["f1"], cap

    chosen = evaluate(_restrict(propose(grouped, best_cap), report), truth_report)
    return {
        "selection_services": len(select),
        "report_services": len(report),
        "chosen_cap": best_cap,
        "selection_f1": round(best_f1, 4),
        "report": chosen,
    }


def run(
    certificates: Iterable[dict[str, Any]],
    *,
    caps: Sequence[int] = (2, 3, 5, 10, 15, 20, 25, 40, 60),
) -> dict[str, Any]:
    rows = records(certificates)
    found = clusters(rows)
    truth = set()
    for members in found.values():
        truth |= pairs_of(members)

    issuer_only = evaluate(propose(group_by(rows, "issuer")), truth)
    batch = evaluate(propose(group_by(rows, "batch")), truth)
    curve = sweep(rows, caps)

    return {
        "services": len({onion for row in rows for onion in row.onions}),
        "certificates": len(rows),
        "clusters": len(found),
        "largest_cluster": max((len(m) for m in found.values()), default=0),
        "true_pairs": len(truth),
        "issuer_only": issuer_only,
        "issuer_and_date": batch,
        "sweep": curve,
        "held_out": held_out_cap(rows, caps),
        "service_count_note": (
            "A wildcard SAN is folded into the name it covers: '*.x.onion' and 'x.onion' are one "
            "holder, so counting them as two services would split a single operator across two "
            "nodes and corrupt the ground truth. The 688 unique onions reported elsewhere count "
            "a wildcard as its own name; this evaluation reports 516 over the same cache for "
            "that reason."
        ),
    }
