"""Presentation rules (Part E2.5).

The interface carries the safety burden, because that is where an investigator forms a
belief. Everything else in this package constrains what the system *does*; this module
constrains what it *says*, which is the part that ends up in a report and in someone's
head.

Two mechanisms, both deliberately blunt:

1. **A banned-term lint.** E2.5 names three families that must not appear anywhere in
   candidate output -- "match", "same person", and "identified" -- so they are checked by
   regex against every rendered string, and a hit raises rather than warns. If a detector
   is called "favicon match", rendering its result fails, which is the intended outcome:
   the fix is to rename the method to what it actually establishes ("shared favicon
   hash"), not to soften the check.
2. **States that cannot imply confirmation.** A tool that never asserts identity has to
   be able to say so while still showing something, so every candidate carries a state
   drawn from a fixed set, and the phrase "confirmed by" is attached to exactly one of
   them -- the one an analyst created by their own recorded action.

The abstention state is a first-class output, not an empty list. "No candidate above
threshold" is a finding about the case, and a system that renders it as a blank panel has
made not-knowing look like a bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from gates.errors import IdentityAssertion, PresentationError

#: What a rendered candidate may never say (E2.5). The verb forms are matched by stem so
#: that "matched", "matching" and "matches" cannot slip through a check written for
#: "match". "identity" is deliberately *not* matched by the `identif` stem: it is the name
#: of module M3 and a legitimate noun, whereas "identified" is the assertion being banned.
BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("match", r"\bmatch(?:es|ed|ing|er)?\b"),
    ("same person", r"\bsame\s+(?:person|individual|actor|operator|user|subject)\b"),
    ("is the same", r"\bis\s+the\s+same\b"),
    ("identified", r"\bidentif(?:y|ies|ied|ication|iable)\b"),
)


class LinkState(str, Enum):
    """The only states a candidate link may be rendered in."""

    POSSIBLE = "possible"
    UNCONFIRMED = "unconfirmed"
    ANALYST_CONFIRMED = "analyst_confirmed"
    ANALYST_REJECTED = "analyst_rejected"


#: The label rendered for each state. Kept as a dict so that adding a state without
#: deciding how it may be labelled is a KeyError rather than a silent default.
STATE_LABEL: dict[LinkState, str] = {
    LinkState.POSSIBLE: "possible (below confirm threshold, unconfirmed)",
    LinkState.UNCONFIRMED: "unconfirmed (awaiting analyst decision)",
    LinkState.ANALYST_CONFIRMED: "confirmed by analyst decision",
    LinkState.ANALYST_REJECTED: "rejected by analyst decision",
}

#: The one phrase that may only appear alongside an analyst's own recorded decision.
ANALYST_ONLY_PHRASE = "confirmed by"


def lint(text: str) -> list[str]:
    """Return the banned terms present in `text` (empty when it is clean)."""
    found: list[str] = []
    for name, pattern in BANNED_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.append(name)
    return found


def assert_safe(text: str) -> None:
    """Raise if the text asserts identity in any of the ways E2.5 forbids."""
    found = lint(text)
    if found:
        raise IdentityAssertion(
            "E2.5: rendered output contains a banned identity assertion "
            f"({', '.join(repr(f) for f in found)}). The strongest permitted string is "
            "'candidate link' with a score, a method and evidence references. "
            f"Offending text: {text!r}"
        )


@dataclass(frozen=True)
class CandidateLink:
    """One proposed link between two artefacts, with the evidence behind it.

    `counter_evidence` is not optional decoration: when a detector has evidence pointing
    the other way, the analyst has to see it, or the view has been curated into a
    conclusion the evidence does not support.
    """

    case_id: str
    left: str
    right: str
    score: float
    method: str
    evidence_refs: tuple[str, ...]
    counter_evidence: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.evidence_refs:
            raise PresentationError(
                "E2.5: a candidate link without evidence references is not reportable"
            )


def state_for(
    score: float,
    *,
    confirm_threshold: float,
    decision: str | None = None,
) -> LinkState:
    """Which state this candidate renders in.

    Above the threshold but undecided is still *unconfirmed*: clearing a score bar is not
    a human judgement, and only the analyst's recorded action produces the state that
    says so.
    """
    if decision == "confirmed":
        return LinkState.ANALYST_CONFIRMED
    if decision == "rejected":
        return LinkState.ANALYST_REJECTED
    if decision is None:
        return (
            LinkState.UNCONFIRMED if score >= confirm_threshold else LinkState.POSSIBLE
        )
    raise PresentationError(f"unknown analyst decision {decision!r}")


def render_candidate(
    link: CandidateLink,
    *,
    confirm_threshold: float,
    decision: str | None = None,
) -> str:
    """Render one candidate link, then refuse to return it if it asserts identity.

    The lint runs on the finished string rather than on its parts, so the check covers
    the assembled output including anything interpolated from a detector's method name.
    """
    state = state_for(link.score, confirm_threshold=confirm_threshold, decision=decision)
    label = STATE_LABEL[state]

    lines = [
        f"Candidate link -- score {link.score:.4f} (confirm threshold {confirm_threshold:.2f})",
        f"  state: {label}",
        f"  pair: {link.left} <-> {link.right}",
        f"  method: {link.method}",
        f"  evidence: {', '.join(link.evidence_refs)}",
    ]
    if link.counter_evidence:
        lines.append(f"  counter-evidence: {'; '.join(link.counter_evidence)}")
    else:
        lines.append("  counter-evidence: none recorded for this pair")
    lines.append(
        "  this is a proposed link for analyst review, not a statement of fact"
    )

    text = "\n".join(lines)
    assert_safe(text)

    # "confirmed by" belongs to exactly one state: the one an analyst created. Any other
    # state that emits it would be claiming a human decision that never happened.
    if state is not LinkState.ANALYST_CONFIRMED and ANALYST_ONLY_PHRASE in text:
        raise IdentityAssertion(
            "E2.5: state "
            f"{state.value!r} rendered the phrase {ANALYST_ONLY_PHRASE!r}, which may only "
            "accompany a recorded analyst decision"
        )
    return text


def render_abstention(case_id: str, *, confirm_threshold: float) -> str:
    """The reachable "nothing to show" state -- a finding, rendered as one.

    Deliberately not an empty string or a blank panel: a system that renders
    not-knowing as nothing has made a correct refusal look like a failure, and an analyst
    who cannot tell the two apart will lower the threshold until something appears.
    """
    text = "\n".join(
        [
            f"Case {case_id}: no candidate link above the confirm threshold "
            f"({confirm_threshold:.2f}).",
            "  state: abstained -- no candidate is proposed for this query",
            "  this is a result, not an error: the tool has nothing above threshold to "
            "put in front of you",
        ]
    )
    assert_safe(text)
    return text


def confirm_threshold_reached(score: float, *, confirm_threshold: float) -> bool:
    """Whether a score is strong enough to be worth an analyst's attention at all."""
    return score >= confirm_threshold