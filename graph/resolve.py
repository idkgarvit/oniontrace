"""Entity resolution over the artefact graph (M3).

The engine proposes links. It never merges. Read :mod:`graph.store` for why that
separation matters.

Two kinds of link come out of here and they are labelled differently because they
are not the same claim:

``deterministic``
    The same deliberate artefact value was observed on both services and that value
    is rare enough to carry operator signal. This is a fact about two observations;
    it is still not a fact about two people, which is why it arrives as a candidate
    link rather than a merge.

``probabilistic``
    Several weak signals agree. Its score is an aggregation of those signals and it
    is reported with the weights that produced it, so a reader can disagree with the
    weighting instead of having to accept a number.

The counter-evidence list is not decoration. A pair can share a favicon and still be
two operators, and the two reasons that is possible are both modelled: a value that
a vendor ships (``STOCK_DEFAULT``), and a value published elsewhere that anyone
could copy (``PUBLIC_PLACEHOLDER``). When support and doubt both exist the pair is
marked ``conflict`` and both sides are kept.
"""

from __future__ import annotations

import hashlib
import itertools
from collections import Counter, defaultdict
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Iterable, Sequence

from graph.model import (
    NON_IDENTIFYING_VALUES,
    PUBLIC_PLACEHOLDER,
    STOCK_DEFAULT,
    ArtefactKind,
    Edge,
    EdgeMethod,
    EdgeStatus,
    Evidence,
)
from graph.store import utc_now

#: How much a match on each artefact class is worth. The ordering is a claim about
#: deliberate choice: a contact handle is typed by a human, a 404 page is emitted by
#: software. Every weight is stated here so the aggregate in `score` can be
#: recomputed by hand from the evidence list.
WEIGHTS: dict[str, float] = {
    ArtefactKind.CONTACT_HANDLE.value: 0.90,
    ArtefactKind.ANALYTICS_ID.value: 0.85,
    ArtefactKind.FAVICON.value: 0.80,
    ArtefactKind.SSH_HOSTKEY.value: 0.70,
    ArtefactKind.APP_JS.value: 0.55,
    ArtefactKind.TLS_SPKI.value: 0.45,
    ArtefactKind.ERROR_404.value: 0.25,
}

#: A value on more services than this is treated as a population default and earns
#: no operator signal. Derived in the paper from cluster size: with clusters of
#: three, a value shared by four or more services cannot be cluster-specific.
DEFAULT_FREQUENCY_CAP = 3

#: Score at or above which a probabilistic pair is called a candidate rather than
#: merely possible. Both are shown; the threshold only changes the label.
CANDIDATE_THRESHOLD = 0.60


@dataclass(frozen=True)
class Proposal:
    """One pair, with everything the store needs and nothing already decided."""

    a_entity: str
    b_entity: str
    method: str
    score: float
    status: str
    supporting: list[Evidence]
    counter: list[Evidence]


def _edge_id(a: str, b: str) -> str:
    left, right = (a, b) if a <= b else (b, a)
    return f"edg-{hashlib.sha256(f'{left}|{right}'.encode()).hexdigest()[:16]}"


def artefact_frequencies(artefacts: Iterable) -> Counter:
    """How many distinct services carry each (kind, value) pair."""

    services_per_value: dict[tuple[str, str], set[str]] = defaultdict(set)
    for art in artefacts:
        services_per_value[(art.kind, art.value)].add(art.service_id)
    return Counter({k: len(v) for k, v in services_per_value.items()})


def _discriminative(art, freq: Mapping[tuple[str, str], int], cap: int) -> bool:
    """Whether a value can carry operator signal at all.

    Three separate reasons a value identifies nobody, all checked here so no caller
    can forget one:

    * it is empty;
    * it is a vendor default or a published placeholder;
    * it is shared by more services than the cap allows.
    """

    if not art.value or art.value in NON_IDENTIFYING_VALUES:
        return False
    return freq[(art.kind, art.value)] <= cap


def _counter_reasons(
    a_arts: dict[str, str],
    b_arts: dict[str, str],
    shared: dict[str, str],
    *,
    has_discriminative_support: bool,
) -> list[Evidence]:
    """Reasons to doubt a pair, each carrying what produced it.

    A shared vendor default is a real reason not to count *that signal*, which is
    already handled by excluding it from the supporting list. It is only a reason to
    doubt the *pair* when it is the only thing linking them; if the two services also
    share a unique value, the default adds nothing and must not be promoted into a
    contradiction, because then every true pair would read as a conflict and the
    conflict label would stop meaning anything.

    A deliberate identifier present on both services but **different** is doubt
    regardless of what else matched.
    """

    out: list[Evidence] = []

    if not has_discriminative_support:
        for kind, value in shared.items():
            if value == STOCK_DEFAULT:
                out.append(
                    Evidence(
                        kind=kind,
                        value=value,
                        a_service="",
                        b_service="",
                        weight=0.0,
                        note=(
                            "vendor default, so this match says the two services "
                            "installed the same software and nothing about who "
                            "operates them"
                        ),
                    )
                )
            elif value == PUBLIC_PLACEHOLDER:
                out.append(
                    Evidence(
                        kind=kind,
                        value=value,
                        a_service="",
                        b_service="",
                        weight=0.0,
                        note=(
                            "published placeholder, so anyone could carry it; linking "
                            "a person to it would be a false accusation"
                        ),
                    )
                )

    # Only kinds where a difference is meaningful. Two installs of the same software
    # routinely share a 404 page, so a difference there says nothing.
    for kind in (
        ArtefactKind.CONTACT_HANDLE.value,
        ArtefactKind.FAVICON.value,
        ArtefactKind.ANALYTICS_ID.value,
    ):
        av, bv = a_arts.get(kind), b_arts.get(kind)
        if not av or not bv:
            continue
        if av in NON_IDENTIFYING_VALUES or bv in NON_IDENTIFYING_VALUES:
            continue
        if av != bv:
            out.append(
                Evidence(
                    kind=kind,
                    value=f"{av} != {bv}",
                    a_service="",
                    b_service="",
                    weight=0.0,
                    note=f"both services carry a deliberate {kind} and the values differ",
                )
            )

    return out


def propose_for_pair(
    a_entity: str,
    a_services: Sequence[str],
    a_arts: dict[str, str],
    b_entity: str,
    b_services: Sequence[str],
    b_arts: dict[str, str],
    freq: Mapping[tuple[str, str], int],
    *,
    cap: int = DEFAULT_FREQUENCY_CAP,
    threshold: float = CANDIDATE_THRESHOLD,
) -> Proposal | None:
    """Score one pair, or return None when there is nothing to say.

    Returning None for a pair with no discriminative overlap is deliberate: an edge
    asserting "these two share nothing" would be noise on the graph, and absence of
    evidence is reported by the missing edge.
    """

    shared: dict[str, str] = {}
    supporting: list[Evidence] = []

    for kind in sorted(set(a_arts) & set(b_arts)):
        av, bv = a_arts[kind], b_arts[kind]
        if av != bv:
            continue
        shared[kind] = av
        if av in NON_IDENTIFYING_VALUES:
            continue
        if freq[(kind, av)] > cap:
            continue
        supporting.append(
            Evidence(
                kind=kind,
                value=av,
                a_service=a_services[0] if a_services else "",
                b_service=b_services[0] if b_services else "",
                weight=WEIGHTS.get(kind, 0.3),
                note=f"seen on {freq[(kind, av)]} service(s), cap {cap}",
            )
        )

    if not shared:
        return None

    # A pair linked only by vendor defaults is the exact false merge the paper
    # measures, and drawing it as an edge would fill the graph with 46 links that
    # each say nothing about who operates either service. It is not dropped, though:
    # `default_cohorts` reports it separately so the UI can warn that these services
    # share a shipped default and must not be linked on it. Silence here would hide
    # the trap; an edge would present it as a finding.
    if not supporting:
        return None

    counter = _counter_reasons(
        a_arts, b_arts, shared, has_discriminative_support=True
    )

    # Aggregate: noisy-OR over the supporting weights, so two independent weak
    # signals beat one and no combination can exceed 1.0.
    remaining = 1.0
    for ev in supporting:
        remaining *= 1.0 - ev.weight
    score = 1.0 - remaining

    if supporting:
        method = (
            EdgeMethod.DETERMINISTIC.value
            if len(supporting) == 1
            else EdgeMethod.PROBABILISTIC.value
        )
    else:
        # Nothing discriminative matched; the pair is only linked by defaults, and
        # that belongs on the graph as a stated doubt rather than a silent non-edge.
        method = EdgeMethod.DETERMINISTIC.value

    if counter and supporting:
        status = EdgeStatus.CONFLICT.value
    elif score >= threshold:
        status = EdgeStatus.CANDIDATE.value
    else:
        status = EdgeStatus.POSSIBLE.value

    return Proposal(
        a_entity=a_entity,
        b_entity=b_entity,
        method=method,
        score=score,
        status=status,
        supporting=supporting,
        counter=counter,
    )


def resolve(
    entities: Sequence,
    artefacts: Sequence,
    *,
    cap: int = DEFAULT_FREQUENCY_CAP,
    threshold: float = CANDIDATE_THRESHOLD,
) -> list[Proposal]:
    """All pairs worth drawing, ordered by score then pair id.

    String artefacts are collapsed per entity and per kind because an operator with
    three services that all carry the same favicon has one favicon, not three, and
    counting it three times would inflate the aggregate.
    """

    freq = artefact_frequencies(artefacts)

    by_service: dict[str, dict[str, str]] = defaultdict(dict)
    for art in artefacts:
        by_service[art.service_id][art.kind] = art.value

    ent_arts: dict[str, dict[str, str]] = {}
    ent_services: dict[str, list[str]] = {}
    for ent in entities:
        merged: dict[str, str] = {}
        for sid in ent.service_ids:
            for kind, value in by_service.get(sid, {}).items():
                merged.setdefault(kind, value)
        ent_arts[ent.entity_id] = merged
        ent_services[ent.entity_id] = list(ent.service_ids)

    out: list[Proposal] = []
    for a, b in itertools.combinations(sorted(ent_arts), 2):
        prop = propose_for_pair(
            a,
            ent_services[a],
            ent_arts[a],
            b,
            ent_services[b],
            ent_arts[b],
            freq,
            cap=cap,
            threshold=threshold,
        )
        if prop is not None:
            out.append(prop)

    out.sort(key=lambda p: (-p.score, min(p.a_entity, p.b_entity), max(p.a_entity, p.b_entity)))
    return out


def default_cohorts(artefacts: Iterable) -> list[dict]:
    """Groups of services that share a vendor default or a published placeholder.

    This exists so the trap stays visible. These are precisely the values that a
    naive correlator counts as evidence and a careful one discards, and an analyst
    who cannot see the cohort cannot understand why a link they expected is missing.

    Reported as cohorts rather than pairwise edges: a default shared by nine services
    is one fact about the population, not thirty-six facts about operators.
    """

    services_per_value: dict[tuple[str, str], set[str]] = {}
    for art in artefacts:
        if art.value in NON_IDENTIFYING_VALUES:
            services_per_value.setdefault((art.kind, art.value), set()).add(
                art.service_id
            )

    cohorts = []
    for (kind, value), services in sorted(services_per_value.items()):
        if len(services) < 2:
            continue
        cohorts.append(
            {
                "kind": kind,
                "value": value,
                "service_count": len(services),
                "services": sorted(services),
                "why": (
                    "vendor default: shared because the software ships it"
                    if value == STOCK_DEFAULT
                    else "published placeholder: anyone could carry it"
                ),
            }
        )
    cohorts.sort(key=lambda c: (-c["service_count"], c["kind"]))
    return cohorts


def to_edges(proposals: Iterable[Proposal]) -> list[Edge]:
    """Convert proposals into stored edges, stamping each with a stable id."""

    now = utc_now()
    return [
        Edge(
            edge_id=_edge_id(p.a_entity, p.b_entity),
            a_entity=p.a_entity,
            b_entity=p.b_entity,
            method=p.method,
            score=p.score,
            status=p.status,
            supporting=p.supporting,
            counter=p.counter,
            created_at=now,
        )
        for p in proposals
    ]
