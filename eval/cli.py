"""Command-line interface for the evaluation harness.

Usage
-----
    python -m eval run --seed 20260925 --out ./out
    python -m eval report --results ./out/eval-results.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval import harness, report
from farm.model import FarmConfig


def cmd_run(args: argparse.Namespace) -> int:
    cfg = FarmConfig(seed=args.seed)
    results = harness.run(
        cfg,
        dev_fraction=args.dev_fraction,
        min_precision=args.precision_floor,
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval-results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    (out / "eval-report.md").write_text(report.render(results), encoding="utf-8")

    meta = results["meta"]
    print(f"wrote evaluation to {out}/")
    print("  eval-results.json  full results document")
    print("  eval-report.md     rendered report")
    print()
    print(f"  seed {meta['seed']}  farm {meta['farm_fingerprint']}")
    print(f"  dev clusters : {', '.join(results['split']['dev_clusters'])}")
    print(f"  test clusters: {', '.join(results['split']['test_clusters'])}")
    print()

    rules = results["modules"]["M2_misconfig_rules"]
    for label, key in (("careful", "careful_rules"), ("naive", "naive_rules")):
        entry = rules[key]
        precision = (entry.get("precision") or {}).get("value")
        print(
            f"  M2 rules ({label:7s}) recall={entry['recall']['value']:.3f} "
            f"precision={precision:.3f} fp={entry['fp']}"
        )

    correlation = results["modules"]["M2_correlation"]
    for label in ("naive", "frequency_filtered"):
        rank = correlation[label]["ranking"]["precision@1"]
        links = correlation[label]["link_scoring"]
        print(
            f"  M2 corr  ({label:17s}) p@1={rank['value']:.3f} "
            f"link_precision={links['precision']:.3f} "
            f"false_merge={links['false_merge_rate']:.3f}"
        )

    entity = results["modules"]["M3_entity_resolution"]
    for label in ("exact_match", "placeholder_blocklisted"):
        links = entity[label]["links"]
        print(
            f"  M3 entity({label:22s}) precision={links['precision']:.3f} "
            f"false_merge={links['false_merge_rate']:.3f}"
        )

    attribution = results["modules"]["M5_attribution"]
    for model, entry in attribution["models"].items():
        print(
            f"  M5 attr  ({model:13s}) long_r@5={entry['long']['recall@5']:.2f} "
            f"short_hit@3={entry['short']['hit@3']['value']:.2f} "
            f"(random {entry['short']['random_baseline']['hit@3']:.2f})"
        )

    provenance_result = results["modules"]["E4_provenance"]
    print(
        f"  E4 provenance verified={provenance_result['verification']['verified']} "
        f"tamper_test='{provenance_result['tamper_self_test']['conclusion']}'"
    )

    print()
    for key in ("retrieval_unthresholded", "retrieval_at_frozen_threshold"):
        scoring = results["abstention"][key]["scoring"]
        print(
            f"  abstention ({key:30s}) {scoring['correct_abstentions']}/"
            f"{scoring['total']} correct, {scoring['false_assertions']} false assertions"
        )

    print()
    print(f"  {len(results['limitations'])} stated limitations travel with the results")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = Path(args.results)
    results = json.loads(path.read_text(encoding="utf-8"))
    markdown = report.render(results)
    if args.out == "-":
        print(markdown)
    else:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval",
        description="ONIONTRACE evaluation harness: score the modules against the farm.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run the full evaluation")
    run_parser.add_argument("--seed", type=int, default=FarmConfig().seed)
    run_parser.add_argument("--out", type=str, default="./out")
    run_parser.add_argument(
        "--dev-fraction",
        type=float,
        default=0.5,
        dest="dev_fraction",
        help="fraction of clusters held out for threshold fitting",
    )
    run_parser.add_argument(
        "--precision-floor",
        type=float,
        default=0.95,
        dest="precision_floor",
        help="precision the frozen thresholds must hold on dev",
    )
    run_parser.set_defaults(func=cmd_run)

    report_parser = sub.add_parser("report", help="re-render a results file")
    report_parser.add_argument("--results", type=str, required=True)
    report_parser.add_argument("--out", type=str, default="-")
    report_parser.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())