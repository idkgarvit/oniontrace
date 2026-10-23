"""Read Certificate Transparency logs at the source instead of through a mirror.

Why this module exists
----------------------
The project used to query ``crt.sh`` for certificates naming an onion address. That mirror
answers ``Disallow: /`` to every user agent and throttles under load, so it is a source that
does not want to be queried, and a research artefact resting on it inherits that problem.

The authoritative source is the log itself. RFC 6962 defines a small HTTP interface
(``get-sth``, ``get-entries``, ``get-proof-by-hash``) that every public log serves without
credentials, and a log's signed tree head is a cryptographic commitment that a certificate
existed at a given time. That is *stronger* evidence than "a mirror said so": the mirror is
a database somebody maintains, while the tree head is signed by the log operator's key.

What this module does not do
----------------------------
It does not search. A log is append-only and indexed by sequence number, not by name, and
the public logs together hold billions of entries. There is no honest way to ask a log "find
me every certificate naming a .onion address" -- that is a query the mirror provides and the
log does not. Discovery belongs to sources that invite it; this module supplies proof.

Layout of the pieces
--------------------
``parse_log_list``   the operator JSON that names logs and carries their public keys
``parse_sth``        one signed tree head
``verify_sth``       ECDSA over the RFC 6962 ``TreeHeadSignature`` structure
``parse_leaf``       one Merkle tree leaf, as ``get-entries`` returns it
``names_in_certificate``  dNSName SANs out of a DER certificate

None of those touch the network. ``CtLogClient`` is the thin layer that does, and it takes a
``fetch`` callable so a test can hand it bytes without a socket existing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Callable

#: Google's list of known logs, in the v3 schema. It changes a few times a year as logs are
#: created and retired, which is exactly why it is fetched rather than embedded: a pinned
#: list would quietly rot, and a rotated log key would start failing verification.
LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

#: RFC 6962 allows a thousand entries per call; several logs cap lower and answer 400 above
#: their own limit, so stay inside the smallest published cap rather than learning it by
#: being refused.
MAX_ENTRIES_PER_CALL = 256

#: TLS 1.2 ciphersuite constants, used by the DigitallySigned wrapper the STH arrives in.
_HASH_SHA256 = 4
_SIG_ECDSA = 3

#: RFC 6962 section 3.5: the two one-byte constants that prefix a tree head signature.
_VERSION_V1 = 0
_SIGNATURE_TYPE_TREE_HASH = 1

#: RFC 6962 section 3.4: entry types inside a timestamped leaf.
_X509_ENTRY = 0
_PRECERT_ENTRY = 1


class CtLogError(Exception):
    """A log gave something this module will not pretend to understand."""


class CtLogVerificationFailed(CtLogError):
    """A tree head did not verify. Never downgrade this to a warning."""


@dataclass(frozen=True)
class CtLog:
    """One log, as the operator list describes it."""

    description: str
    url: str
    log_id: bytes
    key_der: bytes

    @property
    def base(self) -> str:
        """The log's base URL with exactly one trailing slash, for building endpoints."""
        return self.url if self.url.endswith("/") else self.url + "/"


@dataclass(frozen=True)
class SignedTreeHead:
    """A log's signed commitment to everything it has ever accepted."""

    tree_size: int
    timestamp: int
    root_hash: bytes
    #: The full ``DigitallySigned`` structure, algorithm header included, as it arrived.
    signature: bytes


@dataclass(frozen=True)
class Leaf:
    """One Merkle tree leaf, as ``get-entries`` hands it over."""

    timestamp: int
    entry_type: int
    #: The DER the leaf carries: a whole certificate for an x509 entry, a TBS for a precert.
    payload: bytes

    @property
    def is_precert(self) -> bool:
        return self.entry_type == _PRECERT_ENTRY


def _b64(payload: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001 - every decoder failure is the same answer here
        raise CtLogError(f"{field} is not valid base64: {exc}") from exc


def parse_log_list(payload: bytes) -> tuple[CtLog, ...]:
    """Every usable log in an operator list, in the order the list gives them.

    Unusable logs are dropped rather than carried with a flag: a retired or read-only log
    cannot serve entries, and a caller that has to check a state field before every call is
    a caller that will eventually forget to.
    """
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CtLogError(f"the log list is not JSON: {exc}") from exc

    logs: list[CtLog] = []
    for operator in document.get("operators", []):
        for entry in operator.get("logs", []):
            state = entry.get("state") or {}
            if "usable" not in state:
                continue
            key_der = _b64(entry.get("key", ""), field=f"{entry.get('description')} key")
            log_id = _b64(entry.get("log_id", ""), field=f"{entry.get('description')} log_id")
            url = entry.get("url", "")
            if not url:
                raise CtLogError(f"log {entry.get('description')!r} has no url")
            logs.append(
                CtLog(
                    description=entry.get("description", url),
                    url=url,
                    log_id=log_id,
                    key_der=key_der,
                )
            )
    if not logs:
        raise CtLogError("the log list named no usable logs")
    return tuple(logs)


def parse_sth(payload: bytes) -> SignedTreeHead:
    """One ``get-sth`` response."""
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CtLogError(f"the tree head is not JSON: {exc}") from exc
    try:
        root_hash = _b64(document["sha256_root_hash"], field="sha256_root_hash")
        signature = _b64(document["tree_head_signature"], field="tree_head_signature")
        tree_size = int(document["tree_size"])
        timestamp = int(document["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CtLogError(f"the tree head is missing a field: {exc}") from exc
    if len(root_hash) != 32:
        raise CtLogError(f"sha256_root_hash is {len(root_hash)} bytes, not 32")
    return SignedTreeHead(
        tree_size=tree_size, timestamp=timestamp, root_hash=root_hash, signature=signature
    )


def tree_head_signature_input(sth: SignedTreeHead) -> bytes:
    """The exact bytes RFC 6962 says a log signs for a tree head.

    Spelled out field by field rather than assembled with a library so the layout is
    visible: two one-byte constants, two big-endian uint64s, and the root hash.
    """
    return struct.pack(
        ">BBQQ", _VERSION_V1, _SIGNATURE_TYPE_TREE_HASH, sth.timestamp, sth.tree_size
    ) + sth.root_hash


def verify_sth(sth: SignedTreeHead, log: CtLog) -> None:
    """Prove that ``log`` really signed ``sth``, or raise ``CtLogVerificationFailed``.

    Also checks that the key in the operator list hashes to the log's published id, which
    catches that specific failure where a list is served with the wrong key for a log --
    without it, a substituted key would verify a substituted tree head perfectly well.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    digest = hashlib.sha256(log.key_der).digest()
    if log.log_id and digest != log.log_id:
        raise CtLogVerificationFailed(
            f"{log.description}: the operator list's key does not hash to the log id "
            f"(key hashes to {digest.hex()}, list says {log.log_id.hex()})"
        )

    signature = sth.signature
    if len(signature) < 4:
        raise CtLogVerificationFailed(f"{log.description}: signature is {len(signature)} bytes")
    hash_alg, sig_alg = signature[0], signature[1]
    length = int.from_bytes(signature[2:4], "big")
    if hash_alg != _HASH_SHA256 or sig_alg != _SIG_ECDSA:
        raise CtLogVerificationFailed(
            f"{log.description}: expected ECDSA-SHA256, got hash {hash_alg} sig {sig_alg}"
        )
    if len(signature) != 4 + length:
        raise CtLogVerificationFailed(
            f"{log.description}: signature claims {length} bytes but carries {len(signature) - 4}"
        )

    try:
        public_key = serialization.load_der_public_key(log.key_der)
    except Exception as exc:  # noqa: BLE001
        raise CtLogVerificationFailed(f"{log.description}: unusable log key: {exc}") from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise CtLogVerificationFailed(f"{log.description}: log key is not ECDSA")

    try:
        public_key.verify(
            signature[4:], tree_head_signature_input(sth), ec.ECDSA(hashes.SHA256())
        )
    except InvalidSignature as exc:
        raise CtLogVerificationFailed(
            f"{log.description}: the tree head's signature does not verify against the "
            "operator key. A tree head that does not verify is not evidence of anything."
        ) from exc


def parse_leaf(leaf_input: bytes) -> Leaf:
    """One Merkle tree leaf, from the ``leaf_input`` a log returns base64-encoded.

    Layout, RFC 6962 section 3.4: version, leaf type, timestamp, entry type, then either a
    length-prefixed certificate or a precertificate's issuer hash and length-prefixed TBS.
    """
    if len(leaf_input) < 12:
        raise CtLogError(f"a leaf of {len(leaf_input)} bytes is too short to hold a header")
    version, leaf_type = leaf_input[0], leaf_input[1]
    if version != _VERSION_V1:
        raise CtLogError(f"unsupported leaf version {version}")
    if leaf_type != 0:
        raise CtLogError(f"unsupported leaf type {leaf_type}, expected a timestamped entry")
    timestamp = int.from_bytes(leaf_input[2:10], "big")
    entry_type = int.from_bytes(leaf_input[10:12], "big")

    body = leaf_input[12:]
    if entry_type == _X509_ENTRY:
        if len(body) < 3:
            raise CtLogError("an x509 entry ended before its length prefix")
        length = int.from_bytes(body[:3], "big")
        if len(body) < 3 + length:
            raise CtLogError(f"an x509 entry claims {length} bytes and carries {len(body) - 3}")
        return Leaf(timestamp=timestamp, entry_type=entry_type, payload=body[3 : 3 + length])
    if entry_type == _PRECERT_ENTRY:
        if len(body) < 35:
            raise CtLogError("a precert entry ended inside its issuer key hash")
        length = int.from_bytes(body[32:35], "big")
        if len(body) < 35 + length:
            raise CtLogError(
                f"a precert entry claims {length} bytes and carries {len(body) - 35}"
            )
        return Leaf(timestamp=timestamp, entry_type=entry_type, payload=body[35 : 35 + length])
    raise CtLogError(f"unknown entry type {entry_type}")


def names_in_certificate(der: bytes) -> tuple[str, ...]:
    """Every dNSName in a DER certificate's subjectAltName, lowercased and deduplicated.

    Returns an empty tuple rather than raising for a precertificate's TBS, which is not a
    complete certificate and cannot be loaded as one. Callers that care about the difference
    should check ``Leaf.is_precert`` first.
    """
    from cryptography import x509

    try:
        certificate = x509.load_der_x509_certificate(der)
    except Exception:  # noqa: BLE001 - a TBS is not a certificate; this is not an error here
        return ()
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return ()
    names: list[str] = []
    for name in san.get_values_for_type(x509.DNSName):
        lowered = name.lower().rstrip(".")
        if lowered and lowered not in names:
            names.append(lowered)
    return tuple(names)


class CtLogClient:
    """Talks to one log. Takes a ``fetch`` so tests never open a socket."""

    def __init__(self, log: CtLog, fetch: Callable[[str], bytes]) -> None:
        self.log = log
        self._fetch = fetch

    def sth(self, *, verify: bool = True) -> SignedTreeHead:
        head = parse_sth(self._fetch(f"{self.log.base}ct/v1/get-sth"))
        if verify:
            verify_sth(head, self.log)
        return head

    def entries(self, start: int, end: int) -> tuple[Leaf, ...]:
        """Inclusive range, as the log defines it. Never wider than the published cap."""
        if start < 0 or end < start:
            raise CtLogError(f"refusing the range {start}..{end}")
        if end - start + 1 > MAX_ENTRIES_PER_CALL:
            raise CtLogError(
                f"refusing {end - start + 1} entries in one call; the cap here is "
                f"{MAX_ENTRIES_PER_CALL} so no log has to refuse it for us"
            )
        payload = self._fetch(f"{self.log.base}ct/v1/get-entries?start={start}&end={end}")
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CtLogError(f"get-entries did not return JSON: {exc}") from exc
        entries = document.get("entries")
        if not isinstance(entries, list):
            raise CtLogError("get-entries returned no entries field")
        return tuple(
            parse_leaf(_b64(entry["leaf_input"], field="leaf_input")) for entry in entries
        )


def requests_fetch(proxy: str | None = None, timeout: float = 45.0) -> Callable[[str], bytes]:
    """A fetcher over HTTP(S), optionally through a SOCKS5 proxy.

    Kept separate from the client so the parsing above is testable with no network, and so
    the proxy choice belongs to the caller rather than being baked into log handling.
    """

    def fetch(url: str) -> bytes:
        import requests

        proxies = {"http": proxy, "https": proxy} if proxy else None
        response = requests.get(url, timeout=timeout, proxies=proxies, allow_redirects=False)
        if response.status_code != 200:
            raise CtLogError(f"{url} answered {response.status_code}")
        return response.content

    return fetch
