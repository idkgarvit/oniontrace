"""Telegram public-preview collection: ``t.me/s/<channel>`` and nothing else.

Scope, from the plan (B2): *server-rendered HTML preview only, no credentials, no
MTProto, no member lists.* The collector's only interaction with Telegram is an
unauthenticated GET of a page a browser would render publicly. There is no login path,
no session object, and no place in this module where a token could be passed -- so the
absence of credentials is a property of the code, not a promise in a comment.

The mechanics that make this a collector rather than a scraper:

* **Backward pagination.** ``t.me/s/<channel>`` returns the newest slice of history;
  ``?before=<id>`` returns the slice immediately older than ``<id>``. Walking a channel
  therefore means carrying the *oldest* id seen so far as the cursor.
* **Dedupe by message id.** Overlapping pages are normal. Message identity is
  ``channel/message_id``, which is stable across fetches, so re-collection adds nothing.
* **Resumable checkpoints.** The cursor and the set of ids already collected are stored
  per case, so an interrupted run resumes rather than restarting -- and a restart does
  not silently duplicate a channel's history into the case twice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

PREVIEW_HOST = "t.me"
_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def preview_url(channel: str, *, before: int | None = None) -> str:
    """The URL for one page of a channel's public preview history."""
    if not _CHANNEL_RE.match(channel):
        raise ValueError(f"{channel!r} is not a valid public channel name")
    suffix = f"?before={before}" if before is not None else ""
    return f"https://{PREVIEW_HOST}/s/{channel}{suffix}"


@dataclass(frozen=True)
class PreviewMessage:
    """One message as rendered publicly. No author identity beyond the handle shown."""

    channel: str
    message_id: int
    posted_at: str
    text: str

    @property
    def dedupe_key(self) -> str:
        """Identity used to decide whether this message is already in the case."""
        return f"{self.channel}/{self.message_id}"


class _PreviewParser(HTMLParser):
    """Pulls message id, timestamp and text out of a ``t.me/s/`` page.

    Deliberately tolerant: it looks for two class markers and the ``data-post``
    attribute and ignores everything else. Depending on more of the real page's markup
    would make this break whenever Telegram changes a stylesheet.
    """

    def __init__(self, channel: str) -> None:
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.messages: list[PreviewMessage] = []
        self._depth = 0
        self._msg_depth: int | None = None
        self._msg_id: int | None = None
        self._posted_at = ""
        self._text_depth: int | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        if tag != "div":
            # Depth is tracked over div nesting only. Counting every tag would drift on
            # <time> and on void elements like <br>, which have no closing tag, and the
            # message-boundary arithmetic would silently mis-nest.
            if tag == "time" and self._msg_depth is not None and found.get("datetime"):
                self._posted_at = found["datetime"]
            return
        self._depth += 1
        classes = found.get("class", "")
        if self._msg_depth is None and "widget_message_wrap" in classes:
            self._msg_depth = self._depth
            self._msg_id = _message_id(found.get("data-post", ""), self.channel)
            self._posted_at = ""
            self._parts = []
            return
        if self._msg_depth is not None and self._text_depth is None:
            if "js-message_text" in classes:
                self._text_depth = self._depth
                self._parts = []

    def handle_data(self, data):
        if self._text_depth is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self._text_depth == self._depth:
            self._text_depth = None
        if self._msg_depth == self._depth:
            if self._msg_id is not None:
                text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
                self.messages.append(
                    PreviewMessage(
                        channel=self.channel,
                        message_id=self._msg_id,
                        posted_at=self._posted_at,
                        text=text,
                    )
                )
            self._msg_depth = None
            self._msg_id = None
            self._parts = []
        self._depth -= 1


def _message_id(data_post: str, channel: str) -> int | None:
    """``data-post="channel/1234"`` -> 1234."""
    if "/" not in data_post:
        return None
    head, _, tail = data_post.partition("/")
    if head != channel:
        # A preview page should carry only its own channel's messages. Dropping foreign
        # ones keeps a malformed or hostile page from injecting another channel into
        # this case's collection.
        return None
    try:
        return int(tail)
    except ValueError:
        return None


def parse_preview(html: str, *, channel: str) -> list[PreviewMessage]:
    """One page of history, newest first."""
    parser = _PreviewParser(channel)
    parser.feed(html)
    parser.close()
    return sorted(parser.messages, key=lambda m: m.message_id, reverse=True)


def oldest_id(messages: list[PreviewMessage]) -> int | None:
    """The cursor for the next page backward, or None if the walk is finished."""
    if not messages:
        return None
    return min(m.message_id for m in messages)


class MessageLedger:
    """Message ids already held for a case, so re-collection is a no-op.

    Built from the case store on start-up rather than from memory, so a new process
    cannot re-collect a channel's history a second time just because it forgot.
    """

    def __init__(self, known_keys: tuple[str, ...] = ()):
        self._seen: set[str] = set(known_keys)
        self.added = 0
        self.duplicates = 0

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def claim(self, message: PreviewMessage) -> bool:
        """True if this message is new to the case; False if the case already holds it."""
        if message.dedupe_key in self._seen:
            self.duplicates += 1
            return False
        self._seen.add(message.dedupe_key)
        self.added += 1
        return True

    @property
    def size(self) -> int:
        return len(self._seen)

