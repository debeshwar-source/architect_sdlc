"""Runtime configuration for the architecture-level SDLC orchestrator.

In plain words: this file is the "settings page" of the project. Every
other file imports it to find out which AI model to use, how many times
the plan may be revised, and where files should be read from / saved to.
Most settings can be changed without touching code by setting an
environment variable (a named setting that lives outside the program,
in your terminal).
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into the environment.

    Plain words: lets you keep settings and secrets (like the Jira token) in
    a local `.env` file instead of typing them into the terminal. Variables
    that are already set in the environment win, so a real environment
    variable always overrides the file. Blank lines and `#` comments are
    ignored, and matching quotes around a value are stripped. Values are
    never printed or logged."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Read the .env file(s) before any setting below is looked up. The files are
# git-ignored; see .env.example for the names. The first value found wins:
#   1. real environment variables
#   2. the app's data folder, if CLAUDE_SDLC_ARCHITECT_HOME is set (this is
#      where the packaged desktop app keeps the user's own settings, because
#      the app bundle itself is read-only)
#   3. the project folder (normal use from a checkout)
_home = os.environ.get("CLAUDE_SDLC_ARCHITECT_HOME")
if _home:
    _load_dotenv(Path(_home) / ".env")
_load_dotenv(Path(__file__).resolve().parent / ".env")

# The Claude Agent SDK talks to the bundled Claude Code CLI, which
# authenticates the same way an interactive Claude Code session does
# (a Claude subscription login via `claude login`, or ANTHROPIC_API_KEY /
# ANTHROPIC_AUTH_TOKEN if set) -- no separate API key is required.
#
# Plain words: the default AI model ("brain") used by every agent.
# os.environ.get(NAME, default) means: "use the environment variable
# NAME if the user set it, otherwise fall back to the default after the
# comma".
MODEL_ID = os.environ.get("CLAUDE_SDLC_ARCHITECT_MODEL_ID", "claude-sonnet-5")

# Models selectable per agent (see AGENT_NAMES below) in the Streamlit
# UI's "Model per agent" expander and the CLI's --agent-model flag --
# the current Claude model roster available via a Claude Code
# subscription login, same as MODEL_ID above.
#
# Plain words: the menu of AI models a user may pick from when they want
# a particular stage (e.g. architecture) to use a stronger or cheaper one.
AVAILABLE_MODELS = [
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1",
]

# The agents whose model is independently choosable, keyed by the
# same name each agents/<name>.py module is named after ("release"
# here is agents/release.py, invoked from the "release_planning" node).
#
# Plain words: the "specialist workers" in the pipeline
# (Requirement -> Design -> Quality Planner -> Release). Each one can be given its own AI model.
AGENT_NAMES = ["requirement", "design", "quality", "release"]

# ---- Folder locations ------------------------------------------------
# ROOT is the folder this file lives in, i.e. the project's top folder.
# (__file__ = this file's path; .resolve() makes it absolute;
# .parent goes up one level to the containing folder.)
ROOT = Path(__file__).resolve().parent
# Where the knowledge bases live. Each sub-folder is ONE knowledge base (for
# example knowledge_base/streamly/), and each .md file inside it describes one
# application. The user picks a knowledge base when starting a run, or chooses
# "create something new" -- and a new knowledge base is only added here when the
# user says so at the end of the release. Knowledge bases are OPTIONAL: with
# none, the agents design from scratch. See knowledge_base/loader.py.
KNOWLEDGE_BASES_ROOT = Path(
    os.environ.get("CLAUDE_SDLC_ARCHITECT_KB_ROOT", str(ROOT / "knowledge_base"))
)
# Escalation limits (agents/escalation.py): when an agent is unsure it asks the Engineering Manager,
# and the manager asks the user only if it cannot decide. Hard caps, so questions can never loop.
ESC_BOUNCES_PER_TASK = int(os.environ.get("CLAUDE_SDLC_ARCHITECT_ESC_BOUNCES_PER_TASK", "2"))
ESC_REASSIGNS_PER_TASK = int(os.environ.get("CLAUDE_SDLC_ARCHITECT_ESC_REASSIGNS_PER_TASK", "1"))
ESC_USER_QUESTIONS_PER_RELEASE = int(os.environ.get("CLAUDE_SDLC_ARCHITECT_ESC_USER_QUESTIONS", "3"))
ESC_EXTRA_CALLS_PER_RELEASE = int(os.environ.get("CLAUDE_SDLC_ARCHITECT_ESC_EXTRA_CALLS", "12"))
# Where each agent's instruction sheet ("skill", i.e. its system prompt) lives.
SKILLS_DIR = ROOT / "skills"
# Where every run's output files (logs, results, mock tickets) are saved.
RUNS_ROOT = Path(os.environ.get("CLAUDE_SDLC_ARCHITECT_RUNS_ROOT", str(ROOT / "runs")))

# Jira connection (optional). Read from the environment / .env at the moment
# of use by tools/jira_client.py, so the token is never stored in this module.
# JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY -- see .env.example.

# Cross-run memory (hive/memory.py): one index.jsonl of finished runs.
# Plain words: a folder where summaries of past runs are kept so future
# runs can learn from similar earlier ones.
MEMORY_DIR = Path(os.environ.get("CLAUDE_SDLC_ARCHITECT_MEMORY_DIR", str(ROOT / "memory")))
