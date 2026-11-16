"""The demo case's accuracy is measured, not asserted.

`scripts/qa/demo_accuracy.py` scores the case the console shows against the farm it was built
from. These tests run the same scoring and pin its headline numbers, because a demo that stops
being right should fail a test rather than quietly become a nicer-looking demo.

They skip when the case or the farm is absent, with the command that builds it, in the same way
the farm-table check in `test_docs.py` does. A skip here means nothing was measured, and the
battery says so rather than calling it a pass.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCORER = ROOT / "scripts" / "qa" / "demo_accuracy.py"
CASE = "CASE-DEMO"


def _scorer():
    """Load the scoring script as a module, so the test scores rather than parses stdout."""

    spec = importlib.util.spec_from_file_location("demo_accuracy", SCORER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load the scorer at {SCORER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scored() -> dict:
    module = _scorer()
    farm_path = ROOT / "out" / "farm.json"
    if not farm_path.exists():
        pytest.skip("no built farm to score against; run `python -m farm generate --out out` first")
    db = ROOT / "casework" / CASE / "graph.db"
    if not db.exists():
        pytest.skip(
            f"no built {CASE} to score; run `python -m oniontrace run --seed 20260925 "
            f"--case {CASE}` first"
        )
    farm = module.load_farm(farm_path)
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        return {
            "links": module.score_links(connection, farm),
            "findings": module.score_findings(connection, farm),
            "farm": farm,
        }
    finally:
        connection.close()


def test_every_proposed_link_is_a_real_operator_cluster(scored):
    """Precision is the claim the whole correlation module rests on, and it is per-link.

    The console shows twelve links either way. What makes them evidence is that all twelve are
    pairs of services the farm actually put in one cluster, and this test is what turns that
    from a screenshot into a measurement.
    """

    links = scored["links"]
    assert links["proposed"] > 0, "the demo case proposed no links at all"
    assert links["false_proposals"] == [], "a proposed link is not a real cluster pair"
    assert links["precision"] == 1.0


def test_no_true_pair_was_missed(scored):
    """Recall matters as much as precision here: silence is the safe answer, not the right one."""

    links = scored["links"]
    assert links["true"] > 0
    assert links["missed"] == [], "a true pair went unreported"
    assert links["recall"] == 1.0


def test_no_link_rests_only_on_a_widespread_artefact(scored):
    """The project's headline failure, checked on the demo case link by link.

    A stock favicon or a default analytics id matches byte for byte across thousands of unrelated
    deployments. A link whose entire support is that class of artefact is the failure mode the
    frequency cap exists to remove, so the demo case is required to contain none of them -- and
    the farm does contain 45 such fixtures for the run to avoid.
    """

    links = scored["links"]
    assert len(scored["farm"]["hard_negatives"]) > 0, "no hard negatives to avoid"
    assert links["held_by_widespread_only"] == []


def test_no_service_without_a_cluster_mate_is_linked(scored):
    """Six abstention cases say the right answer is silence; two services can never be linked."""

    links = scored["links"]
    assert scored["farm"]["abstention_cases"], "no abstention cases in the farm"
    assert links["singleton_links"] == [], "a singleton was merged with something"


def test_every_flaw_the_run_was_asked_to_find_has_a_finding(scored):
    """Findings are scored against single-service flaws, not against shared artefacts.

    A cluster-scope entry such as `shared_favicon` is linking evidence: it is what makes three
    services look like one operator, and it is scored in the link tests above. Counting it as a
    missing finding would report a recall of 19 of 37 for a run that found everything it was
    asked to find, which is a number about the scorer rather than about the detector.
    """

    findings = scored["findings"]
    assert findings["expected"] > 0
    assert findings["missed"] == [], "an injected flaw went unreported"
    assert findings["caught"] == findings["expected"]
    assert findings["shared_artefacts"] > 0, "the cluster-scope entries are not being separated"


def test_no_finding_fires_on_a_service_with_nothing_wrong(scored):
    """A detector that reports a flaw nobody injected is a false positive, not a bonus.

    Seven of the nineteen rules once had no negative fixture at all, so their share of a 19/19
    precision figure was unmeasured. This is the same question asked of the built case: is every
    recorded finding traceable to something the farm actually planted?
    """

    assert scored["findings"]["unexplained"] == []
