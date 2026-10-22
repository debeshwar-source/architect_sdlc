# Component catalog

`[Verified]` Components are nested-manifest directories, else top-level folders. The last column is the first module docstring found (`[Verified]` text, not a judgement of responsibility).

| Component | Source files | Lines | Test files referencing it | Docstring |
|---|---|---|---|---|
| [claude_sdlc_architect (root files)](../applications/root/overview.md) | 8 | 3777 | 7 | CLI entrypoint for the SDLC planning orchestrator. |
| [tools](../applications/tools/overview.md) | 8 | 2912 | 6 | Publishes the design document as a page in a REAL Confluence space, over Confluence's REST API. |
| [agents](../applications/agents/overview.md) | 7 | 1093 | 4 | Shared helpers for architecture-level agents. |
| [hive](../applications/hive/overview.md) | 4 | 537 | 1 | The hive: shared run memory for the pipeline. |
| [knowledge_base](../applications/knowledge-base/overview.md) | 2 | 312 | 4 | Loads the pre-existing architecture knowledge base: one .md file per |
| [orchestrator](../applications/orchestrator/overview.md) | 2 | 1075 | 3 | SDLC planning pipeline, wired as a LangGraph state machine. |
| [packaging](../applications/packaging/overview.md) | 1 | 47 | none | `[Unknown]` |

See also: [dependency map](dependency-map.md).

<!-- manual:begin notes -->
<!-- manual:end notes -->
