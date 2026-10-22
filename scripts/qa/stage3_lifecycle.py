"""Stage 3: the identity lifecycle, from the UI only.

Confirm -> merge -> reverse is the one workflow where a bug destroys work rather than
merely displaying it wrongly. A merge that cannot be reached from the console is a feature
that does not exist for the person using the console, so this drives the buttons and checks
the database and the DOM after each step.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import first_page  # noqa: E402
from stage2_interact import real_click, reset_case  # noqa: E402

BASE = "http://127.0.0.1:8420"
CASE = "CASE-DEMO"
results: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def graph() -> dict:
    with urllib.request.urlopen(f"{BASE}/api/case/{CASE}/graph") as fh:
        return json.load(fh)


async def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    shots = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("out/screen/qa")

    reset_case(CASE)
    print("case reset")

    async with await first_page(port, f"{BASE}/case/{CASE}") as c:
        await c.drain(0.8)

        g0 = graph()
        n_ent0 = g0["counts"]["entities"]
        cand = next(e for e in g0["edges"] if e["status"] == "candidate")
        cid = cand["edge_id"]
        sel = f'.edgelist li.edge[data-edge-id="{cid}"]'

        # -- merge must not be offered before a human agrees ------------- #
        offered = await c.js(f"(() => {{const li=document.querySelector({sel!r});"
                             "return li ? !!li.querySelector('.mergeform') : null})()")
        check("merge is not offered on an unconfirmed link", offered is False,
              f"mergeform present={offered}")

        # -- confirm it -------------------------------------------------- #
        await real_click(c, f"{sel} button.ok", 0)
        g1 = graph()
        conf = next(e for e in g1["edges"] if e["edge_id"] == cid)
        check("link confirmed", conf["status"] == "confirmed", conf["status"])

        offered = await c.js(f"(() => {{const li=document.querySelector({sel!r});"
                             "return li ? !!li.querySelector('.mergeform') : null})()")
        check("confirming the link reveals the merge control", offered is True,
              f"mergeform present={offered}")

        # -- fill and submit the merge ----------------------------------- #
        await c.js(
            f"""(() => {{const f=document.querySelector({sel!r} + ' .mergeform');
                 f.querySelector('[name=actor]').value='g.kanojia';
                 f.querySelector('[name=rationale]').value=
                   'QA: shared unique favicon and identical build string on both hosts';
                 return true}})()"""
        )
        filled = await c.js(
            f"""(() => {{const f=document.querySelector({sel!r} + ' .mergeform');
                 return f.checkValidity()}})()"""
        )
        check("merge form passes its own validation when filled", filled is True, str(filled))

        await real_click(c, f"{sel} .mergeform button.merge", 0)
        g2 = graph()
        check("merging reduces the live entity count",
              g2["counts"]["entities"] == n_ent0 - 1,
              f"{n_ent0} -> {g2['counts']['entities']}")
        check("the merge is recorded in the log",
              len(g2.get("merges", [])) == 1,
              f"{len(g2.get('merges', []))} merge(s), merged count {g2['counts']['merged']}")

        # Exactly one of the pair points at the other. `next()` over the pair finds the
        # survivor half the time, which is how this check passed as a failure.
        pair = [e for e in g2["entities"]
                if e["entity_id"] in (cand["a_entity"], cand["b_entity"])]
        absorbed = [e for e in pair if e.get("merged_into")]
        check("exactly one of the pair is marked as merged into the other",
              len(absorbed) == 1
              and absorbed[0]["merged_into"] in (cand["a_entity"], cand["b_entity"])
              and absorbed[0]["merged_into"] != absorbed[0]["entity_id"],
              f"{absorbed[0]['entity_id']} -> {absorbed[0]['merged_into']}" if absorbed else "none")

        # The graph should redraw with one fewer node.
        nodes = await c.js("document.querySelectorAll('#graph circle').length")
        check("the graph redraws with the merged node gone",
              nodes == g2["counts"]["entities"],
              f"{nodes} nodes drawn, {g2['counts']['entities']} live of "
              f"{len(g2['entities'])} recorded")
        await c.screenshot(str(shots / "05-after-merge.png"))

        # -- reverse it -------------------------------------------------- #
        rev_btn = await c.js(
            "(() => {const b=document.querySelector('.panel form[action$=\"/reverse\"] button.no');"
            "return b ? true : false})()"
        )
        check("a reverse button appears in the merge log", rev_btn is True, str(rev_btn))

        clicked = await real_click(c, '.panel form[action$="/reverse"] button.no', 0)
        g3 = graph()
        check("reversing restores the entity count",
              g3["counts"]["entities"] == n_ent0,
              f"{g2['counts']['entities']} -> {g3['counts']['entities']}")
        check("the reversal is recorded rather than the merge being deleted",
              len(g3.get("merges", [])) == 1 and g3["merges"][0].get("reversed_at"),
              f"reversed_at={g3['merges'][0].get('reversed_at') if g3.get('merges') else None}")
        restored = await c.js("document.querySelectorAll('#graph circle').length")
        check("the graph redraws with the node restored", restored == n_ent0,
              f"{restored} nodes")
        await c.screenshot(str(shots / "06-after-reverse.png"))

        # -- what the evidence pack says about the merge ----------------- #
        pdf = urllib.request.urlopen(f"{BASE}/case/{CASE}/evidence.pdf").read()
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(pdf)
            path = fh.name
        text = subprocess.run(["pdftotext", "-layout", path, "-"],
                              capture_output=True, text=True).stdout
        Path(path).unlink(missing_ok=True)
        check("the evidence pack carries the merge log",
              "merge" in text.lower() and "g.kanojia" in text,
              f"{len(pdf):,} bytes")
        check("the evidence pack states it is not a chain of custody",
              "not a chain of custody" in text.lower(), "")

        probs = c.problems()
        check("no console errors through the lifecycle", not probs, "; ".join(probs[:2]))

    bad = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
