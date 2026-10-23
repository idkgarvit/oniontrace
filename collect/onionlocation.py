"""Find onion services the way their operators intend them to be found: the operator says so.

Why this replaces a search index
--------------------------------
A search engine answers "give me onions matching this word". Every public engine that does
that refuses automated querying -- ahmia.fi publishes ``Disallow: /search/`` and enforces it
with a redirect -- and the alternatives are either curated lists of criminal marketplaces,
which this project will not touch, or directories of sensitive services, which it should not
enumerate. That is not a gap in the tooling, it is the state of the world, and no amount of
retrying closes it.

What operators actually publish is a pointer from their own clearnet site to their own onion
service, in one of two standard forms:

  * the ``Onion-Location`` HTTP response header (draft, widely deployed since 2021)
  * ``<meta http-equiv="onion-location" content="...">`` in the page itself

Both are the site saying "this onion is mine". That is a *self-declared* clearnet/onion pair,
which is stronger evidence than a search ranking for two reasons: the pair is asserted by the
party best placed to know, and the assertion is retrieval-evidence, because it was attached to
a response the collector actually received and hashed.

This is also the mechanism the paper's own citation studies (Syverson et al., PoPETs 2025), so
collecting it here keeps the project measuring something that exists rather than something
invented.

What this module does not do
----------------------------
It does not crawl. It parses one response at a time, given the headers and body of a page the
caller already fetched, so politeness, scope and retention stay in the caller where they
already live. It also refuses any advertised URL whose host is not a well-formed onion address:
a clearnet site pointing at another clearnet site is not this signal, whatever it calls the
header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

#: The response header, lowercased: HTTP field names are case-insensitive and the code below
#: reads from a plain dict built by whoever fetched the page.
HEADER_NAME = "onion-location"

#: v3 onion addresses are 56 base32 characters, v2 were 16. v2 is withdrawn but old pages
#: still advertise them, and recording one honestly is better than silently dropping it: a
#: reader can see that the publisher never migrated.
_V3_LABEL = re.compile(r"^[a-z2-7]{56}$")
_V2_LABEL = re.compile(r"^[a-z2-7]{16}$")


@dataclass(frozen=True)
class Advertisement:
    """One clearnet page declaring one onion service."""

    #: The onion host, lowercased, without a scheme or path.
    onion_address: str
    #: The URL exactly as published, so a reader can see what the operator wrote.
    advertised_url: str
    #: The clearnet page that carried it.
    source_url: str
    #: ``header`` or ``meta``, because the two have different evidentiary weight: a header
    #: comes from the server, a meta tag from the document body, and only one of those is
    #: guaranteed to have been set by whoever controls the response.
    form: str

    @property
    def is_v3(self) -> bool:
        return bool(_V3_LABEL.match(self.onion_address.removesuffix(".onion")))


def _label_is_valid(host: str) -> bool:
    if not host.endswith(".onion"):
        return False
    label = host.removesuffix(".onion")
    return bool(_V3_LABEL.match(label) or _V2_LABEL.match(label))


def _advertisement(value: str, *, source_url: str, form: str) -> Advertisement | None:
    """Turn one advertised URL into an Advertisement, or nothing if it is not an onion.

    Returns ``None`` rather than raising for a malformed or clearnet value: a page can carry
    anything in a header, and the caller is parsing a response, not validating a form.
    """
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        return None
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not _label_is_valid(host):
        return None
    return Advertisement(
        onion_address=host,
        advertised_url=text,
        source_url=source_url,
        form=form,
    )


def from_headers(headers: Mapping[str, str], *, source_url: str) -> tuple[Advertisement, ...]:
    """Every ``Onion-Location`` value in one response's headers.

    Repeated fields arrive merged by most clients, joined with a comma, so the value is split
    back apart: two onions advertised on one page are two advertisements, not one malformed
    URL.
    """
    found: list[Advertisement] = []
    for name, value in headers.items():
        if name.lower() != HEADER_NAME:
            continue
        for candidate in str(value).split(","):
            advertisement = _advertisement(candidate, source_url=source_url, form="header")
            if advertisement is not None:
                found.append(advertisement)
    return tuple(found)


_ATTR = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["']([^"']*)["']""")
_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)


def from_html(body: str, *, source_url: str) -> tuple[Advertisement, ...]:
    """Every ``<meta http-equiv="onion-location">`` in a page body.

    Deliberately a small parser rather than a DOM: this reads one tag out of a document that
    may be megabytes of hostile markup, and a regex over ``<meta ...>`` cannot be made to
    recurse into anything the way a tree builder can.
    """
    found: list[Advertisement] = []
    for tag in _META_TAG.findall(body or ""):
        attributes = {name.lower(): value for name, value in _ATTR.findall(tag)}
        if attributes.get("http-equiv", "").strip().lower() != HEADER_NAME:
            continue
        advertisement = _advertisement(
            attributes.get("content", ""), source_url=source_url, form="meta"
        )
        if advertisement is not None:
            found.append(advertisement)
    return tuple(found)


def advertisements(
    *, headers: Mapping[str, str] | None, body: str, source_url: str
) -> tuple[Advertisement, ...]:
    """Both forms, header first, deduplicated by the advertised URL.

    A page commonly publishes both, and they are usually identical. Header first because it
    survives a page that fails to render, and deduplicated because the two forms describing
    the same onion are one fact stated twice, not two findings.
    """
    seen: dict[str, Advertisement] = {}
    for advertisement in (
        *from_headers(headers or {}, source_url=source_url),
        *from_html(body, source_url=source_url),
    ):
        seen.setdefault(f"{advertisement.form}:{advertisement.advertised_url}", advertisement)
    return tuple(seen.values())
