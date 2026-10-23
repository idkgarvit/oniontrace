"""A publisher describing its own onion, parsed from the two forms that exist.

The value here is not "does a regex match". It is that a page is hostile input: anything can
be in a header or a meta tag, and the only thing worth recording is a well-formed onion
address on a URL the page itself supplied. Everything else must come back empty rather than
half-parsed, because a half-parsed pair is an attribution claim with a typo in it.
"""

from collect.onionlocation import (
    HEADER_NAME,
    advertisements,
    from_headers,
    from_html,
)

V3 = "juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd"
V3_OTHER = "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad"
V2 = "expyuzz4wqqyqhjn"

SOURCE = "https://example.org/where-to-find-us"


# --------------------------------------------------------------------------- #
# The header
# --------------------------------------------------------------------------- #


def test_a_header_advertisement_is_captured_with_its_source():
    found = from_headers({HEADER_NAME: f"http://{V3}.onion/"}, source_url=SOURCE)
    assert len(found) == 1
    assert found[0].onion_address == f"{V3}.onion"
    assert found[0].advertised_url == f"http://{V3}.onion/"
    assert found[0].source_url == SOURCE
    assert found[0].form == "header"
    assert found[0].is_v3


def test_the_header_name_is_matched_whatever_case_the_server_used():
    """HTTP field names are case-insensitive; a dict lookup that assumes one case finds none."""
    found = from_headers({"Onion-Location": f"http://{V3}.onion/"}, source_url=SOURCE)
    assert len(found) == 1
    found = from_headers({"ONION-LOCATION": f"http://{V3}.onion/"}, source_url=SOURCE)
    assert len(found) == 1


def test_two_onions_joined_into_one_header_are_two_advertisements():
    """Most clients merge repeated fields with a comma; splitting them back is not optional."""
    found = from_headers(
        {HEADER_NAME: f"http://{V3}.onion/, http://{V3_OTHER}.onion/"}, source_url=SOURCE
    )
    assert {advertisement.onion_address for advertisement in found} == {
        f"{V3}.onion",
        f"{V3_OTHER}.onion",
    }


def test_a_header_pointing_at_a_clearnet_url_is_not_this_signal():
    """A site naming another clearnet site is not publishing an onion, whatever the field says."""
    assert from_headers({HEADER_NAME: "https://example.org/"}, source_url=SOURCE) == ()


def test_a_malformed_onion_label_is_refused_rather_than_trimmed_into_shape():
    for bad in (
        f"http://{V3[:-1]}.onion/",          # 55 characters
        f"http://{V3}x.onion/",              # 57 characters
        f"http://{V3}.onion.evil.example/",  # onion as a label, clearnet as the host
        "http://.onion/",
        "http://abcdefgh.onion/",            # 8 characters: neither v2 nor v3
    ):
        assert from_headers({HEADER_NAME: bad}, source_url=SOURCE) == (), bad


def test_a_v2_address_is_captured_and_marked_as_one():
    """v2 is withdrawn, but a publisher who never migrated is a fact worth recording."""
    found = from_headers({HEADER_NAME: f"http://{V2}.onion/"}, source_url=SOURCE)
    assert len(found) == 1
    assert not found[0].is_v3


def test_a_bare_onion_without_a_scheme_is_refused():
    """The specification requires an absolute URL; accepting a bare host invites a guess."""
    assert from_headers({HEADER_NAME: f"{V3}.onion"}, source_url=SOURCE) == ()


# --------------------------------------------------------------------------- #
# The meta tag
# --------------------------------------------------------------------------- #


def test_a_meta_advertisement_is_captured():
    body = f'<html><head><meta http-equiv="onion-location" content="http://{V3}.onion/"></head></html>'
    found = from_html(body, source_url=SOURCE)
    assert len(found) == 1
    assert found[0].onion_address == f"{V3}.onion"
    assert found[0].form == "meta"


def test_the_meta_tag_is_read_whatever_the_attribute_order_or_quoting():
    for tag in (
        f'<meta http-equiv="onion-location" content="http://{V3}.onion/">',
        f'<meta content="http://{V3}.onion/" http-equiv="onion-location">',
        f"<meta http-equiv='Onion-Location' content='http://{V3}.onion/'>",
        f'<META HTTP-EQUIV="ONION-LOCATION" CONTENT="http://{V3}.onion/">',
    ):
        assert len(from_html(tag, source_url=SOURCE)) == 1, tag


def test_a_meta_tag_with_another_http_equiv_is_ignored():
    body = '<meta http-equiv="refresh" content="0; url=https://example.org/">'
    assert from_html(body, source_url=SOURCE) == ()


def test_a_meta_tag_with_no_content_attribute_is_ignored():
    assert from_html('<meta http-equiv="onion-location">', source_url=SOURCE) == ()


# --------------------------------------------------------------------------- #
# Both forms together
# --------------------------------------------------------------------------- #


def test_the_header_and_the_meta_tag_describing_one_onion_stay_two_records():
    """Two forms is one fact stated twice, but the forms carry different weight, so both stay.

    A header comes from the server; a meta tag from the document body. Collapsing them would
    throw away which party made the claim.
    """
    headers = {HEADER_NAME: f"http://{V3}.onion/"}
    body = f'<meta http-equiv="onion-location" content="http://{V3}.onion/">'
    found = advertisements(headers=headers, body=body, source_url=SOURCE)
    assert len(found) == 2
    assert {advertisement.form for advertisement in found} == {"header", "meta"}


def test_the_same_form_repeated_describing_the_same_onion_is_one_record():
    headers = {HEADER_NAME: f"http://{V3}.onion/, http://{V3}.onion/"}
    found = advertisements(headers=headers, body="", source_url=SOURCE)
    assert len(found) == 1


def test_a_page_with_nothing_to_say_returns_nothing():
    assert advertisements(headers={}, body="<html></html>", source_url=SOURCE) == ()
