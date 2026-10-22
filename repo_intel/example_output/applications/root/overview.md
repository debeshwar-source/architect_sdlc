# claude_sdlc_architect (root files)

`[Verified]` 8 source file(s), 3777 lines, 8 test file(s). Parent: [component catalog](../../architecture/component-catalog.md).

## What the code says about itself

`[Verified]` Module docstrings (author claims, not independently checked):

- `cli.py` — CLI entrypoint for the SDLC planning orchestrator.
- `config.py` — Runtime configuration for the architecture-level SDLC orchestrator.
- `desktop.py` — Blueprint as a desktop app: the same Streamlit UI, shown in its own window.
- `llm.py` — Claude Agent SDK client.
- `run_log.py` — Shared run-log dumping, used by both cli.py and streamlit_app.py.
- `session_store.py` — Saving and reopening sessions.
- `streamlit_app.py` — Blueprint: the web page (UI) for the SDLC architect.

## Entry points

- `[Verified]` `cli.py` has an `if __name__ == "__main__"` block
- `[Verified]` `desktop.py` has an `if __name__ == "__main__"` block

## Key symbols (largest first)

| Symbol | Kind | Location | Docstring |
|---|---|---|---|
| `main` | function | `cli.py:L352-568` | Run the program. Returns 0 on success, 1 on failure (the standard |
| `self_test` | function | `desktop.py:L318-453` | Check the install works, without opening a window. Prints one line per |
| `render_stage_body` | function | `streamlit_app.py:L1040-1148` | Write one stage's detailed content. Must be called from inside |
| `_print_node_update` | function | `cli.py:L252-349` | — |
| `render_design_document` | function | `streamlit_app.py:L528-608` | The Design agent's output as a design document: overview, architecture diagram, tables of |
| `render_quality_editor` | function | `streamlit_app.py:L649-720` | Boxes to edit the test plan: its summary and every field of every test case, plus removing |
| `stage_label` | function | `streamlit_app.py:L960-1030` | The informative, final label for a completed stage -- used as |
| `call_llm_json` | function | `llm.py:L97-160` | `model`, if given, overrides config.MODEL_ID for this call only |
| `render_story_tree` | function | `streamlit_app.py:L909-957` | — |
| `render_quality_plan` | function | `streamlit_app.py:L723-769` | The Quality Planner's test plan: an approach summary, the test cases as a table, and which |
| `render_design_wording_editor` | function | `streamlit_app.py:L480-525` | Boxes to reword the design document: its title, overview and every description, label and |
| `run` | function | `desktop.py:L267-311` | Start the server and show the app in a window (or the browser). |
| `render_requirement_editor` | function | `streamlit_app.py:L420-461` | Boxes to reword the requirement: title, scope, acceptance criteria, out of scope. |
| `render_jira_result` | function | `streamlit_app.py:L866-906` | — |
| `render_requirement_document` | function | `streamlit_app.py:L811-850` | The Requirement agent's output as a document: shown here, and downloadable as |

## Relationships

- Depends on [agents](../agents/overview.md) — `desktop.py`
- Depends on [hive](../hive/overview.md) — `desktop.py`
- Depends on [knowledge_base](../knowledge-base/overview.md) — `cli.py`
- Depends on [orchestrator](../orchestrator/overview.md) — `cli.py`
- Depends on [tools](../tools/overview.md) — `cli.py`
- Used by [agents](../agents/overview.md) — `agents/_common.py`
- Used by [hive](../hive/overview.md) — `hive/blackboard.py`
- Used by [knowledge_base](../knowledge-base/overview.md) — `knowledge_base/loader.py`
- Used by [orchestrator](../orchestrator/overview.md) — `orchestrator/graph.py`

## Configuration read from the environment

| Variable | Read at |
|---|---|
| `CLAUDE_SDLC_ARCHITECT_HOME` | `config.py:L44` |
| `NAME` | `config.py:L55` |
| `CLAUDE_SDLC_ARCHITECT_MODEL_ID` | `config.py:L58` |
| `CLAUDE_SDLC_ARCHITECT_KB_ROOT` | `config.py:L94` |
| `CLAUDE_SDLC_ARCHITECT_RUNS_ROOT` | `config.py:L99` |
| `CLAUDE_SDLC_ARCHITECT_MEMORY_DIR` | `config.py:L108` |
| `APPDATA` | `desktop.py:L99` |
| `XDG_DATA_HOME` | `desktop.py:L100` |
| `PARENT_PID_ENV` | `desktop.py:L212` |
| `BLUEPRINT_PARENT_PID` | `streamlit_app.py:L66` |

Catalog: [configuration](../../operations/configuration.md)

## Tests

- `[Verified]` imported by `conftest.py`
- `[Verified]` imported by `test_design.py`
- `[Verified]` imported by `test_desktop.py`
- `[Verified]` imported by `test_hive.py`
- `[Verified]` imported by `test_quality.py`
- `[Verified]` imported by `test_sessions.py`
- `[Verified]` imported by `test_ui.py`

## Files

- `cli.py`
- `config.py`
- `desktop.py`
- `llm.py`
- `run_desktop.sh`
- `run_log.py`
- `session_store.py`
- `streamlit_app.py`

<!-- manual:begin notes -->
<!-- manual:end notes -->
