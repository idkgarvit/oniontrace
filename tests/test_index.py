"""Tests for the index this project builds for itself.

Two things are being pinned here. First, that the extraction and ranking behave the way the
prose in ``collect/index.py`` says they do, because a keyword index that quietly matches
substrings or ignores half a query produces confident nonsense and looks fine in a demo.
Second, that the index cannot cite itself: the answers this project gave must never become
evidence for the next answer.

The addresses used below are hand-made and deliberately meaningless. Nothing here contacts
anything, and no real service is named.
"""

from __future__ import annotations

import json

import pytest

from collect.index import (
    DEFAULT_INDEX_FILENAME,
    MAX_CONTEXT_CHARS,
    OnionIndex,
    addresses_with_context,
    index_for_case,
    readable_text,
)

#: Base32 alphabet only: no 0, 1, 8 or 9. An "address" containing those is not an address,
#: which is the mistake this project already made once in a test of its own.
A = "a" * 56 + ".onion"
B = "b" * 56 + ".onion"
C = "c" * 56 + ".onion"


@pytest.fixture
def index(tmp_path) -> OnionIndex:
    return OnionIndex(tmp_path / "index.sqlite")


# --------------------------------------------------------------------------- #
# Reading text
# --------------------------------------------------------------------------- #


def test_markup_is_stripped_to_the_words_a_reader_would_see():
    html = (
        "<html><head><title>t</title><style>body{color:red}</style></head>"
        "<body><p>Market &amp; forum</p><!-- hidden note --><script>var x=1</script></body></html>"
    )
    text = readable_text(html)
    assert "Market & forum" in text
    assert "color:red" not in text
    assert "var x" not in text
    assert "hidden note" not in text
    assert "<" not in text


def test_an_address_is_kept_with_the_words_around_it():
    text = "mirrors for the market: " + A + " is the main one, " + B + " the backup"
    found = addresses_with_context(text)
    assert set(found) == {A, B}
    assert "main one" in found[A]
    assert "backup" in found[B]


def test_the_context_window_is_bounded_and_says_where_it_was_cut():
    text = ("padding words " * 60) + A + (" trailing words" * 60)
    context = addresses_with_context(text)[A]
    assert len(context) <= MAX_CONTEXT_CHARS + 6
    assert context.startswith("...")
    assert context.endswith("...")


def test_an_address_repeated_three_hundred_times_is_recorded_once():
    text = f"<a href='http://{A}/'>mirror</a>" * 300
    assert list(addresses_with_context(text)) == [A]


def test_a_sixteen_character_v2_address_is_not_an_address():
    """The project collects v3 addresses; a v2 name is a different thing and a dead one."""
    assert addresses_with_context("old service abcdefghijklmnop.onion") == {}


# --------------------------------------------------------------------------- #
# Storing and finding
# --------------------------------------------------------------------------- #


def test_an_empty_index_answers_nothing_and_says_so(index):
    assert index.empty
    assert index.stats() == {"mentions": 0, "onions": 0, "sources": 0}
    assert index.search("market") == []


def test_ingesting_a_page_makes_its_addresses_searchable(index):
    written = index.ingest_text(
        text="a marketplace for stolen accounts listing " + A,
        source_url="http://example.invalid/one",
        title="Market index",
        captured_at="2026-10-01T00:00:00+00:00",
    )
    assert written == 1
    hits = index.search("marketplace")
    assert [hit.onion for hit in hits] == [A]
    assert hits[0].source_url == "http://example.invalid/one"
    assert hits[0].captured_at == "2026-10-01T00:00:00+00:00"


def test_the_same_page_ingested_twice_updates_rather_than_duplicates(index):
    index.ingest_text(text="first wording " + A, source_url="http://example.invalid/p")
    index.ingest_text(text="second wording " + A, source_url="http://example.invalid/p")
    assert index.stats()["mentions"] == 1
    assert "second wording" in index.search("wording")[0].context


def test_the_same_address_on_two_pages_is_two_pieces_of_evidence(index):
    index.ingest_text(text="main mirror " + A, source_url="http://example.invalid/one")
    index.ingest_text(text="backup mirror " + A, source_url="http://example.invalid/two")
    assert index.stats() == {"mentions": 2, "onions": 1, "sources": 2}


def test_a_query_matches_only_when_every_word_is_present(index):
    index.ingest_text(text="stolen card shop " + A, source_url="http://example.invalid/one")
    index.ingest_text(text="carding tutorial " + B, source_url="http://example.invalid/two")
    assert [hit.onion for hit in index.search("stolen card")] == [A]
    assert [hit.onion for hit in index.search("stolen tutorial")] == []


def test_a_word_matches_whole_words_only(index):
    """The padded haystack exists for this: "art" must not answer for "carte" or "market"."""
    index.ingest_text(text="carte blanche " + A, source_url="http://example.invalid/one")
    assert index.search("art") == []
    assert [hit.onion for hit in index.search("carte")] == [A]


def test_a_title_match_outranks_a_body_match(index):
    index.ingest_text(text="body mentions market " + A, source_url="http://example.invalid/a")
    index.ingest_text(
        text="body mentions nothing relevant " + B,
        source_url="http://example.invalid/b",
        title="market",
    )
    assert [hit.onion for hit in index.search("market")] == [B, A]
    assert index.search("market")[0].score > index.search("market")[1].score


def test_the_same_query_returns_the_same_order_every_time(index, tmp_path):
    for address in (C, A, B):
        index.ingest_text(text="market " + address, source_url=f"http://example.invalid/{address}")
    first = [hit.onion for hit in index.search("market")]
    second = [hit.onion for hit in OnionIndex(tmp_path / "index.sqlite").search("market")]
    assert first == second == sorted(first)


def test_the_limit_is_an_answer_bound_not_a_silent_truncation(index):
    for letter in ("a", "b", "c", "d"):
        index.ingest_text(text=f"market {letter * 56}.onion", source_url=f"http://example.invalid/{letter}")
    assert len(index.search("market", limit=2)) == 2


def test_a_query_with_no_words_is_refused_rather_than_answered(index):
    index.ingest_text(text="market " + A, source_url="http://example.invalid/one")
    with pytest.raises(ValueError):
        index.search("   !!! ")


# --------------------------------------------------------------------------- #
# Ingesting what the case already holds
# --------------------------------------------------------------------------- #


def test_a_stored_snapshot_is_ingested_with_its_own_provenance(index):
    payload = {
        "origin": "onion_service",
        "source_url": "http://" + A + "/",
        "captured_at": "2026-10-02T00:00:00+00:00",
        "content": {"title": "Shop", "excerpt": "<p>our mirrors: " + B + "</p>"},
    }
    assert index.ingest_payload(payload) == 1
    hit = index.search("mirrors")[0]
    assert hit.onion == B
    assert hit.source_url == "http://" + A + "/"


def test_the_index_never_ingests_its_own_answers(index):
    """A results page lists what the index already knows; ingesting it would be self-citation.

    The payload here carries text on purpose. The answer this project stores today holds only
    parsed candidates, which would ingest nothing even without the guard, so a test built from
    the current shape would pass for the wrong reason: it was written that way first, and
    removing the guard left it green. What the guard actually protects is the next provider,
    which may well store the page it read. This payload is that future shape.
    """
    payload = {
        "origin": "public_index",
        "source_url": "oniontrace-index://CASE/search?q=market",
        "content": {
            "query": "market",
            "candidates": [{"onion": A}],
            "text": "a marketplace listing " + A + " and " + B,
        },
    }
    assert index.ingest_payload(payload) == 0
    assert index.empty


def test_ingesting_snapshots_reports_what_it_did(index):
    payloads = [
        {
            "origin": "onion_service",
            "source_url": "http://" + A + "/",
            "content": {"excerpt": "links " + B},
        },
        {"origin": "onion_location", "source_url": "https://example.invalid/x", "content": {}},
        {"origin": "onion_service", "source_url": "http://" + C + "/", "content": {"excerpt": "no links here"}},
    ]
    written, pages = index.ingest_snapshots(payloads)
    assert (written, pages) == (1, 1)


def test_a_page_with_no_address_is_counted_as_read_and_not_written(index):
    written, pages = index.ingest_snapshots(
        [{"origin": "onion_service", "source_url": "http://x/", "content": {"excerpt": "nothing"}}]
    )
    assert (written, pages) == (0, 0)
    assert index.empty


# --------------------------------------------------------------------------- #
# Importing a dataset
# --------------------------------------------------------------------------- #


def test_a_jsonl_dataset_is_imported_line_by_line(index, tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        "\n".join(
            [
                json.dumps({"onion": A, "title": "market", "source_url": "dataset:test"}),
                "# a comment, ignored",
                "",
                json.dumps({"onion": "not-an-onion", "title": "junk"}),
                json.dumps({"onion": B, "context": "a market mirror"}),
            ]
        ),
        encoding="utf-8",
    )
    written, skipped = index.ingest_jsonl(dataset)
    assert (written, skipped) == (2, 1)
    assert {hit.onion for hit in index.search("market")} == {A, B}


def test_a_dataset_line_without_a_usable_address_is_skipped_not_fatal(index, tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"onion": "abc"}\n{"onion": "%s"}\n' % A, encoding="utf-8")
    written, skipped = index.ingest_jsonl(dataset)
    assert (written, skipped) == (1, 1)


def test_importing_a_missing_dataset_raises_rather_than_reporting_success(index, tmp_path):
    with pytest.raises(FileNotFoundError):
        index.ingest_jsonl(tmp_path / "absent.jsonl")


def test_an_imported_address_keeps_the_citation_it_was_given(index, tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps({"onion": A, "title": "market", "source_url": "https://example.invalid/list"}),
        encoding="utf-8",
    )
    index.ingest_jsonl(dataset)
    assert index.search("market")[0].source_url == "https://example.invalid/list"


# --------------------------------------------------------------------------- #
# Where the index lives
# --------------------------------------------------------------------------- #


class _Cases:
    """The one method ``index_for_case`` needs, so the test cannot accidentally use more."""

    def __init__(self, root):
        self._root = root

    def derived_dir(self, case_id: str):
        return self._root / "cases" / case_id / "derived"


def test_the_index_lives_inside_the_directory_the_purge_deletes(tmp_path):
    """Retention is only real if derived data is inside the thing that gets removed."""
    index = index_for_case(_Cases(tmp_path), "CASE-1")
    assert index.path.parent == tmp_path / "cases" / "CASE-1" / "derived"
    assert index.path.name == DEFAULT_INDEX_FILENAME


def test_a_snapshot_stored_by_the_gate_is_readable_too(index):
    """The gate's demonstration snapshots keep their text at the top level, not in content."""
    assert index.ingest_payload({"seed": "http://example.invalid/demo", "body": "links " + A}) == 1
    hit = index.search("links")[0]
    assert hit.onion == A
    assert hit.source_url == "http://example.invalid/demo"


def test_a_recorded_address_is_indexed_even_when_the_excerpt_was_cut_before_it(index):
    """The live directory walk found this: the address sat past the 4000-char excerpt.

    The snapshot recorded it in ``onions``, and an index that only read the excerpt counted
    that page as having found nothing. One page, two onions, zero mentions is a silent
    negative, which is the one kind of wrong answer this project cannot ship.
    """
    payload = {
        "origin": "onion_location",
        "source_url": "https://publisher.invalid/directory/org/",
        "content": {
            "title": "Org AS",
            "excerpt": "<html><body>" + ("filler " * 700) + "</body></html>",
            "onions": [A, B],
        },
    }
    assert index.ingest_payload(payload) == 2
    assert {hit.onion for hit in index.search("org")} == {A, B}
    assert index.search("org")[0].context  # the title stands in for the missing sentence
