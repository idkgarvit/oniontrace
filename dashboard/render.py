"""Evidence pack renderer (M6-lite).

Produces a single PDF that carries the case, every entity, every proposed link with
its evidence, the merge log and the artefact digest ledger, so an analyst can hand
someone a file rather than a screenshot of a dashboard.

The pack states its own limits on its first page. That is deliberate and it is the
same discipline the paper uses: a document that looks authoritative is exactly the
one whose boundaries need printing, because the person reading it was not there when
it was produced.

What a digest in this pack does and does not establish:

* it establishes that the bytes match the digest recorded at collection time;
* it does **not** establish who wrote them, that the source was honest, or that the
  file is admissible. An adversary who can rewrite both the evidence and the ledger
  can recompute consistent hashes. The pack therefore says "provenance under stated
  assumptions" and never "chain of custody".
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: A4; margin: 18mm 16mm; @bottom-center {{
      content: "ONIONTRACE evidence pack · {case_id} · page " counter(page) " of " counter(pages);
      font-size: 7.5pt; color: #666; }} }}
  body {{ font-family: "DejaVu Sans", sans-serif; font-size: 8.6pt; color: #111; line-height: 1.42; }}
  h1 {{ font-size: 15pt; margin: 0 0 2mm; }}
  h2 {{ font-size: 10.5pt; margin: 7mm 0 2mm; border-bottom: 1px solid #bbb; padding-bottom: 1mm; }}
  .sub {{ color: #555; font-size: 8pt; margin-bottom: 5mm; }}
  table {{ width: 100%; border-collapse: collapse; margin: 2mm 0 4mm; }}
  th, td {{ border: 1px solid #ccc; padding: 1.6mm 2mm; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; font-size: 7.6pt; text-transform: uppercase; letter-spacing: .3px; }}
  .score {{ font-family: "DejaVu Sans Mono", monospace; white-space: nowrap; }}
  .badge {{ display: inline-block; padding: 0.4mm 1.6mm; border-radius: 2px; font-size: 7pt;
            text-transform: uppercase; letter-spacing: .3px; }}
  .b-candidate {{ background: #d9ecd4; color: #1c5c17; }}
  .b-possible  {{ background: #fdf0cf; color: #6b4c00; }}
  .b-conflict  {{ background: #fadbd8; color: #7d1c12; }}
  .b-confirmed {{ background: #d6e4f7; color: #123a6b; }}
  .b-rejected  {{ background: #e4e4e4; color: #444; }}
  .limits {{ background: #fbf7e8; border-left: 3px solid #c8a13a; padding: 2.5mm 3mm; font-size: 7.8pt; }}
  .limits b {{ display: block; margin-bottom: 1mm; }}
  .mono {{ font-family: "DejaVu Sans Mono", monospace; font-size: 6.9pt; word-break: break-all; }}
  ul {{ margin: 1mm 0 2mm 4mm; padding: 0; }}
  li {{ margin: 0.7mm 0; }}
  .muted {{ color: #666; }}
</style></head><body>

<h1>ONIONTRACE — case evidence pack</h1>
<div class="sub">
  Case <b>{case_id}</b> · generated {generated} · seed-independent export of the case graph
</div>

<div class="limits">
  <b>What this document does and does not establish</b>
  Every link in this pack is a <i>candidate</i>. The graph proposes relationships and a named
  analyst decides; nothing here asserts that two services share an operator. Scores are
  aggregate weights over observed artefacts and are shown so they can be disputed.
  A SHA-256 digest establishes that captured bytes match the digest recorded at collection
  time. It does not establish authorship, source honesty, or admissibility, and it is not a
  chain of custody: an adversary who can rewrite both the evidence and the ledger can
  recompute consistent hashes. Collection was passive and limited to what an anonymous
  visitor receives; no credentials were used and nothing was exploited.
</div>

<h2>Summary</h2>
<table>
  <tr><th>Entities</th><th>Merged</th><th>Proposed links</th><th>Conflicts</th><th>Notes</th></tr>
  <tr>
    <td>{n_entities}</td><td>{n_merged}</td><td>{n_edges}</td>
    <td>{n_conflicts}</td><td>{n_notes}</td>
  </tr>
</table>

<h2>Entities</h2>
<table>
  <tr><th style="width:26%">Entity</th><th style="width:12%">Status</th>
      <th style="width:30%">Services</th><th>Artefacts observed</th></tr>
  {entity_rows}
</table>

<h2>Proposed links</h2>
<table>
  <tr><th style="width:20%">Pair</th><th style="width:10%">Method</th>
      <th style="width:8%">Score</th><th style="width:10%">Status</th><th>Evidence</th></tr>
  {edge_rows}
</table>

<h2>Vendor-default cohorts</h2>
<p class="muted" style="margin-top:0">
  Values shared because software ships them or because they were published elsewhere.
  These are excluded from every link above, and are listed so their absence can be explained.
</p>
<table>
  <tr><th style="width:24%">Kind</th><th style="width:16%">Value</th>
      <th style="width:10%">Services</th><th>Why it does not identify</th></tr>
  {cohort_rows}
</table>

<h2>Merge log</h2>
<table>
  <tr><th style="width:16%">Merge</th><th style="width:24%">Survivor / absorbed</th>
      <th style="width:18%">Actor</th><th>Rationale</th></tr>
  {merge_rows}
</table>

<h2>Notes</h2>
{notes_block}

<h2>Artefact digest ledger</h2>
<table>
  <tr><th style="width:22%">Artefact</th><th style="width:22%">Service / path</th><th>SHA-256 of observation</th></tr>
  {ledger_rows}
</table>

</body></html>"""


def _badge(status: str) -> str:
    return f'<span class="badge b-{html.escape(status)}">{html.escape(status)}</span>'


def _evidence_cell(edge: dict[str, Any]) -> str:
    parts = []
    if edge["supporting"]:
        parts.append("<b>support</b><ul>")
        for ev in edge["supporting"]:
            parts.append(
                f"<li>{html.escape(ev['kind'])} = <span class=\"mono\">"
                f"{html.escape(str(ev['value'])[:64])}</span> "
                f"<span class=\"muted\">(w {ev['weight']:.2f}; {html.escape(ev['note'])})</span></li>"
            )
        parts.append("</ul>")
    if edge["counter"]:
        parts.append("<b>doubt</b><ul>")
        for ev in edge["counter"]:
            parts.append(
                f"<li>{html.escape(ev['kind'])}: "
                f"<span class=\"mono\">{html.escape(str(ev['value'])[:64])}</span> "
                f"<span class=\"muted\">({html.escape(ev['note'])})</span></li>"
            )
        parts.append("</ul>")
    return "".join(parts) or '<span class="muted">no evidence recorded</span>'


def render_evidence_pack(
    *,
    case_id: str,
    graph: dict[str, Any],
    cohorts: list[dict[str, Any]],
    merges: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> bytes:
    """Build the PDF bytes for one case."""

    from weasyprint import HTML

    entities = [e for e in graph["entities"] if e["merged_into"] is None]
    merged = [e for e in graph["entities"] if e["merged_into"]]

    entity_rows = "".join(
        "<tr>"
        f"<td class=\"mono\">{html.escape(e['entity_id'])}</td>"
        f"<td>{_badge(e['status'])}</td>"
        f"<td class=\"mono\">{html.escape(', '.join(e['services']) or '—')}</td>"
        f"<td>{html.escape(', '.join(sorted({a['kind'] for a in e['artefacts']})) or 'none')}</td>"
        "</tr>"
        for e in entities
    )
    for m in merged:
        entity_rows += (
            "<tr>"
            f"<td class=\"mono\">{html.escape(m['entity_id'])}</td>"
            f"<td>{_badge('merged')}</td>"
            f"<td colspan=\"2\" class=\"muted\">merged into "
            f"{html.escape(m['merged_into'] or '')}</td>"
            "</tr>"
        )

    edge_rows = "".join(
        "<tr>"
        f"<td class=\"mono\">{html.escape(e['a_entity'])}<br>{html.escape(e['b_entity'])}</td>"
        f"<td>{html.escape(e['method'])}</td>"
        f"<td class=\"score\">{e['score']:.3f}</td>"
        f"<td>{_badge('conflict' if e['has_conflict'] else e['status'])}</td>"
        f"<td>{_evidence_cell(e)}</td>"
        "</tr>"
        for e in sorted(graph["edges"], key=lambda x: -x["score"])
    )

    cohort_rows = "".join(
        "<tr>"
        f"<td class=\"mono\">{html.escape(c['kind'])}</td>"
        f"<td class=\"mono\">{html.escape(c['value'])}</td>"
        f"<td class=\"score\">{c['service_count']}</td>"
        f"<td>{html.escape(c['why'])}</td>"
        "</tr>"
        for c in cohorts
    ) or '<tr><td colspan="4" class="muted">none observed</td></tr>'

    merge_rows = "".join(
        "<tr>"
        f"<td class=\"mono\">{html.escape(m['merge_id'])}</td>"
        f"<td class=\"mono\">{html.escape(m['survivor'])} / {html.escape(m['absorbed'])}</td>"
        f"<td>{html.escape(m['actor'])}</td>"
        f"<td>{html.escape(m['rationale'])}"
        + (
            f' <span class="muted">— reversed {html.escape(m["reversed_at"])}</span>'
            if m.get("reversed_at")
            else ""
        )
        + "</td></tr>"
        for m in merges
    ) or '<tr><td colspan="4" class="muted">no merges recorded in this case</td></tr>'

    if notes:
        notes_block = "<table><tr><th style=\"width:16%\">Actor</th><th style=\"width:18%\">When</th><th>Note</th></tr>" + "".join(
            "<tr>"
            f"<td>{html.escape(n['actor'])}</td>"
            f"<td class=\"mono\">{html.escape(n['created_at'])}</td>"
            f"<td>{html.escape(n['body'])}</td>"
            "</tr>"
            for n in notes
        ) + "</table>"
    else:
        notes_block = '<p class="muted">No analyst notes recorded.</p>'

    ledger_rows = "".join(
        "<tr>"
        f"<td class=\"mono\">{html.escape(a['kind'])}</td>"
        f"<td class=\"mono\">{html.escape(a['service_id'])}<br>{html.escape(a['path'])}</td>"
        f"<td class=\"mono\">{html.escape(a['evidence_sha256'])}</td>"
        "</tr>"
        for e in entities
        for a in sorted(e["artefacts"], key=lambda x: (x["service_id"], x["kind"]))
    )

    doc = TEMPLATE.format(
        case_id=html.escape(case_id),
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        n_entities=len(entities),
        n_merged=len(merged),
        n_edges=len(graph["edges"]),
        n_conflicts=graph["counts"].get("conflicts", 0),
        n_notes=len(notes),
        entity_rows=entity_rows or '<tr><td colspan="4" class="muted">no entities</td></tr>',
        edge_rows=edge_rows or '<tr><td colspan="5" class="muted">no proposed links</td></tr>',
        cohort_rows=cohort_rows,
        merge_rows=merge_rows,
        notes_block=notes_block,
        ledger_rows=ledger_rows or '<tr><td colspan="3" class="muted">no artefacts</td></tr>',
    )
    return HTML(string=doc).write_pdf()
