"""The keyword origin, answered by the index this project built.

The origin used to ship without a provider: the public index it was named after refuses
automation, so a query there produced a recorded refusal and nothing else. These tests pin
the replacement, which is an index built from pages the collector was allowed to read.

What they check is mostly what a keyword search gets wrong when nobody is looking: that it
never opens a socket, that a hit names the page that produced it rather than the machine
that repeated it, that an empty answer is recorded as an answer instead of a success, and
that addresses a case already declared are not offered back as new leads.
"""

from __future__ import annotations

import pytest

from collect.collector import (
    ORIGIN_AHMIA,
    SEARCH_PROVIDER_AHMIA,
    SEARCH_PROVIDER_LOCAL,
    CollectionPolicy,
    CollectionScope,
    Collector,
    checkpoint_source,
)
from collect.errors import CollectionError
from collect.schedule import Politeness
from gates.gate import CollectionGate
from gates.roles import Principal, Role
from collect.clock import FrozenClock

CASE = "CASE-LOCAL-001"
OWNER = Principal(principal_id="a.sharma", role=Role.CASE_OWNER)

#: An address nothing contacts. Hand-made, so a hit proves parsing rather than luck.
CITING = "http://m" + "a" * 55 + ".onion/"
FOUND = "b" * 56 + ".onion"
DECLARED = "c" * 56 + ".onion"

FAST = Politeness(min_host_delay_seconds=0.0)


class RefusingTransport:
    """A transport that fails the test if anything tries to use it."""

    name = "refusing"

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def check(self, url: str, headers=None) -> None:  # pragma: no cover - never reached
        raise AssertionError(f"the local index opened a network path for {url}")

    def fetch(self, url: str, headers=None):  # pragma: no cover - never reached
        self.fetched.append(url)
        raise AssertionError(f"the local index fetched {url}")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at("2026-10-05T09:00:00+00:00")


@pytest.fixture
def gate(tmp_path, clock):
    gate = CollectionGate(tmp_path / "gate", clock=clock.as_gate_clock())
    gate.acknowledge(OWNER)
    gate.open_case(
        OWNER,
        case_id=CASE,
        title="Keyword discovery from collected pages",
        authorisation_ref="AUTH-2026-0100",
        retention_days=30,
    )
    return gate


def make_collector(gate, transport, clock) -> Collector:
    return Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(politeness=FAST),
    )


def seed_snapshot(gate, *, excerpt: str, source_url: str = CITING, origin: str = "onion_service"):
    """Put one collected page in the case, the way a previous run would have."""
    return gate.cases.store_snapshot(
        CASE,
        {
            "case_id": CASE,
            "origin": origin,
            "target": "mirror page",
            "dedupe_key": f"{origin}:{source_url}:seed",
            "captured_at": "2026-10-04T00:00:00+00:00",
            "source_url": source_url,
            "retention_days": 90,
            "warc_record_id": None,
            "body_sha256": None,
            "content": {"title": "Mirror list", "excerpt": excerpt},
        },
    )


def summary_for(report, origin: str):
    return next(summary for summary in report.origins if summary.origin == origin)


# --------------------------------------------------------------------------- #
# The default is the provider that works
# --------------------------------------------------------------------------- #


def test_the_default_provider_is_the_one_this_project_builds():
    assert CollectionScope().search_provider == SEARCH_PROVIDER_LOCAL


def test_an_unknown_provider_is_refused_by_name(gate, clock):
    collector = make_collector(gate, RefusingTransport(), clock)
    scope = CollectionScope(search_queries=("market",), search_provider="ask-jeeves")
    with pytest.raises(CollectionError) as caught:
        collector.collect(case_id=CASE, principal=OWNER, scope=scope)
    assert "ask-jeeves" in str(caught.value)
    assert SEARCH_PROVIDER_LOCAL in str(caught.value)


# --------------------------------------------------------------------------- #
# What the local provider does, and does not, do
# --------------------------------------------------------------------------- #


def test_a_local_query_opens_no_socket(gate, clock):
    """The whole point of a local index is that nothing can refuse it and nothing leaks."""
    transport = RefusingTransport()
    collector = make_collector(gate, transport, clock)
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(search_queries=("market",)),
    )
    assert transport.fetched == []
    assert summary_for(report, ORIGIN_AHMIA).pages == 0


def test_an_empty_index_records_that_the_query_was_asked_and_answered_nothing(gate, clock):
    """An empty answer is a claim about this index on this date, so it is stored as one."""
    collector = make_collector(gate, RefusingTransport(), clock)
    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(search_queries=("market",)),
    )
    stored = [s for s in gate.collected_snapshots(OWNER, CASE) if s["origin"] == ORIGIN_AHMIA]
    assert len(stored) == 1
    assert stored[0]["content"]["candidate_count"] == 0
    assert stored[0]["content"]["provider"] == SEARCH_PROVIDER_LOCAL
    notes = summary_for(report, ORIGIN_AHMIA).notes
    assert any("index is empty" in note for note in notes)


def test_a_page_in_the_case_can_answer_a_later_query(gate, clock):
    seed_snapshot(gate, excerpt="<p>mirrors for the market: " + FOUND + "</p>")
    collector = make_collector(gate, RefusingTransport(), clock)

    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(search_queries=("market",)),
    )

    assert [proposal.seed for proposal in report.proposals] == [FOUND]
    proposal = report.proposals[0]
    assert proposal.origin == ORIGIN_AHMIA
    # The citing page, not the index URI: an analyst checking a lead needs the page that
    # named the address, not the machine that repeated it.
    assert proposal.discovered_from == CITING
    assert "market" in proposal.reason


def test_the_answer_is_stored_with_the_page_behind_each_hit(gate, clock):
    seed_snapshot(gate, excerpt="mirrors for the market: " + FOUND)
    collector = make_collector(gate, RefusingTransport(), clock)
    collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(search_queries=("market",)),
    )
    stored = next(
        s for s in gate.collected_snapshots(OWNER, CASE) if s["origin"] == ORIGIN_AHMIA
    )
    candidates = stored["content"]["candidates"]
    assert [c["onion"] for c in candidates] == [FOUND]
    assert candidates[0]["source_url"] == CITING
    assert stored["source_url"].startswith("oniontrace-index://")


def test_an_address_the_case_already_declared_is_not_offered_back(gate, clock):
    gate.add_seed(CASE, DECLARED, principal=OWNER)
    seed_snapshot(gate, excerpt="both mirrors: " + DECLARED + " and " + FOUND)
    collector = make_collector(gate, RefusingTransport(), clock)

    report = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(
            onion_addresses=(DECLARED,), search_queries=("mirrors",)
        ),
        # Only the keyword path: the declared seed would otherwise be fetched by the
        # onion_service path, and this transport exists to prove nothing is fetched here.
        origins=(ORIGIN_AHMIA,),
    )

    assert [proposal.seed for proposal in report.proposals] == [FOUND]


def test_a_query_is_not_asked_twice(gate, clock):
    seed_snapshot(gate, excerpt="mirrors for the market: " + FOUND)
    collector = make_collector(gate, RefusingTransport(), clock)
    collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(search_queries=("market",)),
    )

    second = collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(search_queries=("market",)),
    )

    assert summary_for(second, ORIGIN_AHMIA).stored == 0
    assert any("not searched again" in note for note in summary_for(second, ORIGIN_AHMIA).notes)
    assert len(
        [
            s
            for s in gate.collected_snapshots(OWNER, CASE)
            if s["origin"] == ORIGIN_AHMIA
        ]
    ) == 1


def test_the_checkpoint_names_the_query_not_the_whole_scope(gate, clock):
    """Two queries in one run are two facts, so each gets its own resumable cursor."""
    seed_snapshot(gate, excerpt="mirrors for the market: " + FOUND)
    collector = make_collector(gate, RefusingTransport(), clock)
    collector.collect(
        case_id=CASE,
        principal=OWNER,
        scope=CollectionScope(search_queries=("market", "mirrors")),
    )
    from collect.checkpoints import CheckpointStore

    store = CheckpointStore(gate.cases.derived_dir(CASE), clock=clock)
    market = store.load(CASE, checkpoint_source(ORIGIN_AHMIA, "market"))
    mirrors = store.load(CASE, checkpoint_source(ORIGIN_AHMIA, "mirrors"))
    assert market is not None and market.completed
    assert mirrors is not None and mirrors.completed


def test_a_query_is_asked_again_when_the_index_has_grown(gate, clock):
    """The empty answer was true of an empty index, not forever.

    Without this, the first run marks the query done against no corpus at all and the case
    can fill up with evidence that the query will never look at again.
    """
    collector = make_collector(gate, RefusingTransport(), clock)
    scope = CollectionScope(search_queries=("market",))
    collector.collect(case_id=CASE, principal=OWNER, scope=scope)

    seed_snapshot(gate, excerpt="mirrors for the market: " + FOUND)
    second = collector.collect(case_id=CASE, principal=OWNER, scope=scope)

    assert [proposal.seed for proposal in second.proposals] == [FOUND]
    assert any(
        "asked again" in note for note in summary_for(second, ORIGIN_AHMIA).notes
    )


def test_a_query_is_not_asked_again_against_an_index_of_the_same_size(gate, clock):
    seed_snapshot(gate, excerpt="mirrors for the market: " + FOUND)
    collector = make_collector(gate, RefusingTransport(), clock)
    scope = CollectionScope(search_queries=("market",))
    collector.collect(case_id=CASE, principal=OWNER, scope=scope)

    again = collector.collect(case_id=CASE, principal=OWNER, scope=scope)

    assert summary_for(again, ORIGIN_AHMIA).stored == 0
    assert any("not searched again" in note for note in summary_for(again, ORIGIN_AHMIA).notes)


def test_the_run_report_says_which_index_answered(gate, clock):
    collector = make_collector(gate, RefusingTransport(), clock)
    scope = CollectionScope(search_queries=("market",))
    assert "index: local" in "\n".join(scope.summary())
