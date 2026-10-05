"""Ahmia seed discovery.

The plan (B2) says collection starts from "Ahmia seeds". This module parses an Ahmia
results page into onion addresses -- it does not decide what is worth collecting. The
step that matters legally happens *after* it: a discovered address becomes a case seed
only when a human adds it with ``gate.add_seed``, and only then can it be fetched.

So discovery being automated is not scope creep. The gate is the human decision, and
this module has no way to make one: it cannot register a seed, only propose a list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

AHMIA_HOST = "ahmia.fi"

# v3 onion addresses are 56 characters of the base32 alphabet, so the shape is exact
# rather than a loose "something.onion" match. A malformed address is therefore never
# silently accepted as a seed.
ONION_RE = re.compile(r"\b([a-z2-7]{56}\.onion)\b")


def search_url(query: str) -> str:
    """The results URL for one search term.

    The term is percent-encoded rather than interpolated. A query is input someone else
    chose, and an unencoded ``&`` or ``#`` in it would change the *request* instead of the
    search -- adding a parameter to a call whose parameters this package does not control.
    """
    return f"https://{AHMIA_HOST}/search/?{urlencode({'q': query})}"


@dataclass(frozen=True)
class SeedCandidate:
    address: str
    title: str
    snippet: str
    source_url: str

    @property
    def index_url(self) -> str:
        return f"http://{self.address}/"


def extract_addresses(text: str) -> list[str]:
    """Unique onion addresses in first-seen order.

    Order is preserved because the collector walks seeds in discovery order, and a set
    would make the walk order depend on hash randomisation -- which would make a
    collection run non-reproducible from its seed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in ONION_RE.finditer(text):
        address = match.group(1)
        if address not in seen:
            seen.add(address)
            out.append(address)
    return out


def parse_results(html: str, *, source_url: str = "") -> list[SeedCandidate]:
    """Pulls ``(address, title, snippet)`` triples out of a results page.

    Titles and snippets are kept because they are what an analyst reads when deciding
    whether a discovered address is in scope for the case at all. Dropping them would
    leave the human authorising collection with an address and nothing else.
    """
    candidates: list[SeedCandidate] = []
    for block in _result_blocks(html):
        addresses = extract_addresses(block["href"] + " " + block["title"])
        if not addresses:
            continue
        candidates.append(
            SeedCandidate(
                address=addresses[0],
                title=block["title"],
                snippet=block["snippet"],
                source_url=source_url,
            )
        )
    return candidates


def _result_blocks(html: str) -> list[dict[str, str]]:
    """Splits the results region into per-hit chunks.

    Tag-soup splitting on the ``result-container`` anchor: enough to recover one block
    per hit from the fixture pages, and honest about being a heuristic rather than a
    CSS engine.
    """
    blocks: list[dict[str, str]] = []
    parts = html.split('class="result-container"')[1:]
    for part in parts:
        href = _first('href="', '"', part)
        body = part.split(">", 1)[-1] if ">" in part else part
        title = _first("<strong>", "</strong>", body) or _first("<h3>", "</h3>", body)
        snippet = _first('<p class="snippet">', "</p>", body)
        blocks.append({"href": href, "title": title, "snippet": snippet})
    return blocks


def _first(open_token: str, close_token: str, text: str) -> str:
    start = text.find(open_token)
    if start < 0:
        return ""
    start += len(open_token)
    end = text.find(close_token, start)
    if end < 0:
        return ""
    return text[start:end].strip()
