# claude_sdlc_architect

The Streamlit UI in this package is branded **Blueprint** on screen
(page title, headings) -- this README keeps the package/directory name
throughout since that's what you actually `cd` into and import.

The same architecture-level pipeline as `../sdlc_architect/` --
Requirement → Architecture → Quality → Implementation Spec → Review →
Release, reasoning over a pre-loaded knowledge base of known
applications (no live repo) -- but driven by **Claude Code itself**
(via the Claude Agent SDK) instead of Amazon Bedrock. No
`ANTHROPIC_API_KEY` or AWS credentials required: it authenticates
exactly like an interactive Claude Code session.

## Why this exists, and how it differs from `sdlc_architect`

`sdlc_architect` calls Bedrock's Converse API directly, with a
hand-rolled JSON-repair loop (regex-extract a JSON object from the
response text, and if it doesn't parse or is missing a required key,
send a follow-up "repair this" prompt).

This system instead drives the Claude Agent SDK's `query()` -- the
same engine that runs this Claude Code CLI. Since this pipeline
reasons purely over pre-loaded knowledge base text (no live repo to
explore), no tools are involved at all here, unlike
`../claude_native_sdlc/`'s conversion of `../ai_native_sdlc/`: every
call is a single structured request, and `output_format` gets
schema-validated JSON back directly from the CLI, so there's no
regex extraction or text-based repair loop to maintain.

**Every agent file started byte-for-byte identical to `sdlc_architect`'s**
and still shares its knowledge base (`knowledge_base/applications/*.md`)
and `orchestrator/graph.py` (the same LangGraph wiring, including the
human-in-the-loop revision cycle). The agents, skills, and Streamlit UI
have since diverged from `sdlc_architect`: this system's agents each
return a `clarifying_questions` field (genuine ambiguities, with
concrete options and a recommendation) alongside their normal output,
and its Streamlit UI's Human approval mode puts those questions to the
reviewer directly instead of only offering a free-text note. The
Streamlit UI also got a couple of things borrowed from
`../claude_native_sdlc/`'s: a "Reject & stop" option at every approval
gate (alongside Approve and Send my answers), and a live-updating
status label -- while a stage's LLM call is in flight, it rotates
through a Claude-Code-style whimsical verb + what it's plausibly
chewing on ("Spelunking the knowledge base…"), so a stage that takes
tens of seconds doesn't look frozen. If you want any of this mirrored
back into `sdlc_architect`, it isn't automatic -- ask for it
explicitly. The two systems are otherwise independent; this one
doesn't import from the other.

## The hive (shared run memory)

Borrowed in spirit from agent-orchestration platforms (e.g. Munder
Difflin), scaled to what a tool-less, single-pass pipeline needs.
`hive/` is dependency-free and file-backed; both `cli.py` and the
Streamlit UI get it because it is wired into the shared nodes in
`orchestrator/graph.py`.

- **Event log** -- `runs/<id>/events.jsonl`, append-only: stage
  start/finish/failure, human notes, circuit-breaker trips.
- **Blackboard** -- `runs/<id>/blackboard.json`: decisions, open
  questions, risks and *standing human decisions*, each tagged with
  stage/iteration/status. A human's note to a stage is re-injected into
  that stage's prompt on every later pass, so an automated
  Review→Architecture retry no longer forgets it.
- **Circuit breaker** (`hive/breaker.py`) -- watches the automated
  retry loop for a BLOCKING finding that keeps recurring: **steer**
  (try a different approach) → **constrain** (scope frozen; also fires
  immediately if the plan grew while findings recurred) → **stop**
  (outcome `BREAKER_TRIPPED`). Note: with `MAX_REVIEW_ITERATIONS = 3`
  the iteration cap usually ends the run before `stop` is reached;
  steer/constrain are what actually act. Raise the cap to give `stop`
  room.
- **Cross-run memory** -- finished runs append to `memory/index.jsonl`
  (gitignored); similar past runs are recalled into the Requirement and
  Architecture prompts. Lexical (BM25), not embeddings; `memory.recall()`
  is the seam to swap.
- **Streamlit sidebar** -- live breaker badge, blackboard sections and
  event log, refreshed after every stage.

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

# UI -- choose Agentic (unattended) or Human approval (per-stage
# clarifying questions + free-text suggestions) mode in the form
streamlit run streamlit_app.py
```

Set `CLAUDE_SDLC_ARCHITECT_MODEL_ID` to use a different Claude model
(default `claude-sonnet-5`).

## Demo data

Same as `sdlc_architect`: `knowledge_base/applications/*.md` describes
seven **fictional** microservices forming a small, coherent OTT
streaming platform ("Streamly") -- written for this demo, not pulled
from any real system.

## Layout

```
config.py                       runtime configuration (model id, paths -- no AWS/API-key config)
llm.py                          Claude Agent SDK wrapper: call_llm_json(), same signature as the Bedrock build
knowledge_base/applications/*.md   one doc per known application (demo data, unchanged)
knowledge_base/loader.py          reads the knowledge base (unchanged)
skills/*.md                       one skill (system prompt) per agent (unchanged)
agents/*.py                       the 6 architecture-level agents (byte-for-byte unchanged)
tools/issue_tracker.py            Jira-style epic + tickets, local mock (unchanged)
orchestrator/graph.py             LangGraph wiring (unchanged, including HITL suggest-changes)
cli.py                            entrypoint
runs/<run_id>/                    history.json + jira_mock/ (gitignored, created on demand)
```
