"""Service content generation and opsec-flaw injection.

Each service is a small synthetic site with a handful of endpoints. Flaws are then
injected into it from the catalogue. The critical mechanism in this module is the
pairing of every fingerprint rule with a **hard negative that creates false
similarity across unrelated clusters**:

  * a CLUSTER flaw puts the *same* artefact on services inside one cluster
    (a genuine link -- the correlator should find it);
  * the matching HARD NEGATIVE puts the *same default* artefact on services in
    *different* clusters (a spurious link -- a naive rule will "find" it too, and
    that is how its precision gets measured).

Without the second half, a correlator's precision can only be measured against
services it already got right, which inflates it (Part D4).

Fixture builders are registered in FIXTURE_BUILDERS, keyed by flaw kind, so adding a
rule to the catalogue means adding one small function here.

MODELLING DECISION: what `shared_artifacts` represents
-----------------------------------------------------
`Service.shared_artifacts` holds the **extracted observation** for each fingerprint
kind -- the value a real collector would have computed by fetching the favicon, the
TLS certificate, the static asset, and hashing it. It is not literal served bytes.

That abstraction is deliberate and it is the right one for this evaluation. Artefact
*extraction* is a separate module (M2's fingerprint correlator); the farm's job is to
supply the observations and the ground-truth labels, so the correlator's *linking*
decision can be scored independently of whether extraction was implemented correctly.
The `.env` and 404 fixtures are different: those are served as real documents, because
their content is what the rule reads.
"""

from __future__ import annotations

import random
from typing import Callable

from farm.catalogue import CLUSTER_FLAWS, SINGLE_FLAWS, FlawSpec
from farm.language import TOPIC_VOCABULARY
from farm.model import (
    Endpoint,
    Flaw,
    Service,
    deterministic_hex,
    deterministic_onion_address,
)

SERVICE_TITLES: tuple[str, ...] = (
    "Trading Post", "Swap Shop", "Second Look", "Old Stock", "Corner Store",
    "Parts Bin", "Shelf Life", "Hand-Me-Down", "The Backroom", "Odd Lots",
    "Spare Room", "Clear Out",
)

# The stock-default value used by hard negatives. Deliberately identical across
# *unrelated* clusters -- that is the false similarity the correlator must not
# be fooled by.
STOCK_DEFAULT_ARTIFACT = "STOCK_DEFAULT"


def build_base_site(rng: random.Random, service: Service) -> list[Endpoint]:
    """The innocuous part of a service: index, about, search, contact.

    Three of these are marked `is_clean_control`, which is what gives the detector's
    false-positive rate a denominator.
    """
    vocab = TOPIC_VOCABULARY.get(service.topic, ("item",))
    picks = rng.sample(vocab, min(5, len(vocab)))

    return [
        Endpoint(
            path="/",
            body=(
                f"<h1>{service.title}</h1>"
                f"<p>Listings for {', '.join(picks)}. Updated weekly.</p>"
                "<p><a href='/about'>About</a> <a href='/search'>Search</a> "
                "<a href='/contact'>Contact</a></p>"
            ),
            headers={"Content-Type": "text/html"},
        ),
        Endpoint(
            path="/about",
            body="<h1>About</h1><p>A small hobby marketplace. No accounts needed.</p>",
            is_clean_control=True,
        ),
        Endpoint(
            path="/search",
            body="<form action='/search'><input name='q'><button>Go</button></form>",
            is_clean_control=True,
        ),
        Endpoint(
            path="/contact",
            body="<h1>Contact</h1><p>Reply to any listing to get in touch.</p>",
            is_clean_control=True,
        ),
    ]


def make_service(
    rng: random.Random,
    seed: int,
    service_id: str,
    cluster_id: str | None,
    topic: str,
) -> Service:
    """Create a service with its base content and a stable synthetic address."""
    service = Service(
        service_id=service_id,
        cluster_id=cluster_id,
        address=deterministic_onion_address(seed, service_id),
        title=rng.choice(SERVICE_TITLES),
        topic=topic,
    )
    service.endpoints = build_base_site(rng, service)
    return service


# --------------------------------------------------------------------------- #
# Fixture builders
#
# Each builder receives the flaw spec, the owning service, and the seed, and returns
# the single endpoint that carries the flaw. Fixtures expose *only* what the rule
# needs, so a detector cannot pass by matching incidental content.
# --------------------------------------------------------------------------- #

FixtureBuilder = Callable[[FlawSpec, Service, int], Endpoint]


def _fx_server_status(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "<html><head><title>Apache Server Status</title></head><body>"
            "<h1>Apache Server Status</h1>"
            "<table><tr><th>Srv</th><th>PID</th><th>Client</th><th>Request</th></tr>"
            "<tr><td>0-0</td><td>1501</td><td>127.0.0.1</td><td>GET / HTTP/1.1</td></tr>"
            "</table></body></html>"
        ),
    )


def _fx_directory_autoindex(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "<html><head><title>Index of /uploads</title></head><body>"
            "<h1>Index of /uploads</h1><pre>"
            '<a href="../">Parent Directory</a>\n'
            '<a href="pic01.jpg">pic01.jpg</a>\n'
            '<a href="notes.txt">notes.txt</a>\n'
            "</pre></body></html>"
        ),
    )


def _fx_phpinfo(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "<html><body><table>"
            "<tr><td>PHP Version</td><td>7.4.33</td></tr>"
            "<tr><td>Loaded Configuration File</td><td>/etc/php/php.ini</td></tr>"
            "<tr><td>Server API</td><td>Apache 2.0 Handler</td></tr>"
            "</table></body></html>"
        ),
    )


def _fx_dot_git(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
            '[remote "origin"]\n\turl = https://git.internal.local/app.git\n'
        ),
    )


def _fx_dot_env(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    # Decoy only. No real credentials, not even synthetic-looking ones.
    return Endpoint(
        path=spec.path,
        body="APP_NAME=example\nAPP_ENV=local\nDB_PASSWORD=\nAPI_KEY=\n",
    )


def _fx_backup_file(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "<?php\n// application source backup\nrequire 'config.php';\n"
            "function render_home() { /* ... */ }\n"
        ),
    )


def _fx_default_admin_panel(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "<html><head><title>Admin Login</title></head><body>"
            "<h1>Admin Login</h1><form><input name='user'>"
            "<input name='pass' type='password'><button>Sign in</button></form>"
            "<p>Powered by the vendor default installer.</p></body></html>"
        ),
    )


def _fx_robots_path_leak(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "User-agent: *\n"
            "Disallow: /internal-inventory/\n"
            "Disallow: /staging-db-dump/\n"
            f"Disallow: /{service.service_id}-private/\n"
        ),
        headers={"Content-Type": "text/plain"},
    )


def _fx_staging_page(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "<h1>Staging environment</h1>"
            "<p>Pre-production build. Not for public use.</p>"
        ),
    )


def _fx_debug_endpoint(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            '{"cmdline":["/srv/app/server","--env=prod"],'
            '"memstats":{"Alloc":1209344,"Sys":7340032},"uptime":"14h2m"}'
        ),
        headers={"Content-Type": "application/json"},
    )


def _fx_readme_internal_hostname(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "Deployment notes\n----------------\n"
            "Service host: db-replica-03.lan\n"
            "Address: 10.14.22.7\n"
            "Restart with: systemctl restart app\n"
        ),
        headers={"Content-Type": "text/plain"},
    )


def _fx_exif_gps(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    # Metadata-only fixture: the GPS block is the signal, not the pixels. On live
    # collection, media is referenced by URL and never fetched (Part E2.1).
    gps = deterministic_hex(seed, f"gps:{service.service_id}", length=12)
    return Endpoint(
        path=spec.path,
        body=(
            f"[JPEG fixture] EXIF GPSLatitude=28.6139 GPSLongitude=77.2090 "
            f"GPSDateStamp=2026:01:04 GPSVersionID=2.3.0 nonce={gps}"
        ),
        headers={"Content-Type": "image/jpeg"},
    )


def _fx_verbose_error(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    return Endpoint(
        path=spec.path,
        body=(
            "<h1>500 Internal Server Error</h1><pre>"
            "Traceback (most recent call last):\n"
            '  File "/srv/app/handlers/router.py", line 88, in dispatch\n'
            "    handler = self.routes[path]\n"
            "KeyError: '/api/v1/missing/'\n"
            "</pre>"
        ),
        status=500,
    )


def _fx_default_404(spec: FlawSpec, service: Service, seed: int) -> Endpoint:
    """A custom 404 page whose bytes are shared inside one cluster only.

    The page embeds a marker derived from the cluster id, so the served bytes are
    genuinely identical across a cluster and genuinely different between clusters.
    An earlier version served one stock page to every service, which meant every
    service in the farm shared it -- a signal that would link the entire farm and
    therefore measured nothing.
    """
    if service.cluster_id:
        marker = deterministic_hex(
            seed, f"cluster-artifact:{spec.kind}:{service.cluster_id}", length=16
        )
        body = (
            "<html><head><title>404 Not Found</title></head>"
            f"<body><h1>Page not found</h1><p>ref: {marker}</p>"
            "<p><a href='/'>Back to listings</a></p></body></html>"
        )
    else:
        # An un-clustered service serves a vendor-default page, which is the hard
        # negative: it is shared with other stock instances, not with an operator.
        body = (
            "<html><head><title>404 Not Found</title></head>"
            "<body><center><h1>404 Not Found</h1></center>"
            "<hr><center>nginx</center></body></html>"
        )
    return Endpoint(path=spec.path, body=body, status=404)


# --------------------------------------------------------------------------- #
# Rule-level hard negatives
#
# One fixture per rule, each built so it trips that rule's *pattern* while carrying
# none of its *meaning*. These are what turn the misconfiguration false-positive rate
# from a formality into a measurement: without them the rule engine scores 19/19 and
# has demonstrated nothing, because there is nothing present for it to get wrong.
#
# Not every catalogue rationale had a constructible fixture. The last seven were left
# unimplemented for a while, and the reason was wrong: they are the rules whose flaw lives in
# a header or in a single line of a file, and it looked as though a proxy would make them
# indistinguishable at the response level. It does not. Every one of them has a distinguishing
# feature the careful rule reads and the careless rule ignores, which is exactly what a hard
# negative is for: the page looks like the flaw and the discriminator says it is not.
# `rules_with_hard_negative_fixture()` now covers all nineteen, and a test pins that.
# --------------------------------------------------------------------------- #

HARD_NEGATIVE_FIXTURES: dict[str, tuple[str, str, dict[str, str]]] = {
    # A 404 page mentioning "server-status" in a nav link. A naive substring rule
    # fires; the status code and the absent mod_status table say otherwise.
    "server_status": (
        "/server-status",
        "<html><body><h1>Not Found</h1><p>Try "
        "<a href='/server-status'>server-status</a> from the admin network.</p>"
        "</body></html>",
        {},
    ),
    # The env path answers, at 200, and carries no assignment. The naive rule keys on the
    # path and fires; the careful rule reads the body and finds nothing to disclose.
    "dot_env": (
        "/.env",
        "<html><body><h1>Not Found</h1><p>No such file.</p></body></html>",
        {},
    ),
    # A backup-suffixed path that serves a page rather than source. ".bak" is enough for the
    # naive rule; the absence of an opening PHP tag is what the careful rule checks.
    "backup_file": (
        "/index.php.bak",
        "<html><body><h1>Backup unavailable</h1></body></html>",
        {},
    ),
    # Apache named without a version. "Apache" in the Server header trips the naive rule;
    # a version token is what makes a banner a disclosure, and there is none.
    "apache_version_banner": (
        "/about",
        "<html><body><h1>About</h1></body></html>",
        {"Server": "Apache"},
    ),
    # The same for nginx, at a different path so the two fixtures never collide on one
    # service. A bare product name is not a version disclosure.
    "nginx_version_banner": (
        "/colophon",
        "<html><body><h1>Colophon</h1></body></html>",
        {"Server": "nginx"},
    ),
    # A Date header that exists and is UTC. The naive rule asks whether a Date header is
    # present, which is every response ever served; the careful rule asks whether the clock
    # leaks a local offset, and GMT means it does not.
    "timezone_leak": (
        "/contact",
        "<html><body><h1>Contact</h1></body></html>",
        {"Date": "Fri, 23 Oct 2026 13:00:00 GMT"},
    ),
    # An ETag in a shape that leaks nothing: a release tag, not the inode-and-size pair that
    # names the file's stat on disk. Presence alone is the naive test.
    "etag_pattern": (
        "/style.css",
        "body{color:#111}",
        {"ETag": '"v5-1a2b"'},
    ),
    # Three of the four headers a default panel stack emits, with X-Request-Id missing. The
    # naive rule fires on any X- header; the careful rule requires the whole set, because a
    # partial set is a deliberately configured server rather than a default one.
    "http_header_order": (
        "/faq",
        "<html><body><h1>FAQ</h1></body></html>",
        {
            "X-Powered-By": "PHP/8.1.2",
            "Vary": "Accept-Encoding",
            "Cache-Control": "no-store",
        },
    ),
    # An application-rendered gallery at /uploads/. Contains "Index" and anchors, so a
    # loose autoindex heuristic fires; there is no server-generated listing.
    "directory_autoindex": (
        "/uploads/",
        "<html><body><h1>Uploads Index</h1>"
        '<a href="/uploads/1">1</a> <a href="/uploads/2">2</a>'
        "</body></html>",
        {},
    ),
    # Static documentation describing phpinfo. A loose rule fires on the function name
    # and the version string; there is no interpreter output.
    "phpinfo": (
        "/docs/phpinfo.html",
        "<html><body><h1>Debugging with phpinfo()</h1>"
        "<p>Call <code>phpinfo()</code> to inspect your PHP 8.1 configuration.</p>"
        "</body></html>",
        {},
    ),
    # A .git/config that exists but refuses to disclose anything.
    "dot_git": (
        "/.git/config",
        "<html><body><h1>403 Forbidden</h1><p>Access denied.</p></body></html>",
        {},
    ),
    # The application's own authenticated login. Same path and shape as a vendor
    # default; distinguished only by the absence of vendor-default branding.
    "default_admin_panel": (
        "/admin/",
        "<html><head><title>Admin Login</title></head><body>"
        "<h1>Admin Login</h1><form><input name='u'>"
        "<input name='p' type='password'></form></body></html>",
        {},
    ),
    # robots.txt listing only generic guessed paths. Matches the shape of a leak,
    # names nothing real.
    "robots_path_leak": (
        "/robots.txt",
        "User-agent: *\nDisallow: /admin/\nDisallow: /search\n",
        {"Content-Type": "text/plain"},
    ),
    # The app's normal index, mentioning staging only in a navigation label.
    "staging_page": (
        "/staging/",
        "<html><body><h1>Listings</h1>"
        "<p><a href='/about'>About</a> <a href='/notes'>Staging notes</a></p>"
        "</body></html>",
        {},
    ),
    # Sample payload published for API documentation, not live runtime state.
    "debug_endpoint": (
        "/debug/vars",
        '{"note":"example response only","memstats":{"Alloc":0},"docs":"/api"}',
        {"Content-Type": "application/json"},
    ),
    # Documentation using the IETF example ranges, which are reserved precisely so
    # that they cannot identify anyone.
    "readme_internal_hostname": (
        "/README.txt",
        "Deployment notes\n----------------\n"
        "Example hostname: db.example.com\nExample address: 192.0.2.7\n",
        {"Content-Type": "text/plain"},
    ),
    # A GPS tag present but set to null island: the inference is a location that does
    # not exist.
    "exif_gps_in_image": (
        "/assets/img/stock-photo.jpg",
        "[JPEG fixture] EXIF GPSLatitude=0.0 GPSLongitude=0.0 GPSVersionID=2.3.0",
        {"Content-Type": "image/jpeg"},
    ),
    # A stack trace with the paths already redacted.
    "verbose_error_path_disclosure": (
        "/api/v1/redacted/",
        "<h1>500 Internal Server Error</h1><pre>"
        "Traceback (most recent call last):\n"
        '  File "<redacted>", line 12, in dispatch\n'
        "</pre>",
        {},
    ),
    # A framework-default cookie name, shared by millions of unrelated deployments.
    "cookie_name_unique": (
        "/",
        "<html><body><h1>Listings</h1></body></html>",
        {"Set-Cookie": "PHPSESSID=abc123; Path=/; HttpOnly"},
    ),
}


def rules_with_hard_negative_fixture() -> list[str]:
    """Rules that have an implemented negative fixture.

    The complement of this list is a stated limitation: those rules' false-positive
    rates are measured only against true positives and are therefore optimistic.
    Reporting the gap is the point.
    """
    return sorted(HARD_NEGATIVE_FIXTURES)


def inject_rule_hard_negative(service: Service, kind: str) -> bool:
    """Attach a rule's negative fixture to a service. Returns False if none exists.

    The endpoint is deliberately **not** marked `is_clean_control`. A negative fixture
    sits at a flaw path on a service that does not carry that flaw, so it is a distinct
    kind of control: it tests one specific rule rather than asserting general
    cleanliness. A rule that fires here produces a false positive through the normal
    scoring path, which is exactly the intended measurement.
    """
    fixture = HARD_NEGATIVE_FIXTURES.get(kind)
    if fixture is None:
        return False
    path, body, headers = fixture
    existing = service.endpoint(path)
    if existing is not None:
        existing.body = body
        existing.status = 200
        existing.headers.update(headers)
    else:
        service.endpoints.append(
            Endpoint(path=path, body=body, headers=dict(headers))
        )
    return True


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

FIXTURE_BUILDERS: dict[str, FixtureBuilder] = {
    "server_status": _fx_server_status,
    "directory_autoindex": _fx_directory_autoindex,
    "phpinfo": _fx_phpinfo,
    "dot_git": _fx_dot_git,
    "dot_env": _fx_dot_env,
    "backup_file": _fx_backup_file,
    "default_admin_panel": _fx_default_admin_panel,
    "robots_path_leak": _fx_robots_path_leak,
    "staging_page": _fx_staging_page,
    "debug_endpoint": _fx_debug_endpoint,
    "readme_internal_hostname": _fx_readme_internal_hostname,
    "exif_gps_in_image": _fx_exif_gps,
    "verbose_error_path_disclosure": _fx_verbose_error,
    "identical_404_page_hash": _fx_default_404,
}

# Flaws expressed as a response header rather than a page body. Insertion order is
# meaningful for `http_header_order`, and Python dicts preserve it, so the ordering
# fixture below is a real ordered mapping rather than a set.
HEADER_FLAWS: dict[str, dict[str, str]] = {
    "apache_version_banner": {"Server": "Apache/2.4.52 (Ubuntu)"},
    "nginx_version_banner": {"Server": "nginx/1.24.0"},
    "cookie_name_unique": {"Set-Cookie": "sess_9f2c1ab4=1; Path=/; HttpOnly"},
    "timezone_leak": {"Date": "Mon, 12 Jan 2026 14:03:11 +0530"},
    "etag_pattern": {"ETag": 'W/"5f8-1a2b3c4d"'},
    "http_header_order": {
        "X-Powered-By": "app-server",
        "Vary": "Accept-Encoding",
        "Cache-Control": "no-store",
        "X-Request-Id": "req-0001",
    },
}

# Which artefact key each cluster-scope fingerprint flaw populates.
CLUSTER_FLAW_ARTIFACT_KEY: dict[str, str] = {
    "shared_favicon": "favicon_sha256",
    "shared_tls_cert_fingerprint": "tls_spki_sha256",
    "shared_ssh_hostkey": "ssh_hostkey_sha256",
    "identical_static_asset_hash": "app_js_sha256",
    "shared_analytics_id": "analytics_id",
    "identical_404_page_hash": "error404_sha256",
}


class UnknownFixtureError(KeyError):
    """Raised when the catalogue has a flaw kind with no registered fixture builder.

    This is deliberately loud: a flaw that cannot be injected would silently reduce
    the denominator of the recall metric and make the farm's own ground truth wrong.
    """


def inject_single_flaw(
    service: Service,
    spec: FlawSpec,
    seed: int,
) -> None:
    """Inject a SINGLE-scope flaw into one service."""
    builder = FIXTURE_BUILDERS.get(spec.kind)
    if builder is not None:
        endpoint = builder(spec, service, seed)
        existing = service.endpoint(endpoint.path)
        if existing is not None:
            existing.body = endpoint.body
            existing.status = endpoint.status
            existing.headers.update(endpoint.headers)
        else:
            service.endpoints.append(endpoint)
        return

    headers = HEADER_FLAWS.get(spec.kind)
    if headers is not None:
        index = service.endpoint("/")
        if index is None:  # pragma: no cover - base site always has "/"
            index = Endpoint(path="/", body="")
            service.endpoints.append(index)
        index.headers.update(headers)
        return

    raise UnknownFixtureError(
        f"no fixture builder registered for flaw {spec.kind!r}"
    )


# --------------------------------------------------------------------------- #
# Cluster artefacts: genuine links vs the false links that measure precision
# --------------------------------------------------------------------------- #


def assign_cluster_artifact(service: Service, spec: FlawSpec, seed: int) -> str:
    """Give a service the cluster's shared artefact -- a GENUINE link.

    The value is derived from the cluster id, so every service in the cluster shares
    exactly this value and no service outside it does. This is the positive class the
    correlator is scored against.
    """
    if service.cluster_id is None:  # pragma: no cover - guarded by the generator
        raise ValueError("cluster artefacts cannot be assigned to a singleton service")
    key = CLUSTER_FLAW_ARTIFACT_KEY[spec.kind]
    value = deterministic_hex(
        seed, f"cluster-artifact:{spec.kind}:{service.cluster_id}", length=32
    )
    service.shared_artifacts[key] = value
    return value


def assign_stock_default(service: Service, spec: FlawSpec) -> str:
    """Give a service a *vendor-default* artefact -- a SPURIOUS link.

    This is the hard-negative mechanism. The artefact value is identical across
    services in *different* clusters, so any rule that links services on "these two
    share this value" will produce a false merge here. Those false merges are exactly
    what the precision metric in Part E2 is measuring.
    """
    key = CLUSTER_FLAW_ARTIFACT_KEY[spec.kind]
    service.shared_artifacts[key] = STOCK_DEFAULT_ARTIFACT
    return STOCK_DEFAULT_ARTIFACT


def unbuildable_flaw_kinds() -> list[str]:
    """Catalogue kinds with no fixture path.

    Must be empty. A kind with no builder would silently shrink the recall metric's
    denominator and make the farm's own ground truth wrong, so the generator asserts
    on this rather than warning.
    """
    missing: list[str] = []
    for spec in SINGLE_FLAWS:
        if spec.kind not in FIXTURE_BUILDERS and spec.kind not in HEADER_FLAWS:
            missing.append(spec.kind)
    for spec in CLUSTER_FLAWS:
        if spec.kind not in CLUSTER_FLAW_ARTIFACT_KEY:
            missing.append(spec.kind)
    return missing


def make_flaw_record(
    flaw_id: str,
    spec: FlawSpec,
    service_ids: list[str],
) -> Flaw:
    """Build the ground-truth record for an injected flaw."""
    return Flaw(
        flaw_id=flaw_id,
        kind=spec.kind,
        scope=spec.scope,
        rule_class=spec.rule_class,
        service_ids=list(service_ids),
        path=spec.path,
        signal=spec.signal,
    )