"""The transport seam, and the only transport the default configuration can use.

Why this file exists separately from ``collect.live``
-----------------------------------------------------
Everything here is capable of serving bytes from memory and nothing more. There is no
socket, no HTTP client, no subprocess, and no lazy import hiding in a function body.
That is an architectural claim, and ``tests/test_collect.py`` enforces it by parsing
this module and every other default-path module and asserting the absence of network
and process imports.

The live transport lives in ``collect/live.py`` and refuses to fetch anything unless it
is constructed with ``authorised_mode=True``. Choosing that flag is a human decision; it
is never a default.

The fixture transport models the two things a real one has to model: a page either
exists at a URL or it does not, and the same URL can return different bytes over time.
It is deliberately strict about both -- an unregistered URL raises rather than returning
an empty body, because an empty body recorded as an observation is worse than a crash.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from .errors import PageNotFound, UnknownHost


@dataclass(frozen=True)
class Page:
    """One page a host serves, as a fixture."""

    body: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def body_bytes(self) -> bytes:
        return self.body.encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body_bytes).hexdigest()


@dataclass(frozen=True)
class FetchResult:
    """What a transport returned for one URL at one moment."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    retrieved_at: str
    transport: str
    #: The body was cut at the transport's byte cap, so what is stored is a prefix of what the
    #: server sent. Recorded rather than inferred, because a truncated page and a short page look
    #: identical once they are bytes, and an analyst reading a finding off one needs to know which.
    truncated: bool = False

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def byte_length(self) -> int:
        return len(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None


class Transport(Protocol):
    """What the collector needs of a transport: one method, no credentials."""

    name: str

    def fetch(self, url: str) -> FetchResult:
        ...


class FixtureTransport:
    """Serves registered pages from memory. Opens no sockets. Holds no credentials.

    ``hosts`` declares which hosts exist. Asking for a host outside that set is a scope
    error and raises :class:`UnknownHost`; asking for a path on a known host that was
    never registered returns a 404, because that is what a real host does.

    ``fetched`` records every URL requested, in order. Tests use it to assert that the
    collector stayed inside the case's seed list and that pagination issued exactly the
    requests it claims to have issued.
    """

    name = "fixture"

    def __init__(self, *, clock, hosts: tuple[str, ...] = (), pages: dict[str, Page] | None = None):
        self._clock = clock
        self._hosts = tuple(hosts)
        self._pages: dict[str, Page] = dict(pages or {})
        self.fetched: list[str] = []
        # Pages never widen scope: a page on an undeclared host cannot be registered.

    @property
    def hosts(self) -> tuple[str, ...]:
        return self._hosts

    def register(self, url: str, page: Page) -> None:
        host = _host_of(url)
        if host not in self._hosts:
            raise UnknownHost(f"{url} is on {host!r}, which is not a declared fixture host")
        self._pages[url] = page

    def replace(self, url: str, page: Page) -> None:
        """Point a URL at new bytes, to model a service that changed between visits."""
        if url not in self._pages:
            raise PageNotFound(f"no fixture registered for {url}")
        self._pages[url] = page

    def fetch(self, url: str) -> FetchResult:
        host = _host_of(url)
        if host not in self._hosts:
            raise UnknownHost(
                f"refusing {url}: host {host!r} is not a declared fixture host. "
                "A seed outside the declared hosts is a scope error, not a 404."
            )
        self.fetched.append(url)
        page = self._pages.get(url)
        if page is None:
            page = Page(
                body="<h1>Not Found</h1>",
                status=404,
                headers={"Content-Type": "text/html"},
            )
        return FetchResult(
            url=url,
            status=page.status,
            headers=dict(page.headers),
            body=page.body_bytes,
            retrieved_at=self._clock.now().isoformat(),
            transport=self.name,
        )


def _host_of(url: str) -> str:
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0].split("?", 1)[0].lower()
