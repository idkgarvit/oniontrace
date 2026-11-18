"""A controlled, live onion lab: pages we publish, flaws we plant, truth we know.

Why this module exists
----------------------
Every number in the M2 report was measured on a generated farm. That is a legitimate way
to test rules and correlation, but it cannot answer "does the acquisition path work" or
"does the detector fire on a real HTTP response". This module builds the smallest live
experiment that can answer those two questions without measuring a stranger:

* eight onion services are published by the project's own disposable Tor;
* two of them are operator clusters of three, sharing a favicon, an analytics id and a
  contact handle;
* two singleton services are unrelated but carry the same stock vendor analytics id,
  which is the correlation trap: a naive matcher links them, a frequency cap must not;
* all nineteen detection rules have one planted positive, and a set of clean controls
  trips each rule's *shape* without its meaning;
* the collector reads the front pages and a fixed list of well-known paths through the
  real jail, and this module turns the WARC bytes back into endpoints and artefacts.

The truth is still constructed -- that is unavoidable without labelled real cases -- but
the bytes, headers, Tor rendezvous, robots checks, WARC records and parsing are real. The
report says exactly that; it does not upgrade "live path" to "real-world accuracy".

The server is deliberately small. It reads ``routes.json`` and serves one response per
path, with per-route headers, so a flaw can be a status body, a banner, a cookie, a date
offset or an ETag without any framework in the way.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #

#: The nineteen rule names in ``eval.detectors.misconfig.RULES``. Kept as a tuple here so
#: the lab cannot silently drift when a rule is added: the coverage test compares the two.
RULE_KINDS: tuple[str, ...] = (
    "server_status",
    "directory_autoindex",
    "phpinfo",
    "dot_git",
    "dot_env",
    "backup_file",
    "default_admin_panel",
    "robots_path_leak",
    "staging_page",
    "debug_endpoint",
    "readme_internal_hostname",
    "exif_gps_in_image",
    "verbose_error_path_disclosure",
    "apache_version_banner",
    "nginx_version_banner",
    "cookie_name_unique",
    "timezone_leak",
    "etag_pattern",
    "http_header_order",
)

#: Where each planted flaw lives. A path is not a guess: it is the conventional location
#: the corresponding rule is written to inspect.
FLAW_PATHS: dict[str, str] = {
    "server_status": "/server-status",
    "directory_autoindex": "/files/",
    "phpinfo": "/phpinfo.php",
    "dot_git": "/.git/config",
    "dot_env": "/.env",
    "backup_file": "/backup.php.bak",
    "default_admin_panel": "/admin/",
    "robots_path_leak": "/robots.txt",
    "staging_page": "/staging",
    "debug_endpoint": "/debug/vars",
    "readme_internal_hostname": "/README.txt",
    "exif_gps_in_image": "/photo.jpg",
    "verbose_error_path_disclosure": "/error",
    "apache_version_banner": "/.git/config",
    "nginx_version_banner": "/staging",
    "cookie_name_unique": "/admin/",
    "timezone_leak": "/files/",
    "etag_pattern": "/photo.jpg",
    "http_header_order": "/admin/",
}

#: Which service carries which flaw. Every rule appears exactly once, so the careful
#: table has nineteen positives and a miss is unambiguous.
PLANTED: dict[str, tuple[str, ...]] = {
    "alpha-1": ("server_status", "verbose_error_path_disclosure"),
    "alpha-2": ("directory_autoindex", "timezone_leak"),
    "alpha-3": ("phpinfo", "debug_endpoint"),
    "beta-1": ("dot_git", "readme_internal_hostname", "apache_version_banner"),
    "beta-2": ("dot_env", "exif_gps_in_image", "etag_pattern"),
    "beta-3": ("backup_file",),
    "gamma-1": ("default_admin_panel", "cookie_name_unique", "http_header_order"),
    "gamma-2": ("robots_path_leak", "staging_page", "nginx_version_banner"),
}

#: Routes that trip a rule's pattern without its meaning. The careful table must not fire
#: on any of these; the naive table is expected to.
CLEAN_CONTROLS: dict[str, tuple[str, ...]] = {
    "alpha-1": ("/photo.jpg",),
    "alpha-3": ("/admin/",),
    "gamma-1": ("/robots.txt", "/README.txt"),
    "gamma-2": ("/error",),
}

#: Stock vendor value. It appears on four services, including two cluster members, so a
#: frequency cap of three removes it while leaving the three-service cluster artefacts.
VENDOR_ANALYTICS = "UA-000000-1"
STOCK_SERVICES = frozenset({"alpha-2", "beta-2", "gamma-1", "gamma-2"})

SERVICES: tuple[tuple[str, str | None, str], ...] = (
    ("alpha-1", "alpha", "Paper Lantern Books"),
    ("alpha-2", "alpha", "Paper Lantern Outpost"),
    ("alpha-3", "alpha", "Paper Lantern Support"),
    ("beta-1", "beta", "Atlas Print Works"),
    ("beta-2", "beta", "Atlas Paper Supply"),
    ("beta-3", "beta", "Atlas Studio"),
    ("gamma-1", None, "North Pier Notes"),
    ("gamma-2", None, "South Quay Supply"),
)

CLUSTER_ARTIFACTS: dict[str, dict[str, str]] = {
    "alpha": {
        "analytics_id": "UA-ALPHA-4102",
        "contact_handle": "ops@alpha-lantern.example",
    },
    "beta": {
        "analytics_id": "UA-BETA-7731",
        "contact_handle": "desk@beta-atlas.example",
    },
}


@dataclass(frozen=True)
class LabRoute:
    path: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "headers": dict(self.headers),
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, path: str, data: dict[str, Any]) -> "LabRoute":
        return cls(
            path=path,
            status=int(data.get("status", 200)),
            headers=dict(data.get("headers") or {}),
            body=str(data.get("body") or ""),
        )


@dataclass
class LabService:
    service_id: str
    cluster_id: str | None
    title: str
    artifacts: dict[str, str]
    routes: dict[str, LabRoute]
    flaws: tuple[str, ...] = ()
    clean_controls: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "cluster_id": self.cluster_id,
            "title": self.title,
            "artifacts": dict(self.artifacts),
            "flaws": list(self.flaws),
            "clean_controls": list(self.clean_controls),
            "routes": {path: route.to_dict() for path, route in sorted(self.routes.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LabService":
        return cls(
            service_id=str(data["service_id"]),
            cluster_id=data.get("cluster_id"),
            title=str(data.get("title") or data["service_id"]),
            artifacts=dict(data.get("artifacts") or {}),
            routes={
                path: LabRoute.from_dict(path, route)
                for path, route in (data.get("routes") or {}).items()
            },
            flaws=tuple(data.get("flaws") or ()),
            clean_controls=tuple(data.get("clean_controls") or ()),
        )


@dataclass
class Lab:
    seed: int
    services: list[LabService]

    @property
    def service_ids(self) -> tuple[str, ...]:
        return tuple(service.service_id for service in self.services)

    def service(self, service_id: str) -> LabService:
        for service in self.services:
            if service.service_id == service_id:
                return service
        raise KeyError(service_id)

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for service in self.services:
            if service.cluster_id:
                out.setdefault(service.cluster_id, []).append(service.service_id)
        return out

    @property
    def fingerprint(self) -> str:
        """A digest over the lab's content, not over the digest field itself.

        ``to_dict`` carries the fingerprint, so hashing ``to_dict`` would recurse. The
        digest is computed over the fingerprint-free body and then written into the
        serialised form; that way an edited ``lab.json`` can be checked against its own
        ``fingerprint`` field instead of merely carrying it.
        """

        return hashlib.sha256(
            json.dumps(self._body(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    def _body(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "services": [service.to_dict() for service in self.services],
        }

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        body["fingerprint"] = self.fingerprint
        return body

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Lab":
        return cls(
            seed=int(data["seed"]),
            services=[LabService.from_dict(item) for item in data["services"]],
        )


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #


def _favicon_bytes(name: str) -> bytes:
    """Deterministic bytes standing in for a favicon.

    The value is a hash rather than a real image because the correlator hashes the bytes;
    what matters is that two services in the same cluster serve identical bytes and two
    services in different clusters do not.
    """

    return b"ONIONTRACE-LAB-FAVICON\x00" + hashlib.sha256(name.encode()).digest()


def _favicon_data_uri(name: str) -> str:
    return "data:image/png;base64," + base64.b64encode(_favicon_bytes(name)).decode("ascii")


def _page(
    *,
    title: str,
    tagline: str,
    favicon: str,
    analytics: str | None = None,
    vendor_analytics: str | None = None,
    contact: str | None = None,
) -> str:
    head = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{title}</title>",
        f'<link rel="icon" href="{favicon}">',
    ]
    if analytics:
        head.append(f'<meta name="analytics-id" content="{analytics}">')
    if vendor_analytics:
        head.append(f'<meta name="vendor-analytics" content="{vendor_analytics}">')
    head.append("</head><body>")
    head.append(f"<h1>{title}</h1>")
    head.append(f"<p>{tagline}</p>")
    if contact:
        head.append(f'<p><a href="mailto:{contact}">{contact}</a></p>')
    head.append('<p><a href="/about">About this service</a></p>')
    head.append("</body></html>")
    return "\n".join(head)


def _flaw_route(kind: str, *, title: str) -> LabRoute:
    """The response that makes one rule fire, with no other rule's signature in it."""

    path = FLAW_PATHS[kind]
    html = "text/html; charset=utf-8"
    if kind == "server_status":
        return LabRoute(path, body=(
            "<!doctype html><html><body><h1>Apache Server Status</h1>"
            "<p>Server uptime: 4 days 2 hours</p></body></html>"
        ), headers={"Content-Type": html})
    if kind == "directory_autoindex":
        return LabRoute(path, body=(
            "<!doctype html><html><body><h1>Index of /files/</h1>"
            "<pre>notes.txt\nledger.csv</pre></body></html>"
        ), headers={"Content-Type": html})
    if kind == "phpinfo":
        return LabRoute(path, body=(
            "<!doctype html><html><body><h1>PHP Version 8.2.1</h1>"
            "<p>Loaded Configuration File /etc/php.ini</p></body></html>"
        ), headers={"Content-Type": html})
    if kind == "dot_git":
        return LabRoute(path, body=(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
        ), headers={"Content-Type": "text/plain"})
    if kind == "dot_env":
        return LabRoute(path, body=(
            "APP_ENV=production\nSECRET_KEY=lab-only-not-a-secret\n"
        ), headers={"Content-Type": "text/plain"})
    if kind == "backup_file":
        return LabRoute(path, body=(
            "<?php\n$db = 'local';\necho 'ok';\n"
        ), headers={"Content-Type": "text/plain"})
    if kind == "default_admin_panel":
        return LabRoute(path, body=(
            f"<!doctype html><html><body><h1>Admin Login</h1>"
            f"<p>{title}: vendor default credentials are still enabled.</p></body></html>"
        ), headers={"Content-Type": html})
    if kind == "robots_path_leak":
        return LabRoute(path, body=(
            "User-agent: *\nDisallow: /internal-vault/\n"
        ), headers={"Content-Type": "text/plain"})
    if kind == "staging_page":
        return LabRoute(path, body=(
            f"<!doctype html><html><body><h1>{title}</h1>"
            "<p>Staging environment: not for public use.</p></body></html>"
        ), headers={"Content-Type": html})
    if kind == "debug_endpoint":
        return LabRoute(path, body=(
            '{"cmdline": ["/srv/app"], "memstats": {"Alloc": 1024}}'
        ), headers={"Content-Type": "application/json"})
    if kind == "readme_internal_hostname":
        return LabRoute(path, body=(
            f"{title}\nBuild host: 10.0.0.5\nDeploy with the internal runner.\n"
        ), headers={"Content-Type": "text/plain"})
    if kind == "exif_gps_in_image":
        return LabRoute(path, body=(
            "EXIF GPSLatitude=28.6139 GPSLongitude=77.2090\n"
        ), headers={"Content-Type": "image/jpeg"})
    if kind == "verbose_error_path_disclosure":
        return LabRoute(path, body=(
            "Traceback (most recent call last):\n"
            '  File "/srv/app/main.py", line 7, in <module>\n'
            "RuntimeError: lab failure\n"
        ), headers={"Content-Type": "text/plain"})
    if kind == "apache_version_banner":
        return LabRoute(path, body="<html><body>Configuration</body></html>",
                        headers={"Content-Type": html, "Server": "Apache/2.4.49"})
    if kind == "nginx_version_banner":
        return LabRoute(path, body="<html><body>Preview</body></html>",
                        headers={"Content-Type": html, "Server": "nginx/1.18.0"})
    if kind == "cookie_name_unique":
        return LabRoute(path, body="<html><body>Sign in</body></html>",
                        headers={"Content-Type": html,
                                 "Set-Cookie": "ot_session=lab; Path=/; HttpOnly"})
    if kind == "timezone_leak":
        return LabRoute(path, body="<html><body>Files</body></html>",
                        headers={"Content-Type": html,
                                 "Date": "Mon, 17 Nov 2026 01:00:00 -0530"})
    if kind == "etag_pattern":
        return LabRoute(path, body="<html><body>Photo</body></html>",
                        headers={"Content-Type": html, "ETag": 'W/"1a2b-3c4d"'})
    if kind == "http_header_order":
        return LabRoute(path, body="<html><body>Sign in</body></html>", headers={
            "Content-Type": html,
            "X-Powered-By": "lab",
            "Vary": "Accept",
            "Cache-Control": "no-store",
            "X-Request-Id": "lab-1",
        })
    raise KeyError(kind)


def _base_routes(service: LabService, *, tagline: str) -> dict[str, LabRoute]:
    """The clean shape every service shares, before a flaw is planted on it."""

    routes: dict[str, LabRoute] = {
        "/": LabRoute("/", body=_page(
            title=service.title,
            tagline=tagline,
            favicon=_favicon_data_uri(service.artifacts["favicon"]),
            analytics=service.artifacts.get("analytics_id"),
            vendor_analytics=service.artifacts.get("vendor_analytics"),
            contact=service.artifacts.get("contact_handle"),
        ), headers={"Content-Type": "text/html; charset=utf-8"}),
        "/about": LabRoute("/about", body=(
            f"<!doctype html><html><body><h1>About {service.title}</h1>"
            "<p>An independent service in this controlled lab.</p></body></html>"
        ), headers={"Content-Type": "text/html; charset=utf-8"}),
        "/robots.txt": LabRoute("/robots.txt", body="User-agent: *\nAllow: /\n",
                                headers={"Content-Type": "text/plain"}),
        "/admin/": LabRoute("/admin/", body=(
            "<!doctype html><html><body><h1>Admin Login</h1>"
            "<p>Sign in with your operator account.</p></body></html>"
        ), headers={"Content-Type": "text/html; charset=utf-8"}),
        "/README.txt": LabRoute("/README.txt", body=(
            "Contact the operator through the form on the front page.\n"
        ), headers={"Content-Type": "text/plain"}),
        "/error": LabRoute("/error", body="Something went wrong.\n",
                           headers={"Content-Type": "text/plain"}),
        "/photo.jpg": LabRoute("/photo.jpg", body=(
            "EXIF GPSLatitude=0 GPSLongitude=0\n"
        ), headers={"Content-Type": "image/jpeg"}),
    }
    # Paths the probe list asks for that this service does not publish. A 404 is still an
    # observation, and the naive rules that ignore status are expected to fire on it.
    for path in FLAW_PATHS.values():
        routes.setdefault(path, LabRoute(path, status=404, body="Not Found\n",
                                         headers={"Content-Type": "text/plain"}))
    return routes


def build_lab(seed: int = 20261117) -> Lab:
    """Build the whole lab, deterministically, with no network and no addresses."""

    services: list[LabService] = []
    for service_id, cluster_id, title in SERVICES:
        artifacts: dict[str, str] = {
            "favicon": hashlib.sha256(
                f"favicon:{cluster_id or service_id}".encode()
            ).hexdigest(),
        }
        if cluster_id:
            artifacts.update(CLUSTER_ARTIFACTS[cluster_id])
        if service_id in STOCK_SERVICES:
            artifacts["vendor_analytics"] = VENDOR_ANALYTICS
        service = LabService(
            service_id=service_id,
            cluster_id=cluster_id,
            title=title,
            artifacts=artifacts,
            routes={},
            flaws=PLANTED[service_id],
            clean_controls=CLEAN_CONTROLS.get(service_id, ()),
        )
        routes = _base_routes(service, tagline=(
            "Books, print and paper from an independent counter."
            if cluster_id == "alpha" else
            "Print, paper and studio work from an independent counter."
            if cluster_id == "beta" else
            "Notes and supplies from an independent counter."
        ))
        body_paths: set[str] = set()
        for kind in service.flaws:
            path = FLAW_PATHS[kind]
            replacement = _flaw_route(kind, title=title)
            existing = routes.get(path) or LabRoute(path)
            header_only = kind in (
                "apache_version_banner", "nginx_version_banner", "cookie_name_unique",
                "timezone_leak", "etag_pattern", "http_header_order",
            )
            if header_only:
                headers = dict(existing.headers)
                headers.update(replacement.headers)
                routes[path] = LabRoute(path, status=200, headers=headers,
                                        body=existing.body or replacement.body)
            else:
                if path in body_paths:
                    raise ValueError(f"{service_id}: two body flaws share {path}")
                body_paths.add(path)
                routes[path] = replacement
        # A clean control is a route whose body/headers deliberately trip a pattern. It is
        # marked here so `scan_service` can be scored on it, and it must not be a flaw.
        for path in service.clean_controls:
            if path in routes and path not in {FLAW_PATHS[k] for k in service.flaws}:
                routes[path] = _clean_control_route(path, title)
        service.routes = routes
        services.append(service)
    return Lab(seed=seed, services=services)


def _clean_control_route(path: str, title: str) -> LabRoute:
    html = {"Content-Type": "text/html; charset=utf-8"}
    if path == "/admin/":
        return LabRoute(path, body=(
            "<!doctype html><html><body><h1>Admin Login</h1>"
            "<p>Sign in with your operator account.</p></body></html>"
        ), headers=html)
    if path == "/robots.txt":
        return LabRoute(path, body="User-agent: *\nDisallow: /admin/\n",
                        headers={"Content-Type": "text/plain"})
    if path == "/README.txt":
        return LabRoute(path, body=(
            "Local check only: 127.0.0.1\n"
        ), headers={"Content-Type": "text/plain"})
    if path == "/error":
        return LabRoute(path, body=(
            "Traceback (most recent call last):\n  the lab failure has no path\n"
        ), headers={"Content-Type": "text/plain"})
    if path == "/photo.jpg":
        return LabRoute(path, body="EXIF GPSLatitude=0 GPSLongitude=0\n",
                        headers={"Content-Type": "image/jpeg"})
    if path == "/debug/vars":
        return LabRoute(path, status=404, body="Not Found\n",
                        headers={"Content-Type": "text/plain"})
    return LabRoute(path, status=404, body="Not Found\n",
                    headers={"Content-Type": "text/plain"})


# --------------------------------------------------------------------------- #
# Reading the live result
# --------------------------------------------------------------------------- #


def extract_artifacts(body: str) -> dict[str, str]:
    """The identifying values a front page carries, in the form the correlator scores.

    A data-URI favicon is used so the artefact is inside the one page the collector already
    reads. The value stored is the SHA-256 of the decoded bytes, which is what a real
    favicon hash would be; the base64 is transport, not identity.
    """

    out: dict[str, str] = {}
    icon = re.search(
        r'<link[^>]+rel="icon"[^>]+href="data:image/png;base64,([^"]+)"', body
    )
    if icon:
        try:
            out["favicon"] = hashlib.sha256(base64.b64decode(icon.group(1))).hexdigest()
        except (ValueError, TypeError):
            pass
    analytics = re.search(r'<meta name="analytics-id" content="([^"]+)"', body)
    if analytics:
        out["analytics_id"] = analytics.group(1)
    vendor = re.search(r'<meta name="vendor-analytics" content="([^"]+)"', body)
    if vendor:
        out["vendor_analytics"] = vendor.group(1)
    contact = re.search(r'href="mailto:([^"]+)"', body)
    if contact:
        out["contact_handle"] = contact.group(1)
    return out


@dataclass(frozen=True)
class Observed:
    path: str
    status: int
    headers: dict[str, str]
    body: str

    @property
    def byte_length(self) -> int:
        return len(self.body.encode("utf-8"))


def parse_warc(text: str) -> dict[str, dict[str, Observed]]:
    """Map ``host -> path -> response`` from the collector's WARC-style stream.

    The WARC block carries a ``Content-Length`` for the HTTP block, so the body is sliced by
    that number rather than by the boundary marker. The parser walks one record at a time:
    it reads a header block, jumps exactly ``Content-Length`` bytes, and only then looks for
    the next record. Splitting the whole stream on ``WARC/1.0`` would be shorter and wrong,
    because a page is allowed to contain that text.

    The walk is over UTF-8 bytes, not over characters: ``Content-Length`` counts bytes, and
    a page with a non-ASCII character would otherwise end the slice early.
    """

    out: dict[str, dict[str, Observed]] = {}
    raw = text.encode("utf-8")
    marker = b"WARC/1.0\r\n"
    pos = 0
    while True:
        start = raw.find(marker, pos)
        if start < 0:
            break
        head_end = raw.find(b"\r\n\r\n", start)
        if head_end < 0:
            break
        head = raw[start + len(marker):head_end].decode("utf-8", "replace")
        fields: dict[str, str] = {}
        for line in head.split("\r\n"):
            if ":" in line:
                name, value = line.split(":", 1)
                fields[name.strip().lower()] = value.strip()
        block_start = head_end + 4
        try:
            length = int(fields["content-length"])
        except (KeyError, ValueError):
            # Without a length there is no statement about where this record ends, and
            # guessing would put the next record's header into this body. Stop instead.
            break
        block = raw[block_start:block_start + length]
        # Advance past the block before searching again, so a marker inside the body
        # cannot be mistaken for the start of a record.
        pos = block_start + length
        if fields.get("warc-type") != "response":
            continue
        http_head, _, http_body = block.partition(b"\r\n\r\n")
        lines = http_head.decode("utf-8", "replace").split("\r\n")
        if not lines or not lines[0].startswith("HTTP/"):
            continue
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            continue
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip()] = value.strip()
        uri = fields.get("warc-target-uri", "")
        host = (urlsplit(uri).hostname or "").lower()
        path = urlsplit(uri).path or "/"
        if not host:
            continue
        out.setdefault(host, {})[path] = Observed(
            path=path,
            status=status,
            headers=headers,
            body=http_body.decode("utf-8", "replace"),
        )
    return out


def observations_from_warc(paths: Iterable[Path]) -> dict[str, dict[str, Observed]]:
    """Merge every WARC file in a case into one host → path → response map.

    Read as bytes, not with ``read_text``. Universal-newline translation turns the WARC's
    CRLF block separators into LF, and a parser that looks for ``WARC/1.0\\r\\n`` then finds
    nothing -- which is exactly what happened on the first live run while the in-memory
    test kept passing.
    """

    merged: dict[str, dict[str, Observed]] = {}
    for path in sorted(paths):
        text = path.read_bytes().decode("utf-8", "replace")
        for host, pages in parse_warc(text).items():
            merged.setdefault(host, {}).update(pages)
    return merged


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _farm_from_services(lab: Lab, services: list[Any]):
    """Build the farm-shaped object the existing scorers already consume.

    Reusing ``FarmTruth`` is deliberate: the live experiment is scored by the same code as
    the synthetic one, so a difference in the result is a difference in the data, not in a
    second implementation of precision and recall.
    """

    from farm.model import Cluster, FarmConfig, FarmTruth, Flaw, FlawScope, RuleClass

    clusters = [
        Cluster(cluster_id=cluster_id, service_ids=service_ids,
                shared_artifacts={})
        for cluster_id, service_ids in lab.clusters().items()
    ]
    flaws = []
    for service in lab.services:
        for kind in service.flaws:
            flaws.append(Flaw(
                flaw_id=f"live-{service.service_id}-{kind}",
                kind=kind,
                scope=FlawScope.SINGLE,
                rule_class=RuleClass.CONFIG,
                service_ids=[service.service_id],
                path=FLAW_PATHS[kind],
                signal=kind,
            ))
    return FarmTruth(
        config=FarmConfig(),
        clusters=clusters,
        services=services,
        flaws=flaws,
    )


def build_observed_farm(lab: Lab, addresses: dict[str, str],
                        observed: dict[str, dict[str, Observed]]):
    """A ``FarmTruth`` whose endpoints and artefacts came from live bytes.

    The service id stays the lab's id so truth and observation join without a lookup table;
    the onion address is kept on the service for the record.
    """

    from farm.model import Endpoint, Service

    services = []
    for service in lab.services:
        address = addresses[service.service_id]
        pages = observed.get(address)
        if not pages:
            raise KeyError(
                f"no live response was recorded for {service.service_id} ({address}); "
                f"the WARC holds: {', '.join(sorted(observed)) or 'nothing'}"
            )
        front = pages.get("/")
        if front is None:
            raise KeyError(f"{service.service_id} has no front page in the live result")
        endpoints = [
            Endpoint(
                path=page.path,
                body=page.body,
                status=page.status,
                headers=dict(page.headers),
                is_clean_control=page.path in service.clean_controls,
            )
            for page in sorted(pages.values(), key=lambda item: item.path)
        ]
        shared = extract_artifacts(front.body)
        services.append(Service(
            service_id=service.service_id,
            cluster_id=service.cluster_id,
            address=address,
            title=service.title,
            topic="lab",
            endpoints=endpoints,
            shared_artifacts=shared,
            flaws=list(service.flaws),
        ))
    return _farm_from_services(lab, services)


def build_lab_farm(lab: Lab):
    """The same farm without live bytes, for tests and for checking the lab's design."""

    from farm.model import Endpoint, Service

    services = []
    for service in lab.services:
        endpoints = [
            Endpoint(
                path=route.path,
                body=route.body,
                status=route.status,
                headers=dict(route.headers),
                is_clean_control=route.path in service.clean_controls,
            )
            for route in sorted(service.routes.values(), key=lambda item: item.path)
        ]
        services.append(Service(
            service_id=service.service_id,
            cluster_id=service.cluster_id,
            address=service.service_id,
            title=service.title,
            topic="lab",
            endpoints=endpoints,
            shared_artifacts=dict(service.artifacts),
            flaws=list(service.flaws),
        ))
    return _farm_from_services(lab, services)


def _path_accuracy(farm, lab: Lab) -> dict[str, int]:
    """How many detections landed on the path the flaw was planted at.

    ``misconfig.evaluate`` scores by (service, rule), which is the farm's definition. A
    live run can be sharper: here the planted path is known, so a rule that fires on the
    right service at the wrong page is counted separately rather than as a clean hit.
    """

    from eval.detectors import misconfig

    expected = {
        (service.service_id, kind): FLAW_PATHS[kind]
        for service in lab.services
        for kind in service.flaws
    }
    hits = [
        hit for service in farm.services
        for hit in misconfig.scan_service(farm, service.service_id)
    ]
    correct = {
        (hit.service_id, hit.kind)
        for hit in hits
        if expected.get((hit.service_id, hit.kind)) == hit.path
    }
    wrong_path = {
        (hit.service_id, hit.kind)
        for hit in hits
        if (hit.service_id, hit.kind) in expected
        and expected[(hit.service_id, hit.kind)] != hit.path
    }
    return {
        "expected": len(expected),
        "on_planted_path": len(correct),
        "on_wrong_path": len(wrong_path),
    }


def score(lab: Lab, addresses: dict[str, str],
          observed: dict[str, dict[str, Observed]]) -> dict[str, Any]:
    """Score the live acquisition with the project's own detectors and metrics."""

    from eval.detectors import correlation, misconfig
    from eval.harness import correlation_experiment

    farm = build_observed_farm(lab, addresses, observed)
    careful = misconfig.evaluate(farm)
    naive = misconfig.evaluate(farm, misconfig.NAIVE_RULES)

    cluster_sizes = [len(ids) for ids in lab.clusters().values()]
    cap = max(cluster_sizes) if cluster_sizes else 1
    naive_corr = correlation_experiment(farm, max_artifact_frequency=None)
    filtered_corr = correlation_experiment(farm, max_artifact_frequency=cap)

    singletons = [service.service_id for service in lab.services if not service.cluster_id]
    abstention = {"naive": 0, "filtered": 0, "queries": len(singletons)}
    for service_id in singletons:
        pool = farm.correlation_pool(service_id)
        if not correlation.rank_services(farm, service_id, pool,
                                         max_artifact_frequency=None):
            abstention["naive"] += 1
        if not correlation.rank_services(farm, service_id, pool,
                                         max_artifact_frequency=cap):
            abstention["filtered"] += 1

    return {
        "live": {
            "services": len(lab.services),
            "clusters": len(lab.clusters()),
            "addresses": len(addresses),
            "routes_observed": sum(len(pages) for pages in observed.values()),
            "clean_controls": sum(len(s.clean_controls) for s in lab.services),
            "lab_fingerprint": lab.fingerprint,
        },
        "detection": {
            "careful": careful.to_dict(farm),
            "naive": naive.to_dict(farm),
            "path_accuracy": _path_accuracy(farm, lab),
        },
        "correlation": {
            "cap": cap,
            "naive": naive_corr,
            "filtered": filtered_corr,
            "singleton_abstention": abstention,
        },
        "scope_limit": (
            "live bytes, constructed truth: the services are ours and the flaws are "
            "planted; this measures the live path, not accuracy on third-party services"
        ),
    }


def write_report(result: dict[str, Any], out_dir: Path, *, case_id: str) -> tuple[Path, Path]:
    """Write the machine record and a short markdown table beside it."""

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "live-lab.json"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")

    live = result["live"]
    detection = result["detection"]
    corr = result["correlation"]
    careful = detection["careful"]
    naive = detection["naive"]
    path = detection["path_accuracy"]

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{100 * float(value):.1f}%"

    lines = [
        "# ONIONTRACE live controlled lab",
        "",
        f"Case: `{case_id}`  ",
        f"Lab fingerprint: `{live['lab_fingerprint']}`  ",
        f"Services: {live['services']} in {live['clusters']} clusters; "
        f"{live['addresses']} onion addresses; {live['routes_observed']} live responses",
        "",
        "The services and flaws are ours. The Tor rendezvous, HTTP responses, headers, "
        "WARC records and parsing are real; the truth is constructed, so this measures the "
        "live path rather than accuracy on third-party services.",
        "",
        "## Detection",
        "",
        "| table | recall | precision | TP | FP | FN |",
        "|---|---|---|---|---|---|",
        f"| careful | {pct(careful['recall']['value'] if careful['recall'] else None)} | "
        f"{pct(careful['precision']['value'] if careful['precision'] else None)} | "
        f"{careful['tp']} | {careful['fp']} | {careful['fn']} |",
        f"| naive | {pct(naive['recall']['value'] if naive['recall'] else None)} | "
        f"{pct(naive['precision']['value'] if naive['precision'] else None)} | "
        f"{naive['tp']} | {naive['fp']} | {naive['fn']} |",
        "",
        f"Planted-path accuracy: {path['on_planted_path']}/{path['expected']} on the "
        f"planted path, {path['on_wrong_path']} on a different path.",
        "",
        "## Correlation",
        "",
        f"Frequency cap: {corr['cap']}.",
        "",
        "| variant | precision@1 | link precision | links | TP | FP |",
        "|---|---|---|---|---|---|",
    ]
    for name in ("naive", "filtered"):
        block = corr[name]
        ranking = block["ranking"]
        links = block["link_scoring"]
        lines.append(
            f"| {name} | {pct(ranking['precision@1']['value'])} | "
            f"{pct(links.get('precision'))} | "
            f"{links['proposed']} | {links['tp']} | {links['fp']} |"
        )
    abstention = corr["singleton_abstention"]
    lines += [
        "",
        f"Singleton abstention: filtered {abstention['filtered']}/{abstention['queries']}, "
        f"naive {abstention['naive']}/{abstention['queries']}.",
        "",
        "## Scope",
        "",
        result["scope_limit"] + ".",
        "",
        "Reproduce: `python3 scripts/run_live_lab.py` (publishes eight short-lived "
        "services, collects them through the jail, scores, then stops and removes the "
        "publishing Tor).",
        "",
    ]
    md_path = out_dir / "live-lab.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------- #
# The static lab server
# --------------------------------------------------------------------------- #

class _LabHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        path = urlsplit(self.path).path
        route = self.server.routes.get(path)  # type: ignore[attr-defined]
        if route is None:
            route = LabRoute(path, status=404, body="Not Found\n",
                             headers={"Content-Type": "text/plain"})
        body = route.body.encode("utf-8")
        self.send_response_only(route.status)
        headers = {
            "Server": "nginx",
            "Date": formatdate(usegmt=True),
            "Content-Type": "text/html; charset=utf-8",
        }
        headers.update(route.headers)
        headers.setdefault("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # The lab is eight services answering a few dozen requests. Logging every one makes
        # the transcript unreadable and tells an observer nothing the WARC does not.
        return


def serve(service_dir: Path, port: int) -> None:
    data = json.loads((service_dir / "routes.json").read_text(encoding="utf-8"))
    routes = {
        path: LabRoute.from_dict(path, route)
        for path, route in (data.get("routes") or data).items()
    }
    server = ThreadingHTTPServer(("127.0.0.1", port), _LabHandler)
    server.routes = routes  # type: ignore[attr-defined]
    print(f"serving {service_dir} on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="write the lab's routes and ground truth")
    build.add_argument("--out", required=True, type=Path)
    build.add_argument("--seed", type=int, default=20261117)

    srv = sub.add_parser("serve", help="serve one lab service from routes.json")
    srv.add_argument("service_dir", type=Path)
    srv.add_argument("port", type=int)

    args = parser.parse_args(argv)
    if args.command == "build":
        lab = build_lab(args.seed)
        for service in lab.services:
            directory = args.out / "services" / service.service_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "routes.json").write_text(
                json.dumps({"routes": service.to_dict()["routes"]}, indent=2) + "\n",
                encoding="utf-8",
            )
        (args.out / "lab.json").write_text(
            json.dumps(lab.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {len(lab.services)} services to {args.out} "
              f"(fingerprint {lab.fingerprint})")
        return 0
    if args.command == "serve":
        serve(args.service_dir, args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
