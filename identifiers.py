"""Identifier formats used by the synthetic farm and the graph extractor.

Two sides of the same problem live here on purpose:

  * the farm needs to *emit* values whose checksums are real, because a detector
    that only pattern-matches a prefix would pass on a farm that emits nonsense;
  * the graph needs to *validate* what it reads, because a 40-character hex string
    is a favicon hash as often as it is a PGP fingerprint.

Keeping one implementation means the generator cannot accidentally emit a wallet the
validator rejects, and the validator cannot be quietly loosened to make the farm pass.

Dependencies: standard library only. ``keccak_256`` is implemented rather than imported
because Monero uses the original Keccak padding, not SHA3-256, and ``hashlib`` does not
expose it on every build.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

# --------------------------------------------------------------------------- #
# Base58
# --------------------------------------------------------------------------- #

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: i for i, char in enumerate(BASE58_ALPHABET)}


def base58_encode(payload: bytes) -> str:
    number = int.from_bytes(payload, "big")
    encoded = ""
    while number:
        number, rem = divmod(number, 58)
        encoded = BASE58_ALPHABET[rem] + encoded
    pad = len(payload) - len(payload.lstrip(b"\x00"))
    return "1" * pad + (encoded or "")


def base58_decode(text: str) -> bytes:
    number = 0
    for char in text:
        if char not in _BASE58_INDEX:
            raise ValueError(f"not base58: {char!r}")
        number = number * 58 + _BASE58_INDEX[char]
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def base58check_encode(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58_encode(payload + checksum)


def base58check_decode(text: str) -> bytes:
    raw = base58_decode(text)
    if len(raw) < 5:
        raise ValueError("base58check payload too short")
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError("base58check checksum mismatch")
    return payload


# --------------------------------------------------------------------------- #
# Bech32 / bech32m (BIP-173, BIP-350)
# --------------------------------------------------------------------------- #

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_INDEX = {char: i for i, char in enumerate(BECH32_CHARSET)}
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _bech32_polymod(values: Iterable[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    check = 1
    for value in values:
        top = check >> 25
        check = ((check & 0x1FFFFFF) << 5) ^ value
        for i, generator in enumerate(generators):
            if (top >> i) & 1:
                check ^= generator
    return check


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: Iterable[int], from_bits: int, to_bits: int, pad: bool) -> list[int]:
    acc = 0
    bits = 0
    out: list[int] = []
    maxv = (1 << to_bits) - 1
    max_acc = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise ValueError("invalid value in convertbits")
        acc = ((acc << from_bits) | value) & max_acc
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        raise ValueError("invalid padding in convertbits")
    return out


def bech32_encode(hrp: str, witness_version: int, program: bytes) -> str:
    """Encode a SegWit address with the checksum its witness version requires."""

    data = [witness_version, *_convertbits(program, 8, 5, True)]
    const = _BECH32_CONST if witness_version == 0 else _BECH32M_CONST
    polymod = _bech32_polymod([*_hrp_expand(hrp), *data, 0, 0, 0, 0, 0, 0]) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in (*data, *checksum))


def bech32_decode(address: str) -> tuple[str, int, bytes]:
    """Decode and checksum-verify a SegWit address.

    Raises ``ValueError`` on mixed case, a bad checksum, a bad witness version, or a
    witness program whose length no consensus rule permits.
    """

    if not 8 <= len(address) <= 90:
        raise ValueError("bech32 address length out of range")
    if any(ord(c) < 33 or ord(c) > 126 for c in address):
        raise ValueError("bech32 address contains a non-printable character")
    if address.lower() != address and address.upper() != address:
        raise ValueError("bech32 address has mixed case")
    address = address.lower()
    separator = address.rfind("1")
    if separator < 1 or separator + 7 > len(address):
        raise ValueError("bech32 separator missing or misplaced")
    hrp, encoded = address[:separator], address[separator + 1 :]
    try:
        data = [_BECH32_INDEX[c] for c in encoded]
    except KeyError as exc:
        raise ValueError(f"invalid bech32 character: {exc.args[0]!r}") from None

    check = _bech32_polymod([*_hrp_expand(hrp), *data])
    if check == _BECH32_CONST:
        encoding = "bech32"
    elif check == _BECH32M_CONST:
        encoding = "bech32m"
    else:
        raise ValueError("bech32 checksum mismatch")

    payload = data[:-6]
    if not payload:
        raise ValueError("bech32 address has no witness version")
    version = payload[0]
    if version > 16:
        raise ValueError("witness version out of range")
    program = bytes(_convertbits(payload[1:], 5, 8, False))
    if not 2 <= len(program) <= 40:
        raise ValueError("witness program length out of range")
    if version == 0:
        if encoding != "bech32":
            raise ValueError("witness v0 must use bech32, not bech32m")
        if len(program) not in (20, 32):
            raise ValueError("witness v0 program must be 20 or 32 bytes")
    elif encoding != "bech32m":
        raise ValueError("witness v1+ must use bech32m")
    return hrp, version, program


def encode_p2wpkh(hrp: str, key_hash: bytes) -> str:
    if len(key_hash) != 20:
        raise ValueError("P2WPKH requires a 20-byte key hash")
    return bech32_encode(hrp, 0, key_hash)


# --------------------------------------------------------------------------- #
# Keccak-256 (original padding, as used by Ethereum and Monero)
# --------------------------------------------------------------------------- #

_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

_KECCAK_ROTC = (
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
)

_MASK = (1 << 64) - 1


def _rol(value: int, shift: int) -> int:
    if shift == 0:
        return value
    return ((value << shift) | (value >> (64 - shift))) & _MASK


def _keccak_f(state: list[int]) -> None:
    for rc in _KECCAK_RC:
        # theta
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
             for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] ^= d[x]

        # rho and pi
        b = [0] * 25
        for y in range(5):
            for x in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(
                    state[x + 5 * y], _KECCAK_ROTC[x + 5 * y]
                )

        # chi
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ (
                    (~b[(x + 1) % 5 + 5 * y] & _MASK) & b[(x + 2) % 5 + 5 * y]
                )

        # iota
        state[0] ^= rc


def keccak_256(data: bytes) -> bytes:
    rate = 136
    state = [0] * 25
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0)
    padded.append(0x80)

    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8 : i * 8 + 8], "little")
        _keccak_f(state)

    out = b"".join(state[i].to_bytes(8, "little") for i in range(4))
    return out


# --------------------------------------------------------------------------- #
# Monero addresses
# --------------------------------------------------------------------------- #

_MONERO_DECODED_SIZES = {2: 1, 3: 2, 5: 3, 6: 4, 7: 5, 9: 6, 10: 7, 11: 8}


def monero_base58_decode(text: str) -> bytes:
    out = bytearray()
    for offset in range(0, len(text), 11):
        block = text[offset : offset + 11]
        size = _MONERO_DECODED_SIZES.get(len(block))
        if size is None:
            raise ValueError("invalid Monero base58 block length")
        number = 0
        for char in block:
            if char not in _BASE58_INDEX:
                raise ValueError(f"not base58: {char!r}")
            number = number * 58 + _BASE58_INDEX[char]
        out.extend(number.to_bytes(size, "big"))
    return bytes(out)


def monero_base58_encode(payload: bytes) -> str:
    """Encode bytes in Monero's blockwise base58.

    Included so the farm can emit a checksum-valid address that the validator accepts;
    a round-trip test is not evidence of correctness on its own, which is why the
    checksum test tampers with a byte and asserts rejection.
    """

    out: list[str] = []
    for offset in range(0, len(payload), 8):
        block = payload[offset : offset + 8]
        number = int.from_bytes(block, "big")
        encoded: list[str] = []
        for _ in range(11):
            number, rem = divmod(number, 58)
            encoded.append(BASE58_ALPHABET[rem])
        encoded.reverse()
        # A full 8-byte block is 11 chars. A short final block is rendered in its
        # natural width, which is how Monero keeps addresses unambiguous.
        width = {1: 2, 2: 3, 3: 5, 4: 6, 5: 7, 6: 9, 7: 10, 8: 11}[len(block)]
        out.extend(encoded[-width:])
    return "".join(out)


def encode_monero_address(network_byte: int, public_keys: bytes) -> str:
    if len(public_keys) != 64:
        raise ValueError("a Monero address carries a 32-byte spend and view key")
    payload = bytes([network_byte]) + public_keys
    checksum = keccak_256(payload)[:4]
    return monero_base58_encode(payload + checksum)


def validate_monero_address(address: str) -> bool:
    if len(address) not in (95, 106) or not address or address[0] not in "48":
        return False
    try:
        raw = monero_base58_decode(address)
    except ValueError:
        return False
    if len(raw) not in (69, 77):
        return False
    if keccak_256(raw[:-4])[:4] != raw[-4:]:
        return False
    network = raw[0]
    if len(raw) == 69:
        return network in (18, 42, 49)
    return network in (19, 43, 50)


# --------------------------------------------------------------------------- #
# Ethereum
# --------------------------------------------------------------------------- #

_ETH_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{40}\b")


def is_checksum_eth_address(address: str) -> bool:
    """EIP-55 check. An all-lowercase address carries no checksum and is accepted."""

    body = address[2:]
    if body.lower() == body or body.upper() == body:
        return True
    digest = keccak_256(body.lower().encode("ascii")).hex()
    for char, nibble in zip(body, digest[:40], strict=True):
        if char.isalpha():
            if int(nibble, 16) >= 8 and not char.isupper():
                return False
            if int(nibble, 16) < 8 and not char.islower():
                return False
    return True


# --------------------------------------------------------------------------- #
# PGP fingerprints
# --------------------------------------------------------------------------- #

_HEX40 = re.compile(r"\b[0-9A-Fa-f]{40}\b")
_HEX64 = re.compile(r"\b[0-9A-Fa-f]{64}\b")
_GROUPED_HEX = re.compile(
    r"\b(?:[0-9A-Fa-f]{4}[ -]){9}[0-9A-Fa-f]{4}\b"
    r"|\b(?:[0-9A-Fa-f]{4}[ -]){15}[0-9A-Fa-f]{4}\b"
)


def normalise_pgp_fingerprint(value: str) -> str:
    """Canonical form: uppercase, no spaces, 40 or 64 hex characters."""

    normalised = re.sub(r"[\s:-]", "", value).upper()
    if len(normalised) not in (40, 64) or any(c not in "0123456789ABCDEF" for c in normalised):
        raise ValueError("not a PGP fingerprint")
    if normalised == "0" * len(normalised):
        raise ValueError("all-zero fingerprint is not a key")
    return normalised


def find_pgp_fingerprints(text: str) -> list[str]:
    """Fingerprints that are labelled or grouped, never a bare 40-hex hash.

    The context requirement is the false-positive control: a favicon digest and a
    v4 fingerprint have the same shape, and a collector that treats every 40-hex
    token as a key will link two operators on an asset hash. Bare 40/64-hex tokens
    are therefore accepted only inside a PGP armour block or on a line that says
    "fingerprint"; grouped tokens (four hex digits at a time) are unambiguous.
    """

    found: list[str] = []
    in_armour = False
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if "-----begin pgp" in lowered:
            in_armour = True
        elif "-----end pgp" in lowered:
            in_armour = False

        candidates: list[str] = []
        if "fingerprint" in lowered or in_armour:
            candidates.extend(_HEX40.findall(stripped))
            candidates.extend(_HEX64.findall(stripped))
        candidates.extend(_GROUPED_HEX.findall(stripped))

        for candidate in candidates:
            try:
                normalised = normalise_pgp_fingerprint(candidate)
            except ValueError:
                continue
            if normalised not in found:
                found.append(normalised)
    return found


# --------------------------------------------------------------------------- #
# Wallet discovery
# --------------------------------------------------------------------------- #

_BECH32_CANDIDATE = re.compile(r"\b(?:bc|tb|bcrt)1[02-9ac-hj-np-z]{6,87}\b", re.IGNORECASE)
_BASE58_CANDIDATE = re.compile(r"\b[13][1-9A-HJ-NP-Za-km-z]{25,34}\b")
_XMR_CANDIDATE = re.compile(r"\b[48][1-9A-HJ-NP-Za-km-z]{94}\b|\b[48][1-9A-HJ-NP-Za-km-z]{105}\b")


def validate_btc_address(address: str) -> bool:
    lowered = address.lower()
    if lowered.startswith(("bc1", "tb1", "bcrt1")):
        try:
            hrp, version, program = bech32_decode(address)
        except ValueError:
            return False
        return (hrp, version, len(program)) in {
            ("bc", 0, 20), ("bc", 0, 32), ("tb", 0, 20), ("tb", 0, 32),
            ("bcrt", 0, 20), ("bcrt", 0, 32),
        } or (hrp in {"bc", "tb", "bcrt"} and version >= 1)
    try:
        payload = base58check_decode(address)
    except ValueError:
        return False
    return len(payload) == 21 and payload[0] in (0x00, 0x05)


def find_wallet_addresses(text: str) -> list[tuple[str, str]]:
    """Every checksum-valid wallet address in text, with its chain.

    A regex candidate that fails its checksum is dropped rather than reported: the
    point of parsing a wallet is that it can be compared exactly, and a one-character
    transcription error is a different person's address.
    """

    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(value: str, chain: str) -> None:
        if value not in seen:
            seen.add(value)
            found.append((chain, value))

    for match in _BECH32_CANDIDATE.finditer(text):
        candidate = match.group(0)
        if validate_btc_address(candidate):
            add(candidate.lower(), "btc")
    for match in _BASE58_CANDIDATE.finditer(text):
        candidate = match.group(0)
        if validate_btc_address(candidate):
            add(candidate, "btc")
    for match in _XMR_CANDIDATE.finditer(text):
        candidate = match.group(0)
        if validate_monero_address(candidate):
            add(candidate, "xmr")
    for match in _ETH_ADDRESS.finditer(text):
        candidate = match.group(0)
        if int(candidate[2:], 16) != 0 and is_checksum_eth_address(candidate):
            add(candidate, "eth")
    return found
