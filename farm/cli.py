"""Command-line interface for the ONIONTRACE synthetic farm.

Usage
-----
    python -m farm generate --seed 20260925 --out ./out
    python -m farm summary  --seed 20260925
    python -m farm validate

Every number the farm reports comes with the seed that produced it, so a reviewer can
regenerate the exact farm and re-derive the number (Part E).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from farm.catalogue import catalogue_summary
from farm.generator import generate_farm, validate_config
from farm.model import FarmConfig, FlawScope
from farm.services import unbuildable_flaw_kinds


def _summary_payload(farm) -> dict:
    """The machine-readable summary the evaluation harness and report both read."""
    return {
        "seed": farm.config.seed,
        "counts": {
            "services": len(farm.services),
            "clustered_services": sum(1 for s in farm.services if s.cluster_id),
            "singleton_services": sum(1 for s in farm.services if not s.cluster_id),
            "clusters": len(farm.clusters),
            "flaws_total": len(farm.flaws),
            "flaws_single": len(farm.flaws_by_scope(FlawScope.SINGLE)),
            "flaws_cluster": len(farm.flaws_by_scope(FlawScope.CLUSTER)),
            "hard_negatives": len(farm.hard_negatives),
            "personas": len(farm.personas),
            "subjects": sum(1 for p in farm.personas if p.role.value == "subject"),
            "near_miss_decoys": sum(
                1 for p in farm.personas if p.role.value == "near_miss_decoy"
            ),
            "abstention_cases": len(farm.abstention_cases),
            "clean_services": len(farm.clean_services()),
            "clean_control_endpoints": len(farm.clean_control_endpoints()),
        },
        "baselines": {
            "correlation_precision_at_1_restricted": round(
                farm.precision_at_1_baseline(), 6
            ),
            "correlation_precision_at_1_full": round(
                farm.precision_at_1_baseline(restricted_to_clustered=False), 6
            ),
            "retrieval": {f"k={k}": farm.retrieval_baselines(k) for k in (1, 3, 5, 10)},
        },
    }


def _write_report(path: Path, farm, summary: dict) -> None:
    """A human-readable summary for pasting into the report."""
    counts = summary["counts"]
    base = summary["baselines"]
    lines = [
        "# ONIONTRACE synthetic farm -- generated summary",
        "",
        f"Seed: `{farm.config.seed}` (regenerate with the command in `README.md`)",
        "",
        "## Composition",
        "",
        "| Quantity | Value |",
        "|---|---|",
    ]
    for key, value in counts.items():
        lines.append(f"| {key.replace('_', ' ')} | {value} |")

    lines += [
        "",
        "## Random baselines (Part E1: reported next to every result)",
        "",
        "| Metric | Random baseline |",
        "|---|---|",
        f"| correlation precision@1 (restricted pool) | "
        f"{base['correlation_precision_at_1_restricted']} |",
        f"| correlation precision@1 (full pool) | "
        f"{base['correlation_precision_at_1_full']} |",
    ]
    for k_label, payload in base["retrieval"].items():
        k = k_label.split("=")[1]
        lines.append(
            f"| retrieval hit@{k} (pool {int(payload['pool_size'])}) "
            f"| {round(payload['hit_at_k'], 4)} |"
        )

    lines += [
        "",
        "> Any reported score at or below these numbers is indistinguishable from chance.",
        "> The farm exists so that claim can be checked rather than asserted.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def cmd_generate(args: argparse.Namespace) -> int:
    cfg = FarmConfig(seed=args.seed)
    if args.posts:
        cfg.posts_per_persona = args.posts
    farm = generate_farm(cfg)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    summary = _summary_payload(farm)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "farm.json").write_text(farm.to_json(include_text=True), encoding="utf-8")
    (out / "ground-truth.json").write_text(
        farm.to_json(include_text=False, indent=2), encoding="utf-8"
    )
    _write_report(out / "report.md", farm, summary)

    c = summary["counts"]
    print(f"wrote farm to {out}/")
    print("  summary.json       composition and random baselines")
    print("  farm.json          full ground truth including generated text")
    print("  ground-truth.json  ground truth without text (smaller)")
    print("  report.md          summary table for the report")
    print()
    print(
        f"  services {c['services']}, flaws {c['flaws_total']}, "
        f"personas {c['personas']}, hard negatives {c['hard_negatives']}, "
        f"abstention {c['abstention_cases']}"
    )
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    farm = generate_farm(FarmConfig(seed=args.seed))
    print(json.dumps(_summary_payload(farm), indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Self-checks that must pass before any number from the farm is believed.

    These encode the Part D4 requirements as executable assertions: without decoys,
    hard negatives, clean controls, and abstention cases, the metrics the farm exists
    to support are either inflated or unmeasurable.
    """
    problems: list[str] = []

    missing = unbuildable_flaw_kinds()
    if missing:
        problems.append(f"catalogue kinds with no fixture path: {missing}")

    try:
        validate_config(FarmConfig())
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        problems.append(f"default config invalid: {exc}")

    farm = generate_farm()
    if not farm.flaws_by_scope(FlawScope.CLUSTER):
        problems.append("no CLUSTER-scope flaws: the correlator has no positive class")
    if not farm.hard_negatives:
        problems.append(
            "no hard negatives: precision would be measured against true positives only"
        )
    if not farm.clean_control_endpoints():
        problems.append("no clean control endpoints: rule FP rate has no denominator")
    if not [a for a in farm.abstention_cases if a.kind == "correlation"]:
        problems.append("no correlation abstention cases")
    if not [a for a in farm.abstention_cases if a.kind == "retrieval"]:
        problems.append("no retrieval abstention cases")

    # Determinism is a hard requirement, not a nicety.
    if generate_farm().to_json() != farm.to_json():
        problems.append("farm generation is not deterministic for a fixed seed")

    cat = catalogue_summary()
    print(
        f"catalogue: {cat['total']} flaws "
        f"({cat['single']} single, {cat['cluster']} cluster)"
    )
    print(f"rules without a hard negative: {cat['without_hard_negative'] or 'none'}")
    print()

    if problems:
        print("FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("PASS - all farm self-checks satisfied")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="farm",
        description="ONIONTRACE synthetic farm generator (deterministic, seed-driven).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="write a farm to disk")
    gen.add_argument("--seed", type=int, default=FarmConfig().seed)
    gen.add_argument("--out", type=str, default="./out")
    gen.add_argument("--posts", type=int, default=None, help="posts per persona")
    gen.set_defaults(func=cmd_generate)

    summ = sub.add_parser("summary", help="print composition and baselines as JSON")
    summ.add_argument("--seed", type=int, default=FarmConfig().seed)
    summ.set_defaults(func=cmd_summary)

    val = sub.add_parser("validate", help="run the farm self-checks")
    val.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())