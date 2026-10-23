"""Every third-party import has to be in requirements.txt, or a fresh clone breaks.

The repository already shipped with `collect/ctlog_direct.py` importing `cryptography` while
requirements.txt did not list it. Nothing failed, because the author's machine had it installed
for another reason -- which is exactly how this class of bug survives: the person testing is
the person who never had to install it.

So this walks the source, finds what is imported from outside the standard library and outside
this repository, and insists each one appears in the manifest under its distribution name.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# An import name is not always a distribution name. Everything not listed here is assumed to
# match, which is true for requests, cryptography, fastapi, uvicorn, weasyprint, pytest,
# websockets and numpy.
DISTRIBUTIONS = {
    "PIL": "pillow",
    "pptx": "python-pptx",
    "multipart": "python-multipart",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
}

SKIP_DIRS = {".git", "__pycache__", "out", "tmp", "node_modules", ".venv", "venv", ".mypy_cache"}


def normalise(name: str) -> str:
    """PEP 503: pyproject and pip both compare distributions this way."""

    return re.sub(r"[-_.]+", "-", name).lower()


def declared() -> set[str]:
    names = set()
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0]
        if requirement:
            names.add(normalise(requirement))
    return names


def local_names() -> set[str]:
    """Anything importable from inside this repository is not a dependency."""

    names = set()
    for path in ROOT.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        names.add(path.stem)
        for parent in path.parents:
            if (parent / "__init__.py").exists():
                names.add(parent.name)
    return names


def imported_modules() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        if SKIP_DIRS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as broken:
            raise AssertionError(f"{path} does not parse, so its imports are unknown: {broken}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.setdefault(alias.name.split(".")[0], set()).add(str(path))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.setdefault(node.module.split(".")[0], set()).add(str(path))
    return found


def test_every_third_party_import_is_declared():
    manifest = declared()
    internal = local_names()
    missing = {}
    for module, files in imported_modules().items():
        if module in sys.stdlib_module_names or module in internal or module.startswith("_"):
            continue
        if normalise(DISTRIBUTIONS.get(module, module)) in manifest:
            continue
        missing[module] = sorted(files)
    assert not missing, "imported but not in requirements.txt: " + "; ".join(
        f"{module} (from {', '.join(files[:2])})" for module, files in sorted(missing.items())
    )


def test_the_manifest_is_not_a_wish_list():
    """Every declared line has to be a real requirement, not a comment or a stray word."""

    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        assert re.match(r"^[A-Za-z0-9._-]+(\[[^\]]+\])?\s*([<>=!~]=?\s*[\w.*+]+)?$", line), (
            f"not a requirement: {line!r}"
        )


def test_certificate_transparency_has_its_dependency():
    """The specific bug this file was written after, pinned by name."""

    assert "cryptography" in declared()
