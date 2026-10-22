"""Artefact extraction from collected snapshots (M3 input).

Extraction is deliberately dumb. It reads the artefact values the acquisition layer
already recorded against a service and attaches, to each one, the digest of the
snapshot it was read from. It does not score, rank or interpret; that is
:mod:`graph.resolve`'s job and keeping the two apart is what makes an edge's
evidence checkable against a file.

Two values are recorded but flagged non-identifying:

``STOCK_DEFAULT``
    A vendor ships this value, so sharing it says the two operators installed the
    same software. The paper measures the cost of forgetting that.
``PUBLIC_PLACEHOLDER``
    A value published in a directory or a forum post, so anyone could carry it.
    Tying a person to it would be a false accusation with a hash attached.
"""

from __future__ import annotations

import hashlib

from graph.model import Artefact, ArtefactKind, NON_IDENTIFYING_VALUES

#: Where each artefact class was observed. The path is recorded so an analyst can go
#: and look, and so a self-comparison of a service against itself is impossible.
KIND_SOURCES: dict[str, str] = {
    ArtefactKind.FAVICON.value: "/favicon.ico",
    ArtefactKind.ANALYTICS_ID.value: "/",
    ArtefactKind.TLS_SPKI.value: "tls:handshake",
    ArtefactKind.SSH_HOSTKEY.value: "ssh:banner",
    ArtefactKind.APP_JS.value: "/app.js",
    ArtefactKind.ERROR_404.value: "/__ot_404_probe__",
    ArtefactKind.CONTACT_HANDLE.value: "/contact",
}

KNOWN_KINDS = frozenset(KIND_SOURCES)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def endpoint_digest(service_id: str, artefact: Artefact) -> str:
    """Recompute what the store should have recorded, for verification.

    The service id is folded in because the same favicon bytes on two services are
    two observations, and a ledger that produced one digest for both could not tell
    a shared file from a copy-paste error in the export.
    """

    return _digest(f"{service_id}\n{artefact.kind}\n{artefact.value}\n{artefact.path}")


def extract_artefacts(farm, *, observed_at: str) -> list[Artefact]:
    """Every identifying value the farm's services carry, with its evidence digest.

    Ordering is by service then kind so a diff between two extractions is stable and
    a reviewer can compare runs without sorting by hand.
    """

    out: list[Artefact] = []
    for svc in farm.services:
        for kind in sorted(svc.shared_artifacts):
            value = svc.shared_artifacts[kind]
            if value is None:
                continue
            value = str(value)
            path = KIND_SOURCES.get(kind, "/")

            if kind not in KNOWN_KINDS:
                # Recorded, not dropped: an unexpected kind is a fact about the
                # collector, and silently discarding it would hide a change in what
                # the farm produces.
                path = "/"

            art = Artefact(
                artefact_id=f"art-{svc.service_id}-{kind}",
                kind=kind,
                value=value,
                service_id=svc.service_id,
                path=path,
                evidence_sha256="",
                observed_at=observed_at,
            )
            art = Artefact(
                artefact_id=art.artefact_id,
                kind=art.kind,
                value=art.value,
                service_id=art.service_id,
                path=art.path,
                evidence_sha256=endpoint_digest(svc.service_id, art),
                observed_at=art.observed_at,
            )
            out.append(art)
    return out


def summarise(artefacts: list[Artefact]) -> dict:
    """Counts for the report, split by whether a value can identify anybody."""

    identifying = [a for a in artefacts if a.is_identifying]
    by_kind: dict[str, int] = {}
    for a in artefacts:
        by_kind[a.kind] = by_kind.get(a.kind, 0) + 1

    return {
        "total": len(artefacts),
        "identifying": len(identifying),
        "non_identifying": len(artefacts) - len(identifying),
        "non_identifying_values": sorted(NON_IDENTIFYING_VALUES - {""}),
        "by_kind": dict(sorted(by_kind.items())),
        "services_covered": len({a.service_id for a in artefacts}),
    }
