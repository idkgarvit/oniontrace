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
    ORIGIN_AHMIA,
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
    sitemap_index_children,
    sitemap_urls,
)
from collect.errors import CollectionError
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

    @property
    def pages(self) -> list[str]:
        """What was requested of the source, without the robots policy probe.

        Every host is asked once per run for robots.txt (see `Collector._fetch`), so it appears in
        `requested`. Assertions about which pages a walk read mean pages.
        """
        return [url for url in self.requested if not url.endswith("/robots.txt")]


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


def test_an_index_of_sitemaps_is_not_read_as_a_list_of_pages():
    """A big site splits its map; the index names maps, and each one is followed once."""
    index = "<sitemapindex><sitemap><loc>https://x.invalid/news.xml</loc></sitemap></sitemapindex>"
    assert sitemap_index_children(index) == ("https://x.invalid/news.xml",)
    urlset = "<urlset><url><loc>https://x.invalid/a</loc></url></urlset>"
    assert sitemap_index_children(urlset) == ()
    assert sitemap_index_children("") == ()


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
                '<a href="/directory/one/">One</a>'
                '<a href="/directory/two/">Two</a>'
                '<a href="https://elsewhere.invalid/three">Off-host</a>'
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
    first_run = list(transport.pages)
    transport.requested.clear()

    collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    second_run = list(transport.pages)

    # Run one spends its four pages on the directory, its robots file, and two entries.
    assert f"{BASE}one/" in first_run and f"{BASE}two/" in first_run
    assert f"{BASE}three/" not in first_run
    # Run two skips what was already read and moves on to the entry it never reached.
    assert f"{BASE}three/" in second_run
    assert f"{BASE}one/" not in second_run, "the page already read was read again"
    assert f"{BASE}two/" not in second_run


def test_a_capped_walk_advances_past_the_prefix_it_already_read(gate, clock):
    """The cap must select unread pages, not the first N pages of the index.

    With a three-page budget the index and its robots file leave room for exactly one
    child. The earlier code truncated the listing to that one child and then skipped it
    as already read, so every retry fetched the index and no new page. Five children,
    three runs: each run must reach the next unread child.
    """
    names = ("one", "two", "three", "four", "five")
    pages = {
        BASE: Page(
            body="<html><body>"
            + "".join(f'<a href="/directory/{name}/">{name}</a>' for name in names)
            + "</body></html>"
        ),
    }
    for name in names:
        pages[f"{BASE}{name}/"] = Page(body="<html><body>leaf</body></html>")

    transport = make_transport(clock, pages)
    scope = CollectionScope(directories=(BASE,))

    seen: list[str] = []
    for _ in range(3):
        # A fresh collector per run, because the robots probe is cached on the instance:
        # reusing it gives the second run two spare pages and hides the one-page case.
        transport.requested.clear()
        Collector(
            gate=gate,
            transport=transport,
            clock=clock,
            policy=CollectionPolicy(
                politeness=Politeness(min_host_delay_seconds=0.0, max_pages_per_source=3)
            ),
        ).collect(
            case_id=CASE,
            principal=OWNER,
            scope=scope,
            origins=(ORIGIN_ONION_LOCATION,),
        )
        seen.extend(
            url.rsplit("/directory/", 1)[-1]
            for url in transport.requested
            if url.startswith(BASE) and not url.endswith("robots.txt")
        )

    assert [name for name in seen if name] == ["one/", "two/", "three/"], seen


def test_a_walked_directory_is_not_walked_again(gate, clock):
    transport = make_transport(clock, directory_pages())
    collector = collector_for(gate, transport, clock)
    scope = CollectionScope(directories=(BASE,))
    collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    first = len(transport.pages)

    second = collector.collect(
        case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,)
    )

    assert len(transport.pages) == first
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


# --------------------------------------------------------------------------- #
# The map the host declares, and starting from a front page
# --------------------------------------------------------------------------- #


def test_a_hosts_declared_sitemap_is_parsed_out_of_its_robots_file():
    from collect.robots import sitemaps

    text = (
        "User-agent: *\nDisallow:\n\n"
        "Sitemap: https://x.invalid/sitemap.xml\n"
        "sitemap: https://x.invalid/news.xml\n"
        "Sitemap: https://x.invalid/sitemap.xml\n"
    )
    assert sitemaps(text) == ("https://x.invalid/sitemap.xml", "https://x.invalid/news.xml")
    assert sitemaps("User-agent: *\nDisallow: /search/\n") == ()
    assert sitemaps("") == ()


def test_a_sparse_front_page_is_completed_from_the_sitemap_the_host_declared(gate, clock):
    """The operator names the host; the host names its pages. That is the whole point.

    A front page links to its navigation. The entries live on pages the site lists in the
    sitemap it declares in robots.txt, and a walk that only followed links would report a
    host with fifty entries as a host with none.
    """
    root = f"https://{HOST}/"
    pages = {
        root: Page(body='<html><body><a href="/about/">About</a></body></html>'),
        f"https://{HOST}/robots.txt": Page(
            body=f"User-agent: *\nDisallow:\nSitemap: {root}sitemap.xml\n"
        ),
        f"{root}sitemap.xml": Page(
            body=(
                '<?xml version="1.0"?><urlset>'
                f"<url><loc>{root}directory/one/</loc></url>"
                f"<url><loc>{root}directory/two/</loc></url>"
                "</urlset>"
            )
        ),
        f"{root}directory/one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
        f"{root}directory/two/": Page(body=f'<a href="http://{SECOND}/">two</a>'),
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(root,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert f"{root}sitemap.xml" in transport.requested
    assert {proposal.seed for proposal in report.proposals} == {FIRST, SECOND}
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("declared this sitemap in robots.txt" in note for note in summary.notes)


def test_a_page_that_already_fills_the_budget_does_not_fetch_its_sitemap(gate, clock):
    """Reading a map that can only repeat what the page already gave is a wasted page."""
    pages = {
        BASE: Page(
            body="<html><body>"
            '<a href="/directory/one/">One</a>'
            '<a href="/directory/two/">Two</a>'
            "</body></html>"
        ),
        f"{BASE}one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
        f"{BASE}two/": Page(body=f'<a href="http://{SECOND}/">two</a>'),
        f"https://{HOST}/robots.txt": Page(
            body=f"User-agent: *\nDisallow:\nSitemap: https://{HOST}/sitemap.xml\n"
        ),
        f"https://{HOST}/sitemap.xml": Page(
            body=f"<urlset><url><loc>{BASE}one/</loc></url></urlset>"
        ),
    }
    transport = make_transport(clock, pages)
    # Three pages for the run: the directory and its two entries. The page already names
    # both entries, so the walk has a full list and no reason to spend a page on the map.
    collector = Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(
            politeness=Politeness(min_host_delay_seconds=0.0, max_pages_per_source=3)
        ),
    )
    collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert f"https://{HOST}/sitemap.xml" not in transport.requested


def test_a_sitemap_index_is_followed_one_level(gate, clock):
    root = f"https://{HOST}/"
    pages = {
        root: Page(body='<html><body><a href="/about/">About</a></body></html>'),
        f"https://{HOST}/robots.txt": Page(
            body=f"User-agent: *\nDisallow:\nSitemap: {root}sitemap.xml\n"
        ),
        f"{root}sitemap.xml": Page(
            body=(
                "<sitemapindex>"
                f"<sitemap><loc>{root}maps/news.xml</loc></sitemap>"
                "</sitemapindex>"
            )
        ),
        f"{root}maps/news.xml": Page(
            body=f"<urlset><url><loc>{root}directory/one/</loc></url></urlset>"
        ),
        f"{root}directory/one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(root,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert f"{root}maps/news.xml" in transport.requested
    assert [p.seed for p in report.proposals] == [FIRST]


def test_a_sitemap_on_another_host_is_not_read(gate, clock):
    """A robots file is the host's own statement; it cannot widen the walk's scope."""
    root = f"https://{HOST}/"
    pages = {
        root: Page(body='<html><body><a href="/about/">About</a></body></html>'),
        f"https://{HOST}/robots.txt": Page(
            body="User-agent: *\nDisallow:\nSitemap: https://elsewhere.invalid/sitemap.xml\n"
        ),
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(root,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert not any("elsewhere.invalid" in url for url in transport.requested)
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("not this walk's host" in note for note in summary.notes)


def test_a_declared_sitemap_that_errors_does_not_cost_the_walk_its_pages(gate, clock):
    """An error page is not a map with nothing in it, and it must not end the walk."""
    root = f"https://{HOST}/"
    pages = {
        root: Page(body='<html><body><a href="/directory/one/">One</a></body></html>'),
        f"https://{HOST}/robots.txt": Page(
            body=f"User-agent: *\nDisallow:\nSitemap: {root}gone.xml\n"
        ),
        f"{root}gone.xml": Page(body="not found", status=404),
        f"{root}directory/one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(root,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert [p.seed for p in report.proposals] == [FIRST]
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("answered 404" in note for note in summary.notes)


def test_a_sitemap_offered_from_a_front_page_is_ordered_sections_first():
    """A map lists a site in publication order; `/directory/` must not queue behind 2026.

    Pointed at a front page there is no path prefix to prefer, so without an order the walk
    spends its budget on the newest headlines and never reaches the section that lists the
    entries.
    """
    xml = (
        "<urlset>"
        "<url><loc>https://x.invalid/news/2026/09/17/a-headline/</loc></url>"
        "<url><loc>https://x.invalid/news/2026/09/18/another/</loc></url>"
        "<url><loc>https://x.invalid/directory/</loc></url>"
        "<url><loc>https://x.invalid/about/team/history/</loc></url>"
        "</urlset>"
    )
    ordered = child_pages(xml, base_url="https://x.invalid/", limit=3)
    assert ordered[0] == "https://x.invalid/directory/"
    assert len(ordered) == 3


def test_the_map_is_not_read_to_repeat_what_the_page_already_named(gate, clock):
    """The shallowest pages in a map are usually exactly the front page's own links.

    Offering them again would spend the walk's room on pages it already has and leave the
    entries it does not have unread, so the page's list is excluded before the map fills.
    """
    root = f"https://{HOST}/"
    pages = {
        root: Page(body='<html><body><a href="/about/">About</a></body></html>'),
        f"https://{HOST}/robots.txt": Page(
            body=f"User-agent: *\nDisallow:\nSitemap: {root}sitemap.xml\n"
        ),
        f"{root}sitemap.xml": Page(
            body=(
                "<urlset>"
                f"<url><loc>{root}about/</loc></url>"
                f"<url><loc>{root}one/</loc></url>"
                "</urlset>"
            )
        ),
        f"{root}about/": Page(body="<p>no onion here</p>"),
        f"{root}one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(root,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert f"{root}one/" in transport.requested
    assert [p.seed for p in report.proposals] == [FIRST]
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("names 1 new page(s)" in note for note in summary.notes), summary.notes


def test_a_walk_that_reads_the_map_still_finishes_inside_its_budget(gate, clock):
    """The map is a page, so a run that reads one must plan for it.

    Before this, the cap was measured before the map fetch: the walk planned one page more
    than it had, dropped the last entry, and never marked the directory done -- a run that
    reads a map could not finish however large its budget.
    """
    root = f"https://{HOST}/"
    pages = {
        root: Page(body='<html><body><a href="/about/">About</a></body></html>'),
        f"https://{HOST}/robots.txt": Page(
            body=f"User-agent: *\nDisallow:\nSitemap: {root}sitemap.xml\n"
        ),
        f"{root}sitemap.xml": Page(
            body=(
                "<urlset>"
                f"<url><loc>{root}about/</loc></url>"
                + "".join(
                    f"<url><loc>{root}{name}/</loc></url>"
                    for name in ("one", "two", "three", "four", "five", "six")
                )
                + "</urlset>"
            )
        ),
        f"{root}about/": Page(body="<p>no onion here</p>"),
        f"{root}one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
        f"{root}two/": Page(body=f'<a href="http://{SECOND}/">two</a>'),
        f"{root}three/": Page(body=f'<a href="http://{THIRD}/">three</a>'),
    }
    transport = make_transport(clock, pages)
    collector = Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(
            politeness=Politeness(min_host_delay_seconds=0.0, max_pages_per_source=7)
        ),
    )
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(root,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    # Seven pages: the front page, robots, the map, and four pages under the directory.
    # The map offered six pages and the run had room for three of them after paying for
    # the map itself; the rest are for the next run, not for this one's budget.
    assert len(transport.requested) == 7, transport.requested
    assert f"{root}four/" not in transport.requested
    assert {p.seed for p in report.proposals} == {FIRST, SECOND, THIRD}
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert not any("budget ran out" in note for note in summary.notes), summary.notes


def test_a_walk_stops_before_its_permit_expires_rather_than_losing_everything(gate, clock):
    """The gate refuses an ingest made after the permit expires, and it refuses it late.

    A fifteen-minute permit and a host that asks for ten seconds between pages is a walk
    that cannot finish thirty pages in time. Without a deadline the walking succeeds, the
    ingest is then refused, and every page read is lost -- the run paid the host's
    politeness and stored nothing. Stopping first keeps what was read and leaves a cursor.
    """
    pages = {
        BASE: Page(
            body="<html><body>"
            '<a href="/directory/one/">One</a>'
            '<a href="/directory/two/">Two</a>'
            '<a href="/directory/three/">Three</a>'
            '<a href="/directory/four/">Four</a>'
            "</body></html>"
        ),
        f"{BASE}one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
        f"{BASE}two/": Page(body=f'<a href="http://{SECOND}/">two</a>'),
        f"{BASE}three/": Page(body=f'<a href="http://{THIRD}/">three</a>'),
        f"{BASE}four/": Page(body='<p>nothing here</p>'),
        # Four minutes and ten seconds between pages: four pages do not fit in the
        # fifteen minutes the gate allows, which is the whole point.
        f"https://{HOST}/robots.txt": Page(body="User-agent: *\nDisallow:\nCrawl-delay: 250\n"),
    }
    transport = make_transport(clock, pages)

    class SlowHost:
        def sleep(self, seconds: float) -> float:
            clock.advance(seconds=seconds)
            return seconds

    collector = collector_for(gate, transport, clock, waiter=SlowHost())
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    # The run stored what it read instead of raising a refusal at the end.
    stored = [
        s for s in gate.collected_snapshots(OWNER, CASE) if s["origin"] == ORIGIN_ONION_LOCATION
    ]
    assert len(stored) == 3
    assert [p.seed for p in report.proposals] == [FIRST, SECOND, THIRD]
    # The page it could not reach was not requested at all, and it says why.
    assert f"{BASE}four/" not in transport.requested
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("permit expires at" in note for note in summary.notes), summary.notes
    assert any("1 page(s) unread" in note for note in summary.notes), summary.notes


def test_a_fetch_that_would_cross_the_permit_deadline_is_not_attempted(gate, clock):
    """The deadline holds for every request, including a map read from inside the walk."""
    root = f"https://{HOST}/"
    pages = {
        # A page with no links, so the map is the only request the walk has to make.
        root: Page(body="<html><body><p>nothing here</p></body></html>"),
        # Fourteen minutes and forty seconds: the wait the host asks for is itself long
        # enough to eat a fifteen-minute permit, so the map cannot be fetched inside it.
        f"https://{HOST}/robots.txt": Page(
            body=f"User-agent: *\nDisallow:\nCrawl-delay: 880\nSitemap: {root}sitemap.xml\n"
        ),
        f"{root}sitemap.xml": Page(
            body=f"<urlset><url><loc>{root}one/</loc></url></urlset>"
        ),
        f"{root}one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
    }
    transport = make_transport(clock, pages)

    class SlowHost:
        def sleep(self, seconds: float) -> float:
            clock.advance(seconds=seconds)
            return seconds

    collector = collector_for(gate, transport, clock, waiter=SlowHost())
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(root,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert f"{root}sitemap.xml" not in transport.requested
    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    assert any("was not requested" in note for note in summary.notes), summary.notes


def test_a_path_that_raises_does_not_leave_its_permit_deadline_behind(gate, clock):
    """The deadline belongs to one path's permit, so it must not outlive that path.

    ``collect`` stamps the expiry before walking and clears it in a ``finally``. A path
    is allowed to raise -- a scope violation propagates by design, and a configuration
    error should -- so without that reset the stamp would stay set on a collector the
    caller may reuse, and later requests would be refused against a permit already spent.
    """
    pages = {BASE: Page(body=f'<a href="http://{FIRST}/">one</a>')}
    collector = collector_for(gate, make_transport(clock, pages), clock)

    with pytest.raises(CollectionError, match="unknown search provider"):
        collector.collect(
            case_id=CASE,
            principal=OWNER,
            scope=CollectionScope(
                search_queries=("market",), search_provider="no-such-index"
            ),
            origins=(ORIGIN_AHMIA,),
        )

    assert collector._permit_expires_at is None


def test_a_page_that_links_home_does_not_walk_itself_as_a_child(gate, clock):
    """The live failure, in one fixture.

    `https://freedom.press/` links to `/` from its own masthead. The walk read that link as
    a page underneath the directory, fetched the index a second time, and recorded it under
    the same checkpoint key the directory marker used -- so the directory was marked as
    walked while the note beside it said it was not. The next run then refused to continue
    and the site was frozen at seven pages.

    The first assertion is the fault: a home link must not become a child. The second is the
    symptom the fault caused, and it is the one an operator saw: the next run, handed a
    larger budget, fetched nothing at all and reported the index as already walked. Measured
    rather than assumed: removing the self-link drop fails the first assertion, while
    reverting the marker's key alone still passes this test. So the self-link drop is what
    makes the walk resume here, and the key change is why a page form nobody has written yet
    cannot collide the same way. Both are in place because a marker that can collide is a
    bug waiting for one.
    """
    # A front page, because that is where the bug bit: with no path prefix to prefer, the
    # list is depth-ordered and `/` has depth zero, so the self-link was the first child the
    # walk reached rather than the last.
    front = f"https://{HOST}/"
    pages = {
        front: Page(
            body="<html><body>"
            '<a href="/">Home</a>'
            '<a href="/one/">One</a>'
            '<a href="/two/">Two</a>'
            '<a href="/three/">Three</a>'
            "</body></html>"
        ),
        f"{front}one/": Page(body=f'<a href="http://{FIRST}/">one</a>'),
        f"{front}two/": Page(body=f'<a href="http://{SECOND}/">two</a>'),
        f"{front}three/": Page(body=f'<a href="http://{THIRD}/">three</a>'),
    }
    transport = make_transport(clock, pages)
    collector = Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(
            # Four pages: the robots lookup, the front page, and two of its three children. The
            # robots request is charged because it is a request, which is what leaves one child
            # for the second run to reach. That is the whole point of the test, so the number is
            # load-bearing and expected to be exact.
            politeness=Politeness(min_host_delay_seconds=0.0, max_pages_per_source=4)
        ),
    )
    scope = CollectionScope(directories=(front,))

    collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    first_run = list(transport.pages)
    assert first_run.count(front) == 1, "the walk fetched its own index page as a child"
    assert f"{front}three/" not in first_run, (
        "the first walk was supposed to be capped before the last child; without that there is no "
        "resume to test"
    )

    print("FIRST:", transport.pages)
    transport.requested.clear()
    second = collector.collect(case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,))
    second_run = list(transport.pages)
    print("SECOND:", second_run)
    print("NOTES:", [n for o in second.origins for n in o.notes][:4])

    assert f"{front}three/" in second_run, (
        "a page linking home froze the directory: the next run thought it was finished"
    )
    assert f"{front}one/" not in second_run


def test_a_truncated_index_says_that_the_cap_chose_not_the_site(gate, clock):
    """A capped list read as complete is how a walk reports a site it never finished."""
    links = "".join(f'<a href="/directory/p{i}/">{i}</a>' for i in range(6))
    pages = {
        BASE: Page(body=f"<html><body>{links}</body></html>"),
    }
    for i in range(6):
        pages[f"{BASE}p{i}/"] = Page(body=f"<p>page {i}</p>")
    transport = make_transport(clock, pages)
    collector = Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(
            politeness=Politeness(min_host_delay_seconds=0.0, max_pages_per_source=3)
        ),
    )
    scope = CollectionScope(directories=(BASE,))

    report = collector.collect(
        case_id=CASE, principal=OWNER, scope=scope, origins=(ORIGIN_ONION_LOCATION,)
    )

    summary = next(s for s in report.origins if s.origin == ORIGIN_ONION_LOCATION)
    truncated = [n for n in summary.notes if "more pages than this walk may carry" in n]
    assert truncated, summary.notes
    assert "a larger page budget walks further" in truncated[0]


def test_a_page_that_advertises_only_in_a_header_is_not_read_as_empty(gate, clock):
    """The live case: freedom.press publishes its onion in `Onion-Location` and nowhere else.

    The walk read eleven of its pages, wrote each response into the case's archive, and
    reported "0 address(es) across 11 page(s)" -- while the header naming fpfjxcrmw...onion
    sat in the WARC it had just written. A host that publishes its onion the way the standard
    provides is not a host with no onion.
    """
    pages = {
        BASE: Page(body='<html><body><a href="/directory/one/">One</a></body></html>'),
        f"{BASE}one/": Page(
            body="<html><body>nothing but prose</body></html>",
            headers={"Onion-Location": f"http://{FIRST}/"},
        ),
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)

    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert [proposal.seed for proposal in report.proposals] == [FIRST]
    stored = gate.collected_snapshots(OWNER, CASE)
    advertised = [s for s in stored if s["content"].get("form") == "header"]
    assert advertised, "the header advertisement was not stored as evidence"
    assert advertised[0]["source_url"] == f"{BASE}one/"
    assert advertised[0]["content"]["advertised_url"] == f"http://{FIRST}/"
    assert advertised[0]["content"]["onion_address"] == FIRST


def test_the_index_page_is_read_for_what_it_advertises_too(gate, clock):
    """A front-page walk starts on the front page, which is where the header usually is."""
    pages = {
        BASE: Page(
            body="<html><body>no links and no mentions</body></html>",
            headers={"Onion-Location": f"http://{SECOND}/"},
        )
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)

    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert [proposal.seed for proposal in report.proposals] == [SECOND]


def test_an_address_named_on_many_pages_is_proposed_once(gate, clock):
    pages = {
        BASE: Page(
            body='<html><body><a href="/directory/one/">One</a>'
            '<a href="/directory/two/">Two</a></body></html>'
        ),
        f"{BASE}one/": Page(
            body="<p>nothing</p>", headers={"Onion-Location": f"http://{FIRST}/"}
        ),
        f"{BASE}two/": Page(
            body="<p>nothing</p>", headers={"Onion-Location": f"http://{FIRST}/"}
        ),
    }
    transport = make_transport(clock, pages)
    collector = collector_for(gate, transport, clock)

    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(directories=(BASE,)),
        origins=(ORIGIN_ONION_LOCATION,),
    )

    assert [proposal.seed for proposal in report.proposals] == [FIRST]
