"""What a host says about automated access, and what this project does about it.

Why this exists
---------------
Two sources in this project's history produced the same lesson from opposite directions.
``ahmia.fi`` publishes ``Disallow: /search/`` and enforces it. ``crt.sh`` publishes
``Disallow: /`` for everyone and serves data anyway. A tool that hardcodes one host as
"allowed" and another as "removed" has not made a policy, it has made two exceptions.

So the rule is procedural instead: **ask the host, record the answer, refuse when the answer
is no, and let a human override only on the record.** The override exists because a
researcher may have a lawful reason to query a source whose crawler policy is broader than
its API policy -- but that has to be a decision somebody made, with their name on it, not a
line of code that quietly stopped checking.

What this module does not do
----------------------------
It does not fetch anything. Parsing a robots file and deciding what it means is pure, so it
can be tested against the awkward cases -- wildcards, anchors, empty Disallow, several
groups -- without a socket. The caller owns the request, because the caller already owns
polite fetching and the budget.
"""

from __future__ import annotations

import re
from typing import Iterable

#: The product token this collector offers. A robots file may name it explicitly, which is
#: how an operator says "not you" to a specific crawler; the wildcard group is the fallback.
PRODUCT_TOKEN = "oniontrace"

_DIRECTIVE = re.compile(r"^(?P<field>[A-Za-z-]+)\s*:\s*(?P<value>.*)$")


def _matches(pattern: str, path: str) -> bool:
    """Does one Disallow pattern cover this path?

    robots.txt matching is prefix matching with two special cases: ``*`` matches any run of
    characters, and a trailing ``$`` anchors the end. Written as a regex conversion rather
    than startswith because ``/*?`` -- disallow anything with a query string -- is a real
    rule in a real file and a startswith check silently ignores it.
    """
    if not pattern:
        return False
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = re.escape(body).replace(r"\*", ".*")
    if anchored:
        return re.fullmatch(expression, path) is not None
    return re.match(expression, path) is not None


def _groups(text: str) -> list[tuple[list[str], list[str]]]:
    """Split a robots file into ``(agents, disallow rules)`` groups.

    Consecutive ``User-agent`` lines share one group, and a group ends at the first rule
    that follows it once another ``User-agent`` appears. Getting this wrong is how a crawler
    ends up obeying the rules meant for a different robot, so it is separated out where a
    test can call it directly.
    """
    groups: list[tuple[list[str], list[str]]] = []
    agents: list[str] = []
    rules: list[str] = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _DIRECTIVE.match(line)
        if not match:
            continue
        field = match.group("field").lower()
        value = match.group("value").strip()
        if field == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif field == "disallow" and agents:
            rules.append(value)
    if agents or rules:
        groups.append((agents, rules))
    return groups


def rules_for(text: str, *, user_agent: str = PRODUCT_TOKEN) -> tuple[str, ...]:
    """Every Disallow pattern that applies to this collector.

    A group applies if one of its agents is ``*`` or names this product. When several groups
    apply they are combined, which is stricter than some crawlers and right here: the cost of
    obeying an extra rule is one request not made.

    ``Allow`` directives are ignored, which over-blocks rather than under-blocks. A robots
    file that allows a path inside a disallowed tree is telling a crawler how to behave; this
    collector would rather skip that path than argue about precedence with a stranger's file.
    """
    collected: list[str] = []
    for agents, rules in _groups(text):
        if any(agent == "*" or agent.startswith(user_agent.lower()) for agent in agents):
            collected.extend(rules)
    return tuple(collected)


def disallowed_by(text: str, path: str, *, user_agent: str = PRODUCT_TOKEN) -> str | None:
    """The rule that forbids this path, or ``None`` when the host says nothing about it.

    ``path`` must include the query string when there is one: a rule like ``/*?`` is about
    the query, and a caller that passes only ``/search/`` gets a wrong answer.
    """
    for pattern in rules_for(text, user_agent=user_agent):
        if _matches(pattern, path):
            return pattern
    return None


def verdict(text: str | None, path: str, *, user_agent: str = PRODUCT_TOKEN) -> tuple[bool, str]:
    """``(allowed, explanation)`` for one path on one host.

    ``text`` is ``None`` when the host served no robots file at all. That is not a refusal:
    a host with nothing to say has said nothing, and treating an absent file as a ban would
    turn a 404 into a policy decision the host never made.
    """
    if text is None:
        return True, "no robots.txt was served, so the host states no policy"
    rule = disallowed_by(text, path, user_agent=user_agent)
    if rule is None:
        return True, "robots.txt does not disallow this path"
    # Quoted in the file's own vocabulary so the note reads as a citation, not a paraphrase:
    # a reader can find "Disallow: /search/" in the file and see for themselves.
    return False, f"robots.txt says Disallow: {rule}"


def summarise(rules: Iterable[str]) -> str:
    """A short, quotable rendering of the rules that apply, for a note or an audit entry."""
    listed = [rule for rule in rules if rule]
    return "; ".join(f"Disallow: {rule}" for rule in listed[:4]) or "no disallow rules"

_CRAWL_DELAY_RE = re.compile(r"crawl-delay\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_SITEMAP_RE = re.compile(r"^sitemap\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def sitemaps(text: str) -> tuple[str, ...]:
    """The sitemap URLs a host declares in its robots file, in the order it lists them.

    This is the host telling a crawler where its own map is. Reading it is the difference
    between a walk that has to be handed the right entry page and one that can start from
    the host's front page, because a site that publishes a sitemap has already said which
    of its pages it wants found.

    Not group-scoped, unlike ``Disallow``: the standard puts ``Sitemap`` outside the agent
    groups, so a file with several blocks still declares its maps exactly once.
    """
    declared: list[str] = []
    for match in _SITEMAP_RE.findall(text or ""):
        value = match.strip()
        if value and value not in declared:
            declared.append(value)
    return tuple(declared)


def crawl_delay(text: str) -> float | None:
    """The host's requested delay between requests, when it states one.

    ``Crawl-delay`` is not part of the robots standard any more, and a site that states it is
    asking anyway. The largest value in the file is honoured: a policy that applies to other
    agents and not to this one still describes how the host wants to be read, and reading it
    faster than asked because the rule does not strictly bind is a choice a collector should
    not make on its own.

    Deliberately takes no ``user_agent``. It used to accept one and ignore it, which is worse
    than not having it: a caller could pass their own token and reasonably believe the answer
    was scoped to them, while the function returned the file-wide maximum either way. The
    over-compliance is the policy; the parameter was a claim the code did not keep.
    """
    values = [float(match) for match in _CRAWL_DELAY_RE.findall(text)]
    return max(values) if values else None
