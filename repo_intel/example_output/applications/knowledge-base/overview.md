# knowledge_base

`[Verified]` 2 source file(s), 312 lines, 0 test file(s). Parent: [component catalog](../../architecture/component-catalog.md).

## What the code says about itself

`[Verified]` Module docstrings (author claims, not independently checked):

- `knowledge_base/loader.py` — Loads the pre-existing architecture knowledge base: one .md file per

## Key symbols (largest first)

| Symbol | Kind | Location | Docstring |
|---|---|---|---|
| `apply_updates` | function | `knowledge_base/loader.py:L194-238` | Apply the Design agent's `knowledge_base_updates` list. Each item: |
| `load_all` | function | `knowledge_base/loader.py:L65-86` | Read every .md file in ONE knowledge-base folder, one per application. |
| `existing_dependency_edges` | function | `knowledge_base/loader.py:L289-310` | (caller, callee) edges for the architecture as it exists TODAY, |
| `_extract_field` | function | `knowledge_base/loader.py:L47-62` | — |
| `augment_doc` | function | `knowledge_base/loader.py:L179-191` | Append a dated section to an EXISTING document. Existing text is |
| `list_knowledge_bases` | function | `knowledge_base/loader.py:L100-111` | Every knowledge base under the root: each sub-folder is one. Hidden and |
| `create_knowledge_base` | function | `knowledge_base/loader.py:L141-150` | Create a NEW, empty knowledge base. Refuses a name that is already |
| `create_doc` | function | `knowledge_base/loader.py:L167-176` | Create a NEW application document. Refuses to overwrite an |
| `suggest_kb_name` | function | `knowledge_base/loader.py:L124-132` | A folder-safe knowledge base name based on a project title, made unique |
| `kb_path` | function | `knowledge_base/loader.py:L114-121` | The folder of the existing knowledge base called `name`. Only names that |
| `_safe_name` | function | `knowledge_base/loader.py:L157-164` | — |
| `format_full_kb_for_prompt` | function | `knowledge_base/loader.py:L271-278` | Full knowledge base text -- for agents that need to reason |
| `ApplicationDoc` | class | `knowledge_base/loader.py:L39-44` | — |
| `format_overview_for_prompt` | function | `knowledge_base/loader.py:L263-268` | Cheap, one-line-per-app summary -- for agents that just need to |
| `get_doc` | function | `knowledge_base/loader.py:L281-286` | — |

## Relationships

- Depends on [claude_sdlc_architect (root files)](../root/overview.md) — `knowledge_base/loader.py`
- Used by [claude_sdlc_architect (root files)](../root/overview.md) — `cli.py`
- Used by [agents](../agents/overview.md) — `agents/design.py`
- Used by [orchestrator](../orchestrator/overview.md) — `orchestrator/graph.py`

## Tests

- `[Verified]` imported by `test_design.py`
- `[Verified]` imported by `test_hive.py`
- `[Verified]` imported by `test_sessions.py`
- `[Verified]` imported by `test_ui.py`

## Files

- `knowledge_base/__init__.py`
- `knowledge_base/loader.py`

<!-- manual:begin notes -->
<!-- manual:end notes -->
