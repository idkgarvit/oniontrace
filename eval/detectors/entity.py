"""Entity resolution (M3).

Proposes candidate merges between personas from extracted artefacts, and is scored
against the farm's known true and false merge pairs.

The negative class is the point. Every decoy carries the same widely-published
placeholder handle -- a public exchange address, a community key -- so an exact-match
rule merges all of them. Those merges are false, and measuring them is what makes the
false-merge rate meaningful rather than decorative.

Two variants, matching the two defences a real implementation would need:

  * **exact match** -- merge on any shared artefact value. This is the obvious
    implementation and the placeholder defeats it.
  * **placeholder-blocklisted** -- ignore values known to be widely published. This
    removes the placeholder false merges, at the cost of requiring a maintained
    blocklist. It does nothing for a *novel* shared value, which is a stated limit.

Per Part E2.5, this module proposes **candidate links with scores and evidence**. It
never asserts identity, and nothing here auto-merges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from farm.model import PUBLIC_PLACEHOLDER_ARTIFACT


@dataclass(frozen=True)
class CandidateLink:
    """A proposed merge between two personas, with the evidence behind it."""

    left: str
    right: str
    score: float
    shared_keys: tuple[str, ...]
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "score": round(self.score, 6),
            "shared_keys": list(self.shared_keys),
            "evidence": self.evidence,
        }


def propose_merges(
    farm,
    *,
    artifacts: dict[str, dict[str, str]] | None = None,
    block_placeholder: bool = False,
) -> list[CandidateLink]:
    """Propose candidate persona merges, ranked by score.

    `block_placeholder=True` drops artefact values known to be widely published. The
    blocklist is the only defence against a shared placeholder; it is also the reason
    a real deployment has to maintain one, which is a cost worth stating plainly.

    `artifacts` lets a caller supply what was actually extracted from text rather than
    reading the farm's ground-truth map directly. Omit it to score the resolution
    logic in isolation from extraction quality.
    """
    if artifacts is None:
        artifacts = {p.persona_id: dict(p.shared_artifacts) for p in farm.personas}

    persona_ids = sorted(artifacts)
    links: list[CandidateLink] = []

    for i, left in enumerate(persona_ids):
        for right in persona_ids[i + 1:]:
            left_artifacts = artifacts[left]
            right_artifacts = artifacts[right]
            comparable = sorted(set(left_artifacts) & set(right_artifacts))
            if not comparable:
                continue

            shared: list[str] = []
            for key in comparable:
                value = left_artifacts[key]
                if value != right_artifacts[key]:
                    continue
                if block_placeholder and value == PUBLIC_PLACEHOLDER_ARTIFACT:
                    continue
                shared.append(key)

            if not shared:
                continue

            links.append(
                CandidateLink(
                    left=left,
                    right=right,
                    score=len(shared) / len(comparable),
                    shared_keys=tuple(shared),
                    evidence="; ".join(f"{k}={left_artifacts[k]}" for k in shared),
                )
            )

    links.sort(key=lambda link: (-link.score, link.left, link.right))
    return links


def evaluate(
    farm,
    *,
    threshold: float = 0.0,
    block_placeholder: bool = False,
) -> dict[str, Any]:
    """Score proposed merges against the farm's true and false merge pairs.

    Returns the full link-scoring breakdown plus the decoy-specific failure count, so
    the report can state how often the system would have merged two different people.
    """
    from eval.metrics import score_links

    links = propose_merges(farm, block_placeholder=block_placeholder)
    proposed = [(link.left, link.right) for link in links if link.score >= threshold]

    truth = farm.true_merge_pairs()
    false_pairs = farm.false_merge_pairs()

    universe = {
        (a, b)
        for i, a in enumerate(sorted(p.persona_id for p in farm.personas))
        for b in sorted(p.persona_id for p in farm.personas)[i + 1:]
    }

    scoring = score_links(proposed, truth, universe=universe, threshold=threshold)
    proposed_set = {tuple(sorted(p)) for p in proposed}
    false_merges_on_decoys = len(proposed_set & {tuple(sorted(p)) for p in false_pairs})

    return {
        "variant": "placeholder-blocklisted" if block_placeholder else "exact-match",
        "links": scoring.to_dict(),
        "proposed_links": [link.to_dict() for link in links],
        "true_merge_pairs": len(truth),
        "decoy_false_pairs": len(false_pairs),
        "false_merges_on_decoys": false_merges_on_decoys,
        "decoy_false_merge_rate": (
            false_merges_on_decoys / len(false_pairs) if false_pairs else 0.0
        ),
    }
