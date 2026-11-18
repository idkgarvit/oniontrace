"""Trust edges from explicit vouch statements.

A vouch is a *relationship*, not an identity claim. "A vouches for B" says the two
operators know each other; it does not say they are the same person. The graph stores
it as its own edge method so it can be shown next to identity candidates without
being counted as one, and :meth:`graph.store.GraphStore.merge` refuses to act on it.

The edge is between the entities that operate the two services, because that is what
the graph has. The evidence names the handle that was vouched for and the page the
statement was read from, so a reader can see which operator the claim was about even
when the entity covers several people.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from graph.extract import TrustClaim
from graph.model import (
    Edge,
    EdgeMethod,
    EdgeStatus,
    Evidence,
    TRUST_STATEMENT,
)
from graph.store import utc_now


def _trust_edge_id(a_entity: str, b_entity: str) -> str:
    left, right = (a_entity, b_entity) if a_entity <= b_entity else (b_entity, a_entity)
    digest = hashlib.sha256(f"{left}|{right}|trust".encode()).hexdigest()[:16]
    return f"edg-trust-{digest}"


def build_trust_edges(
    entities: Sequence, claims: Iterable[TrustClaim]
) -> list[Edge]:
    """One edge per distinct service pair with a published vouch.

    Claims whose two services resolve to the same entity are skipped: a page that
    vouches for somebody it also hosts says nothing about a relationship between two
    operators, and drawing it as an edge would add a self-loop.
    """

    entity_for_service: dict[str, str] = {}
    for entity in entities:
        for service_id in entity.service_ids:
            entity_for_service[service_id] = entity.entity_id

    by_pair: dict[tuple[str, str], list[TrustClaim]] = {}
    for claim in claims:
        source = entity_for_service.get(claim.source_service)
        target = entity_for_service.get(claim.target_service)
        if source is None or target is None or source == target:
            continue
        pair = (source, target) if source <= target else (target, source)
        by_pair.setdefault(pair, []).append(claim)

    now = utc_now()
    edges: list[Edge] = []
    for (left, right), pair_claims in sorted(by_pair.items()):
        evidence = [
            Evidence(
                kind=TRUST_STATEMENT,
                value=f"@{claim.target_handle}",
                a_service=claim.source_service,
                b_service=claim.target_service,
                weight=0.0,
                note=(
                    f"explicit vouch on {claim.path} in {claim.source_service}; "
                    "a social claim about a relationship, not identity evidence"
                ),
            )
            for claim in sorted(pair_claims, key=lambda c: (c.source_service, c.target_handle))
        ]
        edges.append(
            Edge(
                edge_id=_trust_edge_id(left, right),
                a_entity=left,
                b_entity=right,
                method=EdgeMethod.TRUST.value,
                # Deliberately zero: this number is not identity confidence. The
                # method label is what tells a reader what the edge is.
                score=0.0,
                status=EdgeStatus.POSSIBLE.value,
                supporting=evidence,
                counter=[],
                created_at=now,
            )
        )
    return edges
