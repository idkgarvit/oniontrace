"""M6-lite analyst dashboard.

One case at a time, served on loopback, reading the case's identity graph. The
design constraint that shapes every screen is that the graph proposes and a person
decides, so the interface has to make three things visible that a dashboard normally
hides:

* **the score behind every link**, with ``possible`` rendered as possible and not
  quietly promoted to a finding;
* **the counter-evidence**, side by side with the support, on any pair where both
  exist;
* **who decided what**, because a merge with no named actor is not auditable.

There is no authentication. That is a stated limitation, not an oversight: this is a
single-analyst tool bound to ``127.0.0.1``, and the report says so rather than
implying a multi-user deployment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from graph.model import EdgeStatus, GraphError
from graph.resolve import default_cohorts
from graph.store import GraphStore

BASE_DIR = Path(__file__).resolve().parent
CASE_ROOT = Path(os.environ.get("ONIONTRACE_CASES", BASE_DIR.parent / "casework"))

app = FastAPI(title="ONIONTRACE", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# -- helpers ------------------------------------------------------------ #
def _db_path(case_id: str) -> Path:
    """Resolve a case id to its database, refusing anything that escapes the root.

    The case id arrives from the URL, so it is checked against the directory listing
    rather than sanitised by string substitution. A traversal attempt simply does not
    name a case that exists.
    """

    if not case_id or "/" in case_id or "\\" in case_id or case_id.startswith("."):
        raise HTTPException(status_code=400, detail="invalid case id")
    candidate = (CASE_ROOT / case_id / "graph.db").resolve()
    if CASE_ROOT.resolve() not in candidate.parents:
        raise HTTPException(status_code=400, detail="invalid case id")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f"no graph for case {case_id}")
    return candidate


def _open(case_id: str) -> GraphStore:
    return GraphStore(_db_path(case_id), case_id=case_id)


def _cases() -> list[dict[str, Any]]:
    out = []
    if not CASE_ROOT.exists():
        return out
    for d in sorted(CASE_ROOT.iterdir()):
        db = d / "graph.db"
        if not db.is_file():
            continue
        try:
            with GraphStore(db, case_id=d.name) as store:
                counts = store.export()["counts"]
        except Exception:  # a corrupt case must not take the index down
            counts = {"entities": 0, "edges": 0, "conflicts": 0, "merged": 0}
        out.append({"case_id": d.name, "counts": counts})
    return out


def _group_by_service(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group findings by service for display, biggest first.

    Grouped rather than listed flat because a finding's meaning depends on which
    service it was found on: five findings on one host is one misconfigured operator,
    the same five spread across five hosts is a fleet.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        grouped.setdefault(f["service_id"], []).append(f)
    return [
        {"service_id": sid, "findings": items}
        for sid, items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]


def _entity_view(store: GraphStore, entity_id: str) -> dict[str, Any]:
    ent = store.entity(entity_id)
    all_artefacts = {a.service_id: [] for a in []}
    for art in store.artefacts():
        all_artefacts.setdefault(art.service_id, []).append(art)

    by_service: dict[str, list[dict[str, Any]]] = {}
    for f in store.findings():
        by_service.setdefault(f["service_id"], []).append(f)

    services = [
        {
            "service_id": sid,
            "artefacts": all_artefacts.get(sid, []),
            "findings": by_service.get(sid, []),
        }
        for sid in ent.service_ids
    ]

    edges = [
        e
        for e in store.edges()
        if entity_id in (e.a_entity, e.b_entity)
    ]
    edges.sort(key=lambda e: -e.score)
    return {"entity": ent, "services": services, "edges": edges}


# -- views -------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", {"cases": _cases()}
    )


@app.get("/case/{case_id}", response_class=HTMLResponse)
def case_view(request: Request, case_id: str) -> HTMLResponse:
    with _open(case_id) as store:
        graph = store.export()
        findings = store.findings()
        cohorts = default_cohorts(store.artefacts())
        merges = [
            {
                "merge_id": m.merge_id,
                "survivor": m.survivor,
                "absorbed": m.absorbed,
                "actor": m.actor,
                "rationale": m.rationale,
                "created_at": m.created_at,
                "reversed_at": m.reversed_at,
            }
            for m in store.merge_log()
        ]
        notes = store.notes()

    live = [e for e in graph["entities"] if e["merged_into"] is None]
    return templates.TemplateResponse(
        request,
        "case.html",
        {
            "case_id": case_id,
            "graph_json": json.dumps(graph),
            "counts": graph["counts"],
            "entities": live,
            "edges": sorted(graph["edges"], key=lambda e: -e["score"]),
            "cohorts": cohorts,
            "merges": merges,
            "notes": notes,
            "findings": findings,
            "findings_by_service": _group_by_service(findings),
        },
    )


@app.get("/api/case/{case_id}/graph")
def api_graph(case_id: str) -> JSONResponse:
    with _open(case_id) as store:
        return JSONResponse(store.export())


@app.get("/case/{case_id}/entity/{entity_id}", response_class=HTMLResponse)
def entity_view(request: Request, case_id: str, entity_id: str) -> HTMLResponse:
    with _open(case_id) as store:
        try:
            view = _entity_view(store, entity_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        notes = store.notes(entity_id=entity_id)
    return templates.TemplateResponse(
        request,
        "entity.html",
        {
            "case_id": case_id,
            "entity": view["entity"],
            "services": view["services"],
            "edges": view["edges"],
            "notes": notes,
        },
    )


# -- actions ------------------------------------------------------------ #
@app.post("/case/{case_id}/note")
def add_note(
    case_id: str,
    body: str = Form(...),
    actor: str = Form(...),
    entity_id: str = Form(default=""),
) -> RedirectResponse:
    with _open(case_id) as store:
        store.add_note(body, actor=actor, entity_id=entity_id or None)
    target = f"/case/{case_id}/entity/{entity_id}" if entity_id else f"/case/{case_id}"
    return RedirectResponse(target, status_code=303)


@app.post("/case/{case_id}/edge/{edge_id}/status")
def set_edge_status(
    case_id: str, edge_id: str, status: str = Form(...), actor: str = Form(...)
) -> RedirectResponse:
    if status not in {EdgeStatus.CONFIRMED.value, EdgeStatus.REJECTED.value, EdgeStatus.POSSIBLE.value}:
        raise HTTPException(status_code=400, detail="invalid status")
    with _open(case_id) as store:
        try:
            store.set_edge_status(edge_id, status, actor=actor)
        except GraphError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.post("/case/{case_id}/merge")
def merge(
    case_id: str,
    survivor: str = Form(...),
    absorbed: str = Form(...),
    actor: str = Form(...),
    rationale: str = Form(...),
) -> RedirectResponse:
    with _open(case_id) as store:
        try:
            store.merge(survivor, absorbed, actor=actor, rationale=rationale)
        except GraphError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.post("/case/{case_id}/reverse")
def reverse(
    case_id: str,
    merge_id: str = Form(...),
    actor: str = Form(...),
    reason: str = Form(default=""),
) -> RedirectResponse:
    with _open(case_id) as store:
        try:
            store.reverse_merge(merge_id, actor=actor, reason=reason)
        except GraphError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/case/{case_id}", status_code=303)


# -- evidence pack ------------------------------------------------------ #
@app.get("/case/{case_id}/evidence.pdf")
def evidence_pdf(case_id: str) -> Response:
    """Render the case's evidence pack to PDF.

    WeasyPrint is imported here rather than at module load so the dashboard still
    starts on a machine without the PDF stack; the route then fails with a clear
    message instead of the whole app failing to import.
    """

    try:
        from dashboard.render import render_evidence_pack
    except ImportError as exc:  # pragma: no cover - depends on host
        raise HTTPException(
            status_code=503, detail=f"PDF rendering unavailable: {exc}"
        ) from exc

    with _open(case_id) as store:
        graph = store.export()
        findings = store.findings()
        cohorts = default_cohorts(store.artefacts())
        merges = [
            {
                "merge_id": m.merge_id,
                "survivor": m.survivor,
                "absorbed": m.absorbed,
                "actor": m.actor,
                "rationale": m.rationale,
                "created_at": m.created_at,
                "reversed_at": m.reversed_at,
            }
            for m in store.merge_log()
        ]
        notes = store.notes()

    pdf = render_evidence_pack(
        case_id=case_id, graph=graph, cohorts=cohorts, merges=merges,
        notes=notes, findings=findings,
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{case_id}-evidence.pdf"'},
    )


def run() -> None:
    """Start the dashboard on loopback.

    Host is not configurable. A tool holding investigative data should not be one
    environment variable away from being served on every interface.
    """

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ONIONTRACE_PORT", "8420")))
