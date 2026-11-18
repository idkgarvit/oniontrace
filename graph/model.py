"""Identity graph types (M3).

The doctrine this module encodes comes from the plan and is the whole reason the
package exists:

  * the system produces **candidate merges, never assertions**;
  * a merge is **reversible**, **logged**, and **carries its evidence**;
  * a below-threshold pair is still shown, labelled ``possible``, with its score
    visible rather than hidden;
  * when evidence points both ways on a pair, **both sides are shown** instead of
    the system quietly picking one.

Nothing here merges two entities on its own. A high score changes a label; it does
not change the graph. Only :meth:`graph.store.GraphStore.merge` moves nodes, and
that requires an actor and a rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ArtefactKind(str, Enum):
    """Artefact classes the acquisition layer can observe.

    Every member is something a passive visitor could read off a page or a
    handshake. There is no member for "logged in and saw", because this system does
    not authenticate anywhere.
    """

    FAVICON = "favicon_sha256"
    ANALYTICS_ID = "analytics_id"
    TLS_SPKI = "tls_spki_sha256"
    SSH_HOSTKEY = "ssh_hostkey_sha256"
    APP_JS = "app_js_sha256"
    ERROR_404 = "error404_sha256"
    CONTACT_HANDLE = "contact_handle"
    PGP_FINGERPRINT = "pgp_fingerprint"
    WALLET_ADDRESS = "wallet_address"


#: Not an artefact: a social claim read off a page. It is kept separate from the
#: artefact kinds because a vouch is evidence of a relationship between operators,
#: not evidence that two observations are the same operator.
TRUST_STATEMENT = "trust_statement"


#: A value that many services share because a vendor ships it, not because an
#: operator chose it. The farm marks these explicitly; a correlator that treats them
#: as identity evidence merges strangers, which is the finding the paper measures.
STOCK_DEFAULT = "STOCK_DEFAULT"

#: A value that a decoy carries precisely because it is public knowledge, so tying
#: anyone to it would be a false accusation dressed as a finding.
PUBLIC_PLACEHOLDER = "PUBLIC_PLACEHOLDER"

#: Values that appear on a page but identify nobody, whatever their frequency.
NON_IDENTIFYING_VALUES = frozenset({STOCK_DEFAULT, PUBLIC_PLACEHOLDER, ""})


class EdgeMethod(str, Enum):
    """How a link was derived, kept separate because the two are not comparable.

    A deterministic join is a fact about two observations: the same artefact value
    was seen on both. A probabilistic match is an inference over several weak
    signals. Reporting them in one ranked list without saying which is which would
    let an inference borrow the authority of a fact.
    """

    DETERMINISTIC = "deterministic"
    PROBABILISTIC = "probabilistic"
    #: An explicit social claim ("X vouches for Y"). It is not identity evidence and
    #: cannot justify a merge; it is stored so the relationship is visible next to the
    #: identity candidates without being mistaken for one.
    TRUST = "trust"


class EdgeStatus(str, Enum):
    """Where a link sits in the analyst workflow.

    ``POSSIBLE`` is the important one. It exists so a below-threshold pair is
    surfaced rather than dropped, with its score attached, which is what the plan
    means by rendering it as "possible" instead of hiding it.
    """

    POSSIBLE = "possible"
    CANDIDATE = "candidate"
    CONFLICT = "conflict"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Artefact:
    """One observation of one identifying value on one service.

    `evidence_sha256` points at the snapshot the value was read from, so a claim
    that two services share a favicon can be traced to the bytes that show it.
    """

    artefact_id: str
    kind: str
    value: str
    service_id: str
    path: str
    evidence_sha256: str
    observed_at: str

    @property
    def is_identifying(self) -> bool:
        return self.value not in NON_IDENTIFYING_VALUES

    def to_dict(self) -> dict[str, Any]:
        return {
            "artefact_id": self.artefact_id,
            "kind": self.kind,
            "value": self.value,
            "service_id": self.service_id,
            "path": self.path,
            "evidence_sha256": self.evidence_sha256,
            "observed_at": self.observed_at,
            "identifying": self.is_identifying,
        }


@dataclass(frozen=True)
class Evidence:
    """A single reason a link was proposed, or a single reason it was doubted.

    Both directions use this type on purpose. A doubt that cannot carry its evidence
    is not a doubt, and the conflict view exists to show these side by side.
    """

    kind: str
    value: str
    a_service: str
    b_service: str
    weight: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "a_service": self.a_service,
            "b_service": self.b_service,
            "weight": round(self.weight, 6),
            "note": self.note,
        }


@dataclass
class Entity:
    """A candidate operator, not a person.

    The label says what the graph believes so far. `service_ids` is the evidence
    that put those services together; an entity with two services has two
    observations behind it and no more.
    """

    entity_id: str
    label: str
    case_id: str
    status: str = "candidate"
    service_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    # Provenance and freshness, required by the PS's entity-extraction item. The
    # defaults keep older databases readable: rows written before these columns
    # existed come back empty rather than failing to load.
    category: str = ""
    last_scan: str = ""
    source: str = ""
    # Whether this entity has been folded into another. It lives on the model because
    # every caller that asks "can I act on this entity" needs it, and a store method that
    # silently dropped a database column left merge state invisible outside SQL.
    merged_into: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "label": self.label,
            "case_id": self.case_id,
            "status": self.status,
            "service_ids": list(self.service_ids),
            "created_at": self.created_at,
            "category": self.category,
            "last_scan": self.last_scan,
            "source": self.source,
            "merged_into": self.merged_into,
        }


@dataclass
class Edge:
    """A proposed relationship between two entities.

    `supporting` and `counter` are both lists and are both shown. A pair where each
    is non-empty is a conflict, and the UI's job is to display the disagreement, not
    to resolve it by arithmetic.
    """

    edge_id: str
    a_entity: str
    b_entity: str
    method: str
    score: float
    status: str
    supporting: list[Evidence] = field(default_factory=list)
    counter: list[Evidence] = field(default_factory=list)
    created_at: str = ""

    @property
    def has_conflict(self) -> bool:
        return bool(self.supporting) and bool(self.counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "a_entity": self.a_entity,
            "b_entity": self.b_entity,
            "method": self.method,
            "score": round(self.score, 6),
            "status": self.status,
            "supporting": [e.to_dict() for e in self.supporting],
            "counter": [e.to_dict() for e in self.counter],
            "has_conflict": self.has_conflict,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class MergeRecord:
    """A reversible merge.

    The plan requires merges to be reversible and logged. `before_state` is a
    complete snapshot of the two entities as they stood, so reversal restores them
    exactly rather than approximating the undo from a diff.
    """

    merge_id: str
    survivor: str
    absorbed: str
    actor: str
    rationale: str
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: str
    reversed_at: str | None = None
    reversed_by: str | None = None


class GraphError(Exception):
    """Raised when an operation would violate the graph's doctrine."""


class NoSuchEntity(GraphError):
    """Unknown entity id."""


class MergeNotReversible(GraphError):
    """The merge cannot be undone in its current state."""
