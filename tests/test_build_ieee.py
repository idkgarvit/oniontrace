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
