"""Walking a publisher's index, and the constraints that keep the walk lawful.

A directory is the one acquisition path that follows links by itself, so it is the one that
needs its limits asserted rather than described: the same host, the run's budget, the host's
robots policy, and the delay the host asked for. Every test here feeds the collector a page
that would tempt it past one of those limits, and checks that it did not go.
"""

from __future__ import annotations

import pytest

from collect.clock import FrozenClock
from collect.collector import (
    ORIGIN_ONION_LOCATION,
    CollectionPolicy,
    CollectionScope,
    Collector,
)
from collect.directory import (
    child_pages,
    looks_like_sitemap,
    onions_in,
    same_host_pages,
    sitemap_urls,
)
from collect.schedule import Politeness
from collect.transport import FixtureTransport, Page
from gates.gate import CollectionGate
from gates.roles import Principal, Role

CASE = "CASE-DIR-001"
OWNER = Principal(principal_id="a.sharma", role=Role.CASE_OWNER)
HOST = "directory.invalid"
BASE = f"https://{HOST}/directory/"

#: Hand-made addresses: base32 has no 0, 1, 8 or 9.
FIRST = "a" * 56 + ".onion"
SECOND = "b" * 56 + ".onion"
THIRD = "c" * 56 + ".onion"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at("2026-10-06T09:00:00+00:00")


@pytest.fixture
def gate(tmp_path, clock):
    gate = CollectionGate(tmp_path / "gate", clock=clock.as_gate_clock())
    gate.acknowledge(OWNER)
    gate.open_case(
        OWNER,
        case_id=CASE,
        title="Directory walk",
        authorisation_ref="AUTH-2026-0200",
        retention_days=30,
    )
    return gate


class Recorder:
    """Wraps the fixture transport so a test can see what was and was not requested."""

    name = "fixture"

    def __init__(self, inner) -> None:
        self._inner = inner
        self.requested: list[str] = []

    def check(self, url: str, headers=None) -> None:
        self._inner.check(url, headers)

    def fetch(self, url: str):
        self.requested.append(url)
        return self._inner.fetch(url)


def make_transport(clock, pages: dict[str, Page]) -> Recorder:
    return Recorder(
        FixtureTransport(clock=clock, hosts=(HOST,), pages=pages)
    )


def collector_for(gate, transport, clock, *, waiter=None) -> Collector:
    return Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(politeness=Politeness(min_host_delay_seconds=0.0)),
        sleeper=waiter,
    )


# --------------------------------------------------------------------------- #
# Reading markup
# --------------------------------------------------------------------------- #


def test_a_sitemap_is_recognised_by_what_it_says_not_by_its_url():
    assert looks_like_sitemap('<?xml version="1.0"?><urlset>')
    assert looks_like_sitemap("<sitemapindex><sitemap><loc>x</loc></sitemap></sitemapindex>")
    assert not looks_like_sitemap("<!doctype html><html><body>hi</body></html>")


def test_a_sitemap_yields_its_urls_with_entities_resolved():
    xml = "<urlset><url><loc>https://x.invalid/a?b=1&amp;c=2</loc></url></urlset>"
    assert sitemap_urls(xml) == ("https://x.invalid/a?b=1&c=2",)


def test_a_links_target_is_read_from_the_markup_not_the_visible_text():
    """A directory publishes the address as a link; the target never reaches the text."""
    html = '<p><a href="http://' + FIRST + '/">our newsroom</a></p>'
    visible = html.split("<a")[0] + html.split("</a>")[1]
    assert FIRST not in visible
    assert onions_in(html) == (FIRST,)


def test_addresses_come_back_once_each_in_the_order_they_appear():
    html = f'<a href="http://{SECOND}/">b</a> <a href="http://{FIRST}/">a</a>'
    assert onions_in(html) == (SECOND, FIRST)


def test_a_link_to_another_host_is_not_followed():
    html = '<a href="https://elsewhere.invalid/page">x</a>'
    assert same_host_pages(["/local", "https://elsewhere.invalid/page"], base_url=BASE) == (
        f"https://{HOST}/local",
    )


def test_an_asset_is_not_a_page():
    assert same_host_pages(["/a.png", "/style.css", "/real"], base_url=BASE) == (
        f"https://{HOST}/real",
    )


def test_a_faceted_link_to_the_same_index_comes_last():
    """`?country=3` and `?topic=7` are the same page in six filters; the entries are not."""
    xml = (
        "<urlset>"
        f"<url><loc>{BASE}?country=3</loc></url>"
        f"<url><loc>{BASE}real-entry/</loc></url>"
        "</urlset>"
    )
    pages = child_pages(xml, base_url=BASE, limit=2)
    assert pages[0] == f"{BASE}real-entry/"


def test_the_directory_prefix_is_preferred_and_the_cap_is_kept():
    xml = (
        "<urlset>"
        f"<url><loc>{BASE}one/</loc></url>"
        f"<url><loc>https://{HOST}/elsewhere/</loc></url>"
        f"<url><loc>{BASE}two/</loc></url>"
        "</urlset>"
    )
    pages = child_pages(xml, base_url=BASE, limit=3)
    assert pages[0] == f"{BASE}one/" and pages[1] == f"{BASE}two/"
    assert child_pages(xml, base_url=BASE, limit=1) == (f"{BASE}one/",)


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


def directory_pages() -> dict[str, Page]:
    return {
        BASE: Page(
            body=(
                "<html><body><h1>Directory</h1>"
                f'<a href="/directory/one/">One</a>'
                f'<a href="/directory/two/">Two</a>'
                f'<a href="https://elsewhere.invalid/three">Off-host</a>'
                "</body></html>"
            )
        ),
        f"{BASE}one/": Page(
            body=f'<html><body><a href="http://{FIRST}/">newsroom one</a></body></html>'
        ),
        f"{BASE}two/": Page(body="<html><body>nothing here</body></html>"),
    }


def test_a_directory_page_yields_the_addresses_on_the_pages_under_it(gate, clock):
    transport = make_transport(clock, directory_pages())
    collector = collector_for(gate, transport, clock)

    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert [proposal.seed for proposal in report.proposals] == [FIRST]
    assert report.proposals[0].discovered_from == f"{BASE}one/"
    stored = [s for s in gate.collected_snapshots(OWNER, CASE) if s["origin"] == ORIGIN_ONION_LOCATION]
    assert len(stored) == 1
    assert stored[0]["source_url"] == f"{BASE}one/"
    assert stored[0]["content"]["directory"] == BASE
    assert FIRST in stored[0]["content"]["onions"]
    # The page with nothing on it is read and not stored: a snapshot with no address in it
    # would be a row in the case that answers no question the case asks.
    assert stored[0]["content"]["title"]


def test_the_walk_never_leaves_the_host_it_was_given(gate, clock):
    transport = make_transport(clock, directory_pages())
    collector = collector_for(gate, transport, clock)
    collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )
    assert all(HOST in url for url in transport.requested)
    assert not any("elsewhere.invalid" in url for url in transport.requested)


def test_the_delay_the_host_asked_for_is_the_delay_the_walk_waits(gate, clock):
    """securedrop.org asks for ten seconds; a walk that ignored it would be reading on terms
    the host stated and this tool declined."""
    pages = directory_pages()
    pages[f"https://{HOST}/robots.txt"] = Page(
        body="User-agent: *\nDisallow:\nCrawl-delay: 10\n"
    )
    transport = make_transport(clock, pages)
    waits: list[float] = []

    class Waiter:
        def sleep(self, seconds: float) -> float:
            waits.append(seconds)
            return seconds

    collector = collector_for(gate, transport, clock, waiter=Waiter())
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    # The throttle records its own (zero) waits as well; what matters is that every page
    # under the directory was preceded by the delay the host asked for.
    assert waits.count(10.0) == 2, waits
    assert max(waits) == 10.0
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("robots.txt asks for 10s" in note for note in summary.notes)


def test_a_capped_walk_resumes_where_it_stopped(gate, clock):
    """A budget that stops at the top of the list must not make the list unwalkable.

    This is how the live run behaved before the fix: it read nine pages, ran out of budget,
    saved no cursor, and the next run read the same nine and stopped in the same place. Per
    page progress is what turns a capped walk into a walk that finishes.
    """
    pages = {
        BASE: Page(
            body="<html><body>"
            '<a href="/directory/one/">One</a>'
            '<a href="/directory/two/">Two</a>'
            '<a href="/directory/three/">Three</a>'
            "</body></html>"
        ),
        f"{BASE}one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
        f"{BASE}two/": Page(body=f'<a href="http://{SECOND}/">two</a>'),
        f"{BASE}three/": Page(body=f'<a href="http://{THIRD}/">three</a>'),
    }
    transport = make_transport(clock, pages)
    collector = Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(
            politeness=Politeness(min_host_delay_seconds=0.0, max_pages_per_source=4)
        ),
    )
    scope = CollectionScope(directories=(BASE,))

    collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    first_run = list(transport.requested)
    transport.requested.clear()

    collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    second_run = list(transport.requested)

    # Run one spends its four pages on the directory, its robots file, and two entries.
    assert f"{BASE}one/" in first_run and f"{BASE}two/" in first_run
    assert f"{BASE}three/" not in first_run
    # Run two skips what was already read and moves on to the entry it never reached.
    assert f"{BASE}three/" in second_run
    assert f"{BASE}one/" not in second_run, "the page already read was read again"
    assert f"{BASE}two/" not in second_run


def test_a_walked_directory_is_not_walked_again(gate, clock):
    transport = make_transport(clock, directory_pages())
    collector = collector_for(gate, transport, clock)
    scope = CollectionScope(directories=(BASE,))
    collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    first = len(transport.requested)

    second = collector.collect(
        case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,)
    )

    assert len(transport.requested) == first
    summary = next(s for s in second.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("not walked again" in note for note in summary.notes)


def test_a_directory_that_errors_is_not_marked_as_read(gate, clock):
    pages = directory_pages()
    pages[BASE] = Page(body="gone", status=500)
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)
    scope = CollectionScope(directories=(BASE,))

    report = collector.collect(
        case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,)
    )
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("answered 500" in note for note in summary.notes)

    # A second run tries again rather than believing the directory was walked.
    collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    assert transport.requested.count(BASE) == 2


def test_an_address_already_declared_is_evidence_but_not_a_proposal(gate, clock):
    gate.add_seed(CASE, FIRST, principal=OWNER)
    transport = make_transport(clock, directory_pages())
    collector = collector_for(gate, transport, clock)

    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(onion_addresses=(FIRST,), directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert report.proposals == ()
    stored = [s for s in gate.collected_snapshots(OWNER, CASE) if s["origin"] == ORIGIN_ONION_LOCATION]
    assert stored and FIRST in stored[0]["content"]["onions"]
