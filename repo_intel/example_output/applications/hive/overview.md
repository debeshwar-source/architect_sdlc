# hive

`[Verified]` 4 source file(s), 537 lines, 0 test file(s). Parent: [component catalog](../../architecture/component-catalog.md).

## What the code says about itself

`[Verified]` Module docstrings (author claims, not independently checked):

- `hive/__init__.py` — The hive: shared run memory for the pipeline.
- `hive/blackboard.py` — Shared blackboard: runs/<run_id>/blackboard.json.
- `hive/events.py` — Append-only event log: runs/<run_id>/events.jsonl.
- `hive/memory.py` — Cross-run memory: recall of similar past runs.

## Key symbols (largest first)

| Symbol | Kind | Location | Docstring |
|---|---|---|---|
| `condense` | function | `hive/memory.py:L190-250` | Shrinks the index to: pinned records (untouched) + one rolling |
| `_summarize` | function | `hive/memory.py:L147-187` | Deterministic roll-up of runs being folded away (no LLM: cheap, |
| `remember` | function | `hive/memory.py:L105-144` | Appends this run to memory. Skips runs that never produced a |
| `format_for_prompt` | function | `hive/memory.py:L289-323` | Turn the best matching past runs into a text block for the AI's |
| `recall` | function | `hive/memory.py:L253-286` | Find the `k` past runs most similar to `query` (the new request). |
| `emit` | function | `hive/events.py:L27-49` | Appends one event and returns it. Never raises -- observability |
| `format_for_prompt` | function | `hive/blackboard.py:L115-135` | Standing human decisions for `stage`, formatted for its prompt; |
| `post` | function | `hive/blackboard.py:L60-79` | Pin a new note under `section`. Ignored if the section name is |
| `read` | function | `hive/events.py:L52-64` | Read the whole diary back as a list of events (oldest first). |
| `_load` | function | `hive/memory.py:L71-83` | — |
| `supersede` | function | `hive/blackboard.py:L82-93` | Marks a stage's active entries superseded -- called just before |
| `read` | function | `hive/blackboard.py:L40-50` | Load the whiteboard. If the file is missing or damaged, return an |
| `_doc_text` | function | `hive/memory.py:L92-102` | — |
| `resolve_questions` | function | `hive/blackboard.py:L96-103` | Mark a stage's open questions as answered (a human replied). |
| `human_decisions_for` | function | `hive/blackboard.py:L106-112` | The still-valid instructions a human gave to this particular stage. |

## Relationships

- Depends on [claude_sdlc_architect (root files)](../root/overview.md) — `hive/blackboard.py`
- Used by [claude_sdlc_architect (root files)](../root/overview.md) — `desktop.py`
- Used by [orchestrator](../orchestrator/overview.md) — `orchestrator/graph.py`

## Tests

- `[Verified]` imported by `test_hive.py`

## Files

- `hive/__init__.py`
- `hive/blackboard.py`
- `hive/events.py`
- `hive/memory.py`

<!-- manual:begin notes -->
<!-- manual:end notes -->
