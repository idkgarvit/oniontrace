/* Entity graph renderer — no dependencies.
 *
 * A small force-directed layout in plain SVG. The bundled Cytoscape.js path was not
 * taken because a demo that needs a CDN at presentation time is a demo that can fail
 * in the room; this draws from the JSON already embedded in the page.
 *
 * Encoding choices that matter for reading the graph honestly:
 *   colour  = link status, so a "possible" pair never looks like a confirmed one;
 *   width   = score, so a weak link is visibly weak;
 *   dashes  = below threshold, so the eye separates proposed from supported.
 */
(function () {
  "use strict";

  var data = window.__GRAPH__;
  var svg = document.getElementById("graph");
  if (!data || !svg) return;

  var W = 900, H = 520;
  var NS = "http://www.w3.org/2000/svg";

  var live = data.entities.filter(function (e) { return !e.merged_into; });
  if (!live.length) {
    svg.innerHTML = '<text x="' + W / 2 + '" y="' + H / 2 +
      '" fill="#8595a8" font-size="13" text-anchor="middle">No entities in this case</text>';
    return;
  }

  var idx = {};
  var nodes = live.map(function (e, i) {
    idx[e.entity_id] = i;
    var a = (i / live.length) * Math.PI * 2;
    return {
      id: e.entity_id,
      services: e.services || [],
      x: W / 2 + Math.cos(a) * 150 + (Math.random() - 0.5) * 20,
      y: H / 2 + Math.sin(a) * 130 + (Math.random() - 0.5) * 20,
      vx: 0, vy: 0, fixed: false
    };
  });

  var links = data.edges
    .filter(function (e) { return idx[e.a_entity] !== undefined && idx[e.b_entity] !== undefined; })
    .map(function (e) {
      return {
        s: idx[e.a_entity], t: idx[e.b_entity],
        score: e.score,
        status: e.has_conflict ? "conflict" : e.status
      };
    });

  var COLORS = {
    candidate: "#7ee0c0", possible: "#d29922",
    conflict: "#ff8a5b", confirmed: "#4ea1ff", rejected: "#4a5665"
  };

  // ---- simulation ---------------------------------------------------- //
  function tick(alpha) {
    var i, j, a, b, dx, dy, d2, d, f;

    // repulsion between every pair: the only O(n^2) part, fine for a single case
    for (i = 0; i < nodes.length; i++) {
      for (j = i + 1; j < nodes.length; j++) {
        a = nodes[i]; b = nodes[j];
        dx = b.x - a.x; dy = b.y - a.y;
        d2 = dx * dx + dy * dy || 1;
        d = Math.sqrt(d2);
        f = 5200 / d2;
        dx /= d; dy /= d;
        a.vx -= dx * f; a.vy -= dy * f;
        b.vx += dx * f; b.vy += dy * f;
      }
    }

    // springs along links, stiffer for a stronger score
    for (i = 0; i < links.length; i++) {
      var L = links[i], p = nodes[L.s], q = nodes[L.t];
      dx = q.x - p.x; dy = q.y - p.y;
      d = Math.sqrt(dx * dx + dy * dy) || 1;
      f = (d - 130) * 0.012 * (0.5 + L.score);
      dx /= d; dy /= d;
      p.vx += dx * f; p.vy += dy * f;
      q.vx -= dx * f; q.vy -= dy * f;
    }

    var cx = W / 2, cy = H / 2;
    for (i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.fixed) { n.vx = n.vy = 0; continue; }
      n.vx += (cx - n.x) * 0.0022;      // gentle pull inward so nothing drifts away
      n.vy += (cy - n.y) * 0.0022;
      n.x += n.vx * alpha;
      n.y += n.vy * alpha;
      n.vx *= 0.72; n.vy *= 0.72;
      n.x = Math.max(46, Math.min(W - 46, n.x));
      n.y = Math.max(38, Math.min(H - 38, n.y));
    }
  }

  // ---- draw ---------------------------------------------------------- //
  var gLink = document.createElementNS(NS, "g");
  var gNode = document.createElementNS(NS, "g");
  svg.appendChild(gLink);
  svg.appendChild(gNode);

  var linkEls = links.map(function (L) {
    var l = document.createElementNS(NS, "line");
    l.setAttribute("stroke", COLORS[L.status] || "#4a5665");
    l.setAttribute("stroke-width", (0.9 + L.score * 2.6).toFixed(2));
    l.setAttribute("stroke-opacity", L.status === "possible" ? "0.42" : "0.78");
    if (L.status === "possible") l.setAttribute("stroke-dasharray", "5 4");
    gLink.appendChild(l);
    return l;
  });

  var nodeEls = nodes.map(function (n) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node");
    g.style.cursor = "pointer";

    var c = document.createElementNS(NS, "circle");
    var r = 13 + Math.min(n.services.length, 4) * 2.4;
    c.setAttribute("r", r);
    c.setAttribute("fill", "#1b222c");
    c.setAttribute("stroke", "#4ea1ff");
    c.setAttribute("stroke-width", "1.6");
    g.appendChild(c);

    var t = document.createElementNS(NS, "text");
    t.setAttribute("y", r + 13);
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("fill", "#93a4b8");
    t.setAttribute("font-size", "10");
    t.setAttribute("font-family", "ui-monospace, monospace");
    t.textContent = n.id.replace(/^ent-/, "").replace(/^svc-/, "");
    g.appendChild(t);

    var title = document.createElementNS(NS, "title");
    title.textContent = n.id + "\n" + n.services.join("\n");
    g.appendChild(title);

    g.addEventListener("click", function () {
      window.location.href = "/case/" + encodeURIComponent(data.case_id) +
        "/entity/" + encodeURIComponent(n.id);
    });

    // drag
    g.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      n.fixed = true;
      g.setPointerCapture(ev.pointerId);
      function move(e2) {
        var pt = toSvg(e2);
        n.x = pt.x; n.y = pt.y; render();
      }
      function up(e2) {
        n.fixed = false;
        try { g.releasePointerCapture(e2.pointerId); } catch (_) {}
        g.removeEventListener("pointermove", move);
        g.removeEventListener("pointerup", up);
      }
      g.addEventListener("pointermove", move);
      g.addEventListener("pointerup", up);
    });

    gNode.appendChild(g);
    return { g: g, circle: c };
  });

  function toSvg(ev) {
    var r = svg.getBoundingClientRect();
    return {
      x: ((ev.clientX - r.left) / r.width) * W,
      y: ((ev.clientY - r.top) / r.height) * H
    };
  }

  function render() {
    for (var i = 0; i < links.length; i++) {
      var L = links[i], p = nodes[L.s], q = nodes[L.t];
      linkEls[i].setAttribute("x1", p.x); linkEls[i].setAttribute("y1", p.y);
      linkEls[i].setAttribute("x2", q.x); linkEls[i].setAttribute("y2", q.y);
    }
    for (var j = 0; j < nodes.length; j++) {
      nodeEls[j].g.setAttribute("transform",
        "translate(" + nodes[j].x.toFixed(1) + "," + nodes[j].y.toFixed(1) + ")");
    }
  }

  // settle, then stop: a graph that keeps moving is a distraction on a projector
  var steps = 0;
  (function loop() {
    tick(steps < 120 ? 1.0 : 0.45);
    render();
    if (++steps < 320) requestAnimationFrame(loop);
  })();
})();
