"""The harness has one job: never report success it cannot see.

An exit code says a program finished. It does not say the program measured anything, and this
project has already shipped two stages that exited 0 while reporting a refusal as an empty
result. So the contract is tested from both sides: a stage that prints its verdict passes, a
stage that exits 0 and prints nothing is UNPROVEN, and every claim function is fed the output
that should defeat it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _harness():
    spec = importlib.util.spec_from_file_location(
        "full_check", ROOT / "scripts" / "full_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module has to be registered before it runs: dataclasses look up their own module in
    # sys.modules while the class body is being evaluated, and an unregistered module makes
    # that lookup return None.
    sys.modules["full_check"] = module
    spec.loader.exec_module(module)
    return module


harness = _harness()


def _stage(command: str, claim: Any = None, tor: bool = False) -> Any:
    return harness.Stage(
        name="probe",
        command=command,
        why="a probe",
        claim=claim or harness.verdict_line,
        needs_tor=tor,
    )


# --- the verdict line ---------------------------------------------------------------


def test_a_verdict_line_is_the_summary():
    proved, sentence = harness.verdict_line("noise\nVERDICT: 36 checks passed\n", "")
    assert proved
    assert sentence == "36 checks passed"


def test_the_last_verdict_line_wins():
    proved, sentence = harness.verdict_line(
        "VERDICT: first claim\nmore noise\nVERDICT: second claim\n", ""
    )
    assert proved
    assert sentence == "second claim"


def test_output_with_no_verdict_line_proves_nothing():
    proved, reason = harness.verdict_line("everything looks fine to me\n", "")
    assert not proved
    assert "no VERDICT line" in reason


def test_a_verdict_line_too_short_to_mean_anything_is_rejected():
    proved, reason = harness.verdict_line("VERDICT: ok\n", "")
    assert not proved
    assert "too short" in reason


# --- the test suite's own summary ---------------------------------------------------


def test_a_real_pytest_summary_is_a_verdict():
    proved, sentence = harness.pytest_claim("460 passed in 46.17s\n", "")
    assert proved
    assert sentence == "460 tests passed in 46.17s"


def test_a_pytest_summary_with_a_failure_in_the_output_is_not_a_verdict():
    """pytest prints both lines when a run is half green; the count alone is not enough."""

    output = "3 failed, 460 passed in 46.17s\n"
    proved, reason = harness.pytest_claim(output, "")
    assert not proved
    assert "failed" in reason


def test_a_suite_that_shrank_is_not_a_pass():
    output = f"{harness.MIN_TESTS - 1} passed in 4.0s\n"
    proved, reason = harness.pytest_claim(output, "")
    assert not proved
    assert "floor" in reason


def test_no_summary_line_is_not_a_pass():
    """A collection error prints nothing that looks like a summary."""

    proved, reason = harness.pytest_claim("ImportError: no module named bwrap\n", "")
    assert not proved
    assert "summary line" in reason


# --- the pipeline proves itself with a file, not with stdout ------------------------


def test_the_manifest_is_the_evidence(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "stages": [{"stage": "farm", "ok": True}, {"stage": "gates", "ok": True}],
            }
        ),
        encoding="utf-8",
    )
    proved, sentence = harness.pipeline_claim(manifest)("", "")
    assert proved
    assert sentence == "2 of 2 stages ok, run run-1"


def test_a_manifest_with_a_failed_stage_is_not_a_pass(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"stages": [{"stage": "graph", "ok": False}]}), encoding="utf-8"
    )
    proved, reason = harness.pipeline_claim(manifest)("", "")
    assert not proved
    assert "graph" in reason


def test_a_missing_manifest_is_not_a_pass(tmp_path: Path):
    proved, reason = harness.pipeline_claim(tmp_path / "nothing.json")("", "")
    assert not proved
    assert "no manifest" in reason


# --- and the one that matters -------------------------------------------------------


def test_a_stage_that_exits_zero_without_a_verdict_is_unproven(tmp_path: Path):
    """The whole point. `true` succeeds; it also proves nothing."""

    stage = _stage("true")
    harness.run(stage, tmp_path / "absent.sock")
    assert stage.status == "UNPROVEN"
    assert "no VERDICT line" in stage.summary


def test_a_stage_that_states_its_verdict_passes(tmp_path: Path):
    stage = _stage("echo 'VERDICT: 12 checks passed'")
    harness.run(stage, tmp_path / "absent.sock")
    assert stage.status == "PASS"
    assert stage.summary == "12 checks passed"


def test_a_stage_that_fails_is_failed_whatever_it_printed(tmp_path: Path):
    stage = _stage("echo 'VERDICT: everything is wonderful'; exit 3")
    harness.run(stage, tmp_path / "absent.sock")
    assert stage.status == "FAIL"


def test_a_stage_without_its_precondition_is_skipped_never_passed(tmp_path: Path):
    stage = _stage("echo 'VERDICT: 9 checks passed'", tor=True)
    harness.run(stage, tmp_path / "absent.sock")
    assert stage.status == "SKIP"
    assert "no Tor socket" in stage.summary
    assert stage.notes and "tor_disposable.sh start" in stage.notes[0]


def test_a_socket_file_with_no_tor_behind_it_is_a_skip_not_a_failure(tmp_path: Path):
    """A dead tor leaves its socket behind, and socat's signal number is not a finding.

    This is how two stages reported FAIL once: the bridge was started against a socket whose
    process had exited, socat died, and the harness printed "exiting on signal 15" as if the
    environment check had measured something. The socket file proves nothing; one connect
    attempt does.
    """
    import socket as socket_module
    import tempfile

    # A unix socket path is capped at 108 bytes, and pytest's tmp_path is longer than that
    # once the test name is in it. This project has already lost an afternoon to that limit.
    short = Path(tempfile.mkdtemp(dir="/tmp"))
    stale = short / "stale.sock"
    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()  # the file stays; nothing is listening

    stage = _stage("echo 'VERDICT: 5 of 5 preflight checks proved'", tor=True)
    harness.run(stage, stale)
    assert stage.status == "SKIP"
    assert "nothing behind it" in stage.summary
    assert stage.notes and "tor_disposable.sh start" in stage.notes[0]


def test_a_socket_with_a_listener_behind_it_runs_the_stage(tmp_path: Path):
    import socket as socket_module
    import tempfile

    short = Path(tempfile.mkdtemp(dir="/tmp"))
    live = short / "live.sock"
    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    server.bind(str(live))
    server.listen(1)
    try:
        stage = _stage("echo 'VERDICT: 5 of 5 preflight checks proved'", tor=True)
        harness.run(stage, live)
    finally:
        server.close()
    assert stage.status == "PASS"


def test_an_unproven_stage_fails_the_run(tmp_path: Path):
    results = [_stage("true"), _stage("echo 'VERDICT: 3 checks passed'")]
    for stage in results:
        harness.run(stage, tmp_path / "absent.sock")
    report = harness._counts(results)
    assert report["UNPROVEN"] == 1
    assert report["PASS"] == 1


@pytest.mark.parametrize("status", ["PASS", "FAIL", "UNPROVEN", "SKIP", "running"])
def test_every_status_has_a_colour(status: str):
    assert status in harness.COLOURS


# --- a skipped stage is not a pass --------------------------------------------------


def _finished(status: str, name: str = "probe") -> Any:
    """A stage in a terminal state, without running anything."""
    stage = _stage("true")
    stage.name = name
    stage.status = status
    stage.summary = {"PASS": "12 checks passed", "SKIP": "no Tor socket"}.get(status, status)
    return stage


def test_a_run_with_a_skipped_stage_does_not_claim_every_check_passed():
    """The banner is the part people read, and it disagreed with its own report.

    A run that skipped the two live stages printed "ALL CHECKS PASSED" over two amber SKIP
    rows, printed "verdict: all checks passed", and exited 0. Every sentence of that was
    false about a check that never ran.
    """
    sentence, code = harness._verdict([_finished("PASS"), _finished("SKIP", "Live check")])

    assert code != 0
    assert "all checks passed" not in sentence.lower()
    assert "1 were skipped" in sentence or "1 was skipped" in sentence
    assert "Live check" in sentence


def test_the_banner_is_amber_and_names_the_skip_count(tmp_path: Path):
    results = [_finished("PASS", "suite"), _finished("SKIP", "Live check")]
    destination = tmp_path / "report.html"
    harness.render(results, {"when": "", "commit": "", "branch": "", "tree": "", "host": ""},
                   destination)
    page = destination.read_text(encoding="utf-8")

    assert "ALL CHECKS PASSED" not in page
    assert "1 PASSED, 1 SKIPPED" in page
    assert harness.COLOURS["SKIP"][0] in page


def test_a_clean_run_still_says_it_passed(tmp_path: Path):
    """The strict rule must not turn a genuine green run into an amber one."""
    results = [_finished("PASS", "suite"), _finished("PASS", "paper")]
    sentence, code = harness._verdict(results)

    assert (sentence, code) == ("all 2 stages passed", 0)
    destination = tmp_path / "report.html"
    harness.render(results, {"when": "", "commit": "", "branch": "", "tree": "", "host": ""},
                   destination)
    assert "ALL CHECKS PASSED" in destination.read_text(encoding="utf-8")


def test_the_machine_readable_result_carries_the_verdict(tmp_path: Path):
    """It shipped without one, so a consumer had to guess success from the rows.

    The first consumer to ask for it read ``result["verdict"]`` from a passing run and got
    None, which is exactly the shape of a bug that survives a green run.
    """
    results = [_finished("PASS", "suite"), _finished("SKIP", "Live check")]
    destination = tmp_path / "result.json"
    harness.write_json(results, {"commit": "abc"}, destination, "1 of 2 stages passed")
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert payload["verdict"] == "1 of 2 stages passed"
    assert payload["passed"] is False
    assert payload["incomplete"] is True
    assert payload["counts"]["SKIP"] == 1
    assert payload["stages"][0]["status"] == "PASS"


def test_the_json_says_passed_only_when_every_stage_ran_and_passed(tmp_path: Path):
    destination = tmp_path / "result.json"
    harness.write_json([_finished("PASS")], {"commit": "abc"}, destination, "all 1 stages passed")
    assert json.loads(destination.read_text(encoding="utf-8"))["passed"] is True

    harness.write_json([_finished("FAIL")], {"commit": "abc"}, destination, "a failure")
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["incomplete"] is False
