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
FARM = ROOT / "out" / "farm.json"


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


@pytest.fixture
def case(tmp_path, monkeypatch):
    """A real case built from the shipped farm into a throwaway root."""

    if not FARM.exists():
        pytest.skip("out/farm.json not generated")

    monkeypatch.setenv("ONIONTRACE_CASES", str(tmp_path))
    monkeypatch.setattr(app_module, "CASE_ROOT", tmp_path)

    from graph.cli import main as graph_main

    db = tmp_path / "CASE-T" / "graph.db"
    rc = graph_main(
        ["--db", str(db), "--case", "CASE-T", "build", "--farm", str(FARM)]
    )
    assert rc == 0
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


def test_evidence_pack_is_a_pdf_that_states_its_limits(case):
    client, cid, _ = case
    r = client.get(f"/case/{cid}/evidence.pdf")
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"

    text = _pdf_text(r.content)
    assert "candidate" in text.lower()
    assert "not a chain of custody" in text.lower()
    assert cid in text
