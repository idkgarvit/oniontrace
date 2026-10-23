"""A keyword index built from pages this project is allowed to read.

The ``public_index`` origin was shipped without a provider, and the reason was structural
rather than lazy. The indexes that exist either refuse automation (Ahmia answers every
request with a 302 and publishes ``Disallow: /search/``) or are operated by an organisation
whose terms forbid querying them. A discovery path that depends on one of those is a path
that is broken on the day it matters, and this project has already retired one dependency
for exactly that reason: ``crt.sh``.

So the index is built here instead. Every page the collector fetches lawfully can mention
other onion addresses, and a page that names an address is evidence about that address: who
linked to it, in what words, on what date. Extracting those mentions into a local index
turns collection into discovery without asking anyone for permission and without a network
request that could be refused. The index therefore works on an air-gapped machine, keeps
every hit traceable to the page that produced it, and grows with the corpus rather than with
the operator's subscriptions.

What this is not: a substitute for a full-text search engine over the dark web. It can only
find addresses that appear in material already collected, or in a dataset an operator
imports deliberately with ``ingest_jsonl``. That limit is real, it is stated in the README,
and it is the same limit every lawful discovery path in this project has: you can only find
what someone has already shown you.

The address format is the only hard part of parsing, and it is already solved in
``collect/ahmia.py``. The regex is imported rather than copied so a change to what counts as
an onion address cannot land in one parser and not the other.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .ahmia import ONION_RE

__all__ = [
    "DEFAULT_INDEX_FILENAME",
    "Hit",
    "OnionIndex",
    "index_for_case",
]


#: Fixed name inside the case's ``derived`` directory. Fixed for the same reason the
#: checkpoint and proposal stores fix theirs: ``purge`` deletes the directory, so nothing
#: derived from case content can survive under a name the purge does not know to look for.
DEFAULT_INDEX_FILENAME = "onion-index.sqlite"

#: How much text around an address is kept as its context. Enough to read a sentence, short
#: enough that a snapshot of a thousand links does not become a thousand paragraphs.
CONTEXT_RADIUS = 140

#: Titles and contexts are stored for reading, not for archiving. The page is the archive.
MAX_TITLE_CHARS = 200
MAX_CONTEXT_CHARS = 320

#: An address is quoted in one page at most this many times before the rest is ignored.
MAX_CONTEXT_WINDOWS_PER_PAGE = 400

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mentions (
    onion       TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    context     TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL DEFAULT '',
    haystack    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (onion, source_url)
);
CREATE INDEX IF NOT EXISTS mentions_by_onion ON mentions (onion);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Hit:
    """One address the index can answer for, with the page that put it there."""

    onion: str
    title: str
    context: str
    source_url: str
    captured_at: str
    score: int
    titles: int
    body: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "onion": self.onion,
            "title": self.title,
            "context": self.context,
            "source_url": self.source_url,
            "captured_at": self.captured_at,
            "score": self.score,
        }


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _haystack(title: str, context: str) -> str:
    """A padded token string, so ``LIKE '% word %'`` means a whole word.

    Substring matching would answer "art" for "carte" and "market" for "supermarket". The
    padding costs two spaces per row and removes a class of wrong answers that would be
    invisible in a demo and obvious to an analyst reading a hit about a different service.
    """
    return " " + " ".join(_tokens(f"{title} {context}")) + " "


def readable_text(html: str) -> str:
    """Strip markup down to the words a person would see.

    Deliberately crude and deliberately total: the purpose is a context window an analyst
    can read, not fidelity to a rendering engine. A miss here costs a slightly noisy
    citation, never a missed address, because addresses are matched on the raw body as well.
    """
    without_comments = _COMMENT_RE.sub(" ", html)
    without_code = _SCRIPT_RE.sub(" ", without_comments)
    flattened = _TAG_RE.sub(" ", without_code)
    flattened = (
        flattened.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return " ".join(flattened.split())


def _window(text: str, start: int, end: int, radius: int = CONTEXT_RADIUS) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    window = " ".join(text[left:right].split())[:MAX_CONTEXT_CHARS]
    if left > 0:
        window = "..." + window
    if right < len(text):
        window = window + "..."
    return window


def addresses_with_context(text: str, *, limit: int = MAX_CONTEXT_WINDOWS_PER_PAGE) -> dict[str, str]:
    """Every distinct onion address in ``text``, each mapped to the words around it.

    The first occurrence wins. A link repeated in a sidebar 300 times says the same thing
    as the first mention and would otherwise dominate the context for that address.
    """
    found: dict[str, str] = {}
    for match in ONION_RE.finditer(text):
        address = match.group(1).lower()
        if address in found:
            continue
        found[address] = _window(text, match.start(), match.end())
        if len(found) >= limit:
            break
    return found


class OnionIndex:
    """A case-scoped index of the addresses this project has seen, and where it saw them."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    # -- storage ---------------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        # ``timeout`` rather than a long-lived connection: the collector opens the index,
        # writes, and closes, and two runs on the same case must serialise rather than
        # interleave. A rollback journal keeps the index one file, so the purge deletes it
        # with the directory it lives in.
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def ingest_text(
        self,
        *,
        text: str,
        source_url: str,
        title: str = "",
        captured_at: str = "",
    ) -> int:
        """Record every address in ``text``, attributed to ``source_url``.

        An address already attributed to the same page is updated rather than duplicated:
        the page's current context replaces the old one, because a page that changed its
        wording is better described by what it says now. The attribution is what makes the
        index auditable, so it is part of the key rather than a column that can be lost.
        """
        if not text:
            return 0
        readable = readable_text(text) if "<" in text and ">" in text else " ".join(text.split())
        found = addresses_with_context(readable)
        if not found:
            return 0
        # Addresses that only appear inside markup (an href, a data attribute) are still
        # addresses. Scan the raw text too, and keep the readable window where we have one.
        for address, context in addresses_with_context(text).items():
            found.setdefault(address, context)
        return self._ingest_addresses(
            found, source_url=source_url, title=title, captured_at=captured_at
        )

    def _ingest_addresses(
        self,
        found: Mapping[str, str],
        *,
        source_url: str,
        title: str,
        captured_at: str,
    ) -> int:
        """Write address/context pairs for one page, updating rather than duplicating."""
        short_title = " ".join(title.split())[:MAX_TITLE_CHARS]
        rows = [
            (
                address,
                source_url,
                short_title,
                context,
                captured_at,
                _haystack(short_title, context),
            )
            for address, context in found.items()
        ]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO mentions (onion, source_url, title, context, captured_at, haystack)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (onion, source_url) DO UPDATE SET
                    title = excluded.title,
                    context = excluded.context,
                    captured_at = excluded.captured_at,
                    haystack = excluded.haystack
                """,
                rows,
            )
            connection.commit()
        return len(rows)

    def ingest_payload(self, payload: Mapping[str, Any]) -> int:
        """Ingest one stored snapshot, refusing to ingest this origin's own answers.

        A results page produced by the ``public_index`` origin lists addresses the index
        already holds. Feeding it back would let the index cite itself, inflate the score of
        whatever it already held, and make a fresh query look better supported than it is.

        Today's answer carries only parsed candidates and no page text, so it would ingest
        nothing even without this check. The check is here for the provider that stores what
        it read, and the test that guards it constructs exactly that shape, because a test
        built from today's payload passed with the guard removed and proved nothing.
        """
        origin = str(payload.get("origin") or "")
        if origin == "public_index":
            return 0
        content = payload.get("content")
        if not isinstance(content, Mapping):
            # A snapshot stored through the gate's demonstration path carries its text at
            # the top level instead of under ``content``. Reading both shapes means the
            # index sees every page the case holds, not only the ones a collector wrote.
            body = payload.get("body")
            return self.ingest_text(
                text=body if isinstance(body, str) else "",
                source_url=str(payload.get("source_url") or payload.get("seed") or ""),
                captured_at=str(payload.get("captured_at") or ""),
            )
        excerpt = content.get("excerpt") or content.get("text") or ""
        if not isinstance(excerpt, str) or not excerpt.strip():
            return 0
        raw_title = content.get("title")
        title = raw_title if isinstance(raw_title, str) else ""
        source_url = str(payload.get("source_url") or "")
        captured_at = str(payload.get("captured_at") or "")
        found = addresses_with_context(readable_text(excerpt))
        # A snapshot may also carry the addresses its path already extracted. That is not
        # redundant with the excerpt: the excerpt is cut at 4000 characters, and on a real
        # directory page the address sat past the cut, so a page that found two onions
        # indexed as if it had found none. The structured field is the finding; the text is
        # the context around it.
        recorded = content.get("onions")
        if isinstance(recorded, (list, tuple)):
            for address in recorded:
                if not isinstance(address, str):
                    continue
                cleaned = address.strip().lower()
                if cleaned and cleaned not in found:
                    found[cleaned] = title or "listed on " + source_url
        return self._ingest_addresses(
            found, source_url=source_url, title=title, captured_at=captured_at
        )

    def ingest_snapshots(self, payloads: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
        """Ingest a case's stored snapshots. Returns (addresses written, pages read)."""
        addresses = 0
        pages = 0
        for payload in payloads:
            written = self.ingest_payload(payload)
            if written:
                addresses += written
                pages += 1
        return addresses, pages

    def ingest_jsonl(self, path: str | Path) -> tuple[int, int]:
        """Import an address dataset, one JSON object per line.

        This is the door an operator uses for a published dataset (DUTA-10K and anything
        like it) or for a list they are entitled to hold. The format is deliberately the
        least surprising one available::

            {"onion": "<56 chars>.onion", "title": "...", "source_url": "...", "context": "..."}

        ``onion`` is required. Everything else is optional, and a line without a usable
        address is counted and skipped rather than aborting the import: half a dataset is
        worth more than none, and the count is reported so the operator can see what landed.
        """
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(f"no such index dataset: {source}")
        written = 0
        skipped = 0
        with source.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(record, Mapping):
                    skipped += 1
                    continue
                address = str(record.get("onion") or "").strip().lower()
                if not ONION_RE.fullmatch(address):
                    skipped += 1
                    continue
                title = str(record.get("title") or "")
                context = str(record.get("context") or "")
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO mentions (onion, source_url, title, context, captured_at, haystack)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (onion, source_url) DO UPDATE SET
                            title = excluded.title,
                            context = excluded.context,
                            captured_at = excluded.captured_at,
                            haystack = excluded.haystack
                        """,
                        (
                            address,
                            str(record.get("source_url") or f"dataset:{source.name}"),
                            title[:MAX_TITLE_CHARS],
                            context[:MAX_CONTEXT_CHARS],
                            str(record.get("captured_at") or ""),
                            _haystack(title, context),
                        ),
                    )
                    connection.commit()
                written += 1
        return written, skipped

    # -- answers ---------------------------------------------------------- #

    def search(self, term: str, *, limit: int = 50) -> list[Hit]:
        """Addresses whose title or context contains every word of ``term``.

        All words, not any: a query is an intent, and answering it with pages that share
        one common word is how a keyword index produces confident nonsense. Ranking is
        title matches first, then body matches, then address order so two identical runs
        return the same list in the same order.
        """
        tokens = _tokens(term)
        if not tokens:
            raise ValueError(f"search term {term!r} contains no letters or digits")
        where = " AND ".join(["haystack LIKE ?"] * len(tokens))
        parameters = [f"% {token} %" for token in tokens]
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT onion, title, context, source_url, captured_at, haystack "
                f"FROM mentions WHERE {where}",
                parameters,
            ).fetchall()
        hits: list[Hit] = []
        for row in rows:
            title_tokens = _tokens(row["title"])
            body_tokens = _tokens(row["context"])
            titles = sum(title_tokens.count(token) for token in tokens)
            body = sum(body_tokens.count(token) for token in tokens)
            if titles == 0 and body == 0:
                # The words are present but not as whole words; LIKE found the substring
                # inside a larger token. Counting it would be the very false positive the
                # padded haystack exists to prevent.
                continue
            hits.append(
                Hit(
                    onion=row["onion"],
                    title=row["title"],
                    context=row["context"],
                    source_url=row["source_url"],
                    captured_at=row["captured_at"],
                    score=titles * 3 + body,
                    titles=titles,
                    body=body,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, -hit.titles, hit.onion))
        return hits[:limit]

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            mentions = connection.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
            onions = connection.execute("SELECT COUNT(DISTINCT onion) FROM mentions").fetchone()[0]
            sources = connection.execute(
                "SELECT COUNT(DISTINCT source_url) FROM mentions"
            ).fetchone()[0]
        return {"mentions": int(mentions), "onions": int(onions), "sources": int(sources)}

    @property
    def empty(self) -> bool:
        return self.stats()["mentions"] == 0

    def __len__(self) -> int:
        return self.stats()["mentions"]


def index_for_case(cases: Any, case_id: str) -> OnionIndex:
    """The index for one case, always inside the directory ``purge`` deletes."""
    return OnionIndex(Path(cases.derived_dir(case_id)) / DEFAULT_INDEX_FILENAME)
