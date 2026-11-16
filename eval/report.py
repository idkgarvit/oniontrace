"""Render an evaluation result document as Markdown.

The report is written so that the **random baseline and the sample size sit next to
every headline number**. That is not decoration: a result whose baseline is not stated
invites the reader to assume zero, and a percentage without its n invites them to assume
the interval is narrow. Both assumptions would flatter the system.
"""

from __future__ import annotations

from typing import Any


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _m1(module: dict[str, Any]) -> dict[str, Any]:
    return module if isinstance(module, dict) else {}


def check(module: dict[str, Any], name: str) -> str:
    """Render one M1 seam check as pass/fail text."""
    checks = _m1(module).get("checks", {})
    return "pass" if checks.get(name) else "**FAIL**"


def first_stored(module: dict[str, Any]) -> str:
    checks = _m1(module).get("checks", {})
    stored = checks.get("first_run_stored", 0)
    return f"{stored} snapshots" if stored else "**FAIL: stored nothing**"


def _ci(payload: dict[str, Any] | None) -> str:
    if not payload or "ci95" not in payload:
        return "n/a"
    low, high = payload["ci95"]
    return f"[{low * 100:.1f}, {high * 100:.1f}]"


def render(results: dict[str, Any]) -> str:
    """Render the results document as a Markdown report."""
    meta = results["meta"]
    split = results["split"]
    modules = results["modules"]
    out: list[str] = []

    out += [
        "# ONIONTRACE evaluation results",
        "",
        f"- Seed: `{meta['seed']}`  |  farm fingerprint: `{meta['farm_fingerprint']}`",
        f"- Harness: v{meta['harness_version']}  |  precision floor: {meta['precision_floor']}",
        f"- Reproduce: `{meta['reproduction_command']}`",
        "",
        "> **Scored here:** M1 collection integrity (fixture seam), M2 rules, M2 correlation, "
        "M3 entity resolution, M5 attribution, E4 provenance. **Not scored on live traffic:** "
        "the live fetcher is dark, so M1 is `fixture-verified`, never `live-tested`.",
        "",
        "> Every number below is reproducible from the seed above. Thresholds were fitted "
        "on the dev clusters and frozen before any test score existed.",
        "",
        "## M1 — Collection integrity",
        "",
        "Fixture-seam checks over a real gate and collector running in-memory pages, so "
        "the machinery -- permits, scope, cursors, WARC, budgets, the proposal queue -- is "
        "measured rather than asserted.",
        "",
        "| Check | Result |",
        "|---|---|---|",
        f"| scope refused before the request | {check(modules['M1_collection'], 'scope_refused_unsent')} |",
        f"| first run stored findings | {first_stored(modules['M1_collection'])} |",
        f"| second run stored nothing, fetched nothing | {check(modules['M1_collection'], 'second_run_idempotent')} |",
        f"| cursors saved where runs stopped | {check(modules['M1_collection'], 'checkpoints_saved')} |",
        f"| every snapshot points at its own WARC record | {check(modules['M1_collection'], 'warc_records_linked')} |",
        f"| the collector added no seed | {check(modules['M1_collection'], 'no_self_seeding')} |",
        "",
        "> This is a `fixture-verified` score. A pass means the collection machinery is "
        "gated and resumable; it does not mean live collection would succeed, and a report "
        "that drops this sentence is misrepresenting the number.",
        "",
        "## Held-out split",
        "",
        f"- **dev** (threshold fitting only): {', '.join(split['dev_clusters']) or 'none'}",
        f"- **test** (scored separately below): {', '.join(split['test_clusters']) or 'none'}",
        "",
        "The module tables below score the whole farm. The figures in this table are the "
        "same rules and the same frozen thresholds applied to the test clusters alone, "
        "with the candidate pool and the ground truth restricted to them: the only "
        "numbers here where every scored service was unseen when the thresholds were "
        "chosen.",
        "",
    ]

    held_out = results.get("held_out") or {}
    if held_out:
        test_rules = held_out["M2_misconfig_rules"]
        test_corr = held_out["M2_correlation"]["at_frozen_threshold"]["ranking"]
        farm_rules = modules["M2_misconfig_rules"]
        farm_corr = modules["M2_correlation"]["at_frozen_threshold"]["ranking"]

        def rule_cell(entry: dict[str, Any], metric: str) -> str:
            payload = entry.get(metric) or {}
            denominator = (entry["tp"] + entry["fn"]) if metric == "recall" else (
                entry["tp"] + entry["fp"]
            )
            return (
                f"{_pct(payload.get('value'))} {_ci(payload)} "
                f"({entry['tp']}/{denominator})"
            )

        def corr_cell(payload: dict[str, Any]) -> str:
            p1 = payload["precision@1"]
            return (
                f"{_pct(p1.get('value'))} {_ci(p1)} "
                f"({p1['successes']}/{p1['total']})"
            )

        out += [
            "| Measured on test clusters only | Result | Whole farm |",
            "|---|---|---|",
            f"| Careful-rule recall | {rule_cell(test_rules['careful_rules'], 'recall')} "
            f"| {rule_cell(farm_rules['careful_rules'], 'recall')} |",
            f"| Careful-rule precision | {rule_cell(test_rules['careful_rules'], 'precision')} "
            f"| {rule_cell(farm_rules['careful_rules'], 'precision')} |",
            f"| Naive-rule recall | {rule_cell(test_rules['naive_rules'], 'recall')} "
            f"| {rule_cell(farm_rules['naive_rules'], 'recall')} |",
            f"| Correlation precision@1 (frozen threshold) | {corr_cell(test_corr)} "
            f"| {corr_cell(farm_corr)} |",
            f"| Correlation MRR (frozen threshold) | {test_corr['mrr']:.3f} "
            f"| {farm_corr['mrr']:.3f} |",
            "",
            f"> {held_out['note']} The test half is "
            f"{held_out['query_count']} clustered services in "
            f"{len(held_out['clusters'])} clusters, which is small enough that the "
            "interval, not the point estimate, is the finding.",
            "",
        ]

    out += [
        "## Frozen thresholds",
        "",
        "| Threshold | Value | Fitted on | dev precision | dev recall | dev n |",
        "|---|---|---|---|---|---|",
    ]
    for threshold in results["thresholds"]:
        out.append(
            f"| `{threshold['name']}` | {threshold['value']:.4f} | "
            f"{threshold['fitted_on']} | {threshold['dev_precision']:.3f} | "
            f"{threshold['dev_recall']:.3f} | {threshold['dev_n']} |"
        )

    # --- M2 rules ------------------------------------------------------------- #
    rules = modules["M2_misconfig_rules"]
    out += [
        "",
        "## M2 — Misconfiguration rules",
        "",
        "Both rule tables are scored against the same ground truth. The gap between them "
        "is what shows the negative fixtures can fail a careless rule set, and therefore "
        "that the careful table's score is meaningful rather than vacuous.",
        "",
        "| Rule set | Recall | Precision | TP | FP | FN |",
        "|---|---|---|---|---|---|",
    ]
    for label, key in (("careful", "careful_rules"), ("naive (substring)", "naive_rules")):
        entry = rules[key]
        recall = entry.get("recall") or {}
        precision = entry.get("precision") or {}
        out.append(
            f"| {label} | {_pct(recall.get('value'))} {_ci(recall)} "
            f"| {_pct(precision.get('value'))} | {entry['tp']} | {entry['fp']} "
            f"| {entry['fn']} |"
        )
    out += [
        "",
        f"Negative fixtures implemented for {len(rules['rules_with_hard_negative_fixture'])} "
        "of the single-scope rules; the rest are listed under limitations.",
        "",
    ]

    # --- M2 correlation ------------------------------------------------------- #
    corr = modules["M2_correlation"]
    out += [
        "## M2 — Cross-service correlation",
        "",
        "| Variant | precision@1 | 95% CI | n | MRR | random baseline |",
        "|---|---|---|---|---|---|",
    ]
    for label, key in (
        ("naive", "naive"),
        ("frequency-filtered", "frequency_filtered"),
    ):
        ranking = corr[key]["ranking"]
        p1 = ranking["precision@1"]
        out.append(
            f"| {label} | {_pct(p1['value'])} | {_ci(p1)} | {ranking['n_queries']} "
            f"| {ranking['mrr']:.3f} | {_pct(ranking['random_baseline_precision@1'])} |"
        )

    out += [
        "",
        "Candidate-link scoring, which is where a correlator that links everything is "
        "caught. Ranking can look respectable while a large number of spurious links are "
        "proposed, so both are reported.",
        "",
        "| Variant | proposed | TP | FP | precision | false-merge rate |",
        "|---|---|---|---|---|---|",
    ]
    for label, key in (
        ("naive", "naive"),
        ("frequency-filtered", "frequency_filtered"),
        ("naive at frozen threshold", "at_frozen_threshold"),
    ):
        links = corr[key]["link_scoring"]
        out.append(
            f"| {label} | {links['proposed']} | {links['tp']} | {links['fp']} "
            f"| {_pct(links['precision'])} | {_pct(links['false_merge_rate'])} |"
        )
    out += [
        "",
        f"Frequency cap: `{corr['frequency_cap']}` (derived from cluster size, not tuned "
        "on results).",
        "",
    ]

    # --- M3 entity resolution ------------------------------------------------- #
    entity = modules["M3_entity_resolution"]
    out += [
        "## M3 — Entity resolution",
        "",
        "Precision-dominant by construction: `false_merge_rate = FP/(TP+FP)`, so the two "
        "targets the earlier plan stated separately were one target stated twice.",
        "",
        "| Variant | TP | FP | precision | recall | false-merge rate | decoy false merges |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, key in (
        ("exact match", "exact_match"),
        ("placeholder-blocklisted", "placeholder_blocklisted"),
    ):
        links = entity[key]["links"]
        out.append(
            f"| {label} | {links['tp']} | {links['fp']} | {_pct(links['precision'])} "
            f"| {_pct(links['recall'])} | {_pct(links['false_merge_rate'])} "
            f"| {entity[key]['false_merges_on_decoys']}/{entity[key]['decoy_false_pairs']} |"
        )

    # --- M5 attribution ------------------------------------------------------- #
    attribution = modules["M5_attribution"]
    out += [
        "",
        "## M5 — Attribution (baseline)",
        "",
        f"**{attribution['scope_limit']}**",
        "",
        "| Model | long recall@5 | long n | short hit@3 | short n | short random baseline |",
        "|---|---|---|---|---|---|",
    ]
    for model, entry in attribution["models"].items():
        out.append(
            f"| `{model}` | {_pct(entry['long']['recall@5'])} | {entry['long']['n_queries']} "
            f"| {_pct(entry['short']['hit@3']['value'])} | {entry['short']['n_queries']} "
            f"| {_pct(entry['short']['random_baseline']['hit@3'])} |"
        )
    out += [
        "",
        "The length split is required, not cosmetic: a single figure over mixed lengths "
        "hides that short posts carry far less authorship signal.",
        "",
        "Style drift (D3) is the sharper test. Queries are split by whether any held-out "
        "post was written after the persona's *recorded* drift point, so the pre-drift "
        "column is a control group rather than a second score:",
        "",
        "| Model | post-drift hit@3 | n | pre-drift hit@3 (control) | n |",
        "|---|---|---|---|---|",
    ]
    for model, entry in attribution["models"].items():
        drift = entry["drift_breakdown"]
        drifted = drift["drifted_corpus_queries"]["hit@3"]
        stable = drift["stable_corpus_queries"]["hit@3"]
        out.append(
            f"| `{model}` | {_pct(drifted['value'])} | {drifted['total']} "
            f"| {_pct(stable['value'])} | {stable['total']} |"
        )
    out += [
        "",
        "Read as a crossover, not as two rankings. A model that is simply weak everywhere "
        "would look drift-robust on the left-hand column alone; it is the control column "
        "that rules that out, and it is why the split is reported rather than the single "
        "aggregate it is often collapsed into.",
    ]

    # --- M5 by writing regime -------------------------------------------------- #
    # The code-mix block is the project's differentiator, and a lift reported only in aggregate
    # cannot show that the block helps where it is supposed to and does not hurt the controls.
    # Grouped here rather than in a note so the claim has a command behind it: if a regime is
    # missing from this table, the block was never measured on it.
    base = attribution["models"].get("combined")
    gated = attribution["models"].get("combined_gated")
    if base and gated:
        regimes = sorted(set(base.get("style_breakdown") or {}))
        out += [
            "",
            "### By writing regime (why the code-mix block exists)",
            "",
            "`combined` is function words plus behaviour; `combined_gated` adds code-mix features "
            "only when the corpus that was observed actually mixes. Each row's `n` is the number "
            "of queries in that regime, and a row with a small `n` moves a whole query at a time.",
            "",
            "| Regime | n | `combined` hit@1 | `combined_gated` hit@1 | change |",
            "|---|---|---|---|---|",
        ]
        for regime in regimes:
            a = (base["style_breakdown"][regime]["hit@1"])
            b = (gated["style_breakdown"][regime]["hit@1"])
            av = a["value"] if isinstance(a, dict) else a
            bv = b["value"] if isinstance(b, dict) else b
            out.append(
                f"| `{regime}` | {base['style_breakdown'][regime]['n_queries']} "
                f"| {_pct(av)} | {_pct(bv)} | {bv - av:+.3f} |"
            )
        out += [
            "",
            "A `pure_english` row that moves is not a contradiction: the gate changes how the "
            "**candidates** are represented, so a monolingual query against a mixed pool can "
            "shift by a query or two. That is why `n` sits beside every row.",
        ]

    # --- E4 provenance -------------------------------------------------------- #
    provenance_result = modules["E4_provenance"]
    verification = provenance_result["verification"]
    tamper = provenance_result["tamper_self_test"]
    out += [
        "",
        "## E4 — Provenance and tamper detection",
        "",
        f"- Ledger entries: {provenance_result['ledger_entry_count']}  |  "
        f"manifest: `{provenance_result['manifest_sha256'][:16]}...`",
        f"- Verification clean: **{verification['verified']}** "
        f"({verification['entries_checked']} entries checked)",
        f"- Tamper self-test: **{tamper['conclusion']}** "
        f"(`{tamper['mutated']}`, {tamper['mismatch_count']} mismatch detected)",
        f"- Assumption: {provenance_result['assumptions']}",
        "",
        "## Abstention",
        "",
        "| Run | Thresholded | Correct abstentions | False assertions | Rate |",
        "|---|---|---|---|---|",
    ]
    for key, entry in results["abstention"].items():
        if key == "note":
            continue
        scoring = entry["scoring"]
        out.append(
            f"| `{key}` | {entry.get('thresholded', False)} "
            f"| {scoring['correct_abstentions']}/{scoring['total']} "
            f"| {scoring['false_assertions']} | {_pct(scoring['abstention_rate'])} |"
        )
    out += [
        "",
        "> A false assertion is the system answering a question it cannot answer. For a "
        "tool that proposes candidate identities this is the failure that produces a "
        "wrong name.",
        "",
        "## Stated limitations",
        "",
    ]
    for index, note in enumerate(results["limitations"], start=1):
        out.append(f"{index}. {note}")

    out += [
        "",
        "---",
        "",
        "Generated by the evaluation harness. Numbers without the seed and the command "
        "above are opinions.",
        "",
    ]
    return "\n".join(out)
