"""Presents a knowledge base written by repo_intel to the SDLC pipeline (knowledge_base/loader.py).

The pipeline reads a knowledge base as ONE flat document per application (`**Repo:**`, `**Purpose:**`, `## Depends on`...).
repo_intel writes a nested, evidence-labelled folder instead. This adapter builds the flat view FROM those pages at load
time (nothing extra is stored, so it can never drift from them):

  * one document per component page (`applications/<x>/overview.md`), with its dependencies as `## Depends on`
  * one `<repo>-system` document with purpose, stack, decisions, configuration and open questions
  * documents the pipeline itself saved at the top level (user-approved design updates) are appended to the
    matching component document, or kept as their own application when they match none.

Evidence labels such as [Verified] / [Inferred] / [Unknown] are kept, so agents can see how much to trust each line.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import layout
from .analyze import ROOT
from .generate import slug

SECTION_LIMIT = 7000
Doc = Tuple[str, Path, str, Optional[str], Optional[str]]     # name, path, content, repo_url, purpose


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _section(text: str, heading: str) -> str:
    m = re.search(rf"(?ms)^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)", text)
    return m.group(1).strip() if m else ""


def _strip_notes(text: str) -> str:
    return re.sub(r"<!-- manual:(?:begin|end) \w+ -->\n?", "", text).strip()


def _trim(text: str, limit: int = SECTION_LIMIT) -> str:
    text = _strip_notes(text)
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…(truncated; full page in the knowledge base folder)"


def _first_sentence(s: str, n: int = 280) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def is_generated(kb_dir: Path) -> bool:
    return layout.is_generated_kb(kb_dir)


def application_docs(kb_dir: Path) -> List[Doc]:
    index = {}
    try:
        index = json.loads((kb_dir / layout.MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    prov = index.get("provenance", {})
    url = layout.normalize_source(prov["repository_url"]) if prov.get("repository_url") else None
    repo = layout.repo_name(url) if url else kb_dir.name
    files = index.get("files", {})
    # component key (as the analyser named it) for each page slug
    comp_by_slug = {slug(c): c for c in {f["component"] for f in files.values() if "component" in f}}
    name_of = lambda s: (f"{repo}-root" if comp_by_slug.get(s) == ROOT else comp_by_slug.get(s, s))

    stack = _read(kb_dir / "repository-overview" / "technology-stack.md")
    frameworks = [l[2:].strip() for l in _section(stack, "Frameworks and notable libraries (from dependencies)").splitlines() if l.startswith("- ")]
    commit = (prov.get("commit") or "")[:12]

    docs: List[Doc] = []
    pages = sorted((kb_dir / "applications").glob("*/overview.md")) if (kb_dir / "applications").is_dir() else []
    # who uses whom, taken from the "Depends on" lines of each page
    deps: Dict[str, List[str]] = {}
    used_by: Dict[str, List[str]] = {}
    for p in pages:
        me = name_of(p.parent.name)
        for m in re.finditer(r"^- Depends on (?:\[[^\]]*\]\(\.\./([^/]+)/overview\.md\)|([^\n—]+?)) — ", _read(p), re.M):
            other = name_of(m.group(1)) if m.group(1) else m.group(2).strip()
            deps.setdefault(me, []).append(other)
            used_by.setdefault(other, []).append(me)

    for p in pages:
        s, text = p.parent.name, _read(p)
        name = name_of(s)
        langs = sorted({f["language"] for f in files.values() if f.get("component") == comp_by_slug.get(s) and f.get("language")})
        ptxt = _section(text, "Purpose").strip()
        if ptxt.startswith("`[Inferred]`"):
            purpose = _first_sentence(ptxt.replace("`[Inferred]`", "").strip()) + " (inferred)"
        elif ptxt.startswith("`[Verified]`"):
            purpose = _first_sentence(ptxt.split("):", 1)[-1].strip()) + " (from module docstring)"
        else:
            purpose = None
        head = [f"# {name}", ""]
        if url:
            head.append(f"- **Repo:** `{url}`")
        if langs or frameworks:
            head.append(f"- **Tech stack:** {', '.join(langs + frameworks)}")
        if purpose:
            head.append(f"- **Purpose:** {purpose}")
        head.append(f"- **Source of this document:** generated from the code at commit `{commit}` by repo_intel; "
                    f"labels such as [Verified] / [Inferred] / [Unknown] state how well each line is supported.")
        body = ["", "## Depends on (calls)", ""] + ([f"- `{d}` (code import)" for d in sorted(set(deps.get(name, [])))] or ["None found in the code."])
        if used_by.get(name):
            body += ["", "## Used by", ""] + [f"- `{d}`" for d in sorted(set(used_by[name]))]
        detail = re.sub(r"\A# .*\n", "", _strip_notes(text)).strip()
        content = "\n".join(head + body + ["", "## Details from code analysis", "", detail]) + "\n"
        docs.append((name, p, content, url, purpose))

    sysdoc = _system_doc(kb_dir, repo, url, commit, frameworks, files, [d[0] for d in docs])
    if sysdoc:
        docs.append(sysdoc)
    return _merge_pipeline_docs(kb_dir, docs, url)


def _system_doc(kb_dir, repo, url, commit, frameworks, files, comp_names) -> Optional[Doc]:
    purpose_page = _read(kb_dir / "repository-overview" / "system-purpose.md")
    quote = re.search(r"^> (.+)$", purpose_page, re.M)
    synth = re.search(r"`\[Inferred\]` (.+)", _section(purpose_page, "What the system does"))
    purpose = (_first_sentence(synth.group(1)) + " (inferred)") if synth else (_first_sentence(quote.group(1)) + " (from README)") if quote else None
    langs = sorted({f["language"] for f in files.values() if f.get("language")})
    parts = [f"# {repo}-system", ""]
    if url:
        parts.append(f"- **Repo:** `{url}`")
    parts.append(f"- **Tech stack:** {', '.join(langs + frameworks) or 'unknown'}")
    if purpose:
        parts.append(f"- **Purpose:** {purpose}")
    parts += [f"- **Source of this document:** repo_intel knowledge base for commit `{commit}`.", "",
              "Whole-repository context: the components below are documented as separate applications.", ""]
    if comp_names:
        parts += ["## Components", ""] + [f"- {c}" for c in sorted(comp_names)] + [""]
    added = False
    for title, rel_path in (("Architecture", "architecture/architecture-overview.md"),
                            ("Integration boundaries", "architecture/integration-boundaries.md"),
                            ("API contracts", "data-and-contracts/api-contracts.md"),
                            ("Dependency map", "architecture/dependency-map.md"),
                            ("Decisions and rationale", "decisions-and-rationale/verified-decisions.md"),
                            ("Configuration", "operations/configuration.md"),
                            ("Build and test", "operations/build-and-test.md"),
                            ("Technical debt signals", "quality-and-reliability/technical-debt.md"),
                            ("Unresolved questions", "decisions-and-rationale/unresolved-questions.md")):
        t = _read(kb_dir / rel_path)
        if t:
            parts += [f"## {title}", "", re.sub(r"\A# .*\n", "", _trim(t)).strip(), ""]
            added = True
    if not added and not comp_names:
        return None
    return (f"{repo}-system", kb_dir / "README.md", "\n".join(parts), url, purpose)


def _merge_pipeline_docs(kb_dir: Path, docs: List[Doc], url) -> List[Doc]:
    """Top-level *.md files were saved by the SDLC pipeline (approved design updates), not by repo_intel."""
    by_slug = {slug(d[0]): i for i, d in enumerate(docs)}
    for p in sorted(kb_dir.glob("*.md")):
        if p.name == "README.md" or p.name.endswith(".proposed.md"):
            continue
        text = _read(p)
        i = by_slug.get(p.stem)
        if i is not None:
            n, path, c, u, pur = docs[i]
            docs[i] = (n, path, c + "\n\n## Updates saved by the SDLC pipeline\n\n" + text.strip() + "\n", u, pur)
        else:
            m = re.search(r"\*\*Repo:\*\*\s*`?([^\n`]+)`?", text)
            pm = re.search(r"\*\*Purpose:\*\*\s*(.+)", text)
            docs.append((p.stem, p, text, m.group(1).strip() if m else None, pm.group(1).strip() if pm else None))
    return docs
