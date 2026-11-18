# ONIONTRACE

ONIONTRACE is a passive OSINT pipeline for Tor onion services. It collects only what an
anonymous visitor can see, extracts identifiers and shared artefacts, detects
misconfigurations, proposes links between services, and presents the evidence in an analyst
console. Every proposed link carries a score and the check that produced it: the tool
supplies leads, not identity claims.

> **Authorised use only.** Collection is passive and never exploits a target. Nothing in
> this repository contacts a third party's onion service by default. Read
> [`ACCEPTABLE_USE.md`](ACCEPTABLE_USE.md) before running it against anything you do not own.

## What it does

- **Collect** — front pages, directory walks, `Onion-Location` pairs, a public search index,
  certificate transparency, and public Telegram previews, behind signed permits and budgets.
- **Extract** — PGP fingerprints, BTC/XMR addresses, emails, analytics IDs, hosted resources
  and TLS certificate fields, each as a typed artefact with provenance.
- **Detect** — the deployment mistakes that make services findable: version banners, default
  pages, exposed panels, missing security headers.
- **Correlate** — links services on shared artefacts, with a frequency cap so a value that
  appears on half the network cannot merge unrelated operators.
- **Attribute** — stylometric retrieval over generated personas, reported as synthetic
  retrieval rather than authorship of a real person.
- **Graph** — a per-case identity graph with evidence, trust edges, analyst decisions and an
  append-only audit trail.

## Requirements

- Python 3.10 or newer.
- No third-party packages for the core pipeline, farm, evaluation or gates.
- Optional extras in `requirements.txt` for live collection, certificate transparency, the
  console and the QA scripts.

## Install

```bash
git clone https://github.com/idkgarvit/oniontrace.git
cd oniontrace
python -m pip install -r requirements.txt   # optional, for the console and live paths
python -m pytest -q                          # 838 tests
```

## Quick start

```bash
# The analyst console in its own window (loopback only)
run/oniontrace-gui
run/oniontrace-gui --case CASE-1

# The whole pipeline in one command: farm -> gates -> detect -> correlate -> graph
python -m oniontrace run --seed 20260925 --case CASE-1
# writes out/CASE-1-20260925/manifest.json and casework/CASE-1/
```

The console is the main interface: `run/oniontrace-gui` starts a local service on a free
loopback port, opens it in a Chromium app window, and stops the service when the window
closes. `python -m dashboard` serves the same console for a headless machine.

## Useful commands

```bash
python -m farm validate                          # check the generated farm is consistent
python -m farm generate --seed 20260925 --out out
python -m farm summary --seed 20260925

python -m gates --root casework ack --accept --actor analyst --role case_owner
python -m collect targets --seed 20260925        # what a fixture run can serve
python -m collect --root casework run --case CASE-1 --actor analyst \
    --role case_owner --fixtures --search-query marketplace

python -m eval run --seed 20260925 --out out     # score every module against the farm
python -m eval report --results out/eval-results.json
```

## Layout

```
farm/        deterministic synthetic farm: services, flaws, personas, ground truth
collect/     passive collection paths, permits, budgets, WARC and checkpoints
eval/        metrics, splits, detectors and the evaluation harness
gates/       cases, authorisation, retention, audit, exports, acceptable use
graph/       identity graph, artefact resolution and trust edges
dashboard/   analyst console (FastAPI + Jinja)
scripts/     QA, verification and data-generation tooling
tests/       unit and integration tests
data/        frozen evidence used by the tests
```

Generated output goes to `out/`; case data goes to `casework/`. Both are gitignored.

## Evaluation

There is no public ground truth for who operates which onion service, so correlation is
measured on a farm where the operator clusters are known by construction. On seed
`20260925` a naive correlator proposes 58 links at precision 0.207; freezing a score
threshold lifts that to 0.571, and a frequency cap derived from cluster size reaches 12/12
with a false-merge rate of 0.000. The result repeats on 20 seeds and six farm topologies.

The real-data checks are deliberately narrower: 3,018 certificates from certificate
transparency, eight onion services the project published itself, 112/112 responses, and no
third-party onion service fetched. See `python -m eval run` for the full report.

## Tests

```bash
python -m pytest -q
```

The suite covers the farm, collection fixtures, gates, graph, evaluation and the console.

## License

MIT. See [`LICENSE`](LICENSE).
