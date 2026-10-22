# tools

`[Verified]` 8 source file(s), 2912 lines, 1 test file(s). Parent: [component catalog](../../architecture/component-catalog.md).

## What the code says about itself

`[Verified]` Module docstrings (author claims, not independently checked):

- `tools/confluence_client.py` — Publishes the design document as a page in a REAL Confluence space, over Confluence's REST API.
- `tools/design_doc.py` — The Design agent's output as a design document: shown in the app, downloadable as Word
- `tools/diagrams.py` — Architecture diagrams, drawn as PNG images.
- `tools/fonts.py` — The one font the app draws with in PDFs and diagrams, and what to do with characters
- `tools/issue_tracker.py` — Jira-style epic > stories > tasks > subtasks, written as local mock
- `tools/jira_client.py` — Creates the release backlog in a REAL Jira project, over Jira's REST API.
- `tools/requirement_doc.py` — The Requirement agent's output as a document: shown on screen, and downloadable

## Key symbols (largest first)

| Symbol | Kind | Location | Docstring |
|---|---|---|---|
| `JiraClient` | class | `tools/jira_client.py:L195-565` | — |
| `architecture_diagram` | function | `tools/diagrams.py:L222-456` | The map of what is being built and how it connects, as PNG bytes. |
| `ConfluenceClient` | class | `tools/confluence_client.py:L125-308` | — |
| `build_model` | function | `tools/design_doc.py:L171-295` | The document as plain data: {"title", "meta", "blocks"}. Blocks are one of |
| `write_release` | function | `tools/issue_tracker.py:L164-281` | Save the epic and everything under it as files in `output_dir` |
| `to_docx` | function | `tools/requirement_doc.py:L126-228` | — |
| `to_docx` | function | `tools/design_doc.py:L458-554` | — |
| `to_pdf` | function | `tools/design_doc.py:L561-641` | — |
| `_Canvas` | class | `tools/diagrams.py:L71-149` | — |
| `flow_diagram` | function | `tools/diagrams.py:L576-648` | One flow as a sequence diagram: participants across the top, steps down the page. |
| `to_pdf` | function | `tools/requirement_doc.py:L259-322` | — |
| `_story_md` | function | `tools/issue_tracker.py:L45-90` | Field layout matches a real Jira Story's issue view: a details |
| `_fields_of` | function | `tools/design_doc.py:L70-113` | Every piece of wording that can be changed: path, label, kind ("line" = one line, |
| `build_order_diagram` | function | `tools/diagrams.py:L655-693` | The order things must land in, left to right and top to bottom. |
| `_epic_md` | function | `tools/issue_tracker.py:L121-152` | Mirrors a real Jira Epic's issue view: its own details panel, |

## Relationships

- Used by [claude_sdlc_architect (root files)](../root/overview.md) — `cli.py`
- Used by [orchestrator](../orchestrator/overview.md) — `orchestrator/graph.py`

## Tests

- `[Verified]` imported by `test_design.py`
- `[Verified]` imported by `test_documents.py`
- `[Verified]` imported by `test_hive.py`
- `[Verified]` imported by `test_quality.py`
- `[Verified]` imported by `test_ui.py`
- `[Verified]` imported by `tools/test_plan_doc.py`

## Files

- `tools/__init__.py`
- `tools/confluence_client.py`
- `tools/design_doc.py`
- `tools/diagrams.py`
- `tools/fonts.py`
- `tools/issue_tracker.py`
- `tools/jira_client.py`
- `tools/requirement_doc.py`

<!-- manual:begin notes -->
<!-- manual:end notes -->
