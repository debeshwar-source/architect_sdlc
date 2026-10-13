"""
Architecture diagrams, drawn as PNG images.

Plain words: this draws the pictures in the design document -- the map of everything
that has to be built and how the pieces connect, step-by-step diagrams of key flows,
and the order things must be built in. The pictures are drawn by this program (with
Pillow), so they need no outside tool and look the same in the app, in Word and PDF
files, and when attached to a Confluence page.

Three diagrams:
  * architecture_diagram  boxes grouped in layers (clients, edge, services, messaging,
                          data, external systems), coloured by status (new / changed /
                          existing / external), joined by labelled arrows (solid =
                          request/response, dashed = asynchronous event).
  * flow_diagram          a sequence diagram: who calls whom, in order.
  * build_order_diagram   the order the pieces must be built in.

Inputs are plain dicts, as produced by the Design agent:
    component:  {"name", "kind", "status", "technology"?}
    connection: {"from", "to", "kind"?, "label"?, "protocol"?, "status"?}
    flow:       {"name", "steps": [{"from", "to", "message", "kind"?}]}
Anything unknown or missing falls back to a sensible default, so a diagram is always
drawn.
"""

import math
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                      # Pillow missing: drawing says so, instead of the app failing to start
    Image = ImageDraw = ImageFont = None

from tools import fonts

SCALE = 2                                   # drawn at 2x, then shrunk: smooth lines and text
INK = (27, 31, 42)
GREY = (107, 114, 128)
FAINT = (156, 163, 175)
BAND = (249, 250, 252)
WHITE = (255, 255, 255)

# How each status looks: box fill, outline, and the small tag shown on the box.
STATUS = {
    "new":      {"fill": (220, 252, 231), "line": (22, 163, 74),  "tag": "NEW"},
    "changed":  {"fill": (255, 237, 213), "line": (234, 88, 12),  "tag": "CHANGED"},
    "existing": {"fill": (224, 231, 255), "line": (79, 70, 229),  "tag": ""},
    "external": {"fill": (243, 244, 246), "line": (107, 114, 128), "tag": "EXTERNAL"},
}
EDGE_COLOR = {"new": (22, 163, 74), "changed": (234, 88, 12), "existing": (120, 128, 142)}

# Which horizontal band a kind of component sits in (top to bottom).
LAYER_OF_KIND = {"client": 0, "gateway": 1, "service": 2, "job": 2, "library": 2,
                 "messaging": 3, "cache": 4, "database": 4, "external": 5}
LAYER_TITLE = {0: "CLIENTS", 1: "EDGE", 2: "SERVICES", 3: "MESSAGING", 4: "DATA", 5: "EXTERNAL SYSTEMS"}
KIND_LABEL = {"client": "CLIENT", "gateway": "GATEWAY", "service": "SERVICE", "job": "BACKGROUND JOB",
              "library": "LIBRARY", "messaging": "MESSAGING", "cache": "CACHE", "database": "DATABASE",
              "external": "EXTERNAL SYSTEM"}
ASYNC_KINDS = {"async_event", "async", "event", "return"}

NODE_W, NODE_H = 214, 86
GAP_X = 52
MARGIN_X = 64


# ------------------------------------------------------------
# A small drawing surface (everything given in normal pixels, drawn at SCALE)
# ------------------------------------------------------------

class _Canvas:
    def __init__(self, width: int, height: int):
        if Image is None:
            raise fonts.MissingLibrary("Diagrams need Pillow. Install it with: pip install pillow")
        self.w, self.h = int(width), int(height)
        self.img = Image.new("RGB", (self.w * SCALE, self.h * SCALE), WHITE)
        self.d = ImageDraw.Draw(self.img)
        self._fonts: Dict[Tuple[float, str], ImageFont.FreeTypeFont] = {}

    def font(self, size: float, style: str = "regular") -> ImageFont.FreeTypeFont:
        key = (size, style)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(fonts.font_path(style), int(round(size * SCALE)))
        return self._fonts[key]

    def text_width(self, text: str, size: float, style: str = "regular") -> float:
        return self.font(size, style).getlength(fonts.stand_in(text)) / SCALE

    def text(self, xy, text: str, size: float, style="regular", fill=INK, anchor="la") -> None:
        self.d.text((xy[0] * SCALE, xy[1] * SCALE), fonts.stand_in(text), font=self.font(size, style),
                    fill=fill, anchor=anchor)

    def wrap(self, text: str, max_width: float, size: float, style="regular", max_lines=2) -> List[str]:
        """Break `text` into at most `max_lines` lines that fit `max_width`; the last line
        ends with an ellipsis if the text had to be cut."""
        words, lines, line = text.split(), [], ""
        for word in words:
            trial = f"{line} {word}".strip()
            if self.text_width(trial, size, style) <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip(".,;: ") + "…"
        # A single word longer than the width: trim it.
        fitted = []
        for ln in lines:
            while self.text_width(ln, size, style) > max_width and len(ln) > 1:
                ln = ln[:-2] + "…"
            fitted.append(ln)
        return fitted or [""]

    def line(self, points: Sequence[Tuple[float, float]], fill, width=2.0, dashed=False) -> None:
        pts = [(x * SCALE, y * SCALE) for x, y in points]
        if not dashed:
            self.d.line(pts, fill=fill, width=int(round(width * SCALE)), joint="curve")
            return
        for a, b in _dash_segments(pts, 9 * SCALE, 6 * SCALE):
            self.d.line([a, b], fill=fill, width=int(round(width * SCALE)))

    def polygon(self, points, fill) -> None:
        self.d.polygon([(x * SCALE, y * SCALE) for x, y in points], fill=fill)

    def rrect(self, box, radius, fill=None, outline=None, width=2.0, dashed=False) -> None:
        x0, y0, x1, y1 = [v * SCALE for v in box]
        if not dashed:
            self.d.rounded_rectangle((x0, y0, x1, y1), radius=radius * SCALE, fill=fill, outline=outline,
                                     width=int(round(width * SCALE)))
            return
        if fill:
            self.d.rounded_rectangle((x0, y0, x1, y1), radius=radius * SCALE, fill=fill)
        r = radius * SCALE
        outline_pts = [(x0 + r, y0), (x1 - r, y0), (x1, y0 + r), (x1, y1 - r), (x1 - r, y1), (x0 + r, y1),
                       (x0, y1 - r), (x0, y0 + r), (x0 + r, y0)]
        for a, b in _dash_segments(outline_pts, 10 * SCALE, 6 * SCALE):
            self.d.line([a, b], fill=outline, width=int(round(width * SCALE)))

    def ellipse(self, box, fill=None, outline=None, width=2.0) -> None:
        self.d.ellipse([v * SCALE for v in box], fill=fill, outline=outline, width=int(round(width * SCALE)))

    def png(self) -> bytes:
        small = self.img.resize((self.w, self.h), Image.LANCZOS)
        buffer = BytesIO()
        small.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()


def _dash_segments(points, on: float, off: float):
    """Pieces of a polyline to draw so that it looks dashed."""
    pattern, drawing, remaining = (on, off), True, on
    segments = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length == 0:
            continue
        ux, uy, pos = (x1 - x0) / length, (y1 - y0) / length, 0.0
        while pos < length:
            step = min(remaining, length - pos)
            if drawing:
                segments.append(((x0 + ux * pos, y0 + uy * pos), (x0 + ux * (pos + step), y0 + uy * (pos + step))))
            pos += step
            remaining -= step
            if remaining <= 1e-6:
                drawing = not drawing
                remaining = pattern[0] if drawing else pattern[1]
    return segments


def _bezier(p0, p1, p2, p3, steps=48):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        pts.append((u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]))
    return pts


def _arrowhead(canvas: _Canvas, tip, toward, color, size=12) -> None:
    """A filled triangle at `tip`, pointing in the direction from `toward` to `tip`."""
    dx, dy = tip[0] - toward[0], tip[1] - toward[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    base = (tip[0] - ux * size, tip[1] - uy * size)
    px, py = -uy * size * 0.45, ux * size * 0.45
    canvas.polygon([tip, (base[0] + px, base[1] + py), (base[0] - px, base[1] - py)], color)


def _clean_name(value: Any) -> str:
    return " ".join(str(value or "").split())


# ------------------------------------------------------------
# Architecture diagram
# ------------------------------------------------------------

def _normalise_components(components: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for c in components or []:
        name = _clean_name(c.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        kind = c.get("kind") if c.get("kind") in KIND_LABEL else "service"
        status = c.get("status") if c.get("status") in STATUS else "existing"
        layer = 5 if (status == "external" or kind == "external") else LAYER_OF_KIND[kind]
        out.append({"name": name, "kind": kind, "status": status, "layer": layer,
                    "technology": _clean_name(c.get("technology"))})
    return out


def _empty_diagram(message: str) -> bytes:
    canvas = _Canvas(640, 160)
    canvas.text((320, 80), message, 18, fill=GREY, anchor="mm")
    return canvas.png()


def architecture_diagram(components: Sequence[Dict[str, Any]], connections: Sequence[Dict[str, Any]],
                         title: str = "", _geometry: Optional[Dict[str, Any]] = None) -> bytes:
    """The map of what is being built and how it connects, as PNG bytes.
    (`_geometry`: tests pass a dict to receive where everything was placed.)"""
    nodes = _normalise_components(components)
    if not nodes:
        return _empty_diagram("No components to draw")
    by_name = {n["name"]: n for n in nodes}
    edges = []
    for c in connections or []:
        a, b = _clean_name(c.get("from")), _clean_name(c.get("to"))
        if a in by_name and b in by_name and a != b:
            edges.append({"from": a, "to": b, "kind": c.get("kind") or "sync_api",
                          "label": _clean_name(c.get("label")), "protocol": _clean_name(c.get("protocol")),
                          "status": c.get("status") if c.get("status") in EDGE_COLOR else "existing"})

    layers: Dict[int, List[Dict[str, Any]]] = {}
    for n in nodes:
        layers.setdefault(n["layer"], []).append(n)
    layer_ids = sorted(layers)
    pos_of = {lid: i for i, lid in enumerate(layer_ids)}
    L = len(layer_ids)
    widest = max(len(v) for v in layers.values())
    width = max(1000, widest * (NODE_W + GAP_X) - GAP_X + 2 * MARGIN_X)

    # ---- 1. sideways position of every box: ordered within its layer, lined up with its neighbours ----
    neighbours: Dict[str, List[str]] = {n["name"]: [] for n in nodes}
    for e in edges:
        neighbours[e["from"]].append(e["to"])
        neighbours[e["to"]].append(e["from"])
    for lid in layer_ids:
        row = layers[lid]
        for i, n in enumerate(row):
            n["x"] = (width - (len(row) * NODE_W + (len(row) - 1) * GAP_X)) / 2 + i * (NODE_W + GAP_X)
    for lid in layer_ids + layer_ids[::-1] + layer_ids + layer_ids[::-1]:
        row = layers[lid]
        desired = {}
        for n in row:
            xs = [by_name[m]["x"] + NODE_W / 2 for m in neighbours[n["name"]] if by_name[m]["layer"] != lid]
            desired[n["name"]] = sum(xs) / len(xs) if xs else n["x"] + NODE_W / 2
        row.sort(key=lambda n: desired[n["name"]])
        cursor, placed = MARGIN_X, []
        for n in row:
            x = max(desired[n["name"]] - NODE_W / 2, cursor)
            placed.append(x)
            cursor = x + NODE_W + GAP_X
        limit = width - MARGIN_X - NODE_W                  # pull back anything pushed past the right edge
        for i in range(len(placed) - 1, -1, -1):
            placed[i] = min(placed[i], limit)
            limit = placed[i] - NODE_W - GAP_X
        shift = sum(desired[n["name"]] - NODE_W / 2 - x for n, x in zip(row, placed)) / len(row)
        shift = max(MARGIN_X - placed[0], min(shift, width - MARGIN_X - NODE_W - placed[-1]))
        for n, x in zip(row, placed):
            n["x"] = x + shift
    centre_x = {n["name"]: n["x"] + NODE_W / 2 for n in nodes}

    # ---- 2. which side of each box a connection uses, and where along that side ----
    kinds, side_of = [], []
    for e in edges:
        pa, pb = pos_of[by_name[e["from"]]["layer"]], pos_of[by_name[e["to"]]["layer"]]
        kinds.append("same" if pa == pb else "adjacent" if abs(pa - pb) == 1 else "span")
        side_of.append(("top", "top") if pa == pb else (("bottom", "top") if pa < pb else ("top", "bottom")))
    ends: Dict[Tuple[str, str], List[Tuple[int, str, float]]] = {}
    for idx, e in enumerate(edges):
        ends.setdefault((e["from"], side_of[idx][0]), []).append((idx, "from", centre_x[e["to"]]))
        ends.setdefault((e["to"], side_of[idx][1]), []).append((idx, "to", centre_x[e["from"]]))
    attach_x: Dict[Tuple[int, str], float] = {}
    for (name, _), items in ends.items():
        items.sort(key=lambda it: (it[2], it[0]))
        for k, (idx, which, _) in enumerate(items):
            attach_x[(idx, which)] = by_name[name]["x"] + 20 + (k + 1) / (len(items) + 1) * (NODE_W - 40)

    # ---- 3. a connection that skips a layer needs a clear vertical path: straight down its own
    #         column if that is free of boxes, else a free corridor between the boxes ----
    used_channels: List[float] = []
    span_plan: Dict[int, Tuple[str, float]] = {}
    for idx, e in enumerate(edges):
        if kinds[idx] != "span":
            continue
        pa, pb = pos_of[by_name[e["from"]]["layer"]], pos_of[by_name[e["to"]]["layer"]]
        x0, x1 = attach_x[(idx, "from")], attach_x[(idx, "to")]
        blocked = sorted((by_name[n["name"]]["x"] - 22, by_name[n["name"]]["x"] + NODE_W + 22)
                         for n in nodes if min(pa, pb) < pos_of[n["layer"]] < max(pa, pb))
        free = lambda x: not any(lo <= x <= hi for lo, hi in blocked)
        if free(x0):
            span_plan[idx] = ("source", x0)
        elif free(x1):
            span_plan[idx] = ("target", x1)
        else:
            corridors, cursor = [], MARGIN_X - 36
            for lo, hi in blocked:
                if lo - cursor >= 24:
                    corridors.append((cursor, lo))
                cursor = max(cursor, hi)
            corridors.append((cursor, width - MARGIN_X + 36))
            mid, best = (x0 + x1) / 2, None
            for lo, hi in corridors:
                cx = min(max(mid, lo + 10), hi - 10)
                while any(abs(cx - u) < 13 for u in used_channels) and cx + 13 < hi - 6:
                    cx += 13
                if best is None or abs(cx - mid) < best[0]:
                    best = (abs(cx - mid), cx)
            used_channels.append(best[1])
            span_plan[idx] = ("corridor", best[1])

    # ---- 4. how many horizontal "lanes" each gap between layers must hold, then the heights ----
    # gap g is the space above layer position g (gap 0 = above the first layer).
    usage: Dict[int, List[Tuple[int, str]]] = {g: [] for g in range(L + 1)}
    for idx, e in enumerate(edges):
        pa, pb = pos_of[by_name[e["from"]]["layer"]], pos_of[by_name[e["to"]]["layer"]]
        if kinds[idx] == "same":
            usage[pa].append((idx, "only"))
        elif kinds[idx] == "adjacent":
            usage[max(pa, pb)].append((idx, "only"))
        else:
            first, last = (pa + 1, pb) if pa < pb else (pa, pb + 1)
            if span_plan[idx][0] in ("target", "corridor"):
                usage[first].append((idx, "first"))
            if span_plan[idx][0] in ("source", "corridor"):
                usage[last].append((idx, "last"))
    BAND_ABOVE, BAND_BELOW, PITCH = 30, 16, 34
    gap_h = {g: 46 + max(24, PITCH * len(usage[g])) for g in range(1, L)}
    gap_h[0] = (46 + PITCH * len(usage[0])) if usage[0] else 40
    top = 70 if title else 30
    node_y, y = {}, top
    for p in range(L):
        y += gap_h[p]
        node_y[p] = y
        y += NODE_H
    height = int(y + BAND_BELOW + 82)
    for lid in layer_ids:
        for n in layers[lid]:
            n["y"] = node_y[pos_of[lid]]
    box_of = {n["name"]: (n["x"], n["y"], n["x"] + NODE_W, n["y"] + NODE_H) for n in nodes}

    def region(g: int) -> Tuple[float, float]:       # the vertical strip lanes in gap g may use
        r_top = (node_y[g - 1] + NODE_H + BAND_BELOW) if g > 0 else top
        r_bottom = (node_y[g] - BAND_ABOVE) if g < L else height
        return r_top + 8, r_bottom - 8

    lane_y: Dict[Tuple[int, str], float] = {}
    for g, entries in usage.items():
        if not entries:
            continue

        def order(entry):                              # runs ordered so that they cross as little as possible
            idx, which = entry
            sx, tx = attach_x[(idx, "from")], attach_x[(idx, "to")]
            if kinds[idx] == "same":
                return (0, -abs(sx - tx))
            return (1, -sx) if tx >= sx else (2, sx)
        entries.sort(key=order)
        r_top, r_bottom = region(g)
        for k, entry in enumerate(entries):
            lane_y[entry] = r_top + (k + 0.5) * (r_bottom - r_top) / len(entries)

    # ---- 5. the actual paths ----
    def point(idx: int, which: str) -> Tuple[float, float]:
        name = edges[idx][which]
        side = side_of[idx][0 if which == "from" else 1]
        return attach_x[(idx, which)], (box_of[name][1] if side == "top" else box_of[name][3])

    routes: List[List[Tuple[float, float]]] = []
    for idx, e in enumerate(edges):
        p0, p1 = point(idx, "from"), point(idx, "to")
        if kinds[idx] == "same":
            ly = lane_y[(idx, "only")]
            pts = [p0, (p0[0], ly), (p1[0], ly), p1]
        elif kinds[idx] == "adjacent":
            ly = lane_y[(idx, "only")]
            pts = [p0, (p0[0], ly), (p1[0], ly), p1]
        else:
            mode, cx = span_plan[idx]
            if mode == "source":
                ly = lane_y[(idx, "last")]
                pts = [p0, (p0[0], ly), (p1[0], ly), p1]
            elif mode == "target":
                ly = lane_y[(idx, "first")]
                pts = [p0, (p0[0], ly), (p1[0], ly), p1]
            else:
                ly1, ly2 = lane_y[(idx, "first")], lane_y[(idx, "last")]
                pts = [p0, (p0[0], ly1), (cx, ly1), (cx, ly2), (p1[0], ly2), p1]
        routes.append(_round_corners(pts, 11))

    canvas = _Canvas(width, height)
    if title:
        canvas.text((MARGIN_X - 20, 24), title, 22, "bold")
    for lid in layer_ids:
        p = pos_of[lid]
        canvas.rrect((16, node_y[p] - BAND_ABOVE, width - 16, node_y[p] + NODE_H + BAND_BELOW), 10, fill=BAND)
        canvas.text((30, node_y[p] - BAND_ABOVE + 7), LAYER_TITLE[lid], 11, "bold", fill=FAINT)
    for e, pts in zip(edges, routes):
        color = EDGE_COLOR[e["status"]]
        canvas.line(pts, color, 3.0 if e["status"] != "existing" else 2.2, dashed=e["kind"] in ASYNC_KINDS)
        _arrowhead(canvas, pts[-1], pts[-4] if len(pts) > 4 else pts[0], color, 13)
    for n in nodes:
        _draw_node(canvas, n)

    # ---- 6. labels: on a clear stretch of their own line, away from boxes, other labels and other lines ----
    node_boxes = list(box_of.values())
    label_boxes: List[Tuple[float, float, float, float]] = []
    for i, (e, pts) in enumerate(zip(edges, routes)):
        lines = []
        if e["label"]:
            lines += [(ln, 12.5, "bold", INK) for ln in canvas.wrap(e["label"], 200, 12.5, "bold", 2)]
        if e["protocol"]:
            lines.append((canvas.wrap(e["protocol"], 200, 10.5, "regular", 1)[0], 10.5, "regular", GREY))
        if not lines:
            continue
        w = max(canvas.text_width(t, s, st) for t, s, st, _ in lines) + 14
        h = sum(s + 3.5 for _, s, _, _ in lines) + 8
        others = [r for j, r in enumerate(routes) if j != i]
        chosen, best = None, None
        for cx, cy in _label_spots(pts, w, h):
            box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            cost = (100 * sum(_overlap(box, nb, 5) for nb in node_boxes)          # on top of a box: worst
                    + 100 * (box[0] < 4 or box[1] < 4 or box[2] > width - 4 or box[3] > height - 4)
                    + 40 * sum(_overlap(box, lb, 4) for lb in label_boxes)        # on top of another label
                    + 10 * sum(_polyline_hits(r, box, 3) for r in others))        # across another line
            if best is None or cost < best:
                chosen, best = box, cost
            if cost == 0:
                break
        label_boxes.append(chosen)
        canvas.rrect(chosen, 7, fill=WHITE, outline=EDGE_COLOR[e["status"]], width=1.4)
        ty = chosen[1] + 5
        for t_, s_, st_, fill_ in lines:
            canvas.text(((chosen[0] + chosen[2]) / 2, ty), t_, s_, st_, fill=fill_, anchor="ma")
            ty += s_ + 3.5

    if _geometry is not None:
        _geometry.update({"size": (width, height), "nodes": box_of, "labels": label_boxes,
                          "routes": routes, "edges": edges})
    _draw_legend(canvas, height - 44, width)
    return canvas.png()


def _round_corners(points, radius):
    """A right-angle path with its corners smoothed; returns a dense list of points."""
    pts = [points[0]] + [p for i, p in enumerate(points[1:], 1) if p != points[i - 1]]
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
        d1, d2 = math.hypot(x1 - x0, y1 - y0), math.hypot(x2 - x1, y2 - y1)
        r = min(radius, d1 / 2, d2 / 2)
        a = (x1 - (x1 - x0) / d1 * r, y1 - (y1 - y0) / d1 * r)
        b = (x1 + (x2 - x1) / d2 * r, y1 + (y2 - y1) / d2 * r)
        for k in range(9):
            t = k / 8
            out.append(((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * x1 + t * t * b[0],
                        (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * y1 + t * t * b[1]))
    out.append(pts[-1])
    return out


def _label_spots(points, w=0.0, h=0.0):
    """Candidate label centres along a path, best first: the middle of its longest horizontal
    stretches (the label sits on the line), then beside its vertical stretches (the line stays
    visible). Spots right at the arrowhead are skipped."""
    end = points[-1]
    segments = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length > 30:
            segments.append((abs(y1 - y0) < 1, length, (x0, y0), (x1, y1)))
    segments.sort(key=lambda s: (not s[0], -s[1]))
    spots = []
    for horizontal, _, (x0, y0), (x1, y1) in segments:
        for f in (0.5, 0.3, 0.7, 0.4, 0.6, 0.18, 0.82, 0.1, 0.9):
            cx, cy = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
            if math.hypot(cx - end[0], cy - end[1]) < 44:      # keep clear of the arrowhead
                continue
            if horizontal:
                spots.append((cx, cy))
                spots.append((cx, cy - h / 2 - 10))            # just above the line...
                spots.append((cx, cy + h / 2 + 10))            # ...or just below it
            else:
                spots.append((cx + w / 2 + 9, cy))             # to the right of the line...
                spots.append((cx - w / 2 - 9, cy))             # ...or to the left
    for horizontal, _, (x0, y0), (x1, y1) in segments:         # last resort: on a vertical line
        if not horizontal:
            spots.append(((x0 + x1) / 2, (y0 + y1) / 2))
    return spots or [points[len(points) // 2]]


def _polyline_hits(points, box, pad=0) -> bool:
    """Does a path pass through `box`? (checked every few pixels along it)"""
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) // 4))
        for k in range(steps + 1):
            x, y = x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps
            if box[0] - pad <= x <= box[2] + pad and box[1] - pad <= y <= box[3] + pad:
                return True
    return False


def _overlap(a, b, pad=0) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _hits(points, boxes, pad=0) -> bool:
    return any(b[0] - pad <= x <= b[2] + pad and b[1] - pad <= y <= b[3] + pad for x, y in points for b in boxes)


def _draw_node(canvas: _Canvas, n: Dict[str, Any]) -> None:
    style = STATUS[n["status"]]
    x, y = n["x"], n["y"]
    box = (x, y, x + NODE_W, y + NODE_H)
    dashed = n["status"] == "external"
    if n["kind"] == "database":                               # a cylinder
        top_h = 18
        canvas.rrect((x, y + top_h / 2, x + NODE_W, y + NODE_H - top_h / 2), 2, fill=style["fill"])
        canvas.line([(x, y + top_h / 2), (x, y + NODE_H - top_h / 2)], style["line"], 2.2)
        canvas.line([(x + NODE_W, y + top_h / 2), (x + NODE_W, y + NODE_H - top_h / 2)], style["line"], 2.2)
        canvas.ellipse((x, y + NODE_H - top_h, x + NODE_W, y + NODE_H), fill=style["fill"], outline=style["line"], width=2.2)
        canvas.rrect((x + 1.5, y + top_h / 2 + 4, x + NODE_W - 1.5, y + NODE_H - top_h / 2 - 2), 0, fill=style["fill"])
        canvas.ellipse((x, y, x + NODE_W, y + top_h), fill=style["fill"], outline=style["line"], width=2.2)
        text_top = y + 26
    else:
        canvas.rrect(box, 12, fill=style["fill"], outline=style["line"], width=2.4, dashed=dashed)
        text_top = y + 22
    name_lines = canvas.wrap(n["name"], NODE_W - 28, 17, "bold", 2)
    for i, ln in enumerate(name_lines):
        canvas.text((x + NODE_W / 2, text_top + i * 20), ln, 17, "bold", anchor="ma")
    sub = KIND_LABEL[n["kind"]] + (f" · {n['technology']}" if n["technology"] else "")
    sub = canvas.wrap(sub, NODE_W - 24, 11.5, "regular", 1)[0]
    canvas.text((x + NODE_W / 2, y + NODE_H - 24), sub, 11.5, "regular", fill=GREY, anchor="ma")
    tag = style["tag"]
    if tag:
        tw = canvas.text_width(tag, 10, "bold") + 14
        canvas.rrect((x + NODE_W - tw - 8, y - 9, x + NODE_W - 8, y + 9), 9, fill=style["line"])
        canvas.text((x + NODE_W - 8 - tw / 2, y), tag, 10, "bold", fill=WHITE, anchor="mm")


def _draw_legend(canvas: _Canvas, y: float, width: float) -> None:
    x = 30
    for key, label in (("new", "New"), ("changed", "Changed"), ("existing", "Existing"), ("external", "External")):
        s = STATUS[key]
        canvas.rrect((x, y, x + 24, y + 16), 4, fill=s["fill"], outline=s["line"], width=1.8, dashed=key == "external")
        canvas.text((x + 32, y + 8), label, 12.5, fill=INK, anchor="lm")
        x += 40 + canvas.text_width(label, 12.5) + 22
    canvas.line([(x, y + 8), (x + 38, y + 8)], EDGE_COLOR["existing"], 2.2)
    canvas.text((x + 46, y + 8), "request / response", 12.5, anchor="lm")
    x += 46 + canvas.text_width("request / response", 12.5) + 24
    canvas.line([(x, y + 8), (x + 38, y + 8)], EDGE_COLOR["existing"], 2.2, dashed=True)
    canvas.text((x + 46, y + 8), "asynchronous event", 12.5, anchor="lm")


# ------------------------------------------------------------
# Flow (sequence) diagram
# ------------------------------------------------------------

def flow_diagram(flow: Dict[str, Any], components: Sequence[Dict[str, Any]]) -> bytes:
    """One flow as a sequence diagram: participants across the top, steps down the page."""
    steps = [s for s in (flow or {}).get("steps") or [] if _clean_name(s.get("from")) and _clean_name(s.get("to"))]
    if not steps:
        return _empty_diagram("This flow has no steps")
    info = {n["name"]: n for n in _normalise_components(components)}
    order: List[str] = []
    for s in steps:
        for key in ("from", "to"):
            name = _clean_name(s[key])
            if name not in order:
                order.append(name)

    n = len(order)
    probe = _Canvas(10, 10)
    longest = max(probe.text_width(name, 14.5, "bold") for name in order)
    col = max(190, min(290, 1180 // n), int(longest) + 52)
    margin = 36
    width = n * col + 2 * margin
    centre = {name: margin + col * (i + 0.5) for i, name in enumerate(order)}
    head_y, head_h = 62, 62
    canvas_probe = _Canvas(width, 10)

    # Work out each step's height first (long messages wrap onto more lines).
    rows = []
    for i, s in enumerate(steps, start=1):
        a, b = _clean_name(s["from"]), _clean_name(s["to"])
        span = abs(centre[b] - centre[a]) if a != b else 190
        text = f"{i}. {_clean_name(s.get('message'))}".strip()
        lines = canvas_probe.wrap(text, max(150, span - 26), 13, "regular", 3)
        rows.append((s, a, b, lines))
    y = head_y + head_h + 30
    placed = []
    for s, a, b, lines in rows:
        y += len(lines) * 17 + 8
        placed.append((s, a, b, lines, y))
        y += 34 if a != b else 52
    height = int(y + 30)

    canvas = _Canvas(width, height)
    canvas.text((margin, 22), _clean_name((flow or {}).get("name")) or "Flow", 20, "bold")
    for name in order:
        c = info.get(name, {"kind": "service", "status": "existing"})
        style = STATUS[c["status"]]
        x = centre[name]
        canvas.line([(x, head_y + head_h), (x, height - 18)], FAINT, 1.4, dashed=True)
        canvas.rrect((x - col / 2 + 12, head_y, x + col / 2 - 12, head_y + head_h), 10, fill=style["fill"],
                     outline=style["line"], width=2.2, dashed=c["status"] == "external")
        lines = canvas.wrap(name, col - 44, 14.5, "bold", 2)
        top = head_y + 10 if len(lines) == 2 else head_y + 17
        for k, ln in enumerate(lines):
            canvas.text((x, top + k * 18), ln, 14.5, "bold", anchor="ma")
        canvas.text((x, head_y + head_h - 16), KIND_LABEL.get(c["kind"], "SERVICE"), 10.5, fill=GREY, anchor="ma")

    for s, a, b, lines, ay in placed:
        color = INK
        dashed = (s.get("kind") or "call") in ASYNC_KINDS
        xa, xb = centre[a], centre[b]
        if a == b:                                               # a call to itself: a small loop
            canvas.line([(xa, ay), (xa + 46, ay), (xa + 46, ay + 24), (xa + 4, ay + 24)], color, 2.0, dashed)
            _arrowhead(canvas, (xa + 3, ay + 24), (xa + 30, ay + 24), color, 11)
            for k, ln in enumerate(lines):
                canvas.text((xa + 56, ay - 8 + k * 17 - (len(lines) - 1) * 8), ln, 13, anchor="lm")
        else:
            d = 1 if xb > xa else -1
            canvas.line([(xa + 4 * d, ay), (xb - 3 * d, ay)], color, 2.0, dashed)
            _arrowhead(canvas, (xb - 2 * d, ay), (xb - 30 * d, ay), color, 12)
            top = ay - 6 - len(lines) * 17
            for k, ln in enumerate(lines):
                half = canvas.text_width(ln, 13) / 2 + 4
                canvas.rrect(((xa + xb) / 2 - half, top + k * 17 - 1, (xa + xb) / 2 + half, top + k * 17 + 16), 3, fill=WHITE)
                canvas.text(((xa + xb) / 2, top + k * 17), ln, 13, anchor="ma")
    return canvas.png()


# ------------------------------------------------------------
# Build-order diagram
# ------------------------------------------------------------

def build_order_diagram(order: Sequence[str], components: Sequence[Dict[str, Any]]) -> bytes:
    """The order things must land in, left to right and top to bottom."""
    names = [_clean_name(n) for n in order or [] if _clean_name(n)]
    if not names:
        return _empty_diagram("No build order to draw")
    info = {c["name"]: c for c in _normalise_components(components)}
    per_row, box_w, box_h, gap = 4, 220, 74, 58
    rows = [names[i:i + per_row] for i in range(0, len(names), per_row)]
    width = max(900, per_row * box_w + (per_row - 1) * gap + 2 * 50)
    height = 70 + len(rows) * (box_h + 56) + 20
    canvas = _Canvas(width, height)
    canvas.text((50, 24), "Build order: earlier steps must land before later ones", 18, "bold")
    left = (width - (per_row * box_w + (per_row - 1) * gap)) / 2
    pos = {}
    for r, row in enumerate(rows):
        for c, name in enumerate(row):
            pos[name] = (left + c * (box_w + gap), 70 + r * (box_h + 56))
    for i in range(len(names) - 1):
        (x0, y0), (x1, y1) = pos[names[i]], pos[names[i + 1]]
        if abs(y0 - y1) < 1:
            canvas.line([(x0 + box_w, y0 + box_h / 2), (x1, y1 + box_h / 2)], GREY, 2.4)
            _arrowhead(canvas, (x1, y1 + box_h / 2), (x1 - 30, y1 + box_h / 2), GREY, 12)
        else:                                                     # wrap to the next row
            mid = y0 + box_h + 28
            canvas.line([(x0 + box_w / 2, y0 + box_h), (x0 + box_w / 2, mid), (x1 + box_w / 2, mid), (x1 + box_w / 2, y1)], GREY, 2.4)
            _arrowhead(canvas, (x1 + box_w / 2, y1), (x1 + box_w / 2, y1 - 30), GREY, 12)
    for i, name in enumerate(names, start=1):
        c = info.get(name, {"kind": "service", "status": "existing", "technology": ""})
        style = STATUS[c["status"]]
        x, y = pos[name]
        canvas.rrect((x, y, x + box_w, y + box_h), 12, fill=style["fill"], outline=style["line"], width=2.4,
                     dashed=c["status"] == "external")
        canvas.ellipse((x - 14, y - 14, x + 18, y + 18), fill=style["line"])
        canvas.text((x + 2, y + 2), str(i), 15, "bold", fill=WHITE, anchor="mm")
        for k, ln in enumerate(canvas.wrap(name, box_w - 44, 15.5, "bold", 2)):
            canvas.text((x + box_w / 2 + 8, y + 14 + k * 19), ln, 15.5, "bold", anchor="ma")
        canvas.text((x + box_w / 2, y + box_h - 20), KIND_LABEL.get(c["kind"], "SERVICE") + (f" · {style['tag'].title()}" if style["tag"] else ""),
                    11, fill=GREY, anchor="ma")
    return canvas.png()
