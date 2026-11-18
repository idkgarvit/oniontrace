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

Nothing in this package touches a third party's live target. The farm is self-contained: it
generates service descriptions, served fixtures, corpora, and ground truth on disk. There is no
crawler in `farm/`, and no part of this repository reaches a network by default — the acquisition
package below runs against those same generated fixtures unless a human passes `--live` **and**
`--authorised-mode`, and no test does. The one deliberate exception is the controlled live lab,
which publishes eight onion services of this project's own and fetches them over real Tor to prove
the transport path; its publisher and keys are disposable, and it measures no third party.

## Quick start

### The console, in a window

```bash
run/oniontrace-gui                     # the analyst console, in a window of its own
run/oniontrace-gui --case CASE-1       # open straight into a case
```

One command starts the local service on a free loopback port, waits until it answers, opens a
Chromium app window (no address bar, no tabs, its own window class) and stops the service when you
close the window. Nothing is left running behind it. There is no terminal interface to learn and no
browser address to paste: the console is the product, so it opens like one.

If no window shell is installed the launcher says so and prints the URL rather than failing
silently, and `python -m dashboard` still serves the same console for a headless machine.

### The pipeline, from the command line

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

**Coverage is now complete, and it is checked in both directions.** All nineteen single-scope
rules have a negative fixture served for them. The last seven — `dot_env`, `backup_file`,
`apache_version_banner`, `nginx_version_banner`, `timezone_leak`, `etag_pattern` and
`http_header_order` — were the ones whose flaw lives in a header or in one line of a file, and
they had been left out on the argument that a proxy makes them indistinguishable at the
response level. That argument was wrong. Each has a feature the careful rule reads and the
careless rule ignores:

| rule | the fixture looks like | the discriminator |
|---|---|---|
| `dot_env` | `/.env` answers 200 | the body carries no `key=value` line |
| `backup_file` | a `.bak` path answers 200 | the body is a page, not source with `<?php` |
| `apache_version_banner` | `Server: Apache` | no version token after the product name |
| `nginx_version_banner` | `Server: nginx` | no version token after the product name |
| `timezone_leak` | a `Date` header exists | it is GMT, so no local offset is disclosed |
| `etag_pattern` | an `ETag` exists | it is a release tag, not an inode-and-size pair |
| `http_header_order` | custom headers are present | three of the four default-stack headers |

**This is measured, not asserted.** `tests/test_hard_negatives.py` holds each fixture to two
conditions at once: the careful rule must stay silent on it, and the careless rule must fire.
A fixture that fires the careful rule is a false positive the farm planted itself, and a fixture
that tempts nobody proves nothing. Both ends are asserted for all nineteen, so a new rule cannot
join the set without a fixture that has teeth.

**What the fixtures changed.** The careful table is unchanged at 19 true positives and no false
positives, and that number now means something it did not before. The careless table moved from
17 true positives and 14 false positives (precision 0.548) to 17 and 21 (precision 0.447):
every one of the seven new fixtures fires the careless rule on the clean control, which is
exactly what a fixture with teeth is supposed to do. The separation between the two tables got
wider because the measurement got honest, not because the rules changed.

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

`python -m eval run` builds a farm, splits it by cluster, fits thresholds on dev and
freezes them, then scores every module on the whole farm and repeats the M2 measurements
on the held-out clusters alone, so an unseen-operator figure sits beside the full-farm one
instead of being implied by the split's existence. Each detector is implemented in
both a *naive* and a *careful* form, because a score with no comparable baseline is not a
result. The gap between the two is the measurement of whether the farm's hard negatives
are doing their job — if the careful and naive variants scored the same, the negative
fixtures would be decoration.

### Headline results (seed 20260925, farm `d1c3f9e8ef1ab5b3`)

| Claim | Result |
|---|---|
| M2 rule detection — careful | **100% recall, 0 false positives** |
| M2 rule detection — naive substring | 89.5% recall, 44.7% precision, 21 FP |
| M2 correlation precision@1 — naive → frequency-filtered | **0.750 → 1.000** (random baseline 0.154) |
| M2 correlation link precision — naive → filtered | **0.207 → 1.000** (false-merge 0.793 → 0.000) |
| M2 correlation at the frozen threshold | link precision **0.571** — thresholding alone does *not* fix it |
| M3 entity precision — exact → placeholder-blocklisted | **0.571 → 1.000** (decoy false merges 6/6 → 0/6) |
| M3 identifiers — PGP + wallets | 16 + 16 observations, every one checksum-valid |
| M3 trust edges | **12/12** planted vouches recovered; 12 are separate `trust` edges and none justifies a merge |
| Label-free corroboration | class strength keeps **12/12** farm links; blunt two-class rule keeps **6/12**; one soft class refused |
| M5 style drift — paired within-persona | content **16/16 → 0/16**; function words **11/16 → 13/16** (McNemar p=3.1e-05 vs 0.50); four stable personas are the placebo |
| E4 provenance | 88 entries verified; a planted mutation detected, then restored clean |
| Retrieval abstention | **0/4 → 4/4** correct abstentions once the frozen threshold is applied |
| M5 code-mix block (gated at mix ≥ 0.20) | Hinglish-heavy writers **0.500 → 0.750**, Hinglish-light **0.250 → 0.750**, template reusers **unchanged**; pure English +0.125 (n=8, one query) |

Two of these are worth reading closely.

**The threshold result is negative, and it is reported as such.** Freezing a score
threshold does not fix correlation link precision (0.571). Vendor-default artefacts
produce *perfect* scores, so no threshold can separate them from genuine cluster
artefacts. Only frequency filtering does (1.000). Publishing the thresholded number
without the unthresholded one would have hidden this.

**The drift result is paired, and the pairing is the point.** The earlier table compared different
personas before and after drift. With `posts_per_persona=12`, every drifted persona's held-out
posts sit after the drift point, so its "pre-drift" column could only contain template reusers:
two populations, not one. The replacement fixes one profile per persona, generates 24 posts, puts
the drift at post 12, and moves only the query window. Content vocabulary collapses from 16/16 to
0/16 (McNemar p=3.1e-05) while function words move from 11/16 to 13/16 (p=0.50). Four stable
personas are the placebo, and their small movements are why the 16/16 collapse is the number that
matters. Full record: `out/experiments.json` and `out/experiments.md`.

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
sites — not captured traffic. The byte-fetching code in `collect/live.py` is exercised two
ways: the guards by unit tests, and the fetch path by a controlled live lab this project
publishes itself. The eight-service lab described below collected **112 of 112 responses**
through real Tor on 2026-11-17. What remains unmeasured is accuracy on other people's onion
services, not whether the transport works. `python -m collect gates` states the same
distinction, so a demo cannot quietly upgrade one into the other.

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

# If the permit key has ever been readable beyond its owner, answer it rather than live with it
python -m gates key rotate --actor root --role admin \
    --reason "permit.key was 0664 until the mode repair on 2026-11-10"

# The demo: every refusal, in order
python -m gates demo
```

`key rotate` archives the retired key, writes the reason and a fingerprint of each key onto the
trail, and mints a replacement. Every permit the old key signed stops verifying, which is the
point: after an exposure, permits that some unknown party may have minted are exactly what has to
stop working. A process that is already running picks the change up on its next signature check
rather than at restart, because rotation only works if it reaches the collector that is mid-run.

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

## The execution environment (workflow phase 1)

Isolation is the first phase of every published investigation workflow and the one most
often asserted. A launcher that says `--unshare-net` isolates nothing if the operator runs
the collector without it, and a collector that reaches the open internet from a workstation
has already made the mistake the isolation existed to prevent. So the environment is checked
by observation and collection refuses when the check fails.

```bash
python -m oniontrace env                          # preflight, wherever you are
scripts/run_isolated.sh --self-test               # preflight, inside the jail
scripts/run_isolated.sh -- python -m collect run --case C1 --live --authorised-mode
```

| Check | How it is answered | Why not the obvious way |
|---|---|---|
| own network namespace | enumerates interfaces; a fresh namespace has `lo` only | comparing `/proc/1/ns/net` passes wrongly inside a PID namespace, where PID 1 is the jail's own init |
| direct egress blocked | opens a TCP connection to a routable address; success **fails** the check | a firewall listing can be correct about rules on a different machine |
| Tor reachable | SOCKS5 greeting, reads the method-selection reply | an open port says nothing about what is listening; an HTTP proxy on the SOCKS port would collect clearnet traffic and call it Tor |
| case directory writable | writes a file and removes it | a permission bit is not a filesystem |

**The jail has no network namespace at all** — not a firewall rule, not a proxy environment
variable. Its only route out is a unix socket bind-mounted from the host where Tor listens,
because a unix socket is a filesystem object and crosses a namespace boundary that an IP
socket cannot. That turns "the collector is configured to use Tor" into "the collector has
no other option".

The difference is measurable, and the test suite measures it rather than describing it.
Inside the jail the probe prints `BLOCKED` and `IFACES ['lo']`; with `--unshare-all` removed
from the launcher it prints `CONNECTED` and `IFACES ['eth0', 'lo', 'virbr0', 'wlan0']`. The
launcher therefore has its own test: dropping the flag fails the suite rather than quietly
shipping an unsandboxed wrapper.

The gate also sits in front of live collection as the last guard, after authorisation and
after the per-URL checks, so a run refused for lacking authorisation keeps that reason
instead of being told its problem is the environment.

**Not installed here:** there is still no system `tor` package, because installing one needs
root and a client-only daemon does not need to be installed. `scripts/tor_disposable.sh`
fetches the distribution's signed `.deb`, unpacks it with `dpkg-deb -x` into
`~/.cache/oniontrace-tor`, and runs it in a screen session under the same style of jail:

```bash
scripts/tor_disposable.sh start     # unpack, configure, run, wait for bootstrap
scripts/tor_disposable.sh status    # socket, bootstrap, session
scripts/tor_disposable.sh clean     # stop and delete every byte it wrote
```

Nothing lands in `/usr`, no service is enabled, and `clean` leaves no trace. The instance is
`ClientOnly 1`, so it never relays for anyone else, and `/home` and `/root` are empty tmpfs
mounts inside its jail — a client-only daemon that can read `~/.hermes/.env` is a liability.

**`--deep`** adds a fifth check that asks Tor to build a circuit and CONNECT to the Tor
Project's check host. It is off by default because it needs the working network and it
reaches a third party, but it retires the report's own first limitation, and the report
drops that line when it runs. Measured, end to end, from inside the no-network jail:

```
ok  own network namespace: loopback only, no routable interface
ok  direct egress blocked: 1.1.1.1:443 unreachable from here (OSError)
ok  tor reachable:         SOCKS5 no-auth handshake completed at unix:.../tor.sock
ok  case directory writable
ok  tor circuit:           CONNECT to check.torproject.org:443 succeeded through Tor

not established by this report:
  - a private network namespace says nothing about the cleanliness of the host outside it
  - this checks the environment the collector starts in, not everything it does afterwards
```

A jail with no route out, reaching a verified Tor exit, is the whole design in one output.

The refusal is a `GateRefused` (`E2.6` namespace, `E2.7` egress, `E2.8` Tor) and names
every failing check at once. The probes live in `collect/preflight.py`, not in `gates/`,
because the gates package is required to have no network or subprocess capability;
`gates/environment.py` holds only the verdict.

### What the live sources said

The first live runs went out on 2026-10-23 through the jail and a client-only Tor, against
case `CASE-LIVE-2`, with `public_index` and `certificate_log` and **no onion target in
scope**. Neither source returned data, and neither said so politely. Both answers are
recorded, because a source refusing a query and a source having nothing to say are
different facts and only one of them is about the world.

| Source | Answer | What the collector recorded |
| --- | --- | --- |
| `ahmia.fi` search | `302` to `/`, on every query, with any user agent | A refusal, not a ranking with zero results. The note quotes the host's own `robots.txt` (`Disallow: /search/`, `Disallow: /*?`) and keeps the `Onion-Location` header the refusal carried. **Removed from the allowance on 2026-10-23**: measured three times, same answer, so the tool no longer names the host |
| `crt.sh` | `429` | Nothing parsed, and the query deliberately left un-checkpointed so a re-run retries it instead of treating throttling as an empty result |

Two things this changed in the code. A `3xx` used to fall through to the parser, so the run
printed `0 candidate addresses parsed` with `errors=0` — true, and together a lie. A `3xx`
is now its own outcome. And the SSRF guard used to resolve every host before the allowance
check, which inside a no-network jail fails for *every* name and refuses a run for being
well contained; an allowlisted name that cannot resolve here is now exempt, while a
private, loopback, link-local or IP-literal verdict still refuses even when allowlisted.

The practical consequence is a source problem, not a tool problem: ahmia.fi publishes a
machine-readable refusal and then enforces it, and crt.sh fronts data whose authoritative
source is the CT log servers themselves. Reading certificate transparency at the log rather
than through a mirror that would rather not be queried is the honest fix, and it is built:
see the next subsection.

### Collecting from a live onion, and how it is proven

Every acquisition number in this project came from a fixture until 2026-10-23, when the
collector fetched a live onion service for the first time. Pointing it at a stranger's hidden
service would have measured a third party who did not ask to be measured, and pointing it at a
local page called `onion.example` tests the parser while skipping the only hard part, which is
Tor. So the check publishes an onion of its own: a static page served from inside the
collection jail, published by the same disposable Tor, then collected back through the real Tor
network and the real jail bridge.

```bash
python3 scripts/live_onion_check.py            # explains what it would do, publishes nothing
python3 scripts/live_onion_check.py --publish  # publishes, collects, verifies, stops
```

The page carries a nonce generated for that run. A cache, a stale snapshot, a fixture that
happens to contain the same words, or a collector that stored something it never fetched all
fail to produce it, so the check can only pass by completing the round trip.

Three details cost an attempt each, and all three are about publishing rather than collecting.
A freshly published hidden service is not findable immediately: its descriptor has to reach the
hash ring, and the measured waits across runs were 5.8 seconds on a tor that had already been up
for three minutes, then 78 seconds, then 240 seconds where the answer did not arrive at all, and
finally 319.5 seconds on a cold instance. The window is 600 seconds for that reason, and it says
so while it waits. A publisher started on top of a running client dies with "another Tor process
is running with the same data directory", so the script stops the old instance and refuses to
start a second one. And a `.onion` target with no SOCKS proxy is refused rather than attempted,
because an onion name sent to ordinary DNS leaks the target to the resolver and cannot work
anyway; that refusal is what the first hand-run produced, which is the guard behaving as
designed.

The successful run is kept as evidence: `casework/cases/CASE-LIVE-11`, one seed, one snapshot,
one cursor, one WARC record of the raw exchange, `pages=1 fetched=1 stored=1 errors=0`, against
an address this project published and then took down. The stored snapshot contains the page that
run served and nothing else was claimed from it. Case data is never committed, by rule, so that
directory exists on the machine that ran the check and the way to see it elsewhere is to run
the check again.

#### The eight-service lab, and what it adds

One page proves a round trip. It does not exercise fourteen routes per service, nineteen
detectors, or the correlator. `eval/live_lab.py` builds a fixed bed of eight services: two
clusters of three sharing a favicon, an analytics ID and a contact handle, plus two
singletons, with all nineteen rules planted exactly once, five clean controls, and a stock
vendor analytics value shared by four services as a correlation trap.

```bash
python3 scripts/run_live_lab.py            # explains the run; publishes nothing
python3 scripts/run_live_lab.py --publish  # publishes eight, collects, scores, tears down
```

The publisher is one disposable Tor with eight `HiddenServiceDir` blocks. The collector is a
**second, keyless** Tor, so the analyst process never holds the service keys. Every address is
warmed to two consecutive `200`s before the case opens, then collected inside the no-network
jail with the fixed thirteen-path probe list, and the WARC is scored against truth written
before publication.

The clean run is `CASE-LAB-LIVE-5` (2026-11-17, fingerprint `aa73822ae80a7f1f`): **112 of 112
responses** stored, `errors=0`, `out-of-scope=0`, 304 seconds.

| Check | Live result |
| --- | --- |
| careful rules | 19 TP, 0 FP, 0 FN |
| naive rules | 17 TP, 59 FP, 2 FN (precision 22.4%) |
| planted-path accuracy | 19/19 |
| filtered correlation | 6 links, 6 TP, 0 FP (100.0%) |
| naive correlation | 11 links, 6 TP, 5 FP (54.5%) |
| singleton abstention | 2/2 filtered, 0/2 naive |

The naive false-positive count is larger than the farm's because this bed serves fourteen paths
per service and the naive rules fire on clean paths; compare within the bed, not across it.
The scope limit is the same as everywhere else in this README: the services and the flaws are
ours, so this measures the live path, not accuracy on third-party onion services.

### Certificate transparency at the source

`collect/ctlog_direct.py` reads the logs themselves instead of a mirror. Every public log
serves RFC 6962 without credentials, and a log's signed tree head is a cryptographic
commitment: the operator's key signs `sha256_root_hash` and `tree_size`, so a head that
verifies is evidence a mirror cannot fake.

Measured from inside the isolation jail on 2026-10-23, through Tor:

```
operator list: 21 usable logs
  ok   Google Argon2026h2: tree_size=3,453,420,667 signature verified
  ok   Google Argon2027h1: tree_size=661,236,563 signature verified
  ok   Google Xenon2026h2: tree_size=2,986,793,766 signature verified
  ok   Google Xenon2027h1: tree_size=533,197,054 signature verified
4 of 4 tree heads verified against their published keys
  reading entries 3453420664..3453420666 of Google Argon2026h2
  3453420664: precert, 0 name(s): (no names readable in the leaf)
  3453420665: x509, 1 name(s): portal.foresite.tech
  3453420666: x509, 1 name(s): x.clio.alterspective.com.au
```

Verification checks two things, and the second is the one that matters: the ECDSA signature
over the RFC 6962 `TreeHeadSignature` bytes, **and** that the key in the operator list
hashes to that log's published `log_id`. Without the second check a substituted key would
verify a substituted head perfectly well.

Run it with `python3 scripts/ct_log_check.py` inside the jail.

The honest limit: **this is verification, not search.** A log is append-only and indexed by
sequence number, not by name, and SSLMate's own documentation puts the public logs at over
4 billion entries and 17TB. There is no way to ask a log "find every certificate naming a
.ONION address" — that is a query a mirror provides and a log does not. Discovery therefore comes from sources
that invite it, which is the next subsection.

### How discovery works now, and why not a search index

Ahmia answered "give me onions matching this word" and stopped answering. The obvious move is
another index, and it is the wrong one. Every public engine that does keyword search over
hidden services either refuses automated querying (ahmia), is a curated list of criminal
marketplaces this project will not touch, or is a directory of sensitive services it should
not enumerate. That is not a gap in the tooling; it is the state of the world.

What operators publish instead is a pointer from their own clearnet site to their own onion,
in two standard forms: the `Onion-Location` response header, and
`<meta http-equiv="onion-location">`. Both are the publisher asserting "this onion is mine".
That is a *self-declared* pair, which is better evidence than a ranking for two reasons: the
party best placed to know is the one asserting it, and the assertion arrived attached to a
response the collector received and hashed.

`collect/onionlocation.py` parses both forms; the `onion_location` origin reads one page per
declared clearnet address and records the pair. It never follows the onion — a tool that
visits whatever a page points at can be walked into a target by a hostile page.

Measured from inside the jail through Tor on 2026-10-23, nine publisher front pages:

| Page | Answer |
| --- | --- |
| `www.torproject.org` | 1 advertisement (`2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion`) |
| `freedom.press` | 1 advertisement (`fpfjxcrmw437h6z2xl3w4czl55kvkmxpapg37bbopsafdu7q454byxid.onion`) |
| `securedrop.org` | 1 advertisement (`sdolvtfhatvsysc6l34d65ymdwxcujausv7k5jk4cy5ttzhjoi6fzvyd.onion`) |
| `www.propublica.org`, `www.debian.org`, `duckduckgo.com`, `www.bbc.com`, `www.eff.org` | answered, no advertisement at the root page |
| `www.nytimes.com` | `403` — the Tor exit was refused; recorded as a refusal, not as "no advertisement" |

Three pairs from nine pages, and the refusals counted separately from the absences, because
those are different facts. A publisher who does not advertise is not a publisher who has no
onion.

### The keyword origin has a provider again, and it is one this project builds

The `public_index` origin shipped with no provider: the index it was named after refuses
automated queries, so a keyword search produced a recorded refusal and nothing else. The
honest fix was not to find another public engine, because there is no other public engine that
permits the question. It was to build the index locally out of pages the collector is already
allowed to read.

`collect/index.py` does that. Every collected page can mention other onion addresses, and a
page that names an address is evidence about it: who linked to it, in what words, on what
date. Extraction is one regex borrowed from the parser that already existed, so what counts as
an address cannot drift between the two; the citation is part of the row's primary key, so no
hit can be stored without the page that produced it.

Two doors fill it. Automatically, from the case's own stored snapshots, refreshed before the
first query of every run, which is what makes discovery a consequence of collection. And
deliberately, through `--index-import <file.jsonl>`, one JSON object per line, which is the
path for a published dataset such as DUTA-10K or a list an analyst is entitled to hold:

```
{"onion": "<56 chars>.onion", "title": "Escrow marketplace", "source_url": "...", "context": "..."}
```

The whole path was run end to end on a throwaway gate root, with the fixture transport and no
socket opened:

```
  search queries: 1 (index: local)
index import dataset.jsonl: 3 address(es) added, 0 line(s) skipped
public_index       pages=0   fetched=0   stored=1   dup=0   out-of-scope=0   errors=0  waited=0.0s
  - local index: no page budget is spent, because no page is fetched
  - local index holds 3 mentions of 3 addresses from 1 pages
  - 'escrow marketplace': 3 of 3 addresses matched
```

Then a page was stored through the gate carrying one new address, and a fresh query found it:
`4 mentions of 4 addresses from 2 pages, 1 of them added from 1 stored snapshots on this run`,
one hit, one proposal, and the proposal cited the page rather than the index
(`from http://example.invalid/mirror-list-2`). Nothing in the path can turn a proposal into a
seed, because the proposal store is where it stops.

Three properties are deliberate and tested. The index never ingests its own answers: a
`public_index` snapshot is skipped on the way in, or the next query would cite the previous
one and every score would inflate. Words match as whole words, because answering "art" for
"carte" is the failure mode a keyword index hides best. And both the index and the snapshots it
reads live inside the case's `derived` directory, so a purge deletes them together; an index
that outlived its case would be retention with an asterisk.

The limits are real and stated. This can only find addresses that appear in material already
collected or explicitly imported, and it holds one sentence of context per address rather than
a copy of the web. It is a discovery aid that gets better as a case collects, not a crawler
that finds a market nobody pointed at. `--search-provider ahmia` still exists, still runs the
old path, and still records the refusal, because deleting the evidence that a source was tried
and said no would be the wrong kind of tidy.

### What the web actually offers, measured rather than assumed

Before building anything, the obvious question was whether some public index would simply
answer automated queries. Five were tried from a real browser on 2026-10-24, and the answer
was no in four different ways:

| Source | What it says to an automated reader |
| --- | --- |
| `ahmia.fi` | `Disallow: /search/` and `Disallow: /*?`, enforced with a 302 |
| `darksearch.io` | the certificate does not validate; the service is gone |
| `searx.be` | "Verifying your browser", a bot challenge |
| `onionsearchengine.com` | "Sorry, you have been blocked" |
| `tor.taxi`, `dark.fail` | robots allows reading, but these are curated lists of marketplaces, which this project does not touch |

So the index stayed local, and the question became where a lawful crawler can find addresses
at all. The answer is publishers who list their own: `securedrop.org` publishes a directory of
newsrooms, its robots file is `Disallow:` with nothing after it and `Crawl-delay: 10`, and its
sitemap names 316 URLs of which 55 are directory entries.

`collect/directory.py` walks that. `--directory URL` reads one page, takes the pages under it
from the sitemap or from the links, keeps the same host, orders distinct paths ahead of facet
links such as `?country=3`, and spends the run's page budget like every other path.

Nothing about that walk is hardcoded, and the entry point is the only thing an operator
supplies. `--directory` has no default and appears nowhere in the source outside a docstring
and one test. What the walk does with the URL it is given is the automatic part: it parses the
page, follows the links, and now also reads the map the host declares for itself. A front page
links to its navigation, not to its entries, so when the page leaves budget unclaimed the walk
reads the `Sitemap:` line out of `robots.txt`, fetches that map once, follows a sitemap index
one level, and tops the list up from it. The page always wins: a page that already supplies as
many entries as the walk may carry does not trigger a sitemap fetch at all, because spending a
page to be told nothing new is not politeness. The map cannot repeat the page either -- what it
offers is filtered against the links the page already named, or a depth-ordered map would spend
the room on `/about/` for a second time. And because a map is itself a page, its fetch is
charged to the budget before the room it fills is measured; without that the walk plans one
page more than it may spend, drops its last entry, and never marks the directory done. A map
declared on another host is not read, since a robots file is the host's statement about itself
and cannot widen the run's scope.

Pointed at a front page there is no path prefix to prefer, and a sitemap lists a site in the
order it published, so the walk orders what the map returns by path depth: sections are
shallower than articles, and `/directory/` must not queue behind three hundred headlines.

The permit is a deadline, not a receipt. A host that asks for ten seconds between pages makes
a fifteen-minute permit two dozen pages long, and the gate refuses an ingest presented after it
-- so without a guard the walk succeeds, the store is refused, and every page read is lost
after the host has already been paid the politeness. The collector checks the deadline before
each request, holding the transport's own timeout in hand so a page cannot start that would
finish after it, and stops with what it has and a per-page cursor. The run then stores what it
read and the next one continues from there.

Proved live on `securedrop.org` on 2026-10-25: a ten-minute request timeout against a
fifteen-minute permit put the deadline at five minutes of walking. The walk stopped at 300
seconds with 21 of 40 pages stored, 19 named as unread, and a cursor per page. The byte budget
was 10.3 MB of the 64 MB allowed, so time was the only limit -- and the earlier attempt that
hit the 8 MB ceiling first showed what the guard is for, because a byte stop also keeps the
pages it read instead of losing them to a late refusal.

The first live attempt at this proof also found a defect in the guard itself. The live
transport is wrapped by the environment preflight, and the wrapper hid the transport's
`settings`, so the reserve silently fell back to a 30-second default while each request was
allowed ten minutes: the guard could never fire before the deadline it was meant to protect.
The wrapper now passes the setting through, and a test pins it.

That means the operator can point the walk at `https://securedrop.org/` and let the site name
its own pages, which is the difference between a crawler that is handed its answer and one
that asks the publisher where to look.

The crawl delay is honoured, and that is not a detail. A host that states ten seconds between
requests has stated how it wants to be read; a walk that ignores it is not a polite crawler
with a bug, it is a crawler reading on terms the publisher did not offer.

Run live through the jail and Tor on 2026-10-24, ten-page budget. The first attempt found
nothing and was not a bug: `securedrop.org` answers through Tor in 19.8 seconds, which is the
live transport's default timeout to the tenth of a second, so the fetch died at the boundary
and the run recorded `errors=1` rather than a directory with no addresses in it. The retry
used `--timeout 60`, which is what a clearnet host behind Tor needs:

```
  - https://securedrop.org/directory/: its robots.txt asks for 10s between requests, and
    this walk waits that long rather than the run's own setting
  - the page budget ran out mid-walk; the directory is not marked as read, so a later run
    finishes it instead of believing it was done
2 proposal(s) written to the case's queue:
  sdolvtfhatvsysc6l34d65ymdwxcujausv7k5jk4cy5ttzhjoi6fzvyd.onion  (site-wide)
  tiykfvhb562gheutfnedysnhrxpxoztyszkqyroloyepwzxmxien77id.onion  (Aftenposten's own)
```

Eight pages read, eight snapshots stored, two addresses found, and one of them is an
organisation's own onion that no `Onion-Location` header on the front page would have shown.
Both became proposals, because nothing in this package can do more than that.

The run stopped at its page ceiling, and the first version of that was a trap: the directory
carried no cursor, so the next run started at the top of the same nine pages and stopped in
the same place, forever. Progress is now recorded per page, so a capped walk continues from
where it stopped. That is also what the note above means when it says the directory is not
marked as read: the pages read are remembered, the list is not.

The next run with a ceiling of fourteen proved it on the same case: it skipped the pages it
had already read, walked eleven it had not, and the case now holds nineteen snapshots and
eleven distinct addresses, among them Der Spiegel's, the Financial Times' and Forbes' own
onions, each still a proposal and none of them a seed.

A third defect came out of the same run, and it is the worst of the three because it looked
like a result. Pointed at `https://freedom.press/`, the walk read eleven of its pages, archived
each response into the case, and reported `0 address(es) across 11 page(s)`. The archive it had
just written contained this, on every one of those pages:

```
onion-location: http://fpfjxcrmw437h6z2xl3w4czl55kvkmxpapg37bbopsafdu7q454byxid.onion/
```

The walk read page bodies, and freedom.press publishes its onion in the header and nowhere in
the markup. So a host that advertises its onion exactly the way the standard provides was
reported as a host with no onion, which is the one kind of wrong answer this project cannot
ship. Every page a walk reads now gets the same treatment as any other read page: what it
advertises in `Onion-Location`, in a `meta` tag, and what it merely mentions in its body are
all recorded, and the payload says which of the three it was. Re-run into a fresh case, the
same host yields:

```
1 proposal(s) written to the case's queue:
  fpfjxcrmw437h6z2xl3w4czl55kvkmxpapg37bbopsafdu7q454byxid.onion
      advertised in the page's header under the directory freedom.press
```

Then the index, on the same case, with no socket opened:

```
  - local index holds 9 mentions of 2 addresses from 8 pages
  - 'aftenposten': 2 of 2 addresses matched
2 proposal(s) ... (public_index, from https://securedrop.org/directory/aftenposten-as/)
```

Two defects came out of running it for real, and both were the kind a fixture never shows.
The index read only the page's 4000-character excerpt, and the address sat past the cut, so a
page that found two onions indexed as if it had found none; it now reads the addresses the path
recorded. And a query checkpoint meant "asked once", so the first empty answer would have been
final while the case filled up underneath it; the checkpoint now carries the index's size, and
a grown corpus makes the question new again.

### Asking the source instead of deciding about it

Removing ahmia for refusing and keeping crt.sh for serving would be two exceptions, not a
policy. So the collector asks: it fetches `robots.txt` once per host, evaluates the rule
against the exact path and query it intends to use, and refuses when the answer is no.

Measured live on 2026-10-23, one run, one query:

```
certificate_log    pages=0  fetched=0  stored=0  errors=0  BLOCKED
  - crt.sh: robots.txt says Disallow: /. Nothing was requested.
    --accept-source-policy records a human decision to use the source anyway,
    and repeats it in this run's notes
BUDGET             pages=1/50 bytes=26/8388608
```

Twenty-six bytes is the robots file itself. The query was never sent.

`--accept-source-policy` exists because a lawful reason to use a source whose crawler policy
is wider than its API policy is a real thing, and it should be somebody's decision rather
than a line of code that quietly stopped checking. The flag puts that decision in the run's
notes every time it is used.

Three details that are easy to get wrong, and have tests: `/*?` is a real rule meaning "any
URL with a query string", so matching is a wildcard conversion rather than ``startswith``; an
absent robots file is a host that stated no policy, not a ban, while a `401` or `403` on the
file is the standard's "assume everything is disallowed"; and `Allow` directives are ignored,
which over-blocks rather than under-blocks, because arguing precedence with a stranger's file
is not this tool's job.

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
  index.py       the keyword index this project builds from pages it collected, and the
                 imports that let an operator load a published dataset into it
  directory.py   the walk under a publisher's index: same host, capped, and the pages it
                 points at rather than the pages it happens to show
  ahmia.py       the public index parser, kept selectable and kept refusing: the index it
                 was written for answers automated queries with a redirect
  onionlocation.py  self-published pairs: the Onion-Location header and the meta tag
  robots.py      the host's own policy: rules, wildcard matching, the crawl delay it
                 asks for, and the sitemap it declares
  ctlog_direct.py  RFC 6962: operator list, signed tree heads, verified signatures,
                 Merkle leaves -- certificate transparency read at the source
  ctlog.py       certificate transparency: onion/clearnet associations, and their caveat
  telegram.py    public previews: backward history walks, message ledger, cursor safety
  transport.py   the fetch protocol, plus the fixture transport every test runs against
  live.py        the live transport: off by default, gated five ways, exercised by the
                 controlled eight-service lab over real Tor
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
  cli.py         `run`, `show` and `env`; exit code 1 when any stage failed or the preflight failed
tests/
  test_pipeline.py 13 tests; pins stage failure recording, reproducibility, per-case run dirs
  test_graph.py  38 tests; each pins a refusal (merge, reversal, vendor default, contradiction)
  test_dashboard.py 54 tests; pins route refusals, traversal rejection, evidence-pack limits,
                   injection through collected values, case creation from the browser,
                   the timeline filter, CSV escaping, and the origin/CSP boundary
  test_demo_accuracy.py 6 tests; scores the built demo case against the farm it came from --
                  per-link precision and recall, hard negatives, singletons, findings
  test_farm.py   73 tests; each locks in a property a reported metric depends on
  test_eval.py   69 tests; each pins what a number in the report is allowed to mean
  test_gates.py 105 tests; each asserts a refusal, or that the refusal is on the record
  test_docs.py    8 tests; README counts, layout paths and documented commands vs reality
  test_deck.py    7 tests; the deck carries notes on every slide, stays 16:9, keeps its
                  figures above 200 dpi, and refuses to build without diagrams or capture
  test_collect.py 85 tests; each asserts what a run stores, skips, proposes or refuses
  test_collect_cli.py 16 tests; each asserts what the operator-facing path will not do
  test_environment.py 26 tests; each pins one check, one refusal, or that the shipped
                  launcher really isolates -- two of them start a real bwrap jail
  test_robots.py 14 tests; said no, said nothing, said it in a way this has to
                  interpret -- including the two real files that started this
  test_onionlocation.py 14 tests; the two published forms, and every malformed value a
                  hostile page could put in either of them
  test_live_lab.py 30 tests; the lab is deterministic, every rule has one planted
                  positive, and the live path is scored against truth fixed first
  test_ctlog_direct.py 13 tests; each builds its own key and bytes, so a tree head
                  either verifies offline or it does not -- none of them opens a socket
  test_full_check.py 28 tests; the harness contract -- exit 0 without a verdict is
                  UNPROVEN, a skipped stage is never a pass, and every claim function is
                  fed the output that defeats it
  test_dependencies.py 3 tests; every third-party import is in requirements.txt, so a
                  fresh clone installs what the code actually imports
  test_base_rates.py 11 tests; the farm's one claim about the world matches the measurement
                  that produced it, and the correlation result survives the drop to it
  test_ct_ground_truth.py 21 tests; the evaluator's arithmetic, its cap, and a held-out
                  split that never lets a cluster be learned on one half and scored on the
                  other -- including the wildcard rule that keeps one holder from becoming
                  two services
  test_build_ieee.py 17 tests; the IEEE build holds its contents -- tables wrap inside the
                  column, a 56-character address breaks without gaining a character, and
                  inline code is not escaped twice
  test_paper_drop.py 3 tests; every number in the drop comes from the run this process
                  started, a half-finished run is reported rather than parsed, and one
                  seed is not run twice
  test_index.py  26 tests; extraction, whole-word matching, ranking, and a guard that the
                  index never ingests its own answers as evidence for the next answer
  test_local_index.py 12 tests; the keyword origin answered by the local index -- no socket
                  opened, every hit citing the page behind it, declared seeds never offered
                  back, and a query asked again only when the index has actually grown
  test_directory_walk.py 34 tests; a directory walk that stays on its host, keeps the cap,
                  waits the delay robots.txt asks for, resumes per page when the budget
                  stops it, advances past a prefix it has already read, stops before its
                  permit expires rather than losing the pages, and never mistakes a facet
                  link for an entry
  test_experiments.py 5 tests; the paired drift design, the seed and topology sweeps,
                  the cap ablation, and the query-level significance tests stay paired
                  and deterministic
```

## Base rates are measured, not chosen

The farm plants a stock-default artefact on every eligible service, which is a 100 percent
prevalence claim sitting under every correlation number in the report. That was the last
assumption in the experiment with no source behind it.

Real certificates supply one. On 3,018 onion-bearing certificates there are 19 issuers, the
most common covers 1,788 of them (59.2 percent), and 3,015 of 3,018 sit under an issuer that
another certificate also uses:

```bash
python3 scripts/measure_base_rates.py     # rewrites base_rates.json, prints the share
python3 scripts/base_rate_sensitivity.py  # the farm at 100%, 59.2% and 25%
```

| prevalence | defaults planted | naive precision | capped precision |
|---|---|---|---|
| 100% (design) | 18 | 0.207 | 1.000 |
| 59.2% (measured) | 10 | 0.444 | 1.000 |
| 25% | 4 | 0.800 | 1.000 |

The cap's precision does not move, because a value shared by more services than a cluster
cannot be cluster specific at any prevalence. What moves is the naive correlator, which gets
worse as defaults become more common -- so the designed 100 percent rate is the hardest case
for it and the fairest headline, and the conclusion does not depend on it. The one thing that
does stop discriminating at low prevalence is the ranking view, where naive precision@1 also
reaches 1.000; the link view keeps separating them.

The farm keeps 100 percent as its default so every published number stays reproducible, and
`FarmConfig(widespread_default_share=...)` exists so a reader can check the claim rather than
take it. `tests/test_base_rates.py` holds the constant to the measurement and the conclusion to
the measured rate.

## Real groupings, and how little the certificate fields recover them

The farm's correlation numbers are scored against ground truth the generator wrote down. That is
the only way to compute precision at all, and it is also why they cannot be a claim about the
world. Certificate Transparency supplies ground truth nobody in this project invented: a
certificate naming several onion services asserts that one holder controls all of them, signed
by a CA, produced for a purpose unrelated to this work.

So the question the farm cannot answer becomes answerable. Given only the fields a passive
observer has, how much of that real grouping is recoverable?

```bash
python3 scripts/ct_ground_truth.py    # rewrites ct_ground_truth.json, prints a verdict
```

On the cached corpus that is 516 services in 466 certificate-implied clusters, 205 true pairs,
largest cluster 12. The two linkers the corpus supports are the issuer alone, and the issuer
together with the issue date:

| linker | pairs proposed | precision | recall | F1 |
|---|---|---|---|---|
| issuer alone | 32,855 | 0.006 | 1.000 | 0.012 |
| issuer + issue date | 1,304 | 0.134 | 0.854 | 0.232 |
| + cap at 10 | 509 | 0.273 | 0.678 | 0.389 |
| + cap at 3 (the farm's rule) | 106 | 0.179 | 0.093 | 0.122 |

The issuer alone is the clearest possible illustration of the problem this project exists for.
It finds every real pair and proposes 32,650 false ones, because one CA issues to most of the
population. Adding the issue date is a genuine improvement and still leaves seven of eight
proposed links wrong.

The cap is where the honest part gets uncomfortable. It is selected on one half of the clusters
and reported on the other, because reporting the best cap found on the data it is scored on is a
fit and not a result. Chosen that way it holds F1 0.222 on the unseen half, against 0.389 at its
best point in sample. The farm's own rule, a cap of three, scores 0.122 here: the cap that works
is a property of the population, and the population was invented when that number was chosen.

Two conclusions follow, and the second one is the useful one.

Certificate fields are a weak linker on real data, so no claim in this project should rest on
them. And that is fine, because Certificate Transparency was never needed here as a feature.
It is needed as a target: an independent assertion of who owns what, signed by a third party,
against which a correlator can be scored. Treating co-certificate as a feature instead would
have been reading the answer key, which is why `eval/ct_ground_truth.py` keeps it on the target
side and tests that it stays there.

One detail that looks like a discrepancy and is not. Elsewhere the corpus is described as
holding 688 unique onions; here it is 516. A wildcard SAN is folded into the name it covers,
because `*.x.onion` and `x.onion` are one holder, and counting them as two services would split
a single operator across two nodes in the ground truth itself. The evaluator prints that note
next to its own count so the two numbers cannot be read as a contradiction.


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
  transport. Fixture-verified invariants plus a controlled eight-service live lab over Tor
  (112 of 112 responses, `CASE-LAB-LIVE-5`). 85-test suite in `tests/test_collect.py`, 16 in
  `tests/test_collect_cli.py`, and 30 offline lab tests in `tests/test_live_lab.py`.
- **Evaluation harness** (Part G item 4) — by-cluster splits, threshold freezing with a
  safe-abstain fallback, six modules scored (M1 collection integrity on the fixture seam,
  M2 rules, M2 correlation, M3 entity resolution, M5 attribution, E4 provenance),
  abstention scored both thresholded and unthresholded, Markdown report, CLI, 65-test
  suite.
- **Deployment gates** (Part E2) — cases with recorded authorisation and retention,
  signed collection permits, per-case seeds, purge with a kept tombstone, append-only
  hash-chained audit, role-based access with case scoping, identity-assertion lint,
  watermarked exports, acceptable-use acknowledgement, CLI with a refusal demo,
  105-test suite (nine of them pinning audit-trail truncation and damage reporting).
  **284 tests pass in total.** (838 today, after `graph/`, `dashboard/`, the code-mix
  block, the end-to-end pipeline and the deck build landed.)

Not yet implemented: **M4** blockchain attribution (stretch, explicitly out of scope) and
the **M5 transformer upgrade** (the committed TF-IDF baseline lives in
`eval/detectors/attribution.py`). **Live acquisition has run, and the sources refused it**
(2026-10-23, `CASE-LIVE-1` and `CASE-LIVE-2`): the transport reached two real public
services from inside the isolation jail and both declined to answer, which the collector
recorded as refusals rather than as empty results. See *What the live sources said* below.
M1 stays scored as `fixture-verified`, and the live runs are reported as `live-refused`.

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
cluster pairs with no false positive** (precision 1.000, recall 1.000). Ten of those score
above the candidate threshold and two fall below it; the two below are still shown, labelled
`possible`, with their scores visible.

**Identifiers and relationships are separate claims.** PGP fingerprints and wallet addresses
are parsed from the served pages, not copied from the farm manifest, and a value is recorded
only if its format validates: grouped hex or a labelled/armoured fingerprint for PGP, and the
chain checksum for Bitcoin (Base58Check or Bech32/Bech32m), Monero (Keccak-256) and Ethereum
(EIP-55). A 40-hex favicon digest is not a fingerprint, because the extractor requires context.
Multiple keys or wallets per service are kept, since collapsing to the first would hide the
link the extractor exists to find.

A vouch is stored as a `trust` edge with score `0.0`, next to any identity edge on the same
pair. `merge()` refuses a trust edge outright: knowing two operators vouch for each other is
not knowing they are the same operator. Negated vouches ("we do not vouch for ...") are
ignored, and every clustered service on the farm carries one as a hard negative. Entities
also carry `category`, `last_scan` and `source`, shown on the entity card and exported in the
CSV, so freshness and provenance do not require opening SQLite.

**Validation without labels.** `eval/labelfree.py` measures what can be measured when nobody
publishes a `cluster_id`: operator declarations as ground truth, recurrence across crawls,
audit sampling with a Wilson interval, and corroboration by artefact class. A hard class (SSH
host key, TLS key, application bundle) is sufficient evidence on its own; two independent soft
classes are required otherwise. On the farm that keeps 12/12 true links, where a blunt
two-class rule keeps 6/12. `eval/realdata.py` turns advertisements, certificate associations
and mirror lists into declarations and scores the correlator against them; it is tested end to
end but has not been run against a third-party crawl, so no field precision number is claimed.

**`dashboard/` — M6-lite console.** FastAPI, bound to `127.0.0.1` with the host not
configurable, serving one case at a time: entity graph, per-link evidence including
counter-evidence, confirm/reject, reversible merges, analyst notes, and a one-click PDF
evidence pack carrying the artefact digest ledger and its own statement of what a digest
does not establish. There is no authentication. That is a stated limitation for a
single-analyst tool, not an oversight.

```bash
python -m graph --db ./casework/CASE-1/graph.db --case CASE-1 build --farm ./out/farm.json
./run/oniontrace-gui       # the console in a Chromium app window, service stopped on close
python -m dashboard        # or: http://127.0.0.1:8420
```

### Running a case from the browser

The console is the operated interface, not a viewer bolted onto a command-line tool. From an
empty index a person can build a case, read it, annotate it, decide on links, and export it
without opening a terminal:

| Route | What it does |
|---|---|
| `/new` | Runs the same `run_pipeline` entry point as the CLI, so the gate, permit and audit are unchanged. Refusals (missing owner, missing authorisation, duplicate id, mistyped seed, retention out of range) come back as sentences in the form, never as a JSON body rendered as the page. |
| `/case/{id}/timeline` | Artefacts, findings, links and notes on one axis, filterable by day. Artefacts carry `observed_at`; the other records carry when they were written, and the page says which clock each row is on. |
| `/case/{id}/report` | The stage manifest as a table and the generated `REPORT.md`, with `manifest.json` and `REPORT.md` downloadable. The console used to show the result of a run but not the run. |
| `/case/{id}/export.csv` | The whole case as one table, next to the JSON API and the evidence PDF. |

The CSV route is the one with a security decision in it. Every string in a case was read off an
onion service, so a cell beginning `=`, `+`, `-` or `@` is a formula to Excel and to most
importers. The export prefixes such a cell with an apostrophe, and a test posts a note beginning
`=HYPERLINK(...)` and asserts it arrives inert. A tool that exports text written by the people it
investigates has to assume the spreadsheet is the next place it will be opened.

### A note on what none of this claims

The farm measures our own detectors against our own ground truth, both of which are ours.
The gates enforce a policy under an identity they do not verify, and their audit trail is
tamper-evident rather than tamper-proof. Neither shows that any of these techniques work on
a live network. That claim needs the Part E2.2 pilot and the deployment gates in a real
deployment, and until then every number and every guarantee in this repository is a
statement about a fixture and a boundary we drew ourselves.


## One command that runs everything

```bash
python3 scripts/full_check.py                # terminal
python3 scripts/full_check.py --live --html out/full-check.html   # page that fills in as it goes
```

Ten stages in order: the test suite, the paper verifier, the diagrams, the deck, the pipeline
end to end, the base-rate sensitivity check, the certificate-transparency ground truth, the
gates' refusals, the execution environment, and certificate transparency against real logs.
*The manuscript is not distributed with this repository.* `paper/` is a local artefact, so the
paper stage runs only on a machine that has that build; the project code, tests and data drop
stand on their own.
(The count is checked against the report by `tests/test_docs.py`, so this list and the run
have to agree.) Exit code is 0 only when every stage passed, and a stage whose precondition is
absent reports SKIP rather than PASS, because an unrun check that reports success is the
failure mode the script exists to catch.

**A stale result file can block its own replacement.** `out/full-check.json` is both written by
this script and read by `tests/test_docs.py`, which refuses to let the M2 report claim a clean
check the project has not recorded. A run that ends dirty therefore fails its own test suite
stage on the next attempt, and no amount of re-running makes the file clean again. To record a
fresh clean check, move the old file aside first, then run:

```bash
mv out/full-check.json /tmp/full-check.stale.json   # so the ratchet skips instead of failing
python3 scripts/full_check.py                       # with Tor running, for the two live stages
```

Removing a stale artefact is not editing evidence: the replacement is produced by the run.
Hand-editing the file to make it pass would be, and the ratchet exists to stop exactly that.

### The stage has to say it passed

Each stage ends by printing its own verdict on the last line of standard output:

```
VERDICT: 5 diagrams measured in a browser, 0 layout problems
```

A stage that exits 0 without printing one is **UNPROVEN**, never PASS, and an unproven stage
fails the run. A return code says a program finished. It does not say the program measured
anything, and this repository has twice shipped a stage that exited 0 while reporting a
refusal as an empty result. Success is therefore a claim the stage makes, not a conclusion the
harness draws from `$?`.

Two stages keep their own format. The test suite is read from pytest's summary line, strictly:
a failure count anywhere in the output, or fewer than 440 tests collected, means no verdict, so
a suite that silently shrank cannot pass. The pipeline is read from `manifest.json` on disk
rather than from what it prints, because the last line of its console output is a closing
brace.

`tests/test_full_check.py` pins this from both sides, including the case that matters: `true`
exits 0, and the harness must call it UNPROVEN.

A **SKIP is not a pass**, and the first version of this harness quietly disagreed with itself
about that. It printed `ALL CHECKS PASSED` whenever no stage had *failed*, so a run where the
two live stages never started showed a green banner over two amber rows, printed
`verdict: all checks passed`, and exited 0. It was found by reading `out/full-check.json` and
finding no verdict field in it at all, which is how a green run hides a broken claim. The
banner now reads `8 PASSED, 2 SKIPPED`, the exit code is non-zero, and the verdict says the
run is incomplete and names the stages that measured nothing. The JSON carries the same
verdict as the page, plus `passed` and `incomplete`, so nothing downstream has to infer
success from a list of rows. Starting Tor with `scripts/tor_disposable.sh start` is what turns
those two stages back into evidence.

### It found two things while being written

`scripts/verify_diagrams.py` launched Chromium on a fixed port; a browser left running from an
earlier session held it, the verifier's own browser failed to bind, and the debugger URL
answered for the *old* window. That page has no width on its svg, so all 525 labels measured as
off-canvas -- confident, meaningless numbers. The port is now chosen free at runtime, the
target is checked to be the file this run opened, and a zero-sized canvas or a page with no
text at all is an error instead of a verdict.

It also caught the harness's own omission: after the contract landed, the diagram stage came
back UNPROVEN because its verdict line had not been written yet. That is the check working.


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

## Is the demo case actually right?

The console shows a case, and a case with twelve links on it looks the same whether those
twelve are the twelve true pairs or twelve plausible-looking wrong ones. So the demo case is
scored against the ground truth it was generated from, item by item:

```bash
python3 scripts/qa/demo_accuracy.py                  # prints the report, exit 0 whenever it ran
python3 scripts/qa/demo_accuracy.py --json out/demo/demo-accuracy.json
```

It answers four questions, and each one names the wrong answers rather than counting them:
every proposed link against the real operator clusters (precision and recall, with false
proposals listed alongside the artefacts that supported them and missed pairs listed by
address); whether any link rests *only* on a widespread artefact, which is this project's
headline failure; every injected single-service flaw against the findings, with the missed
ones listed; and whether anything was linked to a service that has no cluster-mate at all,
which is what the abstention cases exist to catch.

Measured on 2026-11-11, `CASE-DEMO` against `out/farm.json` (seed 20260925):

```
links
  proposed 12, of which true 12, false 0
  true pairs in the farm 12, of which found 12, missed 0
  precision 1.000   recall 1.000
  0 proposals are supported only by hard-negative class artefacts
  no link touches a singleton service, which is what the abstention cases test

findings
  recorded 19, explained by a single-service injected flaw 19 of 19
  18 further injected entries are cluster-scope shared artefacts, which are linking
  evidence and are scored above, not findings
  no finding fires on a service with nothing injected
```

`tests/test_demo_accuracy.py` runs the same scoring and pins those numbers, so the demo
cannot quietly stop being right. The farm's correlation ground truth is synthetic: these
numbers say the pipeline is right about a world that was built to be scored, not about a world
nobody has labelled.

## Rebuilding the deck

The deck is thirteen slides, one idea each, built around a drawn figure rather than a block of
text. The console slide now shows a case created through the browser form, and a Module 3 slide
covers checksum-validated identifiers, trust edges and the label-free corroboration rule. Three
scripts produce it, and each one refuses to guess:

```bash
python scripts/build_diagrams.py                          # five diagrams -> out/fig/*.png
python -m dashboard &                                     # the console slide needs it
python scripts/capture_dashboard.py --case CASE-DECK --selector '#graph' --width 1500 --height 1400
python scripts/build_presentation.py --out deck.pptx      # assemble and audit the text
python scripts/verify_deck.py deck.pptx                   # bounds and text fit
python scripts/analyse_slides.py out/preview/slides     # measure the rendered pixels
python scripts/export_notes.py deck.pptx notes.md        # speaking notes, one per slide
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

The console is captured as one element, not as a whole page. A full-page capture puts about
1920 CSS pixels across the slide, which lands the console's 10 to 13 pixel labels at roughly
3.5pt, unreadable past the front row. Cropping to the graph panel at a 900 pixel viewport
makes the same labels about 7pt, and the capture refuses an element wider than 1000 CSS pixels
for exactly that reason.

`analyse_slides.py` reads the rendered PNGs for what geometry cannot see: a slide that is
nearly blank because a figure failed to load, content running into an edge, and one slide
sitting on a different left margin from the rest. It judges none of the taste, only the
mistakes.

`export_notes.py` writes the speaker notes beside the deck, titled by each slide's real
heading. Taking the first text box would title four sections MODULE 2, VALIDATION,
LIMITATIONS and IN ONE SLIDE, because that box is the small kicker above the title.

Every number on the diagrams is read from `out/eval-results.json` at build time, so a figure
on a slide cannot disagree with the file it came from. That path is gitignored scratch, so
`data/eval-results.json` holds the same file frozen at the commit that produced the paper and
is used when a clean checkout has no `out/`. A freshly generated `out/` always wins.
