"""One continuous analyst session, end to end, in a single browser context.

The staged harness resets between phases so each result is independent. This does the
opposite on purpose: it opens the console, works the case the way a person would, and never
resets, so anything that only breaks after a previous action is visible here and invisible
there. It reports what happened at each step rather than asserting a list.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import first_page  # noqa: E402
from stage2_interact import real_click, reset_case  # noqa: E402

BASE = "http://127.0.0.1:8420"
CASE = "CASE-DEMO"
OUT = Path("out/screen/walkthrough")


def api() -> dict:
    with urllib.request.urlopen(f"{BASE}/api/case/{CASE}/graph") as fh:
        return json.load(fh)


def say(step: str, detail: str = "") -> None:
    print(f"\n{step}" + (f"\n   {detail}" if detail else ""))


async def shot(c, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    size = await c.screenshot(str(path))
    print(f"   [shot] {path} ({size:,} B)")


async def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222

    say("0. reset", "rebuilding the case so this is a clean run, not a continuation")
    reset_case(CASE)
    g = api()
    print(f"   {g['counts']['entities']} entities, {g['counts']['edges']} links, "
          f"{len(g['findings'])} findings, {len(g['notes'])} notes")

    async with await first_page(port, f"{BASE}/") as c:
        await c.drain(0.8)

        # --- landing ---------------------------------------------------- #
        say("1. landing page")
        landing = await c.js(
            "({h1: document.querySelector('h1')?.textContent,"
            "  cards: [...document.querySelectorAll('.card')].map(a=>a.getAttribute('href')),"
            "  empty: !!document.querySelector('.empty')})"
        )
        print(f"   h1 {landing['h1']!r}, cases {landing['cards']}, empty-state {landing['empty']}")
        await shot(c, "01-landing")

        # --- into the case ---------------------------------------------- #
        say("2. click into the case")
        await real_click(c, ".card", 0)
        print(f"   now at {await c.js('location.pathname')}")
        top = await c.js(
            "({h1: document.querySelector('h1')?.textContent.trim(),"
            "  stats: [...document.querySelectorAll('.stat')].map(s=>"
            "    s.querySelector('.l').textContent.trim()+'='+s.querySelector('.n').textContent.trim()),"
            "  nodes: document.querySelectorAll('#graph circle').length,"
            "  links: document.querySelectorAll('.edgelist li.edge').length})"
        )
        print(f"   {top['h1']}")
        print(f"   {', '.join(top['stats'])}")
        print(f"   graph drew {top['nodes']} nodes and {top['links']} link cards")
        await shot(c, "02-case")

        # --- read a link before touching it ----------------------------- #
        say("3. read the top link before deciding anything")
        link = await c.js(
            """(() => {const li=document.querySelector('.edgelist li.edge');
                 return {pair: [...li.querySelectorAll('.edgehead a')].map(a=>a.textContent),
                         status: li.querySelector('.badge').textContent.trim(),
                         score: li.querySelector('.score').textContent,
                         support: [...li.querySelectorAll('.evidence .sup')]
                                    .map(x=>x.textContent.trim().replace(/\\s+/g,' ').slice(0,72)),
                         doubt: [...li.querySelectorAll('.evidence .con')]
                                    .map(x=>x.textContent.trim().replace(/\\s+/g,' ').slice(0,72))}})()"""
        )
        print(f"   {link['pair'][0]} <-> {link['pair'][1]}   score {link['score']}  [{link['status']}]")
        for s in link["support"]:
            print(f"     + {s}")
        for d in link["doubt"]:
            print(f"     - {d}")

        # --- open one of them ------------------------------------------- #
        say("4. open the left entity's card")
        await real_click(c, ".edgelist li.edge .edgehead a", 0)
        card = await c.js(
            "({path: location.pathname, h1: document.querySelector('h1')?.textContent.trim(),"
            "  services: [...document.querySelectorAll('.svcblock h3')].map(h=>h.textContent.trim().replace(/\\s+/g,' ')),"
            "  artefacts: document.querySelectorAll('.svcblock table tr').length - 1,"
            "  findings: [...document.querySelectorAll('.svcblock .findlist li')].map(l=>l.textContent.trim().slice(0,50))})"
        )
        print(f"   {card['h1']}  ({card['path'].split('/')[-1]})")
        print(f"   services: {card['services']}")
        print(f"   artefacts on record: {card['artefacts']}")
        for f in card["findings"]:
            print(f"     ! {f}")
        await shot(c, "03-entity")

        # --- go back and act -------------------------------------------- #
        say("5. back to the graph, confirm that link")
        await c.goto(f"{BASE}/case/{CASE}")
        await c.drain(0.5)
        first_id = await c.js(
            "document.querySelector('.edgelist li.edge').dataset.edgeId"
        )
        sel = f'.edgelist li.edge[data-edge-id="{first_id}"]'
        await real_click(c, f"{sel} button.ok", 0)
        after = next(e for e in api()["edges"] if e["edge_id"] == first_id)
        print(f"   {first_id[-6:]} is now {after['status']}")
        print(f"   merge control offered: "
              f"{await c.js(f'(() => !!document.querySelector({sel!r} + \" .mergeform\"))()')}")

        # --- merge ------------------------------------------------------- #
        say("6. merge them, with a reason")
        await c.js(
            f"""(() => {{const f=document.querySelector({sel!r} + ' .mergeform');
                 f.querySelector('[name=actor]').value='g.kanojia';
                 f.querySelector('[name=rationale]').value=
                   'walkthrough: unique favicon hash and identical build string';return 1}})()"""
        )
        await shot(c, "04-confirmed-with-merge-form")
        await real_click(c, f"{sel} .mergeform button.merge", 0)
        m = api()
        print(f"   entities {m['counts']['entities']}, merged {m['counts']['merged']}, "
              f"merges in log {len(m['merges'])}")
        if m["merges"]:
            log = m["merges"][0]
            print(f"   log: {log['survivor']} <- {log['absorbed']} by {log['actor']}")
            print(f"        reason: {log['rationale']}")
            print(f"        reversed: {log['reversed_at']}")
        print(f"   graph now draws {await c.js('document.querySelectorAll(\"#graph circle\").length')} nodes")
        await shot(c, "05-merged")

        # --- reverse ----------------------------------------------------- #
        say("7. change my mind and reverse it")
        await real_click(c, '.panel form[action$="/reverse"] button.no', 0)
        r = api()
        print(f"   entities back to {r['counts']['entities']}, merged {r['counts']['merged']}")
        print(f"   merge still in the log, now reversed at {r['merges'][0]['reversed_at']}")
        print(f"   graph draws {await c.js('document.querySelectorAll(\"#graph circle\").length')} nodes")
        await shot(c, "06-reversed")

        # --- a note ------------------------------------------------------ #
        say("8. leave a note for the next analyst")
        await c.js(
            """(() => {const f=document.querySelector('.notebox');
                 f.querySelector('[name=actor]').value='g.kanojia';
                 f.querySelector('[name=body]').value=
                   'Walked the CASE-DEMO links by hand. All twelve carry artefacts that are '
                   + 'either unique or a stated default; nothing needs a forced merge.';return 1})()"""
        )
        await real_click(c, ".notebox button.ok", 0)
        notes = await c.js(
            "[...document.querySelectorAll('.notes li')].map(l=>l.textContent.trim().replace(/\\s+/g,' ').slice(0,90))"
        )
        print(f"   {len(notes)} notes on the case:")
        for n in notes:
            print(f"     · {n}")

        # --- evidence pack ----------------------------------------------- #
        say("9. export the evidence pack")
        pdf = urllib.request.urlopen(f"{BASE}/case/{CASE}/evidence.pdf").read()
        tmp = Path("/tmp/walkthrough-evidence.pdf")
        tmp.write_bytes(pdf)
        pages = subprocess.run(["pdfinfo", str(tmp)], capture_output=True, text=True).stdout
        pages = [ln for ln in pages.splitlines() if ln.startswith("Pages")]
        text = subprocess.run(["pdftotext", "-layout", str(tmp), "-"],
                              capture_output=True, text=True).stdout
        print(f"   {len(pdf):,} bytes, {pages[0] if pages else 'pages ?'}")
        for marker in ("Detection findings", "Proposed links", "Merge log",
                       "not a chain of custody", "Walked the CASE-DEMO links"):
            print(f"   {'yes' if marker.lower() in text.lower() else 'NO ':3}  contains {marker!r}")
        await c.screenshot(str(OUT / "07-finished.png"))

        # --- anything break along the way? ------------------------------- #
        say("10. after all of that")
        probs = c.problems()
        print(f"   console problems: {len(probs)}")
        for p in probs[:5]:
            print(f"     - {p}")
        final = api()
        print(f"   final state: {final['counts']} · notes {len(final['notes'])} "
              f"· merges {len(final['merges'])}")
        await shot(c, "08-final")

    print("\nwalkthrough complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
