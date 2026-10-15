"""Tests for acquisition orchestration (WP2.3).

The names are requirement-shaped, in the style of ``test_gates.py``: if one of these
fails, some sentence in the write-up has stopped being true. The claims being tested are
the ones a reviewer cannot check by reading the code's shape -- that scope is refused
rather than trusted, that a cursor only advances after its bytes are stored, that a walk
that stops says where it stopped, and that a second run does not store the first run's
material again.

Everything here runs against ``FixtureTransport``: in-memory pages, no sockets, and a
clock the tests move by hand. Nothing in this module reaches a network, which is also what
makes the results reproducible.
"""

from __future__ import annotations

import json

import pytest

from collect.ahmia import AHMIA_HOST, extract_addresses, parse_results, search_url
from collect.checkpoints import Checkpoint, CheckpointStore, ProposalStore
from collect.clock import FrozenClock
from collect.collector import (
    EXCERPT_CHARS,
    MAX_SOURCE_CHARS,
    ORIGIN_AHMIA,
    ORIGIN_CT,
    ORIGIN_ONION,
    ORIGIN_TELEGRAM,
    CollectionPolicy,
    CollectionScope,
    Collector,
    OriginSummary,
    checkpoint_source,
    content_id,
)
from collect.ctlog import CTLOG_HOST, query_url
from collect.errors import CheckpointError, CollectionError, ScopeViolation
from collect.fixtures import ahmia_results_html, build_fixtures, preview_page_html
from collect.schedule import Budget, Politeness
from collect.telegram import PREVIEW_HOST, parse_preview, preview_url
from collect.transport import FixtureTransport, Page
from farm.generator import generate_farm
from farm.model import deterministic_onion_address
from gates.audit import AuditAction
from gates.gate import CollectionGate
from gates.roles import Principal, Role

CASE = "CASE-2026-014"

OWNER = Principal(principal_id="a.sharma", role=Role.CASE_OWNER)
ADMIN = Principal(principal_id="r.iyer", role=Role.ADMIN)

#: No host delay and generous ceilings, so a test measures the collector's decisions
#: rather than its patience. Politeness itself is exercised in ``test_schedule``-shaped
#: terms by the budget and cursor tests below, which set their own limits.
FAST = Politeness(min_host_delay_seconds=0.0)
INFRASTRUCTURE_HOSTS = {"ahmia.fi", "crt.sh", PREVIEW_HOST}


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock.at("2026-09-29T12:00:00+00:00")


@pytest.fixture
def gate(tmp_path, clock) -> CollectionGate:
    gate = CollectionGate(tmp_path / "gate", clock=clock.as_gate_clock())
    gate.acknowledge(OWNER)
    return gate


@pytest.fixture
def case(gate) -> str:
    gate.open_case(
        OWNER,
        case_id=CASE,
        title="Escrow forum operator correlation",
        authorisation_ref="AUTH-2026-0091",
        retention_days=30,
    )
    return CASE


@pytest.fixture(scope="session")
def farm():
    """The default farm. Session-scoped because it is deterministic."""
    return generate_farm()


@pytest.fixture
def fixtures(farm, clock):
    """A deterministic fixture transport with three services, one channel, three queries."""
    return build_fixtures(
        farm,
        clock=clock,
        service_limit=3,
        channel_limit=1,
        page_size=3,
        ct_limit=3,
    )


@pytest.fixture
def scope(fixtures) -> CollectionScope:
    return CollectionScope(
        onion_addresses=fixtures.onion_targets,
        search_queries=(fixtures.seed_query,),
        ct_queries=fixtures.ct_queries,
        telegram_channels=fixtures.channels,
    )


def make_collector(gate, transport, clock, *, warc_dir=None, politeness=FAST) -> Collector:
    return Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(politeness=politeness),
        warc_dir=warc_dir,
    )


def stored_snapshots(gate, principal=OWNER) -> tuple[dict, ...]:
    return gate.collected_snapshots(principal, CASE)


def summary_for(report, origin: str):
    """The row for one path. ``report.origins`` is canonical-ordered, so index is unusable."""
    return next(summary for summary in report.origins if summary.origin == origin)


def messages_for(channel: str, count: int) -> list[dict[str, object]]:
    """A channel history: ``count`` messages, ids descending from 1000 + count."""
    return [
        {
            "message_id": 1000 + count - index,
            "posted_at": f"2026-03-{index + 1:02d}T00:00:00+00:00",
            "text": f"listing {index}",
        }
        for index in range(count)
    ]


def history_pages(
    channel: str, messages: list[dict[str, object]], page_size: int
) -> dict[str, Page]:
    """``?before=`` pages for a channel, newest slice first, ending on an empty page.

    Mirrors the contract ``collect/fixtures.py`` builds its own history with: real history
    ends with a page that yields nothing, and a collector that never sees that page never
    has to prove its walk terminates.
    """
    ordered = sorted(messages, key=lambda m: int(m["message_id"]), reverse=True)
    pages: dict[str, Page] = {}
    cursor: int | None = None
    remaining = list(ordered)
    while remaining:
        page, remaining = remaining[:page_size], remaining[page_size:]
        pages[preview_url(channel, before=cursor)] = Page(
            body=preview_page_html(channel, page), headers={"Content-Type": "text/html"}
        )
        cursor = min(int(m["message_id"]) for m in page)
    pages[preview_url(channel, before=cursor)] = Page(
        body=preview_page_html(channel, []), headers={"Content-Type": "text/html"}
    )
    return pages


def telegram_transport(clock, channel: str, messages, page_size: int = 3):
    """A transport serving one channel's history and nothing else."""
    return FixtureTransport(
        clock=clock,
        hosts=(PREVIEW_HOST,),
        pages=history_pages(channel, messages, page_size),
    )


# --------------------------------------------------------------------------- #
# Scope: what a run is allowed to ask for
# --------------------------------------------------------------------------- #


def host_of(url: str) -> str:
    return url.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].lower()


def test_a_run_asks_only_about_what_its_scope_declared(gate, case, fixtures, scope, clock):
    """E2.2's target list is what the case authorised; nothing else leaves the process."""
    collector = make_collector(gate, fixtures.transport, clock)
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    permitted = set(scope.onion_addresses) | INFRASTRUCTURE_HOSTS
    assert fixtures.transport.fetched, "the run must have fetched something to be a test"
    for url in fixtures.transport.fetched:
        assert host_of(url) in permitted, url
    assert report.snapshots_stored > 0
    assert all(summary.refused_scope == 0 for summary in report.origins)


def test_a_request_outside_the_scope_is_refused_unsent(gate, case, fixtures, clock):
    """The refusal happens before the transport, so the URL never becomes a request."""
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=fixtures.onion_targets[:1])
    summary = OriginSummary(origin=ORIGIN_ONION)

    with pytest.raises(ScopeViolation) as excinfo:
        collector._fetch(
            "http://not-declared-anywhere.onion/",
            scope=scope,
            case_id=case,
            seed_label="intruder",
            summary=summary,
            budget=Budget(FAST),
        )

    assert "not in this run's declared scope" in str(excinfo.value)
    assert fixtures.transport.fetched == []
    assert summary.refused_scope == 1


def test_a_run_with_no_targets_is_refused_rather_than_reported_as_success(
    gate, case, fixtures, clock
):
    """A run that requests nothing and reports success is the worst of the options."""
    collector = make_collector(gate, fixtures.transport, clock)

    with pytest.raises(ScopeViolation) as excinfo:
        collector.collect(case_id=case, principal=OWNER, scope=CollectionScope())

    assert "no declared targets" in str(excinfo.value)
    assert fixtures.transport.fetched == []


def test_an_unknown_path_name_is_refused(gate, case, fixtures, clock):
    collector = make_collector(gate, fixtures.transport, clock)

    with pytest.raises(CollectionError) as excinfo:
        collector.collect(
            case_id=case,
            principal=OWNER,
            scope=CollectionScope(onion_addresses=fixtures.onion_targets[:1]),
            origins=("onion",),
        )

    assert "unknown origin" in str(excinfo.value)



# --------------------------------------------------------------------------- #
# Path 1: onion front pages
# --------------------------------------------------------------------------- #


def test_an_address_linked_from_an_in_scope_page_is_proposed_not_fetched(
    gate, case, fixtures, clock
):
    """Following a link would be a scope decision the collector may not make."""
    target = fixtures.onion_targets[0]
    intruder = deterministic_onion_address(20260929, "intruder")
    fixtures.transport.register(
        f"http://{target}/",
        Page(
            body=(
                "<html><head><title>Index</title></head><body>"
                f'<a href="http://{intruder}/">related service</a></body></html>'
            ),
            headers={"Content-Type": "text/html"},
        ),
    )
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=(target,))
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert intruder not in {host_of(url) for url in fixtures.transport.fetched}
    assert intruder in {proposal.seed for proposal in report.proposals}
    proposal = next(p for p in report.proposals if p.seed == intruder)
    assert proposal.origin == ORIGIN_ONION
    assert proposal.discovered_from == f"http://{target}/"
    assert report.origins[0].refused_scope == 1
    # A proposal is not a seed, and only an analyst can make it one.
    assert gate.cases.get(case).seeds == []


def test_a_service_that_stopped_answering_is_still_recorded(gate, case, fixtures, clock):
    """The date a service went dark is evidence; a 404 is not an error to be swallowed."""
    target = fixtures.onion_targets[0]
    fixtures.transport.register(f"http://{target}/", Page(body="<h1>gone</h1>", status=410))
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=(target,))
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    stored = [s for s in stored_snapshots(gate) if s["origin"] == ORIGIN_ONION]
    assert len(stored) == 1
    assert stored[0]["content"]["status"] == 410
    assert report.origins[0].transport_errors == 0


def test_an_unchanged_page_is_not_stored_twice(gate, case, fixtures, clock):
    """Changed bytes are the signal, so a static front page cannot multiply in the case."""
    target = fixtures.onion_targets[0]
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=(target,))

    first = collector.collect(case_id=case, principal=OWNER, scope=scope)
    # The revisit planner backs off after an unchanged visit, so move the clock past the
    # planned revisit to make the page due again. Otherwise the second run would be
    # skipped for scheduling reasons rather than dedupe reasons, and the test would pass
    # without testing dedupe.
    clock.advance(days=8)
    second = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert first.snapshots_stored == 1
    assert second.snapshots_stored == 0
    assert second.origins[0].skipped_duplicate == 1
    assert len(stored_snapshots(gate)) == 1


def test_changed_bytes_are_recorded_on_the_next_visit(gate, case, fixtures, clock):
    """A service that reopens with new content must be recorded, not deduped away."""
    target = fixtures.onion_targets[0]
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=(target,))

    collector.collect(case_id=case, principal=OWNER, scope=scope)
    clock.advance(days=8)
    fixtures.transport.replace(
        f"http://{target}/", Page(body="<html><title>New</title>reopened</html>")
    )
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    keys = [snapshot["dedupe_key"] for snapshot in stored_snapshots(gate)]
    assert report.snapshots_stored == 1
    assert len(keys) == 2 and len(set(keys)) == 2
    assert all(key.startswith(f"{ORIGIN_ONION}:") for key in keys)


def test_a_visit_that_is_not_due_yet_is_not_requested(gate, case, fixtures, clock):
    """Backoff is a decision not to ask, so the run should not even issue the request."""
    target = fixtures.onion_targets[0]
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=(target,))
    collector.collect(case_id=case, principal=OWNER, scope=scope)
    fetched_after_first = len(fixtures.transport.fetched)

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)
    assert len(fixtures.transport.fetched) == fetched_after_first
    assert any("not due until" in note for note in report.origins[0].notes)



# --------------------------------------------------------------------------- #
# Path 2: the public search index
# --------------------------------------------------------------------------- #


def test_search_results_become_proposals_and_are_stored_as_the_ranked_page(
    gate, case, fixtures, clock
):
    """The finding is the ranking, so the page is stored rather than only its addresses."""
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(search_queries=(fixtures.seed_query,))
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert {proposal.seed for proposal in report.proposals} == set(fixtures.onion_targets)
    assert all(proposal.origin == ORIGIN_AHMIA for proposal in report.proposals)

    stored = [s for s in stored_snapshots(gate) if s["origin"] == ORIGIN_AHMIA]
    assert len(stored) == 1
    content = stored[0]["content"]
    assert content["query"] == fixtures.seed_query
    assert content["candidate_count"] == len(fixtures.onion_targets)
    # The ranked order is preserved, so a later reader can ask what was listed and where.
    assert [c["address"] for c in content["candidates"]] == list(fixtures.onion_targets)
    assert stored[0]["source_url"] == search_url(fixtures.seed_query)
    # And none of it is a seed.
    assert gate.cases.get(case).seeds == []


def test_an_address_the_case_already_declared_is_not_proposed_back(
    gate, case, fixtures, clock
):
    """A queue of things an analyst already decided about stops being read."""
    declared = fixtures.onion_targets[0]
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(
        onion_addresses=(declared,), search_queries=(fixtures.seed_query,)
    )
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    proposed = {proposal.seed for proposal in report.proposals}
    assert declared not in proposed
    assert proposed == set(fixtures.onion_targets[1:])


def test_a_search_that_was_already_run_is_not_run_again(gate, case, fixtures, clock):
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(search_queries=(fixtures.seed_query,))
    collector.collect(case_id=case, principal=OWNER, scope=scope)
    fetched = len(fixtures.transport.fetched)

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert len(fixtures.transport.fetched) == fetched
    assert summary_for(report, ORIGIN_AHMIA).stored == 0
    assert any("not searched again" in note for note in summary_for(report, ORIGIN_AHMIA).notes)


# --------------------------------------------------------------------------- #
# Path 3: certificate transparency
# --------------------------------------------------------------------------- #


def test_only_certificates_naming_both_worlds_are_stored(gate, case, fixtures, clock):
    """A clearnet-only certificate says nothing about the hidden service being looked at."""
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(ct_queries=fixtures.ct_queries)
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    stored = [s for s in stored_snapshots(gate) if s["origin"] == ORIGIN_CT]
    assert stored, "the fixture set contains sauteed certificates"
    for snapshot in stored:
        assert snapshot["content"]["clearnet_names"], snapshot
        assert snapshot["content"]["onion"].endswith(".onion")
        assert snapshot["content"]["log_ids"], "an association names the log it came from"
    # One finding per query, not one per certificate: the log returns every row for every
    # query, and storing each row would count the same certificate several times.
    assert len(stored) == len(fixtures.ct_queries)
    assert all(s["source_url"] == query_url(s["target"]) for s in stored)
    assert summary_for(report, ORIGIN_CT).pages == len(fixtures.ct_queries)


def test_a_certificate_association_is_proposed_with_its_clearnet_names(
    gate, case, fixtures, clock
):
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(ct_queries=fixtures.ct_queries)
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    proposals = [p for p in report.proposals if p.origin == ORIGIN_CT]
    assert proposals
    assert "also names" in proposals[0].reason
    assert gate.cases.get(case).seeds == []


def test_a_sauteed_onion_that_is_already_a_seed_is_stored_but_not_proposed(
    gate, case, fixtures, clock
):
    """The certificate is still evidence; it is the *question* that is redundant."""
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(
        onion_addresses=(fixtures.onion_targets[0],), ct_queries=fixtures.ct_queries[1:2]
    )
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    stored = [s for s in stored_snapshots(gate) if s["origin"] == ORIGIN_CT]
    assert stored and stored[0]["content"]["onion"] == fixtures.onion_targets[0]
    assert [p for p in report.proposals if p.origin == ORIGIN_CT] == []



# --------------------------------------------------------------------------- #
# Path 4: telegram public preview history
# --------------------------------------------------------------------------- #

CHANNEL = "chanalpha1"


def test_the_history_walk_visits_every_page_and_stops_on_the_empty_one(clock, gate, case):
    """Termination is a property to be demonstrated, so the fixture ends with emptiness."""
    messages = messages_for(CHANNEL, 7)
    transport = telegram_transport(clock, CHANNEL, messages, page_size=3)
    collector = make_collector(gate, transport, clock)
    scope = CollectionScope(telegram_channels=(CHANNEL,))

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    # Three pages of messages, then the page that says there is no more history.
    assert transport.fetched == [
        preview_url(CHANNEL),
        preview_url(CHANNEL, before=1005),
        preview_url(CHANNEL, before=1002),
        preview_url(CHANNEL, before=1001),
    ]
    assert summary_for(report, ORIGIN_TELEGRAM).stored == len(messages)
    telegram_notes = summary_for(report, ORIGIN_TELEGRAM).notes
    assert any("history exhausted" in note for note in telegram_notes)

    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    saved = store.load(case, checkpoint_source(ORIGIN_TELEGRAM, CHANNEL))
    assert saved is not None and saved.completed is True
    assert saved.pages_fetched == 4

    keys = {s["dedupe_key"] for s in stored_snapshots(gate) if s["origin"] == ORIGIN_TELEGRAM}
    assert keys == {f"{CHANNEL}/{1000 + index}" for index in range(1, 8)}


def test_a_completed_history_is_not_walked_again(clock, gate, case):
    messages = messages_for(CHANNEL, 4)
    transport = telegram_transport(clock, CHANNEL, messages, page_size=2)
    collector = make_collector(gate, transport, clock)
    scope = CollectionScope(telegram_channels=(CHANNEL,))
    collector.collect(case_id=case, principal=OWNER, scope=scope)
    fetched = len(transport.fetched)

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert len(transport.fetched) == fetched
    assert summary_for(report, ORIGIN_TELEGRAM).stored == 0
    assert any("already walked" in note for note in summary_for(report, ORIGIN_TELEGRAM).notes)


def test_a_cursor_that_will_not_advance_stops_the_walk(clock, gate, case):
    """A server ignoring ``before`` would otherwise be asked for one page forever."""
    messages = messages_for(CHANNEL, 3)
    stuck = preview_page_html(CHANNEL, messages)
    oldest = min(int(message["message_id"]) for message in messages)
    transport = FixtureTransport(
        clock=clock,
        hosts=(PREVIEW_HOST,),
        pages={
            preview_url(CHANNEL): Page(body=stuck),
            # The same bytes served for the page *before* the oldest message: the server is
            # not paginating, which is the failure the cursor check exists to catch.
            preview_url(CHANNEL, before=oldest): Page(body=stuck),
        },
    )
    collector = make_collector(gate, transport, clock)
    scope = CollectionScope(telegram_channels=(CHANNEL,))

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert len(transport.fetched) == 2, "the walk must not re-request the same page"
    assert any(
        "cursor did not advance" in note for note in summary_for(report, ORIGIN_TELEGRAM).notes
    )
    # Nothing is recorded as complete, because nothing was proved complete.
    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    saved = store.load(case, checkpoint_source(ORIGIN_TELEGRAM, CHANNEL))
    assert saved is None or saved.completed is False


def test_the_page_budget_stops_a_walk_and_the_stored_cursor_resumes_it(clock, gate, case):
    """A budget stop mid-history must leave a cursor, or the older messages are lost."""
    messages = messages_for(CHANNEL, 9)
    transport = telegram_transport(clock, CHANNEL, messages, page_size=3)
    tight = Politeness(min_host_delay_seconds=0.0, max_pages_per_source=2)
    scope = CollectionScope(telegram_channels=(CHANNEL,))

    first = make_collector(gate, transport, clock, politeness=tight).collect(
        case_id=case, principal=OWNER, scope=scope
    )

    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    saved = store.load(case, checkpoint_source(ORIGIN_TELEGRAM, CHANNEL))
    assert saved is not None
    assert saved.completed is False, "a truncated walk must not be recorded as finished"
    assert saved.cursor == "1004", "the cursor is the oldest message actually stored"
    assert summary_for(first, ORIGIN_TELEGRAM).stored == 6
    assert any("budget" in note for note in summary_for(first, ORIGIN_TELEGRAM).notes)

    # The next run, with room to finish, resumes rather than starting over -- and the third
    # request is the cursor the stopped run left behind, not the top of the history.
    second = make_collector(gate, transport, clock).collect(
        case_id=case, principal=OWNER, scope=scope
    )

    assert transport.fetched[2] == preview_url(CHANNEL, before=1004)
    assert summary_for(second, ORIGIN_TELEGRAM).stored == 3
    finished = store.load(case, checkpoint_source(ORIGIN_TELEGRAM, CHANNEL))
    assert finished is not None and finished.completed is True




# --------------------------------------------------------------------------- #
# Budgets: politeness as a ceiling on requests, not on filing
# --------------------------------------------------------------------------- #


def test_the_budget_is_one_ceiling_for_the_whole_run(gate, case, fixtures, scope, clock):
    """A ceiling per path would be the configured ceiling four times over."""
    tight = Politeness(min_host_delay_seconds=0.0, max_pages_per_source=4)
    collector = make_collector(gate, fixtures.transport, clock, politeness=tight)

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert report.page_ceiling == 4
    assert report.budget_pages == 4
    assert len(fixtures.transport.fetched) == 4
    notes = [note for summary in report.origins for note in summary.notes]
    assert any("was not requested" in note for note in notes), notes


def test_a_request_that_crosses_the_byte_ceiling_is_the_last_request_of_the_run(
    gate, case, fixtures, scope, clock
):
    """A byte limit that still allowed requests would be a filing rule, not a limit."""
    tight = Politeness(min_host_delay_seconds=0.0, max_bytes_per_run=1)
    collector = make_collector(gate, fixtures.transport, clock, politeness=tight)

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert len(fixtures.transport.fetched) == 1
    assert report.snapshots_stored == 0
    assert report.budget_bytes > report.byte_ceiling
    notes = [note for summary in report.origins for note in summary.notes]
    assert any("budget stop" in note for note in notes), notes
    assert any("byte budget" in note for note in notes), notes


def test_the_report_prints_what_the_run_spent_against_which_ceiling(
    gate, case, fixtures, scope, clock
):
    collector = make_collector(gate, fixtures.transport, clock)
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    rows = "\n".join(report.rows())
    assert f"pages={report.budget_pages}/{report.page_ceiling}" in rows
    assert "proposals are not seeds" in rows


# --------------------------------------------------------------------------- #
# The gate's refusals, seen from the collector
# --------------------------------------------------------------------------- #


def test_a_sealed_case_blocks_every_path_and_records_why(gate, case, fixtures, scope, clock):
    """Closing a case stops collection; it does not merely hide it from a UI."""
    gate.cases.seal(case)
    collector = make_collector(gate, fixtures.transport, clock)

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert fixtures.transport.fetched == []
    assert report.snapshots_stored == 0
    assert all(summary.blocked_by_policy for summary in report.origins)
    assert all(
        any("gate refused" in note for note in summary.notes) for summary in report.origins
    )


def test_each_path_leaves_its_own_run_entry_in_the_audit_chain(
    gate, case, fixtures, scope, clock
):
    """One permit per path, because one entry saying "a run happened" explains nothing."""
    collector = make_collector(gate, fixtures.transport, clock)
    collector.collect(case_id=case, principal=OWNER, scope=scope)

    runs = [
        entry for entry in gate.audit.entries() if entry.action == AuditAction.COLLECTION_RUN
    ]
    assert len(runs) == 4
    assert all(entry.case_id == case for entry in runs)


def test_a_path_that_was_refused_stores_nothing_and_records_no_cursor(
    gate, case, fixtures, clock
):
    """A permit refused after the work would be worse than refused before it."""
    gate.cases.seal(case)
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=fixtures.onion_targets[:1])
    collector.collect(case_id=case, principal=OWNER, scope=scope)

    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    assert store.sources() == ()


# --------------------------------------------------------------------------- #
# Storage: where the cursors live, and what the WARC holds
# --------------------------------------------------------------------------- #


def test_checkpoints_live_under_the_case_and_are_deleted_with_it(
    gate, case, fixtures, scope, clock
):
    """A checkpoint that outlived a purge would describe collection that no longer exists."""
    collector = make_collector(gate, fixtures.transport, clock)
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    derived = gate.cases.derived_dir(case)
    store = CheckpointStore(derived, clock=clock)
    assert store.sources(), "a run that stored snapshots records where it stopped"
    for source in store.sources():
        assert (derived / "checkpoints" / f"{source}.json").exists()
        assert "/" not in source
    assert str(derived) in report.resume_note

    gate.purge(case, ADMIN)

    assert not (derived / "checkpoints").exists()
    assert not (derived / "proposals.json").exists()
    assert store.sources() == ()


def test_records_go_to_one_warc_per_run_and_every_snapshot_points_at_its_record(
    gate, case, fixtures, scope, clock, tmp_path
):
    warc_dir = tmp_path / "warc"
    collector = make_collector(gate, fixtures.transport, clock, warc_dir=warc_dir)
    collector.collect(case_id=case, principal=OWNER, scope=scope)

    files = sorted(warc_dir.glob("*.warc"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("WARC/1.0")
    assert case in files[0].name

    for snapshot in stored_snapshots(gate):
        assert snapshot["warc_record_id"], snapshot
        assert snapshot["warc_record_id"] in text
        assert snapshot["body_sha256"]
    # The in-memory buffer is dropped once written: the budget bounds a run, not the heap.
    assert collector.records == []


# --------------------------------------------------------------------------- #
# Whole-run properties: idempotence, and cursors arriving from storage
# --------------------------------------------------------------------------- #


def test_a_second_run_at_the_same_moment_issues_no_requests(gate, case, fixtures, scope, clock):
    """Resumability means the second run knows what the first already settled."""
    collector = make_collector(gate, fixtures.transport, clock)
    first = collector.collect(case_id=case, principal=OWNER, scope=scope)
    fetched_after_first = len(fixtures.transport.fetched)

    second = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert first.snapshots_stored > 0
    assert second.snapshots_stored == 0
    assert len(fixtures.transport.fetched) == fetched_after_first
    # Every path has to say why it did nothing, or "no requests" is indistinguishable from
    # a path that silently did not run.
    for summary in second.origins:
        assert summary.notes, summary


def test_a_front_page_that_is_due_again_is_re_read_and_stores_nothing_on_no_change(
    gate, case, fixtures, scope, clock
):
    """The two mechanisms are different: settled queries stop, monitored pages come back."""
    collector = make_collector(gate, fixtures.transport, clock)
    collector.collect(case_id=case, principal=OWNER, scope=scope)
    before = list(fixtures.transport.fetched)

    clock.advance(days=8)
    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    re_read = fixtures.transport.fetched[len(before) :]
    # In the order the scope declared them, one front page each and nothing else.
    assert re_read == [f"http://{address}/" for address in scope.onion_addresses]
    assert report.snapshots_stored == 0
    assert summary_for(report, ORIGIN_ONION).skipped_duplicate == len(scope.onion_addresses)
    # And the settled paths stayed settled: they were not re-queried just because time passed.
    assert summary_for(report, ORIGIN_AHMIA).pages == 0
    assert summary_for(report, ORIGIN_CT).pages == 0
    assert summary_for(report, ORIGIN_TELEGRAM).pages == 0



def test_the_second_run_rebuilds_its_ledger_from_the_case_not_from_memory(
    gate, case, fixtures, clock
):
    """A collector that trusted its own memory would skip what it never stored."""
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(onion_addresses=fixtures.onion_targets[:1])
    collector.collect(case_id=case, principal=OWNER, scope=scope)

    # A fresh collector, with no memory of the first run at all.
    fresh = make_collector(gate, fixtures.transport, clock)
    clock.advance(days=8)
    report = fresh.collect(case_id=case, principal=OWNER, scope=scope)

    assert report.snapshots_stored == 0
    assert summary_for(report, ORIGIN_ONION).skipped_duplicate == 1


def test_proposals_are_merged_by_seed_across_runs(gate, case, fixtures, clock):
    """A queue with three copies of one address is not a queue of undecided things."""
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(search_queries=(fixtures.seed_query,))
    collector.collect(case_id=case, principal=OWNER, scope=scope)

    store = ProposalStore(gate.cases.derived_dir(case), clock=clock)
    first = len(store.pending())

    # A different search finding the same addresses.
    second_query = "marketplace-again"
    fixtures.transport.register(
        search_url(second_query),
        Page(
            body=ahmia_results_html(
                second_query, [(a, "t", "s") for a in fixtures.onion_targets]
            ),
            headers={"Content-Type": "text/html"},
        ),
    )
    collector.collect(
        case_id=case,
        principal=OWNER,
        scope=CollectionScope(search_queries=(second_query,)),
    )

    assert len(store.pending()) == first
    assert all(item["status"] == "proposed" for item in store.pending())
    assert all(item["added_by"] is None for item in store.pending())



# --------------------------------------------------------------------------- #
# Cursor plumbing: the parts that fail silently if they are wrong
# --------------------------------------------------------------------------- #


def test_checkpoint_sources_are_safe_filenames_and_stay_distinct():
    """A flat substitution would collapse two sources onto one cursor."""
    assert checkpoint_source(ORIGIN_AHMIA, "a/b") == "public_index:a%2Fb"
    assert checkpoint_source(ORIGIN_AHMIA, "a/b") != checkpoint_source(ORIGIN_AHMIA, "a_b")
    assert checkpoint_source(ORIGIN_AHMIA, "a%2Fb") != checkpoint_source(ORIGIN_AHMIA, "a/b")

    long_query = "x" * 300
    key = checkpoint_source(ORIGIN_AHMIA, long_query)
    assert len(key) <= len(ORIGIN_AHMIA) + MAX_SOURCE_CHARS + 14
    assert key != checkpoint_source(ORIGIN_AHMIA, long_query + "y")


def test_a_search_query_with_a_slash_does_not_break_its_checkpoint(gate, case, fixtures, clock):
    """The query is someone else's input; a slash in it must not abort the run."""
    query = "escrow/forum"
    fixtures.transport.register(
        search_url(query),
        Page(body=ahmia_results_html(query, []), headers={"Content-Type": "text/html"}),
    )
    collector = make_collector(gate, fixtures.transport, clock)
    scope = CollectionScope(search_queries=(query,))

    report = collector.collect(case_id=case, principal=OWNER, scope=scope)

    assert summary_for(report, ORIGIN_AHMIA).pages == 1
    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    assert store.sources() == (checkpoint_source(ORIGIN_AHMIA, query),)
    saved = store.load(case, checkpoint_source(ORIGIN_AHMIA, query))
    # Per source, not the run total: the field is named for the source it describes.
    assert saved is not None and saved.pages_fetched == 1


def test_a_corrupt_cursor_is_refused_before_the_request(clock, gate, case):
    """Resuming from a guessed point would leave a gap nobody can see."""
    transport = telegram_transport(clock, CHANNEL, messages_for(CHANNEL, 3), page_size=3)
    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    store.save(
        Checkpoint(
            case_id=case,
            source=checkpoint_source(ORIGIN_TELEGRAM, CHANNEL),
            cursor="tomorrow",
        )
    )
    collector = make_collector(gate, transport, clock)

    with pytest.raises(CheckpointError) as excinfo:
        collector.collect(
            case_id=case,
            principal=OWNER,
            scope=CollectionScope(telegram_channels=(CHANNEL,)),
        )

    assert "message id" in str(excinfo.value)
    assert transport.fetched == []


def test_a_cursor_from_another_case_is_refused(clock, gate, case):
    """Checkpoints are not portable between cases, or a restore would deposit elsewhere."""
    source = checkpoint_source(ORIGIN_TELEGRAM, CHANNEL)
    payload = Checkpoint(case_id="CASE-2026-999", source=source).to_dict()
    folder = gate.cases.derived_dir(case) / "checkpoints"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{source}.json").write_text(json.dumps(payload), encoding="utf-8")

    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    with pytest.raises(CheckpointError) as excinfo:
        store.load(case, source)

    assert "portable" in str(excinfo.value) or "belongs to" in str(excinfo.value)


def test_a_page_title_is_extracted_and_the_excerpt_is_bounded(gate, case, clock):
    """The snapshot is readable without decompressing the WARC, and is not a second copy."""
    address = deterministic_onion_address(20260929, "long-page")
    body = "<html><head><title>Escrow list</title></head><body>" + "x" * 9000 + "</body></html>"
    transport = FixtureTransport(
        clock=clock,
        hosts=(address,),
        pages={f"http://{address}/": Page(body=body, headers={"Content-Type": "text/html"})},
    )
    collector = make_collector(gate, transport, clock)
    collector.collect(
        case_id=case, principal=OWNER, scope=CollectionScope(onion_addresses=(address,))
    )

    snapshot = next(s for s in stored_snapshots(gate) if s["origin"] == ORIGIN_ONION)
    assert snapshot["content"]["title"] == "Escrow list"
    assert len(snapshot["content"]["excerpt"]) == EXCERPT_CHARS
    assert snapshot["content"]["excerpt_truncated"] is True
    assert snapshot["content"]["byte_length"] > EXCERPT_CHARS


# --------------------------------------------------------------------------- #
# Content and page agree
# --------------------------------------------------------------------------- #


def test_the_stored_candidates_are_what_the_ahmia_parser_reads(gate, case, clock):
    """Content that disagreed with the page it came from would be unverifiable later."""
    address = deterministic_onion_address(20260929, "ranked")
    query = "marketplace"
    page_html = ahmia_results_html(
        query, [(address, "Escrow index", "listings and messages")]
    )
    transport = FixtureTransport(
        clock=clock,
        hosts=(AHMIA_HOST,),
        pages={
            search_url(query): Page(
                body=page_html, headers={"Content-Type": "text/html"}
            )
        },
    )
    collector = make_collector(gate, transport, clock)
    collector.collect(
        case_id=case,
        principal=OWNER,
        scope=CollectionScope(search_queries=(query,)),
    )

    stored = next(s for s in stored_snapshots(gate) if s["origin"] == ORIGIN_AHMIA)
    stored_addresses = [c["address"] for c in stored["content"]["candidates"]]
    assert stored_addresses == extract_addresses(page_html)
    assert stored_addresses == [
        candidate.address
        for candidate in parse_results(page_html, source_url=search_url(query))
    ]


def test_the_stored_messages_are_what_the_preview_parser_reads(clock, gate, case):
    """A preview page is a rendering, not a record; the parser is what fixes the text."""
    messages = messages_for(CHANNEL, 3)
    page_html = preview_page_html(CHANNEL, messages)
    oldest = min(int(message["message_id"]) for message in messages)
    transport = FixtureTransport(
        clock=clock,
        hosts=(PREVIEW_HOST,),
        pages={
            preview_url(CHANNEL): Page(body=page_html),
            preview_url(CHANNEL, before=oldest): Page(body=preview_page_html(CHANNEL, [])),
        },
    )
    collector = make_collector(gate, transport, clock)
    collector.collect(
        case_id=case,
        principal=OWNER,
        scope=CollectionScope(telegram_channels=(CHANNEL,)),
    )

    parsed = parse_preview(page_html, channel=CHANNEL)
    stored = [
        snapshot
        for snapshot in stored_snapshots(gate)
        if snapshot["origin"] == ORIGIN_TELEGRAM
    ]
    assert [s["content"]["text"] for s in stored] == [message.text for message in parsed]
    assert [s["content"]["message_id"] for s in stored] == [
        message.message_id for message in parsed
    ]


# --------------------------------------------------------------------------- #
# An error answer is a finding, never content and never completion
# --------------------------------------------------------------------------- #


def test_an_index_error_is_recorded_but_never_stored_as_a_ranking(gate, case, clock):
    """A 500 page is not what was ranked, so storing it would invent that fact."""
    transport = FixtureTransport(
        clock=clock,
        hosts=(AHMIA_HOST,),
        pages={search_url("marketplace"): Page(body="<h1>error</h1>", status=500)},
    )
    collector = make_collector(gate, transport, clock)
    report = collector.collect(
        case_id=case, principal=OWNER, scope=CollectionScope(search_queries=("marketplace",))
    )

    assert summary_for(report, ORIGIN_AHMIA).stored == 0
    assert any("answered 500" in note for note in summary_for(report, ORIGIN_AHMIA).notes)
    assert stored_snapshots(gate) == ()
    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    assert store.sources() == (), "an error is not a settled search"


def test_a_log_that_answers_with_html_rather_than_certificates_stops_safely(
    gate, case, clock
):
    """Rate limiters serve HTML. That must be a noted nothing, not a crash."""
    query = "svc1.example-clear.net"
    transport = FixtureTransport(
        clock=clock,
        hosts=(CTLOG_HOST,),
        pages={query_url(query): Page(body="<h1>slow down</h1>")},
    )
    collector = make_collector(gate, transport, clock)
    report = collector.collect(
        case_id=case, principal=OWNER, scope=CollectionScope(ct_queries=(query,))
    )

    assert summary_for(report, ORIGIN_CT).stored == 0
    ct_notes = summary_for(report, ORIGIN_CT).notes
    assert any("not a certificate list" in note for note in ct_notes)
    assert stored_snapshots(gate) == ()
    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    assert store.sources() == ()


def test_a_log_error_is_recorded_as_an_error_and_nothing_else(gate, case, clock):
    query = "svc1.example-clear.net"
    transport = FixtureTransport(
        clock=clock,
        hosts=(CTLOG_HOST,),
        pages={query_url(query): Page(body="<h1>down</h1>", status=503)},
    )
    collector = make_collector(gate, transport, clock)
    report = collector.collect(
        case_id=case, principal=OWNER, scope=CollectionScope(ct_queries=(query,))
    )

    assert summary_for(report, ORIGIN_CT).stored == 0
    assert any("answered 503" in note for note in summary_for(report, ORIGIN_CT).notes)
    assert stored_snapshots(gate) == ()


def test_a_preview_error_is_neither_history_nor_completion(clock, gate, case):
    """Claiming "history exhausted" over a 500 would close a walk the run never walked."""
    transport = FixtureTransport(
        clock=clock,
        hosts=(PREVIEW_HOST,),
        pages={preview_url(CHANNEL): Page(body="<h1>down</h1>", status=500)},
    )
    collector = make_collector(gate, transport, clock)
    report = collector.collect(
        case_id=case, principal=OWNER, scope=CollectionScope(telegram_channels=(CHANNEL,))
    )

    assert summary_for(report, ORIGIN_TELEGRAM).stored == 0
    notes = summary_for(report, ORIGIN_TELEGRAM).notes
    assert any("answered 500" in note for note in notes)
    assert not any("exhausted" in note for note in notes)
    assert stored_snapshots(gate) == ()
    store = CheckpointStore(gate.cases.derived_dir(case), clock=clock)
    saved = store.load(case, checkpoint_source(ORIGIN_TELEGRAM, CHANNEL))
    assert saved is None or saved.completed is False, "an error never completes a walk"


def test_a_live_guard_refuses_before_any_network_dependency():
    """A clockless live transport must refuse before importing anything network-shaped.

    Regression test for an ordering bug: the clock check sat below the network call, so a
    misconfigured transport would have fetched before refusing.
    """
    from collect.errors import TransportUnavailable
    from collect.live import LiveSettings, LiveTransport

    class ExplodingModule:
        def get(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("no network call may happen before the clock check")

    transport = LiveTransport(
        authorised_mode=True,
        settings=LiveSettings(socks_host="127.0.0.1"),
        clock=None,
    )
    transport._require_dependency = staticmethod(lambda: ExplodingModule())  # noqa: SLF001

    with pytest.raises(TransportUnavailable, match="clock"):
        transport.fetch("https://crt.sh/?q=example.com&output=json")


# --------------------------------------------------------------------------- #
# SSRF guard: a collector that parses attacker-chosen bytes must not be
# steerable into localhost, link-local or private address space.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1", "127.1.2.3", "localhost", "localhost.localdomain",
        "169.254.169.254",  # cloud instance metadata: the classic SSRF prize
        "169.254.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
        "100.64.0.1",  # carrier-grade NAT, is_private on modern Python
        "0.0.0.0", "::1", "fd00::1", "fe80::1",
        "metadata.google.internal", "anything.internal", "printer.local",
        "this-name-does-not-resolve.invalid",
    ],
)
def test_a_non_public_address_is_refused(host):
    """Every category that must never be reachable is refused, with a reason."""
    from collect.live import is_non_public_address

    blocked, why = is_non_public_address(host)
    assert blocked, f"{host} must be refused"
    assert why, "a refusal must say why"


@pytest.mark.parametrize("host", ["93.184.216.34", "crt.sh", "example.com", "ahmia.fi"])
def test_a_public_address_is_allowed(host):
    """The guard must not be a blanket block: real public hosts still pass."""
    from collect.live import is_non_public_address

    blocked, _ = is_non_public_address(host)
    assert not blocked, f"{host} is public and must be allowed"


def test_the_ssrf_guard_runs_before_the_allowance_check():
    """A private address is refused even when it is inside the declared allowance.

    This is the whole point of the guard. An operator widening ``extra_hosts`` is a
    supported configuration, and it must not be possible to turn that into a reachable
    localhost fetch by naming an address instead of a domain.
    """
    from collect.clock import FrozenClock
    from collect.errors import LiveCollectionRefused
    from collect.live import LiveSettings, LiveTransport

    transport = LiveTransport(
        authorised_mode=True,
        settings=LiveSettings(socks_host="127.0.0.1"),
        clock=FrozenClock(),
        extra_hosts=("127.0.0.1", "169.254.169.254", "localhost"),
    )
    for url in ("http://127.0.0.1/admin",
                "http://169.254.169.254/latest/meta-data/",
                "http://localhost:9200/"):
        with pytest.raises(LiveCollectionRefused, match="refus"):
            transport.check(url)


def test_an_onion_name_is_exempt_from_the_ssrf_guard():
    """An onion name is not an address: SOCKS resolves it at the proxy.

    Guarding it as if it were would refuse every onion target, which is the collector's
    whole purpose. Unresolvable clearnet names are still refused, above.
    """
    from collect.clock import FrozenClock
    from collect.live import LiveSettings, LiveTransport

    transport = LiveTransport(
        authorised_mode=True,
        settings=LiveSettings(socks_host="127.0.0.1"),
        clock=FrozenClock(),
    )
    # Must pass the guard and clear the proxy requirement without raising.
    transport.check("http://" + "a" * 56 + ".onion/")


def test_a_nat64_address_is_judged_by_the_ipv4_it_tunnels_to():
    """On a DNS64 network every IPv4 host arrives as 64:ff9b::/96.

    Two failure modes to prevent, and both were real here. Refusing the whole range
    makes legitimate IPv4-only hosts (ahmia.fi resolves to 64:ff9b::2e13:263f on this
    machine) unreachable. Allowing the whole range lets an attacker reach loopback and
    cloud metadata through a NAT64 tunnel. So the embedded IPv4 is decoded and judged.
    """
    from collect.live import is_non_public_address

    # A public IPv4-only host arrives as NAT64 and must be allowed.
    allowed, _ = is_non_public_address("64:ff9b::2e13:263f")   # 46.19.38.63
    assert not allowed, "a NAT64-tunnelled public address must be allowed"

    # The same encoding pointed at loopback, metadata and a private range must not be.
    for nat64, why in (
        ("64:ff9b::7f00:1", "loopback"),
        ("64:ff9b::a9fe:a9fe", "cloud metadata"),
        ("64:ff9b::a00:5", "private"),
    ):
        blocked, reason = is_non_public_address(nat64)
        assert blocked, f"{nat64} tunnels to {why} and must be refused"
        assert "tunnels to non-public" in reason


def test_ahmia_resolves_and_is_permitted_on_this_network():
    """The collector's own allowance host must pass its own guard.

    Guarded because a DNS64 network broke this: ahmia.fi has no AAAA record, so a
    resolver synthesises 64:ff9b::2e13:263f, which a naive reserved-range check refuses.
    """
    from collect.live import is_non_public_address

    blocked, why = is_non_public_address("ahmia.fi")
    assert not blocked, f"ahmia.fi must be fetchable, got: {why}"
