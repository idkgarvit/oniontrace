"""Tests that keep the README honest.

The README is the document a marker reads before running anything, so a stale figure in
it is a claim the project is not making anywhere else. Every count in this repository has
drifted at least once: the layout block cited 43 tests for a file holding 69, the status
paragraph cited 343 after 361 existed, and a test-file count was off by one.

Guarding those by hand fails the same way every time, so they are checked here instead.
Both checks are cheap and neither needs the optional dependencies.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# `  test_foo.py  25 tests; ...` inside the Layout block.
_LAYOUT_TEST_RE = re.compile(r"^\s{2}(test_[a-z_]+\.py)\s+(\d+)[- ]tests?", re.MULTILINE)


def _readme() -> str:
    assert README.exists(), "README.md is the entry document; its absence is a defect"
    return README.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def _collected_everywhere() -> dict[str, int]:
    """Test counts for the whole tests/ directory, in one collection pass.

    Collecting in-process would register these modules in this run's own suite and skew the very
    number being checked, so collection happens in a subprocess. One pass, not one per file: the
    ratchet tests together walked every test file, and a subprocess each time cost 17 seconds of a
    57 second suite to re-derive a number that cannot change while the run is going. The cache is
    per-process, so a fresh run still collects freshly.
    """

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    counts: dict[str, int] = {}
    for line in out.stdout.splitlines():
        entry = line.strip()
        if not entry.startswith("tests/") or "::" not in entry:
            continue
        counts[entry.split("::", 1)[0]] = counts.get(entry.split("::", 1)[0], 0) + 1
    if not counts:
        pytest.fail(f"could not collect tests/:\n{out.stdout}\n{out.stderr}")
    return counts


def _collected(path: Path) -> int:
    """How many tests one file contributes, read from the single pass above."""

    key = path.relative_to(ROOT).as_posix()
    counts = _collected_everywhere()
    if key not in counts:
        pytest.fail(f"could not collect {path}; the pass found {len(counts)} files")
    return counts[key]


def test_readme_test_counts_match_the_suite():
    """A count in the README that the suite contradicts is worse than no count."""

    text = _readme()
    cited = _LAYOUT_TEST_RE.findall(text)
    assert cited, "the layout block no longer cites per-file test counts; update this test"

    wrong: list[str] = []
    for name, claimed in cited:
        path = ROOT / "tests" / name
        if not path.exists():
            wrong.append(f"{name}: cited in README, file does not exist")
            continue
        actual = _collected(path)
        if int(claimed) != actual:
            wrong.append(f"{name}: README says {claimed}, suite collects {actual}")

    assert not wrong, "README test counts have drifted:\n  " + "\n  ".join(wrong)


def test_readme_layout_paths_exist():
    """Every module path shown in the layout block should be real.

    The layout is how a reader finds their way around; a path that was renamed and left
    behind sends them to nothing.
    """

    text = _readme()
    block = re.search(r"## Layout\n\n```\n(.*?)```", text, re.DOTALL)
    assert block, "the Layout block is gone; it is what this test checks"

    missing = []
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.endswith("py") is False and not stripped.endswith("/"):
            continue
        # Lines look like `  model.py       description` or `dashboard/`
        token = stripped.split()[0].rstrip(",")
        if token.endswith("/"):
            candidate = ROOT / token
        elif token.endswith(".py"):
            # a bare filename belongs to the section header above it; skip those here
            continue
        else:
            continue
        if not candidate.exists():
            missing.append(token)

    assert not missing, f"layout cites paths that do not exist: {missing}"


def test_readme_headline_numbers_are_not_superseded():
    """The status paragraph must not cite an older total than the suite reports.

    This is the specific drift that happened: `284 tests pass in total.` was left standing
    while the parenthetical beside it moved, so the document contradicted itself.
    """

    text = _readme()
    numbers = set(int(n) for n in re.findall(r"\b(\d{3}) tests pass in total\b", text))
    if not numbers:
        pytest.skip("the status paragraph no longer states a total")
    newer = set(int(n) for n in re.findall(r"\((\d{3}) today", text))
    if not newer:
        pytest.skip("no parenthetical total to compare against")
    assert max(numbers) <= max(newer), (
        f"README states {sorted(numbers)} as the total but {sorted(newer)} as the current "
        "count; the older figure contradicts the newer one in the same paragraph"
    )


def test_readme_does_not_promise_a_module_that_is_absent():
    """The console and graph are named in prose; both must exist to be runnable."""

    for module in ("graph", "dashboard", "oniontrace"):
        pkg = ROOT / module
        assert (pkg / "__init__.py").exists(), f"README names `{module}/` but it is absent"
        assert (pkg / "__main__.py").exists(), (
            f"README shows `python -m {module}` but there is no __main__.py to run"
        )


def test_every_documented_command_has_a_parser_entry_point():
    """`python -m X` in the README must reach a real CLI.

    Cheap structural check: the module exists and exposes a parser or a main. It does not
    execute the command, so it cannot break the machine it runs on.
    """

    text = _readme()
    modules = set(re.findall(r"python(?:3)? -m ([a-z_]+)", text))
    assert modules, "no `python -m` commands found in the README"

    import importlib

    broken = []
    for name in sorted(modules):
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - any import failure is the finding
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
            continue

        # `python -m X` runs X/__main__.py when X is a package, and X.py's
        # `if __name__ == "__main__"` block when X is a module. A package does not
        # expose `__main__` as an attribute until that submodule is imported, so the
        # file on disk is what to check.
        mod_path = Path(getattr(mod, "__file__", "") or "")
        try:
            mod_path.relative_to(ROOT)
        except ValueError:
            # An installed tool (`python -m pytest`), not a module of this project.
            continue
        if mod_path.name == "__init__.py":
            runner = mod_path.parent / "__main__.py"
            ok = runner.exists()
            where = runner.relative_to(ROOT)
        else:
            ok = mod_path.exists() and "if __name__" in mod_path.read_text(encoding="utf-8")
            where = mod_path.relative_to(ROOT)
        if not ok:
            broken.append(f"{name}: `python -m {name}` would not run ({where} missing or inert)")

    assert not broken, "README commands that do not resolve:\n  " + "\n  ".join(broken)


# --------------------------------------------------------------------------------------- #
# The milestone report carries numbers too, so the same rule applies to it.
# --------------------------------------------------------------------------------------- #

M2_REPORT = ROOT / "docs" / "m2" / "M2_REPORT.md"


def _m2() -> str:
    assert M2_REPORT.exists(), "the M2 report is a delivered artefact; its absence is a defect"
    return M2_REPORT.read_text(encoding="utf-8")


def test_m2_report_test_count_matches_the_suite():
    """The report states the suite size in three places. A milestone document that overstates its
    own evidence is the failure this whole file exists to prevent, so the number is checked against
    the suite rather than trusted."""
    text = _m2()
    cited = {int(n) for n in re.findall(r"(\d{3,4}) tests", text)}
    assert cited, "the report no longer states a test count; update this test"

    collected = sum(_collected(f) for f in sorted((ROOT / "tests").glob("test_*.py")))
    assert cited == {collected}, (
        f"the M2 report cites {sorted(cited)} tests; the suite collects {collected}"
    )


def test_m2_report_farm_table_matches_the_built_farm():
    """Every count in §3.2 is checked against out/farm.json, which is the artefact that produced it.

    The first draft of that table said "approximately six clean controls" because it was written
    from the plan's design target rather than from the farm that was built, which has two. The plan
    and the artefact disagreed and nobody would have noticed.
    """
    import json

    farm_path = ROOT / "out" / "farm.json"
    if not farm_path.exists():
        pytest.skip("no built farm to compare against; run `python -m farm write` first")
    farm = json.loads(farm_path.read_text(encoding="utf-8"))

    services = farm["services"]
    clean = sum(1 for s in services if not s.get("flaws"))
    expected = {
        "Services": len(services),
        "Deliberately clean controls": clean,
        "Injected flaw entries": len(farm["flaws"]),
        "Hard negatives (decoys and near-misses)": len(farm["hard_negatives"]),
        "Personas": len(farm["personas"]),
        "Abstention cases": len(farm["abstention_cases"]),
    }

    text = _m2()
    wrong = []
    for label, value in expected.items():
        row = re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*(\d+)", text, re.MULTILINE)
        if row is None:
            wrong.append(f"{label}: row not found in the report")
            continue
        if int(row.group(1)) != value:
            wrong.append(f"{label}: report says {row.group(1)}, farm.json says {value}")

    assert not wrong, "the M2 report's farm table has drifted from out/farm.json:\n  " + "\n  ".join(wrong)


def test_m2_report_verdict_matches_the_full_check():
    """A report may not claim a clean full check the project did not record."""
    import json

    result = ROOT / "out" / "full-check.json"
    if not result.exists():
        pytest.skip("no full-check result to compare against; run scripts/full_check.py first")
    recorded = json.loads(result.read_text(encoding="utf-8"))
    verdict = recorded.get("verdict") or ""
    counts = recorded.get("counts") or {}

    text = _m2()
    assert "all 10 stages passed" in text, "the report no longer states the full-check verdict"

    if counts.get("PASS", 0) != len(recorded.get("stages") or []):
        pytest.fail(
            f"the recorded full check is not clean ({counts}), so the report cannot say it is. "
            f"Recorded verdict: {verdict!r}"
        )
