"""Misconfiguration rule engine (M2).

Reference implementation of the OnionScan-class rule set, scored against the farm's
injected flaws.

The design choice that matters: rules are evaluated over **every endpoint a service
serves**, not only the paths where flaws were injected. That is what gives the clean
control endpoints a chance to false-positive, and therefore what makes the
false-positive rate a real measurement rather than a formality. A detector that only
inspected known-bad paths would score a perfect FP rate and have demonstrated nothing.

Rules are signature-based on purpose. They are meant to be the *plausible naive*
implementation -- the kind a first pass produces -- so that the hard negatives and
clean controls have something to defeat.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from farm.model import Endpoint, Service

# --------------------------------------------------------------------------- #
# Signature helpers
# --------------------------------------------------------------------------- #

_RFC1918 = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b")
_GPS = re.compile(r"GPSLatitude=([-\d.]+)\s+GPSLongitude=([-\d.]+)")
_BANNER = re.compile(r"^(Apache|nginx)/(\d+\.\d+(?:\.\d+)?)")
_DATE_OFFSET = re.compile(r"([+-])(\d{2})(\d{2})$")


def _nonzero_gps(body: str) -> bool:
    """True only for GPS coordinates that actually carry a location.

    The hard negative is a GPS tag set to the null-island placeholder (0, 0), which
    trips a "GPS present" check while disclosing nothing. A rule that ignores the
    values reports a location that does not exist.
    """
    match = _GPS.search(body)
    if not match:
        return False
    latitude = abs(float(match.group(1)))
    longitude = abs(float(match.group(2)))
    return latitude > 0.5 or longitude > 0.5


def _nonstandard_robots_paths(body: str) -> list[str]:
    """Disallowed paths that are not generic guesses.

    A robots.txt listing only /admin/ and /search/ matches the *shape* of a leak
    while revealing nothing, because those paths are guessed defaults rather than the
    service's real structure.
    """
    standard = {"/admin/", "/search", "/search/", "/private/", "/login"}
    paths = []
    for line in body.splitlines():
        if not line.lower().startswith("disallow:"):
            continue
        path = line.split(":", 1)[1].strip()
        if path and path not in standard:
            paths.append(path)
    return paths


@dataclass(frozen=True)
class RuleHit:
    """A rule firing on one endpoint of one service."""

    service_id: str
    kind: str
    path: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RuleFn = Callable[[Endpoint, Service], "str | None"]


def _has_non_utc_offset(date_header: str) -> bool:
    """True if the Date header carries a non-UTC offset.

    The hard negative is a host whose clock is UTC while its content is localised
    elsewhere. Offset-based timezone inference fails on any host that does not set
    its clock to local time, which is most of them.
    """
    match = _DATE_OFFSET.search(date_header)
    if not match:
        return False
    return (match.group(1), match.group(2)) != ("+", "00")


# --------------------------------------------------------------------------- #
# The rule table
#
# Each rule returns a short evidence string when it fires, or None. Evidence is
# required, not optional: Part E2.5 forbids the tool asserting anything without
# showing what it saw, so a rule that cannot say what it matched cannot be part of
# the tool.
# --------------------------------------------------------------------------- #

RULES: dict[str, RuleFn] = {
    "server_status": lambda e, s: (
        "mod_status table"
        if e.status == 200 and "Apache Server Status" in e.body
        else None
    ),
    "directory_autoindex": lambda e, s: (
        "server-generated index listing"
        if e.status == 200 and "<pre>" in e.body and "Index of /" in e.body
        else None
    ),
    "phpinfo": lambda e, s: (
        "phpinfo table"
        if e.status == 200
        and "PHP Version" in e.body
        and "Loaded Configuration File" in e.body
        else None
    ),
    "dot_git": lambda e, s: (
        "git config body"
        if e.status == 200 and e.body.lstrip().startswith("[core]")
        else None
    ),
    "dot_env": lambda e, s: (
        "env-style key/value file"
        if e.status == 200
        and e.path.endswith(".env")
        and any("=" in line for line in e.body.splitlines())
        else None
    ),
    "backup_file": lambda e, s: (
        "served source backup"
        if e.status == 200 and e.path.endswith(".bak") and "<?php" in e.body
        else None
    ),
    "default_admin_panel": lambda e, s: (
        "vendor-default admin login"
        if e.status == 200 and "Admin Login" in e.body and "vendor default" in e.body
        else None
    ),
    "robots_path_leak": lambda e, s: (
        f"non-standard disallowed paths: {_nonstandard_robots_paths(e.body)}"
        if e.status == 200 and _nonstandard_robots_paths(e.body)
        else None
    ),
    "staging_page": lambda e, s: (
        "staging banner"
        if e.status == 200 and "Staging environment" in e.body
        else None
    ),
    "debug_endpoint": lambda e, s: (
        "runtime debug payload"
        if e.status == 200 and "cmdline" in e.body and "memstats" in e.body
        else None
    ),
    "readme_internal_hostname": lambda e, s: (
        f"RFC1918 address: {_RFC1918.search(e.body).group(0)}"  # type: ignore[union-attr]
        if e.status == 200 and _RFC1918.search(e.body)
        else None
    ),
    "exif_gps_in_image": lambda e, s: (
        f"GPS {_GPS.search(e.body).group(0)}"  # type: ignore[union-attr]
        if _nonzero_gps(e.body)
        else None
    ),
    "verbose_error_path_disclosure": lambda e, s: (
        "traceback with absolute path"
        if "Traceback (most recent call last)" in e.body
        and re.search(r'File "/[^"]+"', e.body)
        else None
    ),
    "apache_version_banner": lambda e, s: (
        f"Server: {e.headers['Server']}"
        if e.headers.get("Server", "").startswith("Apache/")
        and _BANNER.match(e.headers["Server"])
        else None
    ),
    "nginx_version_banner": lambda e, s: (
        f"Server: {e.headers['Server']}"
        if e.headers.get("Server", "").startswith("nginx/")
        and _BANNER.match(e.headers["Server"])
        else None
    ),
    "cookie_name_unique": lambda e, s: (
        "non-default session cookie name"
        if "Set-Cookie" in e.headers
        and not any(
            default in e.headers["Set-Cookie"]
            for default in ("PHPSESSID", "JSESSIONID", "ASP.NET_SessionId")
        )
        else None
    ),
    "timezone_leak": lambda e, s: (
        "non-UTC Date offset"
        if _has_non_utc_offset(e.headers.get("Date", ""))
        else None
    ),
    "etag_pattern": lambda e, s: (
        "inode-style ETag"
        if re.fullmatch(r'W/"[0-9a-f]+-[0-9a-f]+"', e.headers.get("ETag", ""))
        else None
    ),
    "http_header_order": lambda e, s: (
        "uncommon header set"
        if {"X-Powered-By", "Vary", "Cache-Control", "X-Request-Id"} <= set(e.headers)
        else None
    ),
}

# Rule classes the misconfig engine owns. FINGERPRINT rules belong to the correlator,
# because they are a *linking* signal rather than a per-service observation.
MISCONFIG_RULE_CLASSES = frozenset({"http_path", "banner", "metadata", "config"})


# --------------------------------------------------------------------------- #
# The naive rule table
#
# Each rule here matches on the weakest plausible signal for its kind -- the version
# somebody writes in a first pass, and the version that already exists in abandoned
# tooling. It is kept deliberately, because a rule set that scores 100% on the farm
# has demonstrated nothing until something demonstrates that the fixtures can fail.
#
# Running both tables and reporting the gap is what turns "our rules are accurate"
# into a measurement: the farm distinguishes a careful implementation from a careless
# one, and the separation is the evidence.
# --------------------------------------------------------------------------- #

NAIVE_RULES: dict[str, RuleFn] = {
    # No status check: a 404 page mentioning the path counts.
    "server_status": lambda e, s: (
        "body mentions server-status" if "server-status" in e.body else None
    ),
    # No listing-structure check: any page containing "Index" counts.
    "directory_autoindex": lambda e, s: (
        "body mentions Index" if "Index" in e.body else None
    ),
    # No interpreter-output check: the function name alone counts.
    "phpinfo": lambda e, s: ("body mentions phpinfo" if "phpinfo" in e.body else None),
    # No content check at all.
    "dot_git": lambda e, s: (
        "git path answers" if e.path.startswith("/.git") else None
    ),
    "dot_env": lambda e, s: ("env path answers" if e.path.endswith(".env") else None),
    "backup_file": lambda e, s: (
        "backup-suffixed path answers" if e.path.endswith(".bak") else None
    ),
    # No vendor-default branding check: the app's own login looks the same.
    "default_admin_panel": lambda e, s: (
        "admin login form" if "Admin Login" in e.body else None
    ),
    # No standard-path filter: generic guesses count as a leak.
    "robots_path_leak": lambda e, s: (
        "robots disallows something" if "Disallow" in e.body else None
    ),
    # Case-insensitive substring, and no banner check.
    "staging_page": lambda e, s: (
        "body mentions staging" if "staging" in e.body.lower() else None
    ),
    "debug_endpoint": lambda e, s: (
        "debug path answers" if e.path == "/debug/vars" else None
    ),
    # Any IPv4-shaped string, including the reserved documentation range.
    "readme_internal_hostname": lambda e, s: (
        "an address appears" if re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", e.body) else None
    ),
    # GPS tag present, values ignored -- so null island counts as a location.
    "exif_gps_in_image": lambda e, s: (
        "GPS tag present" if "GPSLatitude" in e.body else None
    ),
    # Traceback alone, with no path check.
    "verbose_error_path_disclosure": lambda e, s: (
        "traceback present" if "Traceback" in e.body else None
    ),
    # Product token only, version never verified.
    "apache_version_banner": lambda e, s: (
        "Apache in Server header" if "Apache" in e.headers.get("Server", "") else None
    ),
    "nginx_version_banner": lambda e, s: (
        "nginx in Server header" if "nginx" in e.headers.get("Server", "") else None
    ),
    # Any cookie at all, including framework defaults.
    "cookie_name_unique": lambda e, s: (
        "a cookie is set" if "Set-Cookie" in e.headers else None
    ),
    # Any Date header, offset never checked.
    "timezone_leak": lambda e, s: (
        "a Date header exists" if "Date" in e.headers else None
    ),
    # Any ETag, format never checked.
    "etag_pattern": lambda e, s: (
        "an ETag exists" if "ETag" in e.headers else None
    ),
    # Any X- header, set never checked.
    "http_header_order": lambda e, s: (
        "a custom header exists"
        if any(k.lower().startswith("x-") for k in e.headers)
        else None
    ),
}


# --------------------------------------------------------------------------- #
# Scanning and scoring
# --------------------------------------------------------------------------- #


def scan_service(
    farm,
    service_id: str,
    rules: dict[str, RuleFn] | None = None,
) -> list[RuleHit]:
    """Run a rule table over every endpoint of one service.

    All endpoints, not just flaw paths: the clean control endpoints and the
    rule-level hard negatives exist so the false-positive rate has a denominator, and
    skipping them would defeat that.

    `rules` defaults to the careful table. Pass `NAIVE_RULES` to score the loose
    variant, which is how the harness shows the fixtures have teeth.
    """
    table = RULES if rules is None else rules
    service = farm.service(service_id)
    hits: list[RuleHit] = []
    for endpoint in service.endpoints:
        for kind, rule in table.items():
            evidence = rule(endpoint, service)
            if evidence:
                hits.append(
                    RuleHit(
                        service_id=service_id,
                        kind=kind,
                        path=endpoint.path,
                        evidence=evidence,
                    )
                )
    return hits


@dataclass
class MisconfigScoring:
    """Rule engine results against injected single-scope flaws."""

    hits: list[RuleHit]
    truth: set[tuple[str, str]]
    detected: set[tuple[str, str]]

    @property
    def tp(self) -> int:
        return len(self.truth & self.detected)

    @property
    def fn(self) -> int:
        return len(self.truth - self.detected)

    @property
    def fp(self) -> int:
        return len(self.detected - self.truth)

    @property
    def recall(self) -> float:
        return self.tp / len(self.truth) if self.truth else 0.0

    @property
    def precision(self) -> float:
        return self.tp / len(self.detected) if self.detected else 0.0

    @property
    def missed(self) -> list[tuple[str, str]]:
        return sorted(self.truth - self.detected)

    @property
    def false_positives(self) -> list[tuple[str, str]]:
        return sorted(self.detected - self.truth)

    def false_positives_on_clean_controls(self, farm) -> list[RuleHit]:
        """Firings that landed on a clean control endpoint.

        The sharpest form of the FP measurement: these endpoints were placed on
        flawed services precisely so this number would exist.
        """
        clean = {(sid, ep.path) for sid, ep in farm.clean_control_endpoints()}
        return [hit for hit in self.hits if (hit.service_id, hit.path) in clean]

    def to_dict(self, farm) -> dict[str, Any]:
        from eval.metrics import proportion

        precision = proportion(self.tp, len(self.detected)) if self.detected else None
        recall = proportion(self.tp, len(self.truth)) if self.truth else None
        return {
            "injected_flaws": len(self.truth),
            "detections": len(self.detected),
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "recall": recall.to_dict() if recall else None,
            "precision": precision.to_dict() if precision else None,
            "missed": [f"{sid}:{kind}" for sid, kind in self.missed],
            "false_positives": [f"{sid}:{kind}" for sid, kind in self.false_positives],
            "fp_on_clean_control_endpoints": [
                hit.to_dict() for hit in self.false_positives_on_clean_controls(farm)
            ],
        }


def evaluate(
    farm,
    rules: dict[str, RuleFn] | None = None,
    *,
    service_ids: Iterable[str] | None = None,
) -> MisconfigScoring:
    """Score a rule table against the farm's injected single-scope flaws.

    FINGERPRINT-scope flaws are excluded from the truth set: they are a linking
    signal, scored by the correlator, and counting them here would inflate this
    module's recall with work it does not do.

    `service_ids` restricts both the scanning and the truth set, so a held-out run scores
    only the services it claims to score. The rule table itself is static: no threshold or
    pattern is chosen from these numbers.
    """
    from farm.model import FlawScope

    table = RULES if rules is None else rules
    allowed = None if service_ids is None else set(service_ids)

    hits: list[RuleHit] = []
    for service in farm.services:
        if allowed is not None and service.service_id not in allowed:
            continue
        hits.extend(scan_service(farm, service.service_id, table))

    truth: set[tuple[str, str]] = set()
    for flaw in farm.flaws_by_scope(FlawScope.SINGLE):
        for service_id in flaw.service_ids:
            if allowed is None or service_id in allowed:
                truth.add((service_id, flaw.kind))

    detected = {(hit.service_id, hit.kind) for hit in hits}
    return MisconfigScoring(hits=hits, truth=truth, detected=detected)
