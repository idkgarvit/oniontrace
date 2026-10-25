"""Tests for the paper data drop builder.

The drop is the file a reader uses to check a number in the report against the run that
produced it, so the thing that matters about it is where its numbers come from. The first
version read ``/tmp/seed_<n>/eval-results.json`` if it happened to exist, which meant it
could cite an earlier run's output -- or crash after a reboot -- and either way nobody
could see from the output which had happened.

These tests never run the real evaluator: they stand in for ``subprocess.run`` and check
what the builder does with the directory it asked for, and what it does when the run fails.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _module():
    """Load scripts/build_paper_drop.py without it being importable as a package member."""

    spec = importlib.util.spec_from_file_location(
        "build_paper_drop_under_test", ROOT / "scripts" / "build_paper_drop.py"
    )
    assert spec and spec.loader, "scripts/build_paper_drop.py must stay loadable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    m = _module()
    m._CACHE.clear()
    return m


def _fake_run(payload: dict | None, returncode: int = 0):
    """A stand-in for the eval subprocess that writes into the ``--out`` it was handed."""

    calls: list[list[str]] = []

    def run(cmd, cwd=None, capture_output=True, text=True, timeout=None):
        calls.append(list(cmd))
        out = Path(cmd[cmd.index("--out") + 1])
        if payload is not None:
            (out / "eval-results.json").write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_a_seed_run_is_written_where_this_process_asked(module, monkeypatch):
    """The result must come from the directory passed on the command line.

    This is the property that stopped the builder reading a stale temp file: whatever it
    parses has to be the file the run it just started produced.
    """

    fake = _fake_run({"seed": "20260927", "from": "the run this call started"})
    monkeypatch.setattr(module.subprocess, "run", fake)

    result = module.results_for("20260927")

    assert result["from"] == "the run this call started"
    assert len(fake.calls) == 1
    cmd = fake.calls[0]
    out = Path(cmd[cmd.index("--out") + 1])
    assert cmd[:4] == [cmd[0], "-m", "eval", "run"]
    assert out.name.startswith("oniontrace-seed-20260927-"), (
        "the run must go into a directory this process created and owns, not a fixed "
        f"path shared with other runs; got {out}"
    )


def test_a_failed_eval_run_is_reported_rather_than_parsed(module, monkeypatch, tmp_path):
    """A non-zero run is an error to state, not a partial file to parse.

    The evaluator writes as it goes, so a run that dies halfway leaves a valid-looking
    ``eval-results.json`` behind. Parsing that would put half a run's numbers into the
    drop under the name of a complete one, so the exit code is checked as well as the
    file -- and this fixture writes the file on purpose, so a check that only looks for
    its existence cannot pass here.
    """

    monkeypatch.setattr(
        module.subprocess, "run", _fake_run({"partial": True}, returncode=2)
    )

    with pytest.raises(SystemExit) as excinfo:
        module.results_for("20260927")

    message = str(excinfo.value)
    assert "20260927" in message, "the failure must name the seed that failed"
    assert "rc=2" in message, "the failure must report the exit code it got"
    assert "boom" in message, "the failure must carry the run's own stderr"


def test_the_same_seed_is_asked_for_once_per_process(module, monkeypatch):
    """``main`` asks for the first seed twice; that is a cache hit, not two runs."""

    fake = _fake_run({"seed": "20260927"})
    monkeypatch.setattr(module.subprocess, "run", fake)

    first = module.results_for("20260927")
    second = module.results_for("20260927")

    assert first is second, "a repeated seed must not be re-read as a different object"
    assert len(fake.calls) == 1, "a repeated seed must not start a second evaluator run"
