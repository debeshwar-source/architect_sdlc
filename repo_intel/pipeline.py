"""Orchestration: acquire -> discover -> analyse (cached per file hash) -> render -> merge safely -> validate -> report.

Incremental behaviour
  * Per-file analysis is cached by content hash, so unchanged files are not re-parsed.
  * LLM synthesis is cached by prompt hash, so unchanged inputs cost nothing.
  * Documents are rendered deterministically; a document is only rewritten if its content changed.
  * Human edits are never overwritten: `<!-- manual:begin id -->` blocks are carried over, and if the generated part of a
    document was edited by hand the new version is written beside it as `<name>.proposed.md` and flagged for review.
  * Documents whose source disappeared are deleted if untouched, otherwise marked STALE.
"""

import hashlib
import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional

from . import analyze, generate as G, history, ingest, layout, systems, understand, validate as V
from .discover import discover
from .security import redact

GENERATOR_VERSION = "1"
MANUAL_RE = re.compile(r"<!-- manual:begin (\w+) -->\n?(.*?)\n?<!-- manual:end \1 -->", re.S)
STALE_BANNER = "> **STALE:** the source this document described no longer exists. Review the manual notes below, then delete this file.\n\n"
REVIEW_KINDS = {"migration", "ci", "infra", "api-spec", "manifest"}
MAX_DOCUMENTED = 40


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gen_sha(text: str) -> str:
    """Hash of a document with manual blocks emptied, so annotations never look like edits to generated text."""
    return _sha(MANUAL_RE.sub(lambda m: f"<!-- manual:begin {m.group(1)} -->\n<!-- manual:end {m.group(1)} -->", text))


def merge_manual(new: str, old: str) -> str:
    blocks = {m.group(1): m.group(2) for m in MANUAL_RE.finditer(old) if m.group(2).strip()}
    used = set()

    def put(m):
        if m.group(1) in blocks:
            used.add(m.group(1))
            return f"<!-- manual:begin {m.group(1)} -->\n{blocks[m.group(1)]}\n<!-- manual:end {m.group(1)} -->"
        return m.group(0)
    out = MANUAL_RE.sub(put, new)
    for k, v in blocks.items():
        if k not in used:   # the block's anchor vanished from the new version: keep the text rather than drop it
            out += f"\n<!-- manual:begin {k} -->\n{v}\n<!-- manual:end {k} -->\n"
    return out


def _atomic_write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _commit_url(url: str) -> Optional[str]:
    m = re.match(r"(?:https://|git@)(github\.com|gitlab\.com)[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return f"https://{m.group(1)}/{m.group(2)}/" + ("-/commit/{sha}" if m.group(1) == "gitlab.com" else "commit/{sha}")


def build_model(snap: ingest.RepoSnapshot, cache_dir: Path, excludes=None, max_files=20000, max_commits=500,
                warnings: Optional[List[str]] = None) -> G.Model:
    warnings = warnings if warnings is not None else []
    files = discover(snap.path, excludes)
    if len(files) > max_files:
        warnings.append(f"{len(files)} files exceed max_files={max_files}; analysing the first {max_files} only")
        files = files[:max_files]
    cache = _load(cache_dir / "analysis.json", {})
    if cache.get("_v") != [ingest.ANALYSIS_VERSION, GENERATOR_VERSION]:
        cache = {"_v": [ingest.ANALYSIS_VERSION, GENERATOR_VERSION]}
    new_cache, analyses, deps, scripts, targets = {"_v": cache.get("_v")}, {}, [], {}, {}
    readme = None
    hits = 0
    for f in files:
        if f.kind == "doc" and readme is None and PurePosixPath(f.path).name.upper().startswith("README") and f.analyze \
                and "/" not in f.path:
            readme = (f.path, analyze.read_text(snap.path, f.path))
        if not f.analyze:
            continue
        if f.kind == "manifest":
            text = analyze.read_text(snap.path, f.path)
            deps += analyze.parse_manifest(f.path, text)
            if PurePosixPath(f.path).name == "package.json":
                try:
                    s = json.loads(text).get("scripts") or {}
                    if s:
                        scripts[f.path] = {k: redact(str(v)) for k, v in s.items()}
                except ValueError:
                    pass
        elif f.path.endswith("requirements.txt"):
            deps += analyze.parse_manifest(f.path, analyze.read_text(snap.path, f.path))
        if PurePosixPath(f.path).name == "Makefile":
            targets[f.path] = re.findall(r"(?m)^([A-Za-z0-9_.-]+):(?!=)", analyze.read_text(snap.path, f.path))
        if f.kind not in ("source", "test", "script"):
            continue
        key = f.sha256
        if key in cache and cache[key].get("path") == f.path:
            fa = analyze.FileAnalysis(**{**cache[key]["a"], "symbols": [analyze.Symbol(**s) for s in cache[key]["a"]["symbols"]]})
            hits += 1
        else:
            fa = analyze.analyze_file(analyze.read_text(snap.path, f.path), f)
        analyses[f.path] = fa
        new_cache[key] = {"path": f.path, "a": asdict(fa)}
    _atomic_write(cache_dir / "analysis.json", json.dumps(new_cache))
    stack = analyze.detect_stack(files, deps)
    comps = analyze.build_components(files, analyses)
    commits = history.commits(snap.path, max_commits)
    adrs = [history.parse_adr(analyze.read_text(snap.path, p), p) for p in history.find_adrs(files)[:30]]
    m = G.Model(snap.provenance(), Path(snap.url.rstrip("/")).stem if "://" not in snap.url else snap.url.rstrip("/").split("/")[-1].removesuffix(".git"),
                files, analyses, deps, stack, comps, commits, adrs, readme, scripts, targets, commit_url=_commit_url(snap.url))
    m.cache_hits = hits
    m.endpoints = analyze.resolve_endpoints(files, analyses, m.comps["dotted_map"])
    m.externals = systems.detect(analyses, lambda p: analyze.component_of(p, m.comps["unit_dirs"]))
    m.external_details = systems.details_from_text(
        {p: analyze.read_text(snap.path, p) for s_ in m.externals for p in [u["file"] for u in s_["uses"]]}, m.externals)
    ranked = [c for c, v in G._sorted_comps(m) if v["files"]]
    m.documented = set(ranked[:MAX_DOCUMENTED])
    if len(ranked) > MAX_DOCUMENTED:
        warnings.append(f"{len(ranked)} components found; only the {MAX_DOCUMENTED} largest get their own page")
    return m


def render_all(m: G.Model) -> Dict[str, str]:
    docs: Dict[str, Optional[str]] = {
        "repository-overview/system-purpose.md": G.system_purpose(m),
        "repository-overview/technology-stack.md": G.technology_stack(m),
        "repository-overview/repository-map.md": G.repository_map(m),
        "architecture/architecture-overview.md": G.architecture_overview(m),
        "architecture/integration-boundaries.md": G.integration_boundaries(m),
        "architecture/component-catalog.md": G.component_catalog(m),
        "architecture/dependency-map.md": G.dependency_map(m),
        "operations/configuration.md": G.configuration(m),
        "operations/build-and-test.md": G.build_and_test(m),
        "engineering-practices/testing-strategy.md": G.testing_strategy(m),
        "dependency-catalog/external-dependencies.md": G.external_dependencies(m),
        "decisions-and-rationale/verified-decisions.md": G.decisions(m),
        "decisions-and-rationale/unresolved-questions.md": G.unresolved(m),
        "quality-and-reliability/technical-debt.md": G.technical_debt(m),
        "change-history/significant-changes.md": G.significant_changes(m),
        "data-and-contracts/api-contracts.md": G.api_contracts(m),
        "data-and-contracts/data-models.md": G.data_models(m),
        "data-and-contracts/migrations-and-specs.md": G.contracts(m),
    }
    for f in m.enrichment.get("system", {}).get("flows", []):
        docs["architecture/workflows/" + G.slug(f["name"]) + ".md"] = G.workflow_page(m, f)
    for c in sorted(m.documented):
        docs[G.comp_doc(c)] = G.component_overview(m, c)
    docs = {k: v for k, v in docs.items() if v}
    docs = {k: _drop_dead_links(k, v, set(docs)) for k, v in docs.items()}
    docs["README.md"] = _index(m, docs)
    return docs


def _drop_dead_links(name: str, text: str, existing: set) -> str:
    """Pages may point at optional pages (dependency map, decisions...) that were not produced; show plain text then."""
    base = PurePosixPath(name).parent

    def fix(mo):
        target = mo.group(2).partition("#")[0]
        if not target.endswith(".md") or "://" in target or analyze._normpath(str(base / target)) in existing:
            return mo.group(0)
        return mo.group(1)
    return re.sub(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)", fix, text)


def _index(m: G.Model, docs: Dict[str, str]) -> str:
    p = m.provenance
    groups: Dict[str, List[str]] = {}
    for d in sorted(docs):
        groups.setdefault(d.split("/")[0] if "/" in d else "", []).append(d)
    out = [f"# Knowledge base: {m.repo_name}", "", G.LEGEND, "",
           f"- Source: `{p['repository_url']}` @ `{p['commit'][:12]}`" + (f" (branch `{p['branch']}`)" if p["branch"] else ""),
           f"- Analysis v{p['analysis_version']} (timestamps are kept in `metadata/` so unchanged runs leave documents byte-identical)", "",
           "Generated files are regenerated on each run. Write your own notes only inside the HTML-comment "
           "`manual` block at the bottom of a page (the empty `notes` block); its text is preserved across runs.", ""]
    for g, items in groups.items():
        if not g:
            continue
        out += [f"## {g.replace('-', ' ').title()}", ""]
        for d in items:
            title = next((l[2:] for l in docs[d].splitlines() if l.startswith("# ")), d)
            out.append(f"- [{title}]({d})")
        out.append("")
    out += ["## Metadata", "", "- `metadata/manifest.json`, `metadata/source-index.json`, `metadata/generation-report.json`", ""]
    return "\n".join(out)


def run(source: str, out_dir: Path, ref: Optional[str] = None, token_env: Optional[str] = None, work_dir: Optional[Path] = None,
        excludes: Optional[List[str]] = None, llm_json: Optional[Callable[..., dict]] = None, keep_clone: bool = False,
        max_commits: int = 500, progress: Callable[..., None] = lambda msg, frac=None: None,
        deep: Optional[List[str]] = None) -> dict:
    """progress(message, fraction): fraction is overall completion, 0..1."""
    t0 = time.time()
    out_dir = Path(out_dir)
    meta, cache_dir = out_dir / "metadata", out_dir / ".cache"
    ephemeral = work_dir is None
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="repo_intel_")) / "clone"
    report = {"status": "failed", "kb_dir": str(out_dir), "warnings": [], "added": [], "updated": [], "unchanged": [], "removed": [], "stale": [],
              "needs_review": [], "errors": []}
    manifest = _load(meta / "manifest.json", {"docs": {}})
    report["source"] = layout.normalize_source(source)
    try:
        if not (out_dir / layout.MARKER).exists():
            # claim the folder for this repository before doing anything that can fail, so a retry reuses it
            _atomic_write(out_dir / layout.MARKER, json.dumps({"provenance": {"repository_url": report["source"]}, "files": {}}))
        # share of the bar per stage; the LLM stage only gets a share when it will actually run
        S = {"clone": (0.0, 0.05), "analyse": (0.05, 0.15), "llm": (0.15, 0.90), "write": (0.90, 0.97), "check": (0.97, 1.0)} if llm_json else \
            {"clone": (0.0, 0.25), "analyse": (0.25, 0.6), "write": (0.6, 0.9), "check": (0.9, 1.0)}
        at = lambda stage, f=0.0: S[stage][0] + (S[stage][1] - S[stage][0]) * min(1.0, max(0.0, f))
        progress("Cloning / updating the repository…", at("clone"))
        snap = ingest.acquire(source, work, ref, token_env)
        progress("Analysing files…", at("analyse"))
        prev_index = _load(meta / "source-index.json", {}).get("files", {})
        # claim this folder for this repository right away, so a failed first run is still found (not duplicated) next time
        _atomic_write(meta / "source-index.json", json.dumps({"provenance": snap.provenance(), "files": prev_index}, indent=1, sort_keys=True))
        m = build_model(snap, cache_dir, excludes, max_commits=max_commits, warnings=report["warnings"])
        if llm_json:
            # components read in depth earlier stay deep (re-rendered from cache, so free); new ones are added
            deep_all = sorted(set(manifest.get("deep", [])) | set(deep or []))
            m.enrichment, report["ai_usage"] = _enrich_cached(m, snap.path, llm_json, cache_dir,
                                                              lambda msg, f=0.0: progress(msg, at("llm", f)), deep_all)
            manifest["deep"] = [c for c in deep_all if c in m.documented]
            report["warnings"] += m.enrichment.get("notes", [])
        progress("Writing pages…", at("write"))
        docs = render_all(m)
        _write_docs(out_dir, docs, manifest, report)
        index = {f.path: {"sha256": f.sha256, "kind": f.kind, "language": f.language, "analyzed": f.analyze,
                          "skip_reason": f.skip_reason, "component": analyze.component_of(f.path, m.comps["unit_dirs"])}
                 for f in m.files}
        if prev_index:
            _flag_review(prev_index, index, report)
        _atomic_write(meta / "source-index.json", json.dumps({"provenance": snap.provenance(), "files": index}, indent=1, sort_keys=True))
        report["changed_files"] = ({"added": sorted(set(index) - set(prev_index)), "removed": sorted(set(prev_index) - set(index)),
                                    "modified": sorted(p for p in index if p in prev_index and index[p]["sha256"] != prev_index[p]["sha256"])}
                                   if prev_index else "first run")
        progress("Checking links and references…", at("check"))
        report["quality"] = V.validate(out_dir, snap.path, {p: a.lines for p, a in m.analyses.items()}, set(docs))
        n_src = sum(1 for v in m.comps["components"].values() if v["files"])
        report["quality"]["component_coverage"] = f"{len(m.documented)}/{n_src}"
        report.update(status="ok" if report["quality"]["passed"] else "completed-with-validation-errors",
                      provenance=snap.provenance(), analysis_cache_hits=getattr(m, "cache_hits", 0),
                      files_total=len(m.files), files_analyzed=len(m.analyses), seconds=round(time.time() - t0, 2))
        progress("Done", 1.0)
    except Exception as e:
        report["errors"].append(f"{type(e).__name__}: {redact(str(e))[:500]}")
        report["retry"] = "Safe to re-run: acquisition resumes, documents are written atomically, edits are never overwritten."
    finally:
        manifest["generator_version"] = GENERATOR_VERSION
        _atomic_write(meta / "manifest.json", json.dumps(manifest, indent=1, sort_keys=True))
        _atomic_write(meta / "generation-report.json", json.dumps(report, indent=1, sort_keys=True))
        if ephemeral and not keep_clone:
            shutil.rmtree(work.parent, ignore_errors=True)
    return report


def _enrich_cached(m, root: Path, llm_json, cache_dir: Path, progress, deep=None):
    """Runs the AI reading with a disk cache. Returns (result, usage) - usage counts real calls vs cache hits and the
    approximate input size, so the cost of a build is visible."""
    cache_file = cache_dir / "understanding.json"
    cache = _load(cache_file, {})
    usage = {"calls": 0, "cache_hits": 0, "approx_input_tokens": 0, "approx_output_tokens": 0}

    def cached(**kw):
        k = _sha(kw["prompt"] + str(kw.get("system")))
        if k in cache:
            usage["cache_hits"] += 1
        else:
            cache[k] = llm_json(**kw)
            usage["calls"] += 1
            usage["approx_input_tokens"] += (len(kw["prompt"]) + len(str(kw.get("system") or ""))) // 4
            usage["approx_output_tokens"] += len(json.dumps(cache[k])) // 4
        return cache[k]
    try:
        return understand.understand(m, root, cached, progress, deep=deep), usage
    finally:
        _atomic_write(cache_file, json.dumps(cache))


def _write_docs(out_dir: Path, docs: Dict[str, str], manifest: dict, report: dict):
    recs = manifest.setdefault("docs", {})
    for path, new in sorted(docs.items()):
        fp, prop = out_dir / path, (out_dir / path).with_suffix(".proposed.md")
        if fp.exists():
            old = fp.read_text(encoding="utf-8")
            rec = recs.get(path)
            if not rec or gen_sha(old) != rec["gen_sha"]:
                # generated text was edited by hand (or the file is not ours): never overwrite
                _atomic_write(prop, merge_manual(new, old))
                report["needs_review"].append({"doc": path, "reason": "generated text was edited manually; proposal written to " + prop.name})
                continue
            merged = merge_manual(new, old)
            if merged == old:
                report["unchanged"].append(path)
            else:
                _atomic_write(fp, merged)
                report["updated"].append(path)
            final = merged
        else:
            _atomic_write(fp, new)
            report["added"].append(path)
            final = new
        prop.unlink(missing_ok=True)
        recs[path] = {"gen_sha": gen_sha(final), "generated_by": GENERATOR_VERSION}
    for path in sorted(set(recs) - set(docs)):
        fp = out_dir / path
        if not fp.exists():
            del recs[path]
            continue
        text = fp.read_text(encoding="utf-8")
        has_manual = any(m.group(2).strip() for m in MANUAL_RE.finditer(text))
        if gen_sha(text) == recs[path]["gen_sha"] and not has_manual:
            fp.unlink()
            del recs[path]
            report["removed"].append(path)
            d = fp.parent
            while d != out_dir and d.exists() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        else:
            if not text.startswith("> **STALE:**"):
                _atomic_write(fp, STALE_BANNER + text)
                recs[path]["gen_sha"] = gen_sha(STALE_BANNER + text)
            report["stale"].append(path)


def _flag_review(prev: dict, cur: dict, report: dict):
    for p in sorted(set(prev) | set(cur)):
        a, b = prev.get(p), cur.get(p)
        if a and b and a["sha256"] == b["sha256"]:
            continue
        info = b or a
        change = "added" if not a else "removed" if not b else "modified"
        if info["kind"] in REVIEW_KINDS or G.SENSITIVE.search(p):
            report["needs_review"].append({"file": p, "reason": f"{change} {info['kind']} file affecting "
                                           + ("security" if G.SENSITIVE.search(p) else "build/ops/data/contract surface")})
