"""Provenance and tamper detection (E4).

Builds a hash ledger over every captured snapshot and verifies it, so the report can
make a claim it can actually support.

WHAT THIS DOES **NOT** ESTABLISH, stated first because it is the easy mistake:
SHA-256 establishes byte consistency **relative to a recorded digest**. It does not
establish source truth, authorship, collection time, or admissibility. An adversary who
can rewrite both the snapshots and the ledger can recompute consistent hashes. The
report therefore says *"provenance and tamper detection under stated assumptions"* and
never *"chain of custody"* (Part E4).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

LEDGER_VERSION = 1


@dataclass(frozen=True)
class LedgerEntry:
    """One snapshot's recorded digest and the metadata needed to re-derive it."""

    key: str  # "<service_id><path>"
    service_id: str
    path: str
    sha256: str
    status: int
    method: str = "GET"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_digest(endpoint) -> str:
    """Digest of a snapshot's captured bytes.

    The status code is folded into the hash because a 404 page and a 200 page with the
    same body are different observations, and a ledger that ignored the code would let
    one be substituted for the other undetected.
    """
    payload = f"{endpoint.status}\n{endpoint.body}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_ledger(farm) -> dict[str, Any]:
    """Record a digest for every endpoint of every service."""
    entries: list[LedgerEntry] = []
    for service in farm.services:
        for endpoint in service.endpoints:
            entries.append(
                LedgerEntry(
                    key=f"{service.service_id}{endpoint.path}",
                    service_id=service.service_id,
                    path=endpoint.path,
                    sha256=snapshot_digest(endpoint),
                    status=endpoint.status,
                )
            )
    entries.sort(key=lambda e: e.key)
    digests = "".join(e.sha256 for e in entries)
    return {
        "ledger_version": LEDGER_VERSION,
        "seed": farm.config.seed,
        "entry_count": len(entries),
        "entries": [e.to_dict() for e in entries],
        # A digest over the digests, so the ledger itself is checkable in one value.
        "manifest_sha256": hashlib.sha256(digests.encode("utf-8")).hexdigest(),
        "assumptions": (
            "Byte consistency relative to this recorded digest. Does NOT establish "
            "source truth, authorship, or admissibility. An adversary able to rewrite "
            "both snapshots and ledger can produce a consistent forgery."
        ),
    }


def verify_ledger(farm, ledger: dict[str, Any]) -> dict[str, Any]:
    """Re-derive every digest and report mismatches.

    Returns the verification result plus the specific entries that failed, so a failure
    is diagnosable rather than merely red.
    """
    recorded = {e["key"]: e for e in ledger.get("entries", [])}

    missing: list[str] = []
    mismatched: list[dict[str, Any]] = []
    checked = 0

    for service in farm.services:
        for endpoint in service.endpoints:
            key = f"{service.service_id}{endpoint.path}"
            entry = recorded.get(key)
            if entry is None:
                missing.append(key)
                continue
            checked += 1
            actual = snapshot_digest(endpoint)
            if actual != entry["sha256"]:
                mismatched.append(
                    {"key": key, "recorded": entry["sha256"], "actual": actual}
                )

    ordered = sorted(recorded.values(), key=lambda e: e["key"])
    digests = "".join(e["sha256"] for e in ordered)
    manifest_ok = (
        hashlib.sha256(digests.encode("utf-8")).hexdigest()
        == ledger.get("manifest_sha256")
    )

    return {
        "verified": not mismatched and not missing and manifest_ok and checked > 0,
        "entries_checked": checked,
        "mismatched": mismatched,
        "missing_from_ledger": missing,
        "manifest_digest_matches": manifest_ok,
    }


def tamper_detection_self_test(farm, ledger: dict[str, Any]) -> dict[str, Any]:
    """Mutate one snapshot and confirm verification fails.

    This is the E4 requirement made executable. A provenance mechanism that has never
    been shown to reject anything has not been shown to work at all, so the harness runs
    this rather than asserting it. The original bytes are restored afterwards, so the
    self-test cannot corrupt the farm it is checking.
    """
    target = None
    for service in farm.services:
        for endpoint in service.endpoints:
            if endpoint.body:
                target = (service, endpoint)
                break
        if target:
            break

    if target is None:  # pragma: no cover - every farm has bodies
        return {"runs": False, "reason": "no endpoint with a body to mutate"}

    service, endpoint = target
    original = endpoint.body
    try:
        endpoint.body = original + "\n<!-- tampered -->"
        after = verify_ledger(farm, ledger)
    finally:
        endpoint.body = original

    restored = verify_ledger(farm, ledger)

    return {
        "runs": True,
        "mutated": f"{service.service_id}{endpoint.path}",
        "detected": not after["verified"],
        "mismatch_count": len(after["mismatched"]),
        "clean_after_restore": restored["verified"],
        "conclusion": "tamper detected" if not after["verified"] else "TAMPER NOT DETECTED",
    }


def resolve_claim(
    farm, ledger: dict[str, Any], service_id: str, path: str
) -> dict[str, Any]:
    """Resolve a rule's claim back to its source snapshot.

    Part E4 requires every report claim to resolve to a source and its transformations.
    A finding whose evidence cannot be located in the ledger is not reportable, and this
    returns `resolved: False` rather than guessing at a match.
    """
    key = f"{service_id}{path}"
    entry = next((e for e in ledger.get("entries", []) if e["key"] == key), None)
    if entry is None:
        return {"key": key, "resolved": False, "reason": "no ledger entry for this claim"}

    endpoint = farm.service(service_id).endpoint(path)
    if endpoint is None:
        return {"key": key, "resolved": False, "reason": "snapshot no longer present"}

    actual = snapshot_digest(endpoint)
    return {
        "key": key,
        "resolved": actual == entry["sha256"],
        "recorded_sha256": entry["sha256"],
        "current_sha256": actual,
        "status": entry["status"],
    }