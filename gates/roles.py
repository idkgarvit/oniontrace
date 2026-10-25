"""Roles, permissions and the authorisation check (Part E2.4).

The gate makes a decision from a :class:`Principal` it is *handed*. It never infers a
role from a flag, a file, or an environment variable, because a gate that trusts its own
caller is not a gate.

**Boundary, stated plainly.** Authentication -- establishing that a principal really is
who they claim -- happens *outside* this module and is the deploying agency's
responsibility (SSO, session, badge, whatever it runs). What this module enforces is the
authorisation decision *given* an authenticated principal, which is the part the tool can
actually be held to. The bundled CLI has no authentication, so it is a demonstration
path, not a deployment path; `python -m gates demo` says so where a user will see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from gates.errors import PermissionDenied


class Role(str, Enum):
    """Roles in ascending order of authority (E2.4)."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    CASE_OWNER = "case_owner"
    ADMIN = "admin"


#: Ordered authority. A role carries every permission of the roles below it, which is why
#: the check is a rank comparison rather than a set membership test.
ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.ANALYST: 1,
    Role.CASE_OWNER: 2,
    Role.ADMIN: 3,
}


class Permission(str, Enum):
    """Named permissions. Every gated action names one, so the policy is enumerable."""

    VIEW_CASE = "view_case"
    VIEW_SNAPSHOTS = "view_snapshots"
    VIEW_CANDIDATE_LINKS = "view_candidate_links"
    ADD_SEED = "add_seed"
    START_COLLECTION = "start_collection"
    RUN_QUERY = "run_query"
    RECORD_LINK_ACTION = "record_link_action"
    OPEN_CASE = "open_case"
    CLOSE_CASE = "close_case"
    EXPORT = "export"
    PURGE = "purge"
    READ_AUDIT = "read_audit"


#: The minimum role for each permission. E2.4 fixes three of these explicitly: candidate
#: links need analyst or above, export needs case-owner, purge needs admin.
MINIMUM_ROLE: dict[Permission, Role] = {
    Permission.VIEW_CASE: Role.VIEWER,
    Permission.VIEW_SNAPSHOTS: Role.ANALYST,
    Permission.VIEW_CANDIDATE_LINKS: Role.ANALYST,
    Permission.ADD_SEED: Role.ANALYST,
    Permission.START_COLLECTION: Role.ANALYST,
    Permission.RUN_QUERY: Role.ANALYST,
    Permission.RECORD_LINK_ACTION: Role.ANALYST,
    Permission.OPEN_CASE: Role.CASE_OWNER,
    Permission.CLOSE_CASE: Role.CASE_OWNER,
    Permission.EXPORT: Role.CASE_OWNER,
    Permission.PURGE: Role.ADMIN,
    Permission.READ_AUDIT: Role.ADMIN,
}

@dataclass(frozen=True)
class Principal:
    """An authenticated actor."""

    principal_id: str
    role: Role

    def __post_init__(self) -> None:
        if not self.principal_id or not self.principal_id.strip():
            raise ValueError("a principal needs a non-empty identifier to be auditable")


def require(principal: Principal, permission: Permission) -> None:
    """Raise unless `principal` holds `permission`.

    The role is compared by rank so that admin implies every lower permission, and the
    failure names the required role so the refusal is actionable rather than mysterious.
    """
    required = MINIMUM_ROLE[permission]
    if ROLE_RANK[principal.role] >= ROLE_RANK[required]:
        return
    raise PermissionDenied(
        principal_id=principal.principal_id,
        role=principal.role.value,
        permission=permission.value,
        required=required.value,
    )


def may(principal: Principal, permission: Permission) -> bool:
    """Non-raising form, for rendering decisions (which buttons a view should show)."""
    return ROLE_RANK[principal.role] >= ROLE_RANK[MINIMUM_ROLE[permission]]
