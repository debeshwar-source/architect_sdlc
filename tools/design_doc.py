"""
The Design agent's output as a design document: shown in the app, downloadable as Word
or PDF, and publishable as a Confluence page, with real diagrams.

Plain words: the Design agent decides exactly what has to be built (components) and how
it is all connected (connections, flows). This module lays that out as a proper design
document -- overview, an architecture map, tables of components and interconnections,
step-by-step flow diagrams, build order, contracts, impact, tests, risks -- and draws the
diagrams (tools/diagrams.py). It first builds ONE description of the document
(build_model), then renders it four ways (screen, .docx, .pdf, Confluence), so they can
never disagree.

The user may change the WORDS of the document (titles, descriptions, notes...) but not its
structure: which components exist and how they connect stay as the Design agent made them
(changing those is what "ask the Design agent to change the design" is for). Wording
changes are kept as `edits`, a plain dict of "where" -> "new text"; the design itself is
never altered. `editable_fields()` lists exactly what can be changed.

The Word and PDF libraries are imported only when a file is made; a missing one raises
MissingLibrary with the command that fixes it.
"""

import copy
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from tools import diagrams, fonts
from tools.fonts import MissingLibrary
from tools.requirement_doc import _CONTROL

# fmt -> (label shown to the user, file extension, MIME type)
FORMATS = {
    "docx": ("Word document (.docx)", "docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pdf": ("PDF", "pdf", "application/pdf"),
}

STATUS_LABEL = {"new": "New", "changed": "Changed", "existing": "Existing", "external": "External"}
# Colours: Confluence "status" lozenges, Streamlit coloured text, and RGB for Word / PDF.
CONFLUENCE_COLOUR = {"new": "Green", "changed": "Yellow", "existing": "Blue", "external": "Grey"}
STREAMLIT_COLOUR = {"new": "green", "changed": "orange", "existing": "blue", "external": "gray"}
HEX_COLOUR = {"new": "16A34A", "changed": "EA580C", "existing": "4F46E5", "external": "6B7280"}
KIND_LABEL = {"service": "Service", "client": "Client", "gateway": "Gateway", "database": "Database",
              "messaging": "Messaging", "cache": "Cache", "job": "Background job", "library": "Library",
              "external": "External system"}
CONNECTION_LABEL = {"sync_api": "Request / response", "async_event": "Event (asynchronous)",
                    "data_access": "Data access", "batch": "Batch", "ui_call": "User interface call"}
CONNECTION_SHORT = {"sync_api": "call", "async_event": "event", "data_access": "data", "batch": "batch", "ui_call": "UI"}


def _clean(value: Any) -> str:
    return _CONTROL.sub("", str(value if value is not None else "")).strip()


def _line(value: Any) -> str:
    return " ".join(_clean(value).split())


def default_title(requirement: Optional[Dict[str, Any]]) -> str:
    return f"{_line((requirement or {}).get('title')) or 'Requirement'} — Solution Design"


# ------------------------------------------------------------
# What the user may reword
# ------------------------------------------------------------

def _fields_of(design: Dict[str, Any], title: str) -> List[Dict[str, Any]]:
    """Every piece of wording that can be changed: path, label, kind ("line" = one line,
    "text" = a paragraph, "lines" = one item per line), the section it belongs to, whether
    it may be left empty, and its current value."""
    out: List[Dict[str, Any]] = []

    def add(path, label, kind, section, value, required=False):
        out.append({"path": path, "label": label, "kind": kind, "section": section, "value": value,
                    "required": required})

    add("doc.title", "Document title", "line", "Title and overview", title, True)
    add("overview", "Overview", "text", "Title and overview", design.get("overview") or "", True)
    for i, c in enumerate(design.get("components") or []):
        n = c.get("name", f"component {i + 1}")
        add(f"components.{i}.responsibility", f"{n} — responsibility", "text", "Components", c.get("responsibility") or "", True)
        add(f"components.{i}.technology", f"{n} — technology", "line", "Components", c.get("technology") or "")
        add(f"components.{i}.interfaces", f"{n} — interfaces (one per line)", "lines", "Components", c.get("interfaces") or [])
        add(f"components.{i}.data_owned", f"{n} — data owned (one per line)", "lines", "Components", c.get("data_owned") or [])
    for j, k in enumerate(design.get("connections") or []):
        n = f"{k.get('from')} → {k.get('to')}"
        add(f"connections.{j}.label", f"{n} — what passes", "line", "Interconnections", k.get("label") or "", True)
        add(f"connections.{j}.protocol", f"{n} — protocol", "line", "Interconnections", k.get("protocol") or "")
        add(f"connections.{j}.description", f"{n} — description", "text", "Interconnections", k.get("description") or "")
    for f_i, f in enumerate(design.get("flows") or []):
        add(f"flows.{f_i}.name", f"Flow {f_i + 1} — name", "line", "Key flows", f.get("name") or "", True)
        add(f"flows.{f_i}.description", f"Flow {f_i + 1} — description", "text", "Key flows", f.get("description") or "")
        for m, step in enumerate(f.get("steps") or []):
            add(f"flows.{f_i}.steps.{m}.message", f"Flow {f_i + 1}, step {m + 1} — message", "line", "Key flows",
                step.get("message") or "", True)
    for x, c in enumerate(design.get("cross_service_contracts") or []):
        add(f"cross_service_contracts.{x}.description", f"Contract {c.get('name', x + 1)} — description", "text",
            "Contracts", c.get("description") or "")
    for i, a in enumerate(design.get("affected_applications") or []):
        add(f"affected_applications.{i}.role_in_change", f"{a.get('name')} — role in the change", "text", "Impact", a.get("role_in_change") or "")
        add(f"affected_applications.{i}.reason", f"{a.get('name')} — why it changes", "text", "Impact", a.get("reason") or "")
    for i, a in enumerate(design.get("new_applications_needed") or []):
        add(f"new_applications_needed.{i}.purpose", f"{a.get('name')} — purpose", "text", "Impact", a.get("purpose") or "")
        add(f"new_applications_needed.{i}.justification", f"{a.get('name')} — why it is new", "text", "Impact", a.get("justification") or "")
    for i, a in enumerate(design.get("unaffected_applications_considered") or []):
        add(f"unaffected_applications_considered.{i}.reason", f"{a.get('name')} — why it is unaffected", "text", "Impact", a.get("reason") or "")
    add("test_strategy", "Test strategy", "text", "Tests and risks", design.get("test_strategy") or "")
    for i, r in enumerate(design.get("architecture_risks") or []):
        add(f"architecture_risks.{i}", f"Risk {i + 1}", "text", "Tests and risks", r if isinstance(r, str) else str(r))
    return out


def _walk(root: Any, parts: List[str]) -> Tuple[Any, str]:
    """The container and key/index that `parts` leads to inside `root`."""
    for p in parts[:-1]:
        root = root[int(p)] if isinstance(root, list) else root[p]
    last = parts[-1]
    return root, (int(last) if isinstance(root, list) else last)


def effective(design: Dict[str, Any], requirement: Optional[Dict[str, Any]] = None,
              edits: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], str]:
    """The design with the user's wording changes applied (the original is untouched),
    and the document title. Raises ValueError if an edit points at something that cannot be
    changed, or empties something that must have text."""
    edits = edits or {}
    title = default_title(requirement)
    fields = {f["path"]: f for f in _fields_of(design, title)}
    unknown = sorted(set(edits) - set(fields))
    if unknown:
        raise ValueError("These cannot be edited: " + ", ".join(unknown))
    result = copy.deepcopy(design)
    problems = []
    for path, value in edits.items():
        field = fields[path]
        if field["kind"] == "lines":
            value = [t for t in (_line(v) for v in (value if isinstance(value, list) else str(value).splitlines())) if t]
        else:
            value = _clean(value) if field["kind"] == "text" else _line(value)
        if field["required"] and not value:
            problems.append(field["label"])
            continue
        if path == "doc.title":
            title = value
        else:
            container, key = _walk(result, path.split("."))
            container[key] = value
    if problems:
        raise ValueError("These need some text: " + ", ".join(problems))
    return result, title


def editable_fields(design: Dict[str, Any], requirement: Optional[Dict[str, Any]] = None,
                    edits: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The wording that can be changed, showing the current (edited) text of each."""
    eff, title = effective(design, requirement, edits)
    return _fields_of(eff, title)


# ------------------------------------------------------------
# One description of the document
# ------------------------------------------------------------

def _status_cell(status: str) -> Dict[str, str]:
    return {"status": status if status in STATUS_LABEL else "existing"}


def build_model(requirement: Optional[Dict[str, Any]], design: Dict[str, Any],
                meta: Optional[Dict[str, Any]] = None, edits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The document as plain data: {"title", "meta", "blocks"}. Blocks are one of
    h1 / h2 / p / bullets / numbered / table / image / callout; a table cell is text or
    {"status": ...}. `image` blocks name a diagram (see build_images)."""
    meta = meta or {}
    req = requirement or {}
    d, title = effective(design or {}, req, edits)
    comps, conns, flows = d.get("components") or [], d.get("connections") or [], d.get("flows") or []

    info = [("Date", _line(meta.get("date")) or datetime.now(timezone.utc).strftime("%d %B %Y"))]
    if meta.get("run_id"):
        info.append(("Run", _line(meta["run_id"])))
    mode = {"from_scratch": "Built from scratch", "extend_existing": "Extends an existing system"}.get(req.get("build_mode"))
    if mode:
        info.append(("Type", mode))
    if meta.get("knowledge_base") is not None:
        info.append(("Knowledge base", _line(meta["knowledge_base"]) or "none"))

    blocks: List[Dict[str, Any]] = []
    h1 = lambda t: blocks.append({"type": "h1", "text": t})
    h2 = lambda t: blocks.append({"type": "h2", "text": t})
    p = lambda t, **kw: blocks.append({"type": "p", "text": t, **kw})
    table = lambda header, rows, widths=None: blocks.append({"type": "table", "header": header, "rows": rows, "widths": widths})

    h1("Overview")
    p(_clean(d.get("overview")) or "Not stated.")
    if _clean(req.get("scope_summary")):
        p("Requirement: " + _clean(req["scope_summary"]), style="muted")
    counts = {s: sum(1 for c in comps if c.get("status") == s) for s in STATUS_LABEL}
    if comps:
        p(f"{counts['new']} new, {counts['changed']} changed, {counts['existing']} existing and "
          f"{counts['external']} external components, joined by {len(conns)} connection(s).", style="muted")

    if comps:
        h1("Architecture at a glance")
        blocks.append({"type": "image", "key": "architecture",
                       "caption": "Everything that is built, changed or used, and how it connects. "
                                  "Solid lines are requests and responses; dashed lines are asynchronous events."})
        h1("Components")
        table(["Component", "Type", "Status", "Responsibility", "Technology"],
              [[c["name"], KIND_LABEL.get(c.get("kind"), c.get("kind")), _status_cell(c.get("status")),
                c.get("responsibility") or "", c.get("technology") or ""] for c in comps],
              [1.7, 1.1, 1.15, 3.2, 1.3])
        detail = [[c["name"], "\n".join(c.get("interfaces") or []), "\n".join(c.get("data_owned") or [])]
                  for c in comps if c.get("interfaces") or c.get("data_owned")]
        if detail:
            h2("Interfaces and data")
            table(["Component", "Interfaces", "Data it owns"], detail, [1.8, 3.4, 3.3])

    if conns:
        h1("Interconnections")
        table(["From", "To", "Type", "Protocol", "What passes", "Status"],
              [[k["from"], k["to"], CONNECTION_LABEL.get(k.get("kind"), k.get("kind")), k.get("protocol") or "",
                k["label"] + (f" — {k['description']}" if k.get("description") else ""),
                _status_cell(k.get("status"))] for k in conns],
              [1.65, 1.65, 1.3, 1.5, 2.2, 1.15])
        senders = [c["name"] for c in comps if any(k["from"] == c["name"] for k in conns)]
        receivers = [c["name"] for c in comps if any(k["to"] == c["name"] for k in conns)]
        if 1 < len(senders) and len(receivers) <= 9:
            h2("Who talks to whom")
            rows = []
            for a in senders:
                row = [a]
                for b in receivers:
                    kinds = sorted({CONNECTION_SHORT.get(k.get("kind"), "call") for k in conns if k["from"] == a and k["to"] == b})
                    row.append(", ".join(kinds))
                rows.append(row)
            table(["From \\ To"] + receivers, rows)
            p("call = request/response, event = asynchronous event, data = data access, batch = batch job, UI = user interface call.", style="muted")

    for i, flow in enumerate(flows, start=1):
        if i == 1:
            h1("Key flows")
        h2(flow["name"])
        if flow.get("description"):
            p(flow["description"])
        blocks.append({"type": "image", "key": f"flow-{i}", "caption": f"Flow {i}: {flow['name']}"})
        table(["Step", "From", "To", "Message"],
              [[str(n), s["from"], s["to"], s["message"] + (" (asynchronous)" if s.get("kind") == "async" else " (reply)" if s.get("kind") == "return" else "")]
               for n, s in enumerate(flow["steps"], start=1)], [0.6, 1.8, 1.8, 4.3])

    contracts = d.get("cross_service_contracts") or []
    if contracts:
        h1("Contracts between applications")
        table(["Contract", "What it is", "Owner", "Producer", "Consumers"],
              [[c.get("name", ""), c.get("description", ""), c.get("owning_application", ""), c.get("producer", ""),
                ", ".join(c.get("consumers") or [])] for c in contracts], [1.6, 3.2, 1.3, 1.3, 1.6])

    order = d.get("dependency_order") or []
    if order:
        h1("Build order")
        blocks.append({"type": "image", "key": "build-order", "caption": "The order the applications must be built in."})
        role = {a.get("name"): a.get("role_in_change") for a in d.get("affected_applications") or []}
        role.update({a.get("name"): a.get("purpose") for a in d.get("new_applications_needed") or []})
        blocks.append({"type": "numbered", "items": [(f"{i}.", f"{n}" + (f" — {role[n]}" if role.get(n) else ""))
                                                     for i, n in enumerate(order, start=1)]})

    affected, new_apps, unaffected = (d.get("affected_applications") or [], d.get("new_applications_needed") or [],
                                      d.get("unaffected_applications_considered") or [])
    if affected or new_apps or unaffected:
        h1("Impact on applications")
        if affected:
            h2("Existing applications that change")
            table(["Application", "Role in the change", "Why"], [[a.get("name", ""), a.get("role_in_change", ""), a.get("reason", "")] for a in affected], [1.8, 3.6, 3.1])
        if new_apps:
            h2("New applications")
            table(["Application", "Purpose", "Why a new one"], [[a.get("name", ""), a.get("purpose", ""), a.get("justification", "")] for a in new_apps], [1.8, 3.6, 3.1])
        if unaffected:
            h2("Considered but unaffected")
            table(["Application", "Why it does not change"], [[a.get("name", ""), a.get("reason", "")] for a in unaffected], [2.0, 6.5])

    if _clean(d.get("test_strategy")):
        h1("Test strategy")
        p(_clean(d["test_strategy"]))
    risks = [r if isinstance(r, str) else str(r) for r in d.get("architecture_risks") or []]
    if risks:
        h1("Risks")
        blocks.append({"type": "bullets", "items": [_clean(r) for r in risks if _clean(r)]})
    # (The agent's clarifying questions are asked on screen; they are never part of the document.)
    updates = d.get("knowledge_base_updates") or []
    if updates:
        h1("Knowledge base documentation")
        table(["Application", "Action", "Why"], [[u.get("application", ""), "Create" if u.get("action") == "create" else "Add to", u.get("reason", "")] for u in updates], [2.0, 1.2, 5.3])
    return {"title": title, "meta": info, "blocks": blocks}


def normalise_edits(design: Dict[str, Any], requirement: Optional[Dict[str, Any]],
                    edits: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The edits that really change something: wording typed back to its original is dropped.
    Raises ValueError (like `effective`) if an edit is not allowed."""
    effective(design, requirement, edits)                       # validates
    originals = {f["path"]: f["value"] for f in _fields_of(design, default_title(requirement))}
    kept = {}
    for path, value in (edits or {}).items():
        kind = next(f["kind"] for f in _fields_of(design, default_title(requirement)) if f["path"] == path)
        if kind == "lines":
            value = [t for t in (_line(v) for v in (value if isinstance(value, list) else str(value).splitlines())) if t]
        else:
            value = _clean(value) if kind == "text" else _line(value)
        if value != originals[path]:
            kept[path] = value
    return kept


def attachment_names(design: Dict[str, Any]) -> List[str]:
    """The names of the diagram images a Confluence page for this design carries (worked out
    from the design alone, without drawing anything)."""
    names = []
    if design.get("components"):
        names.append("architecture.png")
    names += [f"flow-{i}.png" for i in range(1, len(design.get("flows") or []) + 1)]
    if design.get("dependency_order"):
        names.append("build-order.png")
    return names


def build_images(design: Dict[str, Any], requirement: Optional[Dict[str, Any]] = None,
                 edits: Optional[Dict[str, Any]] = None) -> Dict[str, bytes]:
    """The diagrams the model refers to, as PNG bytes by key."""
    d, _ = effective(design or {}, requirement, edits)
    comps = d.get("components") or []
    images: Dict[str, bytes] = {}
    if comps:
        images["architecture"] = diagrams.architecture_diagram(comps, d.get("connections") or [])
    for i, flow in enumerate(d.get("flows") or [], start=1):
        images[f"flow-{i}"] = diagrams.flow_diagram(flow, comps)
    if d.get("dependency_order"):
        images["build-order"] = diagrams.build_order_diagram(d["dependency_order"], comps)
    return images


def suggested_filename(requirement: Optional[Dict[str, Any]], fmt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _line((requirement or {}).get("title")).lower()).strip("-")[:50]
    return f"design-{slug or 'document'}.{FORMATS[fmt][1]}"


# ------------------------------------------------------------
# Markdown (plain text view; the app draws the blocks itself so it can show the images)
# ------------------------------------------------------------

def _cell_text(cell: Any, rich: bool = False) -> str:
    if isinstance(cell, dict):
        label = STATUS_LABEL[cell["status"]]
        return f":{STREAMLIT_COLOUR[cell['status']]}[**{label}**]" if rich else label
    return str(cell).replace("|", "\\|").replace("\n", "; " if rich else "<br>")


def table_markdown(block: Dict[str, Any], rich: bool = False) -> str:
    header = "| " + " | ".join(_cell_text(h) for h in block["header"]) + " |"
    rule = "|" + "|".join("---" for _ in block["header"]) + "|"
    rows = ["| " + " | ".join(_cell_text(c, rich) for c in row) + " |" for row in block["rows"]]
    return "\n".join([header, rule] + rows)


def to_markdown(model: Dict[str, Any]) -> str:
    lines = [f"# {model['title']}", "", " · ".join(f"{k}: {v}" for k, v in model["meta"]), ""]
    for b in model["blocks"]:
        t = b["type"]
        if t == "h1":
            lines += [f"## {b['text']}", ""]
        elif t == "h2":
            lines += [f"### {b['text']}", ""]
        elif t == "p":
            lines += [{"bold": f"**{b['text']}**", "muted": f"*{b['text']}*"}.get(b.get("style"), b["text"]), ""]
        elif t == "bullets":
            lines += [f"- {i}" for i in b["items"]] + [""]
        elif t == "numbered":
            lines += [f"{label} {text}" for label, text in b["items"]] + [""]
        elif t == "table":
            lines += [table_markdown(b), ""]
        elif t == "image":
            lines += [f"![{b['caption']}]({b['key']}.png)", f"*{b['caption']}*", ""]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------
# Confluence (storage format)
# ------------------------------------------------------------

def _x(text: Any) -> str:
    return escape(_clean(text)).replace("\n", "<br />")


def _confluence_cell(cell: Any) -> str:
    if isinstance(cell, dict):
        status = cell["status"]
        return ('<ac:structured-macro ac:name="status" ac:schema-version="1">'
                f'<ac:parameter ac:name="colour">{CONFLUENCE_COLOUR[status]}</ac:parameter>'
                f'<ac:parameter ac:name="title">{STATUS_LABEL[status].upper()}</ac:parameter>'
                '</ac:structured-macro>')
    return _x(cell)


def to_confluence(model: Dict[str, Any], image_files: Dict[str, str]) -> str:
    """The document as Confluence "storage format" (the XHTML Confluence stores pages in).
    `image_files` maps a diagram key to the file name it is attached to the page under."""
    out = [
        '<ac:structured-macro ac:name="info" ac:schema-version="1"><ac:rich-text-body>'
        f"<p>{_x(' · '.join(f'{k}: {v}' for k, v in model['meta']))}</p>"
        "</ac:rich-text-body></ac:structured-macro>",
        '<ac:structured-macro ac:name="toc" ac:schema-version="1">'
        '<ac:parameter ac:name="maxLevel">2</ac:parameter></ac:structured-macro>',
    ]
    for b in model["blocks"]:
        t = b["type"]
        if t in ("h1", "h2"):
            out.append(f"<{'h2' if t == 'h1' else 'h3'}>{_x(b['text'])}</{'h2' if t == 'h1' else 'h3'}>")
        elif t == "p":
            text = _x(b["text"])
            out.append({"bold": f"<p><strong>{text}</strong></p>", "muted": f"<p><em>{text}</em></p>"}.get(b.get("style"), f"<p>{text}</p>"))
        elif t == "bullets":
            out.append("<ul>" + "".join(f"<li>{_x(i)}</li>" for i in b["items"]) + "</ul>")
        elif t == "numbered":
            out.append("<ol>" + "".join(f"<li>{_x(text)}</li>" for _, text in b["items"]) + "</ol>")
        elif t == "table":
            head = "".join(f"<th>{_x(h)}</th>" for h in b["header"])
            rows = "".join("<tr>" + "".join(f"<td>{_confluence_cell(c)}</td>" for c in row) + "</tr>" for row in b["rows"])
            out.append(f"<table><tbody><tr>{head}</tr>{rows}</tbody></table>")
        elif t == "image" and b["key"] in image_files:
            out.append(f'<p><ac:image ac:width="960" ac:alt="{escape(b["caption"], {chr(34): "&quot;"})}">'
                       f'<ri:attachment ri:filename="{escape(image_files[b["key"]], {chr(34): "&quot;"})}" /></ac:image></p>'
                       f"<p><em>{_x(b['caption'])}</em></p>")
    return "\n".join(out)


def confluence_package(requirement: Optional[Dict[str, Any]], design: Dict[str, Any],
                       meta: Optional[Dict[str, Any]] = None, edits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything needed to publish: {"title", "body" (storage format), "attachments" {file name: PNG bytes}}."""
    model = build_model(requirement, design, meta, edits)
    images = build_images(design, requirement, edits)
    files = {key: f"{key}.png" for key in images}
    return {"title": model["title"], "body": to_confluence(model, files),
            "attachments": {files[k]: v for k, v in images.items()}}


# ------------------------------------------------------------
# Word (.docx)
# ------------------------------------------------------------

def _fit_inches(png: bytes, max_w: float, max_h: float) -> Tuple[float, float]:
    from PIL import Image
    w, h = Image.open(BytesIO(png)).size
    scale = min(max_w / w, max_h / h)
    return w * scale, h * scale


def to_docx(model: Dict[str, Any], images: Dict[str, bytes]) -> bytes:
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt, RGBColor
    except ImportError as exc:
        raise MissingLibrary("Word export needs python-docx. Install it with: pip install python-docx") from exc

    doc = Document()
    doc.core_properties.title = model["title"]
    doc.core_properties.author = "Blueprint"
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)
    doc.add_heading(model["title"], level=0)
    meta = doc.add_paragraph().add_run("  ·  ".join(f"{k}: {v}" for k, v in model["meta"]))
    meta.font.size, meta.font.color.rgb = Pt(9), RGBColor(0x6B, 0x72, 0x80)

    def shade(cell, hex_fill):
        pr = cell._tc.get_or_add_tcPr()
        el = OxmlElement("w:shd")
        el.set(qn("w:val"), "clear")
        el.set(qn("w:color"), "auto")
        el.set(qn("w:fill"), hex_fill)
        pr.append(el)

    for b in model["blocks"]:
        t = b["type"]
        if t in ("h1", "h2"):
            doc.add_heading(b["text"], level=1 if t == "h1" else 2)
        elif t == "p":
            run = doc.add_paragraph().add_run(b["text"])
            if b.get("style") == "bold":
                run.bold = True
            elif b.get("style") == "muted":
                run.italic, run.font.size, run.font.color.rgb = True, Pt(9.5), RGBColor(0x6B, 0x72, 0x80)
        elif t == "bullets":
            for item in b["items"]:
                doc.add_paragraph(item, style="List Bullet")
        elif t == "numbered":
            for label, text in b["items"]:
                para = doc.add_paragraph()
                para.paragraph_format.left_indent, para.paragraph_format.first_line_indent = Pt(28), Pt(-28)
                para.add_run(f"{label}\t").bold = True
                para.add_run(text)
        elif t == "table":
            grid = doc.add_table(rows=1, cols=len(b["header"]))
            grid.style = "Table Grid"
            for i, h in enumerate(b["header"]):
                cell = grid.rows[0].cells[i]
                cell.text = ""
                run = cell.paragraphs[0].add_run(str(h))
                run.bold, run.font.size = True, Pt(9)
                shade(cell, "E8ECF7")
            for row in b["rows"]:
                cells = grid.add_row().cells
                for i, value in enumerate(row):
                    cells[i].text = ""
                    para = cells[i].paragraphs[0]
                    if isinstance(value, dict):
                        run = para.add_run(STATUS_LABEL[value["status"]].upper())
                        run.bold = True
                        run.font.color.rgb = RGBColor.from_string(HEX_COLOUR[value["status"]])
                    else:
                        run = para.add_run(str(value))
                    run.font.size = Pt(9)
            doc.add_paragraph()
        elif t == "image" and b["key"] in images:
            w, h = _fit_inches(images[b["key"]], 6.4, 8.2)
            doc.add_picture(BytesIO(images[b["key"]]), width=Inches(w), height=Inches(h))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap = doc.add_paragraph().add_run(b["caption"])
            cap.italic, cap.font.size = True, Pt(9)

    def field(paragraph, instruction):
        run = paragraph.add_run()
        for kind, text in (("begin", None), (None, instruction), ("end", None)):
            if kind:
                el = OxmlElement("w:fldChar")
                el.set(qn("w:fldCharType"), kind)
            else:
                el = OxmlElement("w:instrText")
                el.set(qn("xml:space"), "preserve")
                el.text = text
            run._r.append(el)
        run.font.size = Pt(9)

    footer = doc.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("Generated by Blueprint  ·  Page ").font.size = Pt(9)
    field(footer, "PAGE")
    footer.add_run(" of ").font.size = Pt(9)
    field(footer, "NUMPAGES")
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------
# PDF
# ------------------------------------------------------------

def to_pdf(model: Dict[str, Any], images: Dict[str, bytes]) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise MissingLibrary("PDF export needs reportlab. Install it with: pip install reportlab") from exc
    from tools.requirement_doc import _pdf_text, _register_pdf_fonts

    glyphs = _register_pdf_fonts()
    t = lambda value: _pdf_text(str(value), glyphs)
    ink, grey, accent = colors.HexColor("#1B1F2A"), colors.HexColor("#6B7280"), colors.HexColor("#4F46E5")
    body = ParagraphStyle("body", fontName="Vera", fontSize=9.5, leading=13.5, textColor=ink, spaceAfter=4)
    title = ParagraphStyle("title", parent=body, fontName="Vera-Bold", fontSize=21, leading=26, spaceAfter=4)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=12, textColor=grey, spaceAfter=8)
    h1 = ParagraphStyle("h1", parent=body, fontName="Vera-Bold", fontSize=13.5, leading=17, textColor=accent, spaceBefore=12, spaceAfter=5, keepWithNext=1)
    h2 = ParagraphStyle("h2", parent=body, fontName="Vera-Bold", fontSize=11, leading=15, spaceBefore=8, spaceAfter=3, keepWithNext=1)
    bold = ParagraphStyle("bold", parent=body, fontName="Vera-Bold", spaceBefore=4)
    muted = ParagraphStyle("muted", parent=body, fontName="Vera-Italic", fontSize=8.5, textColor=grey)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=18, bulletIndent=6)
    item = ParagraphStyle("item", parent=body, leftIndent=24, bulletIndent=2, bulletFontName="Vera-Bold")
    cell_style = ParagraphStyle("cell", parent=body, fontSize=8, leading=10.5, spaceAfter=0)
    head_style = ParagraphStyle("head", parent=cell_style, fontName="Vera-Bold")
    frame_w = A4[0] - 4.2 * cm

    story: List[Any] = [Paragraph(t(model["title"]), title),
                        Paragraph(t("  ·  ".join(f"{k}: {v}" for k, v in model["meta"])), small)]
    for b in model["blocks"]:
        kind = b["type"]
        if kind == "h1":
            story.append(Paragraph(t(b["text"]), h1))
        elif kind == "h2":
            story.append(Paragraph(t(b["text"]), h2))
        elif kind == "p":
            story.append(Paragraph(t(b["text"]), {"bold": bold, "muted": muted}.get(b.get("style"), body)))
        elif kind == "bullets":
            story += [Paragraph(t(i), bullet, bulletText="•") for i in b["items"]]
        elif kind == "numbered":
            story += [Paragraph(t(text), item, bulletText=label) for label, text in b["items"]]
        elif kind == "table":
            widths = b.get("widths") or [1] * len(b["header"])
            col_w = [frame_w * w / sum(widths) for w in widths]
            data = [[Paragraph(t(h), head_style) for h in b["header"]]]
            for row in b["rows"]:
                cells = []
                for value in row:
                    if isinstance(value, dict):
                        cells.append(Paragraph(f'<font color="#{HEX_COLOUR[value["status"]]}"><b>{STATUS_LABEL[value["status"]].upper()}</b></font>', cell_style))
                    else:
                        cells.append(Paragraph(t(value), cell_style))
                data.append(cells)
            grid = Table(data, colWidths=col_w, repeatRows=1)
            grid.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8ECF7")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD2E0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story += [grid, Spacer(1, 8)]
        elif kind == "image" and b["key"] in images:
            from PIL import Image as PILImage
            w, h = PILImage.open(BytesIO(images[b["key"]])).size
            scale = min(frame_w / w, 22 * cm / h)
            story.append(KeepTogether([Image(BytesIO(images[b["key"]]), width=w * scale, height=h * scale),
                                       Paragraph(t(b["caption"]), muted), Spacer(1, 6)]))

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Vera", 8)
        canvas.setFillColor(grey)
        canvas.drawCentredString(A4[0] / 2, 1.1 * cm, f"Generated by Blueprint  ·  Page {doc.page}")
        canvas.restoreState()

    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, title=model["title"], author="Blueprint", leftMargin=2.1 * cm,
                      rightMargin=2.1 * cm, topMargin=2 * cm, bottomMargin=2 * cm
                      ).build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def build(requirement: Optional[Dict[str, Any]], design: Dict[str, Any], meta: Optional[Dict[str, Any]],
          edits: Optional[Dict[str, Any]], fmt: str) -> Tuple[bytes, str, str]:
    """Make the design document in `fmt` ("docx" or "pdf"): (file bytes, suggested file name, MIME type)."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format {fmt!r}; choose one of {sorted(FORMATS)}")
    # Check the library for this format FIRST, so the message names the right package.
    import importlib
    module, package = ("docx", "python-docx") if fmt == "docx" else ("reportlab", "reportlab")
    try:
        importlib.import_module(module)
    except ImportError as exc:
        raise MissingLibrary(f"{'Word' if fmt == 'docx' else 'PDF'} export needs {package}. "
                             f"Install it with: pip install {package}") from exc
    model = build_model(requirement, design, meta, edits)
    images = build_images(design, requirement, edits)
    data = to_docx(model, images) if fmt == "docx" else to_pdf(model, images)
    return data, suggested_filename(requirement, fmt), FORMATS[fmt][2]
