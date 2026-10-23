"""What the CT log client is allowed to conclude, and what it must refuse to.

Every test here builds its own key and its own bytes. None of them reaches a log, a mirror or
the network: the point of the module is that a tree head either verifies against a key or it
does not, and that question can be asked offline with a key the test controls.
"""

import base64
import datetime
import hashlib
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from collect.ctlog_direct import (
    MAX_ENTRIES_PER_CALL,
    CtLog,
    CtLogClient,
    CtLogError,
    CtLogVerificationFailed,
    names_in_certificate,
    parse_leaf,
    parse_log_list,
    parse_sth,
    tree_head_signature_input,
    verify_sth,
)

ROOT = hashlib.sha256(b"a root hash a log would publish").digest()


# --------------------------------------------------------------------------- #
# Building the things a log would send
# --------------------------------------------------------------------------- #


def make_log():
    """A log with a key this test owns, and the id that key really hashes to."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    log = CtLog(
        description="test log",
        url="https://log.example/",
        log_id=hashlib.sha256(key_der).digest(),
        key_der=key_der,
    )
    return log, key


def signed_head(key, *, tree_size=1234, timestamp=1700000000, root=ROOT, corrupt=False):
    """A tree head signed the way RFC 6962 says, with an option to sign the wrong bytes."""
    sth = parse_sth(
        json.dumps(
            {
                "tree_size": tree_size,
                "timestamp": timestamp,
                "sha256_root_hash": base64.b64encode(root).decode(),
                "tree_head_signature": base64.b64encode(b"\x04\x03\x00\x00").decode(),
            }
        ).encode()
    )
    message = tree_head_signature_input(sth)
    if corrupt:
        message = message[:-1] + bytes([message[-1] ^ 0xFF])
    signature = key.sign(message, ec.ECDSA(hashes.SHA256()))
    wrapped = b"\x04\x03" + len(signature).to_bytes(2, "big") + signature
    return parse_sth(
        json.dumps(
            {
                "tree_size": tree_size,
                "timestamp": timestamp,
                "sha256_root_hash": base64.b64encode(root).decode(),
                "tree_head_signature": base64.b64encode(wrapped).decode(),
            }
        ).encode()
    )


def certificate_with(names):
    """A real DER certificate carrying the given dNSName SANs."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    moment = datetime.datetime(2026, 1, 1)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(moment)
        .not_valid_after(moment + datetime.timedelta(days=90))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in names]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.DER)


def x509_leaf(der, *, timestamp=1700000000):
    return (
        bytes([0, 0])
        + timestamp.to_bytes(8, "big")
        + (0).to_bytes(2, "big")
        + len(der).to_bytes(3, "big")
        + der
    )


# --------------------------------------------------------------------------- #
# The operator list
# --------------------------------------------------------------------------- #


def test_the_list_keeps_usable_logs_and_drops_the_rest():
    """A retired log cannot serve entries, so carrying it with a flag invites a failed call."""
    log, _ = make_log()
    payload = json.dumps(
        {
            "operators": [
                {
                    "name": "Example",
                    "logs": [
                        {
                            "description": "usable",
                            "url": "https://log.example/",
                            "log_id": base64.b64encode(log.log_id).decode(),
                            "key": base64.b64encode(log.key_der).decode(),
                            "state": {"usable": {"timestamp": "2026-01-01T00:00:00Z"}},
                        },
                        {
                            "description": "retired",
                            "url": "https://old.example/",
                            "log_id": base64.b64encode(b"x" * 32).decode(),
                            "key": base64.b64encode(log.key_der).decode(),
                            "state": {"retired": {"timestamp": "2025-01-01T00:00:00Z"}},
                        },
                    ],
                }
            ]
        }
    ).encode()
    logs = parse_log_list(payload)
    assert [entry.description for entry in logs] == ["usable"]


def test_a_list_with_no_usable_logs_is_an_error_not_an_empty_result():
    payload = json.dumps({"operators": [{"name": "x", "logs": []}]}).encode()
    with pytest.raises(CtLogError):
        parse_log_list(payload)


# --------------------------------------------------------------------------- #
# The signature
# --------------------------------------------------------------------------- #


def test_the_signed_bytes_are_the_layout_rfc_6962_defines():
    """Two constants, two big-endian uint64s, then the root. Spelled out, not trusted."""
    sth = parse_sth(
        json.dumps(
            {
                "tree_size": 0x0102030405060708,
                "timestamp": 0x1112131415161718,
                "sha256_root_hash": base64.b64encode(b"\xaa" * 32).decode(),
                "tree_head_signature": base64.b64encode(b"\x04\x03\x00\x00").decode(),
            }
        ).encode()
    )
    message = tree_head_signature_input(sth)
    assert message[:2] == bytes([0, 1]), "version v1, signature type tree_hash"
    assert message[2:10] == bytes.fromhex("1112131415161718")
    assert message[10:18] == bytes.fromhex("0102030405060708")
    assert message[18:] == b"\xaa" * 32
    assert len(message) == 50


def test_a_tree_head_signed_by_the_log_verifies():
    log, key = make_log()
    verify_sth(signed_head(key), log)


def test_a_tree_head_signed_over_different_bytes_does_not_verify():
    """The one failure this module exists to catch: a head that looks right and is not."""
    log, key = make_log()
    with pytest.raises(CtLogVerificationFailed):
        verify_sth(signed_head(key, corrupt=True), log)


def test_a_head_signed_by_another_key_does_not_verify():
    log, _ = make_log()
    _, other_key = make_log()
    with pytest.raises(CtLogVerificationFailed):
        verify_sth(signed_head(other_key), log)


def test_a_key_that_does_not_hash_to_the_log_id_is_refused():
    """Without this, a substituted key verifies a substituted tree head perfectly well."""
    log, key = make_log()
    impostor = CtLog(
        description="test log",
        url=log.url,
        log_id=hashlib.sha256(b"some other key").digest(),
        key_der=log.key_der,
    )
    with pytest.raises(CtLogVerificationFailed) as refusal:
        verify_sth(signed_head(key), impostor)
    assert "does not hash to the log id" in str(refusal.value)


# --------------------------------------------------------------------------- #
# Leaves
# --------------------------------------------------------------------------- #


def test_an_x509_leaf_round_trips():
    der = certificate_with(["abc.onion", "example.com"])
    leaf = parse_leaf(x509_leaf(der, timestamp=1700000001))
    assert leaf.timestamp == 1700000001
    assert not leaf.is_precert
    assert leaf.payload == der


def test_a_precert_leaf_is_marked_as_one_because_its_payload_is_not_a_certificate():
    tbs = b"\x30\x03\x02\x01\x01"
    raw = bytes([0, 0]) + (7).to_bytes(8, "big") + (1).to_bytes(2, "big") + b"\x00" * 32
    raw += len(tbs).to_bytes(3, "big") + tbs
    leaf = parse_leaf(raw)
    assert leaf.is_precert
    assert leaf.payload == tbs
    assert names_in_certificate(leaf.payload) == ()


def test_a_leaf_that_claims_more_bytes_than_it_carries_is_refused():
    der = certificate_with(["abc.onion"])
    raw = bytearray(x509_leaf(der))
    raw[12:15] = (len(der) + 500).to_bytes(3, "big")
    with pytest.raises(CtLogError):
        parse_leaf(bytes(raw))


def test_onion_names_come_out_of_a_certificate_lowercased_and_deduplicated():
    der = certificate_with(["AbC.Onion", "abc.onion", "example.com"])
    assert names_in_certificate(der) == ("abc.onion", "example.com")


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


def test_the_client_refuses_a_range_wider_than_the_cap():
    """The cap exists so no log has to refuse us; learning it by being refused is slower."""
    log, _ = make_log()
    client = CtLogClient(log, fetch=lambda url: b"{}")
    with pytest.raises(CtLogError):
        client.entries(0, MAX_ENTRIES_PER_CALL)


def test_the_client_verifies_the_head_unless_told_not_to():
    log, key = make_log()
    good = signed_head(key)
    payload = json.dumps(
        {
            "tree_size": good.tree_size,
            "timestamp": good.timestamp,
            "sha256_root_hash": base64.b64encode(good.root_hash).decode(),
            "tree_head_signature": base64.b64encode(good.signature).decode(),
        }
    ).encode()
    assert CtLogClient(log, fetch=lambda url: payload).sth().tree_size == good.tree_size

    _, other = make_log()
    bad = signed_head(other)
    payload = json.dumps(
        {
            "tree_size": bad.tree_size,
            "timestamp": bad.timestamp,
            "sha256_root_hash": base64.b64encode(bad.root_hash).decode(),
            "tree_head_signature": base64.b64encode(bad.signature).decode(),
        }
    ).encode()
    with pytest.raises(CtLogVerificationFailed):
        CtLogClient(log, fetch=lambda url: payload).sth()
    # And verification can be switched off, which is a decision a caller has to make aloud.
    assert CtLogClient(log, fetch=lambda url: payload).sth(verify=False).tree_size
