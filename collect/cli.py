"""Command-line interface for acquisition (WP2.2/WP2.3).

Usage
-----
    python -m collect targets --seed 20260925
    python -m collect run --case CASE-1 --actor a.sharma --role case_owner --fixtures
    python -m collect status --case CASE-1 --actor a.sharma --role case_owner
    python -m collect proposals --case CASE-1 --actor a.sharma --role case_owner
    python -m collect gates

Three things about this file are worth stating before reading it, because each closes a
door that would otherwise be open:

**There is no default transport.** ``run`` requires ``--fixtures`` or ``--live``. The
alternative -- picking one when the operator did not say -- means either a network request
nobody asked for or pages that look collected but were generated here. A refusal costs a
line of text; both of those costs are unbounded.

**``--live`` is not enough.** The live transport wants ``--authorised-mode`` as well, and
then wants a SOCKS host before it will touch an ``.onion`` name, and then wants its
optional dependency installed. Those refusals live in ``collect/live.py`` and are not
reimplemented here: this file passes the flag through and reports what came back. A second
copy of the guards in the CLI is a second place for them to drift.

**The scope comes from the case.** ``onion_addresses`` is the case's seed list, filtered to
onion addresses, because E2.2 puts targets in the case and nowhere else. ``--search-query``,
``--ct-query`` and ``--channel`` add the three query-shaped inputs the case has no field
for; they are run-level inputs and are printed back in the report so the run says what it
asked for.

This CLI, like ``python -m gates``, does not authenticate anyone: ``--actor``/``--role`` are
taken at face value. The *role* checks are real; the identity is not.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

from gates.environment import DEFAULT_TOR_ENDPOINT
from gates.gate import CollectionGate
from gates.roles import Permission, Principal, Role, require

from collect.ahmia import ONION_RE, search_url
from collect.index import index_for_case
from collect.checkpoints import CheckpointStore, ProposalStore
from collect.clock import FrozenClock, SystemClock
from collect.collector import (
    ORIGINS,
    SEARCH_PROVIDER_LOCAL,
    SEARCH_PROVIDERS,
    CollectionPolicy,
    CollectionScope,
    Collector,
)
from collect.errors import LiveCollectionRefused
from collect.preflight import check_environment
from collect.fixtures import build_fixtures
from collect.live import LiveSettings, LiveTransport, describe_gates
from collect.probes import PROBE_PATHS
from collect.schedule import ClockSleeper, Politeness, RecordingSleeper

DEMO_WARNING = (
    "role enforcement is real; identity is not -- this CLI takes --role at face value"
)

#: What the plan's Politeness defaults are, named here so ``run --help`` shows them.
DEFAULT_MIN_DELAY = Politeness().min_host_delay_seconds
DEFAULT_MAX_PAGES = Politeness().max_pages_per_source
DEFAULT_MAX_BYTES = Politeness().max_bytes_per_run


def _gate(args: argparse.Namespace) -> CollectionGate:
    return CollectionGate(Path(args.root))


def _principal(args: argparse.Namespace) -> Principal:
    return Principal(principal_id=args.actor, role=Role(args.role))


def _clock(args: argparse.Namespace):
    """Real time by default; ``--at`` freezes it so a demo can be reproduced exactly."""
    if getattr(args, "at", None):
        return FrozenClock.at(args.at)
    return SystemClock()


def _onion_seeds(gate: CollectionGate, case_id: str) -> tuple[str, ...]:
    """The case's declared targets, filtered to onion addresses.

    Not the other way round: this does not widen the case with addresses the collector
    found. A seed in the case is an analyst's decision; an address in a search result is a
    proposal, and the two must not be interchangeable in either direction.
    """
    seeds = gate.cases.get(case_id).seeds
    return tuple(seed for seed in seeds if ONION_RE.fullmatch(seed.strip().lower()))


def _scope_for(args: argparse.Namespace, gate: CollectionGate, case_id: str) -> CollectionScope:
    """The run's scope: the case's seeds, plus the query-shaped inputs the CLI adds."""
    onion = _onion_seeds(gate, case_id)
    return CollectionScope(
        onion_addresses=onion,
        clearnet_pages=tuple(args.clearnet_page or ()),
        directories=tuple(args.directory or ()),
        search_queries=tuple(args.search_query or ()),
        search_provider=getattr(args, "search_provider", SEARCH_PROVIDER_LOCAL),
        ct_queries=tuple(args.ct_query or ()),
        telegram_channels=tuple(args.channel or ()),
    )


def _fixture_transport(args: argparse.Namespace, clock):
    """A deterministic fixture set, built from the same generator and seed every test uses."""
    from farm.generator import generate_farm
    from farm.model import FarmConfig

    fixtures = build_fixtures(
        generate_farm(FarmConfig(seed=args.seed)),
        clock=clock,
        service_limit=args.services,
        channel_limit=args.channels,
        page_size=args.page_size,
        ct_limit=args.ct_limit,
        seed_query=args.fixture_query,
    )
    return fixtures, fixtures.transport


def _live_transport(args: argparse.Namespace, clock, case_id: str, gate: CollectionGate):
    """The live transport, with every guard left in the module that owns them."""
    settings = LiveSettings(
        socks_host=args.socks_host,
        socks_port=args.socks_port,
        timeout_seconds=args.timeout,
        max_bytes=args.max_bytes,
        user_agent=args.user_agent,
    )
    return LiveTransport(
        authorised_mode=args.authorised_mode,
        settings=settings,
        clock=clock,
        # The pages a run reads for a self-published onion are declared scope, so they
        # join the allowance the same way onion seeds do. They are still checked by the
        # SSRF guard first: being named by an operator is not the same as being safe.
        extra_hosts=_onion_seeds(gate, case_id)
        + tuple(_host_of(page) for page in (args.clearnet_page or ()))
        # A directory is a declared read like any other, and its host has to be in the
        # allowance for the walk to be possible at all.
        + tuple(_host_of(page) for page in (args.directory or ())),
    )


def cmd_targets(args: argparse.Namespace) -> int:
    """Print what a fixture set serves, so a demo's addresses are knowable in advance."""
    fixtures, transport = _fixture_transport(args, _clock(args))
    print(
        f"fixture set: seed={args.seed} services={args.services} channels={args.channels} "
        f"page_size={args.page_size} ct_limit={args.ct_limit} query={args.fixture_query!r}"
    )
    print(f"hosts served: {', '.join(transport.hosts)}")
    print("onion services -- the addresses a case would declare as seeds:")
    for address in fixtures.onion_targets:
        print(f"  {address}")
    print(
        f"search query : {fixtures.seed_query} (answered by the local index by default; "
        f"the public index would be {search_url(fixtures.seed_query)}, and it refuses)"
    )
    print(f"cert queries : {', '.join(fixtures.ct_queries)}")
    for channel in fixtures.channels:
        pages = fixtures.pages_by_channel.get(channel, 0)
        print(
            f"channel      : {channel} ({pages} preview page(s), the last one empty by design)"
        )
    print()
    print("None of these is a seed. To declare one:")
    print("  python -m gates case add-seed --case <CASE> --seed <ADDRESS> --actor <you>")
    return 0


class _EnvironmentGatedTransport:
    """The last guard before the first byte leaves the process.

    The transport's own guards answer "is this URL allowed?". This one answers a different
    question -- "is this process safe to send it from?" -- and it runs *after* them, so a
    run refused for lacking authorisation, or for having no SOCKS proxy, keeps its original
    reason instead of being reported as an environment problem it never reached.

    The result is memoised: the preflight opens a socket and sleeps on a timeout, and one
    verdict per run is the unit that makes sense. A passing verdict is not re-tested for
    every URL, and a failing one keeps failing.
    """

    def __init__(self, inner, *, report_factory) -> None:
        self._inner = inner
        self._report_factory = report_factory
        self._report = None

    @property
    def name(self) -> str:
        """Keeps the wrapped transport's name so a report says where the bytes came from."""

        return getattr(self._inner, "name", "environment-gated")

    @property
    def settings(self):
        """The wrapped transport's settings, so the permit reserve is its real timeout.

        The collector keeps a request's timeout in hand when it checks the permit
        deadline, because a page started inside the permit can finish outside it. This
        wrapper is transparent apart from the preflight; hiding the timeout here made the
        reserve a 30-second default while a live request was allowed ten minutes, so the
        guard could never fire before the permit expired.
        """

        return getattr(self._inner, "settings", None)

    def check(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._inner.check(url, headers)
        if self._report is not None and self._report.ok:
            return
        report = self._report_factory()
        self._report = report
        if not report.ok:
            lines = "; ".join(f"{c.name}: {c.detail}" for c in report.failures)
            raise LiveCollectionRefused(
                f"environment is not safe for live collection -- {lines}. "
                "Run under scripts/run_isolated.sh with Tor listening on a unix socket."
            )

    def fetch(self, url: str, headers: dict[str, str] | None = None):
        self.check(url, headers)
        return self._inner.fetch(url, headers)


def cmd_run(args: argparse.Namespace) -> int:
    gate = _gate(args)
    principal = _principal(args)
    clock = _clock(args)
    case = gate.cases.get(args.case)
    state = "purged" if case.purged else ("sealed" if case.sealed else "open")
    print(f"case {case.case_id}: {state}, owner={case.owner}, auth={case.authorisation_ref}")
    print(f"retention until {case.retention_until}")
    print(f"as {principal.principal_id} ({principal.role.value}) -- {DEMO_WARNING}")

    scope = _scope_for(args, gate, args.case)
    for line in scope.summary():
        print(f"  {line}")
    if scope.empty():
        # The collector owns this refusal; its message explains why an empty scope is not a
        # successful no-op run, and duplicating the check here would be a second copy.
        print("  (no targets: the run will refuse rather than report an empty success)")

    if args.live:
        print("transport: live (tor-socks); guards, in the order they fire:")
        print(describe_gates())
        if not args.authorised_mode:
            print(
                "  --authorised-mode was not given, so the transport will refuse at its "
                "first fetch. That refusal is the design, not a bug: live collection is off "
                "until a human says otherwise."
            )
        sleeper = ClockSleeper()
        transport = _EnvironmentGatedTransport(
            _live_transport(args, clock, args.case, gate),
            report_factory=lambda: check_environment(
                tor_endpoint=(
                    f"{args.socks_host}:{args.socks_port}"
                    if args.socks_host
                    else DEFAULT_TOR_ENDPOINT
                ),
                case_root=gate.cases.derived_dir(args.case),
            ),
        )
    else:
        sleeper = RecordingSleeper()
        fixtures, transport = _fixture_transport(args, clock)
        print(f"transport: fixtures (farm seed {args.seed}, no sockets opened)")
        stale = sorted(set(scope.onion_addresses) - set(fixtures.onion_targets))
        if stale:
            print(
                f"  {len(stale)} declared seed(s) are not in this fixture set. A fixture run "
                "exercises the paths; it does not serve the internet, and it will not invent "
                "a page to make a seed look reachable."
            )
            for address in stale:
                print(f"    {address}")

    for dataset in getattr(args, "index_import", None) or ():
        # Imported addresses are case data like anything else: they land in the case's own
        # index, inside the directory the purge deletes, and a sealed case refuses them.
        gate.cases.require_collectable(args.case)
        written, skipped = index_for_case(gate.cases, args.case).ingest_jsonl(dataset)
        print(
            f"index import {dataset}: {written} address(es) added, {skipped} line(s) skipped"
        )

    collector = Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        accept_source_policy=getattr(args, "accept_source_policy", False),
        policy=CollectionPolicy(
            politeness=Politeness(
                min_host_delay_seconds=args.min_delay,
                max_pages_per_source=args.max_pages,
                max_bytes_per_run=args.max_run_bytes,
            )
        ),
        sleeper=sleeper,
        # Inside the case, so the purge reaches the raw exchanges too. Nothing in collect/
        # writes outside a case directory, and the CLI is not the exception.
        warc_dir=gate.cases.derived_dir(args.case) / "warc",
        probe_paths=PROBE_PATHS if args.probe_known_paths else (),
    )
    report = collector.collect(
        case_id=args.case,
        principal=principal,
        scope=scope,
        origins=tuple(args.origin or ORIGINS),
    )

    print()
    for row in report.rows():
        print(row)

    for summary in report.origins:
        if summary.notes:
            print(f"\n{summary.origin}:")
            for note in summary.notes:
                print(f"  - {note}")

    if report.proposals:
        print(f"\n{len(report.proposals)} proposal(s) written to the case's queue:")
        for proposal in report.proposals:
            print(f"  {proposal.seed}")
            print(
                f"      {proposal.reason} ({proposal.origin}, from {proposal.discovered_from})"
            )
        print("  none of these is a seed; accepting one is an analyst action:")
        print("    python -m gates case add-seed --case <CASE> --seed <ADDRESS> --actor <you>")

    blocked = [summary for summary in report.origins if summary.blocked_by_policy]
    if blocked:
        print(
            f"\nrefused: {len(blocked)} of {len(report.origins)} path(s) were blocked by the "
            "gate; the refusals are in the case's audit chain"
        )
        return 1
    return 0



def cmd_status(args: argparse.Namespace) -> int:
    """Read-only: what the case holds, and where each walk stopped."""
    gate = _gate(args)
    principal = _principal(args)
    require(principal, Permission.VIEW_SNAPSHOTS)
    case = gate.cases.get(args.case)
    # Through the gate rather than the case store, so this read is a disclosure event like
    # any other and is recorded in the audit chain.
    snapshots = gate.collected_snapshots(principal, args.case)
    derived = gate.cases.derived_dir(args.case)
    clock = _clock(args)
    store = CheckpointStore(derived, clock=clock)
    proposals = ProposalStore(derived, clock=clock)

    state = "purged" if case.purged else ("sealed" if case.sealed else "open")
    print(f"case {case.case_id}: {state}, owner={case.owner}, auth={case.authorisation_ref}")
    print(f"seeds     : {len(case.seeds)}")
    for seed in case.seeds:
        print(f"  {seed}")
    print(f"snapshots : {len(snapshots)} (this read is recorded in the audit chain)")
    by_origin: dict[str, int] = {}
    for snapshot in snapshots:
        origin = str(snapshot.get("origin", "unknown"))
        by_origin[origin] = by_origin.get(origin, 0) + 1
    for origin in ORIGINS:
        if origin in by_origin:
            print(f"  {origin:<18} {by_origin[origin]}")

    print(f"cursors   : {len(store.sources())}")
    for source in store.sources():
        saved = store.load(args.case, source)
        if saved is None:
            continue
        marked = "complete" if saved.completed else "resumable"
        print(
            f"  {source:<52} {marked:<10} cursor={saved.cursor} "
            f"pages={saved.pages_fetched} at={saved.updated_at}"
        )

    print(f"proposals : {len(proposals.pending())} awaiting a decision (none of them a seed)")

    warc_files = sorted((derived / "warc").glob("*.warc"))
    print(f"WARC      : {len(warc_files)} file(s) of raw exchanges")
    for path in warc_files:
        print(f"  {path.name}  {path.stat().st_size} bytes")
    if not warc_files:
        print("  (none yet: a run writes them under the case's derived directory)")
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    """List candidates awaiting a decision. This command cannot decide one."""
    gate = _gate(args)
    principal = _principal(args)
    require(principal, Permission.VIEW_CANDIDATE_LINKS)
    gate.cases.get(args.case)
    store = ProposalStore(gate.cases.derived_dir(args.case), clock=_clock(args))
    pending = store.pending()
    if not pending:
        print(f"case {args.case}: no proposals awaiting a decision")
        return 0
    for item in pending:
        print(f"{item.get('seed')}")
        print(f"    origin     : {item.get('origin')}")
        print(f"    discovered : {item.get('discovered_from')}")
        print(f"    reason     : {item.get('reason')}")
        print(
            f"    status     : {item.get('status')} (added_by={item.get('added_by')}) "
            f"proposed_at={item.get('proposed_at')}"
        )
    print()
    print(f"{len(pending)} proposal(s). None of these is a seed, and nothing in `collect` can")
    print("make one -- E2.2 requires an analyst to add each seed explicitly:")
    print("  python -m gates case add-seed --case <CASE> --seed <ADDRESS> --actor <you>")
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    """Print the live-transport guards, without pretending they have been exercised."""
    print("live collection is off by default. The guards fire in this order:")
    print(describe_gates())
    print()
    print("Every acquisition test in this repository runs against the fixture transport,")
    print("because a suite that can reach the network by default cannot be run safely. The")
    print("byte-fetching code in `collect/live.py` is exercised by the controlled eight-service")
    print("lab (`python3 scripts/run_live_lab.py --publish`), not by this suite: the guards are")
    print("tested here, and a report must not upgrade the lab into a field result.")
    return 0


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True, help="principal identifier")
    parser.add_argument(
        "--role",
        default="analyst",
        choices=[role.value for role in Role],
        help="role the actor claims (unauthenticated in this CLI; see module docstring)",
    )


def _add_fixture_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=20260925, help="farm seed")
    parser.add_argument("--services", type=int, default=3, help="onion services to serve")
    parser.add_argument("--channels", type=int, default=1, help="telegram channels to serve")
    parser.add_argument("--page-size", type=int, default=3, help="messages per preview page")
    parser.add_argument("--ct-limit", type=int, default=3, help="certificate rows to serve")
    parser.add_argument(
        "--fixture-query",
        default="marketplace",
        help="the search term the fixture index has results for",
    )


def _add_live_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--authorised-mode",
        action="store_true",
        help="assert authorisation for a live target; the transport refuses without it",
    )
    parser.add_argument(
        "--accept-source-policy",
        action="store_true",
        help=(
            "query a source whose robots.txt disallows the path anyway, for a lawful reason "
            "a human holds. Recorded in the run's notes: the decision is never silent"
        ),
    )
    parser.add_argument("--socks-host", default=None, help="SOCKS proxy host for .onion")
    parser.add_argument("--socks-port", type=int, default=9050)
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request seconds")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=2 * 1024 * 1024,
        help="largest body kept from one live response",
    )
    parser.add_argument("--user-agent", default=LiveSettings().user_agent)


def _host_of(url: str) -> str:
    """The hostname of a page the operator named, for the live allowance."""
    return (urlsplit(url).hostname or "").lower()


def _add_scope_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--clearnet-page",
        action="append",
        default=None,
        metavar="URL",
        help=(
            "a clearnet page to read for its self-published onion address (repeatable). "
            "The finding is the pair the publisher declared, not the page; the onion is "
            "never followed from here"
        ),
    )
    parser.add_argument(
        "--directory",
        action="append",
        default=None,
        metavar="URL",
        help=(
            "a publisher page that lists many entries (a directory index or a sitemap "
            "URL), walked once, same host only, capped, and slowed to whatever its robots.txt "
            "asks for (repeatable). Every page read is evidence; every address found is a "
            "proposal, never a seed"
        ),
    )
    parser.add_argument(
        "--search-query",
        action="append",
        default=None,
        help="a search term to run (repeatable); queries are not seeds",
    )
    parser.add_argument(
        "--search-provider",
        choices=SEARCH_PROVIDERS,
        default=SEARCH_PROVIDER_LOCAL,
        help=(
            "which index answers --search-query. 'local' reads the index this project builds "
            "from pages it was allowed to collect, so it always answers and every hit names "
            "the page it came from; 'ahmia' is the public index, which refuses automation and "
            "records that refusal instead of a ranking"
        ),
    )
    parser.add_argument(
        "--index-import",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "JSONL of addresses to add to the case's own index before the run "
            "({'onion': '<56 chars>.onion', 'title': ..., 'source_url': ..., 'context': ...}). "
            "This is the door for a published dataset, and nothing is imported by default"
        ),
    )
    parser.add_argument(
        "--ct-query",
        action="append",
        default=None,
        help="a name to query in certificate transparency (repeatable)",
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=None,
        help="a public telegram channel to walk (repeatable)",
    )
    parser.add_argument(
        "--origin",
        action="append",
        choices=list(ORIGINS),
        default=None,
        help="run only these paths (repeatable; default: all four)",
    )
    parser.add_argument("--at", default=None, help="freeze the clock (ISO 8601) for a demo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collect",
        description=(
            "ONIONTRACE acquisition: four paths (onion services, public index, certificate "
            "transparency, telegram previews), one gate, one throttle. " + DEMO_WARNING
        ),
    )
    parser.add_argument("--root", default="./casework", help="gate root directory")
    sub = parser.add_subparsers(dest="command", required=True)

    targets = sub.add_parser(
        "targets", help="print what a fixture set serves, before declaring any seed"
    )
    _add_fixture_options(targets)
    targets.set_defaults(func=cmd_targets)

    run = sub.add_parser("run", help="run the acquisition paths for one case")
    run.add_argument("--case", required=True)
    _add_actor(run)
    # No default transport: see the module docstring. The refusal is argparse's.
    transport_choice = run.add_mutually_exclusive_group(required=True)
    transport_choice.add_argument(
        "--fixtures", action="store_true", help="serve deterministic fixture pages, no sockets"
    )
    transport_choice.add_argument(
        "--live", action="store_true", help="fetch over the network; off by default"
    )
    _add_scope_options(run)
    _add_fixture_options(run)
    _add_live_options(run)
    run.add_argument(
        "--min-delay",
        type=float,
        default=DEFAULT_MIN_DELAY,
        help="minimum seconds between requests to one host",
    )
    run.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="page ceiling for the run"
    )
    run.add_argument(
        "--max-run-bytes", type=int, default=DEFAULT_MAX_BYTES, help="byte ceiling for the run"
    )
    run.add_argument(
        "--probe-known-paths",
        action="store_true",
        help=(
            "after each onion seed's front page, read the fixed list of well-known "
            "misconfiguration paths in collect/probes.py (13 paths, one GET each). Off by "
            "default: it is a checklist for authorised targets, not a crawler"
        ),
    )
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="what a case holds, and where each walk stopped")
    status.add_argument("--case", required=True)
    _add_actor(status)
    status.add_argument("--at", default=None, help=argparse.SUPPRESS)
    status.set_defaults(func=cmd_status)

    proposals = sub.add_parser("proposals", help="candidates awaiting an analyst decision")
    proposals.add_argument("--case", required=True)
    _add_actor(proposals)
    proposals.add_argument("--at", default=None, help=argparse.SUPPRESS)
    proposals.set_defaults(func=cmd_proposals)

    guards = sub.add_parser(
        "gates", help="the live-collection guards, and what they do not prove"
    )
    guards.set_defaults(func=cmd_gates)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as error:  # noqa: BLE001 - the CLI reports refusals as text
        print(f"REFUSED: {error}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

