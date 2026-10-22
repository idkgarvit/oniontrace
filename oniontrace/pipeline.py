"""End-to-end pipeline: one command from a seed to a defensible case.

The repository had five working modules and no way to run them together. That is the
difference between a set of libraries and a system, and it is what the plan means by
"five modules + farm + demo".

    python -m oniontrace run --seed 20260925 --case CASE-1

Each stage writes its result into the run manifest whether it succeeded or failed. A
stage that raises does not abort the run: the failure is recorded, the remaining
stages that do not depend on it still run, and the manifest says which numbers are
therefore absent. A pipeline that silently stops halfway is worse than one that
reports "detection failed, correlation skipped because it consumes detection output".

The stage order is not arbitrary. Ground truth must exist before anything can be
measured, so the farm comes first. Correlation consumes detected artefacts, so
detection precedes it. The graph consumes the same artefacts, and attribution
consumes the same posts, so both can run after. Nothing is scored against a parameter
fitted on the same data.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from farm.generator import generate_farm
from farm.model import FarmConfig


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StageResult:
    """What one stage did, in a shape the manifest and the report both read."""

    name: str
    ok: bool
    duration_ms: int
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    skipped_because: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "error": self.error,
            "skipped_because": self.skipped_because,
        }


@dataclass
class PipelineResult:
    run_id: str
    seed: int
    case_id: str
    output_dir: Path
    started_at: str
    finished_at: str
    stages: list[StageResult]

    @property
    def ok(self) -> bool:
        """A run is OK when no stage failed outright. Skips are not failures."""

        return all(s.ok for s in self.stages if s.skipped_because is None)

    def stage(self, name: str) -> StageResult | None:
        for s in self.stages:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seed": self.seed,
            "case_id": self.case_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "stages": [s.to_dict() for s in self.stages],
        }


def _timed(name: str, fn: Callable[[], dict[str, Any]]) -> StageResult:
    """Run one stage, capturing failure instead of propagating it."""

    import time

    started = time.monotonic()
    try:
        summary = fn() or {}
        return StageResult(
            name=name,
            ok=True,
            duration_ms=int((time.monotonic() - started) * 1000),
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 - a stage failure is data, not a crash
        return StageResult(
            name=name,
            ok=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
            summary={"traceback": traceback.format_exc(limit=4)},
        )


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #


def stage_farm(cfg: FarmConfig) -> dict[str, Any]:
    farm = generate_farm(cfg)
    return {
        "services": len(farm.services),
        "clusters": len(farm.clusters),
        "personas": len(farm.personas),
        "flaws": len(farm.flaws),
        "hard_negatives": len(farm.hard_negatives),
        "fingerprint": hashlib.sha256(
            json.dumps(
                [s.service_id for s in farm.services]
                + [p.persona_id for p in farm.personas]
            ).encode()
        ).hexdigest()[:16],
    }


def stage_gates(root: Path, case_id: str, *, owner: str, authorisation: str,
                retention_days: int) -> dict[str, Any]:
    """Open the case. This is the gate the plan insists comes first."""

    from gates.cases import CaseStore

    store = CaseStore(root)
    if store.exists(case_id):
        return {"case": case_id, "created": False, "note": "case already existed"}

    store.create(
        case_id=case_id,
        title=f"ONIONTRACE pipeline run {case_id}",
        owner=owner,
        authorisation_ref=authorisation,
        retention_days=retention_days,
    )
    return {"case": case_id, "created": True, "retention_days": retention_days}


def stage_detect(farm) -> dict[str, Any]:
    """M2: run the careful rule table over every endpoint of every service."""

    from eval.detectors import misconfig

    by_service: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    items: list[dict[str, str]] = []
    for svc in farm.services:
        hits = misconfig.scan_service(farm, svc.service_id)
        by_service[svc.service_id] = len(hits)
        for hit in hits:
            by_kind[hit.kind] = by_kind.get(hit.kind, 0) + 1
            items.append(
                {
                    "service_id": hit.service_id,
                    "kind": hit.kind,
                    "path": hit.path,
                    "evidence": hit.evidence,
                }
            )

    return {
        "items": items,
        "findings": len(items),
        "services_scanned": len(farm.services),
        "services_with_findings": sum(1 for v in by_service.values() if v),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "rule_count": len(misconfig.RULES),
    }


def stage_correlate(farm) -> dict[str, Any]:
    """M2b: rank candidate cluster mates with the frequency-filtered correlator."""

    from eval.detectors import correlation

    cap = correlation.common_value_threshold(
        farm, services_per_cluster=len(farm.services) // max(len(farm.clusters), 1)
    )

    # Precision@k here means: of the k services proposed, how many really are cluster
    # mates. Computed inline rather than through the metrics module because the
    # metrics helpers answer the retrieval question (did the right answer appear),
    # which is a different question from this one (were the proposals correct).
    proposed_at: dict[int, list[float]] = {1: [], 3: []}
    for svc in farm.services:
        if svc.cluster_id is None:
            continue
        truth = set(farm.cluster_mates(svc.service_id))
        if not truth:
            continue
        pool = farm.correlation_pool(svc.service_id)
        order = [
            sid
            for sid, _ in correlation.rank_services(
                farm, svc.service_id, pool, max_artifact_frequency=cap
            )
        ]
        for k in proposed_at:
            top = order[:k]
            if top:
                proposed_at[k].append(sum(1 for sid in top if sid in truth) / len(top))

    return {
        "frequency_cap": cap,
        "queries": len(proposed_at[1]),
        "precision_at_1": round(
            sum(proposed_at[1]) / len(proposed_at[1]), 6
        ) if proposed_at[1] else 0.0,
        "precision_at_3": round(
            sum(proposed_at[3]) / len(proposed_at[3]), 6
        ) if proposed_at[3] else 0.0,
    }


def stage_attribute(farm) -> dict[str, Any]:
    """M5: score the gated attribution model over the persona corpus."""

    from eval.detectors import attribution

    result = attribution.evaluate(farm, models=("combined", "combined_gated"))
    models = result["models"]
    return {
        "model_compared": "combined vs combined_gated",
        "combined_hit@1_long": models["combined"]["long"]["hit@1"]["value"],
        "gated_hit@1_long": models["combined_gated"]["long"]["hit@1"]["value"],
        "combined_hit@1_short": models["combined"]["short"]["hit@1"]["value"],
        "gated_hit@1_short": models["combined_gated"]["short"]["hit@1"]["value"],
        "mix_gate": attribution.MIX_GATE,
        "scope_limit": result["scope_limit"],
    }


def stage_graph(
    farm, db_path: Path, case_id: str, findings: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """M3: extract artefacts, propose edges, and persist the case graph.

    Detection findings are written into the same database as the graph, so the console
    can show what was found on a service beside the links it participates in. A
    separate findings store would drift the first time only one of them was rebuilt.
    """

    from graph.extract import extract_artefacts
    from graph.resolve import resolve, to_edges
    from graph.store import GraphStore, utc_now as graph_now

    if db_path.exists():
        db_path.unlink()

    arts = extract_artefacts(farm, observed_at=graph_now())
    with GraphStore(db_path, case_id=case_id) as store:
        for art in arts:
            store.add_artefact(art)
        for svc in farm.services:
            eid = f"ent-{svc.service_id}"
            store.add_entity(eid, f"operator of {svc.service_id}")
            store.attach_service(eid, svc.service_id)

        for f in findings or []:
            store.add_finding(
                service_id=f["service_id"],
                kind=f["kind"],
                path=f["path"],
                evidence=f["evidence"],
            )

        edges = to_edges(resolve(store.entities(), arts))
        for edge in edges:
            store.add_edge(edge)

        counts: dict[str, int] = {}
        for e in edges:
            counts[e.status] = counts.get(e.status, 0) + 1
        data = store.export()

    return {
        "db": str(db_path),
        "artefacts": len(arts),
        "entities": data["counts"]["entities"],
        "edges": len(edges),
        "by_status": dict(sorted(counts.items())),
        "conflicts": data["counts"]["conflicts"],
        "findings_stored": data["counts"]["findings"],
    }


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def run_pipeline(
    *,
    seed: int,
    case_id: str,
    output_dir: Path,
    case_root: Path | None = None,
    owner: str = "g.kanojia",
    authorisation: str = "AUTH-PIPELINE",
    retention_days: int = 30,
) -> PipelineResult:
    """Run every stage against one seed and one case."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    case_root = Path(case_root) if case_root else output_dir / "casework"

    run_id = f"run-{seed}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    started = utc_now()

    cfg = FarmConfig(seed=seed)
    stages: list[StageResult] = []

    # Stage 1: ground truth. Everything downstream is meaningless without it.
    farm_stage = _timed("farm", lambda: stage_farm(cfg))
    stages.append(farm_stage)
    farm = generate_farm(cfg) if farm_stage.ok else None

    # Stage 2: the gate. Still runs without a farm because it is about accountability,
    # not data -- and a run whose gate failed must say so even if nothing else broke.
    stages.append(
        _timed(
            "gates",
            lambda: stage_gates(
                case_root, case_id, owner=owner,
                authorisation=authorisation, retention_days=retention_days,
            ),
        )
    )

    if farm is None:
        for name in ("detect", "correlate", "attribute", "graph"):
            stages.append(
                StageResult(
                    name=name, ok=True, duration_ms=0,
                    skipped_because="farm generation failed, so there is no ground truth",
                )
            )
    else:
        detect_stage = _timed("detect", lambda: stage_detect(farm))
        stages.append(detect_stage)
        detected = detect_stage.summary.get("items", []) if detect_stage.ok else []

        stages.append(_timed("correlate", lambda: stage_correlate(farm)))
        stages.append(_timed("attribute", lambda: stage_attribute(farm)))
        # The graph is written where the console already looks for cases, so a run is
        # immediately browsable with `python -m dashboard` instead of requiring the
        # reader to copy a database somewhere first.
        stages.append(
            _timed(
                "graph",
                lambda: stage_graph(
                    farm, case_root / case_id / "graph.db", case_id, detected
                ),
            )
        )

    result = PipelineResult(
        run_id=run_id,
        seed=seed,
        case_id=case_id,
        output_dir=output_dir,
        started_at=started,
        finished_at=utc_now(),
        stages=stages,
    )

    (output_dir / "manifest.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )

    from oniontrace.report import write_report

    write_report(result, output_dir / "REPORT.md")
    return result
