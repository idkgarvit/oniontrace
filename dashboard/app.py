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

import csv
import io
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
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


# -- the origin boundary ------------------------------------------------ #
# There is no login, so the browser origin is the whole boundary. Two ways a page the
# analyst visits could otherwise reach these routes:
#
# * a plain cross-site form post to http://127.0.0.1:8420/case/.../merge, which would
#   rewrite case state with the analyst's implicit trust of their own machine;
# * DNS rebinding, where evil.example resolves to 127.0.0.1 after the page loads, so the
#   request arrives with a foreign Host header and same-origin checks pass by accident.
#
# Both are closed here rather than per route, because a route added later would otherwise
# inherit the hole. `testserver` is in the list because the test client uses it; it is not
# a name any rebinding attack can own.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname(value: str | None) -> str:
    return (value or "").rsplit(":", 1)[0].strip("[]").strip().lower()


@app.middleware("http")
async def _same_origin_only(request: Request, call_next):
    host = _hostname(request.headers.get("host"))
    if host and host not in _ALLOWED_HOSTS:
        return PlainTextResponse("host not allowed", status_code=421)

    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin:
            if _hostname(urlparse(origin).netloc) not in _ALLOWED_HOSTS:
                return PlainTextResponse("cross-origin write refused", status_code=403)
        else:
            referer = request.headers.get("referer")
            if referer and _hostname(urlparse(referer).netloc) not in _ALLOWED_HOSTS:
                return PlainTextResponse("cross-origin write refused", status_code=403)

    return await call_next(request)


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
        except Exception:  # noqa: BLE001 -- a corrupt case must not take the index down
            counts = {"entities": 0, "edges": 0, "conflicts": 0, "merged": 0}
        out.append({"case_id": d.name, "counts": counts})
    return out


def _refuse(case_id: str, exc: Exception, *, from_form: bool) -> RedirectResponse:
    """Turn a refusal into something the person who asked for it can act on.

    A failing form post used to render FastAPI's raw `{"detail": ...}` as the whole page:
    the analyst lost the case view and got a JSON blob with no way back. A browser form is
    now redirected to the case with the reason in a banner; a programmatic caller still
    gets the 400, because a machine wants the status code.

    Call it as `return _refuse(...)`. A response cannot be raised, and raising one turned
    every refusal into a 500.
    """

    message = str(exc) or exc.__class__.__name__
    if from_form:
        return RedirectResponse(f"/case/{case_id}?error={quote(message)}", status_code=303)
    raise HTTPException(status_code=400, detail=message)


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
def case_view(request: Request, case_id: str, error: str = "") -> HTMLResponse:
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
    live_ids = {e["entity_id"] for e in live}
    # A link whose ends are no longer both live cannot be merged again. The store refuses
    # it, so offering the control would only produce an error page.
    edges = sorted(graph["edges"], key=lambda e: -e["score"])
    for e in edges:
        e["mergeable"] = e["a_entity"] in live_ids and e["b_entity"] in live_ids

    return templates.TemplateResponse(
        request,
        "case.html",
        {
            "case_id": case_id,
            # Passed as data, not as a pre-serialised string: the template's `tojson`
            # filter escapes `<`, `>` and `&`, so a collected artefact value can never
            # close the surrounding <script> tag. json.dumps alone does not escape
            # `</script>`, and the values here come from services an adversary controls.
            "graph": graph,
            "counts": graph["counts"],
            "entities": live,
            "edges": edges,
            "cohorts": cohorts,
            "merges": merges,
            "notes": notes,
            "findings": findings,
            "findings_by_service": _group_by_service(findings),
            "error": error,
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
    case_id: str,
    edge_id: str,
    status: str = Form(...),
    actor: str = Form(...),
    onerror: str = Form(default=""),
) -> RedirectResponse:
    if status not in {EdgeStatus.CONFIRMED.value, EdgeStatus.REJECTED.value, EdgeStatus.POSSIBLE.value}:
        raise HTTPException(status_code=400, detail="invalid status")
    with _open(case_id) as store:
        try:
            store.set_edge_status(edge_id, status, actor=actor)
        except GraphError as exc:
            return _refuse(case_id, exc, from_form=onerror == "case")
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.post("/case/{case_id}/merge")
def merge(
    case_id: str,
    survivor: str = Form(...),
    absorbed: str = Form(...),
    actor: str = Form(...),
    rationale: str = Form(...),
    onerror: str = Form(default=""),
) -> RedirectResponse:
    with _open(case_id) as store:
        try:
            store.merge(survivor, absorbed, actor=actor, rationale=rationale)
        except GraphError as exc:
            return _refuse(case_id, exc, from_form=onerror == "case")
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.post("/case/{case_id}/reverse")
def reverse(
    case_id: str,
    merge_id: str = Form(...),
    actor: str = Form(...),
    reason: str = Form(default=""),
    onerror: str = Form(default=""),
) -> RedirectResponse:
    with _open(case_id) as store:
        try:
            store.reverse_merge(merge_id, actor=actor, reason=reason)
        except GraphError as exc:
            return _refuse(case_id, exc, from_form=onerror == "case")
    return RedirectResponse(f"/case/{case_id}", status_code=303)


# -- case creation ------------------------------------------------------ #
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _get(record: Any, name: str, default: Any = "") -> Any:
    """Read a field from a dataclass or a row dict without caring which it is."""
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _date_or_400(value: str) -> str:
    """Accept an empty string or a calendar date; refuse anything else.

    The timestamps in the store are ISO-8601 text. Comparing their date prefix is exact
    and needs no timezone arithmetic, which is why the filter is expressed in days.
    """
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"not a date: {value}") from exc
    return value


def _in_range(stamp: str, since: str, until: str) -> bool:
    day = (stamp or "")[:10]
    if not day:
        return False
    if since and day < since:
        return False
    return not (until and day > until)


@app.get("/new", response_class=HTMLResponse)
def new_case_form(request: Request, error: str = "") -> HTMLResponse:
    return templates.TemplateResponse(request, "new.html", {"error": error})


@app.post("/new")
def new_case(
    case_id: str = Form(default=""),
    seed: str = Form(default=""),
    owner: str = Form(default=""),
    authorisation: str = Form(default=""),
    retention_days: str = Form(default="30"),
) -> Response:
    """Build a case from the browser, so the demo never needs a terminal.

    The pipeline is the same entry point the CLI uses. Running it here is not a shortcut
    around the gate: the gate is a stage inside it, and a run whose authorisation is
    missing is refused by the same code that refuses it from the shell.

    Every field arrives as text, including the two numbers, on purpose. Typed form fields
    would make FastAPI answer a mistyped value with a 422 and a JSON body rendered as the
    page; a browser form has to come back with a sentence and the case list still visible.
    """
    case_id = case_id.strip()
    owner = owner.strip()
    authorisation = authorisation.strip()

    def refuse(message: str) -> RedirectResponse:
        return RedirectResponse(f"/new?error={quote(message)}", status_code=303)

    try:
        seed_value = int(seed)
    except ValueError:
        return refuse(f"farm seed must be a whole number, not {seed!r}")
    if not 0 <= seed_value <= 2**63 - 1:
        return refuse("farm seed must be between 0 and 2^63-1")
    try:
        retention = int(retention_days)
    except ValueError:
        return refuse(f"retention must be a whole number of days, not {retention_days!r}")

    if not _CASE_ID.match(case_id):
        return refuse("case id must start with a letter or digit and use A-Z a-z 0-9 . _ -")
    if (CASE_ROOT / case_id).exists():
        return refuse(f"case {case_id} already exists")
    if not owner:
        return refuse("an owner is required; an unattributed case is not auditable")
    if not authorisation:
        return refuse("an authorisation reference is required before a case exists")
    if not 1 <= retention <= 3650:
        return refuse("retention must be between 1 and 3650 days")

    from oniontrace.pipeline import run_pipeline

    try:
        run_pipeline(
            seed=seed_value,
            case_id=case_id,
            output_dir=CASE_ROOT / case_id / "run",
            case_root=CASE_ROOT,
            owner=owner,
            authorisation=authorisation,
            retention_days=retention,
        )
    except Exception as exc:  # noqa: BLE001 -- any stage may fail, and the browser gets the reason
        return refuse(f"the pipeline refused or failed: {exc}")

    return RedirectResponse(f"/case/{case_id}", status_code=303)


# -- timeline ----------------------------------------------------------- #
@app.get("/case/{case_id}/timeline", response_class=HTMLResponse)
def timeline(
    request: Request, case_id: str, since: str = "", until: str = ""
) -> HTMLResponse:
    """Every dated record in one list, filterable by day.

    Artefacts, findings, edges and notes carry different timestamps because they are
    produced by different stages. Putting them on one axis is what makes "what did this
    case know on this date" answerable.
    """
    since, until = _date_or_400(since), _date_or_400(until)

    with _open(case_id) as store:
        artefacts = store.artefacts()
        findings = store.findings()
        edges = store.edges()
        notes = store.notes()

    rows: list[dict[str, Any]] = []
    for artefact in artefacts:
        rows.append(
            {
                "record": "artefact",
                "subject": _get(artefact, "service_id"),
                "kind": _get(artefact, "kind"),
                "value": _get(artefact, "value"),
                "at": _get(artefact, "observed_at"),
                "detail": _get(artefact, "evidence_sha256"),
            }
        )
    for finding in findings:
        rows.append(
            {
                "record": "finding",
                "subject": _get(finding, "service_id"),
                "kind": _get(finding, "kind"),
                "value": _get(finding, "path"),
                "at": _get(finding, "created_at"),
                "detail": _get(finding, "evidence"),
            }
        )
    for edge in edges:
        rows.append(
            {
                "record": "edge",
                "subject": f"{_get(edge, 'a_entity')} → {_get(edge, 'b_entity')}",
                "kind": _get(edge, "method"),
                "value": _get(edge, "score"),
                "at": _get(edge, "created_at"),
                "detail": _get(edge, "status"),
            }
        )
    for note in notes:
        rows.append(
            {
                "record": "note",
                "subject": _get(note, "entity_id") or _get(note, "case_id"),
                "kind": _get(note, "actor"),
                "value": _get(note, "body"),
                "at": _get(note, "created_at"),
                "detail": "",
            }
        )

    rows = [row for row in rows if _in_range(row["at"], since, until)]
    rows.sort(key=lambda row: (row["at"], row["record"], str(row["subject"])))

    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "case_id": case_id,
            "rows": rows,
            "since": since,
            "until": until,
            "counts": {
                name: sum(1 for row in rows if row["record"] == name)
                for name in ("artefact", "finding", "edge", "note")
            },
        },
    )


# -- run report ---------------------------------------------------------- #
def _run_dir(case_id: str) -> Path:
    """Where the pipeline put this case's run, after checking the case exists."""

    _db_path(case_id)
    return CASE_ROOT / case_id / "run"


def _brief(value: Any) -> str:
    """Shorten one stage-summary value so the table stays a table.

    Stage summaries are not uniform: ``farm`` records eight counts, ``detect`` records the
    full findings list. Printing the list turned one row into a wall of text and pushed
    every later stage off the screen. Collections become their size. Strings are kept up
    to a paragraph's length, because some of them are claim boundaries -- ``scope_limit``
    is a disclaimer, and clipping it would be the one thing this report must not do. The
    manifest itself is downloadable for a reader who needs the whole record.
    """

    if isinstance(value, (list, tuple, dict)):
        return f"{len(value)} entries"
    text = str(value)
    return text if len(text) <= 240 else text[:237] + "..."


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@app.get("/case/{case_id}/report", response_class=HTMLResponse)
def run_report(request: Request, case_id: str) -> HTMLResponse:
    """What the pipeline did, stage by stage, in the browser.

    The console could show the result of a run but not the run: the stage table lived in
    ``manifest.json`` and the generated narrative in ``REPORT.md``, both on disk. A demo
    that has to open a file manager to show the work is not GUI-operated, so both are
    rendered here. A case built before the pipeline wrote manifests (``graph build`` on
    its own) gets an explanation rather than a 404, because the case is real.
    """

    run_dir = _run_dir(case_id)
    manifest = _read_json(run_dir / "manifest.json")
    report_path = run_dir / "REPORT.md"
    report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""

    stages = []
    if isinstance(manifest, dict):
        for stage in manifest.get("stages", []):
            stages.append(
                {
                    "stage": stage.get("stage", ""),
                    "ok": bool(stage.get("ok")),
                    "duration_ms": stage.get("duration_ms", 0),
                    "note": stage.get("error") or stage.get("skipped_because") or "",
                    "summary": {
                        key: _brief(value)
                        for key, value in (stage.get("summary") or {}).items()
                    },
                }
            )

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "case_id": case_id,
            "manifest": manifest if isinstance(manifest, dict) else None,
            "stages": stages,
            "report": report,
            "has_report": bool(report),
        },
    )


@app.get("/case/{case_id}/manifest.json")
def run_manifest(case_id: str) -> Response:
    """The stage manifest as a file: the machine-readable record behind the page."""

    path = _run_dir(case_id) / "manifest.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"case {case_id} has no run manifest")
    return Response(
        path.read_bytes(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{case_id}-manifest.json"'},
    )


@app.get("/case/{case_id}/report.md")
def run_report_markdown(case_id: str) -> Response:
    """The generated report as a file, for the record and for grep."""

    path = _run_dir(case_id) / "REPORT.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"case {case_id} has no pipeline report")
    return PlainTextResponse(
        path.read_text(encoding="utf-8"),
        headers={"Content-Disposition": f'attachment; filename="{case_id}-REPORT.md"'},
    )


# -- export ------------------------------------------------------------- #
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    """Stop a hostile page from writing a formula into the analyst's spreadsheet.

    Every string in a case was read off an onion service or typed by an analyst who was
    looking at one. To Excel and to most importers a cell beginning with ``=``, ``+``,
    ``-`` or ``@`` is an instruction, not text -- the CSV-injection class that turns an
    exported report into a payload when it is opened. The cell is prefixed with an
    apostrophe, which spreadsheets render as "treat the rest as text"; every other reader
    sees one extra visible character. Numbers are left alone so a score stays a number.
    """

    if isinstance(value, str) and value.startswith(_CSV_FORMULA_LEAD):
        return "'" + value
    return value


@app.get("/case/{case_id}/export.csv")
def export_csv(case_id: str) -> Response:
    """The whole case as CSV, one row per record.

    A single table rather than one file per record type: an analyst who wants to pivot
    in a spreadsheet needs the records side by side, and the `record` column is what
    lets them filter. JSON and the evidence PDF already exist; this is the third format
    the requirement names.
    """
    with _open(case_id) as store:
        graph = store.export()
        artefacts = store.artefacts()
        findings = store.findings()
        edges = store.edges()
        notes = store.notes()

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def row(cells: list[Any]) -> None:
        writer.writerow([_csv_cell(cell) for cell in cells])

    row(
        [
            "record",
            "id",
            "subject",
            "kind",
            "value",
            "status",
            "score",
            "at",
            "category",
            "last_scan",
            "source",
            "detail",
        ]
    )

    for entity in graph["entities"]:
        row(
            [
                "entity",
                entity["entity_id"],
                entity.get("label", ""),
                "",
                ";".join(entity.get("service_ids", [])),
                entity.get("status", ""),
                "",
                entity.get("created_at", ""),
                entity.get("category", ""),
                entity.get("last_scan", ""),
                entity.get("source", ""),
                "",
            ]
        )
    for artefact in artefacts:
        row(
            [
                "artefact",
                _get(artefact, "artefact_id"),
                _get(artefact, "service_id"),
                _get(artefact, "kind"),
                _get(artefact, "value"),
                "",
                "",
                _get(artefact, "observed_at"),
                "",
                "",
                "",
                _get(artefact, "evidence_sha256"),
            ]
        )
    for edge in edges:
        row(
            [
                "edge",
                _get(edge, "edge_id"),
                f"{_get(edge, 'a_entity')} -> {_get(edge, 'b_entity')}",
                _get(edge, "method"),
                "",
                _get(edge, "status"),
                _get(edge, "score"),
                _get(edge, "created_at"),
                "",
                "",
                "",
                "",
            ]
        )
    for finding in findings:
        row(
            [
                "finding",
                _get(finding, "finding_id"),
                _get(finding, "service_id"),
                _get(finding, "kind"),
                _get(finding, "path"),
                "",
                "",
                _get(finding, "created_at"),
                "",
                "",
                "",
                _get(finding, "evidence"),
            ]
        )
    for note in notes:
        row(
            [
                "note",
                _get(note, "note_id"),
                _get(note, "entity_id") or _get(note, "case_id"),
                _get(note, "actor"),
                _get(note, "body"),
                "",
                "",
                _get(note, "created_at"),
                "",
                "",
                "",
                "",
            ]
        )

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{case_id}.csv"'},
    )


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
