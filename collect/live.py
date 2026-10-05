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

from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import LiveCollectionRefused, TransportUnavailable, UnknownHost
from .transport import FetchResult

#: The only clearnet hosts live collection may ever contact. Anything else is scope creep.
CLEARNET_ALLOWANCE: tuple[str, ...] = ("t.me", "ahmia.fi", "crt.sh")

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

