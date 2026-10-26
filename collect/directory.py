"""Turning one publisher page into the many pages it points at.

The ``onion_location`` origin reads a page and records the onion the publisher declared for
itself. That works one page at a time, and most publishers do not advertise on every page:
they advertise on the page about their SecureDrop, in a directory entry, or in a sitemap that
lists every newsroom they run. Waiting to be handed each of those URLs one at a time is how a
discovery path ends up depending on the analyst's memory.

This module is the walk. Given the markup of a page, it returns the pages underneath it that
are worth reading, using the sitemap when the site publishes one and the links when it does
not. It reads nothing and decides nothing about onions; the collector does that, with the same
robots checks, the same budget, and the same refusal recording as any other fetch.

Two decisions are structural rather than convenient.

Same host only. A directory may link to an organisation's own site, and following that would
turn one declared read into a crawl of the internet, chosen by whoever wrote the page. The
walk stays on the host it was pointed at.

The directory prefix, when there is one, is preferred rather than required. ``/directory/``
pages are the ones that carry the entries, but a site that puts its entries at ``/securedrop/``
while linking from ``/directory/`` is not doing anything wrong, and refusing to follow would
be the tool inventing a rule the publisher never stated.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlsplit

from .ahmia import ONION_RE

__all__ = [
    "DEFAULT_PAGE_CAP",
    "child_pages",
    "looks_like_sitemap",
    "onions_in",
    "same_host_pages",
    "sitemap_index_children",
    "sitemap_urls",
]

#: How many pages one directory may hand the collector in a single walk. Deliberately small:
#: a sitemap is a list of everything a publisher has ever written, and following it whole
#: would make one `--directory` flag into a crawl of a site that agreed to be read, not
#: walked. The cap is spent under the run budget like every other page.
DEFAULT_PAGE_CAP = 40

_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)
_SKIP_SUFFIXES = (
    ".xml", ".json", ".txt", ".pdf", ".zip", ".gz", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".ico", ".css", ".js", ".webp", ".mp4", ".mp3",
)


def looks_like_sitemap(text: str) -> bool:
    """Whether this is an XML sitemap rather than an HTML page.

    Checked before parsing because the two formats disagree about what a link is. A sitemap
    wrapped in an HTML error page still says ``<loc>``, and treating it as HTML would return
    nothing while looking like a page with no links.
    """
    head = text[:2000].lstrip().lower()
    return head.startswith("<?xml") or "<urlset" in head or "<sitemapindex" in head


def sitemap_urls(text: str) -> tuple[str, ...]:
    """Every ``<loc>`` in a sitemap, in document order, entities resolved."""
    return tuple(unescape(match).strip() for match in _LOC_RE.findall(text) if match.strip())


def sitemap_index_children(text: str) -> tuple[str, ...]:
    """The sitemaps a sitemap *index* points at, or nothing when this is a plain urlset.

    A large site splits its map, and the declared URL then names other maps rather than
    pages. Without this the walk reads an index, finds every ``<loc>`` ending in ``.xml``,
    drops them as non-pages, and reports a host with a map as a host with nothing on it.
    """
    head = text[:2000].lower()
    if "<sitemapindex" not in head:
        return ()
    return sitemap_urls(text)


def same_host_pages(links: tuple[str, ...] | list[str], *, base_url: str) -> tuple[str, ...]:
    """Keep the http(s) links that stay on the host the walk started from.

    The page's own address is dropped. Almost every page links home -- the logo, the
    breadcrumb, the wordmark in the footer -- and a self-link is not a page underneath the
    one being read. Keeping it costs a fetch of a page already in hand, and, worse, it used
    to be recorded under the same checkpoint key as the directory itself, so reading the
    page a second time as its own child marked the whole directory as walked. That is how a
    capped walk became unresumable: the second run was told the directory had been done.
    """
    base_host = (urlsplit(base_url).hostname or "").lower()
    base_page = urlsplit(base_url)._replace(fragment="").geturl().rstrip("/")
    kept: list[str] = []
    for link in links:
        absolute = urljoin(base_url, link.strip())
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"}:
            continue
        if (parts.hostname or "").lower() != base_host:
            continue
        if parts.path.lower().endswith(_SKIP_SUFFIXES):
            continue
        cleaned = parts._replace(fragment="").geturl()
        if cleaned.rstrip("/") == base_page:
            continue
        if cleaned not in kept:
            kept.append(cleaned)
    return tuple(kept)


def _depth(url: str) -> int:
    """How many path segments a URL has. Shallow pages are sections; deep ones are articles."""
    return len([part for part in urlsplit(url).path.split("/") if part])


def child_pages(
    text: str,
    *,
    base_url: str,
    limit: int = DEFAULT_PAGE_CAP,
) -> tuple[str, ...]:
    """The pages worth reading underneath ``base_url``, from a sitemap or from links.

    Pages under the same path prefix as the page that was read come first, because a
    directory entry lives beside the directory. Pages elsewhere on the host follow, in
    document order, so a site that lists its entries somewhere else still yields them.
    """
    if looks_like_sitemap(text):
        candidates = same_host_pages(sitemap_urls(text), base_url=base_url)
    else:
        candidates = same_host_pages(_HREF_RE.findall(text), base_url=base_url)

    base_path = urlsplit(base_url).path
    prefix = base_path if base_path.endswith("/") else base_path + "/"
    if prefix == "/":
        # Pointed at a front page there is no path prefix to prefer, and a sitemap lists a
        # site in the order it published: `/news/2026/09/17/a-headline/` sits beside
        # `/directory/`. Sections are shallower than articles, so depth orders the list.
        # Python's sort is stable, so URLs of equal depth keep the host's own order.
        return tuple(sorted(candidates, key=_depth))[:limit]

    under_prefix = tuple(url for url in candidates if urlsplit(url).path.startswith(prefix))
    elsewhere = tuple(url for url in candidates if url not in set(under_prefix))
    # Facet links (`?language=14`, `?country=3`) sit under the same path prefix and lead back
    # to the same index, usually with the same site-wide address on every one of them. They
    # are worth having only after the pages that are actually different, so a capped walk
    # spends its budget on distinct entries rather than on the same page in six filters.
    plain = tuple(url for url in under_prefix if not urlsplit(url).query)
    faceted = tuple(url for url in under_prefix if urlsplit(url).query)
    ordered = plain + faceted + elsewhere
    return ordered[:limit]


def onions_in(text: str) -> tuple[str, ...]:
    """Distinct onion addresses in markup or text, in the order they first appear.

    The raw markup is searched rather than the rendered text, because a directory usually
    publishes the address as a link, and a link's target never reaches the visible text.
    """
    seen: list[str] = []
    for match in ONION_RE.finditer(text):
        address = match.group(1).lower()
        if address not in seen:
            seen.append(address)
    return tuple(seen)
