"""Deterministic fact extraction from source text: HTTP endpoints, data models, SQL tables, config defaults, import aliases.
Everything here is read from the syntax tree / text; nothing is guessed and nothing is executed."""

import ast
import re
from typing import Dict, List, Optional

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}
ROUTER_FACTORIES = {"APIRouter", "FastAPI", "Flask", "Blueprint", "Router", "Sanic", "Quart"}
MODEL_BASES = {"BaseModel": "pydantic model", "BaseSettings": "settings", "Enum": "enum", "StrEnum": "enum", "IntEnum": "enum",
               "TypedDict": "typed dict", "NamedTuple": "named tuple", "DeclarativeBase": "orm base", "Base": "orm model",
               "SQLModel": "orm model", "Model": "orm model", "Document": "document model"}
SQL_RE = re.compile(r"(?is)\b(select|insert\s+into|update|delete\s+from)\b")
SQL_TABLE_RE = re.compile(r"(?is)\b(from|join|into|update)\s+([A-Za-z_][\w.]*)")
SQL_STOP = {"select", "set", "where", "values", "lateral", "unnest", "generate_series", "jsonb_array_elements", "table", "only", "the", "a"}
JS_ROUTE_RE = re.compile(r"""\b(\w+)\.(get|post|put|delete|patch|all)\(\s*['"`]([^'"`]+)['"`]""")


def _s(node) -> Optional[str]:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kw(call: ast.Call, *names):
    for k in call.keywords:
        if k.arg in names:
            return k.value
    return None


def _short(node, n=60) -> str:
    try:
        return ast.unparse(node)[:n]
    except Exception:
        return "?"


def _base_name(b) -> str:
    return b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else ""


def python_facts(tree: ast.AST, path: str) -> dict:
    routers: Dict[str, dict] = {}
    endpoints: List[dict] = []
    includes: List[dict] = []
    models: List[dict] = []
    tables: List[dict] = []
    aliases: Dict[str, str] = {}
    import_lines: Dict[str, int] = {}
    env_defaults: Dict[str, Optional[str]] = {}
    pkg = path.rsplit("/", 1)[0].replace("/", ".") if "/" in path else ""
    is_init = path.endswith("__init__.py")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                import_lines.setdefault(a.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:                       # relative import: resolve against this file's package
                parts = pkg.split(".") if pkg else []
                up = node.level - 1 - (0 if is_init else 0)
                parts = parts[:len(parts) - up] if up <= len(parts) else []
                base = ".".join(parts + ([node.module] if node.module else []))
            import_lines.setdefault(base, node.lineno)
            for a in node.names:
                aliases[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
                import_lines.setdefault(f"{base}.{a.name}", node.lineno)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fname = _base_name(node.value.func)
            if fname in ROUTER_FACTORIES:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        pre = _s(_kw(node.value, "prefix", "url_prefix")) or ""
                        routers[t.id] = {"prefix": pre, "kind": fname, "line": node.lineno}
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("include_router", "register_blueprint", "mount") and node.args:
                parent = f.value.id if isinstance(f.value, ast.Name) else _short(f.value)
                includes.append({"parent": parent, "child": _short(node.args[0]), "line": node.lineno,
                                 "prefix": _s(_kw(node, "prefix", "url_prefix")) or (_s(node.args[0]) if f.attr == "mount" else "") or ""})
            # config reads with defaults
            if (isinstance(f, ast.Attribute) and f.attr in ("getenv", "get") and node.args and _s(node.args[0])
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", _s(node.args[0]) or "")
                    and (_short(f.value) in ("os", "os.environ", "environ"))):
                d = node.args[1] if len(node.args) > 1 else _kw(node, "default")
                env_defaults.setdefault(_s(node.args[0]), _short(d, 40) if d is not None else None)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 24 and SQL_RE.search(node.value):
            for m in SQL_TABLE_RE.finditer(node.value):
                t = m.group(2).strip(".").lower()
                if t not in SQL_STOP and not t.startswith(("(", "$", ":", "%")):
                    tables.append({"table": t, "op": {"from": "read", "join": "read", "into": "write", "update": "write"}[m.group(1).lower()],
                                   "line": node.lineno})

    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)):
                    continue
                verb, var = d.func.attr, d.func.value.id
                if verb in HTTP_METHODS or verb in ("route", "websocket", "api_route"):
                    p = _s(d.args[0]) if d.args else _s(_kw(d, "path", "rule"))
                    if p is None:
                        continue
                    methods = [verb.upper()] if verb in HTTP_METHODS else ["WS"] if verb == "websocket" else \
                        [_s(e) or "?" for e in getattr(_kw(d, "methods"), "elts", [])] or ["GET"]
                    params = [f"{a.arg}: {_short(a.annotation, 50)}" for a in node.args.args + node.args.kwonlyargs
                              if a.annotation is not None and a.arg not in ("self", "request", "response")]
                    doc = ast.get_docstring(node)
                    endpoints.append({"router": var, "path": p, "methods": [m.upper() for m in methods], "handler": node.name,
                                      "line": node.lineno, "end_line": node.end_lineno or node.lineno,
                                      "response_model": _short(_kw(d, "response_model"), 50) if _kw(d, "response_model") is not None else
                                      (_short(node.returns, 50) if node.returns is not None else None),
                                      "status_code": _short(_kw(d, "status_code"), 10) if _kw(d, "status_code") is not None else None,
                                      "params": params[:6], "summary": (doc.strip().splitlines()[0][:140] if doc and doc.strip() else None)})
        elif isinstance(node, ast.ClassDef):
            bases = [_base_name(b) for b in node.bases]
            kind = next((MODEL_BASES[b] for b in bases if b in MODEL_BASES), None)
            if kind is None and any(_short(d).startswith(("dataclass", "dataclasses.dataclass")) for d in node.decorator_list):
                kind = "dataclass"
            table = None
            fields = []
            for st in node.body:
                if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in st.targets):
                    table, kind = _s(st.value), "orm model"
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    fields.append(f"{st.target.id}: {_short(st.annotation, 40)}" + (f" = {_short(st.value, 30)}" if st.value is not None else ""))
                elif isinstance(st, ast.Assign) and kind == "enum":
                    for t in st.targets:
                        if isinstance(t, ast.Name) and not t.id.startswith("_"):
                            fields.append(f"{t.id} = {_short(st.value, 30)}")
                elif isinstance(st, ast.Assign) and kind == "orm model" and isinstance(st.value, ast.Call) and _base_name(st.value.func) in ("Column", "mapped_column"):
                    fields.append(f"{_short(st.targets[0], 30)}: {_short(st.value, 60)}")
            n_methods = sum(isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and not st.name.startswith("__") and not st.decorator_list
                            for st in node.body)       # plain behaviour methods (properties / factories / validators don't count)
            if kind == "dataclass" and n_methods >= 1:
                kind = None          # a dataclass with behaviour is a service/helper, not a data model
            if kind:
                models.append({"name": node.name, "kind": kind, "table": table, "line": node.lineno, "end_line": node.end_lineno or node.lineno,
                               "fields": fields[:30], "more": max(0, len(fields) - 30), "doc": (ast.get_docstring(node) or "").strip().splitlines()[0][:140] if ast.get_docstring(node) else ""})
            if table:
                tables.append({"table": table.lower(), "op": "defined", "line": node.lineno})
    return {"routers": routers, "endpoints": endpoints, "includes": includes, "models": models, "tables": tables,
            "aliases": aliases, "import_lines": import_lines, "env_defaults": env_defaults}


def js_facts(text: str) -> dict:
    eps = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in JS_ROUTE_RE.finditer(line):
            if m.group(3).startswith("/") and m.group(1) in ("app", "router", "server", "api", "route", "routes"):
                eps.append({"router": m.group(1), "path": m.group(3), "methods": [m.group(2).upper()], "handler": "", "line": i,
                            "end_line": i, "response_model": None, "status_code": None, "params": [], "summary": None})
    return {"endpoints": eps}


def join_path(*parts: str) -> str:
    out = "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return out if out != "/" else "/"
