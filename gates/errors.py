"""Exceptions raised by the Part E2 deployment gates.

Every refusal carries the reason and the rule it came from, because a gate that simply
returns False is indistinguishable from a bug. The messages are written to be quotable in
an audit log and in a report.
"""

from __future__ import annotations


class GateRefused(Exception):
    """A deployment gate blocked an action.

    Raised for the E2.2 preconditions: no acknowledgement, no case, no authorisation
    reference, an expired case, or a permit that does not verify. This is the "no case,
    no collection" refusal, and it is deliberately a hard error rather than a warning --
    a collector that can be started with a flag is not gated.
    """

    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        super().__init__(f"[{rule}] {message}")


class PermissionDenied(GateRefused):
    """The principal's role does not carry the permission the action requires (E2.4)."""

    def __init__(self, principal_id: str, role: str, permission: str, required: str) -> None:
        self.principal_id = principal_id
        self.role = role
        self.permission = permission
        self.required = required
        super().__init__(
            "E2.4",
            f"{principal_id!r} holds role {role!r}, which does not carry {permission!r} "
            f"(requires {required!r} or above)",
        )


class IdentityAssertion(Exception):
    """Presentation tried to assert identity, which E2.5 forbids.

    Raised by the presentation lint rather than by a reviewer noticing. The rule is that
    the strongest string the tool may emit is "candidate link" with a score, a method and
    evidence references -- so an attempt to emit anything stronger is a defect that stops
    the output rather than a wording preference.
    """


class PresentationError(Exception):
    """Rendered candidate output is malformed (E2.5)."""


class AuditTampered(Exception):
    """The append-only audit chain does not verify (E2.3)."""


class CaseError(GateRefused):
    """A case operation is invalid: unknown case, already purged, missing field."""
