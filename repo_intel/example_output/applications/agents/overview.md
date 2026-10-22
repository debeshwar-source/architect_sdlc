# agents

`[Verified]` 7 source file(s), 1093 lines, 0 test file(s). Parent: [component catalog](../../architecture/component-catalog.md).

## What the code says about itself

`[Verified]` Module docstrings (author claims, not independently checked):

- `agents/_common.py` — Shared helpers for architecture-level agents.
- `agents/design.py` — Design Agent -- the core planning role (formerly the "Architecture Agent").
- `agents/discuss.py` — Discussion Assistant (architecture level) -- advisory chat, not a
- `agents/quality.py` — Quality Planner Agent -- the stage after Design: a test plan in plain English.
- `agents/release.py` — Engineering Manager Agent (module name: release) -- the final stage: the Jira epic, stories, tasks and subtasks.
- `agents/requirement.py` — Requirement Agent (architecture level).

## Key symbols (largest first)

| Symbol | Kind | Location | Docstring |
|---|---|---|---|
| `_validate_architecture` | function | `agents/design.py:L185-266` | The components / connections / flows must be complete and consistent with the rest |
| `run` | function | `agents/release.py:L37-114` | — |
| `_validate` | function | `agents/release.py:L123-196` | Reject incomplete or inconsistent release plans (raises ValueError, |
| `run` | function | `agents/requirement.py:L48-119` | — |
| `run` | function | `agents/design.py:L70-128` | — |
| `validate_clarifying_questions` | function | `agents/_common.py:L114-167` | Shared shape check for the `clarifying_questions` field every |
| `_validate` | function | `agents/design.py:L131-178` | Reject self-contradicting answers (raises ValueError, which makes |
| `run` | function | `agents/quality.py:L35-82` | — |
| `_clean_architecture` | function | `agents/design.py:L269-308` | Tidy the architecture in place (the checks above have passed): trim text, fill in |
| `reply` | function | `agents/discuss.py:L74-110` | Return the assistant's reply to the human's latest chat message. |
| `_validate` | function | `agents/quality.py:L85-120` | Reject an incomplete or inconsistent test plan (raises ValueError, which makes the AI |
| `format_feedback_section` | function | `agents/_common.py:L53-81` | `context_notes` is pre-formatted background from the hive |
| `_clean_kb_updates` | function | `agents/design.py:L311-339` | Tidy the proposed knowledge-base updates, in place. Like the |
| `_format_context` | function | `agents/discuss.py:L39-64` | Bundle the knowledge base and every finished stage's result into |
| `format_dict` | function | `agents/_common.py:L30-50` | Serializes a PRIOR stage's already-finished output for inclusion |

## Relationships

- Depends on [claude_sdlc_architect (root files)](../root/overview.md) — `agents/_common.py`
- Depends on [knowledge_base](../knowledge-base/overview.md) — `agents/design.py`
- Used by [claude_sdlc_architect (root files)](../root/overview.md) — `desktop.py`
- Used by [orchestrator](../orchestrator/overview.md) — `orchestrator/graph.py`

## Tests

- `[Verified]` imported by `test_design.py`
- `[Verified]` imported by `test_hive.py`
- `[Verified]` imported by `test_quality.py`
- `[Verified]` imported by `tools/test_plan_doc.py`

## Files

- `agents/__init__.py`
- `agents/_common.py`
- `agents/design.py`
- `agents/discuss.py`
- `agents/quality.py`
- `agents/release.py`
- `agents/requirement.py`

<!-- manual:begin notes -->
<!-- manual:end notes -->
