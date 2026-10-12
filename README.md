# claude_sdlc_architect

The Streamlit UI in this package is branded **Blueprint** on screen
(page title, headings) -- this README keeps the package/directory name
throughout since that's what you actually `cd` into and import.

Turns a plain-language requirement into a **design** and a ready-to-file
**Jira backlog** using three agents, driven by **Claude Code itself** (via
the Claude Agent SDK). No `ANTHROPIC_API_KEY` or AWS credentials required:
it authenticates exactly like an interactive Claude Code session.

```
Requirement  →  Design  →  Release  →  (you approve)  →  Jira
                  │           └─ epic → stories → tasks → subtasks,
                  │              staged rollout plan, monitoring plan
                  └─ which applications change / are new, in what order,
                     test strategy, risks, and knowledge-base changes
```

| Agent | What it does |
|---|---|
| **Requirement** | Turns the raw request into a title, business goal, scope, acceptance criteria and exclusions. |
| **Design** | Decides what to build: affected and new applications, dependency order, contracts, test strategy, risks -- and what to add to the knowledge base. |
| **Release** | Writes the Jira epic with **stories, tasks and subtasks**, plus the rollout and monitoring plan. |

If the Design agent decides nothing needs to change, the run ends there
(`NO_CHANGE_REQUIRED`) and no Jira backlog is written.

## Choosing which agents run, and steering the design

You decide how much of the pipeline runs:

- **Start with** the Requirement agent (normal), or the **Design agent** -- the
  Requirement agent is skipped and your text is used as the requirement exactly as
  written (nothing is added or interpreted). Handy when the text is already precise.
- **Stop after** the Requirement agent (you just get the structured requirement),
  the Design agent (you get the design, and are still offered its knowledge-base
  documents, but no release plan or Jira backlog), or **Release** (everything).

**Architectural changes in between** -- you can give the Design agent your own
architectural direction (technology choices, services to reuse, things to avoid)
and it treats it as binding:

- type it in the form (*Architectural direction*) before you start, or
- in **Human approval** mode, enter it at the pause after the Requirement agent,
  once you have seen the requirement. (If you start at the Design agent there is
  nothing to review at that point, so the run goes straight to the design.)

The direction goes to the Design agent only. If it conflicts with the requirement
or the knowledge base, the agent follows it and records the conflict under its
risks. It also shows on the sidebar whiteboard, and changing it replaces the
earlier version rather than stacking. After the design you can still ask the Design
agent to revise (the *Answer* box), as before.

From the command line: `--start-at design`, `--stop-after requirement|design|release`,
and `--direction "..."` (or `@file`).

## Knowledge bases

A **knowledge base** is a folder of documents, one per application, that the
agents read to understand an existing system. Each sub-folder of
`knowledge_base/` is one knowledge base (the demo one is
`knowledge_base/streamly/`), so you can keep several -- one per system or
project. They are optional.

**Choosing one** -- when you start a run you pick either:

- **an existing knowledge base** -- the agents design against it, or
- **✚ Create something new** -- no knowledge base is shown to the agents and the
  run is a from-scratch build (below).

In the UI this is the *Knowledge base* picker; in the CLI use `--kb NAME` or
`--new-kb` (`--list-kbs` shows what exists; with exactly one knowledge base it is
used by default, with several you must choose).

**Saving at the end of the release** -- the Design agent only *proposes*
knowledge-base documents. Nothing is written until the very last step, when you
are asked:

- after a **Create something new** run: *"Save this as a new knowledge base?"*
  You see the documents, give the knowledge base a name (a suggestion is
  filled in), and choose **Save** or **Don't save**. A name that already exists
  is refused, so a new knowledge base can never be merged into an old one.
- after a run against an **existing** knowledge base: *"Add these to the knowledge
  base X?"* New applications become new documents; existing ones get a dated
  `## Update (date, run id)` section **appended** -- existing text is never
  rewritten or removed.

If you decline, nothing is written (the proposed documents stay in that run's
`history.json`). An answer is only valid for the design it was asked about, and a
problem saving is recorded in the result instead of failing the run.

### Create-something-new runs ignore every existing knowledge base

When the request is to build something **new, from scratch**, no existing
knowledge base may play any part. A run is treated that way when either:

- you choose **Create something new** (this always wins), or
- the Requirement agent classifies the request that way (`build_mode:
  "from_scratch"`) from its own wording -- even if you had selected an
  existing knowledge base. In that case what it designs is offered as a *new*
  knowledge base, never mixed into the one you selected.

From then on it is enforced in code, not just in the prompt:

- the Design agent, the Requirement agent (when you chose *Create something
  new*) and the chat helper are never shown any knowledge base, and past-run
  memory is not recalled into their prompts;
- the Design validation rejects any answer that names an existing application.

One limit: when the Requirement agent has to *decide* the mode itself, it sees a
one-line-per-application overview of the selected knowledge base while doing so.
Choose *Create something new* (or `--new-kb`) to withhold even that.

To try a run with no knowledge bases at all, point the root at an empty folder:

```bash
CLAUDE_SDLC_ARCHITECT_KB_ROOT=/tmp/my-kbs python3 cli.py \
    --requirement "Build a simple todo app with user accounts and reminders."
```

## Creating the tickets in a real Jira

By default the backlog is written as local files (`runs/<id>/jira_mock/`).
If Jira is configured, the pipeline then **asks permission** and, only if you
approve, creates the same backlog in your real Jira.

**Setup** -- copy `.env.example` to `.env` (git-ignored) and fill in:

```
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@company.com        # Jira Cloud only; leave empty on Data Center
JIRA_API_TOKEN=...                # API token (Cloud) / personal access token (Data Center)
JIRA_PROJECT_KEY=ARCH             # the PROJECT key (letters), not an issue key like ARCH-1
```

The account needs Browse Projects and Create Issues on that project, and the
project needs Epic, Story and Sub-task issue types.

**The permission step, and choosing which tickets** -- before anything is sent
you see the Jira site, project and account, and **tick exactly which tickets to
create**: the epic, each story, and each story's sub-tasks.

- In the UI everything starts ticked. Untick what you don't want, or use
  *Select all* / *Select none*; the counts update as you go, *Create selected
  tickets in Jira* stays disabled until something is ticked, and a sub-task
  can't be ticked without its story. Untick the epic to create the stories
  without one. *Skip* sends nothing.
- In the CLI the prompt numbers the stories: `a` creates everything, `1,3`
  creates those stories with all their sub-tasks and the epic, adding `x` leaves
  the epic out (`1,3,x`), and Enter -- or anything it doesn't understand --
  creates nothing. A non-interactive run counts as "nothing".

**Rewording the tickets** -- in the UI each ticket has an *Edit wording* section
(the epic, and each story with its sub-tasks). You can change titles,
descriptions, acceptance criteria and the steps inside a sub-task. Only words
can change: priority, points, labels, links, which stories exist and how many
tasks they have are fixed, and every ticket must keep a title. The page counts
your edits as you type. When you answer the Jira question -- Create *or* Skip --
the edits are saved to the plan **and the local files**, so the local files
always say what was sent to Jira, and what is sent is exactly the wording you
approved. (If a title is left empty, Create is disabled; Skip still works and
discards that edit.) The CLI does not offer editing yet.

Unselected tickets stay in the local files (`jira_mock/`) and the result page
lists what was not sent. Because progress is tracked against the whole plan,
you can send a few tickets now and the rest later without creating anything twice
(links between stories are added once both exist). Declining, or not configuring
Jira, sends nothing. An approval and a choice of tickets only cover the plan you
were shown: if the plan is revised, you are asked again.

**How the backlog maps onto Jira** -- Jira has three standard levels, the
pipeline produces four:

| Pipeline | Jira |
|---|---|
| Epic | Epic |
| Story | Story, under the epic |
| Task | **Sub-task** of its story |
| Subtask | a bullet under "Steps" in that sub-task's description |

"Blocked by" becomes a Jira *Blocks* link. The component is recorded as a
label (`component-<name>`) rather than a Jira Component, because components
must already exist. Every issue also gets a `arch-run-<run id>` label.

**Safety** -- publishing is idempotent: progress is saved in
`runs/<id>/jira_created.json`, so a retry after a failure resumes without
duplicating tickets, and re-publishing an unchanged plan is a no-op. Fields a
project does not accept (priority, story points) are left out and reported;
a required field the tool cannot fill gives a clear error. The token is never
logged, shown or included in error text. Failures keep the local files and
offer a retry. Outcomes: `RELEASED_TO_JIRA`, `JIRA_FAILED`, or
`APPROVED_RELEASED` (saved locally only).

## The hive (shared run memory)

Dependency-free and file-backed; both `cli.py` and the Streamlit UI get it
because it is wired into the shared nodes in `orchestrator/graph.py`.

- **Event log** -- `runs/<id>/events.jsonl`, append-only: stage
  start/finish/failure and human notes.
- **Blackboard** -- `runs/<id>/blackboard.json`: decisions, open
  questions, risks and *standing human decisions*, each tagged with
  stage/iteration/status. A human's note to a stage is re-injected into
  that stage's prompt on every later pass.
- **Cross-run memory** -- finished runs append to `memory/index.jsonl`
  (gitignored); similar past runs are recalled into the Requirement and
  Design prompts. Lexical (BM25), not embeddings; `memory.recall()` is the
  seam to swap. Runs that a human steered are pinned so condensing never
  folds them away.
- **Streamlit sidebar** -- blackboard sections and the event log,
  refreshed after every stage.

## Prerequisite: be logged into Claude Code

```bash
claude login
```

(If you're reading this from inside a Claude Code session, you're
already logged in -- nothing further to do.) The bundled CLI the SDK
drives reuses that same session; there's no separate API key to
configure. `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` also work if
set, but aren't required.

## Usage

```bash
cd claude_sdlc_architect
pip install -r requirements.txt

python3 cli.py --requirement "Notify users by email and push when a \
    new episode of a show they're watching becomes available, \
    respecting their existing notification preferences."

# or from a file
python3 cli.py --requirement @requirement.txt

# choose a model per agent (requirement, design, release)
python3 cli.py --requirement @requirement.txt --agent-model design=claude-opus-5-5

# pick a knowledge base, or create something new
python3 cli.py --list-kbs
python3 cli.py --requirement @requirement.txt --kb streamly
python3 cli.py --requirement @requirement.txt --new-kb

# UI -- choose a knowledge base (or create something new), and Agentic (unattended)
# or Human approval (pauses after each of the three stages)
streamlit run streamlit_app.py
```

Set `CLAUDE_SDLC_ARCHITECT_MODEL_ID` to use a different Claude model
(default `claude-sonnet-5`). Other settings: `CLAUDE_SDLC_ARCHITECT_KB_ROOT` (the folder holding the knowledge bases),
`CLAUDE_SDLC_ARCHITECT_RUNS_ROOT`, `CLAUDE_SDLC_ARCHITECT_MEMORY_DIR`, and the `JIRA_*` variables above.

## Desktop app

Blueprint can run in its own window instead of a browser tab. It is the same app:
a small Streamlit server starts in the background and the window shows it.

**Quick start (Mac or Linux, Python 3.10 or newer)**

```bash
git clone https://github.com/debeshwar-source/architect_sdlc.git
cd architect_sdlc
./run_desktop.sh
```

That is all. The first run creates a private environment in `.venv-desktop` and
installs what the app needs (a few minutes, needs internet); after that it just starts.
Closing the window stops the app.

Before the agents can work you need to sign in to Claude, in one of two ways:
- install Claude Code and run `claude login` once, or
- put `ANTHROPIC_API_KEY=your-key` in a file named `.env` in the project folder.

For Jira, copy `.env.example` to `.env` and fill it in (see "Creating the tickets in a
real Jira" above). `.env` is never committed.

If you would rather manage the environment yourself:

```bash
python3 -m venv .venv-desktop
.venv-desktop/bin/pip install -r requirements.txt -r requirements-desktop.txt
.venv-desktop/bin/python desktop.py
```

(Without `pywebview` installed it opens your normal browser instead.) On Windows the
equivalent is `py -m venv .venv-desktop`, then `.venv-desktop\Scripts\pip install -r
requirements.txt -r requirements-desktop.txt`, then `.venv-desktop\Scripts\python
desktop.py` -- this is untested; the Mac route is the one that has been tried.

**Build a double-clickable app**

```bash
.venv-desktop/bin/pip install -r requirements-build.txt   # once: adds the packager
./packaging/build_desktop.sh        # about a minute; creates dist/Blueprint.app
open dist/Blueprint.app             # or drag it to Applications
```

The build bundles Python, Streamlit, the pipeline and the Claude Code CLI that comes
with `claude-agent-sdk`, so the app is large (about 480 MB). It uses your existing
Claude login (`claude login`), exactly like the command-line version. After changing
the code, build again.

**Where its data lives** -- the app bundle is read-only, so the packaged app keeps
everything in `~/Library/Application Support/Blueprint/` (Mac): `runs/`, `memory/`,
`knowledge_bases/` (the demo one is copied in on first launch and never touched
again) and `desktop.log`. For Jira, copy `.env.example` in that folder to `.env` and
fill it in. Run from a checkout, nothing moves: data stays in the project folder.

**What it connects to** -- the screen is served only to this computer
(`127.0.0.1`, not the network), Streamlit's usage statistics are off, and it uses the
system font rather than downloading one. The only outside connections are the ones the
pipeline itself makes: Claude for the agents, and Jira when you approve creating
tickets. Links to Jira tickets open in your browser, not inside the window. Closing
the window stops the server (a force-quit does too, within a few seconds).

**Check a build** without opening a window:

```bash
dist/Blueprint.app/Contents/MacOS/Blueprint --self-test            # files, bundled Claude CLI, server
dist/Blueprint.app/Contents/MacOS/Blueprint --self-test --with-ai  # plus one tiny real call to Claude
```

**Limits** -- it is built for the kind of computer you build it on (Apple Silicon here;
Windows or Linux need their own build). It is not code-signed, so another Mac may warn
about an unidentified developer; signing and notarising need an Apple developer
account. There is no custom icon yet.

## Outputs

Everything for a run lands in `runs/<run_id>/`:

- `history.json` -- every stage's output
- `events.jsonl`, `blackboard.json` -- the hive
- `jira_created.json` -- progress of a real-Jira publish (only if you approved one)
- `jira_mock/` -- one `.md` and one `.json` per epic, story, task and
  subtask (e.g. `ARCH-…-S1.md`, `ARCH-…-S1-T1.md`, `ARCH-…-S1-T1-ST1.md`)

Run outcomes: `RELEASED_TO_JIRA`, `JIRA_FAILED`, `APPROVED_RELEASED` (local only), `NO_CHANGE_REQUIRED`, `DESIGN_ONLY` / `REQUIREMENT_ONLY` (you chose to stop early), `INCOMPLETE`.

## Demo data

`knowledge_base/streamly/*.md` describes **fictional**
microservices forming a small, coherent OTT streaming platform
("Streamly") -- written for this demo, not pulled from any real system.

## Tests

```bash
python3 test_hive.py       # the pipeline
python3 test_desktop.py    # the desktop launcher (starts a real local server)
```

No AI or network needed: covers memory, the knowledge-base create/augment
rules (listing, a new knowledge base only on approval, name collisions, no-overwrite, path safety), the real-Jira client against a
fake Jira (hierarchy, links, retries, no duplicates, token safety, approval), the Design and Release
validation, the Jira hierarchy, and a complete from-scratch pipeline run
with the AI replaced by canned answers.

## Layout

```
config.py                       runtime configuration (model id, paths)
llm.py                          Claude Agent SDK wrapper: call_llm_json() / call_llm_text()
agents/requirement.py           stage 1
agents/design.py                stage 2 (formerly "architecture")
agents/release.py               stage 3 -- Jira epic/stories/tasks/subtasks
agents/discuss.py               chat helper for the UI's approval gates
skills/*.md                     one system prompt per agent
knowledge_base/loader.py        lists, reads, creates and appends to knowledge bases
knowledge_base/<name>/            one folder per knowledge base (demo: streamly/)
hive/                           events, blackboard, cross-run memory
tools/issue_tracker.py          local mock Jira
tools/jira_client.py            real Jira over REST (only after approval)
orchestrator/graph.py           LangGraph wiring
cli.py, streamlit_app.py        entry points
desktop.py                      runs the app in its own window (see Desktop app)
packaging/build_desktop.sh      builds dist/Blueprint.app
run_desktop.sh                  one command: sets up and runs the desktop app
requirements-desktop.txt        extra package for the desktop window (pywebview)
requirements-build.txt          adds PyInstaller, only needed to build the app
```
