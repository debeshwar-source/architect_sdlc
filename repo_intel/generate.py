"""Document builders. Each takes the analysed Model and returns Markdown (or None when there is nothing to say).

Evidence labels used everywhere (see README legend):
  [Verified]  read directly from source/config      [Documented]  stated in an ADR/README/commit body
  [Inferred]  interpretation, not stated            [Recommendation]  proposed, not current behaviour
  [Unknown]   cannot be established from the repository
Source references look like `path/file.py:L10-20` and are checked by validate.py.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Dict, List, Optional

from . import history as hist
from .analyze import Dependency, FileAnalysis, ROOT
from .discover import FileInfo
from .security import redact

LEGEND = ("**Evidence labels:** `[Verified]` read from source · `[Documented]` stated in an ADR/README/commit · "
          "`[Inferred]` interpretation · `[Recommendation]` proposed · `[Unknown]` not established.")
MANUAL_NOTES = "<!-- manual:begin notes -->\n<!-- manual:end notes -->"
SENSITIVE = re.compile(r"(?i)(auth|login|passw|secret|token|crypto|permission|acl|oauth|jwt|session|security)")


@dataclass
class Model:
    provenance: dict
    repo_name: str
    files: List[FileInfo]
    analyses: Dict[str, FileAnalysis]
    deps: List[Dependency]
    stack: dict
    comps: dict
    commits: List[dict]
    adrs: List[dict]
    readme: Optional[tuple] = None          # (path, text)
    scripts: Dict[str, dict] = field(default_factory=dict)   # manifest path -> {name: command}
    makefile_targets: Dict[str, List[str]] = field(default_factory=dict)
    enrichment: dict = field(default_factory=dict)           # optional validated LLM output
    commit_url: Optional[str] = None         # format string with {sha}
    documented: set = field(default_factory=set)   # components that get their own page
    endpoints: List[dict] = field(default_factory=list)       # resolved HTTP routes
    externals: List[dict] = field(default_factory=list)       # detected external systems
    external_details: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def paths(self):
        return {f.path for f in self.files}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "root"


def comp_title(m: Model, c: str) -> str:
    return m.repo_name + " (root files)" if c == ROOT else c


def comp_doc(c: str) -> str:
    return f"applications/{slug(c)}/overview.md"


def rel(from_doc: str, to_doc: str) -> str:
    a, b = PurePosixPath(from_doc).parent.parts, PurePosixPath(to_doc).parts
    i = 0
    while i < len(a) and i < len(b) - 1 and a[i] == b[i]:
        i += 1
    return "/".join([".."] * (len(a) - i) + list(b[i:]))


def ref(path: str, a: int = 0, b: int = 0) -> str:
    return f"`{path}" + (f":L{a}" + (f"-{b}" if b and b != a else "") if a else "") + "`"


def table(headers, rows) -> str:
    if not rows:
        return ""
    esc = lambda x: str(x).replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
                     + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]) + "\n"


def commit_ref(m: Model, sha: str) -> str:
    return f"[`{sha[:8]}`]({m.commit_url.format(sha=sha)})" if m.commit_url else f"`{sha[:8]}`"


def _sorted_comps(m: Model):
    return sorted(m.comps["components"].items(), key=lambda kv: -(len(kv[1]["files"]) + len(kv[1]["tests"])))


def comp_lines(m: Model, c: str) -> int:
    return sum(m.analyses[p].lines for p in m.comps["components"][c]["files"] if p in m.analyses)


# ---------------------------------------------------------------------------------------------------------------------

def system_purpose(m: Model) -> str:
    out = ["# System purpose", "", LEGEND, ""]
    if m.readme:
        path, text = m.readme
        paras = [p.strip() for p in re.split(r"\n\s*\n", re.sub(r"(?m)^#.*$|^!\[.*$|^\[!\[.*$", "", text)) if len(p.strip()) > 40]
        if paras:
            out += ["## Stated purpose", "", "`[Documented]` Quoted from " + ref(path) + ":", "",
                    "> " + redact(" ".join(paras[0].split())[:600]), ""]
    else:
        out += ["## Stated purpose", "", "`[Unknown]` The repository has no README; purpose cannot be established from documentation.", ""]
    sysv = m.enrichment.get("system", {})
    if sysv.get("purpose"):
        out += ["## What the system does", "", f"`[Inferred]` {sysv['purpose']}", "",
                f"How it is built: [architecture overview]({rel('repository-overview/system-purpose.md', 'architecture/architecture-overview.md')})", ""]
    langs = ", ".join(f"{l['language']} ({l['share']}%)" for l in m.stack["languages"][:4]) or "none detected"
    out += ["## Shape of the repository", "",
            f"- `[Verified]` {len(m.files)} tracked files; languages: {langs}.",
            f"- `[Verified]` {len(m.comps['components'])} component(s) detected — see [component catalog]({rel('repository-overview/system-purpose.md', 'architecture/component-catalog.md')}).",
            f"- Provenance: commit `{m.provenance['commit'][:12]}`.", "",
            MANUAL_NOTES, ""]
    return "\n".join(out)


def technology_stack(m: Model) -> str:
    s, d = m.stack, "repository-overview/technology-stack.md"
    out = ["# Technology stack", "", "All entries `[Verified]` from file extensions and manifests.", "",
           "## Languages", "", table(["Language", "Bytes", "Share"], [(l["language"], l["bytes"], f"{l['share']}%") for l in s["languages"]])]
    for title, key in (("Build systems", "build_systems"), ("Package managers / ecosystems", "package_managers"),
                       ("Frameworks and notable libraries (from dependencies)", "frameworks")):
        if s[key]:
            out += [f"## {title}", ""] + [f"- {x}" for x in s[key]] + [""]
    for title, key in (("CI/CD", "ci"), ("Infrastructure definitions", "infra"), ("API specifications", "api_specs")):
        if s[key]:
            out += [f"## {title}", ""] + [f"- {ref(x)}" for x in s[key]] + [""]
    out += [f"Full dependency list: [external dependencies]({rel(d, 'dependency-catalog/external-dependencies.md')}).", "", MANUAL_NOTES, ""]
    return "\n".join(out)


def repository_map(m: Model) -> str:
    byd = defaultdict(Counter)
    for f in m.files:
        top = f.path.split("/")[0] if "/" in f.path else "(root)"
        byd[top][f.kind] += 1
    rows = [(f"`{t}`", sum(c.values()), ", ".join(f"{k} {v}" for k, v in c.most_common())) for t, c in sorted(byd.items())]
    skipped = Counter(f.skip_reason for f in m.files if not f.analyze)
    docs = [f.path for f in m.files if f.kind == "doc"]
    out = ["# Repository map", "", "`[Verified]` File counts by top-level path and kind.", "",
           table(["Path", "Files", "Kinds"], rows)]
    if docs:
        out += ["## Existing documentation", ""] + [f"- {ref(p)}" for p in docs[:40]] + [""]
    if skipped:
        out += ["## Files listed but not analysed", ""] + [f"- {n} × {r}" for r, n in skipped.most_common()] + [""]
    out += ["## Components", "", f"See [component catalog]({rel('repository-overview/repository-map.md', 'architecture/component-catalog.md')}).", "", MANUAL_NOTES, ""]
    return "\n".join(out)


def component_catalog(m: Model) -> str:
    d, rows = "architecture/component-catalog.md", []
    for c, v in _sorted_comps(m):
        if not v["files"]:
            continue
        mods = [m.analyses[p].module_doc for p in v["files"] if p in m.analyses and m.analyses[p].module_doc]
        llm = m.enrichment.get("components", {}).get(c, {}).get("purpose")
        rows.append((f"[{comp_title(m, c)}]({rel(d, comp_doc(c))})" if c in m.documented else comp_title(m, c),
                     len(v["files"]), comp_lines(m, c), len(m.comps["tested_by"].get(c, [])) or "none",
                     (f"`[Inferred]` {llm[:160]}" if llm else mods[0][:110] if mods else "`[Unknown]`")))
    return "\n".join(["# Component catalog", "",
                      "`[Verified]` Components are nested-manifest directories, else top-level folders. "
                      "Purpose is an LLM summary (`[Inferred]`, evidence on the component page) when available, else the first module docstring.", "",
                      table(["Component", "Source files", "Lines", "Test files referencing it", "Purpose"], rows),
                      f"See also: [dependency map]({rel(d, 'architecture/dependency-map.md')}).", "", MANUAL_NOTES, ""])


def dependency_map(m: Model) -> Optional[str]:
    edges = m.comps["edges"]
    if not edges:
        return None
    node = lambda c: "n_" + slug(c).replace("-", "_")
    lines = ["graph LR"] + [f'  {node(c)}["{comp_title(m, c)}"]' for c in sorted({e[k] for e in edges for k in ("from", "to")})]
    lines += [f"  {node(e['from'])} --> {node(e['to'])}" for e in edges]
    rows = [(comp_title(m, e["from"]), comp_title(m, e["to"]), e["count"], ", ".join(ref(p) for p in e["evidence"][:3])) for e in edges]
    return "\n".join(["# Internal dependency map", "",
                      "`[Verified]` An edge A → B means files in A import code located in B (static imports only; dynamic imports are not seen).", "",
                      "```mermaid", *lines, "```", "", table(["From", "To", "Importing files", "Evidence"], rows), MANUAL_NOTES, ""])


def evs(items) -> str:
    return ", ".join(ref(e["file"], e["start"], e["end"]) for e in items)


def _bul(items, key="text") -> List[str]:
    return [f"- {i[key]} — {evs(i['evidence'])}" for i in items]


def _purpose(m: Model, c: str, e: dict) -> str:
    if e.get("purpose"):
        return f"`[Inferred]` {e['purpose']}"
    docs = [(p, a.module_doc) for p in m.comps["components"][c]["files"] if p in m.analyses and (a := m.analyses[p]).module_doc]
    if docs:
        return f"`[Verified]` From a module docstring ({ref(docs[0][0])}): {docs[0][1]}"
    return "`[Unknown]` No docstring states this component's purpose, and no LLM summary was generated (run with AI summaries on)."


def component_overview(m: Model, c: str) -> str:
    v, d = m.comps["components"][c], comp_doc(c)
    fa = [m.analyses[p] for p in v["files"] if p in m.analyses]
    e = m.enrichment.get("components", {}).get(c, {})
    out = [f"# {comp_title(m, c)}", "", f"`[Verified]` {len(v['files'])} source file(s), {sum(a.lines for a in fa)} lines, "
           f"{len(v['tests'])} test file(s). Parent: [component catalog]({rel(d, 'architecture/component-catalog.md')}).", "",
           "## Purpose", "", _purpose(m, c, e), ""]
    if e.get("responsibilities"):
        out += ["## Responsibilities", "", "`[Inferred]` each item cites the lines it was derived from.", ""] + _bul(e["responsibilities"]) + [""]
    for w in e.get("workflows", []):
        out += [f"## Workflow: {w['name']}", ""]
        if w["trigger"]:
            out += [f"**Trigger:** {w['trigger']}", ""]
        out += [f"{i}. {s['text']} — {evs(s['evidence'])}" for i, s in enumerate(w["steps"], 1)]
        if w["on_failure"]:
            out += ["", f"**On failure:** {w['on_failure']}"]
        out.append("")
    if e.get("business_rules"):
        out += ["## Business rules enforced here", ""] + _bul(e["business_rules"]) + [""]
    if e.get("gotchas"):
        out += ["## Know before you change this", ""] + _bul(e["gotchas"]) + [""]
    if e.get("decisions"):
        out += ["## Design decisions", "", f"Details, confidence and trade-offs: [decisions]({rel(d, 'decisions-and-rationale/verified-decisions.md')}).", ""] \
               + [f"- {x['decision']} (`{x['basis']}`)" for x in e["decisions"]] + [""]
    if e.get("risks"):
        out += ["## Risks seen in the code", ""] + _bul(e["risks"]) + [""]
    eps = [x for x in m.endpoints if x["file"] in v["files"]]
    if eps:
        out += ["## HTTP endpoints", "", table(["Method", "Path", "Handler", "Request / response", "Mounted"],
               [("/".join(x["methods"]), f"`{x['path']}`", ref(x["file"], x["line"], x["end_line"]) + f" `{x['handler']}`",
                 ", ".join(x["params"][:2] + ([f"→ {x['response_model']}"] if x["response_model"] else [])) or "—",
                 "yes" if x["mounted"] else "**no** — never included in an app") for x in eps]),
                f"All endpoints: [API contracts]({rel(d, 'data-and-contracts/api-contracts.md')})", ""]
    mods = [(p, md) for p in v["files"] if p in m.analyses for md in m.analyses[p].models]
    if mods:
        out += ["## Data models defined here", "", table(["Model", "Kind", "Table", "Location"],
               [(f"`{md['name']}`", md["kind"], f"`{md['table']}`" if md["table"] else "—", ref(p, md["line"], md["end_line"])) for p, md in mods[:25]]),
                f"Fields: [data models]({rel(d, 'data-and-contracts/data-models.md')})", ""]
    ext = [s_ for s_ in m.externals if c in s_["components"]]
    if ext:
        out += ["## External systems used", ""] + [
            f"- **{s_['system']}** ({s_['category']}) — " + ", ".join(ref(u["file"], u["line"]) for u in s_["uses"] if u["file"] in v["files"] or u["file"] in v["tests"])[:200]
            for s_ in ext] + [f"", f"See [integration boundaries]({rel(d, 'architecture/integration-boundaries.md')}).", ""]
    deps_out = [x for x in m.comps["edges"] if x["from"] == c]
    deps_in = [x for x in m.comps["edges"] if x["to"] == c]
    if deps_out or deps_in:
        out += ["## Relationships", "", "`[Verified]` from static imports.", ""]
        out += [f"- Depends on [{comp_title(m, x['to'])}]({rel(d, comp_doc(x['to']))}) — {ref(x['evidence'][0])}" if x["to"] in m.documented
                else f"- Depends on {comp_title(m, x['to'])} — {ref(x['evidence'][0])}" for x in deps_out]
        out += [f"- Used by [{comp_title(m, x['from'])}]({rel(d, comp_doc(x['from']))}) — {ref(x['evidence'][0])}" if x["from"] in m.documented
                else f"- Used by {comp_title(m, x['from'])} — {ref(x['evidence'][0])}" for x in deps_in] + [""]
    env = [(x["name"], p, x["line"], x.get("default")) for p in v["files"] if p in m.analyses for x in m.analyses[p].env_vars]
    if env:
        out += ["## Configuration read from the environment", "", table(["Variable", "Default in code", "Read at"],
               [(f"`{n}`", f"`{dflt}`" if dflt else "none / required", ref(p, ln)) for n, p, ln, dflt in env[:25]]),
                f"Catalog: [configuration]({rel(d, 'operations/configuration.md')})", ""]
    syms = [(a.path, s_) for a in fa for s_ in a.symbols if s_.kind in ("class", "function")]
    syms.sort(key=lambda x: -(x[1].end_line - x[1].line))
    if syms:
        out += ["## Largest symbols", "", table(["Symbol", "Kind", "Location", "Docstring"],
               [(f"`{s_.name}`", s_.kind + (f"({', '.join(s_.bases)})" if s_.bases else ""), ref(p, s_.line, s_.end_line), s_.doc or "—")
                for p, s_ in syms[:10]])]
    eps_ = [a.path for a in fa if a.entrypoint]
    if eps_:
        out += ["## Entry points", ""] + [f"- `[Verified]` {ref(p)} has an `if __name__ == \"__main__\"` block" for p in eps_] + [""]
    tests = m.comps["tested_by"].get(c, [])
    out += ["## Tests", ""]
    out += ([f"- `[Verified]` imported by {ref(t)}" for t in tests[:10]] if tests else
            ["- `[Unknown]` No test file importing this component was found (dynamic loading or indirect tests would not be detected)."]) + [""]
    todos = [(p, t) for p in v["files"] if p in m.analyses for t in m.analyses[p].todos]
    if todos:
        out += ["## Open TODO/FIXME markers", ""] + [f"- `[Verified]` {ref(p, t['line'])} {t['tag']}: {t['text']}" for p, t in todos[:10]] + [""]
    out += ["## Files", "", ", ".join(ref(p) for p in v["files"][:40]) + (f" … and {len(v['files']) - 40} more" if len(v["files"]) > 40 else ""), "", MANUAL_NOTES, ""]
    return "\n".join(out)


def configuration(m: Model) -> Optional[str]:
    by = defaultdict(list)
    for p, a in m.analyses.items():
        for e in a.env_vars:
            by[e["name"]].append((p, e["line"], e.get("default")))
    if not by:
        return None
    rows = []
    for n, u in sorted(by.items()):
        dflt = next((d for _, _, d in u if d), None)
        rows.append((f"`{n}`", f"`{dflt}`" if dflt else "none / required", len(u), ", ".join(ref(p, ln) for p, ln, _ in u[:3])))
    return "\n".join(["# Environment configuration", "",
                      "`[Verified]` Variables read via `os.environ`/`getenv`/`process.env`-style access, with the default written in the code "
                      "(\"none / required\" = no default found at the first read). Valid values are not extracted — check the referenced lines.", "",
                      table(["Variable", "Default in code", "Read sites", "Locations"], rows), MANUAL_NOTES, ""])


def build_and_test(m: Model) -> Optional[str]:
    out = ["# Build and test", "", "`[Verified]` Commands are listed exactly as declared; they were **not** executed.", ""]
    n = len(out)
    for path, sc in m.scripts.items():
        out += [f"## Scripts in {ref(path)}", "", table(["Name", "Command"], [(f"`{k}`", f"`{v}`") for k, v in sc.items()])]
    for path, t in m.makefile_targets.items():
        out += [f"## Make targets in {ref(path)}", "", ", ".join(f"`{x}`" for x in t[:30]), ""]
    if m.stack["ci"]:
        out += ["## CI/CD definitions", ""] + [f"- {ref(p)}" for p in m.stack["ci"]] + [""]
    if len(out) == n:
        return None
    return "\n".join(out + [MANUAL_NOTES, ""])


def testing_strategy(m: Model) -> Optional[str]:
    tests = [f for f in m.files if f.kind == "test"]
    if not tests:
        return None
    n_fn = sum(1 for f in tests for s in (m.analyses[f.path].symbols if f.path in m.analyses else []) if s.name.split(".")[-1].startswith("test"))
    fw = [x for x in m.stack["frameworks"] if x in ("pytest", "Jest")]
    rows = [(comp_title(m, c), len(v["files"]), len(m.comps["tested_by"].get(c, [])) or "none") for c, v in _sorted_comps(m) if v["files"]]
    return "\n".join(["# Testing strategy", "",
                      f"- `[Verified]` {len(tests)} test file(s); {n_fn} Python test function(s) detected; frameworks from dependencies: {', '.join(fw) or 'not detected'}.",
                      "- `[Verified]` Coverage below is **import-based reachability, not line coverage**.", "",
                      table(["Component", "Source files", "Test files importing it"], rows), "## Test files", "",
                      *[f"- {ref(f.path)}" for f in tests[:40]], "", MANUAL_NOTES, ""])


def external_dependencies(m: Model) -> Optional[str]:
    if not m.deps:
        return None
    rows = [(f"`{d.name}`", d.spec or "—", d.ecosystem, "dev/optional" if d.dev else "runtime", ref(d.source)) for d in sorted(m.deps, key=lambda d: (d.dev, d.name))]
    return "\n".join(["# External dependencies", "", "`[Verified]` Declared in manifests. Resolved versions live in lockfiles (not parsed). "
                      "Why each was chosen is `[Unknown]` unless an ADR says so.", "",
                      table(["Package", "Version spec", "Ecosystem", "Scope", "Declared in"], rows), MANUAL_NOTES, ""])


def _llm_decisions(m: Model):
    out = [("system", d) for d in m.enrichment.get("system", {}).get("decisions", [])]
    for c, e in sorted(m.enrichment.get("components", {}).items()):
        out += [(c, d) for d in e.get("decisions", [])]
    return out


def decisions(m: Model) -> Optional[str]:
    sig = hist.significant(m.commits)
    llm_d = _llm_decisions(m)
    if not m.adrs and not sig and not llm_d:
        return None
    out = ["# Decisions and rationale", "", LEGEND, "",
           "Rationale is called **documented** only where an ADR, commit message, README or code comment states it (quoted and checked); "
           "otherwise it is `inferred` or `[Unknown]`. Alternatives are never listed unless a source mentions them.", ""]
    for a in m.adrs:
        out += [f"## ADR: {a['title']}", "", f"- Source: {ref(a['path'])} · status: `{a['status']}`",
                "- Confidence: **documented rationale**" if a["sections"] else "- Confidence: **unknown** (no recognisable sections)"]
        for k, v in a["sections"].items():
            out.append(f"- **{k.title()}:** `[Documented]` {v}")
        out += ["- Current validity: `[Unknown]` — not cross-checked against code.", ""]
    if llm_d:
        out += ["## Design decisions read from the code", "",
                "Each cites the lines where the decision is visible. **Confidence:** `documented` = the rationale is stated in a comment, docstring, "
                "commit or README and quoted verbatim (checked); `inferred` = a plausible reason, not stated; `unknown`. "
                "Alternatives are listed only when a source names them. Revisit conditions are not generated.", ""]
        for scope, x in llm_d:
            conf = {"documented": "Documented rationale", "inferred": "Inferred rationale", "unknown": "Unknown"}[x["basis"]]
            out += [f"### {x['decision']}", "", f"- Scope: {'whole system' if scope == 'system' else comp_title(m, scope)}",
                    f"- Rationale: {x['rationale']}" + (" *(downgraded: the quoted source could not be found)*" if x["downgraded"] else ""),
                    f"- Confidence: **{conf}**" + (f" — \"{x['quote']}\"" if x["quote"] else ""),
                    f"- Alternatives: {x['alternatives'] or '`[Unknown]` none named in the sources'}",
                    f"- Trade-offs: {x['tradeoffs'] or '`[Unknown]`'}",
                    f"- Evidence: {evs(x['evidence'])}", "- Current validity: `[Verified]` the cited code exists at this commit", ""]
    if sig:
        out += ["## Decision-flavoured commits", "",
                "Selected by wording/size heuristics; inclusion does not mean the commit was a major decision.", ""]
        for c in sig:
            out += [f"### {c['subject']}", "", f"- Commit: {commit_ref(m, c['sha'])} · {c['date']} · {c['added']}+/{c['deleted']}− lines · "
                    f"files: {', '.join(ref(f) if f in m.paths else f + ' (no longer present)' for f in c['files'][:4])}"]
            if c["rationale_stated"]:
                out.append(f"- Rationale: `[Documented]` {' '.join(c['body'].split())[:400]}")
            else:
                out.append("- Rationale: `[Unknown]` — commit message gives no reason.")
            out.append("")
    return "\n".join(out + [MANUAL_NOTES, ""])


def unresolved(m: Model) -> str:
    q = []
    if not m.readme:
        q.append("What is the system's purpose? No README found.")
    for c in sorted(m.comps["components"]):
        v = m.comps["components"][c]
        if v["files"] and not m.comps["tested_by"].get(c) and c != ROOT:
            q.append(f"Is `{c}` tested? No test file imports it (see [overview]({rel('decisions-and-rationale/unresolved-questions.md', comp_doc(c))}))." if c in m.documented
                     else f"Is `{c}` tested? No test file imports it.")
    for c in hist.significant(m.commits):
        if not c["rationale_stated"]:
            q.append(f"Why: \"{c['subject']}\" ({commit_ref(m, c['sha'])}) — commit message states no reason.")
    for p, a in m.analyses.items():
        if a.parse_error:
            q.append(f"{ref(p)} could not be parsed ({a.parse_error}); its symbols are missing from this knowledge base.")
    q += [f"{x}" for x in m.enrichment.get("system", {}).get("open_questions", [])]
    if not m.adrs:
        q.append("No architecture decision records found; architectural rationale is mostly `[Unknown]`.")
    return "\n".join(["# Unresolved questions and knowledge gaps", "", "`[Unknown]` items the repository cannot answer. "
                      "Human answers can be added in the notes block and will survive regeneration.", "",
                      *[f"- {x}" for x in q], "", MANUAL_NOTES, ""])


def technical_debt(m: Model) -> Optional[str]:
    todos = [(p, t) for p, a in m.analyses.items() for t in a.todos]
    hot = hist.churn(m.commits)
    if not todos and not hot:
        return None
    out = ["# Technical debt and risk signals", "", "These are signals from evidence, not verdicts.", ""]
    if todos:
        out += ["## TODO / FIXME / HACK markers", "", "`[Verified]` as written by the original authors.", "",
                table(["Location", "Tag", "Text"], [(ref(p, t["line"]), t["tag"], t["text"]) for p, t in todos[:60]])]
    if hot:
        out += ["## Most frequently changed files", "", "`[Verified]` counts within the analysed history window. "
                "High churn may indicate central or unstable code — `[Inferred]`.", "",
                table(["File", "Commits"], [(ref(p), n) for p, n in hot if p in m.paths])]
    unt = [c for c, v in m.comps["components"].items() if v["files"] and not m.comps["tested_by"].get(c) and c != ROOT]
    if unt:
        out += ["## Components with no importing test", "", *[f"- {comp_title(m, c)}" for c in sorted(unt)], ""]
    return "\n".join(out + [MANUAL_NOTES, ""])


def significant_changes(m: Model) -> Optional[str]:
    if not m.commits:
        return None
    by_month = Counter(c["date"][:7] for c in m.commits)
    return "\n".join(["# Change history", "", f"`[Verified]` {len(m.commits)} non-merge commit(s) analysed, "
                      f"{m.commits[-1]['date']} → {m.commits[0]['date']}.", "",
                      "## Activity by month", "", table(["Month", "Commits"], sorted(by_month.items(), reverse=True)),
                      "## Most recent commits", "", *[f"- {commit_ref(m, c['sha'])} {c['date']} — {c['subject']}" for c in m.commits[:20]],
                      "", f"Decision-flavoured commits: [decisions]({rel('change-history/significant-changes.md', 'decisions-and-rationale/verified-decisions.md')}).",
                      "", MANUAL_NOTES, ""])


def contracts(m: Model) -> Optional[str]:
    specs, mig = m.stack["api_specs"], m.stack["migrations"]
    if not specs and not mig:
        return None
    out = ["# API contracts and migrations", "", "`[Verified]` files present; contents are not summarised yet.", ""]
    if specs:
        out += ["## API specifications", ""] + [f"- {ref(p)}" for p in specs] + [""]
    if mig:
        out += ["## Migrations", ""] + [f"- {ref(p)}" for p in mig[:60]] + [""]
    return "\n".join(out + [MANUAL_NOTES, ""])


# ---------------------------------------------------------------------------------------------------------------------
# Architecture, integrations, contracts, workflows

def _mm(text: str, n=60) -> str:
    t = " ".join(re.sub(r"[\"'`:;#<>{}\[\]|\n]", " ", text).split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def _nid(prefix: str, name: str) -> str:
    return prefix + "_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def architecture_overview(m: Model) -> Optional[str]:
    sysv = m.enrichment.get("system", {})
    comps = [c for c in sorted(m.documented)]
    if len(comps) < 2 and not m.externals:
        return None
    d = "architecture/architecture-overview.md"
    out = ["# Architecture overview", "", LEGEND, ""]
    if sysv.get("architecture"):
        out += ["## How the system is put together", "", f"`[Inferred]` {sysv['architecture']}", ""]
    else:
        out += ["`[Unknown]` No narrative architecture summary was generated (run with AI summaries on). The diagram below is built only from static imports and detected integrations.", ""]
    lines = ["graph LR"]
    in_layer = set()
    for i, l in enumerate(sysv.get("layers", [])):
        lines.append(f'  subgraph L{i}["{_mm(l["name"], 40)}"]')
        for t in l["components"]:
            cname = next((c for c in comps if comp_title(m, c) == t), None)
            if cname and cname not in in_layer:
                lines.append(f'    {_nid("c", cname)}["{_mm(t)}"]')
                in_layer.add(cname)
        lines.append("  end")
    for c in comps:
        if c not in in_layer:
            lines.append(f'  {_nid("c", c)}["{_mm(comp_title(m, c))}"]')
    indeg = defaultdict(int)
    for e in m.comps["edges"]:
        if e["from"] in m.documented and e["to"] in m.documented:
            indeg[e["to"]] += 1
    shared = {c for c, n in indeg.items() if n >= max(5, 0.3 * len(comps))}      # imported by many: arrows would only add noise
    for e in m.comps["edges"]:
        if e["from"] in m.documented and e["to"] in m.documented and e["to"] not in shared and e["from"] not in shared:
            lines.append(f'  {_nid("c", e["from"])} --> {_nid("c", e["to"])}')
    for sx in m.externals:
        if sx["category"] == "observability":
            continue
        lines.append(f'  {_nid("x", sx["system"])}[("{_mm(sx["system"])}")]')
        for c in sx["components"]:
            if c in m.documented:
                lines.append(f'  {_nid("c", c)} -.-> {_nid("x", sx["system"])}')
    out += ["## Component and integration diagram", "",
            "`[Verified]` Solid arrows: static imports between components. Dotted arrows: a component imports a client library for that external system. "
            "Layer grouping (if any) is `[Inferred]`.", "", "```mermaid", *lines, "```", ""]
    if shared:
        out += ["Shared modules (imported by many components, so their arrows are omitted here; the full edge list is in "
                f"[dependency map]({rel(d, 'architecture/dependency-map.md')})): " + ", ".join(f"**{comp_title(m, c)}** ({indeg[c]} importers)" for c in sorted(shared)), ""]
    if sysv.get("layers"):
        out += ["## Layers", "", table(["Layer", "Components", "Role"], [(l["name"], ", ".join(l["components"]), l["role"]) for l in sysv["layers"]])]
    if sysv.get("flows"):
        out += ["## Key flows", ""] + [f"- [{f['name']}]({rel(d, 'architecture/workflows/' + slug(f['name']) + '.md')}) — {f['summary']}" for f in sysv["flows"]] + [""]
    for key, title in (("rules", "Cross-cutting business rules"), ("gotchas", "Cross-cutting gotchas"), ("risks", "Risks")):
        if sysv.get(key):
            out += [f"## {title}", ""] + _bul(sysv[key]) + [""]
    out += [f"See also: [integration boundaries]({rel(d, 'architecture/integration-boundaries.md')}), [API contracts]({rel(d, 'data-and-contracts/api-contracts.md')}), "
            f"[component catalog]({rel(d, 'architecture/component-catalog.md')}).", "", MANUAL_NOTES, ""]
    return "\n".join(out)


def integration_boundaries(m: Model) -> Optional[str]:
    if not m.externals:
        return None
    d = "architecture/integration-boundaries.md"
    rows = []
    for sx in m.externals:
        det = "; ".join(m.external_details.get(sx["system"], [])[:3])
        comps = ", ".join(f"[{comp_title(m, c)}]({rel(d, comp_doc(c))})" if c in m.documented else comp_title(m, c) for c in sx["components"][:6])
        rows.append((f"**{sx['system']}**", sx["category"], ", ".join(f"`{p}`" for p in sx["packages"]), comps,
                     ", ".join(ref(u["file"], u["line"]) for u in sx["uses"][:3]), det or "—"))
    return "\n".join(["# Integration boundaries", "",
                      "`[Verified]` External systems detected from imported client libraries. Connection details live in configuration — see "
                      f"[configuration]({rel(d, 'operations/configuration.md')}). What is stored in or fetched from each system is explained on the component pages.", "",
                      table(["System", "Role", "Library", "Used by", "First imports", "Specifics found in code"], rows), MANUAL_NOTES, ""])


def workflow_page(m: Model, f: dict) -> str:
    d = "architecture/workflows/" + slug(f["name"]) + ".md"
    parts = []
    for x in f["messages"]:
        for n in (x["from"], x["to"]):
            if n not in parts:
                parts.append(n)
    ids = {n: f"P{i}" for i, n in enumerate(parts)}
    lines = ["sequenceDiagram"] + [f'  participant {ids[n]} as {_mm(n, 40)}' for n in parts] + \
            [f'  {ids[x["from"]]}->>{ids[x["to"]]}: {_mm(x["text"], 100)}' for x in f["messages"]]
    return "\n".join([f"# Flow: {f['name']}", "", f"`[Inferred]` {f['summary']}", "", "```mermaid", *lines, "```", "",
                      "Derived from: " + evs(f["evidence"]) + ". Participants are restricted to detected components and external systems.", "",
                      f"Back to [architecture overview]({rel(d, 'architecture/architecture-overview.md')}).", "", MANUAL_NOTES, ""])


def api_contracts(m: Model) -> Optional[str]:
    if not m.endpoints:
        return None
    d = "data-and-contracts/api-contracts.md"
    eps = sorted(m.endpoints, key=lambda e: (e["path"], e["methods"]))
    unmounted = [e for e in eps if not e["mounted"]]
    out = ["# API contracts", "", f"`[Verified]` {len(eps)} HTTP route(s) read from route decorators. Paths include the prefixes added where routers are "
           "mounted (`include_router` / `register_blueprint`). Request/response types are the annotations in the code.", "",
           table(["Method", "Path", "Handler", "Params", "Response", "Status", "Summary"],
                 [("/".join(e["methods"]), f"`{e['path']}`", ref(e["file"], e["line"], e["end_line"]) + f" `{e['handler']}`",
                   "<br>".join(e["params"][:4]) or "—", e["response_model"] or "—", e["status_code"] or "—", e["summary"] or "—") for e in eps])]
    if unmounted:
        out += ["## Defined but not mounted", "", "`[Verified]` These routers are defined but no `include_router`/`register_blueprint` call in the "
                "repository attaches them to an app, so they are unreachable as written (possibly commented out):", ""] \
               + [f"- `{'/'.join(e['methods'])} {e['path']}` — {ref(e['file'], e['line'])}" for e in unmounted] + [""]
    return "\n".join(out + [MANUAL_NOTES, ""])


def data_models(m: Model) -> Optional[str]:
    rows = [(p, md) for p, a in sorted(m.analyses.items()) for md in a.models]
    tables_ = defaultdict(list)
    for p, a in m.analyses.items():
        for t in a.tables:
            tables_[t["table"]].append((p, t))
    if not rows and not tables_:
        return None
    out = ["# Data models and storage", "", "`[Verified]` read from class definitions and SQL strings; fields are as annotated in the code.", ""]
    if tables_:
        out += ["## SQL tables referenced", "", table(["Table", "Operations", "Referenced from"],
               [(f"`{t}`", ", ".join(sorted({x['op'] for _, x in u})), ", ".join(sorted({ref(p, x['line']) for p, x in u}))[:300]) for t, u in sorted(tables_.items())])]
    byc = defaultdict(list)
    for p, md in rows:
        byc[analyze_component(m, p)].append((p, md))
    for c, items in sorted(byc.items()):
        out += [f"## {comp_title(m, c)}", ""]
        for p, md in items:
            out += [f"### `{md['name']}` — {md['kind']}" + (f" (table `{md['table']}`)" if md["table"] else ""), "",
                    f"{ref(p, md['line'], md['end_line'])}" + (f" — {md['doc']}" if md["doc"] else ""), ""]
            if md["fields"]:
                out += [", ".join(f"`{f}`" for f in md["fields"]) + (f" … +{md['more']} more" if md["more"] else ""), ""]
    return "\n".join(out + [MANUAL_NOTES, ""])


def analyze_component(m: Model, path: str) -> str:
    from .analyze import component_of
    return component_of(path, m.comps["unit_dirs"])
