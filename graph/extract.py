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

Page identifiers (PGP fingerprints and wallet addresses) are parsed from the served
text rather than copied from the farm's ground truth. That distinction is the point:
the farm may *know* which personas share a key, but the graph only sees what a passive
visitor could read. A parser that passed by reading the manifest would prove nothing
about collection.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from identifiers import find_pgp_fingerprints, find_wallet_addresses
from graph.model import (
    NON_IDENTIFYING_VALUES,
    Artefact,
    ArtefactKind,
)

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
    ArtefactKind.PGP_FINGERPRINT.value: "/contact",
    ArtefactKind.WALLET_ADDRESS.value: "/contact",
}

KNOWN_KINDS = frozenset(KIND_SOURCES)

_PERSONA_LINE = re.compile(r"(?:persona|operator(?:\s+handle)?)\s*[:=]\s*@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})", re.I)
_VOUCH = re.compile(
    r"\b(?:vouch(?:es|ed)?\s+for|trusts?|trusted\s+(?:vendor|operator))\s*:?\s*@"
    r"([A-Za-z0-9][A-Za-z0-9_.-]{0,63})",
    re.I,
)

#: Phrases that reverse the meaning of a vouch. A parser that fires on "we do not
#: vouch for @x" turns a refusal into a relationship, which is the same class of
#: error as treating a vendor default as identity evidence.
_NEGATED_VOUCH = (
    "not vouch",
    "never vouch",
    "no one has vouch",
    "nobody has vouch",
    "cannot vouch",
    "can't vouch",
    "don't vouch",
    "do not vouch",
    "does not vouch",
    "unverified",
    "disputed",
)


@dataclass(frozen=True)
class TrustClaim:
    """One explicit social claim read from a page.

    It names a *handle*, not an entity. Resolving the handle to the service that
    publishes it is a separate step, so the claim can still be shown when no such
    service is in the case.
    """

    source_service: str
    target_handle: str
    path: str
    evidence_sha256: str
    source_handle: str = ""
    target_service: str = ""
    line: str = ""

    def to_dict(self) -> dict:
        return {
            "source_service": self.source_service,
            "source_handle": self.source_handle,
            "target_handle": self.target_handle,
            "target_service": self.target_service,
            "path": self.path,
            "evidence_sha256": self.evidence_sha256,
            "line": self.line,
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _observation_digest(service_id: str, kind: str, value: str, path: str) -> str:
    return _digest(f"{service_id}\n{kind}\n{value}\n{path}")


def endpoint_digest(service_id: str, artefact: Artefact) -> str:
    """Recompute what the store should have recorded, for verification.

    The service id is folded in because the same favicon bytes on two services are
    two observations, and a ledger that produced one digest for both could not tell
    a shared file from a copy-paste error in the export.
    """

    return _observation_digest(
        service_id, artefact.kind, artefact.value, artefact.path
    )


def _make_artefact(
    service_id: str, kind: str, value: str, path: str, observed_at: str
) -> Artefact:
    return Artefact(
        artefact_id=f"art-{service_id}-{kind}-{_digest(value)[:10]}",
        kind=kind,
        value=value,
        service_id=service_id,
        path=path,
        evidence_sha256=_observation_digest(service_id, kind, value, path),
        observed_at=observed_at,
    )


def extract_page_artefacts(service, *, observed_at: str) -> list[Artefact]:
    """PGP fingerprints and wallet addresses read from a service's pages.

    Multiple values of the same kind are kept. A service can publish more than one
    operator's key, and collapsing those to the first would silently drop the very
    link the extractor exists to find.
    """

    out: list[Artefact] = []
    seen: set[tuple[str, str]] = set()
    for endpoint in getattr(service, "endpoints", ()) or ():
        body = getattr(endpoint, "body", "") or ""
        path = getattr(endpoint, "path", "/")
        pairs: list[tuple[str, str]] = [
            (ArtefactKind.PGP_FINGERPRINT.value, value)
            for value in find_pgp_fingerprints(body)
        ]
        pairs.extend(
            (ArtefactKind.WALLET_ADDRESS.value, value)
            for _chain, value in find_wallet_addresses(body)
        )
        for kind, value in pairs:
            if (kind, value) in seen:
                continue
            seen.add((kind, value))
            out.append(_make_artefact(service.service_id, kind, value, path, observed_at))
    return out


def extract_artefacts(farm, *, observed_at: str) -> list[Artefact]:
    """Every identifying value the farm's services carry, with its evidence digest.

    Ordering is by service then kind so a diff between two extractions is stable and
    a reviewer can compare runs without sorting by hand.
    """

    out: list[Artefact] = []
    for svc in farm.services:
        seen: set[tuple[str, str]] = set()
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

            out.append(_make_artefact(svc.service_id, kind, value, path, observed_at))
            seen.add((kind, value))

        for art in extract_page_artefacts(svc, observed_at=observed_at):
            if (art.kind, art.value) not in seen:
                out.append(art)
                seen.add((art.kind, art.value))
    return out


def _visible_lines(body: str) -> list[str]:
    """Text a reader sees, split into claim-sized pieces.

    Pages are often served minified, so splitting the raw HTML on newlines would put
    a positive vouch and a refusal on the same line and let one negation discard
    both. Tags become line breaks, then empty lines are dropped.
    """

    text = re.sub(r"<[^>]+>", "\n", body)
    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_trust_claims(farm) -> list[TrustClaim]:
    """Explicit vouch statements, and the handle-to-service map needed to place them.

    The two passes are separate on purpose. A vouch names a handle; only a page that
    publishes that handle says which service it belongs to. A parser that guessed the
    target from proximity would invent a link whenever two operators were mentioned
    in the same paragraph.
    """

    handle_service: dict[str, str] = {}
    for svc in farm.services:
        for endpoint in getattr(svc, "endpoints", ()) or ():
            body = getattr(endpoint, "body", "") or ""
            for line in _visible_lines(body):
                match = _PERSONA_LINE.search(line)
                if match:
                    handle_service.setdefault(match.group(1), svc.service_id)

    claims: list[TrustClaim] = []
    seen: set[tuple[str, str]] = set()
    for svc in farm.services:
        for endpoint in getattr(svc, "endpoints", ()) or ():
            body = getattr(endpoint, "body", "") or ""
            path = getattr(endpoint, "path", "/")
            source_handle = ""
            for line in _visible_lines(body):
                persona = _PERSONA_LINE.search(line)
                if persona:
                    source_handle = persona.group(1)

                lowered = line.lower()
                if any(phrase in lowered for phrase in _NEGATED_VOUCH):
                    continue
                for match in _VOUCH.finditer(line):
                    target = match.group(1).rstrip(".,;:")
                    target_service = handle_service.get(target)
                    if target_service is None:
                        # The claim is real but its target is not in this case. It is
                        # not an edge yet; dropping it silently would make the
                        # extractor look more precise than it is.
                        continue
                    key = (svc.service_id, target)
                    if key in seen:
                        continue
                    seen.add(key)
                    claims.append(
                        TrustClaim(
                            source_service=svc.service_id,
                            source_handle=source_handle,
                            target_handle=target,
                            target_service=target_service,
                            path=path,
                            evidence_sha256=_digest(
                                f"{svc.service_id}\n{path}\ntrust\n{target}"
                            ),
                            line=line.strip(),
                        )
                    )
    return claims


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
