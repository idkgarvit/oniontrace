"""The D2 opsec-flaw catalogue.

Each entry is a rule the detector may implement, paired with:
  * the path where the flaw is injected, and
  * the `hard_negative_why` -- the reason a fixture can trip this rule's *pattern*
    without tripping its *meaning* (Part D4).

A rule with no plausible hard negative is one whose precision can only be measured
against its own true positives, which inflates it. Where `hard_negative_why` is None
that is stated explicitly rather than left blank.

Nothing here is ever tested against a service we do not own. The catalogue describes
fixtures the farm injects into its own containers (Part E2.7).
"""

from __future__ import annotations

from dataclasses import dataclass

from farm.model import FlawScope, RuleClass


@dataclass(frozen=True)
class FlawSpec:
    """A single injectable flaw definition."""

    kind: str
    rule_class: RuleClass
    scope: FlawScope
    path: str
    signal: str
    hard_negative_why: str | None
    injection_note: str = ""


# --------------------------------------------------------------------------- #
# SINGLE-scope path exposure (HTTP_PATH)
# --------------------------------------------------------------------------- #

_HTTP_PATH_FLAWS: tuple[FlawSpec, ...] = (
    FlawSpec(
        kind="server_status",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/server-status",
        signal="Apache mod_status page reachable; exposes request table and client IPs",
        hard_negative_why=(
            "A /server-status path that returns the application's own 404 page. The "
            "string 'server-status' appears in the body (nav link, sitemap entry) so a "
            "naive grep fires, but no mod_status output is served."
        ),
    ),
    FlawSpec(
        kind="directory_autoindex",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/uploads/",
        signal="Directory listing enabled; 'Index of /uploads' with file table",
        hard_negative_why=(
            "An /uploads/ page that is a real paginated gallery. It contains <a href> "
            "entries and the word 'Index', so autoindex heuristics fire, but the markup "
            "is application-rendered rather than a server listing."
        ),
    ),
    FlawSpec(
        kind="phpinfo",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/phpinfo.php",
        signal="Live phpinfo() output; PHP version, loaded modules, env vars",
        hard_negative_why=(
            "A documentation page *describing* phpinfo with a code sample. Contains "
            "'phpinfo()' and a PHP version string, but is static HTML, not interpreter "
            "output."
        ),
    ),
    FlawSpec(
        kind="dot_git",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/.git/config",
        signal="Exposed git metadata; repository URL and remote refs readable",
        hard_negative_why=(
            "A /.git/config that returns 403 with an app error page. The path exists "
            "and answers non-404, which trips existence-check probes, but no content "
            "is disclosed."
        ),
    ),
    FlawSpec(
        kind="dot_env",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/.env",
        signal="Environment file with credentials, DB DSN, API keys",
        hard_negative_why=(
            "A decoy .env served deliberately, containing only placeholder values "
            "('APP_NAME=example', empty secrets). Trips the pattern exactly; discloses "
            "nothing. Distinguishes 'file is readable' from 'secrets leaked'."
        ),
        injection_note=(
            "Decoy content only. Never contains real credentials, even synthetic ones."
        ),
    ),
    FlawSpec(
        kind="backup_file",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/index.php.bak",
        signal="Source backup served as static text; full application source readable",
        hard_negative_why=(
            "A .bak path that 404s, next to a *legitimate* downloadable template named "
            "*.bak in an unrelated directory. A filename-pattern rule fires on the "
            "legitimate file and is wrong."
        ),
    ),
    FlawSpec(
        kind="default_admin_panel",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/admin/",
        signal="Vendor-default admin login reachable with unchanged default branding",
        hard_negative_why=(
            "The application's own authenticated admin login at /admin/. Same path and "
            "same visual shape as a vendor default; distinguished only by the absence "
            "of vendor-default branding and unchanged-asset fingerprints."
        ),
    ),
    FlawSpec(
        kind="robots_path_leak",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/robots.txt",
        signal="robots.txt disallows non-standard internal paths, naming them outright",
        hard_negative_why=(
            "A robots.txt containing only standard entries (/admin/, /search). Path "
            "count and shape match a leak; the paths are guessed defaults, not the "
            "service's real internal structure."
        ),
    ),
    FlawSpec(
        kind="staging_page",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/staging/",
        signal="Live staging environment reachable; pre-production config and data",
        hard_negative_why=(
            "A /staging/ path announcing 'staging environment' that is in fact a static "
            "placeholder page with no application behind it."
        ),
    ),
    FlawSpec(
        kind="debug_endpoint",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/debug/vars",
        signal="Debug endpoint exposing runtime internals (goruntime/expvar style)",
        hard_negative_why=(
            "A /debug/vars returning a fixed sample JSON payload for the API docs. "
            "Correct content type and shape; no live runtime state."
        ),
    ),
    FlawSpec(
        kind="readme_internal_hostname",
        rule_class=RuleClass.HTTP_PATH,
        scope=FlawScope.SINGLE,
        path="/README.txt",
        signal="README naming an internal hostname or RFC1918 address",
        hard_negative_why=(
            "A README containing an example hostname from the IETF documentation range "
            "(example.com / 192.0.2.0). Matches the hostname regex; is documentation, "
            "not infrastructure disclosure."
        ),
    ),
)

# --------------------------------------------------------------------------- #
# SINGLE-scope version disclosure (BANNER) and embedded metadata (METADATA)
# --------------------------------------------------------------------------- #

_BANNER_FLAWS: tuple[FlawSpec, ...] = (
    FlawSpec(
        kind="apache_version_banner",
        rule_class=RuleClass.BANNER,
        scope=FlawScope.SINGLE,
        path="/",
        signal="Server: Apache/2.4.x with full version reachable in response headers",
        hard_negative_why=(
            "A 'Server: Apache' header with the version deliberately truncated. The "
            "product token matches a version-disclosure rule; the version itself is not "
            "disclosed."
        ),
    ),
    FlawSpec(
        kind="nginx_version_banner",
        rule_class=RuleClass.BANNER,
        scope=FlawScope.SINGLE,
        path="/",
        signal="Server: nginx/1.2x.x with full version reachable in response headers",
        hard_negative_why=(
            "A reversed-proxy 'Server: nginx' on a service whose actual origin is a "
            "different product. The banner is truthful about the edge, misleading about "
            "the origin the analyst cares about."
        ),
    ),
)

_METADATA_FLAWS: tuple[FlawSpec, ...] = (
    FlawSpec(
        kind="exif_gps_in_image",
        rule_class=RuleClass.METADATA,
        scope=FlawScope.SINGLE,
        path="/assets/img/product-1.jpg",
        signal="Hosted image carries EXIF GPS coordinates from the camera",
        hard_negative_why=(
            "An image with EXIF GPS present but set to the null-island placeholder "
            "(0.0, 0.0). Trips a 'GPS tag present' rule while carrying no location."
        ),
        injection_note=(
            "Image is generated by us and re-hosted only inside the farm. On live "
            "collection, media is referenced by URL and never fetched (Part E2.1)."
        ),
    ),
)

# --------------------------------------------------------------------------- #
# SINGLE-scope configuration and header fingerprints (CONFIG)
# --------------------------------------------------------------------------- #

_CONFIG_FLAWS: tuple[FlawSpec, ...] = (
    FlawSpec(
        kind="verbose_error_path_disclosure",
        rule_class=RuleClass.CONFIG,
        scope=FlawScope.SINGLE,
        path="/api/v1/missing/",
        signal="Debug traceback in the error body naming an absolute filesystem path",
        hard_negative_why=(
            "A 500 page showing a stack trace with paths replaced by placeholders "
            "(/srv/app/<redacted>). Shape matches a verbose error; nothing is disclosed."
        ),
    ),
    FlawSpec(
        kind="cookie_name_unique",
        rule_class=RuleClass.CONFIG,
        scope=FlawScope.SINGLE,
        path="/",
        signal="Unusual non-default session cookie name, stable across visits",
        hard_negative_why=(
            "A default framework cookie name (PHPSESSID, JSESSIONID) shared by millions "
            "of unrelated deployments. A 'rare cookie name' rule that fires here links "
            "unrelated services."
        ),
    ),
    FlawSpec(
        kind="timezone_leak",
        rule_class=RuleClass.CONFIG,
        scope=FlawScope.SINGLE,
        path="/",
        signal="Server Date header offset implies a non-UTC operator timezone",
        hard_negative_why=(
            "A service whose Date header is UTC while its content is localised to a "
            "different region. Offset-based timezone inference fails on any host that "
            "does not set its clock to local time, which is most of them."
        ),
    ),
    FlawSpec(
        kind="etag_pattern",
        rule_class=RuleClass.CONFIG,
        scope=FlawScope.SINGLE,
        path="/assets/",
        signal="ETag format matches a specific server's inode-based generator",
        hard_negative_why=(
            "An nginx-style inode ETag on a service that is not nginx. Independent "
            "instances of the same server software produce coincidentally similar ETag "
            "shapes with no shared operator."
        ),
    ),
    FlawSpec(
        kind="http_header_order",
        rule_class=RuleClass.CONFIG,
        scope=FlawScope.SINGLE,
        path="/",
        signal="Uncommon response-header ordering, stable across services",
        hard_negative_why=(
            "Two services behind the same hosting provider share header ordering because "
            "the proxy sets it, not because one operator configured both."
        ),
    ),
)

# --------------------------------------------------------------------------- #
# CLUSTER-scope shared fingerprints -- the correlator's ground truth
# --------------------------------------------------------------------------- #

_FINGERPRINT_FLAWS: tuple[FlawSpec, ...] = (
    FlawSpec(
        kind="shared_favicon",
        rule_class=RuleClass.FINGERPRINT,
        scope=FlawScope.CLUSTER,
        path="/favicon.ico",
        signal="Byte-identical favicon across services in one cluster",
        hard_negative_why=(
            "A stock CMS default favicon, byte-identical across *thousands* of unrelated "
            "deployments. The hash matches exactly and the inference is worthless. This "
            "is the single most important hard negative in the catalogue."
        ),
    ),
    FlawSpec(
        kind="shared_tls_cert_fingerprint",
        rule_class=RuleClass.FINGERPRINT,
        scope=FlawScope.CLUSTER,
        path="/",
        signal="Self-signed certificate with the same SPKI fingerprint across services",
        hard_negative_why=(
            "A certificate issued from a shared hosting or CDN provider, where the "
            "issuer and key are shared by unrelated tenants."
        ),
    ),
    FlawSpec(
        kind="shared_ssh_hostkey",
        rule_class=RuleClass.FINGERPRINT,
        scope=FlawScope.CLUSTER,
        path="/",
        signal="Same SSH host key fingerprint observed across services",
        hard_negative_why=(
            "A cloud image's baked-in default host key, shared by every instance "
            "provisioned from that image until regenerated."
        ),
    ),
    FlawSpec(
        kind="identical_static_asset_hash",
        rule_class=RuleClass.FINGERPRINT,
        scope=FlawScope.CLUSTER,
        path="/assets/js/app.js",
        signal="Custom application asset with an identical content hash across services",
        hard_negative_why=(
            "A widely-mirrored third-party library (jQuery, Bootstrap) at a common path. "
            "Identical hash, zero operator signal."
        ),
    ),
    FlawSpec(
        kind="shared_analytics_id",
        rule_class=RuleClass.FINGERPRINT,
        scope=FlawScope.CLUSTER,
        path="/",
        signal="Same analytics/tracker identifier embedded across services",
        hard_negative_why=(
            "A placeholder or documentation-sample tracking ID ('UA-XXXXX-Y') that "
            "appears in the site template's default configuration."
        ),
    ),
    FlawSpec(
        kind="identical_404_page_hash",
        rule_class=RuleClass.FINGERPRINT,
        scope=FlawScope.CLUSTER,
        path="/definitely-not-here",
        signal="Byte-identical custom 404 page across services in one cluster",
        hard_negative_why=(
            "A default server 404 page (stock nginx/Apache error document) shared by "
            "every instance of that server software before any customisation."
        ),
    ),
)

# --------------------------------------------------------------------------- #
# Assembled catalogue
# --------------------------------------------------------------------------- #

ALL_FLAWS: tuple[FlawSpec, ...] = (
    _HTTP_PATH_FLAWS + _BANNER_FLAWS + _METADATA_FLAWS + _CONFIG_FLAWS + _FINGERPRINT_FLAWS
)

SINGLE_FLAWS: tuple[FlawSpec, ...] = tuple(
    f for f in ALL_FLAWS if f.scope is FlawScope.SINGLE
)
CLUSTER_FLAWS: tuple[FlawSpec, ...] = tuple(
    f for f in ALL_FLAWS if f.scope is FlawScope.CLUSTER
)

FLAWS_BY_KIND: dict[str, FlawSpec] = {f.kind: f for f in ALL_FLAWS}


def get_spec(kind: str) -> FlawSpec:
    """Look up a flaw spec by kind, with a clear error if the kind is unknown."""
    try:
        return FLAWS_BY_KIND[kind]
    except KeyError:
        raise KeyError(
            f"unknown flaw kind {kind!r}; known kinds: {sorted(FLAWS_BY_KIND)}"
        ) from None


def rules_without_hard_negative() -> list[str]:
    """Single-scope rules with no *implemented* negative fixture.

    This answers one question: is a rule's precision measured only against its own true
    positives? A rule is protected only when a negative fixture is actually **served** for
    it, not when a rationale for one is written down.

    The first version of this function returned `[f.kind for f in ALL_FLAWS if not
    f.hard_negative_why]`, which tests whether the *prose* field is populated. All 25
    flaws carry that field, so the function returned an empty list and `farm validate`
    printed "rules without a hard negative: none" -- while seven single-scope rules had no
    fixture implemented at all. The effect was to report that every rule was protected when
    seven were not, and those seven carried **inflated precision**, which is the exact
    defect the two-function split exists to expose.

    The authoritative list is `farm.services.rules_with_hard_negative_fixture()`, which
    reports what the generator actually serves. This function is derived from it so the
    two can no longer disagree.
    """
    from farm.services import rules_with_hard_negative_fixture

    implemented = set(rules_with_hard_negative_fixture())
    return [f.kind for f in SINGLE_FLAWS if f.kind not in implemented]


def catalogue_summary() -> dict[str, object]:
    """Counts used by the report and by the generator's self-check."""
    return {
        "total": len(ALL_FLAWS),
        "single": len(SINGLE_FLAWS),
        "cluster": len(CLUSTER_FLAWS),
        "by_rule_class": {
            rc.value: sum(1 for f in ALL_FLAWS if f.rule_class is rc)
            for rc in RuleClass
        },
        "without_hard_negative": rules_without_hard_negative(),
    }
