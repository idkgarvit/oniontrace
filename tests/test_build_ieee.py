"""The IEEE build has to survive its own contents, not just compile.

Two defects this file now pins were found in the rendered PDF rather than in the source: a
three-column table ran 330 points past the column edge and printed over the neighbouring
column, and a 56-character onion address ran 42 points into the margin. Both compiled without
an error, which is why they survived until someone looked at the log and the page.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("build_ieee", ROOT / "paper" / "build_ieee.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_ieee"] = module
    spec.loader.exec_module(module)
    return module


build = _module()

V3 = "wgbn2pibrxhgddhktgq5jdhw77w6t45gz72ho4inlvabpykexyvwe7id.onion"


class TestTables:
    def test_a_table_becomes_a_tabular_not_a_verbatim_dump(self):
        tex = "\n".join(build._table(["| a | b |", "|---|---|", "| 1 | 2 |"]))
        assert r"\begin{tabular}" in tex
        assert r"\begin{verbatim}" not in tex

    def test_every_column_wraps(self):
        tex = "\n".join(build._table(["| a | b | c |", "|---|---|---|", "| 1 | 2 | 3 |"]))
        assert tex.count(r"p{") == 3

    def test_the_column_widths_leave_room_for_the_padding(self):
        # Sizing the p-columns to the full column width made the table overfull by exactly the
        # inter-column space, 25 points across five columns.
        rows = ["| a | b | c | d | e |", "|---|---|---|---|---|", "| 1 | 2 | 3 | 4 | 5 |"]
        tex = "\n".join(build._table(rows))
        widths = [float(f) for f in re.findall(r"p\{([\d.]+)\\columnwidth\}", tex)]
        assert len(widths) == 5
        assert sum(widths) < 1.0

    def test_a_long_word_sets_a_floor_the_short_cells_cannot_lower(self):
        # A column of numbers was sized for a number, and its header "precision" then printed
        # three points past the edge.
        tex = "\n".join(
            build._table(["| precision | x |", "|---|---|", "| 0.006 | 1 |"])
        )
        assert r"\textbf{precision}" in tex
        assert "p{0.1" in tex or "p{0.2" in tex or "p{0.3" in tex

    def test_cell_text_is_escaped(self):
        tex = "\n".join(build._table(["| a | b |", "|---|---|", "| 50% | x_y |"]))
        assert r"50\%" in tex
        assert r"x\_y" in tex

    def test_a_row_with_fewer_cells_than_the_header_does_not_crash(self):
        tex = "\n".join(build._table(["| a | b | c |", "|---|---|---|", "| 1 |"]))
        assert r"\begin{tabular}" in tex


class TestOnionAddresses:
    def test_a_v3_address_can_break_without_gaining_a_character(self):
        rendered = build.esc(V3)
        assert r"\allowbreak" in rendered
        assert "-" not in rendered.replace(r"\allowbreak", "")
        # Nothing was dropped: the halves and the suffix reassemble the address exactly.
        pieces = re.findall(r"\\texttt\{([a-z2-7]+)\}", rendered)
        assert "".join(pieces) + ".onion" == V3

    def test_a_v2_address_is_left_alone(self):
        address = "abcdefghij234567.onion"
        assert build.esc(address) == r"\texttt{abcdefghij234567}.onion"

    def test_a_clearnet_domain_is_not_touched(self):
        assert build.esc("example.com") == "example.com"

    def test_an_address_inside_a_code_span_is_not_double_wrapped(self):
        rendered = build.esc(f"`{V3}`")
        assert rendered.count(r"\texttt{") == 1

    @pytest.mark.parametrize("text", [V3, "no address here", "a b c"])
    def test_escaping_is_idempotent_in_shape(self, text):
        once = build.esc(text)
        assert build.esc(once) == once or r"\allowbreak" in once


class TestWhereTheSourceComesFrom:
    """The build has to run on a machine other than the one the paper was written on.

    It used to read a hardcoded home directory, which is a path that works for exactly one
    person. The resolution order is now: the explicit variable, a copy vendored into the
    repository, the retired workspace path -- and a variable that points nowhere is an
    error rather than a silent fallback.
    """

    def test_the_env_var_wins(self, tmp_path, monkeypatch):
        chosen = tmp_path / "custom.md"
        chosen.write_text("# chosen\n", encoding="utf-8")
        monkeypatch.setenv("ONIONTRACE_PAPER_MD", str(chosen))
        assert build.paper_source() == chosen

    def test_a_bad_env_var_is_an_error_not_a_silent_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build, "ROOT", tmp_path)
        monkeypatch.setattr(build, "LEGACY_SRC", tmp_path / "legacy.md")
        monkeypatch.setenv("ONIONTRACE_PAPER_MD", str(tmp_path / "missing.md"))

        with pytest.raises(FileNotFoundError, match="ONIONTRACE_PAPER_MD"):
            build.paper_source()

    def test_a_repo_copy_beats_the_retired_workspace_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ONIONTRACE_PAPER_MD", raising=False)
        vendored = tmp_path / "paper.md"
        vendored.write_text("# vendored\n", encoding="utf-8")
        legacy = tmp_path / "legacy.md"
        legacy.write_text("# legacy\n", encoding="utf-8")
        monkeypatch.setattr(build, "ROOT", tmp_path)
        monkeypatch.setattr(build, "LEGACY_SRC", legacy)

        assert build.paper_source() == vendored

    def test_no_source_anywhere_is_none_and_the_build_says_so(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ONIONTRACE_PAPER_MD", raising=False)
        monkeypatch.setattr(build, "ROOT", tmp_path)
        monkeypatch.setattr(build, "LEGACY_SRC", tmp_path / "legacy.md")

        assert build.paper_source() is None
        # A build with nothing to read fails loudly instead of writing an empty paper.
        monkeypatch.setattr(build, "paper_source", lambda: None)
        assert build.main() == 1
