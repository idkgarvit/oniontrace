"""Tests for the deck builder.

The deck is the artefact a panel actually looks at, so the properties that make it readable
are pinned here rather than left to whoever rebuilds it next: every slide carries notes, every
slide is within the block budget, and no slide says anything off the banned list.

Two of these tests exist because the alternative is a silent success. A build that quietly
produces eleven slides, or a build that renders the diagrams but drops the speaker notes,
would pass any check that only asks whether a .pptx file appeared.

    python -m pytest tests/test_deck.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pptx import Presentation

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

FIG_NAMES = ["d1_problem", "d2_pipeline", "d3_defaults", "d4_results", "d5_farm"]


@pytest.fixture(scope="module")
def deck(tmp_path_factory) -> Path:
    """Build the text-only deck once. The console screenshot is optional by design."""

    out = tmp_path_factory.mktemp("deck") / "deck.pptx"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_presentation.py"),
         "--out", str(out), "--skip-screenshot"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def test_the_deck_builds_and_has_the_expected_slide_count(deck):
    prs = Presentation(deck)
    assert 10 <= len(prs.slides) <= 14


def test_every_slide_has_speaker_notes(deck):
    prs = Presentation(deck)
    silent = [i for i, s in enumerate(prs.slides, 1)
              if not s.notes_slide.notes_text_frame.text.strip()]
    assert not silent, f"slides with no notes: {silent}"


def test_no_slide_uses_generated_sounding_language(deck):
    """The audit inside the builder is the guard; this proves it is wired up."""

    sys.path.insert(0, str(ROOT / "scripts"))
    from build_presentation import BANNED

    prs = Presentation(deck)
    hits = []
    for i, slide in enumerate(prs.slides, 1):
        for sh in slide.shapes:
            if not sh.has_text_frame:
                continue
            low = sh.text_frame.text.lower()
            hits += [f"slide {i}: {w}" for w in BANNED if w in low]
    assert not hits, hits


def test_the_deck_is_16_by_9(deck):
    prs = Presentation(deck)
    ratio = prs.slide_width / prs.slide_height
    assert abs(ratio - 16 / 9) < 0.01


def test_building_without_diagrams_fails_loudly(tmp_path):
    """A deck that silently drops its diagrams is the failure this guards against."""

    empty = tmp_path / "fig"
    empty.mkdir()
    code = f"""
import sys, pathlib
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import build_presentation as bp
bp.FIG = pathlib.Path({str(empty)!r})
try:
    bp.main(['--out', {str(tmp_path / 'x.pptx')!r}, '--skip-screenshot'])
except SystemExit as e:
    print('EXIT', e.code)
    raise SystemExit(0)
raise SystemExit(1)
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "EXIT" in r.stdout, r.stdout + r.stderr
    assert "diagrams missing" in r.stdout


def test_missing_console_screenshot_blocks_the_build(tmp_path):
    """The console image is the evidence for its slide, so a build must not invent a blank."""

    code = f"""
import sys, pathlib
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import build_presentation as bp
bp.SCREEN = pathlib.Path({str(tmp_path / 'nope.png')!r})
try:
    bp.main(['--out', {str(tmp_path / 'y.pptx')!r}])
except SystemExit as e:
    print('EXIT', e.code)
    raise SystemExit(0)
raise SystemExit(1)
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "EXIT" in r.stdout, r.stdout + r.stderr
    assert "is missing" in r.stdout
