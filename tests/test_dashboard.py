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
from fastapi.testclient import TestClient  # noqa: E402

import importlib  # noqa: E402

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
            ["pdftotext", "-layout", path, "-"], capture_output=True, text=True
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
    client, cid, tmp_path = case
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
