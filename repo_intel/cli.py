"""Usage: python -m repo_intel.cli build <git-url-or-path> [--kb-root knowledge_base/] [--ref main] [--no-llm]"""

import argparse
import json
import sys
from pathlib import Path

from . import pipeline


def _show_progress(msg, frac=None):
    bar = "" if frac is None else f"[{'#' * int(frac * 20):<20}] {int(frac * 100):>3}%  "
    print(bar + msg, file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="repo_intel", description="Build or update an engineering knowledge base from a Git repository.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="clone/fetch the repository and (re)generate the knowledge base")
    b.add_argument("source", help="Git URL or local repository path")
    b.add_argument("--kb-root", type=Path, help="library folder holding one sub-folder per repository (default: the app's knowledge_base/). "
                   "The repository's folder is created on first run and found again on later runs")
    b.add_argument("--out", type=Path, help="write to exactly this folder instead of the library (advanced)")
    b.add_argument("--ref", help="branch, tag or commit SHA (default: remote default branch)")
    b.add_argument("--token-env", help="name of an environment variable holding an access token for private repositories")
    b.add_argument("--work-dir", type=Path, help="persistent clone location (default: temporary, deleted afterwards)")
    b.add_argument("--exclude", action="append", default=[], help="extra glob to exclude (repeatable)")
    b.add_argument("--max-commits", type=int, default=500)
    b.add_argument("--no-llm", action="store_true", help="facts only: no AI calls at all (free, but no purpose/flows/rules/decisions)")
    b.add_argument("--deep", action="append", default=[], metavar="COMPONENT",
                   help="also read this component in depth (one extra AI call each; repeatable, e.g. --deep app/services). Default build = ONE AI call")
    a = ap.parse_args(argv)
    llm_json = None
    if not a.no_llm:
        import llm   # project-root module; imported lazily so the deterministic path has no SDK dependency
        llm_json = llm.call_llm_json
    out = a.out
    if out is None:
        from . import layout
        root = a.kb_root
        if root is None:
            import config   # project-root module
            root = config.KNOWLEDGE_BASES_ROOT
        out = layout.kb_dir_for(a.source, root)
    rep = pipeline.run(a.source, out, a.ref, a.token_env, a.work_dir, a.exclude, llm_json, max_commits=a.max_commits,
                        progress=_show_progress, deep=a.deep)
    summary = {k: (len(v) if isinstance(v, list) and k in ("added", "updated", "unchanged", "removed", "stale") else v)
               for k, v in rep.items() if k not in ("quality",)}
    q = rep.get("quality", {})
    if "ai_usage" in rep:
        u = rep["ai_usage"]
        print(f"AI usage: {u['calls']} call(s), {u['cache_hits']} cached, ~{u['approx_input_tokens']:,} input / ~{u['approx_output_tokens']:,} output tokens", file=sys.stderr)
    summary["quality"] = {k: (len(v) if isinstance(v, list) else v) for k, v in q.items()}
    print(json.dumps(summary, indent=2, default=str))
    return 0 if rep["status"] in ("ok", "completed-with-validation-errors") else 1


if __name__ == "__main__":
    sys.exit(main())
