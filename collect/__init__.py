"""ONIONTRACE acquisition (WP2.2/WP2.3): four paths, one gate, one throttle.

The acquisition half of the pipeline. It fetches, parses, and hands payloads to
``gates.CollectionGate``; it does not decide what it may keep. That division is the whole
design: the gate owns authority, this package owns scope, and the only writer of case data
it can reach is ``gate.ingest``.

Structure:

  * `collector`   -- the orchestrator: four acquisition paths, budgets, checkpoints, WARC
  * `ahmia`       -- the public search index: ranked results, parsed into proposals
  * `ctlog`       -- certificate transparency: onion/clearnet associations, and their caveat
  * `telegram`    -- public channel previews: backward history walks with a message ledger
  * `transport`   -- the fetch protocol plus the fixture transport every test runs against
  * `live`        -- the live transport: off by default, gated five ways, and **unverified**
  * `schedule`    -- host delays, run budgets, revisit backoff, the sleeper
  * `checkpoints` -- resume cursors and the seed-proposal queue, both case-scoped
  * `warc`        -- WARC 1.0 records for the raw exchanges, referenced from snapshots
  * `fixtures`    -- deterministic fixture pages, generated from the farm's seed
  * `errors`      -- refusals, each naming what was refused and why
  * `cli`         -- command-line interface (`python -m collect ...`)

Importing this package opens no sockets: `live` imports its network dependency lazily,
inside the fetch that its guards have already allowed.

**What is not verified.** Every test in this repository runs against `FixtureTransport`
with in-memory pages. The parsing is exercised against markup written to resemble the real
sites, which is not the same as captured traffic, and the byte-fetching code in `live.py`
has never been executed against a network. "The guards are tested" is not "the fetcher
works", and the report must not upgrade one into the other.
"""

from __future__ import annotations

from collect.ahmia import SeedCandidate, extract_addresses, parse_results, search_url
from collect.checkpoints import Checkpoint, CheckpointStore, ProposalStore
from collect.clock import Clock, FrozenClock, SystemClock, utc_now
from collect.collector import (
    EXCERPT_CHARS,
    ORIGINS,
    ORIGIN_AHMIA,
    SEARCH_PROVIDER_AHMIA,
    SEARCH_PROVIDER_LOCAL,
    SEARCH_PROVIDERS,
    ORIGIN_CT,
    ORIGIN_ONION,
    ORIGIN_TELEGRAM,
    RETENTION_DAYS,
    CollectionPolicy,
    CollectionScope,
    Collector,
    OriginSummary,
    RunReport,
    SeedProposal,
    allowed_hosts,
    checkpoint_source,
    content_id,
    page_title,
)
from collect.ctlog import CertEntry, OnionAssociation, associations, parse_entries, query_url
from collect.errors import (
    CheckpointError,
    CollectionError,
    LiveCollectionRefused,
    PageNotFound,
    SchedulingError,
    ScopeViolation,
    TransportUnavailable,
    UnknownHost,
)
from collect.live import LiveSettings, LiveTransport, describe_gates
from collect.schedule import (
    Budget,
    ClockSleeper,
    HostThrottle,
    Politeness,
    RecordingSleeper,
    RevisitPlanner,
    Sleeper,
    VisitPlan,
)
from collect.telegram import (
    MessageLedger,
    PreviewMessage,
    oldest_id,
    parse_preview,
    preview_url,
)
from collect.transport import FetchResult, FixtureTransport, Page, Transport
from collect.warc import WarcRecord, build_record, stream, summary

__all__ = [
    "Budget",
    "CertEntry",
    "Checkpoint",
    "CheckpointError",
    "CheckpointStore",
    "Clock",
    "ClockSleeper",
    "CollectionError",
    "CollectionPolicy",
    "CollectionScope",
    "Collector",
    "EXCERPT_CHARS",
    "FetchResult",
    "FixtureTransport",
    "FrozenClock",
    "HostThrottle",
    "LiveCollectionRefused",
    "LiveSettings",
    "LiveTransport",
    "MessageLedger",
    "ORIGINS",
    "ORIGIN_AHMIA",
    "SEARCH_PROVIDER_AHMIA",
    "SEARCH_PROVIDER_LOCAL",
    "SEARCH_PROVIDERS",
    "ORIGIN_CT",
    "ORIGIN_ONION",
    "ORIGIN_TELEGRAM",
    "OnionAssociation",
    "OriginSummary",
    "Page",
    "PageNotFound",
    "Politeness",
    "PreviewMessage",
    "ProposalStore",
    "RETENTION_DAYS",
    "RecordingSleeper",
    "RevisitPlanner",
    "RunReport",
    "SchedulingError",
    "ScopeViolation",
    "SeedCandidate",
    "SeedProposal",
    "Sleeper",
    "SystemClock",
    "Transport",
    "TransportUnavailable",
    "UnknownHost",
    "VisitPlan",
    "WarcRecord",
    "allowed_hosts",
    "associations",
    "build_record",
    "checkpoint_source",
    "content_id",
    "describe_gates",
    "extract_addresses",
    "oldest_id",
    "page_title",
    "parse_entries",
    "parse_preview",
    "parse_results",
    "preview_url",
    "query_url",
    "search_url",
    "stream",
    "summary",
    "utc_now",
]

__version__ = "0.1.0"
