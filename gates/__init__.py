"""Part E2 deployment gates: authorised deployment and analyst safety.

Everything here exists to answer one question that a tool producing candidate identities
has to answer before anyone relies on it: *who is allowed to do this, to whom, and what is
left behind afterwards?*

  * `roles`          -- roles, permissions, and the authorisation decision (E2.4)
  * `cases`          -- cases, per-case seeds, retention, purge (E2.2)
  * `audit`          -- append-only hash-chained trail (E2.3)
  * `presentation`   -- candidate rendering, banned identity assertions, abstention (E2.5)
  * `exports`        -- watermarked evidence packs (E2.4)
  * `acceptable_use` -- the statement and its acknowledgement gate (E2.6)
  * `gate`           -- the chokepoint: signed collection permits, every action logged

The plan puts these early rather than last (Part G item 6) because they are the difference
between a tool that can be handed over and a liability. The acquisition package was built
against this seam from its first commit: it cannot store a byte without a permit, an
unacknowledged case, or a principal who owns the case.
"""

from __future__ import annotations

from gates.acceptable_use import Acknowledgement, AcknowledgementStore, statement_text
from gates.audit import AuditAction, AuditEntry, AuditLog
from gates.cases import Case, CaseStore, LinkDecision
from gates.errors import (
    AuditTampered,
    CaseError,
    GateRefused,
    IdentityAssertion,
    PermissionDenied,
    PresentationError,
)
from gates.exports import ExportDecision, Watermark, build_evidence_pack
from gates.gate import PERMIT_TTL_MINUTES, CollectionGate, CollectionPermit
from gates.presentation import (
    BANNED_PATTERNS,
    STATE_LABEL,
    CandidateLink,
    LinkState,
    assert_safe,
    lint,
    render_abstention,
    render_candidate,
    state_for,
)
from gates.roles import ROLE_RANK, Permission, Principal, Role, may, require

__all__ = [
    "Acknowledgement",
    "AcknowledgementStore",
    "AuditAction",
    "AuditEntry",
    "AuditLog",
    "AuditTampered",
    "BANNED_PATTERNS",
    "CandidateLink",
    "Case",
    "CaseError",
    "CaseStore",
    "CollectionGate",
    "CollectionPermit",
    "ExportDecision",
    "GateRefused",
    "IdentityAssertion",
    "LinkDecision",
    "LinkState",
    "PERMIT_TTL_MINUTES",
    "Permission",
    "PermissionDenied",
    "PresentationError",
    "Principal",
    "ROLE_RANK",
    "Role",
    "STATE_LABEL",
    "Watermark",
    "assert_safe",
    "build_evidence_pack",
    "lint",
    "may",
    "render_abstention",
    "render_candidate",
    "require",
    "state_for",
    "statement_text",
]

__version__ = "0.1.0"