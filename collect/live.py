"""The live transport. Off by default, and unable to fetch anything until switched on.

    Read this before assuming anything about the code below.

What this module is
-------------------
The plan (B2) puts live collection behind an explicit ``authorised_mode`` flag, off by
default. This is that flag's implementation. No test in this repository exercises the
network path, because none can: this environment has no Tor circuit and no intention of
making one. **The byte-fetching code below is therefore unverified.** What *is* verified
by tests is everything in front of it -- the refusals. Do not upgrade "the guards are
tested" into "the fetcher works".

Why the refusals come first
---------------------------
``fetch`` checks in a deliberate order, and every check fires *before* any network library
is imported:

1. **Authorisation.** Without ``authorised_mode=True`` there is nothing to discuss.
2. **Credentials.** A URL with userinfo in it, or an ``Authorization``/``Cookie`` header,
   is refused. The plan's Telegram path is public preview only; a request that could carry
   a session is a different activity wearing this module's clothes.
3. **Declared hosts.** ``t.me``, the search engine, the CT log, and ``.onion`` names, and
   nothing else. Switching transports is not a way to widen scope.
4. **Onion hygiene.** A ``.onion`` target with no SOCKS proxy is refused: sending an onion
   name to ordinary DNS both fails and tells your resolver which hidden service you are
   looking for -- an operational-security disclosure about the analyst, not merely a bug.
5. **Dependencies.** Only now does it import anything, and lazily.

Only ``fetch`` imports network libraries, and only after the refusals pass, so importing
this module -- which ``collect/__init__.py`` does -- opens no sockets and pulls in no
network stack.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import LiveCollectionRefused, TransportUnavailable, UnknownHost
from .transport import FetchResult

#: Clearnet hosts the live transport will talk to without an operator adding more. Anything
#: else is scope creep.
#:
#: ``ahmia.fi`` was here and is deliberately gone: it publishes ``Disallow: /search/`` and
#: ``Disallow: /*?`` and then enforces that with a 302 to the root on every query, whatever
#: the user agent. Measured three times on 2026-10-23 (CASE-LIVE-1, -2, -3), each time the
#: same. A host that will not answer is not a host to leave in an allowance, because the
#: allowance is a statement about what the tool may do, and the tool may no longer do this.
#:
#: The ``public_index`` origin is kept rather than deleted: keyword discovery is a real
#: capability and the parser is not specific to any one index. It was shipped with no
#: provider at all, and it now has one that works locally (``collect/index.py``): an index
#: built from pages this project was allowed to read, so the path needs no host and cannot
#: be refused. The ``ahmia`` provider stays selectable so that its refusal is recorded
#: rather than forgotten. Pointing the origin at some third index instead is an operator
#: decision, made through ``extra_hosts``, and it belongs on the record.
#:
#: ``crt.sh`` is gone for a different and more interesting reason. It is not that the host
#: refuses; it is that no certificate log can answer the question this origin asks. A log is
#: append-only and ordered by sequence number, so there is no way to ask it "which
#: certificates name this onion" without an index to search. The question needs a database of
#: parsed logs, which is what crt.sh is, and a database that publishes ``Disallow: /`` for
#: every agent is a source this tool should not depend on. The capability moved rather than
#: disappeared: ``collect/ctlog_direct.py`` reads a log at the source and verifies its signed
#: tree head, which is a cryptographic claim instead of a query, and that work runs as its own
#: stage. The ``certificate_log`` origin is kept, with no provider, so a run that expects it to
#: find something stops and says why.
CLEARNET_ALLOWANCE: tuple[str, ...] = ("t.me",)

#: Names that always resolve to the local host, whatever the resolver says.
_LOCAL_NAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
})

#: NAT64 prefixes that embed an IPv4 address in the low 32 bits (RFC 6052). On a
#: dual-stack network with a DNS64 resolver, every IPv4-only hostname arrives as one of
#: these, so they must be decoded rather than blanket-refused: the embedded address is
#: what the transport would actually reach, and it is what must be judged.
_NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),        # well-known prefix
    ipaddress.ip_network("64:ff9b:1::/48"),      # local-use prefix
)


def _nat64_embedded_ipv4(addr: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """The IPv4 address a NAT64 address maps to, or None if it is not one.

    Without this, a DNS64 network produces two failures at once: legitimate IPv4-only
    hosts (ahmia.fi resolves to 64:ff9b::2e13:263f here) are refused as reserved, and the
    guard's real purpose is defeated in the other direction, because a check that treats
    the whole range as reserved cannot distinguish a public host from
    ``64:ff9b::7f00:1``, which is ``127.0.0.1``. Decoding the low 32 bits as an IPv4
    address resolves both: the embedded address is judged, so a public host passes and a
    tunnel to loopback or to cloud metadata is refused.
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    for net in _NAT64_PREFIXES:
        if addr in net:
            return ipaddress.IPv4Address(int(addr) & 0xFFFF_FFFF)
    return None


def is_non_public_address(host: str) -> tuple[bool, str]:
    """Is ``host`` an address the collector must never fetch, and why.

    This is an SSRF guard, and it exists because every byte this collector parses comes
    from a party that chose those bytes. A page cannot redirect us (redirects are off),
    but an *operator* can slip an address into the allowance and, more importantly, a
    hostname in that allowance can resolve to something private. Onion content is
    attacker-controlled, so treat the address space as hostile.

    Refused, with the reason:
      - loopback (127/8, ::1, and names that always mean the local host)
      - link-local (169.254/16, fe80::/10), which is where cloud instance metadata lives
      - private ranges (RFC 1918, fc00::/7, and carrier-grade NAT 100.64/10)
      - unspecified and multicast and reserved ranges
      - anything that is not an IP literal and cannot be resolved at all

    A name that fails to resolve is refused rather than passed through, because the
    transport would otherwise resolve it later, at fetch time, outside this check.

    Deliberately does not use ``is_global`` alone: that attribute's treatment of some
    ranges has changed across Python versions, so the explicit categories are checked
    as the primary test and ``is_global`` as a backstop.
    """
    name = host.strip().strip("[]").lower().rstrip(".")
    if not name:
        return True, "empty host"
    if name in _LOCAL_NAMES or name.endswith(".localhost"):
        return True, "loopback name"
    if name.endswith((".internal", ".local")):
        return True, "internal-only name suffix"

    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        candidates.append(ipaddress.ip_address(name))
    except ValueError:
        # Not an IP literal: resolve it, so the decision is made on the address the
        # transport would actually connect to rather than on the name.
        try:
            infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError) as exc:
            return True, f"unresolvable ({type(exc).__name__})"
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not candidates:
            return True, "no resolvable address"

    for addr in candidates:
        # A NAT64 address is judged by the IPv4 address it tunnels to, not by its own
        # (reserved) range. See _nat64_embedded_ipv4 for why this matters both ways.
        embedded = _nat64_embedded_ipv4(addr)
        if embedded is not None:
            if (embedded.is_loopback or embedded.is_link_local or embedded.is_private
                    or embedded.is_multicast or embedded.is_unspecified
                    or embedded.is_reserved or not embedded.is_global):
                return True, f"NAT64 address {addr} tunnels to non-public {embedded}"
            continue
        if addr.is_loopback:
            return True, f"loopback address {addr}"
        if addr.is_link_local:
            return True, f"link-local address {addr} (cloud metadata lives here)"
        if addr.is_private:
            return True, f"private address {addr}"
        if addr.is_multicast:
            return True, f"multicast address {addr}"
        if addr.is_unspecified:
            return True, f"unspecified address {addr}"
        if addr.is_reserved:
            return True, f"reserved address {addr}"
        if not addr.is_global:
            return True, f"non-global address {addr}"
    return False, ""

#: Headers that would turn an unauthenticated public fetch into an authenticated one.
CREDENTIAL_HEADERS: tuple[str, ...] = (
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
)

GATES: tuple[str, ...] = (
    "authorised_mode must be True",
    "no credentials in URL or headers",
    "target host inside the declared allowance",
    ".onion targets require a configured SOCKS proxy",
    "optional network dependencies installed by a human",
)


@dataclass(frozen=True)
class LiveSettings:
    """Everything a live run needs configured, with no default that reaches the network."""

    socks_host: str | None = None
    socks_port: int = 9050
    timeout_seconds: float = 20.0
    max_bytes: int = 2 * 1024 * 1024
    user_agent: str = "oniontrace/0.1 (public-preview research; no account, no cookies)"

    @property
    def has_socks_proxy(self) -> bool:
        return bool(self.socks_host)


class LiveTransport:
    """Tor and clearnet fetching, gated five ways, and unverified against a live network."""

    name = "tor-socks"

    def __init__(
        self,
        *,
        authorised_mode: bool = False,
        settings: LiveSettings | None = None,
        clock=None,
        extra_hosts: tuple[str, ...] = (),
    ) -> None:
        self.authorised_mode = bool(authorised_mode)
        self.settings = settings or LiveSettings()
        self._clock = clock
        self._hosts = tuple(CLEARNET_ALLOWANCE) + tuple(extra_hosts)

    # -- the guards, all of which are tested ------------------------------- #

    def check(self, url: str, headers: dict[str, str] | None = None) -> None:
        """Run every refusal. Raises before touching a network library.

        Public, so that a caller, a test or an operator can ask "would this be allowed?"
        without a fetch happening as a side effect.
        """
        if not self.authorised_mode:
            raise LiveCollectionRefused(
                "live collection is off by default. authorised_mode=True asserts that a "
                "human holds authorisation for this target, that the collection is within "
                "scope for the case, and that the operator accepts the risk of running "
                "against a live network. It is not a flag to set in a test, a demo, or a "
                "defaults file."
            )
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise LiveCollectionRefused(f"scheme {parts.scheme!r} is not permitted")
        if parts.username or parts.password:
            raise LiveCollectionRefused(
                "credentials in a URL are refused: this collector performs public, "
                "unauthenticated preview collection only, in every mode"
            )
        for name in headers or {}:
            if name.lower() in CREDENTIAL_HEADERS:
                raise LiveCollectionRefused(
                    f"header {name!r} would authenticate this request; authenticated "
                    "collection is outside the scope of this module"
                )
        host = (parts.hostname or "").lower()
        if not host:
            raise LiveCollectionRefused("refusing a URL with no host")
        # SSRF guard. Onion content is attacker-controlled, so a host that resolves into
        # loopback, link-local or a private range must be refused here rather than at
        # connect time. Onion names have no DNS record and are exempt by construction,
        # since SOCKS resolves them at the proxy and the name is not an address.
        #
        # One narrow exemption, and it is about the resolver rather than the host: an
        # allowlisted name that cannot be resolved here is refused by the full check, but
        # the name is a constant in this file rather than a value from a page, and under
        # socks5h the proxy is what resolves it. Inside an isolation jail there is no
        # resolver by design, so without this a well-contained run would be refused for
        # being well contained. Only the "unresolvable" verdict is exempt: a private,
        # loopback, link-local or IP-literal verdict still refuses even when allowlisted,
        # so nobody can add 127.0.0.1 to the allowance and reach it.
        allowlisted = host in self._hosts
        if not host.endswith(".onion"):
            blocked, why = is_non_public_address(host)
            if blocked and allowlisted and why.startswith("unresolvable"):
                blocked = False
            if blocked:
                # Two different refusals share this raise, so the second sentence has to
                # follow the reason: telling an operator about private address space when
                # the name simply did not resolve sends them looking in the wrong place.
                if why.startswith("unresolvable"):
                    remedy = (
                        "Live collection resolves every host before fetching it, and a name "
                        "that cannot be resolved here cannot be checked. Hosts outside the "
                        "allowance are not exempt from that."
                    )
                else:
                    remedy = (
                        "Live collection is public, unauthenticated preview only, and must "
                        "never reach localhost, link-local, or private address space."
                    )
                raise LiveCollectionRefused(f"refusing {host!r}: {why}. {remedy}")
        if not host.endswith(".onion") and host not in self._hosts:
            raise UnknownHost(
                f"{host!r} is outside the declared allowance {self._hosts}. Widening "
                "scope is a configuration decision, not a code change in this file."
            )
        if host.endswith(".onion") and not self.settings.has_socks_proxy:
            raise LiveCollectionRefused(
                f"refusing to resolve {host} without a SOCKS proxy: an onion name sent to "
                "ordinary DNS leaks the target to your resolver and will not resolve "
                "anyway. Configure LiveSettings.socks_host."
            )

    # -- the unverified part ----------------------------------------------- #

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        """Fetch ``url`` over Tor. GUARDED, AND NEVER EXECUTED BY A TEST HERE."""
        if self._clock is None:
            # Before the network, not after: a transport that fetched first and then
            # discovered it could not timestamp the capture would have performed a live
            # request it cannot account for.
            raise TransportUnavailable("LiveTransport needs a clock to timestamp captures")
        self.check(url, headers)
        module = self._require_dependency()
        proxies: dict[str, str] = {}
        if self.settings.has_socks_proxy:
            # socks5h: resolve the hostname at the proxy, so the target name never
            # reaches local DNS. The 'h' is the whole point of the setting.
            proxy = f"socks5h://{self.settings.socks_host}:{self.settings.socks_port}"
            proxies = {"http": proxy, "https": proxy}
        try:
            response = module.get(
                url,
                headers={"User-Agent": self.settings.user_agent, **(headers or {})},
                proxies=proxies or None,
                timeout=self.settings.timeout_seconds,
                allow_redirects=False,
            )
        except Exception as exc:  # noqa: BLE001 - dependency errors are opaque by nature
            raise TransportUnavailable(f"live fetch of {url} failed: {exc}") from exc
        return FetchResult(
            url=url,
            status=int(response.status_code),
            headers={str(k): str(v) for k, v in dict(response.headers).items()},
            body=response.content[: self.settings.max_bytes],
            retrieved_at=self._clock.now().isoformat(),
            transport=self.name,
        )

    @staticmethod
    def _require_dependency():
        """Import the optional dependency, or say exactly what is missing and why."""
        try:
            import requests  # noqa: PLC0415 - deliberately lazy; see module docstring
        except ImportError as exc:
            raise TransportUnavailable(
                "live collection needs the optional acquisition extras, which are not "
                "installed and are deliberately not hard dependencies: the acquisition "
                "dependencies from Part B2 (requests + PySocks). A test suite that can "
                "reach the network by default cannot be run safely, so the dependency "
                "stays optional and this path stays dark."
            ) from exc
        if not hasattr(requests, "get"):  # pragma: no cover - defensive
            raise TransportUnavailable("the imported requests module is not requests")
        return requests


def describe_gates() -> str:
    """For the CLI: the guards, in the order they fire."""
    return "\n".join(f"  {index}. {gate}" for index, gate in enumerate(GATES, start=1))

