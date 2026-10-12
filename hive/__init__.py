"""The hive: shared run memory for the pipeline.

Three small, dependency-free pieces, all file-backed under the run's
directory (or memory/ for cross-run), so they work identically from
the CLI and the Streamlit UI:

  events      append-only JSONL log of everything that happens in a run
  blackboard  shared, provenance-tagged board: decisions, open
              questions, risks, standing human decisions
  memory      cross-run recall of similar past runs (BM25 over notes)
"""
