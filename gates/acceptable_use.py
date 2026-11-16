"""The acceptable-use gate (Part E2.6).

`ACCEPTABLE_USE.md` is shipped with the tool and surfaced before anything else runs. The
statement is not a licence file nobody reads: acknowledging it is a precondition, recorded
once per deployment, and collection is refused until it is recorded *and* a case exists.

Recorded once per deployment rather than per case on purpose. An acknowledgement that has
to be clicked before every run is a click, and after the third one it is not read at all;
a single recorded acknowledgement naming the person who accepted it is a statement with an
author, which is the thing that can be pointed at later.
"""

from __future__ import annotations

from . import permissions

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from gates.cases import utc_now
from gates.errors import GateRefused
from gates.roles import Principal

#: The statement lives at the repository root so it is visible without running anything.
STATEMENT_PATH = Path(__file__).resolve().parent.parent / "ACCEPTABLE_USE.md"


def statement_text() -> str:
    """The acceptable-use statement as shipped."""
    if not STATEMENT_PATH.exists():  # pragma: no cover - packaging guard
        raise GateRefused(
            "E2.6",
            f"ACCEPTABLE_USE.md is missing from {STATEMENT_PATH}; refusing to run without "
            "the statement that governs use",
        )
    return STATEMENT_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Acknowledgement:
    """Who accepted the statement, and when."""

    principal_id: str
    role: str
    acknowledged_at: str
    statement_digest: str


class AcknowledgementStore:
    """Persists the single deployment-level acknowledgement."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "acceptable-use-ack.json"
        permissions.secure_dir(self.root)

    def is_acknowledged(self) -> bool:
        return self.path.exists()

    def read(self) -> Acknowledgement | None:
        if not self.path.exists():
            return None
        return Acknowledgement(**json.loads(self.path.read_text(encoding="utf-8")))

    def require(self) -> Acknowledgement:
        """Fetch the acknowledgement or refuse with the rule that stopped the run."""
        record = self.read()
        if record is None:
            raise GateRefused(
                "E2.6",
                "collection is refused until ACCEPTABLE_USE.md is acknowledged; run "
                "`python -m gates acceptable-use` to read it, then "
                "`python -m gates ack --accept --actor <name>` to record it",
            )
        return record

    def record(self, principal: Principal, *, now: str | None = None) -> Acknowledgement:
        """Record the acknowledgement, keeping the digest of the version accepted."""
        record = Acknowledgement(
            principal_id=principal.principal_id,
            role=principal.role.value,
            acknowledged_at=now or utc_now(),
            statement_digest=statement_digest(),
        )
        # Names a person and the digest of the statement they accepted.
        permissions.secure_write_text(
            self.path, json.dumps(asdict(record), indent=2, sort_keys=True)
        )
        return record


def statement_digest() -> str:
    """Digest of the statement text as accepted.

    Storing this means that if the statement is later edited, the recorded
    acknowledgement can be shown to refer to an earlier version rather than silently
    appearing to cover the new one.
    """
    return hashlib.sha256(statement_text().encode("utf-8")).hexdigest()[:16]
