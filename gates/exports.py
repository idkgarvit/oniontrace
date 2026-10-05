"""Watermarked evidence packs (Part E2.4).

An export is the point where the tool's output leaves the system that governs it, so it
carries its own provenance: case id, the analyst who exported it, the timestamp, and a
retrieval reference back to the audit entry that recorded the export. Anyone holding the
page can name the case it came from and the action that produced it.

The document is rendered through `presentation.render_candidate`, which means the
identity-assertion lint applies to exports exactly as it applies to the screen. An export
path that bypassed the UI's wording rules would be the one route by which the tool's
output could assert something the tool is forbidden to assert -- so there is no separate
export-only renderer to bypass.

The pack is rendered as Markdown because the M6-lite PDF writer (WP2.6) does not exist
yet. The watermark is embedded in the *content*, not in a page header the renderer owns,
so it survives conversion to any format that preserves text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from gates.cases import Case
from gates.presentation import (
    CandidateLink,
    assert_safe,
    render_abstention,
    render_candidate,
)

#: Repeated in the pack so the reminder travels with the document. Phrased without any
#: of the banned assertion families (E2.5) -- "does not assert a common author", not
#: "does not assert they are the same person", because the latter trips the very lint
#: this document must pass.
STANDING_REMINDER = (
    "Candidate links are proposals for analyst review. They are not findings, and this "
    "document does not assert that any two artefacts have a common author."
)


@dataclass(frozen=True)
class Watermark:
    """Identifying marks carried by an export."""

    case_id: str
    analyst_id: str
    generated_at: str
    retrieval_reference: str

    def as_line(self) -> str:
        return (
            f"WATERMARK case={self.case_id} analyst={self.analyst_id} "
            f"generated_at={self.generated_at} retrieval_reference={self.retrieval_reference}"
        )


@dataclass(frozen=True)
class ExportDecision:
    """An analyst's recorded action on one candidate, as it appears in the pack."""

    link: CandidateLink
    decision: str | None
    decided_by: str | None
    reason: str | None = None


def build_evidence_pack(
    *,
    case: Case,
    watermark: Watermark,
    decisions: Sequence[ExportDecision],
    confirm_threshold: float,
    snapshot_count: int,
) -> str:
    """Render the evidence pack for one case.

    `decisions` carries both the candidates and whatever an analyst has recorded about
    each. A pack that showed candidates without their decisions, or decisions without
    their author, would lose the distinction between what the tool proposed and what a
    person concluded.
    """
    lines: list[str] = [watermark.as_line(), ""]

    lines += [
        f"# Evidence pack -- case {case.case_id}",
        "",
        f"- Title: {case.title}",
        f"- Owning analyst: {case.owner}",
        f"- Authorisation reference: {case.authorisation_ref}",
        f"- Opened: {case.opened_at}",
        f"- Retention until: {case.retention_until}",
        f"- Sealed: {case.sealed_at or 'no'}",
        f"- Purged: {case.purged_at or 'no'}",
        f"- Snapshots held: {snapshot_count}",
        "",
        f"> {STANDING_REMINDER}",
        "",
        "## Candidate links",
        "",
    ]

    if not decisions:
        lines += [render_abstention(case.case_id, confirm_threshold=confirm_threshold), ""]
    else:
        for item in decisions:
            lines.append(
                render_candidate(
                    item.link,
                    confirm_threshold=confirm_threshold,
                    decision=item.decision,
                )
            )
            if item.decision:
                author = item.decided_by or "unrecorded"
                suffix = f" (reason: {item.reason})" if item.reason else ""
                lines.append(f"  recorded: {item.decision} by {author}{suffix}")
            lines.append("")

    lines += [
        "## Governing statement",
        "",
        "This export is governed by ACCEPTABLE_USE.md: authorised investigative use only, "
        "no targeting of any individual without lawful authority, and no redistribution of "
        "ingested content.",
        "",
        watermark.as_line(),
        "",
    ]

    pack = "\n".join(lines)
    # The pack is linted as a whole as well as per candidate: the surrounding prose is
    # written by us and could drift into asserting something the candidates do not.
    assert_safe(pack)
    return pack
