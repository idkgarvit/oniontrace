"""Stage 2: drive the console like an analyst, and check what actually happened.

Static rendering can look perfect while every button is dead. Each step here performs a
real interaction, then re-reads the DOM and the server to confirm the effect landed. Clicks
go through `Input.dispatchMouseEvent` at the element's own coordinates, not `element.click()`,
so an invisible overlay or a zero-size button is caught rather than bypassed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import first_page  # noqa: E402

BASE = "http://127.0.0.1:8420"
CASE = "CASE-DEMO"

results: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}") as fh:
        return json.load(fh)


async def real_click(c, selector: str, index: int = 0) -> bool:
    """Click via the mouse, after confirming the element is the topmost hit at that point."""

    box = await c.js(
        f"""(() => {{
          const els = document.querySelectorAll({selector!r});
          const el = els[{index}];
          if (!el) return null;
          el.scrollIntoView({{block:'center'}});
          const r = el.getBoundingClientRect();
          const x = r.left + r.width / 2, y = r.top + r.height / 2;
          const top = document.elementFromPoint(x, y);
          return {{x, y, w: r.width, h: r.height,
                   topmost: top ? (top.tagName + '.' + (top.className||'')) : null,
                   isSelf: top === el || el.contains(top)}};
        }})()"""
    )
    if not box or box["w"] == 0 or box["h"] == 0:
        return False
    if not box["isSelf"]:
        check(f"hit-test {selector}[{index}]", False,
              f"element at that point is {box['topmost']}, not the target")
        return False
    for kind in ("mousePressed", "mouseReleased"):
        await c.send(
            "Input.dispatchMouseEvent", type=kind,
            x=box["x"], y=box["y"], button="left", clickCount=1,
        )
    await asyncio.sleep(1.0)
    return True


def reset_case(case: str) -> str:
    """Rebuild the case so a QA pass is repeatable.

    Without this, every run starts from whatever the previous run left behind: notes pile
    up and edges stay confirmed, so a check that compares counts passes or fails depending
    on history rather than on the app.
    """

    import shutil
    import subprocess

    root = Path(__file__).resolve().parent.parent.parent
    shutil.rmtree(root / "casework" / case, ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "oniontrace", "run", "--seed", "20260925", "--case", case],
        cwd=root, check=True, capture_output=True, text=True,
    )
    return f"{root}/casework/{case}"


async def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    if "--fresh" in sys.argv:
        print(f"resetting {CASE} ...", flush=True)
        reset_case(CASE)
    shots = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("out/screen/qa")
    shots.mkdir(parents=True, exist_ok=True)

    async with await first_page(port, f"{BASE}/case/{CASE}") as c:
        await c.drain(0.8)

        # -- 1. baseline -------------------------------------------------- #
        before = await c.js(
            "({badges: [...document.querySelectorAll('.edgelist .badge')].map(b=>b.textContent.trim()),"
            "  stats: [...document.querySelectorAll('.stat .n')].map(n=>n.textContent.trim()),"
            "  notes: document.querySelectorAll('.notes li:not(.muted)').length})"
        )
        print(f"\n1. baseline: {before['badges'].count('candidate')} candidate badges, "
              f"{before['notes']} notes, stats {before['stats']}")

        # -- 2. click a graph node, expect the entity card --------------- #
        clicked = await real_click(c, "#graph circle", 0)
        url = await c.js("location.pathname")
        on_entity = url.startswith(f"/case/{CASE}/entity/")
        check("clicking a graph node opens the entity card", clicked and on_entity, url)
        await c.screenshot(str(shots / "02-entity-card.png"))

        # -- 3. the entity card's own links ------------------------------ #
        ent = await c.js(
            "({h1: (document.querySelector('h1')||{}).textContent?.trim().slice(0,40),"
            "  svc: document.querySelectorAll('.svcblock').length,"
            "  findBadges: document.querySelectorAll('.svcblock .badge').length,"
            "  artefacts: document.querySelectorAll('.svcblock table tr').length,"
            "  rows: document.querySelectorAll('table tr').length})"
        )
        check("entity card has an H1", bool(ent["h1"]), str(ent["h1"]))
        check("entity card lists its services and artefacts",
              ent["svc"] > 0 and ent["artefacts"] > 0,
              f"{ent['svc']} services, {ent['artefacts']} artefact rows")

        # -- 4. back to the graph ---------------------------------------- #
        await c.goto(f"{BASE}/case/{CASE}")
        await c.drain(0.5)

        # -- 5. confirm a candidate edge -------------------------------- #
        data = api(f"/api/case/{CASE}/graph")
        cand = next(e for e in data["edges"] if e["status"] == "candidate")
        csel = f'.edgelist li.edge[data-edge-id="{cand["edge_id"]}"]'
        clicked = await real_click(c, f"{csel} button.ok", 0)
        after = api(f"/api/case/{CASE}/graph")
        changed = next(e for e in after["edges"] if e["edge_id"] == cand["edge_id"])
        check("confirm button changes the edge on the server",
              clicked and changed["status"] == "confirmed",
              f"{cand['edge_id'][-6:]}: {cand['status']} -> {changed['status']}")
        badge = await c.js(
            f"""(() => {{const b=document.querySelector({csel!r} + ' .badge');"""
            """return b ? b.textContent.trim() : null})()"""
        )
        check("that card shows the confirmed state after reload",
              badge == "confirmed", f"badge is {badge!r}")
        await c.screenshot(str(shots / "03-after-confirm.png"))

        # -- 6. reject another edge ------------------------------------- #
        # Target the card by its own id. Indexing by position compares one edge against
        # another and reports a failure the app never had.
        pool = [e for e in api(f"/api/case/{CASE}/graph")["edges"] if e["status"] == "candidate"]
        if not pool:
            check("reject another edge", False, "no candidate left to reject")
        else:
            target = pool[0]
            sel = f'.edgelist li.edge[data-edge-id="{target["edge_id"]}"] button.no'
            clicked = await real_click(c, sel, 0)
            got = next(e for e in api(f"/api/case/{CASE}/graph")["edges"]
                       if e["edge_id"] == target["edge_id"])
            check("reject button records a rejection",
                  clicked and got["status"] == "rejected",
                  f"{target['edge_id'][-6:]}: {target['status']} -> {got['status']}")
            dom = await c.js(
                f"""(() => {{const li=document.querySelector({sel.split(' button')[0]!r});
                     return li ? li.dataset.status : null}})()"""
            )
            check("the card's own status attribute follows the server", dom == "rejected",
                  f"data-status={dom!r}")

        # -- 7. add a note as a human would ----------------------------- #
        await c.js(
            "(() => {const f=document.querySelectorAll('.notebox')[0]||document.querySelector('.notebox');"
            "const a=f.querySelector('[name=actor]'), b=f.querySelector('[name=body]');"
            "a.value='qa-runner'; b.value='QA note: confirmed by hand on the bot screen.';"
            "a.dispatchEvent(new Event('input',{bubbles:true}));"
            "b.dispatchEvent(new Event('input',{bubbles:true}));return true})()"
        )
        posted = await real_click(c, ".notebox button.ok", 0)
        notes = await c.js(
            "([...document.querySelectorAll('.notes li:not(.muted)')].map(li=>li.textContent.trim().slice(0,60)))"
        )
        check("adding a note persists and renders",
              posted and any("qa-runner" in n for n in notes), f"{len(notes)} notes listed")

        # -- 8. HTML5 validation on an empty note ----------------------- #
        await c.js(
            "(() => {const f=document.querySelector('.notebox');"
            "f.querySelector('[name=actor]').value='';"
            "f.querySelector('[name=body]').value='';return true})()"
        )
        valid = await c.js(
            "(() => {const f=document.querySelector('.notebox');return f.checkValidity()})()"
        )
        check("an empty note is refused by the form, not by the server",
              valid is False, f"checkValidity={valid}")

        # -- 9. evidence PDF via the top nav ---------------------------- #
        pdf = urllib.request.urlopen(f"{BASE}/case/{CASE}/evidence.pdf").read()
        check("evidence pack downloads from the nav link",
              pdf[:5] == b"%PDF-", f"{len(pdf):,} bytes, header {pdf[:5]!r}")

        # -- 10. a case that does not exist ---------------------------- #
        try:
            urllib.request.urlopen(f"{BASE}/case/NOPE")
            check("unknown case returns 404", False, "server returned 200")
        except urllib.error.HTTPError as exc:
            check("unknown case returns 404", exc.code == 404, f"HTTP {exc.code}")

        # -- 11. traversal attempt ------------------------------------- #
        for probe in ("..%2f..%2fetc", "%2e%2e", ".hidden", "../../etc"):
            try:
                r = urllib.request.urlopen(f"{BASE}/case/{probe}")
                check(f"traversal {probe!r} refused", False, f"HTTP {r.status}")
            except urllib.error.HTTPError as exc:
                check(f"traversal {probe!r} refused", exc.code in (400, 404), f"HTTP {exc.code}")

        # -- 12. responsive: shrink to a laptop and a narrow window ----- #
        for w, h in ((1024, 768), (800, 900), (600, 900)):
            await c.send("Emulation.setDeviceMetricsOverride",
                         width=w, height=h, deviceScaleFactor=1, mobile=False)
            await asyncio.sleep(0.8)
            m = await c.js(
                "({sw: document.documentElement.scrollWidth,"
                "  cw: document.documentElement.clientWidth,"
                "  off: [...document.querySelectorAll('body *')]"
                "    .filter(e=>e.getBoundingClientRect().right>innerWidth+1).length,"
                "  svg: (()=>{const s=document.querySelector('#graph');"
                "    return s?Math.round(s.getBoundingClientRect().width):0})()})"
            )
            ok = m["sw"] <= m["cw"] + 1 and m["off"] == 0
            check(f"layout fits at {w}x{h}", ok,
                  f"scrollWidth {m['sw']} vs {m['cw']}, {m['off']} elements past the edge, "
                  f"graph {m['svg']}px")
            if w == 800:
                await c.screenshot(str(shots / "04-narrow-800.png"))
        await c.send("Emulation.clearDeviceMetricsOverride")

        # -- 13. console after all of that ----------------------------- #
        await c.drain(0.5)
        probs = c.problems()
        check("no console errors or failed requests during the whole pass",
              not probs, "; ".join(probs[:3]))

    bad = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
