"""Validation on real data, where nobody hands you the answer key.

Every number in this repository so far rests on a farm whose truth was generated with
it. That is the right way to measure a detector, and it is useless the moment the input
is a real crawl: real onion services do not ship a `cluster_id`.

The usual response is to skip validation on real data and call the synthetic numbers
"preliminary". This module refuses that, because a system that proposes links about
real people cannot be shipped on the strength of a fixture. It implements the four
things that can be measured on real data without ever inventing a label:

  * **self-declared ground truth** -- operators publish mirror lists, signed
    announcements, and "our other site" banners. Those declarations are labels that
    the operator produced, not the analyst. A link inside a declaration is a true
    positive; a link across two declarations is a demonstrated error.
  * **temporal stability** -- a link caused by a shared operator artefact recurs
    across independent crawls. A link caused by a collision, a CDN default, or a
    copy appears once. Recurrence is measurable without truth.
  * **audit sampling** -- sample proposed links, have a human adjudicate them, and
    report precision with an interval. This is what an intelligence unit does with a
    tool it intends to rely on, and the interval is the honest part.
  * **declared-negative controls** -- pairs drawn from different declarations are
    known negatives. A correlator that links them is measured, not argued about.

None of this proves the system finds every real cluster. It bounds the error rate,
which is the only claim that matters when a false link is an accusation.

Everything here is pure: no network, no Tor, no clock. Callers supply the observations.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Any

Link = tuple[str, str]


def canonical(a: str, b: str) -> Link:
    """Order a pair so the same link is one key regardless of direction."""
    return (a, b) if a <= b else (b, a)


def canonical_links(links: Iterable[Link]) -> set[Link]:
    return {canonical(a, b) for a, b in links}


# --------------------------------------------------------------------------- #
# 1. Self-declared ground truth
# --------------------------------------------------------------------------- #


def score_against_declared(
    proposed: Iterable[Link],
    declared: Mapping[str, Iterable[str]],
    *,
    observable: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Score proposed links against operator declarations.

    ``declared`` maps a declaration id (a mirror-list URL, a signed announcement, an
    "our other site" banner) to the services it names. Only links where *both* ends are
    declared are scored: a link to an undeclared service is unknown, not wrong, and
    counting it as a false positive would punish the correlator for finding something
    the declarations never mentioned.

    ``observable`` restricts the recall denominator to subjects the correlator could
    actually link. A declaration that names a clearnet domain and its onion declares a
    pair that no artefact correlator over onions alone can ever produce; counting it as
    a miss would report a recall the system was never asked to achieve.
    """
    group_of: dict[str, str] = {}
    for group_id, members in declared.items():
        for service in members:
            group_of[service] = group_id

    links = canonical_links(proposed)
    tp = fp = unlabelled = 0
    for a, b in links:
        ga, gb = group_of.get(a), group_of.get(b)
        if ga is None or gb is None:
            unlabelled += 1
        elif ga == gb:
            tp += 1
        else:
            fp += 1

    reachable = None if observable is None else {str(s) for s in observable}
    declared_pairs: set[Link] = set()
    for members in declared.values():
        candidates = sorted(members)
        if reachable is not None:
            candidates = [member for member in candidates if member in reachable]
        for i, a in enumerate(candidates):
            for b in candidates[i + 1 :]:
                declared_pairs.add(canonical(a, b))

    found = links & declared_pairs
    scored = tp + fp
    return {
        "declarations": len(declared),
        "declared_services": len(group_of),
        "declared_pairs": len(declared_pairs),
        "observable": None if observable is None else len(reachable or ()),
        "proposed": len(links),
        "scored": scored,
        "tp": tp,
        "fp": fp,
        "unlabelled": unlabelled,
        "precision": None if scored == 0 else tp / scored,
        "recall": None if not declared_pairs else len(found) / len(declared_pairs),
        "missed_declared_pairs": len(declared_pairs) - len(found),
    }


def declared_negatives(
    services: Sequence[str], declared: Mapping[str, Iterable[str]]
) -> list[Link]:
    """Every pair drawn from two different declarations: a known negative."""
    group_of: dict[str, str] = {}
    for group_id, members in declared.items():
        for service in members:
            group_of[service] = group_id

    ordered = sorted(set(services))
    negatives: list[Link] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            ga, gb = group_of.get(a), group_of.get(b)
            if ga is not None and gb is not None and ga != gb:
                negatives.append(canonical(a, b))
    return negatives


def false_link_rate(proposed: Iterable[Link], negatives: Sequence[Link]) -> dict[str, Any]:
    """How often the correlator links a pair that the declarations separate."""
    links = canonical_links(proposed)
    negative_set = set(negatives)
    hits = links & negative_set
    return {
        "negatives": len(negative_set),
        "linked": len(hits),
        "rate": None if not negative_set else len(hits) / len(negative_set),
        "examples": sorted(hits)[:5],
    }


# --------------------------------------------------------------------------- #
# 2. Temporal stability
# --------------------------------------------------------------------------- #


def temporal_stability(
    windows: Sequence[tuple[str, Iterable[Link]]],
) -> dict[str, Any]:
    """Separate links that recur across independent crawls from one-off collisions.

    ``windows`` is a sequence of ``(label, links)`` in crawl order. A link seen in one
    window may be a genuine artefact, a cached default, or a coincidence; a link seen in
    two independent snapshots is much harder to explain without a shared operator.

    Reports per-link recurrence, the fraction seen in at least two windows, and the
    mean Jaccard overlap between consecutive windows (a drift measure: two crawls of
    the same population should overlap heavily if the method is stable).
    """
    normalised = [(label, canonical_links(links)) for label, links in windows]
    seen: dict[Link, int] = {}
    for _, links in normalised:
        for link in links:
            seen[link] = seen.get(link, 0) + 1

    jaccards: list[float] = []
    for (_, left), (_, right) in pairwise(normalised):
        union = left | right
        jaccards.append(0.0 if not union else len(left & right) / len(union))

    total = len(seen)
    stable = {link: count for link, count in seen.items() if count >= 2}
    return {
        "windows": len(normalised),
        "links_seen": total,
        "stable_links": len(stable),
        "stable_fraction": None if total == 0 else len(stable) / total,
        "recurrence": dict(sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))),
        "mean_jaccard": None if not jaccards else sum(jaccards) / len(jaccards),
        "jaccards": jaccards,
    }


# --------------------------------------------------------------------------- #
# 3. Audit sampling and the interval that makes it honest
# --------------------------------------------------------------------------- #


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval, which stays inside [0, 1] at small n.

    The normal approximation is unusable at the sample sizes an audit can afford --
    it produces intervals that run past 1.0 and collapses to a point at 0 or 100
    percent -- so the interval reported next to an audited precision is this one.
    """
    if total <= 0:
        raise ValueError("interval needs at least one observation")
    if not 0 <= successes <= total:
        raise ValueError("successes must be between 0 and total")
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def audit_sample(
    links: Iterable[Link], size: int, *, seed: int = 0
) -> list[dict[str, Any]]:
    """Draw a reproducible sample of proposed links for human adjudication.

    Deterministic given ``seed`` so an auditor can re-draw the same sample and check
    that the reported precision came from these links. Verdicts are left blank: the
    point of the artifact is that a person fills them in.
    """
    if size <= 0:
        raise ValueError("sample size must be positive")
    unique = sorted(canonical_links(links))
    if size > len(unique):
        raise ValueError("sample larger than the population of links")
    drawn = random.Random(seed).sample(unique, size)
    return [
        {"link": list(link), "verdict": None, "note": ""} for link in sorted(drawn)
    ]


def adjudicated_precision(verdicts: Sequence[bool]) -> dict[str, Any]:
    """Precision from adjudicated verdicts, with its interval and the count behind it."""
    if not verdicts:
        raise ValueError("verdicts must not be empty")
    total = len(verdicts)
    correct = sum(1 for verdict in verdicts if verdict)
    low, high = wilson_interval(correct, total)
    return {
        "adjudicated": total,
        "correct": correct,
        "precision": correct / total,
        "ci95": [round(low, 6), round(high, 6)],
    }


# --------------------------------------------------------------------------- #
# 4. Corroboration across independent artefact classes
# --------------------------------------------------------------------------- #


def require_kinds(
    records: Iterable[Mapping[str, Any]], *, min_kinds: int = 2
) -> dict[str, Any]:
    """Keep only links supported by at least ``min_kinds`` artefact classes.

    One shared artefact is cheap to copy. A service can lift a rival's favicon or paste
    their analytics id to make a naive correlator link them, and that is an attack on
    the operator as much as on the tool. Two independent classes -- say a favicon hash
    *and* a TLS key -- have to be copied together to survive this filter, which is a
    much narrower thing to do by accident.

    ``records`` are mappings with ``a``, ``b`` and ``kinds`` (an iterable of artefact
    class names). Returns the kept and dropped links and why, so the cost in recall is
    visible rather than assumed.
    """
    if min_kinds < 1:
        raise ValueError("min_kinds must be at least 1")
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for record in records:
        kinds = {str(kind) for kind in record.get("kinds", ())}
        item = {"link": [record["a"], record["b"]], "kinds": sorted(kinds)}
        (kept if len(kinds) >= min_kinds else dropped).append(item)
    return {"min_kinds": min_kinds, "kept": kept, "dropped": dropped}


# --------------------------------------------------------------------------- #
# 5. Class strength: not all artefacts are equally cheap to copy
# --------------------------------------------------------------------------- #

#: Artefacts that require control of the service's own key material or code. Copying
#: one means cooperating with the thing it identifies: you cannot match another
#: operator's SSH host key or TLS key without holding that key.
HARD_KINDS: frozenset[str] = frozenset(
    {
        "ssh_hostkey_sha256",
        "tls_spki_sha256",
        "app_js_sha256",
    }
)

#: Artefacts a visitor can read off a page and reproduce in a text editor. They are
#: genuine signals, and they are also the cheapest thing to forge, so one alone is not
#: enough to propose that two services share an operator.
SOFT_KINDS: frozenset[str] = frozenset(
    {
        "favicon_sha256",
        "analytics_id",
        "contact_handle",
        "error404_sha256",
    }
)


def admit_link(kinds: Iterable[str], *, hard: Iterable[str] = HARD_KINDS) -> tuple[bool, str]:
    """Decide whether a set of agreeing artefact classes may support a proposed link.

    The rule is deliberately asymmetric, and it was chosen after measuring the blunt
    alternative. Requiring two classes of any kind keeps precision but throws away every
    cluster whose only shared signal is a hard identifier -- on the reference farm that
    is half the true links, because a shared SSH host key or application bundle hash is
    a single, sufficient piece of evidence.

    So: one hard class is enough, because forging it requires the key or the code. Two
    soft classes are enough, because copying two independent things is a deliberate act
    rather than a default. One soft class is not enough, because that is exactly the
    single copied favicon that a naive correlator treats as identity.

    A class this module does not recognise is treated as soft, which is the
    conservative direction: an unknown signal has to be corroborated.
    """
    hard_set = set(hard)
    present = {str(kind) for kind in kinds}
    hard_hits = present & hard_set
    soft_hits = present - hard_set
    if hard_hits:
        return True, f"hard class: {', '.join(sorted(hard_hits))}"
    if len(soft_hits) >= 2:
        return True, f"{len(soft_hits)} independent soft classes"
    if soft_hits:
        return False, f"single soft class: {', '.join(sorted(soft_hits))}"
    return False, "no agreeing artefact class"


def apply_policy(
    records: Iterable[Mapping[str, Any]],
    *,
    hard: Iterable[str] = HARD_KINDS,
) -> dict[str, Any]:
    """Apply :func:`admit_link` to proposed links and keep the reasons.

    ``records`` are mappings with ``a``, ``b`` and ``kinds``. Keeping the reason on both
    sides of the decision is what makes the output defensible: an analyst can see why a
    link survived, and so can the person it is about.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for record in records:
        kinds = {str(kind) for kind in record.get("kinds", ())}
        admitted, reason = admit_link(kinds, hard=hard)
        item = {
            "link": [record["a"], record["b"]],
            "kinds": sorted(kinds),
            "reason": reason,
        }
        (kept if admitted else dropped).append(item)
    return {"kept": kept, "dropped": dropped, "total": len(kept) + len(dropped)}
