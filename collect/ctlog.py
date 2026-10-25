"""Certificate-transparency log parsing: the sauteed-onion signal.

What this actually yields, stated honestly
------------------------------------------
A certificate whose SAN list contains *both* a ``.onion`` name and a clearnet name
associates the two: whoever provisioned that certificate knew both endpoints at the same
time. That is the whole signal, and it is weak. The CA/B Forum deprecated ``.onion``
names in publicly trusted certificates, so on the live network these entries are rare,
old, and biased toward the handful of large operators who had them issued before the
change. A correlator that treats a CT association as strong evidence will be confidently
wrong about a small, unrepresentative population.

So this module reports associations, never "same operator", and the plan's own
presentation rule (E2.5) is the reason the field is named ``association`` rather than
anything that reads like a conclusion.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

CTLOG_HOST = "crt.sh"

ONION_RE = re.compile(r"\b([a-z2-7]{56}\.onion)\b")

#: Carried into every report so a reader cannot meet an association without the caveat.
SIGNAL_NOTE = (
    "Certificate associations are weak evidence: public CA issuance of .onion names was "
    "deprecated, so such entries are rare, stale and skewed to early large adopters."
)


def query_url(hostname: str) -> str:
    """The crt.sh query URL for one name. Encoded for the same reason as ``search_url``."""
    return f"https://{CTLOG_HOST}/?{urlencode({'q': hostname, 'output': 'json'})}"


@dataclass(frozen=True)
class CertEntry:
    """One issued certificate as a log exposes it."""

    log_id: str
    common_name: str
    san_names: tuple[str, ...]
    not_before: str
    not_after: str
    source_url: str = ""

    @property
    def onions(self) -> tuple[str, ...]:
        return tuple(sorted(n for n in self.san_names if n.endswith(".onion")))

    @property
    def clearnet_names(self) -> tuple[str, ...]:
        return tuple(sorted(n for n in self.san_names if not n.endswith(".onion")))

    @property
    def is_sauteed(self) -> bool:
        """True when one certificate names both an onion and a clearnet host."""
        return bool(self.onions) and bool(self.clearnet_names)


@dataclass(frozen=True)
class OnionAssociation:
    """An onion observed alongside clearnet names in a single certificate."""

    onion: str
    clearnet_names: tuple[str, ...]
    log_ids: tuple[str, ...]
    first_seen: str
    last_seen: str
    note: str = SIGNAL_NOTE

    def to_dict(self) -> dict[str, object]:
        return {
            "onion": self.onion,
            "clearnet_names": list(self.clearnet_names),
            "log_ids": list(self.log_ids),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "note": self.note,
        }


def _split_names(raw: object) -> tuple[str, ...]:
    """crt.sh puts the SAN list in ``name_value``, newline-separated."""
    if isinstance(raw, str):
        parts = re.split(r"[,\n]", raw)
    elif isinstance(raw, list):
        parts = [str(p) for p in raw]
    else:
        parts = []
    names = {p.strip().lower().lstrip("*.") for p in parts if p and p.strip()}
    return tuple(sorted(n for n in names if n))


def parse_entries(payload: str, *, source_url: str = "") -> list[CertEntry]:
    """Parse a crt.sh-shaped JSON array of certificate records."""
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CT log payload is not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError("CT log payload must be a JSON array of certificate records")
    entries: list[CertEntry] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        san_names = _split_names(row.get("name_value") or row.get("san_dns_names"))
        common = str(row.get("common_name") or "").strip().lower()
        if common and common not in san_names:
            san_names = tuple(sorted({*san_names, common}))
        entries.append(
            CertEntry(
                log_id=str(row.get("id") or row.get("leaf_hash") or f"entry-{index}"),
                common_name=common,
                san_names=san_names,
                not_before=str(row.get("not_before") or ""),
                not_after=str(row.get("not_after") or ""),
                source_url=source_url,
            )
        )
    return entries


def associations(entries: list[CertEntry]) -> list[OnionAssociation]:
    """Collapse sauteed certificates into one association per onion address."""
    grouped: dict[str, dict[str, list[str]]] = {}
    for entry in entries:
        if not entry.is_sauteed:
            continue
        bucket = grouped.setdefault(entry.onions[0], {"names": [], "logs": [], "seen": []})
        bucket["names"].extend(entry.clearnet_names)
        bucket["logs"].append(entry.log_id)
        for stamp in (entry.not_before, entry.not_after):
            if stamp:
                bucket["seen"].append(stamp)
    out: list[OnionAssociation] = []
    for onion, bucket in sorted(grouped.items()):
        seen = sorted(bucket["seen"])
        out.append(
            OnionAssociation(
                onion=onion,
                clearnet_names=tuple(sorted(set(bucket["names"]))),
                log_ids=tuple(sorted(set(bucket["logs"]))),
                first_seen=seen[0] if seen else "",
                last_seen=seen[-1] if seen else "",
            )
        )
    return out
