"""Reading a host's own statement about automated access, and refusing on it.

Two real files drive these tests, because the two situations they create are the reason the
module exists: ``crt.sh`` disallows everything and serves data anyway, while ``ahmia.fi``
disallows the search path and enforces it. A project that hardcodes one as permitted and the
other as removed has not made a policy, only two exceptions.
"""

from collect.robots import (
    PRODUCT_TOKEN,
    disallowed_by,
    rules_for,
    summarise,
    verdict,
)

#: crt.sh's actual file, served with 200 and 26 bytes.
CRTSH = "User-agent: *\nDisallow: /\n"

#: ahmia.fi's actual file, abbreviated to the rules that matter here.
AHMIA = (
    "User-agent: *\n"
    "Disallow: /add/onionsadded\n"
    "Disallow: /address\n"
    "Disallow: /static/\n"
    "Disallow: /search/\n"
    "Disallow: /*?\n"
)


def test_a_blanket_disallow_covers_every_path():
    assert disallowed_by(CRTSH, "/?q=example.com&output=json") == "/"
    assert disallowed_by(CRTSH, "/anything") == "/"


def test_a_path_rule_matches_as_a_prefix():
    assert disallowed_by(AHMIA, "/search/?q=market") == "/search/"
    assert disallowed_by(AHMIA, "/search/") == "/search/"


def test_a_query_marker_rule_matches_any_url_with_a_query():
    """``/*?`` is a real rule in a real file, and a startswith check would miss it."""
    assert disallowed_by(AHMIA, "/anything?q=1") == "/*?"
    assert disallowed_by(AHMIA, "/search/?q=1") in {"/search/", "/*?"}


def test_a_path_with_no_query_is_not_covered_by_the_query_marker():
    assert disallowed_by("User-agent: *\nDisallow: /*?\n", "/plain") is None


def test_a_path_outside_every_rule_is_allowed():
    assert disallowed_by(AHMIA, "/about/") is None


def test_an_empty_disallow_rule_allows_everything():
    """``Disallow:`` with no value is the file saying "no restriction", not "all paths"."""
    assert disallowed_by("User-agent: *\nDisallow:\n", "/anything") is None


def test_a_wildcard_in_the_middle_matches_anything_there():
    rules = "User-agent: *\nDisallow: /private/*/dump\n"
    assert disallowed_by(rules, "/private/2026/dump") == "/private/*/dump"
    assert disallowed_by(rules, "/private/2026/keep") is None


def test_an_anchored_rule_matches_only_the_exact_path():
    rules = "User-agent: *\nDisallow: /exact$\n"
    assert disallowed_by(rules, "/exact") == "/exact$"
    assert disallowed_by(rules, "/exact/more") is None


def test_rules_for_another_robot_do_not_apply_to_this_one():
    """Obeying the rules meant for a different crawler is its own kind of wrong."""
    rules = "User-agent: BadBot\nDisallow: /\n\nUser-agent: *\nDisallow:\n"
    assert disallowed_by(rules, "/anything") is None


def test_consecutive_user_agent_lines_share_one_group():
    rules = "User-agent: BadBot\nUser-agent: WorseBot\nDisallow: /\n"
    assert rules_for(rules) == ()


def test_a_group_naming_this_product_applies_alongside_the_wildcard():
    rules = (
        "User-agent: *\nDisallow: /one\n\n"
        f"User-agent: {PRODUCT_TOKEN}/0.1\nDisallow: /two\n"
    )
    assert set(rules_for(rules)) == {"/one", "/two"}


def test_a_host_with_no_robots_file_has_stated_no_policy():
    """A 404 is not a ban. Treating an absent file as one invents a decision nobody made."""
    allowed, why = verdict(None, "/anything")
    assert allowed
    assert "no policy" in why


def test_a_disallowed_verdict_quotes_the_rule_that_did_it():
    allowed, why = verdict(CRTSH, "/?q=x")
    assert not allowed
    assert "Disallow: /" in why


def test_the_summary_is_short_enough_to_quote_in_a_note():
    assert summarise(rules_for(AHMIA)).count("Disallow:") <= 4
    assert summarise(()) == "no disallow rules"
