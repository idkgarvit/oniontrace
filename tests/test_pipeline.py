"""Tests for the end-to-end pipeline and the code-mix feature block.

What is worth pinning here is not that the stages return numbers; it is the
properties that make the numbers trustworthy:

* a stage that fails is **recorded**, and the run still finishes with a manifest that
  says which answers are missing;
* adding the code-mix block did not move any existing model (the paper's figures have
  to survive a refactor);
* the code-mix detector measures zero mix on pure-English text, because a lexicon full
  of English homographs was exactly the bug that made it say otherwise;
* the graph lands where the console looks for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from farm.generator import generate_farm
from farm.model import FarmConfig, StyleRegime

fastapi = pytest.importorskip("fastapi")


@pytest.fixture(scope="module")
def farm():
    return generate_farm(FarmConfig(seed=20260925))


# -- code-mix block ----------------------------------------------------- #
def test_codemix_measures_no_mix_on_pure_english(farm):
    """The bug that mattered: an English article in the Hindi lexicon made every
    English persona register a 7 percent code-mix habit it did not have."""

    from eval.detectors import codemix

    english = [p for p in farm.personas if p.style is StyleRegime.PURE_ENGLISH]
    assert english, "farm generates no pure-English personas; test would be vacuous"

    worst = max(codemix.summarise(p.posts)["code_mix_ratio"] for p in english)
    assert worst == 0.0, f"pure-English persona measured {worst:.3f} mix"


def test_codemix_finds_mix_where_the_generator_put_it(farm):
    from eval.detectors import codemix

    mixed = [p for p in farm.personas if p.style is StyleRegime.HINGLISH_LIGHT]
    assert mixed
    observed = sum(codemix.summarise(p.posts)["code_mix_ratio"] for p in mixed) / len(mixed)
    # The generator mixes at roughly 20 percent. The detector does not need to be
    # exact, but a number near zero or near one would mean it is measuring itself.
    assert 0.10 < observed < 0.50, f"detected {observed:.3f}, expected around 0.20"


def test_lexicon_overlap_is_reported_not_assumed():
    """The detector shares vocabulary with the generator. That is disclosed, and this
    test exists so the disclosure cannot quietly disappear from the module."""

    from eval.detectors import codemix
    from farm import language

    gen = (
        set(language.HINGLISH_FUNCTION_WORDS)
        | set(language.HINGLISH_DISCOURSE_MARKERS)
        | set(language.HINGLISH_CONTENT_TOKENS)
    )
    overlap = codemix.lexicon_overlap(gen)
    assert overlap["shared"] > 0
    assert overlap["share_of_detector"] < 1.0, (
        "a detector lexicon entirely contained in the generator vocabulary cannot be "
        "argued to generalise; the overlap is supposed to be partial"
    )


def test_ambiguous_tokens_are_resolved_by_context():
    from eval.detectors import codemix

    class P:
        def __init__(self, text):
            self.text = text

    # "par" alone is not evidence of Hindi; "par" between Hindi words is.
    assert codemix.code_mix_ratio(P("a par of four on the course")) == 0.0
    assert codemix.code_mix_ratio(P("yeh theek hai par woh nahi")) > 0.0


# -- models ------------------------------------------------------------- #
def test_existing_models_are_unchanged_by_the_new_block(farm):
    """The paper quotes these. A refactor must not move them."""

    from eval.detectors import attribution

    result = attribution.evaluate(farm, models=("combined",))
    long_hit = result["models"]["combined"]["long"]["hit@1"]["value"]
    short_hit = result["models"]["combined"]["short"]["hit@1"]["value"]
    assert (long_hit, short_hit) == (0.5, pytest.approx(0.357143, abs=1e-5))


def test_gated_model_scores_and_never_falls_below_ungated(farm):
    from eval.detectors import attribution

    result = attribution.evaluate(farm, models=("combined", "combined_gated"))
    combined = result["models"]["combined"]
    gated = result["models"]["combined_gated"]
    # The gate can only add features for mixed writers, so it cannot make monolingual
    # text worse than the model it extends.
    assert gated["long"]["hit@1"]["value"] >= combined["long"]["hit@1"]["value"]
    assert gated["short"]["hit@1"]["value"] >= combined["short"]["hit@1"]["value"]


# -- pipeline ----------------------------------------------------------- #
def test_pipeline_runs_every_stage_and_writes_a_manifest(tmp_path):
    from oniontrace.pipeline import run_pipeline

    result = run_pipeline(
        seed=20260925, case_id="CASE-T", output_dir=tmp_path / "run",
        case_root=tmp_path / "casework",
    )
    assert result.ok, [s.to_dict() for s in result.stages]
    assert {s.name for s in result.stages} == {
        "farm", "gates", "detect", "correlate", "attribute", "graph"
    }
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert manifest["ok"] is True
    assert len(manifest["stages"]) == 6
    assert (tmp_path / "run" / "REPORT.md").exists()


def test_graph_is_written_where_the_console_looks(tmp_path):
    from oniontrace.pipeline import run_pipeline

    result = run_pipeline(
        seed=20260925, case_id="CASE-W", output_dir=tmp_path / "run",
        case_root=tmp_path / "casework",
    )
    assert result.ok
    assert (tmp_path / "casework" / "CASE-W" / "graph.db").exists()


def test_a_failing_stage_is_recorded_and_does_not_abort_the_run(tmp_path, monkeypatch):
    """The failure mode this guards: a pipeline that stops silently and still reports
    success, leaving a reader to believe four modules found nothing."""

    from oniontrace import pipeline as mod

    def explode(farm_obj):
        raise RuntimeError("detector unavailable")

    monkeypatch.setattr(mod, "stage_detect", explode)

    result = mod.run_pipeline(
        seed=20260925, case_id="CASE-F", output_dir=tmp_path / "run",
        case_root=tmp_path / "casework",
    )
    detect = result.stage("detect")
    assert detect is not None and not detect.ok
    assert "detector unavailable" in (detect.error or "")
    # The stages after it still ran, and the run is marked not-ok.
    assert result.stage("graph") is not None and result.stage("graph").ok
    assert result.ok is False


def test_report_names_the_limits_of_its_own_numbers(tmp_path):
    from oniontrace.pipeline import run_pipeline

    result = run_pipeline(
        seed=20260925, case_id="CASE-R", output_dir=tmp_path / "run",
        case_root=tmp_path / "casework",
    )
    text = (tmp_path / "run" / "REPORT.md").read_text()
    assert "does not establish" in text
    assert "synthetic" in text.lower()
    assert "four mixed-regime personas" in text


def test_run_is_reproducible_for_the_same_seed(tmp_path):
    """Two runs at the same seed must agree on every scored figure."""

    from oniontrace.pipeline import run_pipeline

    a = run_pipeline(seed=20260925, case_id="C1", output_dir=tmp_path / "a",
                     case_root=tmp_path / "ca")
    b = run_pipeline(seed=20260925, case_id="C2", output_dir=tmp_path / "b",
                     case_root=tmp_path / "cb")

    def figures(res):
        return {
            s.name: {k: v for k, v in s.summary.items() if isinstance(v, (int, float))}
            for s in res.stages
        }

    assert figures(a) == figures(b)


def test_cli_gives_each_case_its_own_run_directory(tmp_path, monkeypatch):
    """Two cases must not share a manifest.

    The defect this guards: `--out` used to default to one directory, so running a
    second case silently overwrote the first case's manifest and the report that went
    with it.
    """

    from oniontrace import cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "--seed", "20260925", "--case", "CASE-A"]) == 0
    assert cli.main(["run", "--seed", "20260925", "--case", "CASE-B"]) == 0

    a = tmp_path / "out" / "CASE-A-20260925" / "manifest.json"
    b = tmp_path / "out" / "CASE-B-20260925" / "manifest.json"
    assert a.exists() and b.exists()
    assert json.loads(a.read_text())["case_id"] == "CASE-A"
    assert json.loads(b.read_text())["case_id"] == "CASE-B"
    # and the console looks in ./casework by default
    assert (tmp_path / "casework" / "CASE-A" / "graph.db").exists()


def test_cli_exit_code_reflects_failure(tmp_path, monkeypatch):
    from oniontrace import cli, pipeline as mod

    monkeypatch.setattr(mod, "stage_detect", lambda farm_obj: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = cli.main([
        "run", "--seed", "20260925", "--case", "CASE-X",
        "--out", str(tmp_path / "run"), "--case-root", str(tmp_path / "cw"),
    ])
    assert rc == 1
