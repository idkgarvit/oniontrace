"""Stage 1: what the page actually renders, and whether it fits.

Nothing here trusts the template. Every check reads the live DOM of the page the browser
painted, because the failures worth finding are the ones the server cannot see: a script
that throws after paint, a panel wider than its column, a colour that cannot be read.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import first_page  # noqa: E402

AUDIT = r"""
(() => {
  const out = {viewport: {w: innerWidth, h: innerHeight}};

  const svg = document.querySelector('#graph');
  out.graph = svg ? {
    circles: svg.querySelectorAll('circle').length,
    lines: svg.querySelectorAll('line').length,
    labels: svg.querySelectorAll('text').length,
    dashed: [...svg.querySelectorAll('line')].filter(l => l.getAttribute('stroke-dasharray')).length,
    box: svg.getBoundingClientRect().toJSON(),
    nodesOutsideViewBox: [...svg.querySelectorAll('circle')].filter(c => {
      const x = +c.getAttribute('cx'), y = +c.getAttribute('cy');
      return x < 0 || y < 0 || x > 900 || y > 520;
    }).length,
  } : null;

  // Horizontal overflow: the classic responsive bug.
  out.docScrollWidth = document.documentElement.scrollWidth;
  out.docClientWidth = document.documentElement.clientWidth;
  out.overflowing = [...document.querySelectorAll('body *')]
    .filter(el => el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX !== 'visible')
    .slice(0, 12)
    .map(el => el.tagName + '.' + (el.className || '').toString().slice(0, 40)
              + ` (${el.scrollWidth} > ${el.clientWidth})`);

  // Clipped text: element whose content is taller than the box and hidden.
  out.clipped = [...document.querySelectorAll('body *')]
    .filter(el => {
      const cs = getComputedStyle(el);
      return (cs.overflow === 'hidden' || cs.overflowY === 'hidden')
             && el.scrollHeight > el.clientHeight + 4;
    })
    .slice(0, 12)
    .map(el => el.tagName + '.' + (el.className || '').toString().slice(0, 40)
              + ` (${el.scrollHeight} > ${el.clientHeight})`);

  // Elements pushed off the right edge.
  out.offRight = [...document.querySelectorAll('body *')]
    .filter(el => el.getBoundingClientRect().right > innerWidth + 1)
    .slice(0, 12)
    .map(el => el.tagName + '.' + (el.className || '').toString().slice(0, 40)
              + ' right=' + Math.round(el.getBoundingClientRect().right));

  // Accessibility surface.
  out.a11y = {
    imgNoAlt: [...document.images].filter(i => !i.hasAttribute('alt')).length,
    btnNoName: [...document.querySelectorAll('button')]
      .filter(b => !(b.textContent || '').trim() && !b.getAttribute('aria-label')).length,
    inputNoLabel: [...document.querySelectorAll('input:not([type=hidden]), textarea, select')]
      .filter(el => {
        if (el.getAttribute('aria-label') || el.getAttribute('placeholder')) return false;
        if (el.id && document.querySelector(`label[for="${el.id}"]`)) return false;
        return !el.closest('label');
      })
      .map(el => el.name || el.tagName),
    headingOrder: [...document.querySelectorAll('h1,h2,h3')].map(h => h.tagName),
    langAttr: document.documentElement.getAttribute('lang'),
  };

  // Contrast of text colours against the nearest opaque background.
  const lum = (c) => {
    const [r, g, b] = c.match(/\d+/g).slice(0, 3).map(Number)
      .map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      if (bg && !/rgba?\([^)]*,\s*0\)/.test(bg) && bg !== 'transparent') return bg;
      n = n.parentElement;
    }
    return getComputedStyle(document.body).backgroundColor;
  };
  out.contrast = [...document.querySelectorAll('p,li,td,th,h1,h2,h3,span,a,code,em,b,button,label')]
    .filter(el => (el.textContent || '').trim() && el.offsetParent !== null)
    .map(el => {
      const cs = getComputedStyle(el);
      const fg = lum(cs.color), bg = lum(bgOf(el));
      const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
      const size = parseFloat(cs.fontSize);
      const large = size >= 24 || (size >= 18.66 && parseInt(cs.fontWeight) >= 700);
      return {sel: el.tagName + '.' + (el.className || '').toString().slice(0, 30),
              ratio: +ratio.toFixed(2), size, large,
              text: (el.textContent || '').trim().slice(0, 40)};
    })
    .filter(x => x.ratio < (x.large ? 3 : 4.5))
    .slice(0, 15);

  out.counts = {
    panels: document.querySelectorAll('.panel').length, 
    edges: document.querySelectorAll('.edgelist li.edge').length,
    findingCards: document.querySelectorAll('.findcard').length,
    stats: [...document.querySelectorAll('.stat .n')].map(n => n.textContent.trim()),
    notes: document.querySelectorAll('.notes li:not(.muted)').length,
    svgEmbedded: typeof window.__GRAPH__ === 'object' && window.__GRAPH__ !== null,
  };

  return out;
})()
"""


async def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    shot_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("out/screen/qa")
    shot_dir.mkdir(parents=True, exist_ok=True)

    target = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8420/case/CASE-DEMO"
    async with await first_page(port, target) as c:
        await c.drain(1.0)
        data = await c.js(AUDIT)
        size = await c.screenshot(str(shot_dir / "01-case-view.png"))

        print(f"viewport {data['viewport']['w']}x{data['viewport']['h']}  screenshot {size:,} B")
        print(f"doc scrollWidth {data['docScrollWidth']} vs clientWidth {data['docClientWidth']}"
              f"  -> {'HORIZONTAL OVERFLOW' if data['docScrollWidth'] > data['docClientWidth'] else 'fits'}")

        g = data["graph"]
        if g is None:
            print("graph: none on this page")
        else:
            print(f"graph: {g['circles']} nodes, {g['lines']} edges, {g['labels']} labels, "
                  f"{g['dashed']} dashed, {g['nodesOutsideViewBox']} outside the viewBox")
            print(f"       svg box {round(g['box']['width'])}x{round(g['box']['height'])}")

        cts = data["counts"]
        print(f"panels {cts['panels']}  edge cards {cts['edges']}  finding cards {cts['findingCards']}"
              f"  notes {cts['notes']}  __GRAPH__ {cts['svgEmbedded']}")
        print(f"stats row: {cts['stats']}")

        for label, key in (("overflowing", "overflowing"), ("clipped", "clipped"),
                           ("off right edge", "offRight")):
            items = data[key]
            print(f"{label}: {len(items)}" + ("".join(f"\n   - {i}" for i in items) if items else ""))

        a = data["a11y"]
        print(f"a11y: img without alt {a['imgNoAlt']}, buttons without a name {a['btnNoName']}, "
              f"unlabelled inputs {a['inputNoLabel'] or 'none'}, html[lang]={a['langAttr']!r}")
        print(f"      headings {a['headingOrder']}")

        con = data["contrast"]
        print(f"contrast failures: {len(con)}")
        for x in con:
            print(f"   - {x['sel']} ratio {x['ratio']} (needs {3 if x['large'] else 4.5}) {x['text']!r}")

        probs = c.problems()
        print(f"console problems: {len(probs)}")
        for p in probs[:10]:
            print(f"   - {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
