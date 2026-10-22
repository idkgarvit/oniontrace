# ONIONTRACE — Farm, Evaluation Harness and Deployment Gates

Deterministic, seed-driven test bed for the ONIONTRACE pipeline, in four parts:

- **`farm/`** — the synthetic farm (**WP2.1**): the ground truth every metric in Part E
  depends on.
- **`collect/`** — acquisition (**WP2.2/WP2.3**): four collection paths — onion service front
  pages, a public search index, certificate transparency, public Telegram previews — behind
  signed collection permits, run budgets and resumable cursors.
- **`eval/`** — the evaluation harness (**Part G item 4**): scores the pipeline modules
  against that ground truth and writes results that carry their own reproduction command.
- **`gates/`** — the deployment gates (**Part E2**): cases, authorisation, retention,
  audit, access control and presentation rules, so the collector has to satisfy them from
  its first commit rather than being retrofitted later.

The harness is deliberately built *before* the modules it measures, and the gates before
the collector they constrain. Numbers produced after the fact cannot be defended;
numbers produced by a frozen harness can be regenerated from a seed by anyone, including a
marker.

Everything here is authored by us from benign vocabulary and is safe to redistribute.

## Scope limit — read this before quoting any number

The farm validates **rules, correlation, and robustness**. It does **not** validate
human authorship attribution.

Personas are generated with known parameters, so a model that recovers them has
recovered the generator, not a property of human writing. Every metric computed over
personas is reported as **synthetic persona retrieval** and must be labelled that way
in the report. Claims about authorship over real human text require a licensed,
author-labelled human corpus and are listed as future work in the plan.

Nothing in this package touches a live target. The farm is self-contained: it generates
service descriptions, served fixtures, corpora, and ground truth on disk. There is no
crawler in `farm/`, and no part of this repository reaches a network by default — the
acquisition package below runs against those same generated fixtures unless a human passes
`--live` **and** `--authorised-mode`, and no test does.

## Quick start

```bash
# The whole chain in one command: farm -> gates -> detect -> correlate -> attribute -> graph
python -m oniontrace run --seed 20260925 --case CASE-1
#   writes out/CASE-1-20260925/{manifest.json,REPORT.md} and casework/CASE-1/graph.db
#   each case gets its own run directory, so a second case cannot overwrite the first
#   casework/ is where the analyst console looks by default

# Check the farm is internally consistent (run this before trusting any metric)
python -m farm validate

# Write a farm to disk
python -m farm generate --seed 20260925 --out ./out

# Print composition and random baselines
python -m farm summary --seed 20260925

# Open a case (the gates own authorisation), then collect against it, offline
python -m gates --root ./casework ack --accept --actor a.sharma --role case_owner
python -m gates --root ./casework case open --case CASE-1 --title "..." \
    --authorisation AUTH-77 --retention-days 30 --actor a.sharma --role case_owner
python -m collect targets --seed 20260925              # what a fixture run can serve
python -m collect --root ./casework run --case CASE-1 --actor a.sharma \
    --role case_owner --fixtures --search-query marketplace
python -m collect --root ./casework proposals --case CASE-1 --actor a.sharma --role case_owner

# Score every module against the farm and write out/{eval-results.json,eval-report.md}
python -m eval run --seed 20260925 --out ./out

# Re-render a report from a saved results file
python -m eval report --results ./out/eval-results.json

# Run the test suite
python -m pytest tests/ -q
```

Requires Python 3.10+. No third-party runtime dependencies. Live collection needs the
optional acquisition extras (`requests` + `PySocks`) and is off until a human turns it on
with `--authorised-mode`; nothing in this repository reaches a network by default.

## What `generate` writes

| File | Contents |
|---|---|
| `out/farm.json` | Full ground truth including all generated text |
| `out/ground-truth.json` | Ground truth without text (smaller, for tooling) |
| `out/summary.json` | Composition counts and the random baselines |
| `out/report.md` | Summary table for pasting into the report |

The report table is deliberately written so the **random baseline sits next to every
metric**. Any score at or below the baseline is indistinguishable from chance, and
saying so is cheaper than being asked.

## What the farm contains

With the default config (`seed=20260925`):

| Quantity | Value | Why it is that number |
|---|---|---|
| Services | 14 | 12 clustered + 2 singletons |
| Clusters | 4 | so a top-k list is not guaranteed to contain the answer |
| Injected flaws | 25 | 19 single-scope + 6 cluster-scope, from the D2 catalogue |
| Hard negatives | 6 | stock-default artefacts planted across unrelated clusters |
| Personas | 20 | 16 subjects + 4 near-miss decoys |
| Abstention cases | 6 | 2 correlation + 4 retrieval |
| Clean services | 2 | the singletons: no flaws, no cluster-mate |
| Clean control endpoints | 42 | 3 per service, on standard paths |

### Random baselines (report these next to your results)

| Metric | Baseline | Derivation |
|---|---|---|
| Correlation precision@1 (restricted pool) | 0.1818 | 2 true mates among 11 alternatives |
| Correlation precision@1 (full pool) | 0.1538 | 2 among 13, singletons included |
| Retrieval Hit@1 (pool 20) | 0.05 | 1/20 |
| Retrieval Hit@3 (pool 20) | 0.15 | 3/20 |
| Retrieval Hit@5 (pool 20) | 0.25 | 5/20 |

The old plan's "short text R@10 > 60%" target was reachable by chance at a 16-persona
pool. These numbers exist so that cannot happen again unnoticed.

## The four mechanisms that make the metrics non-trivial

These are the parts a reviewer should look at first, because they are the parts that
stop the farm from flattering the system it measures.

### 1. Cluster artefacts vs stock defaults (correlation precision)

Fingerprint rules are paired one-to-one with a hard negative. Each cluster shares a
*genuine* value for its artefact kinds; unrelated clusters are given a *vendor-default*
value for some of the same kinds:

```
Genuine  C0: favicon_sha256=<A>, analytics_id=<B>      <- real link
Spurious C1: favicon_sha256=STOCK_DEFAULT              }  a rule keying on
         C2: favicon_sha256=STOCK_DEFAULT              }  "shared value" merges
                                                          }  these two, wrongly
```

**Coverage is partial, and the gap is reported.** Twelve of the nineteen single-scope rules
have a negative fixture served for them. Seven do not: `dot_env`, `backup_file`,
`apache_version_banner`, `nginx_version_banner`, `timezone_leak`, `etag_pattern` and
`http_header_order`. Each has a written rationale for the negative that *should* exist, but a
rationale is not a fixture — so for those seven, precision is never tested against a fixture
designed to trip the pattern without carrying the meaning.

**This qualifies the careful rule set's headline.** All nineteen rules, including the seven
unprotected ones, are scored in the careful table, so its `precision = 1.000` (19 TP / 0 FP)
is partly *unmeasured* rather than earned: for seven of the nineteen, no negative fixture
exists that could have produced a false positive. The honest reading is "no false positive was
observed, and for seven rules none could have been." Closing that gap — implementing the seven
negatives already described — is the first item of follow-up work, and until then the figure
carries the caveat wherever it is quoted.

This gap was hidden until it was attacked. `rules_without_hard_negative()` tested whether the
`hard_negative_why` prose field was populated rather than whether a fixture was served; all 25
flaws carry that field, so `farm validate` printed "rules without a hard negative: none" while
seven rules had no negative at all — reporting that every rule was protected when seven were
not. The function is now derived from the authoritative `rules_with_hard_negative_fixture()`,
and a test pins the exact list so a newly-unprotected rule cannot join it quietly.

Without the spurious half, correlation precision is measured only against services the
correlator already got right, which inflates it.

### 2. Near-miss decoys (false-merge rate)

Each decoy copies its target's **function-word fingerprint exactly** but posts in
**different hours**. So:

- a text-only stylometry model has almost no signal to separate them;
- a behavioural model can.

That asymmetry is what lets the harness answer the plan's E3 question — *do behavioural
features beat lexical features on short posts?* — with a measurement instead of an
assumption. If the system merges these pairs, the false-merge rate is real.

### 3. Template reuse across clusters (the known-hard case)

Template-reuser personas in **different clusters** publish from the same listing
template. A template-match rule links all of them, and every one of those links is
false. This reproduces the real-world case where market listings are copy-pasted vendor
templates with near-zero authorship signal.

### 4. Abstention cases (restraint)

Queries whose correct answer is *not in the candidate pool*:

- **correlation** — the singleton services. They have no cluster-mate, so any
  non-abstaining answer is a false merge;
- **retrieval** — a decoy's text scored against a subjects-only pool, so the author is
  absent by construction.

A system evaluated only on answerable queries can never be scored on restraint, and
restraint is the safety property that matters most for a tool that proposes candidate
identities.

## The evaluation harness

`python -m eval run` builds a farm, splits it by cluster, fits thresholds on dev, freezes
them, then scores every module on the held-out clusters. Each detector is implemented in
both a *naive* and a *careful* form, because a score with no comparable baseline is not a
result. The gap between the two is the measurement of whether the farm's hard negatives
are doing their job — if the careful and naive variants scored the same, the negative
fixtures would be decoration.

### Headline results (seed 20260925, farm `95fc7dacad08648c`)

| Claim | Result |
|---|---|
| M2 rule detection — careful | **100% recall, 0 false positives** |
| M2 rule detection — naive substring | 89.5% recall, 54.8% precision, 14 FP |
| M2 correlation precision@1 — naive → frequency-filtered | **0.750 → 1.000** (random baseline 0.182) |
| M2 correlation link precision — naive → filtered | **0.207 → 1.000** (false-merge 0.793 → 0.000) |
| M2 correlation at the frozen threshold | link precision **0.571** — thresholding alone does *not* fix it |
| M3 entity precision — exact → placeholder-blocklisted | **0.571 → 1.000** (decoy false merges 6/6 → 0/6) |
| M5 style drift — content vs function words | **100% → 31.2%** vs **50% → 75.0%** (a crossover) |
| E4 provenance | 83 entries verified; a planted mutation detected, then restored clean |
| Retrieval abstention | **0/4 → 4/4** correct abstentions once the frozen threshold is applied |
| M5 code-mix block (gated at mix ≥ 0.20) | Hinglish writers **0.500 → 0.750**; English and template reusers unchanged |

Two of these are worth reading closely.

**The threshold result is negative, and it is reported as such.** Freezing a score
threshold does not fix correlation link precision (0.571). Vendor-default artefacts
produce *perfect* scores, so no threshold can separate them from genuine cluster
artefacts. Only frequency filtering does (1.000). Publishing the thresholded number
without the unthresholded one would have hidden this.

**The drift result is a crossover, not a ranking.** Under drift, content words collapse
(100% → 31.2%) while function words hold (50% → 75.0%). Reported one-sided, "function
words are drift-robust" would be indistinguishable from "function words are always
weak" — which is why the pre-drift column is kept as a control and
`test_drift_separates_content_words_from_function_words` asserts *both* directions.

### Two measurement defects found by attacking the harness

The measurement layer is the part a reader is asked to trust, so both of these were found
by trying to make it report something flattering rather than by reading it. Both are fixed
and pinned, and neither changed a number currently quoted — they made the numbers wrong the
moment a variant abstained.

**Abstention used to hide in the denominator.** `precision_at_1` skipped empty rankings
(`if not ranked: continue`), so a query the system declined was dropped from `n` instead of
counted as a miss. A system that abstained on four hard queries and answered one easy one
reported **1.00 over n=1**. That is the opposite of what this project claims about restraint,
and it made `n` depend on the system's own behaviour, so two variants were no longer
comparable over the same queries. The metric now splits the two cases E1 distinguishes: a
declined query *with* a right answer stays in the denominator as a miss (`abstained`), and a
query with *no* right answer is skipped and counted separately (`skipped_no_answer`), because
restraint there is what `score_abstention` measures.

**The correlation baseline described a pool the run did not use.** The harness ranked against
the full 13-candidate pool (singletons included) but reported the *restricted* baseline,
2/11 = 0.1818. The correct figure for the pool actually ranked is 2/13 = **0.1538**, which is
now what the table shows; the restricted value is carried as a separate named field and the
baseline's own note says which pool it belongs to. A baseline computed over a pool the run
did not use is worse than no baseline, because it looks like a control while describing a
different experiment.

## Acquisition (WP2.2/WP2.3)

Four paths, one gate, one throttle. Nothing here keeps what it fetches: bytes are fetched,
recorded to a WARC, parsed into structured findings, and handed to `gates.CollectionGate`,
which is the only writer of case data this package can reach.

| Path | What it fetches | What becomes a snapshot | What it refuses to infer |
|---|---|---|---|
| `onion_service` | each declared seed's front page | one snapshot per **changed** page, keyed by a content fingerprint | that an unchanged page is worth storing again, or that a page which stopped answering should be dropped — the date it went dark is evidence |
| `public_index` | the results page for a declared search term | the whole ranked page, once | that a search result is a target: results become **proposals** |
| `certificate_log` | crt.sh for each declared name | only "sauteed" certificates naming both an onion **and** a clearnet host | that a clearnet-only certificate says anything about a hidden service |
| `telegram_preview` | `t.me/s/<channel>` history, walked backwards | public preview messages, once each | that a preview is the channel: no session, no membership, no locked posts |

```bash
python -m collect targets --seed 20260925                  # what a fixture run can serve
python -m collect --root ./casework run --case CASE-1 --actor a.sharma \
    --role case_owner --fixtures --search-query marketplace --channel chanuser_5d294b4e
python -m collect --root ./casework status --case CASE-1 --actor a.sharma --role case_owner
python -m collect --root ./casework proposals --case CASE-1 --actor a.sharma --role case_owner
python -m collect gates                                    # the live guards, and their limits
```

The demonstration loop is the point: a search returns candidate addresses, the collector
writes them to the case's **proposal** queue, and nothing in `collect/` can promote one.
The analyst decides, with `python -m gates case add-seed`, and the *next* run fetches the
front page of the address they accepted. `test_a_seed_an_analyst_added_is_what_the_next_run_fetches`
and `test_the_cli_offers_no_command_that_turns_a_proposal_into_a_seed` are the two halves of
that claim.

### What is enforced, and how

| Requirement | Mechanism, not just a policy |
|---|---|
| **No case, no collection** (E2.2) | One permit per path, minted immediately before that path fetches and presented to `ingest`; an expired or reassigned permit refuses. A sealed or purged case blocks all four paths and the run exits non-zero. |
| **Scope** (E2.2) | `allowed_hosts` refuses a host outside the declared scope *before the request exists*, so the URL never leaves the process. The scope's onion addresses are the case's seed list — the collector cannot widen its own target set. |
| **A proposal is not a seed** (E2.2) | No `add_seed` call exists anywhere in `collect/`, the proposal store has no method that could promote one, and a test enumerates the CLI's commands to show the route is absent rather than merely unused. |
| **Resumability** (WP2.3) | Cursors advance only *after* `ingest` returns, so a crash re-does unrecorded work instead of skipping recorded work. A walk that stops says where: the checkpoint records `completed=False` and the cursor, and the report prints the path it was saved under. |
| **Politeness** (WP2.3) | One `Budget` per run: a crossed ceiling stops *requests*, not just recording, and the report prints pages/bytes against the ceiling that bound it. Every fetch passes `HostThrottle`, including the first. |
| **Revisit policy** | A monitored front page is re-read on a geometric backoff and stored only when its bytes change; a settled query (search, certificate, completed history) is not repeated at all. Both decisions appear as notes in the run report. |
| **Retention reaches everything** | The WARC, the cursors and the proposal queue are all written under `<root>/cases/<case>/derived/`, so the gate's purge deletes them; a test asserts nothing this package writes escapes a case directory. |
| **Honest reporting** | Every path reports `pages`, `fetched`, `stored`, `dup`, `out-of-scope`, `errors` and prose notes; a blocked path is marked `BLOCKED` rather than looking like an empty one. A 4xx/5xx answer is recorded as a finding, never parsed as content and never mistaken for "history exhausted". |

**What is verified, and what is not.** Every acquisition test runs against
`FixtureTransport`: in-memory pages generated from the farm's seed, no sockets, and a clock
the tests move by hand. So the parsers pass against markup *written to resemble* the real
sites — not captured traffic — and the byte-fetching code in `collect/live.py` has never been
executed against a network, because this environment has no Tor circuit. The guards are
tested; the fetcher is not. `python -m collect gates` prints the same sentence, so a demo
cannot quietly upgrade one into the other.

Two more refusals worth naming. `--live` alone changes nothing: the transport wants
`--authorised-mode`, then a SOCKS host before it will touch an `.onion` name, then its
optional dependency installed by a human. And `run` has **no default transport** — it
requires `--fixtures` or `--live`, because picking one silently means either a network
request nobody asked for or pages that were generated here. Those refusals live in
`collect/live.py` and in the parser; the CLI only reports them, so there is no second copy
to drift.


## The deployment gates (Part E2)

The plan puts these early rather than last (Part G item 6): they are the difference between
a tool that can be handed over and a liability, and retrofitting them after the modules
exist costs more than building them in. They were written before the acquisition package
existed, so the seam the collector would have to satisfy was defined first — and the
collector above now satisfies it: it cannot store a byte without a permit, an unacknowledged
case, or a principal who owns the case.

```bash
# Read the statement, then record acceptance of it
python -m gates acceptable-use
python -m gates ack --accept --actor a.sharma --role case_owner

# A case needs an owner and an authorisation reference before anything else happens
python -m gates case open --case CASE-1 --title "..." --authorisation AUTH-77 \
    --retention-days 30 --actor a.sharma --role case_owner
python -m gates case add-seed --case CASE-1 --seed http://example.onion \
    --actor a.sharma --role case_owner

# Mint a permit, collect against it, then read the record
python -m gates authorise --case CASE-1 --actor a.sharma --role case_owner
python -m gates ingest --case CASE-1 --actor a.sharma --role case_owner
python -m gates audit verify
python -m gates audit tail --case CASE-1 --actor root --role admin

# The demo: every refusal, in order
python -m gates demo
```

### What is enforced, and how

| Requirement | Mechanism, not just a policy |
|---|---|
| **No case, no collection** (E2.2) | The collector needs a `CollectionPermit`: HMAC-signed over case id, principal, expiry and a nonce. A forged, edited, reassigned or expired permit is refused, and each is a separate test. |
| **Per-case seeds, no global list** (E2.2) | Seeds live under the case directory. There is no module-level seed list to inherit targets from. |
| **Retention** (E2.2) | Expiry set at creation; an expired case refuses collection; `sweep_expired` purges and logs. Refusing an unlimited case is deliberate — it is a case nobody decided to end. |
| **Purge-on-close** (E2.2) | Snapshots and derived data are deleted, the purge is logged, and a tombstone keeps the authorisation reference. Once the bytes are gone, that record is the only evidence the collection was authorised. |
| **Append-only audit** (E2.3) | Hash-chained JSON Lines, **plus an anchor** (`audit.anchor.json`) recording the expected entry count and head hash. A chain alone proves the entries *present* are consistent and cannot see a removed suffix; the anchor makes completeness checkable, so an edit, a reorder, a mid-trail removal **or a truncation from the end** all break verification. A malformed line is *reported* as damage, never raised. No public write method exists — the write path is underscore-private, gate-only, and **fails closed**: it verifies the trail before extending it, so a damaged trail cannot be appended to and its anchor cannot be laundered by a later action. |
| **Audit separate from case data** (E2.3) | Different directory tree. A purge cannot reach the record of the purge, which is what makes "deleted, and logged that we did" checkable. |
| **Reversible merges** (E2.3) | Reversal deletes the decision, restores the unconfirmed state, and requires a reason. A refused reversal must not half-apply. |
| **Roles** (E2.4) | Candidate links at analyst and above; export at case owner; purge at admin. Reading the audit is its own permission, because reading the trail is itself a disclosure. |
| **Case scoping** (E2.4) | Holding `case_owner` does not confer authority over someone else's case, or the role would just mean "trusted person". |
| **Never assert identity** (E2.5) | A regex lint on every rendered string and every evidence pack. It bans `match`, `same person` and `identified` in all their forms and *raises* rather than warns. |
| **Watermarked exports** (E2.4) | Case, analyst, timestamp and a retrieval reference derived from the audit entry's own sequence number, at both ends of the pack. |
| **Acceptable use** (E2.6) | `ACCEPTABLE_USE.md` shipped, surfaced on first run, acknowledged once per deployment with the actor and the version digest recorded. |
| **Non-capabilities** (E2.7) | A test parses every module in `gates/` and asserts no import of `socket`, `requests`, `urllib`, `http`, `aiohttp`, `subprocess` or similar. "We would not do that" is a policy; "the code cannot" is a test. |

The legal position is quoted verbatim from the plan in `ACCEPTABLE_USE.md`, including the
sentence the plan calls load-bearing:

> ONIONTRACE does not assert that collection alone creates lawful authority to investigate,
> surveil, or identify any person.

**Where the enforcement stops, stated plainly.** The gate decides what an *authenticated*
principal may do; it never establishes who they are. `Principal` is handed in from outside,
and nothing in the package reads a role from a flag, a file, or the environment. The bundled
CLI has **no authentication at all**, so it is a demonstration path: `--role` is taken at
face value. It shows what the policy does and proves nothing about who is asking. In a
deployment, the principal comes from SSO or equivalent, and the gate's checks are only as
good as that identity.

Likewise the audit trail is tamper *evident*, not tamper *proof*. The anchor closes the
cheap edit — deleting your own last few entries, or deleting the log outright, now fails
verification instead of reading as clean — but it is a second file in the same tree, so
anyone who can rewrite *both* the log and its anchor can still recompute a consistent pair.
Proving absence of tampering needs each head hash shipped somewhere the operator cannot
write, which this tool does not have. `verify` reports `anchored: False` rather than a clean
bill when there is no anchor to check against, so a chain-only pass cannot be mistaken for a
complete one.

## Layout

```
farm/
  model.py       data model; deterministic helpers; baseline computation
  catalogue.py   the 25 D2 flaws, each with its hard-negative rationale
  language.py    vocabularies, sentence frames, listing templates
  personas.py    persona construction, corpus generation, style drift
  services.py    service content and flaw-injection fixtures
  generator.py   orchestration; generate_farm() is the entry point
  cli.py         command-line interface
eval/
  metrics.py     Wilson intervals; ranking, link and abstention scoring
  splits.py      by-cluster held-out split; precision-dominant threshold freezing
  detectors/     naive and careful implementations of each module under test
                 (misconfig, correlation, entity, attribution, provenance)
  harness.py     build -> split -> fit -> freeze -> score; run() is the entry point
  report.py      Markdown renderer; baseline and n beside every number
  cli.py         command-line interface (`python -m eval run|report`)
gates/
  roles.py       roles, permissions, the authorisation decision (E2.4)
  cases.py       cases, per-case seeds, retention, purge, analyst decisions (E2.2)
  audit.py       append-only hash-chained trail (E2.3), plus the anchor that makes
                 a truncated trail detectable
  presentation.py candidate rendering, banned identity assertions, abstention (E2.5)
  exports.py     watermarked evidence packs (E2.4)
  acceptable_use.py the statement and its acknowledgement gate (E2.6)
  gate.py        the chokepoint: signed collection permits, every action logged
  cli.py         command-line interface (`python -m gates ...`); `demo` runs the refusals
collect/
  collector.py   the orchestrator: four paths, budgets, checkpoints, WARC, the run report
  ahmia.py       the public index: ranked results parsed into proposals
  ctlog.py       certificate transparency: onion/clearnet associations, and their caveat
  telegram.py    public previews: backward history walks, message ledger, cursor safety
  transport.py   the fetch protocol, plus the fixture transport every test runs against
  live.py        the live transport: off by default, gated five ways, unverified
  schedule.py    host delays, run budgets, revisit backoff, the sleeper
  checkpoints.py resume cursors and the proposal queue, both inside the case directory
  warc.py        WARC 1.0 records for the raw exchanges, referenced from snapshots
  fixtures.py    deterministic fixture pages generated from the farm's seed
  cli.py         command-line interface (`python -m collect ...`)
graph/
  model.py       artefacts, edges, entities, evidence; the doctrine as types
  extract.py     pull artefacts from a farm, each carrying its snapshot digest
  resolve.py     deterministic and probabilistic links; defaults become cohorts, not edges
  store.py       SQLite: proposals, findings, notes, reversible merges (before-state kept)
  cli.py         build / show / export / confirm / merge / reverse / note
dashboard/
  app.py         FastAPI on loopback; case view, entity view, mutations, evidence PDF
  render.py      HTML-to-PDF evidence pack, digest ledger, first-page limits
  templates/     base, index, case, entity
  static/        graph.js (dependency-free force-directed SVG), style.css
oniontrace/
  pipeline.py    the six stages, a manifest that records failure, no silent half-runs
  report.py      generated Markdown that names the limit of each section
  cli.py         `run` and `show`; exit code 1 when any stage failed
tests/
  test_pipeline.py 13 tests; pins stage failure recording, reproducibility, per-case run dirs
  test_graph.py  27 tests; each pins a refusal (merge, reversal, vendor default, contradiction)
  test_dashboard.py 18 tests; pins route refusals, traversal rejection, evidence-pack limits
  test_farm.py   65 tests; each locks in a property a reported metric depends on
  test_eval.py   65 tests; each pins what a number in the report is allowed to mean
  test_gates.py  95 tests; each asserts a refusal, or that the refusal is on the record
  test_docs.py    5 tests; README counts, layout paths and documented commands vs reality
  test_deck.py    7 tests; the deck carries notes on every slide, stays 16:9, keeps its
                  figures above 200 dpi, and refuses to build without diagrams or capture
  test_collect.py 69 tests; each asserts what a run stores, skips, proposes or refuses
  test_collect_cli.py 16 tests; each asserts what the operator-facing path will not do
```

## Reproducibility

A farm is fully determined by its `FarmConfig`. The same config produces a
byte-identical `farm.json`, which is why the report can cite a seed and a command
instead of shipping a fixture dump:

```bash
python -m farm generate --seed 20260925 --out ./out
python -m farm summary  --seed 20260925
```

All randomness flows through a single `random.Random(seed)` instance; the generator
never touches the global RNG, so results do not depend on import order or test order.

## Style drift

Personas drift at post 4: **content vocabulary** switches to an unrelated topic while
**function-word habits are left untouched**. This is the D3 requirement implemented
literally, and it is what lets the harness show that a content-feature model degrades
under rebranding while a function-word model does not.

The drift point is recorded on the persona as `drift_at`, with `drift_topic` set only when
drift actually happens. The harness reads that boundary to decide which queries count as
post-drift, rather than inferring drift from post counts or query length — an inference
that produces a table looking like a finding while measuring something else. Posts carry
an `index` property recovered from their zero-padded id, so a query's position relative to
the boundary is exactly checkable, and
`test_the_recorded_drift_boundary_is_where_the_vocabulary_changes` fails if the recorded
boundary ever stops being where the vocabulary actually moves.

## Design decisions worth knowing

**`shared_artifacts` holds extracted observations, not served bytes.** For fingerprint
kinds, the farm supplies the value a real collector *would* have computed by fetching
and hashing the artefact. Artefact extraction belongs to M2's correlator; the farm's job
is to supply observations plus ground-truth labels so the *linking* decision can be
scored independently of whether extraction is correct. The `.env` and 404 fixtures are
the exception — those are served as real documents because their content is what the
rule reads.

**Generated text is lexically controlled but semantically meaningless by design.** It is
a statistical fixture, not a corpus. Coherent prose would add no measurement value and
would risk producing content resembling real harmful material, which the farm must never
contain. Expect the raw text to read oddly; that is intended.

**Services are synthetic addresses, not real onion addresses.** Addresses are SHA-256
derived and clearly non-operational. The generator never publishes anything.

## Status

Implemented:

- **Farm** (WP2.1) — flaw catalogue, persona corpora, decoys, hard negatives, abstention
  cases, ground-truth export, CLI, 65-test suite.
- **Acquisition** (WP2.2/WP2.3) — four collection paths (onion front pages, public index,
  certificate transparency, Telegram previews) behind signed permits, one run-wide budget,
  resumable cursors that advance only after an ingest, a seed-proposal queue an analyst
  decides on, WARC records for the raw exchanges, and an operator CLI with no default
  transport. Fixture-verified: every test runs against in-memory pages, and the live
  transport is dark. 85-test suite across `tests/test_collect.py` and
  `tests/test_collect_cli.py`.
- **Evaluation harness** (Part G item 4) — by-cluster splits, threshold freezing with a
  safe-abstain fallback, six modules scored (M1 collection integrity on the fixture seam,
  M2 rules, M2 correlation, M3 entity resolution, M5 attribution, E4 provenance),
  abstention scored both thresholded and unthresholded, Markdown report, CLI, 65-test
  suite.
- **Deployment gates** (Part E2) — cases with recorded authorisation and retention,
  signed collection permits, per-case seeds, purge with a kept tombstone, append-only
  hash-chained audit, role-based access with case scoping, identity-assertion lint,
  watermarked exports, acceptable-use acknowledgement, CLI with a refusal demo,
  95-test suite (nine of them pinning audit-trail truncation and damage reporting).
  **284 tests pass in total.** (380 today, after `graph/`, `dashboard/`, the code-mix
  block, the end-to-end pipeline and the deck build landed.)

Not yet implemented: **M4** blockchain attribution (stretch, explicitly out of scope) and
the **M5 transformer upgrade** (the committed TF-IDF baseline lives in
`eval/detectors/attribution.py`). **Live acquisition is deliberately unfinished**: the
transport exists and its five guards are tested, but no run in this repository has ever
fetched a byte from a third-party target, and any report citing acquisition has to say so.
M1 is therefore scored as `fixture-verified` and never as `live-tested`.

That the correlator is next is the plan's own ordering, not a preference: the farm (WP2.1)
and the harness exist so the modules can be measured as they land, the gates exist so the
collector satisfied them from its first commit, and acquisition now supplies the snapshots
the correlator will read.

## The end-to-end pipeline, and the code-mix block

`python -m oniontrace run` executes the six stages in dependency order and writes a
manifest that records what each one produced. **A failing stage is recorded, not
swallowed**: the stages it feeds still run and the manifest says which answers are
missing. A pipeline that stops silently and still reports success is worse than one that
says detection failed and correlation was therefore skipped.

The generated `REPORT.md` names the limit of each section rather than presenting every
number as equally strong. Attribution figures are labelled synthetic, and the code-mix
gain is labelled as four personas.

**The code-mix block (`eval/detectors/codemix.py`)** adds a token-level language ID,
language-split function-word spaces, a code-mix ratio and an intra-post switch rate. It is
*gated* on detected mix rather than applied to everyone, because measurement said so:

| Writer | Without code-mix | With code-mix |
|---|---|---|
| Hinglish (≈20% mix) | 0.500 | **0.750** |
| Pure English | 0.250 | 0.250 |
| Template reuser | 0.750 | 0.750 |
| Overall hit@1 | 0.400 | 0.450 |

Applied unconditionally, code-mix cut template reusers from 0.750 to 0.250. A writer
reusing a fixed template has little vocabulary variance, so extra dimensions are noise.
`MIX_GATE = 0.20` sits in the middle of a 0.10-wide plateau (0.15–0.25) where the gain
holds with no loss anywhere.

Two limits are stated in the module and repeated here. The gain is 0.500 → 0.750 on
**four** personas, so it is one query flipping: a direction, not a magnitude. And the gate
was chosen on the same farm it is scored on, so it is a fitted value, not a held-out one.

The detector shares vocabulary with the generator that produces the text. That overlap is
measured and reported (`codemix.lexicon_overlap`), because a detector scoring text from
its own word list has not been shown to generalise.

## Analyst console and identity graph

Two modules have landed since the paragraph above was written, and this section replaces it
for those parts.

**`graph/` — M3 identity graph.** SQLite rather than Neo4j, so a marker can run it with the
standard library. The module exists to enforce one rule: *the system produces candidate
merges, never assertions*. A link is a proposal with its evidence attached; `merge()` is the
only operation that moves a node and it demands a named actor, a stated rationale, and a
prior edge, and it writes a complete before-state so `reverse_merge()` restores the pair
exactly. Vendor defaults and published placeholders are excluded from every link and
reported separately as *cohorts*, so an absent link is explainable rather than mysterious.

Measured against the shipped farm, the graph proposes **12 links that recover all 12 true
cluster pairs with no false positive** (precision 1.000, recall 1.000). Six of those score
above the candidate threshold and six fall below it; the six below are still shown, labelled
`possible`, with their scores visible.

**`dashboard/` — M6-lite console.** FastAPI, bound to `127.0.0.1` with the host not
configurable, serving one case at a time: entity graph, per-link evidence including
counter-evidence, confirm/reject, reversible merges, analyst notes, and a one-click PDF
evidence pack carrying the artefact digest ledger and its own statement of what a digest
does not establish. There is no authentication. That is a stated limitation for a
single-analyst tool, not an oversight.

```bash
python -m graph --db ./casework/CASE-1/graph.db --case CASE-1 build --farm ./out/farm.json
python -m dashboard        # http://127.0.0.1:8420
```

### A note on what none of this claims

The farm measures our own detectors against our own ground truth, both of which are ours.
The gates enforce a policy under an identity they do not verify, and their audit trail is
tamper-evident rather than tamper-proof. Neither shows that any of these techniques work on
a live network. That claim needs the Part E2.2 pilot and the deployment gates in a real
deployment, and until then every number and every guarantee in this repository is a
statement about a fixture and a boundary we drew ourselves.


## Browser QA of the console

The console is tested two ways. `tests/test_dashboard.py` drives the routes with a test
client, which catches server-side regressions but cannot see a page. `scripts/qa/` drives a
real Chromium over the DevTools Protocol on a real screen, which catches the things a test
client cannot:

```bash
python -m dashboard                                   # in one shell
chromium --remote-debugging-port=9222 http://127.0.0.1:8420/case/CASE-DEMO

python scripts/qa/stage1_render.py 9222 out/screen/qa  # DOM, layout, contrast, console
python scripts/qa/stage2_interact.py 9222 out/screen/qa --fresh  # every button, every 404
python scripts/qa/stage3_lifecycle.py 9222 out/screen/qa         # confirm -> merge -> reverse
```

Clicks go through `Input.dispatchMouseEvent` at the element's own coordinates after
confirming the element is the topmost hit at that point, so an invisible overlay or a
zero-size button is caught rather than bypassed. `--fresh` rebuilds the case first, because
a QA pass that inherits the previous pass's notes and confirmations measures history
instead of the app.

These found four defects that the route tests could not: the case and index pages shipped
with no `H1` at all (headings started at `H2` and `H3`), the merge operation had a route but
no control, edge cards carried no stable identifier in the DOM, and the render audit counted
the "No notes yet" placeholder as a note.

## Rebuilding the deck

The deck is twelve slides, one idea each, built around a drawn figure rather than a block of
text. Three scripts produce it, and each one refuses to guess:

```bash
python scripts/build_diagrams.py                          # five diagrams -> out/fig/*.png
python -m dashboard &                                     # the console slide needs it
python scripts/capture_dashboard.py                       # photograph it
python scripts/build_presentation.py --out deck.pptx      # assemble and audit the text
python scripts/verify_deck.py deck.pptx                   # bounds and text fit
python scripts/preview_deck.py deck.pptx out/preview --shots --pdf deck.pdf
```

`build_presentation.py` **fails** rather than shipping a placeholder when a diagram or the
console capture is missing; `--skip-screenshot` is the explicit text-only opt-out. It also
runs a text audit over its own output and returns non-zero on an em dash, a banned phrase or
a sentence over 34 words, because those are the things that make a deck read as generated.

There are three separate checks because they fail differently. `verify_deck.py` reads the
pptx and flags shapes outside the slide and text needing more height than its box has,
although it can only estimate text height. `preview_deck.py` rebuilds every shape as
absolutely positioned HTML at the same inch coordinates and lets a real browser lay the text
out, then measures clipping, overflow and image-over-text collisions in the DOM; it also
writes the per-slide PNGs and the PDF from that same render. `capture_dashboard.py` refuses
to run if something is already listening on the console port, since a stale server would be
photographed instead of the one the script just built.

Every number on the diagrams is read from `out/eval-results.json` at build time, so a figure
on a slide cannot disagree with the file it came from. That path is gitignored scratch, so
`data/eval-results.json` holds the same file frozen at the commit that produced the paper and
is used when a clean checkout has no `out/`. A freshly generated `out/` always wins.
