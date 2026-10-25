"""Deterministic fixture pages: what the fixture transport serves instead of a network.

Everything here is generated from a seed, in ``farm/``'s style: same idea of
``deterministic_onion_address`` and ``deterministic_hex``, so a fixture set is
reproducible and a reported collection run can be replayed by anyone who knows the seed.

Three page families, matching the three acquisition paths in the plan:

* **Ahmia results**, listing onion addresses an analyst could propose as seeds.
* **Telegram public previews** (``t.me/s/<channel>``), built from the farm's persona
  posts so the text being collected is the same text M3's retrieval metrics run over.
  Pages are cut into history slices so ``?before=`` pagination has something to paginate.
* **A certificate-transparency log slice**, crt.sh-shaped, containing a mixture of
  onion-only, clearnet-only and "sauteed" certificates. Deliberately mixed: if every
  fixture certificate were a positive, the parser could not be caught treating a
  clearnet-only certificate as an association.

The shapes are approximations of the real markup, chosen to exercise the parsers. They
are not captured traffic, and a parser passing here is not evidence it survives the real
site.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from farm.model import FarmTruth, Persona, deterministic_hex

from .ahmia import AHMIA_HOST, search_url
from .checkpoints import CheckpointStore
from .clock import FrozenClock
from .ctlog import CTLOG_HOST, query_url
from .telegram import PREVIEW_HOST, preview_url
from .transport import FixtureTransport, Page

#: Anchor for every synthetic timestamp. Fixed, not "now", so captures are reproducible.
EPOCH = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)

FIRST_MESSAGE_ID = 1000
FIXTURE_HOSTS: tuple[str, ...] = (AHMIA_HOST, PREVIEW_HOST, CTLOG_HOST)


def channel_for(persona: Persona) -> str:
    """A public channel name for a persona, valid against the preview URL pattern."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in persona.handle)
    return f"chan{cleaned}"[:32]


def posted_at(day_index: int, hour_of_day: int) -> str:
    return (EPOCH + timedelta(days=day_index, hours=hour_of_day)).isoformat(timespec="seconds")


def preview_page_html(channel: str, messages: list[dict[str, object]]) -> str:
    """Render one page of a channel's public preview history."""
    parts = [
        "<html><head><title>Telegram: View @",
        html.escape(channel),
        "</title></head><body>",
        '<div class="tgme_channel_history js_channel_history">',
    ]
    for message in messages:
        parts.append(
            '<div class="tgme_widget_message_wrap js-widget_message_wrap" '
            f'data-post="{html.escape(channel)}/{message["message_id"]}">'
            '<div class="tgme_widget_message">'
            '<div class="tgme_widget_message_text js-message_text" dir="auto">'
            f'{html.escape(str(message["text"]))}'
            "</div>"
            f'<time datetime="{message["posted_at"]}">'
            f'{str(message["posted_at"])[:10]}</time>'
            "</div></div>"
        )
    parts.append("</div></body></html>")
    return "".join(parts)


def ahmia_results_html(query: str, candidates: list[tuple[str, str, str]]) -> str:
    """Render an Ahmia results page for ``query``.

    ``candidates`` are ``(address, title, snippet)``. The markup keeps the two structures
    ``collect.ahmia`` looks for -- the ``result-container`` anchor and the snippet
    paragraph -- and nothing else, so the parser cannot accidentally depend on layout it
    has not been shown.
    """
    parts = [
        "<html><head><title>Ahmia — Results for ",
        html.escape(query),
        "</title></head><body>",
        f'<div class="results">Results for {html.escape(query)}',
    ]
    for address, title, snippet in candidates:
        parts.append(
            '<div class="result"><a class="result-container" '
            f'href="http://{address}/"><strong>{html.escape(title)}</strong></a>'
            f'<p class="snippet">{html.escape(snippet)}</p></div>'
        )
    parts.append("</div></body></html>")
    return "".join(parts)


def ct_slice_json(rows: list[dict[str, object]]) -> str:
    """Render a crt.sh-shaped log slice: a JSON array of certificate records.

    Matches what ``ctlog.parse_entries`` reads -- ``id`` or ``leaf_hash`` for the log id,
    ``common_name``, a newline-separated ``name_value``, and the validity window.
    """
    return json.dumps(rows)




@dataclass
class FixtureSet:
    """Everything a fixture collection run needs, plus what it is allowed to touch."""

    transport: FixtureTransport
    onion_targets: tuple[str, ...]
    channels: tuple[str, ...]
    ct_queries: tuple[str, ...]
    seed_query: str
    pages_by_channel: dict[str, int] = field(default_factory=dict)

    def checkpoints(self, case_dir, clock) -> CheckpointStore:
        return CheckpointStore(case_dir, clock=clock)


def _history_pages(
    messages: list[dict[str, object]], *, page_size: int
) -> list[tuple[int | None, list[dict[str, object]]]]:
    """Cut a channel's history into ``?before=`` pages, newest slice first.

    The final entry is always an empty page. Real history ends that way -- the last
    backward step returns nothing -- and a collector that never sees an empty page never
    has to prove its walk terminates, which is the bug worth catching here.
    """
    ordered = sorted(messages, key=lambda m: int(m["message_id"]), reverse=True)
    pages: list[tuple[int | None, list[dict[str, object]]]] = []
    cursor: int | None = None
    remaining = list(ordered)
    while remaining:
        page, remaining = remaining[:page_size], remaining[page_size:]
        pages.append((cursor, page))
        cursor = min(int(m["message_id"]) for m in page)
    pages.append((cursor, []))
    return pages


def persona_messages(persona: Persona, *, limit: int = 12) -> list[dict[str, object]]:
    """Preview messages for a persona's channel, from the farm's own posts.

    Truncated rather than full-length: a preview page shows a message, not a corpus, and
    keeping the truncation visible in the fixture stops the collector from being credited
    with collecting text the preview never exposed.
    """
    out: list[dict[str, object]] = []
    for post in persona.posts[:limit]:
        out.append(
            {
                "message_id": FIRST_MESSAGE_ID + post.index,
                "posted_at": posted_at(post.day_index, post.hour_of_day),
                "text": post.text[:320],
            }
        )
    return out


def build_fixtures(
    farm: FarmTruth,
    *,
    clock: FrozenClock,
    seed: int = 20260929,
    service_limit: int = 8,
    channel_limit: int = 3,
    page_size: int = 3,
    ct_limit: int = 6,
    seed_query: str = "marketplace",
) -> FixtureSet:
    """Wire a deterministic fixture transport against a generated farm."""
    pages: dict[str, Page] = {}
    services = farm.services[:service_limit]

    for service in services:
        for endpoint in service.endpoints:
            url = f"http://{service.address}{endpoint.path}"
            pages[url] = Page(
                body=endpoint.body,
                status=endpoint.status,
                headers=dict(endpoint.headers),
            )

    candidates = [
        (
            service.address,
            service.title,
            f"Listings and messages about {service.topic}.",
        )
        for service in services
    ]
    pages[search_url(seed_query)] = Page(
        body=ahmia_results_html(seed_query, candidates),
        headers={"Content-Type": "text/html"},
    )

    channels: list[str] = []
    pages_by_channel: dict[str, int] = {}
    for persona in farm.personas[:channel_limit]:
        channel = channel_for(persona)
        channels.append(channel)
        messages = persona_messages(persona)
        history = _history_pages(messages, page_size=page_size)
        pages_by_channel[channel] = len(history)
        for cursor, page in history:
            url = preview_url(channel, before=cursor)
            pages[url] = Page(
                body=preview_page_html(channel, page),
                headers={"Content-Type": "text/html"},
            )

    ct_queries: list[str] = []
    rows: list[dict[str, object]] = []
    for index, service in enumerate(services[:ct_limit]):
        domain = f"svc{deterministic_hex(seed, service.service_id, 6)}.example-clear.net"
        # Cycle through the three certificate shapes so the parser has to distinguish
        # them instead of learning that every certificate it sees is an association.
        shape = index % 3
        if shape == 0:
            names = [domain, service.address]
        elif shape == 1:
            names = [service.address]
        else:
            names = [domain]
        query = domain if shape != 1 else service.address
        ct_queries.append(query)
        rows.append(
            {
                "id": 900_000 + index,
                "issuer_name": "Synthetic Test CA (fixture, not a real issuer)",
                "common_name": names[0],
                "name_value": "\n".join(names),
                "not_before": posted_at(index, 1),
                "not_after": posted_at(90 + index, 1),
            }
        )
    for query in ct_queries:
        pages[query_url(query)] = Page(
            body=ct_slice_json(rows), headers={"Content-Type": "application/json"}
        )

    transport = FixtureTransport(
        clock=clock,
        hosts=FIXTURE_HOSTS + tuple(service.address for service in services),
        pages=pages,
    )
    return FixtureSet(
        transport=transport,
        onion_targets=tuple(service.address for service in services),
        channels=tuple(channels),
        ct_queries=tuple(ct_queries),
        seed_query=seed_query,
        pages_by_channel=pages_by_channel,
    )

