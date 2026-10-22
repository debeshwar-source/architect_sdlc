# Decisions and rationale

**Evidence labels:** `[Verified]` read from source · `[Documented]` stated in an ADR/README/commit · `[Inferred]` interpretation · `[Recommendation]` proposed · `[Unknown]` not established.

Rationale is recorded **only** where an ADR or commit message states it; otherwise it is `[Unknown]`. Alternatives are never listed unless a source mentions them.

## Decision-flavoured commits

Selected by wording/size heuristics; inclusion does not mean the commit was a major decision.

### Three-agent pipeline, real Jira, multiple knowledge bases, desktop app

- Commit: `991cd17a` · 2026-10-12 · 5824+/1647− lines · files: `.env.example`, `.gitignore`, `.streamlit/config.toml`, `README.md`
- Rationale: `[Documented]` Pipeline - Reduce to Requirement -> Design (renamed from Architecture) -> Release; remove the Quality, Implementation Spec and Review stages, the retry loop and the circuit breaker. - Release agent writes a Jira epic with stories, tasks and subtasks. - Choose where a run starts and stops (skip the Requirement agent, or stop after Requirement / Design) and give the Design agent binding architectura

### Default to Human approval mode and replace the page header with a new heading

- Commit: `3d1b6801` · 2026-10-13 · 2+/2− lines · files: `streamlit_app.py`
- Rationale: `[Unknown]` — commit message gives no reason.

### Add Quality Planner, design documents, session store, and editable test plan

- Commit: `98f558bb` · 2026-10-13 · 9227+/784− lines · files: `.env.example`, `README.md`, `agents/design.py`, `agents/quality.py`
- Rationale: `[Unknown]` — commit message gives no reason.

<!-- manual:begin notes -->
<!-- manual:end notes -->
