"""Web UI for the repository knowledge-base generator (repo_intel/).
Run: streamlit run repo_intel_app.py

Plain words: type a Git address (or a folder on this computer), press Build, and read the knowledge base that
comes out. Pressing Build again on the same output folder only updates what changed.
"""

import json
import re
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import config
from repo_intel import generate, layout, pipeline

MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.S)

st.set_page_config(page_title="Repo Knowledge Base", page_icon="📚", layout="wide")


def load_report(out: Path):
    try:
        return json.loads((out / "metadata" / "generation-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_docs(out: Path):
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*.md") if ".cache" not in p.parts)


def render_markdown(text: str):
    """Streamlit cannot draw Mermaid itself, so those blocks are drawn with mermaid.js and the rest is Markdown."""
    pos = 0
    for m in MERMAID_RE.finditer(text):
        if text[pos:m.start()].strip():
            st.markdown(text[pos:m.start()])
        components.html(
            '<pre class="mermaid" style="background:white">' + m.group(1).replace("<", "&lt;") + '</pre>'
            '<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";'
            'mermaid.initialize({startOnLoad:true});</script>', height=420, scrolling=True)
        pos = m.end()
    if text[pos:].strip():
        st.markdown(text[pos:])


def show_result(rep: dict):
    ok = rep.get("status") == "ok"
    (st.success if ok else st.warning if rep.get("status", "").startswith("completed") else st.error)(f"Status: {rep.get('status')}")
    for e in rep.get("errors", []):
        st.error(e)
    if rep.get("retry"):
        st.caption(rep["retry"])
    c = st.columns(5)
    for col, key in zip(c, ("added", "updated", "unchanged", "removed", "stale")):
        col.metric(key.title(), len(rep.get(key, [])))
    u = rep.get("ai_usage")
    if u:
        st.info(f"AI usage: **{u['calls']}** call(s), {u['cache_hits']} answered from cache · ≈ {u['approx_input_tokens']:,} input / "
                f"{u['approx_output_tokens']:,} output tokens.")
    prov = rep.get("provenance")
    if prov:
        st.caption(f"Source `{prov['repository_url']}` · commit `{prov['commit'][:12]}` · branch `{prov['branch']}` · "
                   f"{rep.get('files_total')} files ({rep.get('files_analyzed')} analysed) · {rep.get('seconds')}s · "
                   f"analysis cache hits {rep.get('analysis_cache_hits')}")
    if rep.get("needs_review"):
        st.subheader("Needs human review")
        st.dataframe(rep["needs_review"], use_container_width=True)
    ch = rep.get("changed_files")
    if isinstance(ch, dict):
        with st.expander("Files changed since the last run"):
            st.json(ch)
    q = rep.get("quality")
    if q:
        st.subheader("Quality checks")
        a, b, d, e = st.columns(4)
        a.metric("Broken links", len(q["broken_links"]), help=f"{q['links_checked']} links checked")
        b.metric("Invalid source refs", len(q["invalid_source_refs"]), help=f"{q['source_refs_checked']} refs checked")
        d.metric("Duplicate paragraphs", len(q["duplicate_paragraphs"]))
        e.metric("Component coverage", q.get("component_coverage", "-"))
        for key in ("broken_links", "invalid_source_refs", "duplicate_paragraphs", "orphan_documents"):
            if q.get(key):
                with st.expander(f"{key.replace('_', ' ').title()} ({len(q[key])})"):
                    st.write(q[key])
    if rep.get("warnings"):
        with st.expander(f"Warnings ({len(rep['warnings'])})"):
            for w in rep["warnings"]:
                st.write("- " + w)


def component_key(out: Path, page: str):
    """The analyser's name for the component a page describes (pages/applications/<slug>/overview.md)."""
    try:
        files = json.loads((out / "metadata" / "source-index.json").read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        return None
    slug_ = page.split("/")[1] if page.startswith("applications/") else None
    return next((c for c in {f.get("component") for f in files.values()} if c and slug_ and generate.slug(c) == slug_), None)


def deep_button(out: Path, page: str, text: str):
    comp = component_key(out, page)
    if not comp:
        return
    if "## Workflow:" in text:
        st.caption("This component has been read in depth.")
        return
    if st.button("Explain this component in depth (1 extra AI call)", key="deep_" + page,
                 help="Reads this component's key source files and adds workflows, rules, gotchas and decisions. Cached afterwards."):
        import llm
        src = json.loads((out / "metadata" / "source-index.json").read_text(encoding="utf-8"))["provenance"]["repository_url"]
        bar = st.progress(0.0, text="Starting…")
        rep = pipeline.run(src, out, None, None, None, [], llm.call_llm_json, deep=[comp],
                           progress=lambda msg, frac=None: bar.progress(min(1.0, max(0.0, frac or 0.0)), text=f"{int((frac or 0) * 100)}% · {msg}"))
        bar.empty()
        st.session_state["report"] = rep
        st.rerun()


def show_browser(out: Path):
    docs = list_docs(out)
    if not docs:
        st.info("No documents in this folder yet. Press Build.")
        return
    left, right = st.columns([1, 3])
    with left:
        groups = {}
        for d in docs:
            groups.setdefault(d.split("/")[0] if "/" in d else "(top)", []).append(d)
        names = [d for g in sorted(groups, key=lambda x: (x != "(top)", x)) for d in groups[g]]
        pick = st.radio("Pages", names, key="page_pick", label_visibility="collapsed",
                        format_func=lambda d: d if "/" not in d else d.replace(".md", "").replace("/", " › "))
    with right:
        text = (out / pick).read_text(encoding="utf-8")
        st.caption(f"`{pick}`")
        render_markdown(text)
        st.download_button("Download this page", text, file_name=Path(pick).name, key="dl_" + pick)
        deep_button(out, pick, text)


st.title("📚 Repository Knowledge Base")
st.caption("Turns a Git repository into linked, evidence-labelled Markdown. Source code is read, never executed.")

with st.sidebar:
    st.header("Build")
    source = st.text_input("Git URL or local folder", help="Cloned with your own git setup: if a plain `git clone <url>` works in your terminal, it works here.", placeholder="https://github.com/org/repo.git  or  /path/to/repo")
    ref = st.text_input("Branch, tag or commit (optional)")
    root_text = st.text_input("Knowledge base library folder", value=str(config.KNOWLEDGE_BASES_ROOT),
                              help="Each repository gets its own sub-folder here. Building the same repository again updates that folder.")
    excludes = st.text_area("Extra exclude patterns (one per line)", placeholder="legacy/*\n*.generated.ts")
    max_commits = st.number_input("Max commits to read", 10, 5000, 500, step=50)
    depth = st.radio("AI depth", ["Standard — 1 AI call (recommended)", "Facts only — no AI, free"], index=0,
                     help="Standard sends one compact digest (~10-15k tokens in total) and gets back the purpose, architecture, key flows, rules, "
                          "gotchas and decisions - each backed by cited lines. Facts only skips the AI: endpoints, integrations, models, imports "
                          "and config, but no explanations. To go deeper on one component, open it in the Browse tab and press "
                          "'Explain in depth'. Results are cached, so rebuilding unchanged code costs nothing.")
    use_llm = depth.startswith("Standard")
    go = st.button("Build / update", type="primary", use_container_width=True, disabled=not source.strip())
    root = Path(root_text).expanduser()
    target = layout.kb_dir_for(source, root) if source.strip() else None
    if target is not None:
        st.caption(("Will **update** " if target.exists() else "Will **create** ") + f"`{target.name}/`")
    existing = layout.list_generated(root)
    st.divider()
    opened = st.selectbox("Or open an existing knowledge base", [""] + [p.name for p in existing], key="open_kb",
                          format_func=lambda n: n or "—")

if go:
    st.session_state["current"] = str(target)
    st.session_state.pop("report", None)
elif opened:
    st.session_state["current"] = str(root / opened)
    st.session_state.pop("report", None)
out = Path(st.session_state["current"]) if st.session_state.get("current") else None

if go:
    llm_json = None
    if use_llm:
        import llm
        llm_json = llm.call_llm_json
    bar = st.progress(0.0, text="Starting…")

    def show(msg, frac=None):
        bar.progress(min(1.0, max(0.0, frac or 0.0)), text=f"{int((frac or 0) * 100)}% · {msg}")
    st.session_state["report"] = pipeline.run(
        source.strip(), target, ref.strip() or None, None, None,
        [x.strip() for x in excludes.splitlines() if x.strip()], llm_json, max_commits=int(max_commits), progress=show)
    bar.empty()

report = (st.session_state.get("report") or (load_report(out) if out else None))
t_result, t_browse = st.tabs(["Result", "Browse knowledge base"])
with t_result:
    if report:
        show_result(report)
    else:
        st.info("Enter a repository in the sidebar and press **Build / update**.")
with t_browse:
    if out and out.is_dir():
        st.caption(f"Folder: `{out}`")
        show_browser(out)
    else:
        st.info("Build a repository or open an existing knowledge base to browse it.")
