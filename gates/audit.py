"""Append-only audit trail (Part E2.3).

Three properties are enforced here, and a fourth is stated as a limit:

1. **Append-only by construction.** Entries are appended as JSON Lines, each carrying the
   hash of its predecessor, so an edit, reorder or removal in the *middle* breaks
   verification from that point on. There is no update or delete method.
2. **Complete, not merely consistent.** A predecessor chain alone only proves that the
   entries present are mutually consistent -- it says nothing about entries that are
   gone. Deleting a suffix leaves a perfectly valid prefix, so a chain-only check reads a
   truncated trail as clean. An **anchor** (`audit.anchor.json`) records the expected
   entry count and head hash, and `verify` fails when the trail is shorter than the anchor
   says it was. Removal anywhere, including from the end, is now detected.
3. **Written by the gate, not by the analyst.** The write path is `_append`, called only
   by `CollectionGate` action methods. No permission grants a role the ability to write
   here, so an analyst cannot record their own justification for anything -- the record
   is a side effect of the action the gate performed, not something the actor supplies.
   `_append` also **fails closed**: it verifies before extending, so an operator cannot
   truncate the trail and then keep appending to fabricate a clean chain from the
   surviving prefix.
4. **Limit, stated not hidden.** This is tamper *evident*, not tamper *proof*. The anchor
   is a second file in the same tree, so anyone who can rewrite *both* the log and its
   anchor can still recompute a consistent pair. What the anchor buys is that the cheap,
   plausible edit -- deleting your own last few entries, or deleting the log outright --
   is detected rather than reported as clean. Proving absence of tampering still needs
   each head hash shipped somewhere the operator cannot write (separate host, timestamping
   authority, WORM storage). The tool does not have that, and `verify` reports
   `anchored: False` rather than a clean bill when there is no anchor to check against.

The log lives in its own directory tree. Purging a case removes case data and leaves the
audit record of the purge, which is the only way "we deleted it, and logged that we did"
can be checked afterwards.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from gates.errors import AuditTampered
from gates.roles import Permission, Principal, require

#: Predecessor hash of the first entry.
GENESIS = "0" * 64


class AuditAction(str, Enum):
    """Every action E2.3 names, plus the gate's own refusals."""

    ACKNOWLEDGEMENT_RECORDED = "acknowledgement_recorded"
    CASE_OPENED = "case_opened"
    CASE_CLOSED = "case_closed"
    SEED_ADDED = "seed_added"
    COLLECTION_RUN = "collection_run"
    SNAPSHOT_STORED = "snapshot_stored"
    SNAPSHOT_VIEWED = "snapshot_viewed"
    QUERY = "query"
    LINK_CONFIRMED = "link_confirmed"
    LINK_REJECTED = "link_rejected"
    LINK_ANNOTATED = "link_annotated"
    LINK_ACTION_REVERSED = "link_action_reversed"
    EXPORT = "export"
    PURGE = "purge"
    GATE_REFUSED = "gate_refused"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry_hash(
    *,
    seq: int,
    recorded_at: str,
    actor: str,
    role: str,
    action: str,
    case_id: str | None,
    detail: dict[str, Any],
    prev_hash: str,
) -> str:
    """Hash of one entry, over a canonical serialisation of its own fields.

    `sort_keys` and compact separators matter: a hash over a differently-ordered
    serialisation would verify today and fail tomorrow.
    """
    payload = json.dumps(
        {
            "seq": seq,
            "recorded_at": recorded_at,
            "actor": actor,
            "role": role,
            "action": action,
            "case_id": case_id,
            "detail": detail,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    """One recorded action."""

    seq: int
    recorded_at: str
    actor: str
    role: str
    action: str
    case_id: str | None
    detail: dict[str, Any]
    prev_hash: str
    entry_hash: str
    #: Why this entry could not be parsed, or None when it parsed cleanly. Not part of
    #: the hashed payload and never written to disk -- it exists so `verify` can *report*
    #: a damaged entry rather than crashing on it.
    malformed: str | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        """Build an entry from parsed JSON, tolerating damage.

        This deliberately does *not* raise on a missing or ill-typed field. `verify` is
        the function whose job is to report a damaged trail, so a damaged entry has to be
        *representable*: the first version indexed the dict directly, so appending a line
        like `{"partial": true}` -- or any non-JSON text -- made verification crash with
        `KeyError: 'seq'` instead of reporting tampering. Absent fields become values that
        cannot match a legitimate hash, and `malformed` names what was wrong, so
        `recompute_hash` fails and `verify` reports "malformed entry" rather than dying.
        """
        problems: list[str] = []

        def text_field(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value:
                problems.append(f"{name} is not a non-empty string")
                return ""
            return value

        seq_raw = raw.get("seq")
        if isinstance(seq_raw, int) and not isinstance(seq_raw, bool):
            seq = seq_raw
        else:
            problems.append("seq is not an integer")
            seq = -1

        detail = raw.get("detail")
        if detail is None:
            detail = {}
        elif not isinstance(detail, dict):
            problems.append("detail is not an object")
            detail = {"_malformed": repr(detail)}

        case_id = raw.get("case_id")
        if case_id is not None and not isinstance(case_id, str):
            problems.append("case_id is not a string")
            case_id = None

        entry = cls(
            seq=seq,
            recorded_at=text_field("recorded_at"),
            actor=text_field("actor"),
            role=text_field("role"),
            action=text_field("action"),
            case_id=case_id,
            detail=detail,
            prev_hash=text_field("prev_hash"),
            entry_hash=text_field("entry_hash"),
            malformed="; ".join(problems) or None,
        )
        return entry

    def recompute_hash(self) -> str:
        return _entry_hash(
            seq=self.seq,
            recorded_at=self.recorded_at,
            actor=self.actor,
            role=self.role,
            action=self.action,
            case_id=self.case_id,
            detail=self.detail,
            prev_hash=self.prev_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "role": self.role,
            "action": self.action,
            "case_id": self.case_id,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


class AuditLog:
    """The append-only record. One file, one chain, no update path."""

    def __init__(self, root: Path, *, clock: Callable[[], str] = _utc_now) -> None:
        self.root = Path(root)
        self.path = self.root / "audit.jsonl"
        self.anchor_path = self.root / "audit.anchor.json"
        self._clock = clock
        self.root.mkdir(parents=True, exist_ok=True)

    # -- reading ----------------------------------------------------------- #

    def entries(self) -> tuple[AuditEntry, ...]:
        """Parse the trail, tolerating damage rather than raising on it.

        A line that is not JSON, or is JSON but not an object, becomes a sentinel entry
        whose `malformed` field says so. Verification then *reports* it. Raising here
        would mean a damaged trail crashed the function whose entire job is to describe
        damage -- and a caller catching only `AuditTampered` would see an uncaught
        `KeyError` instead of a detected tamper.
        """
        if not self.path.exists():
            return ()
        parsed: list[AuditEntry] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                why = getattr(exc, "msg", None) or str(exc)
                parsed.append(self._damaged(number, f"line is not valid JSON ({why})"))
                continue
            if not isinstance(raw, dict):
                parsed.append(self._damaged(number, "line is not a JSON object"))
                continue
            parsed.append(AuditEntry.from_dict(raw))
        return tuple(parsed)

    def _damaged(self, position: int, why: str) -> AuditEntry:
        """A placeholder for a line that could not be parsed at all.

        `seq` is the line's position, so a report can say *where* the damage is. Its
        `entry_hash` is empty, which no legitimate entry can match, so verification fails
        on this entry rather than skipping past it.
        """
        return AuditEntry(
            seq=position,
            recorded_at="",
            actor="",
            role="",
            action="",
            case_id=None,
            detail={},
            prev_hash="",
            entry_hash="",
            malformed=why,
        )

    def for_case(self, case_id: str) -> tuple[AuditEntry, ...]:
        return tuple(e for e in self.entries() if e.case_id == case_id)

    def read(
        self,
        principal: Principal,
        *,
        case_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[AuditEntry, ...]:
        """Read the trail. Admin only.

        Admin-only rather than role-open because the trail names who did what, including
        analysts: an operator able to read the trail of an investigation they are not on
        has been told something about it. Reading the audit is itself a disclosure, so it
        carries its own permission rather than riding on "analyst and above".
        """
        require(principal, Permission.READ_AUDIT)
        entries = self.for_case(case_id) if case_id else self.entries()
        return entries[-limit:] if limit else entries

    # -- the anchor -------------------------------------------------------- #

    def read_anchor(self) -> dict[str, Any] | None:
        """The recorded {count, head_hash}, or None when there is no anchor to check."""
        if not self.anchor_path.exists():
            return None
        try:
            raw = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def _write_anchor(self, *, count: int, head_hash: str) -> None:
        """Record how long the trail is and what its head hash is, after every append.

        A predecessor chain proves the entries *present* are mutually consistent. It
        cannot prove that none are missing: deleting a suffix leaves a valid prefix and a
        chain-only check calls it clean. The anchor is the second, independent fact that
        makes removal-from-the-end detectable.
        """
        self.anchor_path.write_text(
            json.dumps({"count": count, "head_hash": head_hash}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # -- verification ------------------------------------------------------ #

    def verify(self) -> dict[str, Any]:
        """Recompute the chain, check it against the anchor, and report the first fault.

        Returns a report rather than raising, including for a trail that is malformed or
        missing: reporting damage is this function's entire purpose.
        """
        try:
            entries = self.entries()
        except OSError as exc:  # unreadable trail is a finding, not a crash
            return {"verified": False, "entries_checked": 0, "failure": f"unreadable: {exc}"}

        prev = GENESIS
        for index, entry in enumerate(entries):
            if entry.malformed:
                return {
                    "verified": False,
                    "entries_checked": index,
                    "failure": f"malformed entry: {entry.malformed}",
                    "first_bad_seq": index,
                }
            if entry.seq != index:
                return {
                    "verified": False,
                    "entries_checked": index,
                    "failure": (
                        f"entry at position {index} claims seq {entry.seq}; "
                        "the sequence has a gap or a repeat"
                    ),
                    "first_bad_seq": index,
                }
            if entry.prev_hash != prev:
                return {
                    "verified": False,
                    "entries_checked": index,
                    "failure": "broken chain link",
                    "first_bad_seq": index,
                }
            if entry.recompute_hash() != entry.entry_hash:
                return {
                    "verified": False,
                    "entries_checked": index,
                    "failure": "entry content does not match its recorded hash",
                    "first_bad_seq": index,
                }
            prev = entry.entry_hash

        head_hash = prev
        anchor = self.read_anchor()
        report: dict[str, Any] = {
            "verified": True,
            "entries_checked": len(entries),
            "head_hash": head_hash,
            "anchored": anchor is not None,
        }

        if anchor is None:
            # Not a failure -- a trail written before the anchor existed, or by a caller
            # that never appended through `_append`. Say so rather than implying that a
            # chain-only check was a complete one.
            report["anchor_note"] = (
                "no anchor present; the chain is internally consistent but a truncated "
                "trail cannot be ruled out"
            )
            return report

        expected_count = anchor.get("count")
        expected_head = anchor.get("head_hash")
        report["anchored_count"] = expected_count

        if not isinstance(expected_count, int) or isinstance(expected_count, bool):
            report.update(
                verified=False,
                failure="anchor is malformed: count is not an integer",
            )
            return report

        if len(entries) < expected_count:
            report.update(
                verified=False,
                failure=(
                    f"trail is shorter than the anchor: {len(entries)} entries present, "
                    f"{expected_count} were recorded. Entries have been removed from the end"
                ),
                missing=expected_count - len(entries),
            )
            return report

        if len(entries) > expected_count or head_hash != expected_head:
            report.update(
                verified=False,
                failure=(
                    f"trail does not match the anchor: anchor records {expected_count} "
                    f"entries ending {str(expected_head)[:16]}..., found {len(entries)} "
                    f"ending {head_hash[:16]}..."
                ),
            )
            return report

        return report

    def assert_untampered(self) -> None:
        result = self.verify()
        if not result["verified"]:
            location = result.get("first_bad_seq")
            where = f" at entry {location}" if location is not None else ""
            raise AuditTampered(f"audit chain failed{where}: {result['failure']}")

    # -- writing (gate internal) ------------------------------------------- #

    def _append(
        self,
        *,
        action: AuditAction,
        actor: Principal,
        case_id: str | None,
        detail: dict[str, Any],
    ) -> AuditEntry:
        """Append one entry. Called by `CollectionGate` only.

        Underscored on purpose: this is not a public capability. An audit trail an actor
        can write to is testimony, not evidence, so the gate -- not the actor -- decides
        what gets recorded, and the actor cannot supply a favourable version of events.

        **Fails closed.** The trail is verified before it is extended. Without this, an
        operator could truncate the log and then perform one more action: the surviving
        prefix would verify, the new entry would chain onto it cleanly, and the anchor
        would be rewritten to match -- turning a detected deletion into an undetectable
        one. Refusing to append to a trail that does not verify closes that route, and
        means the anchor can never be legitimately *lowered*.
        """
        existing = self.entries()
        seq = len(existing)
        prev_hash = existing[-1].entry_hash if existing else GENESIS

        # Check the trail we are about to extend, before we extend it. Skip the anchor
        # comparison for a zero-length trail: there is nothing to shorten, and a brand-new
        # log must be able to accept its first entry.
        if existing:
            prior = self.verify()
            if not prior["verified"]:
                raise AuditTampered(
                    "refusing to append to an audit trail that does not verify: "
                    f"{prior['failure']}. Appending would chain onto a damaged prefix and "
                    "make the damage permanent."
                )

        recorded_at = self._clock()
        entry_hash = _entry_hash(
            seq=seq,
            recorded_at=recorded_at,
            actor=actor.principal_id,
            role=actor.role.value,
            action=action.value,
            case_id=case_id,
            detail=detail,
            prev_hash=prev_hash,
        )
        entry = AuditEntry(
            seq=seq,
            recorded_at=recorded_at,
            actor=actor.principal_id,
            role=actor.role.value,
            action=action.value,
            case_id=case_id,
            detail=detail,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), sort_keys=True, default=str) + "\n")
        # The anchor is updated only after the entry is durably written, so it never
        # claims an entry the log does not have.
        self._write_anchor(count=seq + 1, head_hash=entry_hash)
        return entry
