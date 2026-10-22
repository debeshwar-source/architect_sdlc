"""Static analysis. Parses text only (ast / regex / tomllib / json); repository code is never imported or executed."""

import ast
import json
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from .discover import FileInfo, CODE_LANGS, MANIFESTS
from . import facts
from .security import redact

FRAMEWORKS = {  # dependency name -> label
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI", "streamlit": "Streamlit", "langgraph": "LangGraph",
    "sqlalchemy": "SQLAlchemy", "celery": "Celery", "pytest": "pytest", "pydantic": "Pydantic", "react": "React",
    "next": "Next.js", "vue": "Vue", "express": "Express", "@angular/core": "Angular", "jest": "Jest",
    "spring-boot": "Spring Boot", "gin-gonic/gin": "Gin", "tokio": "Tokio", "actix-web": "Actix Web",
    "kafka": "Kafka", "boto3": "AWS SDK (boto3)", "redis": "Redis client", "psycopg2": "PostgreSQL client",
}
ENV_RE = re.compile(r"""(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|process\.env\.|process\.env\[\s*|"""
                    r"""os\.Getenv\(\s*|System\.getenv\(\s*|env::var\(\s*)["']?([A-Z][A-Z0-9_]{2,})""")
TODO_RE = re.compile(r"(?:#|//|/\*|--)\s*(TODO|FIXME|HACK|XXX)\b[:\s]*(.{0,120})")
JS_IMPORT_RE = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*|require\(\s*)['"]([^'"]+)['"]""")


@dataclass
class Symbol:
    name: str
    kind: str          # class function method
    line: int
    end_line: int
    doc: str = ""
    bases: List[str] = field(default_factory=list)


@dataclass
class FileAnalysis:
    # (env defaults are transient: merged into env_vars by analyze_file)
    path: str
    lines: int
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    env_vars: List[dict] = field(default_factory=list)   # {name, line}
    todos: List[dict] = field(default_factory=list)
    entrypoint: bool = False
    module_doc: str = ""
    parse_error: Optional[str] = None
    aliases: Dict[str, str] = field(default_factory=dict)        # local name -> dotted module path
    import_lines: Dict[str, int] = field(default_factory=dict)   # imported module -> line
    routers: Dict[str, dict] = field(default_factory=dict)
    endpoints: List[dict] = field(default_factory=list)
    includes: List[dict] = field(default_factory=list)
    models: List[dict] = field(default_factory=list)
    tables: List[dict] = field(default_factory=list)


def _first_line(doc: Optional[str]) -> str:
    return redact((doc or "").strip().splitlines()[0][:200]) if doc and doc.strip() else ""


def analyze_python(text: str, path: str) -> FileAnalysis:
    fa = FileAnalysis(path, text.count("\n") + 1)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as e:
        fa.parse_error = f"{type(e).__name__}: {e}"[:200]
        return fa
    fa.module_doc = _first_line(ast.get_docstring(tree))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fa.symbols.append(Symbol(node.name, "class", node.lineno, node.end_lineno or node.lineno,
                                     _first_line(ast.get_docstring(node)), [ast.unparse(b)[:60] for b in node.bases]))
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_"):
                    fa.symbols.append(Symbol(f"{node.name}.{m.name}", "method", m.lineno, m.end_lineno or m.lineno,
                                             _first_line(ast.get_docstring(m))))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fa.symbols.append(Symbol(node.name, "function", node.lineno, node.end_lineno or node.lineno,
                                     _first_line(ast.get_docstring(node))))
        elif isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            fa.entrypoint = True
    ff = facts.python_facts(tree, path)
    fa.aliases, fa.import_lines, fa.routers = ff["aliases"], ff["import_lines"], ff["routers"]
    fa.endpoints, fa.includes, fa.models, fa.tables = ff["endpoints"], ff["includes"], ff["models"], ff["tables"]
    fa.imports = sorted(ff["import_lines"])
    fa._env_defaults = ff["env_defaults"]
    return fa


def analyze_file(text: str, fi: FileInfo) -> FileAnalysis:
    if fi.language == "Python":
        fa = analyze_python(text, fi.path)
    else:
        fa = FileAnalysis(fi.path, text.count("\n") + 1)
        if fi.language in ("JavaScript", "TypeScript", "Vue"):
            for i, line in enumerate(text.splitlines(), 1):
                for imp in JS_IMPORT_RE.findall(line):
                    fa.import_lines.setdefault(imp, i)
            fa.imports = sorted(fa.import_lines)
    defaults = getattr(fa, "_env_defaults", {})
    if fi.language in ("JavaScript", "TypeScript", "Vue"):
        fa.endpoints = facts.js_facts(text)["endpoints"]
    for i, line in enumerate(text.splitlines(), 1):
        for name in sorted(set(ENV_RE.findall(line))):
            fa.env_vars.append({"name": name, "line": i, "default": redact(defaults[name]) if defaults.get(name) else defaults.get(name)})
        if (m := TODO_RE.search(line)):
            fa.todos.append({"line": i, "tag": m.group(1), "text": redact(m.group(2).strip())})
    return fa


# ---- manifests / stack -------------------------------------------------------------------------------------------

@dataclass
class Dependency:
    name: str
    spec: str
    ecosystem: str
    source: str       # manifest path
    dev: bool = False


def parse_manifest(path: str, text: str) -> List[Dependency]:
    name, deps = PurePosixPath(path).name, []
    try:
        if name == "requirements.txt":
            for line in text.splitlines():
                line = line.split("#")[0].strip()
                if line and not line.startswith(("-", "git+", "http")):
                    m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", line)
                    if m:
                        deps.append(Dependency(m.group(1).lower(), m.group(2).strip(), "pypi", path))
        elif name == "pyproject.toml":
            data = tomllib.loads(text)
            for d in data.get("project", {}).get("dependencies", []):
                m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", d)
                if m:
                    deps.append(Dependency(m.group(1).lower(), m.group(2), "pypi", path))
            for grp in data.get("project", {}).get("optional-dependencies", {}).values():
                for d in grp:
                    m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", d)
                    if m:
                        deps.append(Dependency(m.group(1).lower(), m.group(2), "pypi", path, True))
            for n, v in data.get("tool", {}).get("poetry", {}).get("dependencies", {}).items():
                if n.lower() != "python":
                    deps.append(Dependency(n.lower(), v if isinstance(v, str) else json.dumps(v), "pypi", path))
        elif name == "package.json":
            data = json.loads(text)
            for key, dev in (("dependencies", False), ("devDependencies", True), ("peerDependencies", False)):
                for n, v in (data.get(key) or {}).items():
                    deps.append(Dependency(n, str(v), "npm", path, dev))
        elif name == "go.mod":
            for m in re.finditer(r"^\s*(?:require\s+)?([\w.\-]+\.[\w.\-]+/[\w./\-]+)\s+(v[\w.\-+]+)", text, re.M):
                deps.append(Dependency(m.group(1), m.group(2), "go", path))
        elif name == "Cargo.toml":
            data = tomllib.loads(text)
            for key, dev in (("dependencies", False), ("dev-dependencies", True)):
                for n, v in (data.get(key) or {}).items():
                    deps.append(Dependency(n, v if isinstance(v, str) else str(v.get("version", "*")) if isinstance(v, dict) else "*", "cargo", path, dev))
    except (ValueError, tomllib.TOMLDecodeError, AttributeError):
        pass  # malformed manifest: skip rather than fail the run
    return deps


def detect_stack(files: List[FileInfo], deps: List[Dependency]) -> dict:
    lang_bytes = Counter()
    for f in files:
        if f.language and f.analyze:
            lang_bytes[f.language] += f.size
    names = {PurePosixPath(f.path).name for f in files}
    build = sorted({lbl for fn, lbl in {"pyproject.toml": "pyproject", "setup.py": "setuptools", "package.json": "npm scripts",
                                        "go.mod": "Go modules", "Cargo.toml": "Cargo", "pom.xml": "Maven",
                                        "build.gradle": "Gradle", "build.gradle.kts": "Gradle", "Makefile": "make"}.items()
                    if fn in names for lbl in [lbl]})
    pkg = sorted({e for e in (d.ecosystem for d in deps)} | ({"yarn"} if "yarn.lock" in names else set())
                 | ({"pnpm"} if "pnpm-lock.yaml" in names else set()) | ({"poetry"} if "poetry.lock" in names else set()))
    dep_names = {d.name.lower() for d in deps}
    frameworks = sorted({lbl for k, lbl in FRAMEWORKS.items()
                         if any(n.split("[")[0] == k or n.endswith("/" + k) for n in dep_names)})
    by_kind = defaultdict(list)
    for f in files:
        by_kind[f.kind].append(f.path)
    return {
        "languages": [{"language": l, "bytes": b, "share": round(100 * b / max(1, sum(lang_bytes.values())), 1)}
                      for l, b in lang_bytes.most_common()],
        "build_systems": build, "package_managers": pkg, "frameworks": frameworks,
        "ci": sorted(by_kind["ci"]),
        "infra": sorted(by_kind["infra"]),
        "api_specs": sorted(by_kind["api-spec"]),
        "migrations": sorted(by_kind["migration"]),
    }


# ---- components --------------------------------------------------------------------------------------------------

ROOT = "_root"
SPLIT_ABOVE = 14      # a component with more analysable files than this is split by sub-folder
MIN_CHILD = 3


def split_dirs(files: List[FileInfo], unit_dirs: List[str]) -> List[str]:
    """Component directories: manifest directories, plus sub-folders of any folder that is too big to be one component
    (so a 114-file `app/` becomes app/api, app/services, app/repositories/postgres ...)."""
    code = [f.path for f in files if f.kind in ("source", "script", "test") and f.analyze]
    dirs = set(unit_dirs)

    def under(prefix):
        return [p for p in code if p.startswith(prefix + "/")]

    def descend(prefix, depth):
        members = under(prefix)
        if len(members) <= SPLIT_ABOVE or depth > 3:
            return
        counts: Dict[str, int] = defaultdict(int)
        for p in members:
            rest = PurePosixPath(p).relative_to(prefix).parts
            if len(rest) > 1:
                counts[rest[0]] += 1
        for child, n in counts.items():
            if n >= MIN_CHILD:
                dirs.add(f"{prefix}/{child}")
                descend(f"{prefix}/{child}", depth + 1)
    for top in {PurePosixPath(p).parts[0] for p in code if len(PurePosixPath(p).parts) > 1} | set(unit_dirs):
        dirs.add(top) if top not in unit_dirs else None
        descend(top, 1)
    return sorted(dirs)


def component_of(path: str, comp_dirs: List[str]) -> str:
    for u in sorted(comp_dirs, key=len, reverse=True):
        if path.startswith(u + "/"):
            return u
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else ROOT


def dotted_of(path: str) -> Optional[str]:
    p = PurePosixPath(path)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def resolve_module(dotted: str, dotted_map: Dict[str, str]) -> Optional[str]:
    """File for the longest known module path that prefixes `dotted` (so `pkg.mod.Class` -> pkg/mod.py)."""
    parts = dotted.split(".")
    for n in range(len(parts), 0, -1):
        hit = dotted_map.get(".".join(parts[:n]))
        if hit:
            return hit
    return None


def build_components(files: List[FileInfo], analyses: Dict[str, FileAnalysis]) -> dict:
    """Group files into logical units (nested manifest directories, big folders split by sub-folder) and build the
    component-level import graph."""
    manifest_dirs = sorted({str(PurePosixPath(f.path).parent) for f in files
                            if f.kind == "manifest" and PurePosixPath(f.path).name in MANIFESTS
                            and str(PurePosixPath(f.path).parent) != "."})
    unit_dirs = split_dirs(files, manifest_dirs)
    comps: Dict[str, dict] = defaultdict(lambda: {"files": [], "tests": [], "other": []})
    for f in files:
        c = component_of(f.path, unit_dirs)
        bucket = "tests" if f.kind == "test" else "files" if f.kind in ("source", "script") and f.analyze else "other"
        comps[c][bucket].append(f.path)
    comps = {k: v for k, v in comps.items() if v["files"] or v["tests"]}
    dotted_map = {d: f.path for f in files if f.analyze and (d := dotted_of(f.path))}
    paths = {f.path for f in files}
    kinds = {f.path: f.kind for f in files}
    edges: Dict[tuple, set] = defaultdict(set)
    tested_by: Dict[str, set] = defaultdict(set)
    file_edges: Dict[str, set] = defaultdict(set)      # file -> files it imports (in-repo)
    for p, fa in analyses.items():
        src = component_of(p, unit_dirs)
        for imp in fa.imports:
            tfile = None
            if imp.startswith("."):                   # JS/TS relative import
                norm = _normpath(str(PurePosixPath(p).parent / imp))
                for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                    if norm + ext in paths:
                        tfile = norm + ext
                        break
            else:
                tfile = resolve_module(imp, dotted_map)
            if not tfile or tfile == p:
                continue
            file_edges[p].add(tfile)
            tgt = component_of(tfile, unit_dirs)
            if kinds.get(p) == "test":
                tested_by[tgt].add(p)
            elif tgt != src:
                edges[(src, tgt)].add(p)
    return {"unit_dirs": unit_dirs, "components": dict(comps),
            "tested_by": {k: sorted(v) for k, v in tested_by.items()},
            "file_imports": {k: sorted(v) for k, v in file_edges.items()},
            "dotted_map": dotted_map,
            "edges": [{"from": a, "to": b, "evidence": sorted(v)[:5], "count": len(v)} for (a, b), v in sorted(edges.items())]}


def resolve_endpoints(files: List[FileInfo], analyses: Dict[str, FileAnalysis], dotted_map: Dict[str, str]) -> List[dict]:
    """Full URL of every route: router prefix + the prefixes it was mounted with (include_router/register_blueprint)."""
    mounts: Dict[tuple, List[tuple]] = defaultdict(list)     # (file, router var) -> [(parent file, parent var, prefix)]
    for p, fa in analyses.items():
        for inc in fa.includes:
            child = inc["child"]
            head, _, rest = child.partition(".")
            if rest and head in fa.aliases:                  # `mod.router`
                mod = resolve_module(fa.aliases[head], dotted_map)
                var = rest.split(".")[-1]
            elif child in fa.aliases:                        # `from x import router as r`
                dotted = fa.aliases[child]
                mod, var = resolve_module(dotted.rsplit(".", 1)[0], dotted_map), dotted.rsplit(".", 1)[-1]
            else:
                mod, var = p, child.split(".")[-1]
            if mod:
                mounts[(mod, var)].append((p, inc["parent"], inc["prefix"] or ""))

    def prefixes(fpath, var, depth=0):
        own = analyses[fpath].routers.get(var, {}).get("prefix", "") if fpath in analyses else ""
        ups = mounts.get((fpath, var), [])
        if depth > 4 or not ups:
            return [(own, [])]
        out = []
        for pf, pv, pre in ups:
            for upper, via in prefixes(pf, pv, depth + 1):
                out.append((facts.join_path(upper, pre, own), via + [pf]))
        return out

    eps = []
    for p, fa in sorted(analyses.items()):
        for e in fa.endpoints:
            seen = set()
            for pre, via in prefixes(p, e["router"]):
                full = facts.join_path(pre, e["path"]) if e["path"] else facts.join_path(pre)
                if full in seen:
                    continue
                seen.add(full)
                mounted = bool(mounts.get((p, e["router"]))) or fa.routers.get(e["router"], {}).get("kind") in ("FastAPI", "Flask", "Sanic", "Quart") \
                    or not fa.routers
                eps.append({**{k: e[k] for k in ("methods", "handler", "line", "end_line", "response_model", "status_code", "params", "summary")},
                            "path": full, "file": p, "mounted": mounted})
    return eps


def _normpath(p: str) -> str:
    out: List[str] = []
    for part in p.split("/"):
        if part == "..":
            if out:
                out.pop()
        elif part not in (".", ""):
            out.append(part)
    return "/".join(out)


def read_text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="replace")
