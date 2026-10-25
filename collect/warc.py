"""WARC-style raw snapshots: the artefact E4's provenance digest is computed over.

The plan asks for "WARC-style raw snapshots kept for provenance". This is that record.
It is not a full WARC 1.1 implementation and does not pretend to be one -- it is a
response record with the headers that matter for provenance, serialised in the WARC
block format so standard tooling can at least split the stream on the boundary markers.

What is load-bearing:

* ``WARC-Payload-Digest`` is a SHA-256 of the body bytes, and it is what E4's
  digest-chain verification checks. Once a record is sealed, changing one byte of the
  body breaks the chain at that record.
* The record is written **through the gate** (``CollectionGate.ingest``), never
  directly to disk, so "no case, no collection" holds for the snapshot itself and not
  only for the decision to fetch.
* ``record_id`` is derived, not random. Two people replaying the same seed produce the
  same identifiers, which is what makes a reported number re-runnable.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .transport import FetchResult

WARC_VERSION = "WARC/1.0"
BOUNDARY = "168a962a-ab96-45f0-85a6-be9f67eae3f1"


@dataclass(frozen=True)
class WarcRecord:
    """One fetched response, sealed with the case that authorised fetching it."""

    record_id: str
    case_id: str
    target_uri: str
    retrieved_at: str
    status: int
    headers: dict[str, str]
    body: str
    body_sha256: str
    byte_length: int
    transport: str
    seed_label: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def block_digest(self) -> str:
        """Digest over the whole record, headers and metadata included.

        The payload digest alone would not notice a rewritten ``case_id`` -- which is
        the field an auditor cares most about -- so the chain is built over this one.
        """
        return hashlib.sha256(self.serialised().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """The shape stored in the case store and handed to ``gate.ingest``."""
        return {
            "record_id": self.record_id,
            "case_id": self.case_id,
            "url": self.target_uri,
            "status": self.status,
            "sha256": self.body_sha256,
            "block_sha256": self.block_digest,
            "bytes": self.byte_length,
            "retrieved_at": self.retrieved_at,
            "transport": self.transport,
            "seed_label": self.seed_label,
            "content": self.body,
            "extra": dict(self.extra),
        }

    def serialised(self) -> str:
        """WARC-style block text."""
        lines = [
            WARC_VERSION,
            "WARC-Type: response",
            f"WARC-Record-ID: <{self.record_id}>",
            f"WARC-Date: {self.retrieved_at}",
            f"WARC-Target-URI: {self.target_uri}",
            f"WARC-Payload-Digest: sha256:{self.body_sha256}",
            f"ONIONTRACE-Case: {self.case_id}",
            f"ONIONTRACE-Transport: {self.transport}",
            f"ONIONTRACE-Seed-Label: {self.seed_label}",
            "Content-Type: application/http; msgtype=response",
        ]
        block = _http_block(self)
        lines.append(f"Content-Length: {len(block.encode('utf-8'))}")
        lines.append("")
        lines.append(block)
        lines.append("")
        return "\r\n".join(lines) + f"\r\n\r\n--{BOUNDARY}--\r\n\r\n"


def _http_block(record: WarcRecord) -> str:
    head = [f"HTTP/1.1 {record.status} {_reason(record.status)}"]
    for name, value in sorted(record.headers.items()):
        head.append(f"{name}: {value}")
    head.append("")
    head.append(record.body)
    return "\r\n".join(head)


def _reason(status: int) -> str:
    return {200: "OK", 404: "Not Found", 403: "Forbidden", 500: "Server Error"}.get(
        status, "Unknown"
    )


def build_record(
    fetch: FetchResult,
    *,
    case_id: str,
    seed_label: str,
    extra: dict[str, Any] | None = None,
) -> WarcRecord:
    """Seal one fetch into a snapshot belonging to ``case_id``.

    ``case_id`` is a required argument with no default: a snapshot that does not say
    which case authorised it cannot be defended, so the type system refuses to build one.
    """
    if not case_id:
        raise ValueError("a snapshot must name the case that authorised it")
    body = fetch.text
    record_id = _record_id(fetch, case_id, seed_label)
    return WarcRecord(
        record_id=record_id,
        case_id=case_id,
        target_uri=fetch.url,
        retrieved_at=fetch.retrieved_at,
        status=fetch.status,
        headers=dict(fetch.headers),
        body=body,
        body_sha256=fetch.body_sha256,
        byte_length=fetch.byte_length,
        transport=fetch.transport,
        seed_label=seed_label,
        extra=dict(extra or {}),
    )


def _record_id(fetch: FetchResult, case_id: str, seed_label: str) -> str:
    key = f"{fetch.url}|{fetch.body_sha256}|{fetch.retrieved_at}|{case_id}|{seed_label}"
    return "urn:oniontrace:warc:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def stream(records: list[WarcRecord]) -> str:
    """Concatenate records into one WARC-style stream for export."""
    return "".join(record.serialised() for record in records)


def summary(records: list[WarcRecord]) -> str:
    """Compact one-line-per-record form for the CLI and for evidence packs."""
    return json.dumps(
        [
            {
                "record_id": r.record_id,
                "url": r.target_uri,
                "status": r.status,
                "sha256": r.body_sha256[:16],
                "bytes": r.byte_length,
                "retrieved_at": r.retrieved_at,
                "seed_label": r.seed_label,
            }
            for r in records
        ],
        indent=2,
        sort_keys=True,
    )
