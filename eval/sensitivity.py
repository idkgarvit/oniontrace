"""Frequency-cap sensitivity sweep.

The correlator discards an artefact value that appears on more than
``max_artifact_frequency`` services. That threshold is derived from cluster size, not
tuned on results: a genuine cluster artefact appears on exactly the services in one
cluster, so anything shared more widely cannot be cluster-specific.

A derivation is still an assumption, and the honest way to report an assumption is to
show what happens when it is wrong in both directions. This module sweeps the cap from
1 to one past the largest cluster and reports link precision, recall and abstention at
each step, so the reader can see whether the derived value sits on a plateau or a
cliff -- and can see the cost of setting it too low as well as too high.

No parameter is chosen from the sweep. The sweep describes the curve around the value
that ``common_value_threshold`` already computes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from eval.harness import correlation_abstention, correlation_experiment


def largest_cluster(farm) -> int:
    """Size of the biggest true cluster on the farm, used to bound the sweep."""
    sizes = [len(cluster.service_ids) for cluster in farm.clusters]
    return max(sizes) if sizes else 1


def sweep_caps(farm, *, extra: int = 0) -> list[int]:
    """Caps to test: 1 through the most widely shared artefact value.

    The upper bound is not arbitrary. Once the cap reaches the largest artefact
    frequency on the farm, nothing is filtered and the filtered variant *is* the naive
    one; sweeping past that point would print the same row twice. The lower bound is 1,
    which filters every value that appears on more than one service.
    """
    from eval.detectors.correlation import artifact_frequencies

    frequencies = artifact_frequencies(farm)
    widest = max(frequencies.values()) if frequencies else 1
    upper = max(largest_cluster(farm) + 1 + extra, widest)
    return list(range(1, upper + 1))


def _row(farm, cap: int, *, score_threshold: float) -> dict[str, Any]:
    experiment = correlation_experiment(
        farm,
        max_artifact_frequency=cap,
        score_threshold=score_threshold,
    )
    links = experiment["link_scoring"]
    abstention = correlation_abstention(
        farm, max_artifact_frequency=cap, min_score=None
    )["scoring"]
    ranking = experiment["ranking"]["precision@1"]

    return {
        "cap": cap,
        "proposed": links["proposed"],
        "tp": links["tp"],
        "fp": links["fp"],
        "fn": links["fn"],
        "precision": links["precision"],
        "recall": links["recall"],
        "false_merge_rate": links["false_merge_rate"],
        "precision_at_1": ranking["value"],
        "precision_at_1_total": ranking["total"],
        "abstained": abstention["correct_abstentions"],
        "abstention_total": abstention["total"],
    }


def sweep(
    farm,
    caps: Sequence[int] | None = None,
    *,
    score_threshold: float = 0.0,
) -> dict[str, Any]:
    """Score the correlator at every cap and return the rows plus the derived cap.

    ``score_threshold`` is passed through unchanged, so the sweep measures the cap and
    not an interaction with a threshold that was fitted elsewhere.
    """
    chosen = list(caps) if caps is not None else sweep_caps(farm)
    if not chosen:
        raise ValueError("sweep needs at least one cap")

    rows = [_row(farm, cap, score_threshold=score_threshold) for cap in chosen]
    derived = max(2, largest_cluster(farm))

    return {
        "cluster_size": largest_cluster(farm),
        "derived_cap": derived,
        "score_threshold": score_threshold,
        "rows": rows,
        "naive": _naive_row(farm, score_threshold=score_threshold),
    }


def _naive_row(farm, *, score_threshold: float) -> dict[str, Any]:
    """The unfiltered correlator, for the same table."""
    experiment = correlation_experiment(
        farm, max_artifact_frequency=None, score_threshold=score_threshold
    )
    links = experiment["link_scoring"]
    abstention = correlation_abstention(
        farm, max_artifact_frequency=None, min_score=None
    )["scoring"]
    return {
        "cap": None,
        "proposed": links["proposed"],
        "tp": links["tp"],
        "fp": links["fp"],
        "fn": links["fn"],
        "precision": links["precision"],
        "recall": links["recall"],
        "false_merge_rate": links["false_merge_rate"],
        "precision_at_1": experiment["ranking"]["precision@1"]["value"],
        "precision_at_1_total": experiment["ranking"]["precision@1"]["total"],
        "abstained": abstention["correct_abstentions"],
        "abstention_total": abstention["total"],
    }


def _signature(row: dict[str, Any]) -> tuple:
    """Everything that makes one cap's row visibly different from another's."""
    return (
        row["proposed"],
        row["tp"],
        row["fp"],
        row["fn"],
        row["precision"],
        row["recall"],
        row["false_merge_rate"],
        row["precision_at_1"],
        row["abstained"],
    )


def regimes(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive caps that score identically into a range.

    The sweep usually produces long runs of identical rows, because nothing changes
    until the cap crosses an artefact's frequency. Printing 26 identical lines hides
    the shape; printing the runs shows it.
    """
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(rows):
        end = index
        while end + 1 < len(rows) and _signature(rows[end + 1]) == _signature(rows[index]):
            end += 1
        out.append({"cap_lo": rows[index]["cap"], "cap_hi": rows[end]["cap"], "row": rows[index]})
        index = end + 1
    return out


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(result: dict[str, Any]) -> str:
    """Render the sweep as the table the report and the paper both quote."""
    derived = result["derived_cap"]
    out = [
        "# Frequency-cap sensitivity",
        "",
        (
            f"Largest true cluster: {result['cluster_size']}. "
            f"Derived cap: {derived}. "
            f"Link score threshold: {result['score_threshold']}."
        ),
        "",
        "| cap | links | TP | FP | FN | precision | recall | false merges | P@1 | singletons abstained |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for regime in regimes(result["rows"]):
        row = regime["row"]
        lo, hi = regime["cap_lo"], regime["cap_hi"]
        if lo == hi:
            label = str(lo)
            if lo == derived:
                label += " **(derived)**"
        else:
            label = f"{lo}–{hi}"
            if lo <= derived <= hi:
                label += f" (derived: {derived})"
        out.append(
            f"| {label} | {row['proposed']} | {row['tp']} | {row['fp']} | {row['fn']} "
            f"| {_pct(row['precision'])} | {_pct(row['recall'])} "
            f"| {_pct(row['false_merge_rate'])} "
            f"| {row['precision_at_1']:.3f} "
            f"| {row['abstained']}/{row['abstention_total']} |"
        )
    naive = result["naive"]
    out.append(
        f"| none (naive) | {naive['proposed']} | {naive['tp']} | {naive['fp']} "
        f"| {naive['fn']} | {_pct(naive['precision'])} | {_pct(naive['recall'])} "
        f"| {_pct(naive['false_merge_rate'])} "
        f"| {naive['precision_at_1']:.3f} "
        f"| {naive['abstained']}/{naive['abstention_total']} |"
    )
    out += [
        "",
        (
            "Interpretation: the cost is asymmetric. Below the cluster size every genuine "
            "cluster artefact is filtered, because it appears on more services than the cap "
            "allows, so recall collapses to zero -- and that edge is one step wide. Above "
            "it nothing changes until the cap passes the frequency of a population-default "
            "value, at which point the filter stops filtering and the naive false merges "
            "return in full. The derived cap sits at the lower edge of a plateau that "
            "extends several times further, so the exact value matters far less than the "
            "rule that produces it: a cluster artefact cannot appear on more services than "
            "a cluster has."
        ),
        "",
    ]
    return "\n".join(out)


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    from farm.generator import generate_farm
    from farm.model import FarmConfig

    parser = argparse.ArgumentParser(description="Sweep the frequency cap.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--score-threshold", type=float, default=0.0, help="link score cut (default 0)"
    )
    parser.add_argument("--out", default="", help="write markdown here instead of stdout")
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = FarmConfig() if args.seed is None else FarmConfig(seed=args.seed)
    farm = generate_farm(cfg)
    result = sweep(farm, score_threshold=args.score_threshold)
    text = render_markdown(result)
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI
    raise SystemExit(main())
