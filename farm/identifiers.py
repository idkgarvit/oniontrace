"""Generate identifier values whose checksums are real.

The farm's personas already carried a PGP fingerprint and a wallet address in
``shared_artifacts``, but those were opaque hex strings. Publishing them gave the
extractor nothing to validate, so a pattern match on "40 hex characters" would have
looked like a correct implementation. These helpers emit values the validators in
:mod:`identifiers` accept, which makes the extraction test measure parsing rather
than prefix guessing.

Everything is a deterministic function of the seed, so the farm stays reproducible.
"""

from __future__ import annotations

import hashlib

from identifiers import (
    base58check_encode,
    encode_monero_address,
    encode_p2wpkh,
)
from farm.model import deterministic_hex


def make_pgp_fingerprint(seed: int, label: str) -> str:
    """A v4-shaped fingerprint: 40 uppercase hex characters, deterministic."""

    return deterministic_hex(seed, f"pgp:{label}", length=40).upper()


def format_pgp_fingerprint(value: str) -> str:
    """The form a human publishes: groups of four, as GnuPG prints it."""

    return " ".join(value[i : i + 4] for i in range(0, len(value), 4))


def make_wallet_address(seed: int, label: str) -> str:
    """A checksum-valid address on one of four chains, chosen deterministically.

    Covering BTC (SegWit and legacy), Monero and Ethereum means the extractor's
    validators are exercised on every farm build instead of only in unit tests.
    """

    material = hashlib.sha256(f"{seed}:wallet:{label}".encode("utf-8")).digest()
    chain = material[0] % 4
    if chain == 0:
        return encode_p2wpkh("bc", material[1:21])
    if chain == 1:
        return base58check_encode(b"\x00" + material[1:21])
    if chain == 2:
        keys = material + hashlib.sha256(material).digest()
        return encode_monero_address(18, keys)
    return "0x" + material[1:21].hex()


def wallet_chain(value: str) -> str:
    """Label a stored address with its chain, for display and evidence notes."""

    lowered = value.lower()
    if lowered.startswith(("bc1", "tb1", "bcrt1")):
        return "btc"
    if value.startswith(("1", "3")):
        return "btc"
    if value.startswith("0x"):
        return "eth"
    if value.startswith(("4", "8")) and len(value) in (95, 106):
        return "xmr"
    return "unknown"
