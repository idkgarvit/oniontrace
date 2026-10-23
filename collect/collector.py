"""The orchestrator: four acquisition paths, one gate, one throttle, one checkpoint.

Everything in this package converges here. The shape of a run follows from one rule,
stated at the top of ``README.md`` and worth restating because every decision follows it:
**the collector may fetch, and only the gate may let it keep.**

    collector fetches -> parses -> builds payloads -> gate.ingest(permit, principal, payloads)

There is no route by which fetched bytes reach case storage without passing through
``ingest``, and no route to a network without passing through ``Collector._fetch``. Both
are structural rather than conventional: ``_fetch`` is the only caller of the transport,
and it refuses an out-of-scope host before the request exists (``allowed_hosts``), charges
the host throttle before the request leaves, and is the only place that appends to the
run's WARC.

What a run does
---------------

1. Refuses, before any fetch, if the case is sealed, purged, expired, unacknowledged, or
   the principal is not its owner (the gate's doing) or if the scope declares no targets
   (ours).
2. Mints one permit per acquisition path immediately before that path fetches, so a
   permit's short lifetime is meaningful rather than decorative.
3. Fetches through the throttle, records the raw exchange to the run's WARC, parses it into
   structured findings, deduplicates against what the *case already holds*, and ingests.
4. Saves a resumable checkpoint and records any proposed seeds **after** the ingest
   returns -- proposed, never seeded.

Budgets
-------
One ``Budget`` per run, shared by every path, because ``max_bytes_per_run`` means what it
says: a ceiling per path would be the configured ceiling four times over. The page ceiling
is therefore also a run-wide one, which is stricter than the "per source" in its name --
stricter is safe here, and it is the runaway a cursor bug produces that the ceiling exists
to stop.

Division of labour, stated honestly
-----------------------------------
The gate enforces case existence, acknowledgement, role, case ownership, seal and purge
state, and retention expiry, and binds every permit to one case and one principal. The
collector enforces target scope: which addresses, channels, queries and search terms a run
may touch. Scope lives here because the gate's ``ingest`` takes payloads, not targets --
the gate decides *whether this run may store*, the collector decides *what it may ask
for*. One rule closes the gap between the two: a payload naming a case other than its
permit's case is refused at ``ingest``, so a checkpoint restored into the wrong case
cannot deposit someone else's data.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote, urlsplit

from gates.errors import GateRefused
from gates.roles import Principal

from .ahmia import AHMIA_HOST, extract_addresses, parse_results, search_url
from .onionlocation import advertisements
from .robots import PRODUCT_TOKEN, rules_for, summarise, verdict
from .checkpoints import Checkpoint, CheckpointStore, ProposalStore
from .clock import Clock
from .ctlog import CTLOG_HOST, associations, parse_entries, query_url
from .errors import (
    CheckpointError,
    CollectionError,
    ScopeViolation,
    SchedulingError,
    TransportUnavailable,
    UnknownHost,
)
from .schedule import (
    Budget,
    HostThrottle,
    Politeness,
    RecordingSleeper,
    RevisitPlanner,
    Sleeper,
    VisitPlan,
    parse_instant,
)
from .telegram import (
    PREVIEW_HOST,
    MessageLedger,
    oldest_id,
    parse_preview,
    preview_url,
)
from .transport import FetchResult, Transport
from .warc import build_record, stream as warc_stream

# -- the four kinds of thing this package can collect ------------------------ #

ORIGIN_ONION = "onion_service"
ORIGIN_ONION_LOCATION = "onion_location"
ORIGIN_AHMIA = "public_index"
ORIGIN_CT = "certificate_log"
ORIGIN_TELEGRAM = "telegram_preview"

#: Run order. Fixed, so a run's checkpoint and audit sequence are reproducible from the
#: same inputs instead of following a set's iteration order.
ORIGINS: tuple[str, ...] = (
    ORIGIN_ONION,
    ORIGIN_ONION_LOCATION,
    ORIGIN_AHMIA,
    ORIGIN_CT,
    ORIGIN_TELEGRAM,
)

#: Earliest date each kind of snapshot may be dropped, per origin. This is *not* the case
#: retention rule -- the case's own ``retention_until`` still governs when the case must
#: go. These are per-snapshot ceilings, written into each file so a reader knows how stale
#: its contents would be, which a case-level field cannot say about one message.
RETENTION_DAYS: Mapping[str, int] = {
    ORIGIN_ONION: 90,
    # A self-declared pair is a statement about a moment: operators move onions after a
    # takedown or a compromise, so an old advertisement is a lead to re-check, not a fact.
    ORIGIN_ONION_LOCATION: 90,
    ORIGIN_AHMIA: 180,
    ORIGIN_CT: 180,
    ORIGIN_TELEGRAM: 30,
}
#: How much body text is parsed into a snapshot's structured content. The full bytes live
#: in the WARC record referenced by ``warc_record_id``; the snapshot exists so an analyst
#: can read a finding without decompressing a container, and a 4000-char excerpt serves
#: that without duplicating whole sites into the case store where retention has to find them.
EXCERPT_CHARS = 4000
#: Longest escaped source key allowed in a checkpoint filename. Well under the 255-byte
#: per-component limit most filesystems impose, with room for the hash tail and the suffix.
MAX_SOURCE_CHARS = 80

REMINDER_PROPOSALS = (
    "candidates were written to the proposal queue as 'proposed'; none of them is a seed, "
    "and nothing in this package can make one -- see E2.2"
)


@dataclass(frozen=True)
class CollectionPolicy:
    """Run-level policy: politeness, plus how long each kind of finding is kept."""

    politeness: Politeness = field(default_factory=Politeness)
    #: A factory rather than the constant itself: dataclasses refuse a mutable default, and
    #: the refusal is right -- a shared dict would let one case's policy edit another's.
    retention_days: Mapping[str, int] = field(default_factory=lambda: dict(RETENTION_DAYS))

    def validated(self) -> "CollectionPolicy":
        return CollectionPolicy(
            politeness=self.politeness.validated(),
            retention_days=dict(self.retention_days),
        )

    def retention_for(self, origin: str) -> int:
        if origin not in self.retention_days:
            raise CollectionError(f"no retention rule declared for origin {origin!r}")
        return self.retention_days[origin]


@dataclass(frozen=True)
class CollectionScope:
    """What one run is allowed to ask for. Anything outside it is refused, unsent."""

    onion_addresses: tuple[str, ...] = ()
    #: Clearnet pages the operator named as worth reading for a self-published onion.
    clearnet_pages: tuple[str, ...] = ()
    search_queries: tuple[str, ...] = ()
    ct_queries: tuple[str, ...] = ()
    telegram_channels: tuple[str, ...] = ()

    def summary(self) -> tuple[str, ...]:
        return (
            f"onion targets: {len(self.onion_addresses)}",
            f"clearnet pages: {len(self.clearnet_pages)}",
            f"search queries: {len(self.search_queries)}",
            f"ct queries: {len(self.ct_queries)}",
            f"telegram channels: {len(self.telegram_channels)}",
        )

    def empty(self) -> bool:
        return not (
            self.onion_addresses
            or self.clearnet_pages
            or self.search_queries
            or self.ct_queries
            or self.telegram_channels
        )


@dataclass(frozen=True)
class SeedProposal:
    """A seed the collector found and is proposing to an analyst.

    Nothing here can make a proposal into a seed: no call to ``CaseStore.add_seed``
    exists in this package, and none should be added. E2.2 requires an analyst to add
    each seed, so the collector's authority ends at writing the proposal into its own
    checkpoint. A crawler that seeds itself turns a search result into a collection
    target with no human in the loop, and the audit would show collection happening
    without ever showing the decision that caused it.
    """

    seed: str
    origin: str
    discovered_from: str
    reason: str

    def as_record(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "origin": self.origin,
            "discovered_from": self.discovered_from,
            "reason": self.reason,
            "status": "proposed",
            "added_by": None,
        }


@dataclass
class OriginSummary:
    """What one acquisition path did, in counts a reader can check against the audit."""

    origin: str
    pages: int = 0
    fetched: int = 0
    stored: int = 0
    skipped_duplicate: int = 0
    refused_scope: int = 0
    transport_errors: int = 0
    seconds_waited: float = 0.0
    #: The gate refused this path outright, so nothing was requested. A declared field
    #: rather than an attribute set on the fly: a reader that has to ask ``hasattr`` before
    #: trusting a summary is a reader who cannot tell "not blocked" from "not reported".
    blocked_by_policy: bool = False
    notes: list[str] = field(default_factory=list)

    def as_row(self) -> str:
        state = " BLOCKED" if self.blocked_by_policy else ""
        return (
            f"{self.origin:<18} pages={self.pages:<3} fetched={self.fetched:<3} "
            f"stored={self.stored:<3} dup={self.skipped_duplicate:<3} "
            f"out-of-scope={self.refused_scope:<3} errors={self.transport_errors:<2} "
            f"waited={self.seconds_waited:.1f}s{state}"
        )


@dataclass(frozen=True)
class RunReport:
    """The outcome of one run. Printed by the CLI, asserted by the tests."""

    case_id: str
    started_at: str
    finished_at: str
    origins: tuple[OriginSummary, ...]
    snapshots_stored: int
    proposals: tuple[SeedProposal, ...]
    resume_note: str
    #: What the run spent, and against which ceiling. Reported rather than merely
    #: enforced: "the budget was respected" is only checkable if the number that got near
    #: it is printed. A run that stops at 24/24 pages looks identical in the log to one
    #: that stopped at 1/24, and the two mean different things.
    budget_pages: int = 0
    budget_bytes: int = 0
    page_ceiling: int = 0
    byte_ceiling: int = 0

    def rows(self) -> tuple[str, ...]:
        return (
            f"case {self.case_id}: {self.started_at} -> {self.finished_at}",
            self.resume_note,
        ) + tuple(origin.as_row() for origin in self.origins) + (
            f"{'TOTAL':<18} stored={self.snapshots_stored} "
            f"proposals={len(self.proposals)} (proposals are not seeds)",
            f"{'BUDGET':<18} pages={self.budget_pages}/{self.page_ceiling} "
            f"bytes={self.budget_bytes}/{self.byte_ceiling}",
        )


@dataclass
class _PathOutcome:
    """What one acquisition path produced, before any of it is committed.

    Four lists rather than side effects, because of one ordering rule: a checkpoint may
    only advance once its data is durably stored. If a path saved its cursor as it went
    and the ingest then failed -- an expired permit, say -- the next run would resume past
    the gap and the gap would be permanent and invisible. Collecting first and saving
    after ``ingest`` returns is what makes a failed run resumable rather than lossy.
    """

    summary: OriginSummary
    payloads: list[dict[str, object]] = field(default_factory=list)
    proposals: list[SeedProposal] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    visits: list[VisitPlan] = field(default_factory=list)


def content_id(body: bytes) -> str:
    """Short content fingerprint, used to decide whether a page actually changed."""
    return hashlib.sha256(body).hexdigest()[:16]


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def page_title(html: str) -> str:
    """The ``<title>`` text, whitespace-collapsed; empty string when there is none.

    A missing title is not an error -- plenty of onion services ship no title, and the
    absence is information the analyst should see rather than a fabricated "Untitled".
    """
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return " ".join(match.group(1).split())


#: Cursor encodings. A checkpoint's cursor is a free-form string, so each path documents
#: its own; decoding is kept here so a corrupt cursor fails with a message that says which
#: path and which value, rather than as a bare ValueError from deep inside a loop.

def write_visit_cursor(plan: VisitPlan) -> str:
    return json.dumps(
        {"next_due": plan.next_due, "unchanged_streak": plan.unchanged_streak},
        sort_keys=True,
    )


def read_visit_cursor(cursor: str | None) -> tuple[str | None, int]:
    """``(next_due, unchanged_streak)`` from an onion-path cursor; ``(None, 0)`` if fresh."""
    if not cursor:
        return None, 0
    try:
        payload = json.loads(cursor)
        return str(payload["next_due"]), int(payload.get("unchanged_streak", 0))
    except (ValueError, TypeError, KeyError) as error:
        raise CheckpointError(
            f"onion-path visit cursor is unreadable: {cursor!r} ({error}). Refusing to "
            "guess a revisit interval: guessing would either hammer a host that was "
            "being backed off or skip one that was due."
        ) from error


def as_cursor_int(cursor: str | None) -> int | None:
    """Telegram's cursor is a message id, or None meaning "start from the newest"."""
    if cursor in (None, ""):
        return None
    try:
        return int(cursor)
    except ValueError as error:
        raise CheckpointError(
            f"telegram-path cursor is not a message id: {cursor!r}. Refusing to resume "
            "from an unknown point in a channel's history, because the gap it describes "
            "would be invisible."
        ) from error


def checkpoint_source(origin: str, identifier: str) -> str:
    """A checkpoint key that is safe to turn into a filename.

    ``CheckpointStore`` refuses a source containing a path separator, and it is right to:
    the source becomes a filename, and here it is built from a query string or a channel
    name -- input someone else chose. Refusing at save time would be the wrong place to
    discover that, because the run would die after fetching everything and before storing
    any cursor, so the escaping happens once, at the only place sources are built.

    Percent-encoding rather than a flat substitution, because the substitution version is
    not injective: ``"a/b"`` and ``"a_b"`` would collapse onto one file, and one source's
    cursor would then resume another's walk. Truncation keeps the tail of a hash so long
    queries stay distinct instead of sharing a cursor.
    """
    safe = quote(identifier, safe="._-~")
    if len(safe) > MAX_SOURCE_CHARS:
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:MAX_SOURCE_CHARS]}-{digest}"
    return f"{origin}:{safe}"


def allowed_hosts(scope: CollectionScope) -> frozenset[str]:
    """Every host a request may name, given the declared scope.

    Two hosts are fixed infrastructure -- a certificate query goes to crt.sh, a channel
    walk to t.me. Naming them here rather than inferring them from the queries is
    deliberate: the queries are not hostnames, so a query string that happened to contain
    a host would have no way to widen the set.

    ``ahmia.fi`` is deliberately absent. It disallows automated search and enforces that
    with a redirect, so the public-index path ships with no provider rather than with a
    host that will not answer. Clearnet pages a run reads for a self-published onion are
    its own declared scope and join the set below.

    Everything else must be an address the case declared. This is the collector's half of
    the division of labour described at the top of the module: the gate decides whether the
    run may store, this decides what it may ask for.
    """
    return frozenset(
        {AHMIA_HOST, CTLOG_HOST, PREVIEW_HOST}
        | {address.lower() for address in scope.onion_addresses}
        # The pages a run reads for a self-published onion are the run's own scope, not
        # infrastructure: an operator named them, so they are as declared as a seed is.
        | {(urlsplit(page).hostname or "").lower() for page in scope.clearnet_pages}
    )


class Collector:
    """Runs the acquisition paths for one case, through one gate and one throttle."""

    def __init__(
        self,
        *,
        gate,
        transport: Transport,
        clock: Clock,
        policy: CollectionPolicy | None = None,
        sleeper: Sleeper | None = None,
        warc_dir=None,
        revisit: RevisitPlanner | None = None,
        accept_source_policy: bool = False,
    ) -> None:
        self.gate = gate
        self.policy = (policy or CollectionPolicy()).validated()
        self.records: list = []
        self._transport = transport
        self._clock = clock
        self._sleeper = sleeper or RecordingSleeper()
        self._throttle = HostThrottle(
            self.policy.politeness, clock=clock, sleeper=self._sleeper
        )
        self._warc_dir = warc_dir
        self._revisit = revisit or RevisitPlanner()
        #: robots.txt body per host, fetched at most once per run. ``None`` means the host
        #: served nothing, which is not a refusal: a host with no file has stated no policy.
        self._robots_cache: dict[str, str | None] = {}
        #: A human can accept a source whose robots policy is narrower than its published
        #: interface. The decision is recorded on every run it affects, never assumed.
        self.accept_source_policy = bool(accept_source_policy)

    # -- plumbing ---------------------------------------------------------- #

    @property
    def sleeper(self) -> Sleeper:
        return self._sleeper

    @property
    def transport_name(self) -> str:
        return getattr(self._transport, "name", "unknown")

    def _checkpoints(self, case_id: str) -> CheckpointStore:
        """Case-scoped, and inside the purge's reach -- see ``collect/checkpoints.py``."""
        return CheckpointStore(self.gate.cases.derived_dir(case_id), clock=self._clock)

    def _proposals(self, case_id: str) -> ProposalStore:
        """The proposal store for one case, inside its derived directory.

        Case-scoped for the same reason as the checkpoints: a proposal names an address or
        channel the case was watching, so it is case data and the purge has to reach it.
        The store's filename is fixed, so there is no way to leave one behind under a name
        the purge does not know to look for.
        """
        return ProposalStore(
            self.gate.cases.derived_dir(case_id),
            clock=self._clock,
        )

    def _ledger(self, case_id: str, principal: Principal) -> set[str]:
        """Dedupe keys for what the *case already holds*, read back through the gate.

        Rebuilt from stored snapshots rather than carried in memory, because the failure
        this prevents is silent and permanent: a collector trusting its own idea of what
        it had collected would skip a message it believed it stored but had not, and
        nothing downstream would ever revisit it.
        """
        ledger: set[str] = set()
        for payload in self.gate.collected_snapshots(principal, case_id):
            key = payload.get("dedupe_key")
            if isinstance(key, str) and key:
                ledger.add(key)
        return ledger

    def _fetch(
        self,
        url: str,
        *,
        scope: CollectionScope,
        case_id: str,
        seed_label: str,
        summary: OriginSummary,
        budget: Budget,
    ) -> FetchResult | None:
        """One fetch: scoped, throttled, recorded, and not raised out of for a dead host.

        Scope is checked here rather than at the call sites because there are four call
        sites and there must not be a fifth way to reach the transport. The check runs
        before the throttle records a wait, so a refused target costs nothing and appears
        nowhere in the exchange record.

        A host that is gone is a finding, not an exception -- the audit and the WARC are
        better places to learn that than a crashed run. A crossed budget is also handled
        here rather than propagated: the budget exists to stop a cursor bug from running
        forever, and a run that stops and reports is worth more than one that dies and
        loses everything it had already parsed. A scope violation is different again and
        is allowed to propagate: it means the run is asking for something the case never
        declared, and no amount of reporting makes that a legitimate request.
        """
        host = (urlsplit(url).hostname or "").lower()
        if host not in allowed_hosts(scope):
            summary.refused_scope += 1
            raise ScopeViolation(
                f"refusing to request {url}: host {host!r} is not in this run's declared "
                "scope. The target list is what the case authorised; a request outside it "
                "would be scope creep carried by a parser bug."
            )
        if budget.exhausted:
            # Named rather than assumed: "the page budget was reached" printed for a run
            # stopped by bytes would send a reader looking in the wrong place.
            if budget.pages >= budget.politeness.max_pages_per_source:
                reason = f"page budget of {budget.politeness.max_pages_per_source} reached"
            else:
                reason = (
                    f"byte budget of {budget.politeness.max_bytes_per_run} reached after "
                    f"{budget.bytes} bytes"
                )
            summary.notes.append(
                f"{budget.label}: {reason}; {url} was not requested -- the budget is a "
                "ceiling on requests, not only on what is kept"
            )
            return None
        summary.seconds_waited += self._throttle.gate(host)
        summary.pages += 1
        try:
            result = self._transport.fetch(url)
        except UnknownHost as error:
            # The fixture transport raises this for a host its set does not contain, which
            # in a demonstration is normal: a seed can outlive the fixture that served it.
            # Deliberately *not* widened to LiveCollectionRefused -- that is a policy
            # refusal, and it has to reach the operator instead of becoming a note.
            summary.transport_errors += 1
            summary.notes.append(f"{url}: not served by this transport ({error})")
            return None
        except TransportUnavailable as error:
            summary.transport_errors += 1
            summary.notes.append(f"{url}: unreachable, nothing recorded ({error})")
            return None
        summary.fetched += 1
        try:
            budget.spend(byte_length=result.byte_length)
        except SchedulingError as error:
            summary.notes.append(
                f"budget stop: {error}; this page was fetched but is not recorded, so the "
                "case holds no partial body"
            )
            return None
        self.records.append(build_record(result, case_id=case_id, seed_label=seed_label))
        return result

    def _payload(
        self,
        *,
        case_id: str,
        origin: str,
        target: str,
        dedupe_key: str,
        captured_at: str,
        source_url: str,
        content: Mapping[str, object],
    ) -> dict[str, object]:
        """The payload handed to ``gate.ingest``. Field set is fixed, for diffability."""
        record = next(
            (r for r in reversed(self.records) if r.target_uri == source_url), None
        )
        return {
            "case_id": case_id,
            "origin": origin,
            "target": target,
            "dedupe_key": dedupe_key,
            "captured_at": captured_at,
            "source_url": source_url,
            "retention_days": self.policy.retention_for(origin),
            "warc_record_id": record.record_id if record else None,
            "body_sha256": record.body_sha256 if record else None,
            "content": dict(content),
        }

    # -- path 1: onion service front pages --------------------------------- #

    def _path_onion(
        self,
        case_id: str,
        scope: CollectionScope,
        checkpoints: CheckpointStore,
        seen: set[str],
        budget: Budget | None = None,
    ) -> _PathOutcome:
        """Fetch each seed's front page. Changed bytes are the signal.

        The dedupe key carries a content fingerprint, so this path stores a snapshot when
        a page *changes* and none when it does not. The two obvious alternatives are both
        wrong: keying on the address alone means a service that reopens with new content
        is never recorded again, and storing every fetch regardless fills the case with
        identical copies of one static front page and leaves the analyst to diff them.
        """
        summary = OriginSummary(origin=ORIGIN_ONION)
        outcome = _PathOutcome(summary=summary)
        # Run-wide when ``collect`` supplies it, because ``max_bytes_per_run`` means what it
        # says: a ceiling that resets each time a path starts could be spent four times in
        # one run. A standalone call (a test, a one-path run) gets a path-scoped budget.
        budget = budget or Budget(self.policy.politeness, label=ORIGIN_ONION)
        now = self._clock.now()
        declared = set(scope.onion_addresses)
        surfaced: set[str] = set()
        for address in scope.onion_addresses:
            source = checkpoint_source(ORIGIN_ONION, address)
            prior = checkpoints.load(case_id, source)
            due, streak = read_visit_cursor(prior.cursor if prior else None)
            if due and parse_instant(due) > now:
                summary.notes.append(
                    f"{address}: not due until {due} (backoff after {streak} unchanged "
                    "visits); nothing requested"
                )
                continue
            if budget.exhausted:
                summary.notes.append(
                    "this run's budget was reached; "
                    f"{address} and any seeds after it were not requested"
                )
                break
            pages_before = budget.pages
            result = self._fetch(
                f"http://{address}/",
                scope=scope,
                case_id=case_id,
                seed_label=address,
                summary=summary,
                budget=budget,
            )
            if result is None:
                continue
            for found in extract_addresses(result.text):
                if found in declared or found in surfaced:
                    continue
                surfaced.add(found)
                summary.refused_scope += 1
                outcome.proposals.append(
                    SeedProposal(
                        seed=found,
                        origin=ORIGIN_ONION,
                        discovered_from=result.url,
                        reason="linked from an in-scope page; outside scope, so it was "
                        "proposed and not requested",
                    )
                )
            key = f"{ORIGIN_ONION}:{address}:{content_id(result.body)}"
            changed = key not in seen
            if changed:
                seen.add(key)
                outcome.payloads.append(
                    self._payload(
                        case_id=case_id,
                        origin=ORIGIN_ONION,
                        target=address,
                        dedupe_key=key,
                        captured_at=result.retrieved_at,
                        source_url=result.url,
                        content={
                            "status": result.status,
                            "title": page_title(result.text),
                            "media_type": result.header("Content-Type") or "",
                            "byte_length": result.byte_length,
                            "excerpt": result.text[:EXCERPT_CHARS],
                            "excerpt_truncated": len(result.text) > EXCERPT_CHARS,
                        },
                    )
                )
            else:
                summary.skipped_duplicate += 1
            plan = self._revisit.plan(
                target=address,
                changed=changed,
                previous=VisitPlan(
                    target=address,
                    next_due=due or now.isoformat(),
                    interval_seconds=0,
                    unchanged_streak=streak,
                ),
                now=now,
            )
            outcome.visits.append(plan)
            outcome.checkpoints.append(
                Checkpoint(
                    case_id=case_id,
                    source=source,
                    cursor=write_visit_cursor(plan),
                    pages_fetched=budget.pages - pages_before,
                    completed=True,
                )
            )
        if surfaced:
            summary.notes.append(
                f"{len(surfaced)} address(es) appeared on in-scope pages and were proposed "
                "rather than fetched: a crawler that follows links has decided its own "
                "scope, which is the decision this project keeps with an analyst"
            )
        summary.notes.append(
            "a service answering 404 or 500 is still recorded: the date it stopped "
            "answering is a fact the case should hold"
        )
        return outcome

    # -- path 2: words and self-published onion addresses ------------------- #

    def _path_onion_location(
        self,
        case_id: str,
        scope: CollectionScope,
        checkpoints: CheckpointStore,
        seen: set[str],
        budget: Budget | None = None,
    ) -> _PathOutcome:
        """Ask each declared clearnet page where its own onion is, and record the answer.

        The finding is the pair, not the page: a snapshot here says "this clearnet host
        advertised this onion, in this form, at this moment". Nothing follows the onion. A
        publisher describing itself is the source, and a tool that visits whatever it is
        told is a tool a hostile page can walk into a target.
        """
        summary = OriginSummary(origin=ORIGIN_ONION_LOCATION)
        outcome = _PathOutcome(summary=summary)
        budget = budget or Budget(self.policy.politeness, label=ORIGIN_ONION_LOCATION)
        declared = set(scope.onion_addresses)
        for page in scope.clearnet_pages:
            source = checkpoint_source(ORIGIN_ONION_LOCATION, page)
            prior = checkpoints.load(case_id, source)
            if prior and prior.completed:
                summary.notes.append(f"{page}: read at {prior.updated_at}; not re-read")
                continue
            pages_before = budget.pages
            result = self._fetch(
                page,
                scope=scope,
                case_id=case_id,
                seed_label=f"onion-location:{page}",
                summary=summary,
                budget=budget,
            )
            if result is None:
                continue
            if result.status >= 400:
                summary.notes.append(
                    f"{page}: answered {result.status}; nothing recorded and the page is "
                    "not marked as read, because an error page advertises nothing"
                )
                continue
            found = advertisements(headers=result.headers, body=result.text, source_url=page)
            summary.notes.append(f"{page}: {len(found)} onion advertisement(s)")
            for advertisement in found:
                key = (
                    f"{ORIGIN_ONION_LOCATION}:{advertisement.onion_address}:"
                    f"{advertisement.form}"
                )
                if key in seen:
                    summary.skipped_duplicate += 1
                    continue
                seen.add(key)
                outcome.payloads.append(
                    self._payload(
                        case_id=case_id,
                        origin=ORIGIN_ONION_LOCATION,
                        target=advertisement.onion_address,
                        dedupe_key=key,
                        captured_at=result.retrieved_at,
                        source_url=page,
                        content=asdict(advertisement),
                    )
                )
                if advertisement.onion_address in declared:
                    # Already a seed: the advertisement is still evidence and stays stored,
                    # but asking an analyst to approve an address they already named is what
                    # makes a review queue stop being read.
                    continue
                outcome.proposals.append(
                    SeedProposal(
                        seed=advertisement.onion_address,
                        origin=ORIGIN_ONION_LOCATION,
                        discovered_from=page,
                        reason=(
                            f"{advertisement.form} advertised by "
                            f"{urlsplit(page).hostname}: the publisher declared it itself"
                        ),
                    )
                )
            outcome.checkpoints.append(
                Checkpoint(
                    case_id=case_id,
                    source=source,
                    cursor=None,
                    pages_fetched=budget.pages - pages_before,
                    completed=True,
                )
            )
        return outcome

    # -- path 3: public search engine -------------------------------------- #

    def _robots_text(self, url: str, *, scope, case_id, summary, budget) -> str | None:
        """The host's robots file, fetched at most once per run.

        ``None`` means the host served nothing usable, and that is deliberately different
        from an empty file: a 404 is a host that has stated no policy, while a 401 or 403 is
        the standard's "this file cannot be read, assume everything is disallowed".

        A failure here is swallowed. This is context for a decision about one source, and an
        explanation that crashes the run it was written to describe is worse than none.
        """
        host = (urlsplit(url).hostname or "").lower()
        if host in self._robots_cache:
            return self._robots_cache[host]
        text: str | None = None
        # Its own tally: a robots fetch is a diagnostic, not a query, and adding it to the
        # origin's page count would make every "pages" figure in a report one too high in a
        # way a reader cannot see. The budget still pays for the request, because it is one.
        probe = OriginSummary(origin=f"{ORIGIN_CT}:robots")
        try:
            result = self._fetch(
                f"https://{host}/robots.txt",
                scope=scope,
                case_id=case_id,
                seed_label="robots",
                summary=probe,
                budget=budget,
            )
            if result is not None and result.status == 200:
                text = result.text
            elif result is not None and result.status in (401, 403):
                text = "User-agent: *\nDisallow: /"
        except Exception:  # noqa: BLE001 - see the docstring
            text = None
        self._robots_cache[host] = text
        return text

    def _robots_rules(self, url: str, *, scope, case_id, summary, budget) -> str:
        """A short quoting of the disallow rules, for a note explaining a refusal."""
        text = self._robots_text(
            url, scope=scope, case_id=case_id, summary=summary, budget=budget
        )
        if text is None:
            return ""
        rules = rules_for(text, user_agent=PRODUCT_TOKEN)
        return summarise(rules) if rules else ""

    def _path_ahmia(
        self,
        case_id: str,
        scope: CollectionScope,
        checkpoints: CheckpointStore,
        seen: set[str],
        budget: Budget | None = None,
    ) -> _PathOutcome:
        """Search-engine results. What comes out of this path is a proposal, not a seed.

        The whole results page is stored as one snapshot, because the finding is the
        ranking -- which addresses appeared, under which title, on which date. Storing the
        page rather than only the parsed addresses keeps a later question answerable: "was
        that service listed there when you looked, or did someone add it later?"
        """
        summary = OriginSummary(origin=ORIGIN_AHMIA)
        outcome = _PathOutcome(summary=summary)
        budget = budget or Budget(self.policy.politeness, label=ORIGIN_AHMIA)
        # A candidate the case already declared is not a proposal. Offering an analyst the
        # addresses they already gave the case would make the queue look longer than the
        # work in it, and the queue's only value is being the list of *undecided* things.
        declared = set(scope.onion_addresses)
        for query in scope.search_queries:
            source = checkpoint_source(ORIGIN_AHMIA, query)
            prior = checkpoints.load(case_id, source)
            if prior and prior.completed:
                summary.notes.append(
                    f"{query!r}: searched at {prior.updated_at}; not searched again"
                )
                continue
            url = search_url(query)
            pages_before = budget.pages
            result = self._fetch(
                url,
                scope=scope,
                case_id=case_id,
                seed_label=f"search:{query}",
                summary=summary,
                budget=budget,
            )
            if result is None:
                continue
            if 300 <= result.status < 400:
                # A redirect is a refusal wearing a status code. Following it would let the
                # page choose what this collector asks for next, so redirects stay off --
                # but treating the empty body as "nothing ranked" turns a refusal into a
                # negative finding, which is the one mistake this project cannot afford.
                # Header names are case-insensitive by specification and arrive in whatever
                # case the server chose, so a lookup that assumes one case silently reads
                # '' and reports a redirect to nowhere.
                headers = {name.lower(): value for name, value in result.headers.items()}
                location = headers.get("location", "")
                note = (
                    f"{query!r}: the index answered {result.status} to {location!r} instead "
                    "of a results page. Redirects are never followed, so this is recorded "
                    "as a refusal, not as a ranking with no results"
                )
                rules = self._robots_rules(
                    url, scope=scope, case_id=case_id, summary=summary, budget=budget
                )
                if rules:
                    note += f". Its robots.txt says {rules}"
                onion_location = headers.get("onion-location", "")
                if onion_location:
                    # Worth keeping: the source that refused the query still published an
                    # address for itself, which is the correlation signal this project
                    # measures, offered without being asked.
                    note += f". The refusal carried Onion-Location {onion_location!r}"
                summary.notes.append(note)
                continue
            if result.status >= 400:
                summary.notes.append(
                    f"{query!r}: the index answered {result.status}; no results page was "
                    "stored and the query is not marked as done, because an error page is "
                    "not the ranking on this date"
                )
                continue
            candidates = parse_results(result.text, source_url=url)
            summary.notes.append(f"{query!r}: {len(candidates)} candidate addresses parsed")
            key = f"{ORIGIN_AHMIA}:{query}:{content_id(result.body)}"
            if key in seen:
                summary.skipped_duplicate += 1
            else:
                seen.add(key)
                outcome.payloads.append(
                    self._payload(
                        case_id=case_id,
                        origin=ORIGIN_AHMIA,
                        target=query,
                        dedupe_key=key,
                        captured_at=result.retrieved_at,
                        source_url=url,
                        content={
                            "query": query,
                            "candidate_count": len(candidates),
                            "candidates": [asdict(candidate) for candidate in candidates],
                        },
                    )
                )
            outcome.proposals.extend(
                SeedProposal(
                    seed=candidate.address,
                    origin=ORIGIN_AHMIA,
                    discovered_from=url,
                    reason=f"ranked for query {query!r}: {candidate.title}",
                )
                for candidate in candidates
                if candidate.address not in declared
            )
            outcome.checkpoints.append(
                Checkpoint(
                    case_id=case_id,
                    source=source,
                    cursor=None,
                    pages_fetched=budget.pages - pages_before,
                    completed=True,
                )
            )
        return outcome

    # -- path 3: certificate transparency ---------------------------------- #

    def _path_ct(
        self,
        case_id: str,
        scope: CollectionScope,
        checkpoints: CheckpointStore,
        seen: set[str],
        budget: Budget | None = None,
    ) -> _PathOutcome:
        """One certificate-log query per declared name. Associations are the finding.

        Only "sauteed" certificates -- one certificate naming both an onion and a clearnet
        host -- become snapshots. A certificate naming only a clearnet domain says nothing
        about the hidden service the case is about, and storing those too would let a later
        reader mistake a pile of unrelated certificates for evidence.
        """
        summary = OriginSummary(origin=ORIGIN_CT)
        outcome = _PathOutcome(summary=summary)
        budget = budget or Budget(self.policy.politeness, label=ORIGIN_CT)
        declared = set(scope.onion_addresses)
        for query in scope.ct_queries:
            source = checkpoint_source(ORIGIN_CT, query)
            prior = checkpoints.load(case_id, source)
            if prior and prior.completed:
                summary.notes.append(f"{query}: queried at {prior.updated_at}; not re-queried")
                continue
            url = query_url(query)
            # Ask the host before asking it for data. crt.sh publishes "Disallow: /" for
            # every agent and serves JSON anyway, which is not the same situation as ahmia
            # enforcing its refusal -- but it is still the host stating a preference, so the
            # decision to query it belongs to a human and goes on the record.
            parts = urlsplit(url)
            allowed, why = verdict(
                self._robots_text(
                    url, scope=scope, case_id=case_id, summary=summary, budget=budget
                ),
                parts.path + (f"?{parts.query}" if parts.query else ""),
                user_agent=PRODUCT_TOKEN,
            )
            if not allowed:
                if not self.accept_source_policy:
                    summary.blocked_by_policy = True
                    summary.notes.append(
                        f"{parts.hostname}: {why}. Nothing was requested. "
                        "--accept-source-policy records a human decision to use the source "
                        "anyway, and repeats it in this run's notes"
                    )
                    return outcome
                summary.notes.append(
                    f"{parts.hostname}: {why}; continuing because a human accepted the "
                    "source policy for this run"
                )
            pages_before = budget.pages
            result = self._fetch(
                url,
                scope=scope,
                case_id=case_id,
                seed_label=f"ct:{query}",
                summary=summary,
                budget=budget,
            )
            if result is None:
                continue
            if result.status >= 400:
                summary.notes.append(
                    f"{query}: the log answered {result.status}; nothing was parsed and the "
                    "query is not marked as done"
                )
                continue
            try:
                entries = parse_entries(result.text, source_url=url)
            except ValueError as error:
                # A rate limiter or a proxy serving HTML is indistinguishable from a
                # malformed log, and neither should end the run: the bytes are in the WARC
                # and the failure is a fact about this attempt, not about the target.
                summary.notes.append(
                    f"{query}: the log's answer was not a certificate list ({error}); "
                    "nothing stored"
                )
                continue
            found = associations(entries)
            summary.notes.append(
                f"{query}: {len(entries)} certificates, {len(found)} naming both an onion "
                "and a clearnet host"
            )
            for association in found:
                key = f"{ORIGIN_CT}:{query}:{association.onion}"
                if key in seen:
                    summary.skipped_duplicate += 1
                    continue
                seen.add(key)
                outcome.payloads.append(
                    self._payload(
                        case_id=case_id,
                        origin=ORIGIN_CT,
                        target=query,
                        dedupe_key=key,
                        captured_at=result.retrieved_at,
                        source_url=url,
                        content=association.to_dict(),
                    )
                )
                if association.onion in declared:
                    # Already a seed: the certificate is still evidence and stays stored,
                    # but asking an analyst to decide about an address they already gave
                    # the case is what makes a review queue stop being read.
                    continue
                outcome.proposals.append(
                    SeedProposal(
                        seed=association.onion,
                        origin=ORIGIN_CT,
                        discovered_from=url,
                        reason="one certificate also names "
                        + ", ".join(association.clearnet_names),
                    )
                )
            outcome.checkpoints.append(
                Checkpoint(
                    case_id=case_id,
                    source=source,
                    cursor=None,
                    pages_fetched=budget.pages - pages_before,
                    completed=True,
                )
            )
        return outcome

    # -- path 4: telegram public preview history --------------------------- #

    def _path_telegram(
        self,
        case_id: str,
        scope: CollectionScope,
        checkpoints: CheckpointStore,
        seen: set[str],
        budget: Budget | None = None,
    ) -> _PathOutcome:
        """Walk each channel's public preview history backwards by message id.

        The pagination is the part worth reading, because it is where a collector either
        terminates or runs until it fills the disk. ``t.me/s/<channel>`` returns the newest
        slice; each older page is addressed by ``?before=<lowest id already seen>``, so the
        walk ends when a page yields no messages. Three cases get explicit handling:

        * **Empty page** -- the normal end of history, not an error, and the checkpoint is
          written ``completed`` so a later run does not walk it again.
        * **Cursor that will not advance** -- a server ignoring ``before`` would otherwise
          re-request one page forever. The walk stops with a note rather than trusting the
          cursor.
        * **Budget crossed mid-history** -- the walk is resumable, and the checkpoint is
          written ``completed=False`` with the cursor, because saying "done" here would
          silently lose everything older.
        """
        summary = OriginSummary(origin=ORIGIN_TELEGRAM)
        outcome = _PathOutcome(summary=summary)
        budget = budget or Budget(self.policy.politeness, label=ORIGIN_TELEGRAM)
        # Key shapes do not collide: telegram ids are "channel/1234" and every other path
        # prefixes its key with the origin and a colon.
        ledger = MessageLedger(tuple(seen))
        for channel in scope.telegram_channels:
            source = checkpoint_source(ORIGIN_TELEGRAM, channel)
            prior = checkpoints.load(case_id, source)
            if prior and prior.completed:
                summary.notes.append(
                    f"{channel}: history already walked to the end ({prior.updated_at})"
                )
                continue
            cursor = as_cursor_int(prior.cursor if prior else None)
            if cursor is not None:
                summary.notes.append(
                    f"{channel}: resuming the backward walk from message {cursor}"
                )
            pages_at_start = budget.pages
            while not budget.exhausted:
                url = preview_url(channel, before=cursor)
                result = self._fetch(
                    url,
                    scope=scope,
                    case_id=case_id,
                    seed_label=f"telegram:{channel}",
                    summary=summary,
                    budget=budget,
                )
                if result is None:
                    break
                messages = parse_preview(result.text, channel=channel)
                if not messages:
                    if result.status >= 400:
                        # An error page is indistinguishable from an empty one to the
                        # parser, and recording it as "history exhausted" would claim the
                        # walk reached the end of a history it never saw. No checkpoint:
                        # the next run resumes from the cursor it had.
                        summary.notes.append(
                            f"{channel}: the preview answered {result.status}; the walk "
                            "stops here with no checkpoint, because an error is not the end "
                            "of the history"
                        )
                        break
                    summary.notes.append(
                        f"{channel}: nothing older than message {cursor}; history exhausted"
                    )
                    outcome.checkpoints.append(
                        Checkpoint(
                            case_id=case_id,
                            source=source,
                            cursor=str(cursor) if cursor is not None else None,
                            pages_fetched=budget.pages - pages_at_start,
                            completed=True,
                        )
                    )
                    break
                for message in messages:
                    if not ledger.claim(message):
                        continue
                    seen.add(message.dedupe_key)
                    outcome.payloads.append(
                        self._payload(
                            case_id=case_id,
                            origin=ORIGIN_TELEGRAM,
                            target=channel,
                            dedupe_key=message.dedupe_key,
                            captured_at=result.retrieved_at,
                            source_url=url,
                            content={
                                "channel": message.channel,
                                "message_id": message.message_id,
                                "posted_at": message.posted_at,
                                "text": message.text,
                            },
                        )
                    )
                oldest = oldest_id(messages)
                if cursor is not None and oldest is not None and oldest >= cursor:
                    summary.notes.append(
                        f"{channel}: cursor did not advance ({cursor} -> {oldest}); "
                        "stopping rather than re-requesting one page indefinitely"
                    )
                    break
                cursor = oldest
            else:
                summary.notes.append(
                    f"{channel}: page budget of "
                    f"{budget.politeness.max_pages_per_source} reached mid-history; the "
                    "walk stops here and resumes from the stored cursor"
                )
                outcome.checkpoints.append(
                    Checkpoint(
                        case_id=case_id,
                        source=source,
                        cursor=str(cursor) if cursor is not None else None,
                        pages_fetched=budget.pages - pages_at_start,
                        completed=False,
                    )
                )
        summary.skipped_duplicate += ledger.duplicates
        summary.notes.append(
            "public preview only: no session, no membership, no access to messages the "
            "channel does not serve to an anonymous visitor"
        )
        return outcome

    # -- the run ----------------------------------------------------------- #

    def collect(
        self,
        *,
        case_id: str,
        principal: Principal,
        scope: CollectionScope,
        origins: Sequence[str] = ORIGINS,
    ) -> RunReport:
        """Run the requested paths and return what the case now holds.

        The order of operations is the load-bearing part. For each path: a permit is
        minted, the path runs, its payloads go through that permit, and *only after the
        ingest returns* are the checkpoints saved. A crash mid-run therefore resumes by
        re-doing work that was never recorded, never by skipping work that was. Saving a
        cursor before the ingest would be the one ordering that produces permanent
        silence, because the checkpoint is the only thing that makes the collector believe
        it has already seen a page.

        One permit per path rather than one per run, because the gate writes one
        COLLECTION_RUN audit entry per ingest and an audit trail that says "a run
        happened" when four paths were attempted has told the reviewer nothing about which
        of the four produced the snapshots.
        """
        unknown = [origin for origin in origins if origin not in ORIGINS]
        if unknown:
            raise CollectionError(
                f"unknown origin(s): {', '.join(unknown)}. The valid names are "
                f"{', '.join(ORIGINS)}."
            )
        if scope.empty():
            raise ScopeViolation(
                "this run has no declared targets. An empty scope is either a caller bug "
                "or a case with nothing to collect, and a run that requests nothing while "
                "reporting success is worth less than either: it looks like evidence."
            )
        paths = {
            ORIGIN_ONION: self._path_onion,
            ORIGIN_ONION_LOCATION: self._path_onion_location,
            ORIGIN_AHMIA: self._path_ahmia,
            ORIGIN_CT: self._path_ct,
            ORIGIN_TELEGRAM: self._path_telegram,
        }
        started = self._clock.now()
        checkpoints = self._checkpoints(case_id)
        proposals = self._proposals(case_id)
        seen = self._ledger(case_id, principal)
        # One budget for the whole run, shared by every path, because a ceiling per path
        # would be a ceiling four times the configured one and would silently contradict
        # the name of the setting that carries it.
        budget = Budget(self.policy.politeness, label=f"case {case_id}")

        summaries: list[OriginSummary] = []
        stored = 0
        collected: list[SeedProposal] = []
        # Canonical order, not the caller's order, so two runs that name the same four
        # origins in different sequences produce comparable reports and WARC records.
        for origin in [entry for entry in ORIGINS if entry in set(origins)]:
            summary = OriginSummary(origin=origin)
            # Minted before the work: the permit carries the case's policy state at the
            # moment the analyst authorised, and ingesting with it later cannot inherit a
            # state the case moved to while the crawler was out on the network.
            #
            # The refusal is caught rather than predicted. Nothing here peeks at the case's
            # state to decide whether to bother asking, because a pre-check would be a
            # second, weaker copy of the gate's own rules and the two would drift.
            try:
                permit = self.gate.authorise_collection(case_id, principal)
            except GateRefused as error:
                summary.blocked_by_policy = True
                summary.notes.append(
                    f"the gate refused this path, so nothing was requested: {error}. The "
                    "refusal is in the case's audit chain, which is where a run that should "
                    "not have started gets read"
                )
                summaries.append(summary)
                continue
            outcome = paths[origin](case_id, scope, checkpoints, seen, budget)
            summary = outcome.summary
            self.gate.ingest(permit, principal, outcome.payloads)
            summary.stored = len(outcome.payloads)
            stored += summary.stored
            # Reached only if the ingest succeeded. This is the line that makes the
            # ordering real; moving it above the ingest is the bug this whole module is
            # written to prevent.
            for cursor in outcome.checkpoints:
                checkpoints.save(cursor)
            if outcome.proposals:
                proposals.record(
                    case_id, [proposal.as_record() for proposal in outcome.proposals]
                )
                summary.notes.append(REMINDER_PROPOSALS)
            collected.extend(outcome.proposals)
            summaries.append(summary)

        if self._warc_dir is not None and self.records:
            self._write_warc(case_id=case_id, started=started)

        return RunReport(
            case_id=case_id,
            started_at=started.isoformat(),
            finished_at=self._clock.now().isoformat(),
            origins=tuple(summaries),
            snapshots_stored=stored,
            proposals=tuple(_unique_by_seed(collected)),
            resume_note=self._resume_note(case_id, checkpoints),
            budget_pages=budget.pages,
            budget_bytes=budget.bytes,
            page_ceiling=budget.politeness.max_pages_per_source,
            byte_ceiling=budget.politeness.max_bytes_per_run,
        )

    def _write_warc(self, *, case_id: str, started) -> Path:
        """Write this run's records to one WARC file, then forget them.

        Dropped after writing because ``records`` is the whole WARC in memory and the point
        of the budget is that a run's size is bounded by something other than the heap.
        The file is the artifact; this list is only the buffer that precedes it.
        """
        directory = Path(self._warc_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        target = directory / f"{case_id}-{stamp}.warc"
        target.write_text("".join(warc_stream(self.records)), encoding="utf-8", newline="")
        self.records.clear()
        return target

    def _resume_note(self, case_id: str, checkpoints: CheckpointStore) -> str:
        """Where this case's cursors live and what happens to them, spelled out.

        The CLI prints this instead of the paths, which is the whole point: the
        resumability test's pass condition is that the report *names where it stopped*,
        and "it stopped" is not a location.
        """
        saved = checkpoints.sources()
        if not saved:
            return "no cursors were saved; this run left nothing to resume"
        return (
            f"{len(saved)} cursor(s) saved under {checkpoints.root} "
            f"({', '.join(saved)}). Re-running with the same scope resumes from them; "
            "purging the case deletes them with everything else, because they name the "
            "sources this case was watching and how far back it got."
        )


def _unique_by_seed(proposals: Sequence[SeedProposal]) -> list[SeedProposal]:
    """First proposal per seed, in the order they arrived.

    Several paths can surface the same address in one run - a channel post and a
    certificate can name the same service - and the analyst should see one item, not three
    copies to work through. The proposal store dedupes on disk the same way; this keeps the
    in-memory report from contradicting it.
    """
    first: dict[str, SeedProposal] = {}
    for proposal in proposals:
        first.setdefault(proposal.seed, proposal)
    return list(first.values())

