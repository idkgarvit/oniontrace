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

import json
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
