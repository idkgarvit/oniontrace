"""A corpus built from public sources, and the labels those sources carry themselves.

The synthetic farm exists so a detector can be measured. Real data cannot be measured the
same way, because nobody publishes ``cluster_id`` for a live onion service. What real data
*does* publish is declarations: an operator advertises its onion address from a clearnet
domain, a certificate names both an onion and a clearnet host, a market lists its mirrors.
Those are labels produced by the operator rather than by the analyst, and they are the
only honest ground truth available without an authorised partner.

This module is the glue between the acquisition layer and the label-free scorers:

  * it normalises the three source records the project already parses
    (``Advertisement``, ``OnionAssociation``, ``SeedCandidate``) into one observation
    type with provenance;
  * it turns declarations into ground-truth groups, merged transitively, so two clearnet
    domains that advertise the same onion land in one group;
  * it proposes links from shared artefacts under a frequency cap.

Two decisions here only matter on real data, and both were wrong in the farm code:

**Frequency is keyed by ``(kind, value)``, not by ``value``.** The farm's values are
unique enough that keying on the string alone never collided. In a real crawl it will:
the same 64-hex string can be an asset hash in one class and a favicon digest in another.
Counting them together would filter a genuine artefact because an unrelated class happened
to use the same characters.

**The cap is what makes the join tractable.** Pair generation iterates values, not pairs
of subjects: for each ``(kind, value)`` seen on at most ``cap`` services, emit the pairs
among those services and skip the rest. Without the cap a corpus of n subjects needs
n^2/2 comparisons; with it the work is bounded by the number of surviving values times
cap^2. The precision rule and the performance rule are the same rule.

**A known weakness, recorded rather than hidden.** A frequency rule can be turned against
itself: one forged copy of a genuine artefact raises its frequency, and if that crosses the
cap the value is discarded -- so a family whose only evidence was soft artefacts loses its
link because someone copied one of them. ``tests/test_realdata.py`` pins that behaviour.
It is bounded in two ways. Hard classes (SSH host key, TLS key, application bundle) cannot
be forged without the key, so a family supported by one is immune. And the effect needs a
small corpus: at real population sizes a single extra copy does not move a prevalence
threshold, which is why the cap is expressed over the subjects actually observed rather
than over an imagined total.

Nothing here opens a socket. Callers hand in records; loading and fetching belong to
``collect``.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from eval.labelfree import Link, canonical


def normalise_subject(value: str) -> str:
    """Reduce a host, URL, or onion address to one comparable key.

    Accepts ``http://x.onion/path``, ``x.onion``, and ``X.ONION`` and returns
    ``x.onion``. A bare domain is kept as-is; the scheme, port, path and trailing dot
    are not part of identity for correlation.
    """
    text = value.strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    else:
        text = text.split("/", 1)[0]
        text = text.rsplit("@", 1)[-1]
        text = text.split(":", 1)[0]
    return text.rstrip(".")


@dataclass(frozen=True)
class Observation:
    """One artefact value seen on one subject, with where it came from.

    ``provenance`` is deliberately a string rather than a structured object: it has to
    survive a JSONL round trip and still point a reader at the record that supports the
    claim. For CT it is a log id and leaf index; for an advertisement it is the URL that
    carried the header; for a crawl it is the WARC record id.
    """

    subject: str
    kind: str
    value: str
    source: str
    provenance: str
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.subject or not self.kind or not self.value:
            raise ValueError("observation needs subject, kind and value")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Declaration:
    """A statement by an operator or a certificate that these subjects belong together."""

    declaration_id: str
    subjects: tuple[str, ...]
    source: str
    provenance: str

    def __post_init__(self) -> None:
        if len(self.subjects) < 2:
            raise ValueError("a declaration needs at least two subjects")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["subjects"] = list(self.subjects)
        return data


# --------------------------------------------------------------------------- #
# Adapters: existing source records -> corpus records
# --------------------------------------------------------------------------- #


def from_advertisement(advertisement) -> tuple[Declaration, list[Observation]]:
    """An Onion-Location header or meta tag: the operator names its own onion.

    The strongest label in the set, because the clearnet operator is asserting the
    association from infrastructure it controls. It is still an assertion, not a proof:
    a compromised clearnet site could advertise someone else's onion.
    """
    onion = normalise_subject(advertisement.onion_address)
    clearnet = normalise_subject(advertisement.advertised_url or advertisement.source_url)
    declaration = Declaration(
        declaration_id=f"onion-location:{clearnet}->{onion}",
        subjects=tuple(sorted({clearnet, onion})),
        source="onion-location",
        provenance=advertisement.source_url,
    )
    observation = Observation(
        subject=onion,
        kind="onion_location",
        value=clearnet,
        source="onion-location",
        provenance=advertisement.source_url,
    )
    return declaration, [observation]


def from_ct_association(association) -> tuple[Declaration, list[Observation]]:
    """A certificate naming both an onion and clearnet hosts (SAUTE's signal).

    Weaker than an advertisement: the certificate proves the names appeared together in
    one certificate, not that the same party controls both. It is recorded as a
    declaration because that is the claim the certificate makes, and the report says so.
    """
    onion = normalise_subject(association.onion)
    names = tuple(sorted({normalise_subject(name) for name in association.clearnet_names}))
    subjects = tuple(sorted({onion, *names}))
    provenance = f"ct:{','.join(association.log_ids)}"
    declaration = Declaration(
        declaration_id=f"ct:{onion}",
        subjects=subjects,
        source="ct",
        provenance=provenance,
    )
    observations = [
        Observation(
            subject=onion,
            kind="ct_clearnet_name",
            value=name,
            source="ct",
            provenance=provenance,
            observed_at=association.first_seen,
        )
        for name in names
    ]
    return declaration, observations


def from_seed(seed) -> Observation:
    """A search-index result: a subject and where it was listed, no artefact claimed."""
    return Observation(
        subject=normalise_subject(seed.address),
        kind="index_listing",
        value=seed.source_url,
        source="index",
        provenance=seed.source_url,
    )


# --------------------------------------------------------------------------- #
# The corpus
# --------------------------------------------------------------------------- #


class Corpus:
    """Observations and declarations, with the joins the scorers need."""

    def __init__(self) -> None:
        self._observations: list[Observation] = []
        self._declarations: list[Declaration] = []

    # -- building --------------------------------------------------------- #

    def add(self, observation: Observation) -> None:
        self._observations.append(observation)

    def declare(self, declaration: Declaration) -> None:
        self._declarations.append(declaration)

    def extend(self, records: Iterable[Observation | Declaration]) -> None:
        for record in records:
            if isinstance(record, Declaration):
                self.declare(record)
            else:
                self.add(record)

    def __len__(self) -> int:
        return len(self._observations)

    # -- views ------------------------------------------------------------ #

    @property
    def observations(self) -> list[Observation]:
        return list(self._observations)

    @property
    def declarations(self) -> list[Declaration]:
        return list(self._declarations)

    def subjects(self) -> list[str]:
        return sorted({obs.subject for obs in self._observations})

    def artefacts(self) -> dict[str, dict[str, set[str]]]:
        """subject -> kind -> values seen on that subject."""
        out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for obs in self._observations:
            if obs.kind in {"index_listing", "onion_location"}:
                # a listing and a declaration are not artefacts of the service itself
                continue
            out[obs.subject][obs.kind].add(obs.value)
        return {subject: dict(kinds) for subject, kinds in out.items()}

    def frequencies(self) -> Counter[tuple[str, str]]:
        """How many distinct subjects carry each ``(kind, value)``."""
        counts: Counter[tuple[str, str]] = Counter()
        for kinds in self.artefacts().values():
            for kind, values in kinds.items():
                for value in values:
                    counts[(kind, value)] += 1
        return counts

    def declared_groups(self) -> dict[str, set[str]]:
        """Ground-truth groups, merged transitively over shared subjects.

        ``a.onion`` declared by ``shop.example`` and the same onion named in a
        certificate alongside ``cdn.example`` produce one group, because the onion is
        the shared member. Union-find over the declaration edges is the whole algorithm.
        """
        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for declaration in self._declarations:
            first, *rest = declaration.subjects
            for subject in rest:
                union(first, subject)

        groups: dict[str, set[str]] = defaultdict(set)
        for subject in list(parent):
            groups[find(subject)].add(subject)
        return {
            f"declared:{index}": members
            for index, members in enumerate(
                sorted(groups.values(), key=lambda m: min(m))
            )
        }

    # -- link proposal ---------------------------------------------------- #

    def propose(self, *, cap: int) -> list[dict[str, Any]]:
        """Propose links from shared artefacts, discarding values above ``cap``.

        Pairs are generated per surviving value, so a value on many services costs one
        comparison of its frequency and nothing more. That is why the cap is a
        performance boundary as well as a precision rule.
        """
        if cap < 1:
            raise ValueError("cap must be at least 1")

        frequencies = self.frequencies()
        by_value: dict[tuple[str, str], dict[str, None]] = defaultdict(dict)
        for obs in self._observations:
            if obs.kind in {"index_listing", "onion_location"}:
                continue
            by_value[(obs.kind, obs.value)][obs.subject] = None

        links: dict[Link, dict[str, Any]] = {}
        for (kind, value), subjects in by_value.items():
            if frequencies[(kind, value)] > cap:
                continue
            if len(subjects) < 2:
                continue
            for left, right in combinations(sorted(subjects), 2):
                key = canonical(left, right)
                entry = links.setdefault(key, {"a": key[0], "b": key[1], "kinds": set(), "values": {}})
                entry["kinds"].add(kind)
                entry["values"].setdefault(kind, []).append(value)

        return [
            {
                "a": entry["a"],
                "b": entry["b"],
                "kinds": sorted(entry["kinds"]),
                "values": {kind: sorted(values) for kind, values in sorted(entry["values"].items())},
            }
            for entry in sorted(links.values(), key=lambda item: (item["a"], item["b"]))
        ]

    def windows(self, *, cap: int) -> list[tuple[str, list[Link]]]:
        """Group proposed links by the day of the newest observation supporting them.

        A link is only as recent as its weakest observation, so the newest supporting
        timestamp is the honest window for it. Links with no timestamp are dropped: an
        undated link cannot contribute to a recurrence measure.
        """
        latest: dict[tuple[str, str, str], str] = {}
        for obs in self._observations:
            if not obs.observed_at:
                continue
            key = (obs.subject, obs.kind, obs.value)
            if obs.observed_at > latest.get(key, ""):
                latest[key] = obs.observed_at

        buckets: dict[str, list[Link]] = defaultdict(list)
        for link in self.propose(cap=cap):
            stamps: list[str] = []
            for kind, values in link["values"].items():
                for value in values:
                    for subject in (link["a"], link["b"]):
                        stamp = latest.get((subject, kind, value))
                        if stamp:
                            stamps.append(stamp)
            if stamps:
                buckets[max(stamps)[:10]].append(canonical(link["a"], link["b"]))
        return [(day, sorted(links)) for day, links in sorted(buckets.items())]

    # -- persistence ------------------------------------------------------ #

    def to_jsonl(self) -> str:
        lines: list[str] = []
        for observation in self._observations:
            lines.append(json.dumps({"type": "observation", **observation.to_dict()}))
        for declaration in self._declarations:
            lines.append(json.dumps({"type": "declaration", **declaration.to_dict()}))
        return "\n".join(lines) + ("\n" if lines else "")

    def write_jsonl(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(self.to_jsonl(), encoding="utf-8")
        return target

    @classmethod
    def from_jsonl(cls, text: str) -> Corpus:
        corpus = cls()
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"line {number}: not JSON ({error})") from error
            kind = record.pop("type", None)
            if kind == "observation":
                corpus.add(Observation(**record))
            elif kind == "declaration":
                record["subjects"] = tuple(record["subjects"])
                corpus.declare(Declaration(**record))
            else:
                raise ValueError(f"line {number}: unknown record type {kind!r}")
        return corpus

    @classmethod
    def read_jsonl(cls, path: str | Path) -> Corpus:
        return cls.from_jsonl(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Convenience: the whole real-data loop
# --------------------------------------------------------------------------- #


def score_corpus(corpus: Corpus, *, cap: int, min_kinds: int = 1) -> dict[str, Any]:
    """Propose, filter by corroboration, and score against the corpus's own declarations.

    This is the function the real-data experiment calls. ``min_kinds`` is left at 1 by
    default because the class-strength policy in ``eval.labelfree`` already refuses a
    single soft class; raising it applies the blunter count rule on top.
    """
    from eval.labelfree import apply_policy, score_against_declared

    proposals = corpus.propose(cap=cap)
    filtered = apply_policy(proposals)
    links = [(item["link"][0], item["link"][1]) for item in filtered["kept"]]
    declared = corpus.declared_groups()
    # recall is measured only over subjects the correlator can see artefacts for
    score = score_against_declared(links, declared, observable=corpus.artefacts().keys())
    return {
        "cap": cap,
        "proposed": len(proposals),
        "kept_by_policy": len(filtered["kept"]),
        "dropped_by_policy": len(filtered["dropped"]),
        "declarations": len(declared),
        "score": score,
        "links": links,
    }
