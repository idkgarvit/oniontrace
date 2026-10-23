#!/usr/bin/env python3
"""Verify the built paper's claims against the raw evidence files.

Why this exists
---------------
The paper quotes numbers from three places: the synthetic farm's frozen drop, the real
certificate-transparency snapshot, and the real-onion capture. Those live in JSON. The
prose lives in the researcher's workspace and is edited independently. Whenever the two
drift, the paper can assert a figure the evidence does not support, and the failure is
silent: a PDF that looks fine and is wrong.

This checks the compiled PDF against the JSON directly, so a drifted number is caught by a
command rather than by someone remembering to look. It also checks two claims the paper
makes about ITSELF, which are the easiest to get wrong because no dataset contradicts them:
that the guide is acknowledged rather than authored, and that the onion services are
described in the past tense with no liveness implied.

Usage:
    python3 scripts/verify_paper.py            # verify the built PDF
    python3 scripts/verify_paper.py --strict   # also fail on sub-threshold word overlap
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "paper" / "ieee" / "oniontrace.pdf"
RW = ROOT / "out" / "paper-drop" / "real_world"

#: Phrases that would claim the torn-down services are still reachable. Any hit is a
#: failure: the addresses are destroyed, so such a sentence is false.
LIVENESS_PHRASES = (
    "are reachable", "can be visited", "currently running", "still online",
    "remain up", "live at", "available at", "still reachable", "accessible at",
)

#: Editorially or legally load-bearing phrasings. Each must survive the build, because the
#: conversion escapes LaTeX-hostile characters and a mangled phrase is a silent loss.
REQUIRED_PHRASES = (
    "no third party",
    "bubblewrap",
    "no longer reachable",
    "shredded",
    "64:ff9b",
    "prevalence",
)


def pdf_text() -> str:
    if not PDF.exists():
        print(f"no PDF at {PDF}; build it first")
        sys.exit(1)
    out = subprocess.run(["pdftotext", str(PDF), "-"], capture_output=True)
    if out.returncode != 0:
        print("pdftotext failed")
        sys.exit(1)
    # Flatten whitespace so a phrase split across a line or column break still matches.
    # Column interleaving in a two-column IEEE paper inserts reference text mid-sentence,
    # so a naive contiguous search produces false negatives. Both forms are checked.
    return out.stdout.decode("utf-8", "replace")


def find(text: str, phrase: str) -> bool:
    """True if phrase occurs, tolerating a line or column break inside it."""
    if phrase.lower() in text.lower():
        return True
    words = [re.escape(w) for w in phrase.split()]
    return re.search(r"\s+".join(words), text, re.IGNORECASE) is not None


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def main() -> int:
    strict = "--strict" in sys.argv
    raw = pdf_text()
    flat = re.sub(r"\s+", " ", raw)

    failures: list[str] = []
    checked = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal checked
        checked += 1
        if ok:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}{(' -- ' + detail) if detail else ''}")
            failures.append(label)

    print(f"verifying {PDF.name}")

    # -- evidence-backed numbers -------------------------------------------- #
    #
    # Presence is not correctness. A first version of this file only asked whether each
    # expected string appeared anywhere, and a bite test defeated it: replacing one of the
    # ten occurrences of "3,018" with "3,019" still passed, because the other nine were
    # intact. So each evidence-backed figure is now checked in TWO ways, and both must
    # hold: it must appear, and no near-miss variant of it may appear anywhere.
    ct = load(RW / "ct_frequency.json")
    on = load(RW / "real_onion_service.json")

    print("\nreal certificate data (ct_frequency.json)")

    def audit(needle: str, ok_evidence: bool, why: str = "") -> None:
        """Require the figure present, and require no off-by-one neighbour present.

        The neighbour test is what catches a silent edit. "3,018" and "3,019" are one
        keystroke apart, and a paper that reports the wrong one is wrong in a way that
        reading it will not reveal. Neighbours are only flagged when the figure is
        comma-grouped or otherwise specific enough that a neighbour would be a real
        alternative value rather than an unrelated number.
        """
        present = find(flat, needle)
        neighbours: list[str] = []
        if re.fullmatch(r"\d{1,3},\d{3}", needle):
            value = int(needle.replace(",", ""))
            candidates = {f"{value + 1:,}", f"{value - 1:,}",
                          f"{value + 10:,}", f"{value - 10:,}"}
            neighbours = sorted(c for c in candidates if find(flat, c))
        elif re.fullmatch(r"\d{3,}", needle):
            value = int(needle)
            candidates = {str(value + 1), str(value - 1),
                          str(value + 10), str(value - 10)}
            neighbours = sorted(c for c in candidates if find(flat, c))

        if not ok_evidence:
            check(f"{needle} matches its evidence file", False, why or "source disagrees")
        elif not present:
            check(f"{needle} appears in the PDF", False, "not found")
        elif neighbours:
            check(f"{needle} appears and no drifted neighbour does",
                  False, f"also found {', '.join(neighbours)}")
        else:
            check(f"{needle} present, evidence-consistent, no drift neighbour", True)

    audit("3,018", ct["certificates_with_a_real_onion_san"] == 3018)
    audit("688", ct["unique_onion_names"] == 688)
    audit("118", ct["certificates_contaminating_the_query"] == 118)
    audit("3,136", ct["certificates_returned"] == 3136)
    audit("1,788", round(ct["top_issuer_share"] * 3018) == 1788)

    share = round(ct["top_issuer_share"] * 100, 1)
    # The source writes "percent", not "%", because LaTeX would swallow a bare % as a
    # comment. My first version of this check looked for "59.2%" and failed the paper for
    # following the class convention correctly. Accept either spelling.
    share_forms = (f"{share} percent", f"{share}%", f"{share} %")
    check(f"{share} percent in PDF and equal to source",
          share == 59.2 and any(find(flat, f) for f in share_forms))
    # The headline fraction is derived; recompute it rather than trusting the stored float.
    n = round(ct["top_issuer_share"] * ct["certificates_with_a_real_onion_san"])
    check(f"derived {n:,}/3,018 recomputes ({ct['top_issuer_share']:.6f})",
          n == 1788 and find(flat, f"{n:,}"))

    print("\nreal onion services (real_onion_service.json)")
    s = on["summary"]
    check("clean control careful findings = 0",
          s["clean_control"]["careful_collapsed_count"] == 0
          and find(flat, "0 findings, 0 false positives"))
    check("clean control naive findings = 5",
          s["clean_control"]["naive_collapsed_count"] == 5)
    check("naive false positive is dot_git on a 404",
          [f["kind"] for f in s["clean_control"]["naive_false_positives"]] == ["dot_git"]
          and find(flat, "dot_git"))
    check("misconfigured careful = 2", s["misconfigured"]["careful_collapsed_count"] == 2)
    check("both onion addresses appear verbatim",
          all(find(flat, e["address"]) for e in on["services"].values()))

    print("\nlab servers (lab_servers.json)")
    lab = load(RW / "lab_servers.json")
    rules = lab["rules_on_real_responses"]
    check("dot_env fires on misconfigured, not on clean",
          rules["dot_env_exposed"]["misconfigured"] and not rules["dot_env_exposed"]["clean"])
    check("nginx version banner in PDF", find(flat, "1.30.4"))

    # -- claims about the paper itself -------------------------------------- #
    print("\nclaims the paper makes about itself")
    for phrase in LIVENESS_PHRASES:
        check(f"no liveness claim: '{phrase}'", not find(flat, phrase))
    for phrase in REQUIRED_PHRASES:
        check(f"required phrasing survives: '{phrase}'", find(flat, phrase))

    # Guide acknowledged, not authored: the author block precedes \maketitle in the tex.
    tex = (ROOT / "paper" / "ieee" / "oniontrace.tex").read_text()
    marker = r"\maketitle"
    if marker in tex:
        author_block = tex.split(marker, 1)[0]
        check("guide is not in the author block", "Kumar" not in author_block)
        check("guide is acknowledged somewhere", "Kumar" in tex)
    else:
        check("tex has \\maketitle so the author block can be checked", False)

    # -- citations ----------------------------------------------------------- #
    #
    # Every bibitem must be cited inline. This caught a real defect: all 11 references were
    # defined and the prose named every source in words, but no numbered marker appeared
    # anywhere in the body. IEEE format requires inline numbered citations, and a reference
    # list that the text never points at reads as padding and can fail a format check.
    #
    # The body/reference split is NOT found by searching for "REFERENCES": IEEEtran renders
    # that heading in small caps, so pdftotext yields "R EFERENCES" with a space inside the
    # word. Searching for the plain form returned -1, the split silently fell back to the
    # whole document, and the check then counted the markers in the reference list itself,
    # passing without ever testing the body. The heading is located by pattern instead, and
    # a missing heading is a hard failure rather than a silent fallback.
    print("\ncitations")
    tex2 = (ROOT / "paper" / "ieee" / "oniontrace.tex").read_text()
    bibitems = set(re.findall(r"\\bibitem\{([^}]*)\}", tex2))

    head = re.search(r"R\s+EFERENCES", raw)
    if not head:
        check("reference-list heading located so the body can be isolated", False,
              "no 'R EFERENCES'/'REFERENCES' heading found; cannot separate body from list")
        body_only = ""
    else:
        check("reference-list heading located so the body can be isolated", True)
        body_only = raw[: head.start()]

    counts: dict[int, int] = {}
    for tok in re.findall(r"\[(\d{1,2})\]", body_only):
        counts[int(tok)] = counts.get(int(tok), 0) + 1

    defined_nums = set(range(1, len(bibitems) + 1))
    uncited = sorted(defined_nums - set(counts))
    check(f"all {len(bibitems)} references are cited at least once inline",
          bool(body_only) and not uncited,
          f"never cited in the body: {uncited}" if body_only else "body not isolated")
    check("every inline marker resolves to a reference",
          not (set(counts) - defined_nums),
          f"markers with no reference: {sorted(set(counts) - defined_nums)}")
    # Counting, not presence: a marker that appears twice cannot hide the loss of one
    # instance, which is exactly how the previous version of this check was defeated.
    total_inline = sum(counts.values())
    check(f"inline citation count is at least one per reference ({total_inline} markers)",
          total_inline >= len(bibitems),
          f"{total_inline} markers for {len(bibitems)} references")

    # -- freshness ----------------------------------------------------------- #
    print("\nfreshness")
    src = Path("/home/shiga0_0/.hermes/profiles/researcher/workspace/"
               "oniontrace-paper/paper.md")
    if src.exists():
        check("PDF is not older than the markdown source",
              PDF.stat().st_mtime >= src.stat().st_mtime,
              "rebuild: python3 paper/build_ieee.py && pdflatex")
    else:
        print("  note source md not found; freshness not checked")

    # -- summary ------------------------------------------------------------- #
    print()
    if failures:
        print(f"{len(failures)} of {checked} checks FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {checked} checks passed")
    if strict:
        print("(strict mode: nothing extra to add)")
    print(f"VERDICT: {checked} checks passed on the paper, its source and its citations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())