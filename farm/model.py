"""Data model for the ONIONTRACE synthetic farm.

Everything here is designed to be:
  * deterministic -- built from a single integer seed, so a farm is reproducible
    from its seed alone (Part E: "numbers without a reproduction command are opinions")
  * serialisable -- every object emits a plain dict, so the evaluation harness can
    read ground truth without importing this package

Design notes tied to the plan:
  * A flaw is either SINGLE (one service) or CLUSTER (shared across a cluster). The
    CLUSTER scope exists purely so the cross-service correlator has ground truth.
  * Hard negatives are first-class, not an afterthought (Part D4). Every rule that can
    fire needs a fixture that trips its *pattern* without its *meaning*.
  * Abstention cases are first-class (Part D4). A service with no cluster-sibling is
    the correlation-abstention fixture; a query whose answer is absent from the pool
    is the retrieval-abstention fixture.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class FlawScope(str, Enum):
    """Where a flaw is injected, which determines what it can be used to measure."""

    SINGLE = "single"  # one service only -> measures rule recall / FP
    CLUSTER = "cluster"  # shared across a cluster -> measures correlation precision
    ABSENT = "absent"  # deliberately not injected -> measures FP on clean services


class RuleClass(str, Enum):
    """Which detector family a flaw exercises."""

    HTTP_PATH = "http_path"  # reachable endpoint / path exposure
    BANNER = "banner"  # version disclosure
    METADATA = "metadata"  # EXIF and similar embedded data
    FINGERPRINT = "fingerprint"  # hashes/certs/assets shared across services
    CONFIG = "config"  # headers, cookies, ETags, error pages


class PersonaRole(str, Enum):
    """Distinguishes the positive classes from the negative classes.

    The negative classes are the point of the farm: without them precision is
    measured only against true positives, which inflates it (Part D4).
    """

    SUBJECT = "subject"  # a real persona with a known identity
    NEAR_MISS_DECOY = "near_miss_decoy"  # a *different* identity that looks similar
    HARD_NEGATIVE = "hard_negative"  # trips a rule's pattern, not its meaning


class StyleRegime(str, Enum):
    """Controlled writing regime for a persona."""

    PURE_ENGLISH = "pure_english"
    HINGLISH_LIGHT = "hinglish_light"  # ~20% code-mix
    HINGLISH_HEAVY = "hinglish_heavy"  # ~50% code-mix
    TEMPLATE_REUSER = "template_reuser"  # copy-pasted listing templates (hard case)


# --------------------------------------------------------------------------- #
# Persona artefact constants (M3 entity-resolution ground truth)
# --------------------------------------------------------------------------- #

# Artefact keys carried by personas. These are the identifiers an entity-resolution
# step extracts from text (D3: "handles, PGP blocks, BTC/XMR addresses, ...").
PERSONA_ARTIFACT_KEYS: tuple[str, ...] = (
    "pgp_fingerprint",
    "wallet_address",
    "contact_handle",
)

# A widely-published placeholder identifier -- a public exchange deposit address, a
# community PGP key, a shared support handle. Two personas carrying the same
# placeholder are NOT the same person; this is the entity-resolution analogue of the
# correlator's stock-default hard negative, and it is what gives the false-merge rate
# a negative class.
PUBLIC_PLACEHOLDER_ARTIFACT = "PUBLIC_PLACEHOLDER"


# --------------------------------------------------------------------------- #
# Flaws
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HardNegative:
    """A fixture that trips a rule's pattern without tripping its meaning.

    `why` is required and must be a real reason -- if it cannot be written down, the
    fixture is not a hard negative and the rule's precision claim is unsupported.
    """

    fixture_id: str
    flaw_kind: str
    service_id: str
    path: str
    why: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Flaw:
    """One injected opsec flaw, with the ground truth needed to score a detector."""

    flaw_id: str
    kind: str
    scope: FlawScope
    rule_class: RuleClass
    service_ids: list[str]
    path: str
    signal: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scope"] = self.scope.value
        d["rule_class"] = self.rule_class.value
        return d


# --------------------------------------------------------------------------- #
# Endpoints and services
# --------------------------------------------------------------------------- #


@dataclass
class Endpoint:
    """A single served path on a service.

    `is_clean_control` marks endpoints deliberately free of flaws. These are what
    give the false-positive rate a denominator (Part D1: "clean endpoints inside
    flawed services").
    """

    path: str
    body: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    is_clean_control: bool = False

    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["body_sha256"] = self.body_sha256()
        return d


@dataclass
class Service:
    """One synthetic onion service (v3-shaped address).

    The address is a deterministic hash, not a real onion address -- the generator
    never deploys against the live Tor network.
    """

    service_id: str
    cluster_id: str | None
    address: str
    title: str
    topic: str
    endpoints: list[Endpoint] = field(default_factory=list)
    shared_artifacts: dict[str, str] = field(default_factory=dict)
    flaws: list[str] = field(default_factory=list)  # flaw_ids

    @property
    def is_cluster_clean(self) -> bool:
        return not self.flaws

    def endpoint(self, path: str) -> Endpoint | None:
        for ep in self.endpoints:
            if ep.path == path:
                return ep
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "cluster_id": self.cluster_id,
            "address": self.address,
            "title": self.title,
            "topic": self.topic,
            "flaws": list(self.flaws),
            "shared_artifacts": dict(self.shared_artifacts),
            "endpoints": [ep.to_dict() for ep in self.endpoints],
        }


@dataclass
class Cluster:
    """An operator cluster: services believed to share one operator.

    `shared_artifacts` is the ground truth the correlator must recover.
    """

    cluster_id: str
    service_ids: list[str] = field(default_factory=list)
    shared_artifacts: dict[str, str] = field(default_factory=dict)
    persona_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Personas and text
# --------------------------------------------------------------------------- #


@dataclass
class Post:
    """One piece of authored text, with the metadata behavioural features need."""

    post_id: str
    persona_id: str
    service_id: str | None
    text: str
    hour_of_day: int  # 0-23, feeds the posting-hour histogram
    day_index: int
    is_template: bool = False

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def index(self) -> int:
        """This post's position in its persona's corpus, recovered from `post_id`.

        `post_id` is `<persona>-p<NNN>`, zero-padded, so ids sort into generation order.
        Style drift is defined against this index (`Persona.drift_at`), which is what lets
        the evaluation harness ask whether a post was written before or after the drift
        instead of guessing from the text or from post counts.
        """
        return int(self.post_id.rsplit("-p", 1)[1])

    @property
    def length_bucket(self) -> str:
        """Length stratification is required by Part E3 (R@5 long, Hit@3 short)."""
        return "short" if self.word_count < 150 else "long"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["word_count"] = self.word_count
        d["length_bucket"] = self.length_bucket
        d["index"] = self.index
        return d


@dataclass
class Persona:
    """A synthetic author with a *known* identity.

    IMPORTANT (Part D scope limit): because these personas are generated with known
    parameters, a model that recovers them has recovered the generator, not a
    property of human writing. Every metric computed over this class is reported as
    "synthetic persona retrieval", never as authorship attribution.
    """

    persona_id: str
    cluster_id: str | None
    role: PersonaRole
    style: StyleRegime
    handle: str
    # The stylistic fingerprint: held constant per persona so style matching has signal.
    function_words: list[str]
    content_vocabulary: list[str]
    # Style-drift ground truth (D3). `drift_at` is the post index at which this
    # persona's *content* vocabulary switches to `drift_topic`; function words are
    # deliberately untouched. It lives on the model rather than inside the generator
    # because the harness has to be able to ask whether a given query post was written
    # before or after the drift. A harness that instead infers drift from post counts
    # measures the wrong thing while looking like it measured this one.
    drift_at: int | None = None
    drift_topic: str | None = None
    # Behavioural ground truth: the hours this persona actually posts in.
    active_hours: list[int] = field(default_factory=list)
    template_id: str | None = None
    shared_artifacts: dict[str, str] = field(default_factory=dict)
    posts: list[Post] = field(default_factory=list)

    @property
    def total_words(self) -> int:
        return sum(p.word_count for p in self.posts)

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "cluster_id": self.cluster_id,
            "role": self.role.value,
            "style": self.style.value,
            "handle": self.handle,
            "active_hours": list(self.active_hours),
            "template_id": self.template_id,
            "shared_artifacts": dict(self.shared_artifacts),
            "drift_at": self.drift_at,
            "drift_topic": self.drift_topic,
            "post_count": len(self.posts),
            "total_words": self.total_words,
            "posts": [p.to_dict() for p in self.posts] if include_text else [],
        }


# --------------------------------------------------------------------------- #
# Abstention and configuration
# --------------------------------------------------------------------------- #


@dataclass
class AbstentionCase:
    """A query whose correct answer is NOT in the candidate pool.

    The correct behaviour is to say so. Without these, a system can never be scored
    on restraint -- and restraint is the safety story (Part D4).
    """

    case_id: str
    kind: str  # "correlation" | "retrieval"
    query: str
    pool: list[str]
    why_absent: str
    correct_behaviour: str = "abstain"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FarmConfig:
    """Parameters that fully determine a farm, given a seed."""

    seed: int = 20260925
    n_clusters: int = 4
    services_per_cluster: int = 3
    n_singleton_services: int = 2  # no cluster-sibling -> correlation-abstention fixtures
    n_clean_control_services: int = 2  # zero flaws -> FP denominator
    personas_per_cluster: int = 4
    n_near_miss_decoys: int = 4
    n_clean_control_endpoints: int = 6
    n_abstention_retrieval_cases: int = 4
    target_flaw_count: int = 25
    posts_per_persona: int = 12
    #: What fraction of eligible services carry a *widespread* stock default. 1.0 is the
    #: adversarial upper bound the farm was designed around; farm.base_rates records the
    #: measured share so the same experiment can be run at a rate observed in the wild.
    widespread_default_share: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FarmTruth:
    """The complete ground-truth manifest the evaluation harness consumes."""

    config: FarmConfig
    clusters: list[Cluster] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    personas: list[Persona] = field(default_factory=list)
    flaws: list[Flaw] = field(default_factory=list)
    hard_negatives: list[HardNegative] = field(default_factory=list)
    abstention_cases: list[AbstentionCase] = field(default_factory=list)

    # ---------------- derived views used by the harness ---------------- #

    def service(self, service_id: str) -> Service:
        for s in self.services:
            if s.service_id == service_id:
                return s
        raise KeyError(service_id)

    def cluster_mates(self, service_id: str) -> list[str]:
        """True cluster-siblings of a service (the correlator's ground truth)."""
        svc = self.service(service_id)
        if svc.cluster_id is None:
            return []
        return [
            s.service_id
            for s in self.services
            if s.cluster_id == svc.cluster_id and s.service_id != service_id
        ]

    def correlation_pool(self, service_id: str) -> list[str]:
        """Every candidate the correlator may rank for a query service."""
        return [s.service_id for s in self.services if s.service_id != service_id]

    def precision_at_1_baseline(self, *, restricted_to_clustered: bool = True) -> float:
        """Random-guess precision@1 over the correlation pool.

        Part E1 requires this next to every correlation result. Two pool definitions
        exist and both get reported, because they give different numbers:

          * restricted (default) -- the correlator ranks only clustered services.
            For 4 clusters of 3 this is 2/11 ~= 0.182, which is the figure Part E
            quotes.
          * full -- the correlator ranks every other service, singletons included.
            This is the harder and more realistic design, and it is the one where
            singletons act as abstention cases. For 12 clustered + 2 singleton
            services it is 2/13 ~= 0.154.

        Only clustered services are treated as queries: a singleton has no true
        mate, so its correct answer is abstention, not a hit.
        """
        clustered = [s for s in self.services if s.cluster_id is not None]
        if not clustered:
            return 0.0
        pool = len(clustered) - 1 if restricted_to_clustered else len(self.services) - 1
        if pool <= 0:
            return 0.0
        ratios = [len(self.cluster_mates(s.service_id)) / pool for s in clustered]
        return sum(ratios) / len(ratios)

    def clean_services(self) -> list[Service]:
        return [s for s in self.services if s.is_cluster_clean]

    def clean_control_endpoints(self) -> list[tuple[str, Endpoint]]:
        out: list[tuple[str, Endpoint]] = []
        for s in self.services:
            for ep in s.endpoints:
                if ep.is_clean_control:
                    out.append((s.service_id, ep))
        return out

    def flaws_by_scope(self, scope: FlawScope) -> list[Flaw]:
        return [f for f in self.flaws if f.scope == scope]

    def persona_pool(self) -> list[str]:
        """All personas eligible as retrieval candidates, including decoys."""
        return [p.persona_id for p in self.personas]

    def _shared_persona_artifact_pairs(
        self, *, want_placeholder: bool
    ) -> set[tuple[str, str]]:
        """Persona pairs linked by a shared artefact value, split by artefact kind.

        `want_placeholder=False` yields pairs sharing a *genuine* artefact -- the true
        merges an entity-resolution step should find. `want_placeholder=True` yields
        pairs sharing only the widely-published placeholder, which are different
        identities and must NOT be merged.
        """
        pairs: set[tuple[str, str]] = set()
        for i, a in enumerate(self.personas):
            for b in self.personas[i + 1:]:
                for key in set(a.shared_artifacts) & set(b.shared_artifacts):
                    x = a.shared_artifacts[key]
                    y = b.shared_artifacts[key]
                    if x != y:
                        continue
                    if (x == PUBLIC_PLACEHOLDER_ARTIFACT) == want_placeholder:
                        pairs.add((a.persona_id, b.persona_id))
        return pairs

    def true_merge_pairs(self) -> set[tuple[str, str]]:
        """Persona pairs that genuinely share an operator (a real shared artefact)."""
        return self._shared_persona_artifact_pairs(want_placeholder=False)

    def false_merge_pairs(self) -> set[tuple[str, str]]:
        """Pairs a naive exact-match rule would merge, but which are different people.

        This is the negative class for the false-merge rate. Without it, precision is
        measured only against pairs the rule already got right.
        """
        return self._shared_persona_artifact_pairs(want_placeholder=True)

    def retrieval_baselines(self, k: int) -> dict[str, float]:
        """Random baselines for retrieval metrics, from the actual pool size.

        Part E requires these in every row so a Hit@k number cannot be read as
        meaningful when it is close to chance. One relevant item per query.
        """
        pool = len(self.persona_pool())
        if pool == 0:
            return {"pool_size": 0.0, "hit_at_k": 0.0, "recall_at_k": 0.0}
        return {
            "pool_size": float(pool),
            "hit_at_k": min(1.0, k / pool),
            "recall_at_k": min(1.0, k / pool),
        }

    # ---------------- serialisation ---------------- #

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "clusters": [c.to_dict() for c in self.clusters],
            "services": [s.to_dict() for s in self.services],
            "personas": [p.to_dict(include_text=include_text) for p in self.personas],
            "flaws": [f.to_dict() for f in self.flaws],
            "hard_negatives": [h.to_dict() for h in self.hard_negatives],
            "abstention_cases": [a.to_dict() for a in self.abstention_cases],
        }

    def to_json(self, include_text: bool = True, indent: int = 2) -> str:
        return json.dumps(
            self.to_dict(include_text=include_text),
            indent=indent,
            ensure_ascii=False,
        )


# --------------------------------------------------------------------------- #
# Deterministic helpers
# --------------------------------------------------------------------------- #

_ONION_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def deterministic_onion_address(seed: int, label: str, length: int = 56) -> str:
    """Build a stable, obviously-synthetic v3-shaped address.

    This is a hash, not a real v3 address, and the generator never publishes it
    anywhere. It exists so fixtures have stable, reproducible identifiers.
    """
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).digest()
    chars: list[str] = []
    counter = 0
    while len(chars) < length:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        for byte in block:
            chars.append(_ONION_ALPHABET[byte % len(_ONION_ALPHABET)])
            if len(chars) == length:
                break
        counter += 1
    return "".join(chars) + ".onion"


def deterministic_hex(seed: int, label: str, length: int = 64) -> str:
    """Stable hex digest for fingerprints (favicon, cert, SSH host key)."""
    digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).hexdigest()
    while len(digest) < length:
        digest += hashlib.sha256(digest.encode("utf-8")).hexdigest()
    return digest[:length]
