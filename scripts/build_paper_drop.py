#!/usr/bin/env python3
"""Build the paper data drop: everything the researcher asked for, in raw form.

Run from the repo root:  python3 scripts/build_paper_drop.py

Writes under data/evidence/. Nothing here reaches a network, and nothing is summarised
away -- the point is that every number can be traced to a JSON path and a raw fraction.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DROP = ROOT / "data" / "evidence"
SEEDS = ("20260925", "20260926", "20260927")


def sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=120).stdout.strip()
    except Exception as exc:  # pragma: no cover
        return f"(failed: {exc})"


#: Results already generated in this process, keyed by seed. Regenerating is cheap, but
#: ``main`` asks for the same seed twice and there is no reason to pay for it twice.
_CACHE: dict[str, dict] = {}


def results_for(seed: str) -> dict:
    """Results for a seed, regenerating them rather than reading a stale file.

    The first version read fixed paths under /tmp, so it silently reused whatever an
    earlier, differently-configured run had left there -- and failed outright once /tmp
    was cleaned. Both were the same mistake: trusting a file nobody in this process wrote.
    Each seed is now run into a directory this process created and owns, so the numbers
    always come from the code as it stands.
    """
    if seed in _CACHE:
        return _CACHE[seed]

    primary = ROOT / "out" / "eval-results.json"
    if seed == SEEDS[0] and primary.exists():
        _CACHE[seed] = json.loads(primary.read_text())
        return _CACHE[seed]

    with tempfile.TemporaryDirectory(prefix=f"oniontrace-seed-{seed}-") as tmp:
        out = Path(tmp)
        run = subprocess.run(
            [sys.executable, "-m", "eval", "run", "--seed", seed, "--out", str(out)],
            cwd=ROOT, capture_output=True, text=True, timeout=900, check=False,
        )
        produced = out / "eval-results.json"
        if run.returncode != 0 or not produced.exists():
            raise SystemExit(
                f"eval run for seed {seed} failed (rc={run.returncode}):\n"
                f"{run.stderr.strip() or '(no stderr)'}"
            )
        _CACHE[seed] = json.loads(produced.read_text())
    return _CACHE[seed]


def write(rel: str, text: str) -> None:
    p = DROP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    print(f"  wrote {rel}  ({len(text):,} chars)")


def main() -> int:
    DROP.mkdir(parents=True, exist_ok=True)
    print("building paper data drop")

    # ---- (a) frozen bundle -------------------------------------------------- #
    for name in ("eval-results.json", "farm.json", "ground-truth.json", "summary.json",
                 "eval-report.md", "report.md"):
        src = ROOT / "out" / name
        if src.exists():
            shutil.copy2(src, DROP / "frozen" / name)
    (DROP / "frozen").mkdir(parents=True, exist_ok=True)
    for name in ("experiments.json", "experiments.md"):
        src = ROOT / "out" / name
        if src.exists():
            (DROP / "experiments").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, DROP / "experiments" / name)
    env = "\n".join([
        f"git rev-parse HEAD : {sh('git', 'rev-parse', 'HEAD')}",
        f"git status         : {sh('git', 'status', '--porcelain') or '(clean)'}",
        f"python             : {platform.python_version()} ({sys.executable})",
        f"platform           : {platform.platform()}",
        f"machine            : {platform.machine()}",
        "",
        "pip freeze:",
        sh(sys.executable, "-m", "pip", "freeze") or "(none)",
        "",
        "reproduce:  python -m eval run --seed 20260925 --out ./out",
        "reproduce:  python -m farm generate --seed 20260925 --out ./out",
        "reproduce:  python -m pytest -q",
    ])
    write("frozen/ENVIRONMENT.txt", env)

    # The limitations travel with every report, and a drop that quotes an older
    # limitation list is exactly the kind of stale evidence this project keeps finding.
    # Write them from the same results object the rest of the drop uses.
    limitations = results_for(SEEDS[0])["limitations"]
    write(
        "LIMITATIONS_VERBATIM.md",
        "# Limitations, verbatim from the harness output\n\n"
        "These are emitted by `eval/harness.py::_limitations()` and travel with every "
        "report. The researcher may quote them directly.\n\n"
        + "\n\n".join(f"{n}. {text}" for n, text in enumerate(limitations, 1))
        + "\n",
    )

    # ---- (b) per-number provenance table ------------------------------------ #
    d = results_for(SEEDS[0])
    c = d["modules"]["M2_correlation"]
    m = d["modules"]["M3_entity_resolution"]
    a = d["modules"]["M5_attribution"]["models"]
    r = d["modules"]["M2_misconfig_rules"]
    split = f"dev {','.join(d['split']['dev_clusters'])} / test {','.join(d['split']['test_clusters'])}"
    thr = {t["name"]: t for t in d["thresholds"]}

    def pct(v):
        return f"{v:.6f}"

    rows = [
        ("claim", "json_path", "raw_fraction", "value", "ci95", "n", "baseline", "split", "threshold"),
    ]
    def add(claim, path, sc, ci="", base=""):
        if "tp" in sc:
            tp, fp = sc.get("tp", 0), sc.get("fp", 0)
            raw = f"{tp}/{tp + fp}"
        elif "successes" in sc:
            raw = f"{sc['successes']}/{sc['total']}"
        elif "total" in sc and isinstance(sc.get("value"), float):
            # A metric stored as a bare ratio over a known n (long recall@5). Derive the
            # count rather than printing '?', which is what the first version emitted.
            n = sc["total"]
            raw = f"{round(sc['value'] * n)}/{n}"
        elif "n_queries" in sc and isinstance(sc.get("value"), float):
            n = sc["n_queries"]
            raw = f"{round(sc['value'] * n)}/{n}"
        else:
            raw = "?"
        rows.append((claim, path, raw, pct(sc.get("precision", sc.get("value", 0.0))),
                     str(ci), str(sc.get("total", sc.get("n_queries", sc.get("proposed", "")))),
                     str(base), split, "frozen before scoring"))

    add("naive link precision", "modules.M2_correlation.naive.link_scoring", c["naive"]["link_scoring"])
    add("naive at frozen threshold", "modules.M2_correlation.at_frozen_threshold.link_scoring", c["at_frozen_threshold"]["link_scoring"])
    add("frequency-filtered link precision", "modules.M2_correlation.frequency_filtered.link_scoring", c["frequency_filtered"]["link_scoring"])
    add("correlation p@1 naive", "modules.M2_correlation.naive.ranking.precision@1", c["naive"]["ranking"]["precision@1"],
        ci=c["naive"]["ranking"]["precision@1"].get("ci95"), base=c["naive"]["ranking"]["random_baseline_precision@1"])
    add("correlation p@1 filtered", "modules.M2_correlation.frequency_filtered.ranking.precision@1",
        c["frequency_filtered"]["ranking"]["precision@1"],
        ci=c["frequency_filtered"]["ranking"]["precision@1"].get("ci95"),
        base=c["frequency_filtered"]["ranking"]["random_baseline_precision@1"])
    add("M3 exact-match precision", "modules.M3_entity_resolution.exact_match.links", m["exact_match"]["links"])
    add("M3 placeholder-blocklisted precision", "modules.M3_entity_resolution.placeholder_blocklisted.links", m["placeholder_blocklisted"]["links"])
    for model in ("text_full", "text_function", "behaviour", "combined"):
        add(f"attribution {model} long recall@5", f"modules.M5_attribution.models.{model}.long.recall@5",
            {"value": a[model]["long"]["recall@5"], "total": a[model]["long"]["n_queries"]},
            base=a[model]["long"]["random_baseline"]["recall@5"])
        add(f"attribution {model} short hit@3", f"modules.M5_attribution.models.{model}.short.hit@3",
            a[model]["short"]["hit@3"], ci=a[model]["short"]["hit@3"].get("ci95"),
            base=a[model]["short"]["random_baseline"]["hit@3"])
        add(f"attribution {model} DRIFTED hit@3",
            f"modules.M5_attribution.models.{model}.drift_breakdown.drifted_corpus_queries.hit@3",
            a[model]["drift_breakdown"]["drifted_corpus_queries"]["hit@3"],
            ci=a[model]["drift_breakdown"]["drifted_corpus_queries"]["hit@3"].get("ci95"))
        add(f"attribution {model} STABLE hit@3 (control)",
            f"modules.M5_attribution.models.{model}.drift_breakdown.stable_corpus_queries.hit@3",
            a[model]["drift_breakdown"]["stable_corpus_queries"]["hit@3"])
    add("M2 careful rules recall", "modules.M2_misconfig_rules.careful_rules.recall", r["careful_rules"]["recall"], ci=r["careful_rules"]["recall"].get("ci95"))
    add("M2 careful rules precision", "modules.M2_misconfig_rules.careful_rules.precision", r["careful_rules"]["precision"])
    add("M2 naive rules recall", "modules.M2_misconfig_rules.naive_rules.recall", r["naive_rules"]["recall"])
    add("M2 naive rules precision", "modules.M2_misconfig_rules.naive_rules.precision", r["naive_rules"]["precision"])

    md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    for row in rows[1:]:
        md.append("| " + " | ".join(str(x) for x in row) + " |")
    # the two baselines, both stated
    md += ["", "**Both correlation baselines, stated rather than one:**", "",
           f"- restricted pool (2 true mates among 11 alternatives): {c['naive']['ranking']['random_baseline_precision@1_restricted']:.6f}",
           f"- full pool (2 among 13, singletons included): {c['naive']['ranking']['random_baseline_precision@1']:.6f}",
           "", f"- corpus-level baseline note: {c['naive']['ranking']['baseline_note']}"]
    md += ["", "**Frozen thresholds:**", ""]
    for name, t in thr.items():
        md.append(f"- `{name}` = {t['value']} | policy: {t['policy']} | fitted_on: {t['fitted_on']} | dev n={t['dev_n']}")
    write("PROVENANCE.md", "\n".join(md) + "\n")

    # ---- (c) farm spec ------------------------------------------------------- #
    cfg = d["meta"]
    spec = ["# Farm specification\n", "## Config (from eval-results meta)\n", "```json",
            json.dumps(cfg, indent=2)[:4000], "```", "",
            "## Generator source pointers\n",
            "| File | Role |", "|---|---|",
            "| `farm/model.py` | data model, FarmConfig, deterministic helpers, baselines |",
            "| `farm/generator.py` | orchestration; `generate_farm()` entry point |",
            "| `farm/catalogue.py` | the 25 D2 flaws and their negative rationales |",
            "| `farm/personas.py` | persona construction, corpora, style drift |",
            "| `farm/language.py` | vocabularies, sentence frames, listing templates |",
            "| `farm/services.py` | service content and flaw-injection fixtures |", ""]
    write("farm_spec/SPEC.md", "\n".join(spec))

    # ---- (h) new runs -------------------------------------------------------- #
    multis = []
    multis.append("| seed | farm fingerprint | naive precision | thresholded precision | frequency-filtered precision | false-merge |")
    multis.append("|---|---|---|---|---|---|")
    for s in SEEDS:
        dd = results_for(s)
        cc = dd["modules"]["M2_correlation"]
        multis.append("| {} | {} | {:.3f} | {:.3f} | {:.3f} | {:.3f} |".format(
            s, dd["meta"].get("farm_fingerprint", "?")[:12],
            cc["naive"]["link_scoring"]["precision"],
            cc["at_frozen_threshold"]["link_scoring"]["precision"],
            cc["frequency_filtered"]["link_scoring"]["precision"],
            cc["frequency_filtered"]["link_scoring"]["false_merge_rate"]))
    write("new_runs/MULTISEED.md", "\n".join(multis) + "\n")

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())