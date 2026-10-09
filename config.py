"""Runtime configuration for the architecture-level SDLC orchestrator."""

import os
from pathlib import Path

# The Claude Agent SDK talks to the bundled Claude Code CLI, which
# authenticates the same way an interactive Claude Code session does
# (a Claude subscription login via `claude login`, or ANTHROPIC_API_KEY /
# ANTHROPIC_AUTH_TOKEN if set) -- no separate API key is required.
MODEL_ID = os.environ.get("CLAUDE_SDLC_ARCHITECT_MODEL_ID", "claude-sonnet-5")

# Models selectable per agent (see AGENT_NAMES below) in the Streamlit
# UI's "Model per agent" expander and the CLI's --agent-model flag --
# the current Claude model roster available via a Claude Code
# subscription login, same as MODEL_ID above.
AVAILABLE_MODELS = [
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1",
]

# The 6 agents whose model is independently choosable, keyed by the
# same name each agents/<name>.py module is named after ("release"
# here is agents/release.py, invoked from the "release_planning" node).
AGENT_NAMES = [
    "requirement", "architecture", "quality",
    "implementation_spec", "review", "release",
]

# Cross-repo planning has no live-testing loop (there's no code to run
# yet), just a review -> revise cycle -- kept short since each pass is
# a real LLM call across the whole knowledge base.
MAX_REVIEW_ITERATIONS = 3

ROOT = Path(__file__).resolve().parent
KNOWLEDGE_BASE_DIR = Path(
    os.environ.get("CLAUDE_SDLC_ARCHITECT_KB_DIR", str(ROOT / "knowledge_base" / "applications"))
)
SKILLS_DIR = ROOT / "skills"
RUNS_ROOT = Path(os.environ.get("CLAUDE_SDLC_ARCHITECT_RUNS_ROOT", str(ROOT / "runs")))

# Cross-run memory (hive/memory.py): one index.jsonl of finished runs.
MEMORY_DIR = Path(os.environ.get("CLAUDE_SDLC_ARCHITECT_MEMORY_DIR", str(ROOT / "memory")))
