"""Tests that keep the README honest.

The README is the document a marker reads before running anything, so a stale figure in
it is a claim the project is not making anywhere else. Every count in this repository has
drifted at least once: the layout block cited 43 tests for a file holding 69, the status
paragraph cited 343 after 361 existed, and a test-file count was off by one.

Guarding those by hand fails the same way every time, so they are checked here instead.
Both checks are cheap and neither needs the optional dependencies.
"""

from __future__ import annotations

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


def _collected(path: Path) -> int:
    """Count tests without importing them into this session.

    Collecting in-process would register the module in this run's own suite and skew the
    very number being checked.
    """

    out = subprocess.run(
        [sys.executable, "-m", "pytest", str(path), "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    match = re.search(r"(\d+) tests? collected", out.stdout)
    if match is None:
        pytest.fail(f"could not collect {path}:\n{out.stdout}\n{out.stderr}")
    return int(match.group(1))


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
