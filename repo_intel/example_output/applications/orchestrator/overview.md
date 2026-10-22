# orchestrator

`[Verified]` 2 source file(s), 1075 lines, 0 test file(s). Parent: [component catalog](../../architecture/component-catalog.md).

## What the code says about itself

`[Verified]` Module docstrings (author claims, not independently checked):

- `orchestrator/graph.py` — SDLC planning pipeline, wired as a LangGraph state machine.

## Key symbols (largest first)

| Symbol | Kind | Location | Docstring |
|---|---|---|---|
| `SDLCState` | class(TypedDict) | `orchestrator/graph.py:L72-174` | — |
| `design_node` | function | `orchestrator/graph.py:L426-483` | — |
| `jira_publish_node` | function | `orchestrator/graph.py:L711-762` | — |
| `kb_save_node` | function | `orchestrator/graph.py:L797-847` | — |
| `build_graph` | function | `orchestrator/graph.py:L1029-1073` | Assemble the pipeline: register the steps, then draw the arrows |
| `confluence_publish_node` | function | `orchestrator/graph.py:L885-925` | — |
| `_hive` | function | `orchestrator/graph.py:L326-363` | — |
| `_summarize` | function | `orchestrator/graph.py:L252-288` | — |
| `apply_ticket_edits` | function | `orchestrator/graph.py:L592-628` | Accept the user's reworded release plan. Returns the state update (empty |
| `apply_requirement_edits` | function | `orchestrator/graph.py:L649-684` | Accept the user's reworded requirement. Returns the state update (empty if |
| `_post_stage_output` | function | `orchestrator/graph.py:L291-323` | — |
| `_context_for` | function | `orchestrator/graph.py:L218-249` | Hive background for a stage's prompt: standing human decisions |
| `requirement_node` | function | `orchestrator/graph.py:L392-422` | — |
| `kb_save_preview` | function | `orchestrator/graph.py:L765-793` | What saving the knowledge base WOULD do - shown when asking the user. |
| `_derive_run_outcome` | function | `orchestrator/graph.py:L971-998` | — |

## Relationships

- Depends on [claude_sdlc_architect (root files)](../root/overview.md) — `orchestrator/graph.py`
- Depends on [agents](../agents/overview.md) — `orchestrator/graph.py`
- Depends on [hive](../hive/overview.md) — `orchestrator/graph.py`
- Depends on [knowledge_base](../knowledge-base/overview.md) — `orchestrator/graph.py`
- Depends on [tools](../tools/overview.md) — `orchestrator/graph.py`
- Used by [claude_sdlc_architect (root files)](../root/overview.md) — `cli.py`

## Tests

- `[Verified]` imported by `test_design.py`
- `[Verified]` imported by `test_hive.py`
- `[Verified]` imported by `test_quality.py`

## Files

- `orchestrator/__init__.py`
- `orchestrator/graph.py`

<!-- manual:begin notes -->
<!-- manual:end notes -->
