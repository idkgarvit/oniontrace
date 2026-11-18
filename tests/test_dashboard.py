"""Tests for the M6-lite dashboard.

The routes themselves are thin; what is worth pinning is the behaviour a demo can
silently break:

* the graph is refused for a case that does not exist, and a path that would escape
  the case root is rejected rather than normalised into something valid;
* every mutating action lands on disk, so a refresh does not lose the analyst's work;
* a merge posts successfully and the graph reflects it;
* the evidence pack is a real PDF whose text carries the honesty statement, since a
  pack that omits its own limits is the failure mode the doctrine exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
import importlib

from fastapi.testclient import TestClient

# `dashboard/__init__.py` re-exports the FastAPI instance as `app`, which shadows the
# submodule of the same name inside the package namespace. Resolving through
# sys.modules gives the module itself, so patching CASE_ROOT actually works.
app_module = importlib.import_module("dashboard.app")
app = app_module.app

ROOT = Path(__file__).resolve().parent.parent


def _pdf_text(blob: bytes) -> str:
    """Extract PDF text with the system tool, so the test does not need a Python lib."""

    import shutil
    import subprocess
    import tempfile

    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(blob)
        path = fh.name
    try:
        return subprocess.run(
            ["pdftotext", "-layout", path, "-"], capture_output=True, text=True, check=False
        ).stdout
    finally:
        Path(path).unlink(missing_ok=True)


def _farm_json(tmp_path_factory) -> Path:
    """Generate the farm rather than reading out/farm.json.

    Reading the generated file made this whole module skip on a clean checkout, which
    is the same shape of defect as a vacuous assertion: the suite reported success
    while the console had never been exercised. The farm is deterministic for a seed,
    so it can be built here.
    """

    from farm.generator import generate_farm
    from farm.model import FarmConfig

    d = tmp_path_factory.mktemp("farm")
    path = d / "farm.json"
    farm = generate_farm(FarmConfig(seed=20260925))
    # to_json(include_text=True) is what `python -m farm generate` writes; matching it
    # keeps this fixture on the same artefact path the CLI uses.
    path.write_text(farm.to_json(include_text=True), encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def farm_path(tmp_path_factory) -> Path:
    return _farm_json(tmp_path_factory)


@pytest.fixture
def case(tmp_path, monkeypatch, farm_path):

    monkeypatch.setenv("ONIONTRACE_CASES", str(tmp_path))
    monkeypatch.setattr(app_module, "CASE_ROOT", tmp_path)

    from graph.cli import main as graph_main

    db = tmp_path / "CASE-T" / "graph.db"
    rc = graph_main(
        ["--db", str(db), "--case", "CASE-T", "build", "--farm", str(farm_path)]
    )
    assert rc == 0

    # Findings are written by the pipeline's detect stage, not by `graph build`, so
    # the fixture seeds them directly. Testing the console against a case with no
    # findings would pass while the findings panel did nothing.
    from graph.store import GraphStore

    with GraphStore(db, case_id="CASE-T") as store:
        svc = store.entities()[0].service_ids[0]
        store.add_finding(service_id=svc, kind="dot_env", path="/.env",
                          evidence="HTTP 200, body begins AWS_")
        store.add_finding(service_id=svc, kind="server_status", path="/server-status",
                          evidence="mod_status table")

    return TestClient(app), "CASE-T", tmp_path


def test_index_lists_the_case(case):
    client, cid, _ = case
    r = client.get("/")
    assert r.status_code == 200
    assert cid in r.text


def test_case_view_renders_with_a_graph(case):
    client, cid, _ = case
    r = client.get(f"/case/{cid}")
    assert r.status_code == 200
    assert "Entity graph" in r.text
    # The JSON the renderer needs must be embedded, or the SVG stays empty.
    assert "window.__GRAPH__" in r.text


def test_unknown_case_is_404(case):
    client, _, _ = case
    assert client.get("/case/NOPE").status_code == 404


def test_path_traversal_is_rejected(case):
    """Encoded traversal must not resolve to a directory outside the case root.

    A literal ``..`` is deliberately absent: the HTTP client collapses it before the
    request is sent, so testing it would assert against httpx rather than the app.
    The encoded forms are what actually reach the handler.
    """

    client, _, _ = case
    for bad in ["..%2f..%2fetc", "%2e%2e", "..%2fCASE-T", ".hidden"]:
        r = client.get(f"/case/{bad}")
        assert r.status_code in (400, 404), f"{bad} -> {r.status_code}"


def test_graph_api_returns_the_export(case):
    client, cid, _ = case
    data = client.get(f"/api/case/{cid}/graph").json()
    assert data["case_id"] == cid
    assert data["counts"]["entities"] > 0
    assert data["counts"]["edges"] > 0


def test_a_note_persists_across_requests(case):
    client, cid, _ = case
    r = client.post(
        f"/case/{cid}/note",
        data={"actor": "g.kanojia", "body": "check the cert issuer", "entity_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    data = client.get(f"/api/case/{cid}/graph").json()
    assert any(n["body"] == "check the cert issuer" for n in data["notes"])


def test_confirming_an_edge_persists(case):
    client, cid, _ = case
    edge = client.get(f"/api/case/{cid}/graph").json()["edges"][0]["edge_id"]
    r = client.post(
        f"/case/{cid}/edge/{edge}/status",
        data={"status": "confirmed", "actor": "g.kanojia"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    edges = {e["edge_id"]: e for e in client.get(f"/api/case/{cid}/graph").json()["edges"]}
    assert edges[edge]["status"] == "confirmed"


def test_a_merge_requires_a_rationale_and_is_reflected(case):
    client, cid, _ = case
    data = client.get(f"/api/case/{cid}/graph").json()
    edge = data["edges"][0]
    r = client.post(
        f"/case/{cid}/merge",
        data={
            "survivor": edge["a_entity"],
            "absorbed": edge["b_entity"],
            "actor": "g.kanojia",
            "rationale": "same unique favicon",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/api/case/{cid}/graph").json()["counts"]["merged"] == 1


def test_merge_without_evidence_is_refused(case):
    client, cid, _ = case
    r = client.post(
        f"/case/{cid}/merge",
        data={"survivor": "ent-a", "absorbed": "ent-b", "actor": "x", "rationale": "y"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_every_page_has_a_top_level_heading(case):
    """Heading hierarchy starts at H1.

    The case and index pages used to start at H2 and H3, so the outline a screen reader
    announces had no page title in it at all.
    """

    client, cid, _ = case
    for path in ("/", f"/case/{cid}"):
        html = client.get(path).text
        assert "<h1" in html, f"{path} has no H1"
        # and nothing before the first H1 that is an H2
        head = html.split("<main>", 1)[-1]
        first = min(
            (head.find(f"<h{n}") for n in (1, 2, 3) if f"<h{n}" in head),
            default=-1,
        )
        assert head[first:first + 3] == "<h1", f"{path} opens its outline with a lower heading"


def test_merge_is_offered_only_after_a_link_is_confirmed(case):
    """The console must not offer to collapse two entities on evidence alone.

    Before this, `POST /case/{id}/merge` existed but nothing in the UI reached it, so the
    one operation that moves a node was unreachable for the analyst using the console.
    """

    client, cid, _ = case
    data = client.get(f"/api/case/{cid}/graph").json()
    cand = next(e for e in data["edges"] if e["status"] == "candidate")

    html = client.get(f"/case/{cid}").text
    assert f'data-edge-id="{cand["edge_id"]}"' in html, "edge cards are not addressable"
    assert "mergeform" not in html, "merge control offered before any link was confirmed"

    client.post(f"/case/{cid}/edge/{cand['edge_id']}/status",
                data={"status": "confirmed", "actor": "tester"})
    html = client.get(f"/case/{cid}").text
    assert "mergeform" in html, "confirming a link did not reveal the merge control"


def test_merge_from_the_console_is_reversible(case):
    """Confirm -> merge -> reverse, through the same routes the buttons use."""

    client, cid, _ = case
    data = client.get(f"/api/case/{cid}/graph").json()
    before = data["counts"]["entities"]
    cand = next(e for e in data["edges"] if e["status"] == "candidate")

    client.post(f"/case/{cid}/edge/{cand['edge_id']}/status",
                data={"status": "confirmed", "actor": "tester"})
    r = client.post(f"/case/{cid}/merge", data={
        "survivor": cand["a_entity"], "absorbed": cand["b_entity"],
        "actor": "tester", "rationale": "shared unique favicon",
    })
    assert r.status_code in (200, 303)
    mid = client.get(f"/api/case/{cid}/graph").json()
    assert mid["counts"]["entities"] == before - 1
    merge_id = mid["merges"][0]["merge_id"]

    r = client.post(f"/case/{cid}/reverse", data={
        "merge_id": merge_id, "actor": "tester", "reason": "test",
    })
    assert r.status_code in (200, 303)
    after = client.get(f"/api/case/{cid}/graph").json()
    assert after["counts"]["entities"] == before
    assert after["merges"][0]["reversed_at"]


def test_the_merge_control_disappears_once_a_side_is_merged(case):
    """Offering a control the store will refuse is a button that can only produce an error."""

    client, cid, _ = case
    data = client.get(f"/api/case/{cid}/graph").json()
    cand = next(e for e in data["edges"] if e["status"] == "candidate")
    client.post(f"/case/{cid}/edge/{cand['edge_id']}/status",
                data={"status": "confirmed", "actor": "tester"})
    assert "mergeform" in client.get(f"/case/{cid}").text

    client.post(f"/case/{cid}/merge", data={
        "survivor": cand["a_entity"], "absorbed": cand["b_entity"],
        "actor": "tester", "rationale": "shared favicon",
    })
    html = client.get(f"/case/{cid}").text
    assert html.count("mergeform") == 0, "merge offered on a pair that is already merged"

    # and asking anyway is refused rather than silently logged a second time
    r = client.post(f"/case/{cid}/merge", data={
        "survivor": cand["a_entity"], "absorbed": cand["b_entity"],
        "actor": "tester", "rationale": "again",
    })
    assert r.status_code == 400
    assert len(client.get(f"/api/case/{cid}/graph").json()["merges"]) == 1


def test_a_refused_merge_returns_a_page_to_a_browser_and_400_to_a_machine(case):
    """A failing form post must not replace the analyst's page with raw JSON.

    `<input required>` accepts whitespace, so a rationale of three spaces reached the
    store and rendered `{"detail": "a merge requires a rationale"}` as the entire page.
    """

    client, cid, _ = case
    data = client.get(f"/api/case/{cid}/graph").json()
    cand = next(e for e in data["edges"] if e["status"] == "candidate")
    client.post(f"/case/{cid}/edge/{cand['edge_id']}/status",
                data={"status": "confirmed", "actor": "tester"})
    payload = {
        "survivor": cand["a_entity"], "absorbed": cand["b_entity"],
        "actor": "tester", "rationale": "   ",
    }

    # A browser form carries the marker and gets the case back with a banner.
    r = client.post(f"/case/{cid}/merge", data={**payload, "onerror": "case"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "banner err" in page and "requires a rationale" in page
    assert "Entity graph" in page, "the analyst lost the case view"

    # A programmatic caller gets the status code it can branch on.
    r = client.post(f"/case/{cid}/merge", data=payload, follow_redirects=False)
    assert r.status_code == 400
    assert "rationale" in r.json()["detail"]


def test_findings_reach_the_case_view(case):
    """A case view without its findings is not a case view; it is a graph."""

    client, cid, _ = case
    r = client.get(f"/case/{cid}")
    assert r.status_code == 200
    assert "Detection findings" in r.text
    assert "findcard" in r.text


def test_findings_appear_on_the_entity_card(case):
    client, cid, _ = case
    data = client.get(f"/api/case/{cid}/graph").json()
    with_findings = {
        f["service_id"] for f in data.get("findings", [])
    }
    entity = next(
        (e for e in data["entities"] if set(e["services"]) & with_findings), None
    )
    if entity is None:
        pytest.skip("no entity carries a finding in this case")
    r = client.get(f"/case/{cid}/entity/{entity['entity_id']}")
    assert r.status_code == 200
    assert "findings" in r.text


def test_findings_are_in_the_evidence_pack(case):
    client, cid, _ = case
    r = client.get(f"/case/{cid}/evidence.pdf")
    assert r.status_code == 200
    text = _pdf_text(r.content)
    assert "Detection findings" in text


def test_evidence_pack_is_a_pdf_that_states_its_limits(case):
    client, cid, _ = case
    r = client.get(f"/case/{cid}/evidence.pdf")
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"

    text = _pdf_text(r.content)
    assert "candidate" in text.lower()
    assert "not a chain of custody" in text.lower()
    assert cid in text


# -- the console is reachable from a browser, so its data is a parse boundary --- #
#
# Every string in the graph arrives from a service the analyst does not control: an
# analytics id, a page title, a header value. The graph is handed to the page inside a
# <script> block, so a value containing `</script>` used to close that block and start a
# new one. The attack is not hypothetical drill: it is the collector's own input stream.


def test_an_artefact_cannot_break_out_of_the_graph_script(case):
    client, cid, tmp_path = case
    from graph.store import GraphStore

    payload = '</script><img src=x onerror=alert(1)>'
    with GraphStore(tmp_path / cid / "graph.db", case_id=cid) as store:
        store.add_finding(
            service_id=store.entities()[0].service_ids[0],
            kind="page_title",
            path="/",
            evidence=payload,
        )

    html = client.get(f"/case/{cid}").text

    # The only </script> tags left are the ones the template itself wrote.
    assert html.count("</script>") == 2, "a collected value closed the script block early"
    assert "<img src=x" not in html, "the payload reached the DOM as markup"
    # It is still present, as inert data. Dropping it would hide evidence.
    assert "\\u003c/script\\u003e" in html


def test_a_note_cannot_break_out_of_the_graph_script(case):
    client, cid, _ = case
    payload = '</SCRIPT ><svg onload=alert(1)>'

    client.post(f"/case/{cid}/note", data={"actor": "analyst", "body": payload})
    html = client.get(f"/case/{cid}").text

    assert html.count("</script>") == 2
    assert "<svg onload" not in html.lower()


def test_a_cross_origin_write_is_refused(case):
    client, cid, _ = case
    r = client.post(
        f"/case/{cid}/note",
        data={"actor": "attacker", "body": "injected"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "cross-origin" in r.text

    # The note never landed.
    assert "injected" not in client.get(f"/case/{cid}").text


def test_a_write_from_the_console_itself_still_works(case):
    client, cid, _ = case
    r = client.post(
        f"/case/{cid}/note",
        data={"actor": "analyst", "body": "legitimate"},
        headers={"origin": "http://127.0.0.1:8420"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert "legitimate" in client.get(f"/case/{cid}").text


def test_a_rebound_host_header_is_refused(case):
    client, cid, _ = case
    r = client.get(f"/case/{cid}", headers={"host": "evil.example"})
    assert r.status_code == 421

    r = client.get(f"/case/{cid}", headers={"host": "localhost:8420"})
    assert r.status_code == 200


# -- the PDF renderer is the other parse boundary ----------------------- #
#
# The evidence pack turns case data into a document that WeasyPrint renders. Any
# resource the document names is fetched by WeasyPrint and handed to Pillow's format
# dispatcher, which shells out to Ghostscript for EPS. Two published advisories against
# this library live on exactly that path, and Ghostscript is installed on this host, so
# the fetcher is pinned shut here rather than trusted.


def test_the_evidence_pack_refuses_to_fetch_anything():
    from dashboard.render import _no_external_resources

    fetcher = _no_external_resources()
    for url in (
        "https://example.com/tracker.png",
        "file:///etc/passwd",
        "data:image/x-eps;base64,AAAA",
    ):
        with pytest.raises(ValueError, match="refused to load"):
            fetcher.fetch(url)


def test_a_hostile_payload_in_a_finding_renders_as_text(case):
    client, cid, tmp_path = case
    from graph.store import GraphStore

    payload = '<img src="data:image/x-eps;base64,AAAA">'
    with GraphStore(tmp_path / cid / "graph.db", case_id=cid) as store:
        store.add_finding(
            service_id=store.entities()[0].service_ids[0],
            kind="page_title",
            path="/",
            evidence=payload,
        )

    r = client.get(f"/case/{cid}/evidence.pdf")
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"

    # The angle brackets survive as text, so the renderer never saw a tag. pdftotext
    # breaks a long table cell mid-token, so compare with the whitespace removed.
    text = _pdf_text(r.content).replace(" ", "").replace("\n", "")
    assert "<img" in text
    assert 'data:image/x-eps;base64,AAAA">' in text


# --------------------------------------------------------------------------------------- #
# The icon, and why it is a test and not a flourish.
# --------------------------------------------------------------------------------------- #


def test_every_page_declares_its_own_icon(case):
    """A page that declares no icon makes the browser ask for /favicon.ico, and the console
    answered that with a 404 -- one failed request on every load.

    It is recorded as a defect rather than a nicety because the project's own standard is
    "zero errors, warnings or failed requests", and the console walkthrough reported this one
    as its only problem: `console problems: 1`. A QA run that ends on a known 404 teaches the
    next person to read past the count.

    The icon is inline, so this also pins the reason it cannot rot: there is no file to lose
    and no route that can start returning 404 again.
    """
    import base64
    import re

    client, cid, _ = case
    for path in ("/", f"/case/{cid}"):
        page = client.get(path).text
        match = re.search(r'<link rel="icon" href="([^"]+)"', page)
        assert match, f"{path} declares no icon, so the browser will request /favicon.ico"
        uri = match.group(1)
        assert uri.startswith("data:image/svg+xml;base64,"), (
            f"{path} points its icon at {uri[:40]!r}; an external reference is a request that can fail"
        )
        svg = base64.b64decode(uri.split("base64,", 1)[1]).decode()
        assert svg.startswith("<svg") and svg.endswith("</svg>"), "the icon is not a complete SVG"
        assert 'viewBox="0 0 32 32"' in svg, "the icon has no viewBox, so it does not scale"


# --------------------------------------------------------------------------------------- #
# Case creation from the browser, because the console is meant to be operated, not driven
# from a shell. The route is thin; what is pinned here is that every refusal is a sentence
# and a way back, and that the pipeline is called with exactly what the form said.
# --------------------------------------------------------------------------------------- #


def test_new_case_form_renders(case):
    import json
    import re
    import shutil
    import subprocess

    client, _, _ = case
    r = client.get("/new")
    assert r.status_code == 200
    assert 'name="case_id"' in r.text
    assert 'name="authorisation"' in r.text
    assert "Build the case" in r.text

    # Chromium compiles a pattern attribute with the ECMAScript `v` flag, where a bare
    # hyphen in a character class is a syntax error. The form still submitted, so the
    # bug was only visible as a console error and a lost client-side check.
    match = re.search(r'pattern="([^"]+)"', r.text)
    assert match, "the case-id field no longer constrains its input in the browser"
    pattern = match.group(1)
    assert r"\-" in pattern, "escape the hyphen or Chromium rejects the pattern"
    node = shutil.which("node")
    if node:
        compiled = subprocess.run(
            [node, "-e", f"new RegExp({json.dumps(pattern)}, 'v')"],
            capture_output=True, text=True,
        )
        assert compiled.returncode == 0, compiled.stderr


def test_new_case_refuses_without_owner_or_authorisation(case):
    client, _, _ = case
    base = {
        "case_id": "CASE-NEW",
        "seed": "20260925",
        "owner": "g.kanojia",
        "authorisation": "AUTH-1",
        "retention_days": "30",
    }
    for field in ("owner", "authorisation"):
        data = dict(base, **{field: "  "})
        r = client.post("/new", data=data, follow_redirects=False)
        assert r.status_code == 303
        assert "error=" in r.headers["location"]
        assert field in r.headers["location"]


def test_new_case_refuses_an_id_that_is_not_a_case(case):
    client, _, _ = case
    for bad in ("../escape", ".hidden", "has space", ""):
        r = client.post(
            "/new",
            data={
                "case_id": bad,
                "seed": "1",
                "owner": "g.kanojia",
                "authorisation": "AUTH-1",
                "retention_days": "30",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, bad
        assert "case%20id" in r.headers["location"], bad


def test_new_case_refuses_a_case_that_already_exists(case):
    client, cid, _ = case
    r = client.post(
        "/new",
        data={
            "case_id": cid,
            "seed": "1",
            "owner": "g.kanojia",
            "authorisation": "AUTH-1",
            "retention_days": "30",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "already" in r.headers["location"]


def test_new_case_refuses_a_mistyped_number_as_a_sentence(case):
    """A typed form field would answer this with a 422 JSON body rendered as the page."""

    client, _, _ = case
    for field, value in (("seed", "twenty"), ("retention_days", "thirty")):
        data = {
            "case_id": "CASE-NEW",
            "seed": "20260925",
            "owner": "g.kanojia",
            "authorisation": "AUTH-1",
            "retention_days": "30",
        }
        data[field] = value
        r = client.post("/new", data=data, follow_redirects=False)
        assert r.status_code == 303
        assert "error=" in r.headers["location"]
        assert "whole%20number" in r.headers["location"]


def test_new_case_runs_the_pipeline_with_what_the_form_said(case, monkeypatch):
    from oniontrace import pipeline

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(pipeline, "run_pipeline", fake)

    client, _, tmp_path = case
    r = client.post(
        "/new",
        data={
            "case_id": "CASE-NEW",
            "seed": "20260925",
            "owner": " g.kanojia ",
            "authorisation": "AUTH-1",
            "retention_days": "45",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/case/CASE-NEW"
    assert seen["case_id"] == "CASE-NEW"
    assert seen["seed"] == 20260925
    assert seen["owner"] == "g.kanojia"
    assert seen["authorisation"] == "AUTH-1"
    assert seen["retention_days"] == 45
    assert seen["case_root"] == tmp_path
    assert seen["output_dir"] == tmp_path / "CASE-NEW" / "run"


def test_new_case_reports_a_pipeline_refusal_in_the_form(case, monkeypatch):
    from urllib.parse import unquote

    from oniontrace import pipeline

    def fake(**kwargs):
        raise RuntimeError("authorisation AUTH-BAD is not on file")

    monkeypatch.setattr(pipeline, "run_pipeline", fake)

    client, _, _ = case
    r = client.post(
        "/new",
        data={
            "case_id": "CASE-NEW",
            "seed": "1",
            "owner": "g.kanojia",
            "authorisation": "AUTH-BAD",
            "retention_days": "30",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "not on file" in unquote(r.headers["location"])


# --------------------------------------------------------------------------------------- #
# Timeline: one axis for everything the case knows.
# --------------------------------------------------------------------------------------- #


def test_timeline_shows_every_record_type(case):
    client, cid, _ = case
    client.post(
        f"/case/{cid}/note",
        data={"actor": "g.kanojia", "body": "check the cert issuer", "entity_id": ""},
        follow_redirects=False,
    )
    r = client.get(f"/case/{cid}/timeline")
    assert r.status_code == 200
    for word in ('badge">artefact', 'badge">finding', 'badge">edge', 'badge">note'):
        assert word in r.text, word
    assert "check the cert issuer" in r.text


def test_timeline_filters_by_day(case):
    client, cid, _ = case
    empty = client.get(f"/case/{cid}/timeline", params={"since": "2999-01-01"})
    assert empty.status_code == 200
    assert "Nothing in this range" in empty.text

    wide = client.get(f"/case/{cid}/timeline", params={"until": "2999-01-01"})
    assert "Nothing in this range" not in wide.text


def test_timeline_refuses_a_non_date(case):
    client, cid, _ = case
    for bad in ("yesterday", "2026-13-01", "17-11-2026"):
        r = client.get(f"/case/{cid}/timeline", params={"since": bad})
        assert r.status_code == 400, bad


def test_timeline_of_an_unknown_case_is_404(case):
    client, _, _ = case
    assert client.get("/case/NOPE/timeline").status_code == 404


def test_case_pages_link_to_the_new_views(case):
    client, cid, _ = case
    for path in (f"/case/{cid}", f"/case/{cid}/timeline"):
        page = client.get(path).text
        for href in (f"/case/{cid}/timeline", f"/case/{cid}/export.csv", "/new"):
            assert href in page, (path, href)


# --------------------------------------------------------------------------------------- #
# CSV: the third export format, and the one a spreadsheet opens by itself.
# --------------------------------------------------------------------------------------- #


def test_export_csv_has_one_row_per_record(case):
    import csv
    import io

    client, cid, _ = case
    r = client.get(f"/case/{cid}/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f'filename="{cid}.csv"' in r.headers["content-disposition"]

    reader = csv.DictReader(io.StringIO(r.text))
    assert reader.fieldnames == [
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
    rows = list(reader)
    records = {row["record"] for row in rows}
    assert records == {"entity", "artefact", "edge", "finding"}

    graph = client.get(f"/api/case/{cid}/graph").json()
    assert sum(1 for row in rows if row["record"] == "entity") == graph["counts"]["entities"]
    assert sum(1 for row in rows if row["record"] == "edge") == graph["counts"]["edges"]


def test_export_csv_includes_notes(case):
    import csv
    import io

    client, cid, _ = case
    client.post(
        f"/case/{cid}/note",
        data={"actor": "g.kanojia", "body": 'comma, and "quotes"', "entity_id": ""},
        follow_redirects=False,
    )
    rows = list(csv.DictReader(io.StringIO(client.get(f"/case/{cid}/export.csv").text)))
    note = next(row for row in rows if row["record"] == "note")
    assert note["value"] == 'comma, and "quotes"'


def test_export_csv_neutralises_a_spreadsheet_formula(case):
    """A note or artefact can begin with '='; opening the export must not run it."""

    import csv
    import io

    client, cid, _ = case
    client.post(
        f"/case/{cid}/note",
        data={"actor": "g.kanojia", "body": "=HYPERLINK(\"http://x\",\"click\")", "entity_id": ""},
        follow_redirects=False,
    )
    rows = list(csv.DictReader(io.StringIO(client.get(f"/case/{cid}/export.csv").text)))
    note = next(row for row in rows if row["record"] == "note")
    assert note["value"].startswith("'=")
    assert note["value"][1:] == '=HYPERLINK("http://x","click")'


def test_export_csv_of_an_unknown_case_is_404(case):
    client, _, _ = case
    assert client.get("/case/NOPE/export.csv").status_code == 404


def test_new_case_button_builds_a_browsable_case(case):
    """The route's whole purpose: one click, and the console can show the case.

    The other creation tests stub the pipeline so they can pin argument passing. This one
    does not, because a demo whose button only works against a mock is the defect worth
    catching: it runs the real six-stage pipeline, then loads the graph, the timeline and
    the CSV from the case the browser just created.
    """

    client, _, tmp_path = case
    r = client.post(
        "/new",
        data={
            "case_id": "CASE-GUI",
            "seed": "20260925",
            "owner": "g.kanojia",
            "authorisation": "AUTH-GUI",
            "retention_days": "30",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/case/CASE-GUI"

    assert (tmp_path / "CASE-GUI" / "graph.db").is_file()

    page = client.get("/case/CASE-GUI")
    assert page.status_code == 200
    assert "Entity graph" in page.text

    assert client.get("/case/CASE-GUI/timeline").status_code == 200
    assert client.get("/case/CASE-GUI/export.csv").status_code == 200


# --------------------------------------------------------------------------------------- #
# The run report: the console showed the result of a run but never the run itself. Stage
# timings, counts and the generated narrative lived in the run directory, which meant a
# demo had to leave the browser to show the work.
# --------------------------------------------------------------------------------------- #

_CASE_FORM = {
    "seed": "20260925",
    "owner": "g.kanojia",
    "authorisation": "AUTH-GUI",
    "retention_days": "30",
}


def _create(client, case_id: str) -> None:
    r = client.post(
        "/new", data={**_CASE_FORM, "case_id": case_id}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == f"/case/{case_id}"


def test_run_report_shows_every_stage_and_the_generated_report(case):
    client, _, _ = case
    _create(client, "CASE-RUN")

    r = client.get("/case/CASE-RUN/report")
    assert r.status_code == 200
    for stage in ("farm", "gates", "detect", "correlate", "attribute", "graph"):
        assert f"<td>{stage}</td>" in r.text, stage
    assert "Generated report" in r.text
    assert "ONIONTRACE pipeline run" in r.text
    assert "Ground truth (farm)" in r.text
    # the download link has to be there or the page is a dead end for the record
    assert "/case/CASE-RUN/report.md" in r.text


def test_run_report_explains_a_case_built_without_the_pipeline(case):
    """The fixture case is a graph built directly; the case is real, the manifest is not."""

    client, cid, _ = case
    r = client.get(f"/case/{cid}/report")
    assert r.status_code == 200
    assert "No run manifest" in r.text
    assert "New case" in r.text


def test_run_report_markdown_is_a_downloadable_file(case):
    client, _, _ = case
    _create(client, "CASE-RUN-MD")

    r = client.get("/case/CASE-RUN-MD/report.md")
    assert r.status_code == 200
    assert 'filename="CASE-RUN-MD-REPORT.md"' in r.headers["content-disposition"]
    assert r.text.startswith("# ONIONTRACE pipeline run")


def test_run_report_of_an_unknown_case_is_404(case):
    client, _, _ = case
    assert client.get("/case/NOPE/report").status_code == 404
    assert client.get("/case/NOPE/report.md").status_code == 404


def test_run_report_survives_a_corrupt_manifest(case):
    """A half-written manifest must render as a case with no manifest, not a 500."""

    client, _, tmp_path = case
    _create(client, "CASE-CORRUPT")
    (tmp_path / "CASE-CORRUPT" / "run" / "manifest.json").write_text("{not json", encoding="utf-8")

    r = client.get("/case/CASE-CORRUPT/report")
    assert r.status_code == 200
    assert "No run manifest" in r.text


def test_run_report_summarises_a_long_stage_record(case):
    """The detect stage records every finding. Pasting them into one cell made the table
    unreadable and pushed every later stage off the screen; the page says how many and the
    manifest carries the record."""

    client, _, _ = case
    _create(client, "CASE-BRIEF")
    r = client.get("/case/CASE-BRIEF/report")
    assert "19 entries" in r.text
    assert "service_id" not in r.text


def test_run_manifest_is_a_downloadable_file(case):
    import json

    client, _, _ = case
    _create(client, "CASE-MANIFEST")
    r = client.get("/case/CASE-MANIFEST/manifest.json")
    assert r.status_code == 200
    assert 'filename="CASE-MANIFEST-manifest.json"' in r.headers["content-disposition"]
    data = json.loads(r.text)
    assert data["case_id"] == "CASE-MANIFEST"
    assert data["stages"][0]["stage"] == "farm"


# --------------------------------------------------------------------------------------- #
# Entity provenance and trust edges (PS stretch items)
# --------------------------------------------------------------------------------------- #
def _edge_block(html: str, edge_id: str) -> str:
    """The HTML for one link card, so an assertion about a form is about that edge."""

    start = html.index(f'data-edge-id="{edge_id}"')
    end = html.find('data-edge-id="', start + 1)
    if end == -1:
        end = html.index("</ol>", start)
    return html[start:end]


def test_the_entity_card_shows_category_last_scan_and_source(case):
    from graph.store import GraphStore

    client, cid, tmp_path = case
    with GraphStore(tmp_path / cid / "graph.db", case_id=cid) as store:
        entity = store.entities()[0]

    page = client.get(f"/case/{cid}/entity/{entity.entity_id}").text
    assert entity.category in page
    assert entity.last_scan in page
    assert entity.source in page
    assert "Category" in page and "last scan" in page and "source" in page


def test_csv_entity_rows_carry_the_same_provenance(case):
    import csv
    import io

    from graph.store import GraphStore

    client, cid, tmp_path = case
    with GraphStore(tmp_path / cid / "graph.db", case_id=cid) as store:
        entity = store.entities()[0]

    rows = list(
        csv.DictReader(io.StringIO(client.get(f"/case/{cid}/export.csv").text))
    )
    row = next(r for r in rows if r["record"] == "entity" and r["id"] == entity.entity_id)
    assert row["category"] == entity.category
    assert row["last_scan"] == entity.last_scan
    assert row["source"] == entity.source
    assert row["category"] and row["last_scan"] and row["source"]


def test_a_trust_edge_is_shown_and_offers_no_merge(case):
    """A vouch is a relationship; the console must not turn it into a merge button."""

    from graph.model import EdgeMethod
    from graph.store import GraphStore

    client, cid, tmp_path = case
    with GraphStore(tmp_path / cid / "graph.db", case_id=cid) as store:
        trust = next(e for e in store.edges() if e.method == EdgeMethod.TRUST.value)
        identity = next(e for e in store.edges() if e.method != EdgeMethod.TRUST.value)

    # Confirm both so the only thing that can hide the merge form is the method.
    for edge in (trust, identity):
        r = client.post(
            f"/case/{cid}/edge/{edge.edge_id}/status",
            data={"status": "confirmed", "actor": "analyst"},
            follow_redirects=False,
        )
        assert r.status_code == 303, (edge.edge_id, r.status_code)

    page = client.get(f"/case/{cid}").text
    trust_block = _edge_block(page, trust.edge_id)
    identity_block = _edge_block(page, identity.edge_id)

    assert "trust" in trust_block
    assert "mergeform" not in trust_block
    assert "mergeform" in identity_block, (
        "the identity edge must keep its merge control, or the absence above proves nothing"
    )


def test_the_trust_edge_score_is_not_a_confidence_number(case):
    from graph.model import EdgeMethod
    from graph.store import GraphStore

    client, cid, tmp_path = case
    with GraphStore(tmp_path / cid / "graph.db", case_id=cid) as store:
        trust = next(e for e in store.edges() if e.method == EdgeMethod.TRUST.value)
    assert trust.score == 0.0
    block = _edge_block(client.get(f"/case/{cid}").text, trust.edge_id)
    assert "0.000" in block
