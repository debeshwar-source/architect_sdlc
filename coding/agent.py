"""The coding agent: plans a task into small steps, then carries out one step at a time, asking the user when in doubt.

Plain words: unlike the planning agents, this one can read files, and (when carrying out a step) edit them and run
commands, but only inside its own worktree (see workspace.py). It has two jobs:

  plan()     read-only: understand the task and write numbered, granular steps. Edits are impossible here.
  execute()  carry out ONE frozen step, then stop so the user can look at the change.

Every turn ends with a small JSON answer. Either the result, or a "question" for the user; the caller shows the
question, then calls again with the answer and the returned session_id so the agent keeps everything it has read.
"""

import asyncio
import re
import time
from typing import Any, Callable, Dict, List, Optional

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
                              query)

import activity
import config
import token_log
from coding import testrun
from agents._common import load_skill
from agents.catalog import ROLES, prompt_block

MAX_TURNS = 80
_ENV = {"BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "900000"}      # a build or test run may take minutes
MANAGER_TURNS = 100           # Engineering Manager 2 explores on need, so it gets more room than the coding agent
MAX_STEPS = 12

PLAN_RULES = """
You are Engineering Manager 2. You are given ONE task from the first Engineering Manager's backlog, and the repository
(your working directory). Your job: explore the repository and break the task into subtasks so exact that the coding agent
who receives each one does NOT need to explore it: it gets only your instructions and context.

You can only read; you cannot edit, run commands or change anything, and you must not try.

How to work
- Explore the way a developer would: search (Glob, Grep) and open files as the task needs them. Do not read the whole
  repository; start from what the task names and follow the code outward (callers, callees, the closest existing example of
  what is being added, how it is tested and configured). Stop as soon as you can write exact instructions.
- Find out the build and test commands (from the build files, README or CI config) and the conventions the change must follow.
- Verify before you write: every file, class, method, field and endpoint you name must exist (or be marked as new).

Subtasks
- 2-8 of them, ordered by dependency (data/model before API before UI), each leaving the repository working and small
  enough to review as one diff. Each subtask includes its own unit tests (the coder writes them; the tests are run for it).
- THE DIVISION OF LABOUR. You decide WHAT and WHERE; the coding agent decides HOW. You specify: which files to create or
  change and why; the contract (names of classes, methods and endpoints; request and response shape in words; status and
  error behavior; validation rules; edge cases); which existing file or method to follow as the pattern; what to test.
  You leave to the coder: imports, annotations, field and method declarations, method bodies, formatting and boilerplate.
  The coder is a capable engineer who can read the pattern file you point to.
- Keep each subtask's `instructions` to about 150 words. If you need more, the subtask is too big: split it.
- Never write code, not even as inline code. No pasted files, classes or methods; no declarations such as fields or
  annotations. Inline code is for identifiers only (a class, method, table, endpoint or config name).
  BAD:  "Add `@Column(name = "code", nullable = false) private String code;`"
  GOOD: "AccessCoupon entity (table access_coupon, extends BaseEntity like UserOwnership): non-null code, owning user, status,
         validity in days, redeemed-at; generate its id the way UserOwnership does."
- For each: `title`; `agent` (the best expert for its files, from the list given); `instructions` (imperative steps in
  words: what to add or change, the names involved, status codes, error and edge cases, and which existing file or method
  to copy the pattern from -- enough to implement without exploring, but not the implementation); `changes` (each file: `path`, `action` modify|create|delete, `what`); `context` (the facts from the
  repository the coder would otherwise have to read: signatures to call, imports, the pattern to copy and where it lives,
  config keys, naming and style rules); `tests` (which unit tests to write: the cases for the success, failure and edge paths, and the test file or pattern to follow);
  `check` (how to know it works).
- `repo_notes`: build conventions that apply to every subtask (short). Include how the project is built and tested and any
  flags or plugins that make that fail or run slowly offline (for example a download or formatter plugin in the build file
  that needs the network and the flag or property that skips it), so the coder never has to find out by trial and error.
- `test_command`: the ONE command that runs this repository's unit tests (from the build files, README or CI config), as a
  single command with no pipes or chaining, for example "./mvnw test" or "python3 -m pytest -q". A subtask may carry its own
  narrower `test_command` (for example one test class) to run only its tests; leave it empty to use the general one.
- `learnings`: up to 10 durable facts about this repository that would help with FUTURE tasks too (build/test commands,
  conventions, how a kind of thing is done and where an example lives, where things are, gotchas). Each: `text` (one
  sentence) and `files` (the paths it rests on, so it can be re-checked when they change). Nothing specific to this task.
- If a section "WHAT YOU REMEMBER ABOUT THIS REPOSITORY" is given, use it to skip exploring what it already tells you.
  Trust facts marked fresh; re-check the ones marked CHECK before relying on them.
- Do not plan beyond the task. Never invent requirements, files or APIs. If something critical is unknown or ambiguous in a
  way that changes the plan, do NOT guess: answer with status "question" and ask ONE concise question with 2-4 options and
  your recommended option.
- If the user gives feedback on an earlier version, produce the complete revised set.

Answer with JSON only: {"status": "plan" | "question", "summary": "...", "repo_notes": "...", "test_command": "...", "learnings": [{"text": "...", "files": ["..."]}], "steps": [{"title": "...",
"agent": "...", "instructions": "...", "changes": [{"path": "...", "action": "modify", "what": "..."}], "context": "...",
"tests": "...", "test_command": "...", "check": "..."}], "question": {"question": "...", "options": ["..."], "recommended_option": "..."}}.
Include "steps" when status is "plan", and "question" only when status is "question".
"""

EXEC_RULES = """
You are carrying out ONE step of an agreed plan in the repository that is your working directory. Follow this procedure
exactly, and nothing more:

1. READ only the files the step names: the ones to change and the pattern files it points to. Do not explore beyond them.
2. WRITE THE CODE the step describes. The step says what and where; how is your job. Match the repository's language
   level, style, formatter and naming. Smallest change that fully meets the step; no unrelated refactoring; no secrets;
   no new dependencies unless the step needs them.
3. WRITE THE UNIT TESTS (unless this turn says the tests come later) for exactly that change: the success path, the failure paths and the edge cases the step
   mentions, in the repository's own test style and location (follow the test pattern file if the step names one).
4. CHECK, THEN ANSWER. You have a shell (Bash) in the working directory, as in Claude Code: use it to compile or build and to check
   that your work holds. Run the unit tests yourself ONLY when this turn says you may; the user approves the tests first,
   and the program runs them for the report. Never run git commands that change history; the program does the commits.

- Never go looking for task, plan or instruction files (TASK, STEP, PLAN, .claude, markdown notes): everything you were
  given is in this conversation. If something you need is missing from it, ask (status "question") instead of searching.
- Work only inside the working directory, on its own branch. Never commit, push, or touch Git.
- If the step comes with instructions and context from Engineering Manager 2, follow them exactly. They were written from
  an exploration of the repository, so they say what and where; if they turn out to be wrong or incomplete, ask rather
  than improvising.
- If something is genuinely ambiguous in a way that changes the result and the USER can answer it, do NOT guess: finish with
  status "question" and ask ONE concise question with 2-4 options and your recommended option.
- If the step cannot be completed until something else exists or happens (another task's code is not there yet, a service,
  table or setting is missing, someone else must decide), do not work around it: finish with status "blocked", a
  `blocked_reason` (one or two sentences: what is missing and why this step needs it) and `waiting_for`.
- If the user asks for changes to your work, change what they asked and keep the rest as it is.
- When told that tests fail, fix them. On the LAST attempt, for every failure you cannot fix, list it in `unresolved` with
  the test's name and the real reason it still fails.

Your final answer is JSON only: {"status": "done" | "question" | "blocked", "summary": "...",
"tests_written": [{"file": "...", "description": "..."}], "test_command": "...", "assumptions": ["..."], "unresolved": [{"test": "...", "reason": "..."}],
"question": {"question": "...", "options": ["..."], "recommended_option": "..."}, "blocked_reason": "...", "waiting_for": "..."}.
Include "question" only when status is "question" and the blocked fields only when status is "blocked"; include "summary"
always (what you changed, or what you did so far, in two or three sentences).
"""

READ_ONLY = ["Read", "Glob", "Grep"]
ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]    # the coder has a shell like Claude Code; every Bash call and test run is recorded
# Commands the agent must not run: anything that changes Git history/branches/remotes, reaches the network,
# or escalates. Edits are also confined to the working directory by the permission mode.
DISALLOWED = [
    "Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git branch:*)",
    "Bash(git reset:*)", "Bash(git remote:*)", "Bash(git config:*)", "Bash(git worktree:*)", "Bash(git clean:*)",
    "Bash(git stash:*)", "Bash(git rebase:*)", "Bash(git merge:*)",
    "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(sudo:*)", "WebFetch", "WebSearch",
    "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)", "Bash(rm -rf ..:*)", "Bash(rm -rf $HOME:*)", "Bash(chmod -R:*)", "Bash(chown:*)", "Bash(kill:*)",
    "Bash(pkill:*)", "Bash(killall:*)", "Bash(shutdown:*)", "Bash(reboot:*)",
]

_QUESTION = {
    "type": "object",
    "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
                   "recommended_option": {"type": "string"}},
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": ["plan", "question"]},
        "summary": {"type": "string"},
        "repo_notes": {"type": "string"},
        "test_command": {"type": "string"},
        "learnings": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}}},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "agent": {"type": "string"}, "instructions": {"type": "string"},
            "changes": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "action": {"enum": ["modify", "create", "delete"]}, "what": {"type": "string"}}}},
            "context": {"type": "string"}, "tests": {"type": "string"}, "test_command": {"type": "string"},
            "check": {"type": "string"}}}},
        "question": _QUESTION,
    },
    "required": ["status", "summary"],
}
EXEC_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": ["done", "question", "blocked"]},
        "blocked_reason": {"type": "string"},
        "waiting_for": {"type": "string"},
        "summary": {"type": "string"},
        "tests_written": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string"}, "description": {"type": "string"}}}},
        "unresolved": {"type": "array", "items": {"type": "object", "properties": {
            "test": {"type": "string"}, "reason": {"type": "string"}}}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "test_command": {"type": "string"},
        "question": _QUESTION,
    },
    "required": ["status", "summary"],
}

# Added to the prompt in autonomous mode: the coder does everything a Claude Code session would; the user reviews at the end.
AUTO_MODE = ("\n\nHOW TO WORK THIS TURN: do the whole subtask yourself, the way you would in Claude Code. Write the code; write its unit tests; "
             "set up whatever the tests need to run here (for example create a virtual environment and install the dependencies); run the "
             "unit tests with your shell; read the failures and fix them; repeat until they pass or you are genuinely stuck. Do not stop "
             "to ask for approval: the user reviews the code, the tests and their results once, at the end. This turn overrides the earlier "
             "limit on running tests. Ask a question (status \"question\") only if something is ambiguous in a way you cannot settle by "
             "reading the repository. In your answer give `test_command`: the one command that runs this subtask's tests, exactly as it "
             "worked for you (a virtual environment's own interpreter is fine, for example `.venv/bin/python -m pytest -q`; if the tests need an environment variable "
             "such as JAVA_HOME pointing at a newer JDK, write it in front: `JAVA_HOME=/path/to/jdk ./mvnw test`; no pipes, no chaining), "
             "and list every test file you wrote in `tests_written`. If tests still fail at the end, say which and why in `unresolved`.")

# Added to the prompt while the user approves the code before any test is written.
CODE_ONLY = ("\n\nTHIS TURN: write only the production code of the step. Do NOT write any test file yet: the user reviews the code "
             "first, and you write the unit tests in a later turn once they approve it. You may compile or build to check the code; there "
             "are no tests to run yet.")


class Incomplete(Exception):
    """The agent stopped without a usable answer (out of turns, an error...). `session_id` lets it be resumed."""

    def __init__(self, message: str, session_id: Optional[str] = None, bash_runs: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.session_id = session_id
        self.bash_runs = bash_runs or []                # what the shell did before it stopped (still worth recording)


def system_prompt(agent: str, rules: str) -> str:
    skill = load_skill(agent) if agent in ROLES else ""
    return f"{skill}\n\n{rules}".strip()


def _describe(block: ToolUseBlock) -> str:
    data = block.input or {}
    target = data.get("file_path") or data.get("command") or data.get("pattern") or data.get("path") or ""
    return f"{block.name} {str(target)[:100]}".strip()


def _question(answer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    q = answer.get("question") if isinstance(answer.get("question"), dict) else None
    if not q or not str(q.get("question") or "").strip():
        return None
    opts = [str(o) for o in (q.get("options") or []) if str(o).strip()][:4]
    rec = q.get("recommended_option") if q.get("recommended_option") in opts else (opts[0] if opts else "")
    return {"question": str(q["question"]).strip(), "options": opts, "recommended_option": rec}


def clean(answer: Dict[str, Any]) -> Dict[str, Any]:
    """Make a step result safe to show: a 'question' needs text and 'blocked' needs a reason, otherwise treat it as done."""
    q = _question(answer) if answer.get("status") == "question" else None
    reason = " ".join(str(answer.get("blocked_reason") or "").split())
    if q:
        answer["question"] = q
    elif answer.get("status") == "blocked" and reason:
        answer["blocked_reason"] = reason
        answer["waiting_for"] = " ".join(str(answer.get("waiting_for") or "").split())
        answer.pop("question", None)
    else:
        answer["status"] = "done"
        answer.pop("question", None)
    answer["summary"] = str(answer.get("summary") or "")
    answer["assumptions"] = [str(a) for a in (answer.get("assumptions") or [])]
    answer["tests_written"] = [{"file": str(t.get("file") or "").strip(), "description": " ".join(str(t.get("description") or "").split())}
                               for t in (answer.get("tests_written") or []) if isinstance(t, dict) and str(t.get("file") or "").strip()]
    answer["test_command"] = _command(answer.get("test_command"))
    answer["unresolved"] = [{"test": str(u.get("test") or "").strip(), "reason": " ".join(str(u.get("reason") or "").split())}
                            for u in (answer.get("unresolved") or []) if isinstance(u, dict) and str(u.get("test") or "").strip()]
    return answer


def _command(value: Any) -> str:
    """A test command, if it is an acceptable one (see testrun.validate_command); otherwise nothing."""
    text = " ".join(str(value or "").split())
    return text if text and testrun.validate_command(text)[0] else ""


def clean_plan(answer: Dict[str, Any], default_agent: str = "") -> Dict[str, Any]:
    """Make Engineering Manager 2's answer safe to show: numbered subtasks with titles and instructions, an expert agent each
    (an unknown one becomes the default), at most MAX_STEPS; or a question. Raises Incomplete if neither."""
    answer["summary"] = str(answer.get("summary") or "")
    q = _question(answer) if answer.get("status") == "question" else None
    if q:
        return {"status": "question", "summary": answer["summary"], "question": q, "steps": [], "repo_notes": "", "learnings": [], "test_command": ""}
    steps: List[Dict[str, Any]] = []
    for raw in answer.get("steps") or []:
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            continue
        changes = [{"path": str(c.get("path") or "").strip(), "action": c.get("action") if c.get("action") in ("modify", "create", "delete") else "modify",
                    "what": str(c.get("what") or "").strip()}
                   for c in (raw.get("changes") or []) if isinstance(c, dict) and str(c.get("path") or "").strip()]
        agent = raw.get("agent") if raw.get("agent") in ROLES else (default_agent if default_agent in ROLES else next(iter(ROLES)))
        steps.append({"n": len(steps) + 1, "title": str(raw["title"]).strip(), "agent": agent,
                      "detail": str(raw.get("instructions") or raw.get("detail") or "").strip(), "changes": changes,
                      "files": [f"{c['path']} ({c['action']})" for c in changes], "context": str(raw.get("context") or "").strip(),
                      "tests": str(raw.get("tests") or "").strip(), "check": str(raw.get("check") or "").strip(),
                      "test_command": _command(raw.get("test_command"))})
    if not steps:
        raise Incomplete("The manager's plan had no subtasks. Ask it to plan again.")
    learnings = [{"text": " ".join(str(x["text"]).split()), "files": [str(f) for f in (x.get("files") or []) if str(f).strip()][:8]}
                 for x in (answer.get("learnings") or []) if isinstance(x, dict) and str(x.get("text") or "").strip()][:10]
    return {"status": "plan", "summary": answer["summary"], "repo_notes": str(answer.get("repo_notes") or "").strip(),
            "test_command": _command(answer.get("test_command")), "learnings": learnings, "steps": steps[:MAX_STEPS]}


def _text_of(content: Any) -> str:
    """The text of a tool result (a string, or a list of blocks with text)."""
    if isinstance(content, str):
        return content
    return "\n".join(str(c.get("text", "")) for c in (content or []) if isinstance(c, dict))


async def _run(prompt: str, system: str, cwd: str, model: Optional[str], resume: Optional[str],
               on_event: Optional[Callable[[str], None]], schema: Dict[str, Any], tools: List[str],
               disallowed: List[str], max_turns: int) -> Dict[str, Any]:
    options = ClaudeAgentOptions(
        system_prompt=system, model=model or config.MODEL_ID, cwd=cwd, tools=tools, allowed_tools=tools,
        disallowed_tools=disallowed, permission_mode="acceptEdits", max_turns=max_turns, resume=resume,
        output_format={"type": "json_schema", "schema": schema}, env=_ENV if "Bash" in tools else {})
    answer: Optional[Dict[str, Any]] = None
    info: Dict[str, Any] = {}
    detail = ""
    pending: Dict[str, str] = {}                        # Bash tool calls waiting for their result: id -> command
    runs: List[Dict[str, Any]] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    if block.name == "Bash":
                        pending[block.id] = str((block.input or {}).get("command") or "")
                    activity.log(_describe(block), "tool")
                    if on_event:
                        on_event(_describe(block))
                elif isinstance(block, TextBlock) and block.text.strip():
                    activity.log(block.text.strip(), "say")
                    if on_event:
                        on_event(block.text.strip()[:200])
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                    output = _text_of(block.content)
                    runs.append({"command": pending.pop(block.tool_use_id), "output": output, "is_error": bool(block.is_error),
                                 "when": time.strftime("%H:%M:%S"), "ts": time.time()})
                    for line in [l for l in output.splitlines() if l.strip()][-4:] or ["(no output)"]:
                        activity.log(line, "err" if block.is_error else "out")
        elif isinstance(message, ResultMessage):
            answer = message.structured_output
            info = {"session_id": message.session_id, "turns": message.num_turns,
                    "cost_usd": message.total_cost_usd, "seconds": round((message.duration_ms or 0) / 1000, 1),
                    "error": message.is_error}
            detail = f"{message.subtype or ''} {message.stop_reason or ''} {' '.join(map(str, message.errors or []))} {message.result or ''}".strip()
            activity.log(f"finished: {message.num_turns} turns, {round((message.duration_ms or 0) / 1000)}s" + (f", ${message.total_cost_usd:.2f}" if message.total_cost_usd else ""),
                         "err" if message.is_error else "ok")
    info["bash_runs"] = runs[-60:]
    if not isinstance(answer, dict):
        why = "it ran out of turns" if "max_turns" in detail else (detail[:300] or "no reason was given")
        raise Incomplete(f"The coding agent stopped without a final answer ({why}). Your edits so far are kept; press Continue to let it carry on.",
                         info.get("session_id"), info["bash_runs"])
    return {"answer": answer, **info}


def _call(label: str, agent: str, prompt: str, system: str, cwd: str, model: Optional[str], resume: Optional[str],
          on_event, schema, tools, disallowed, max_turns: int = MAX_TURNS) -> Dict[str, Any]:
    try:
        with activity.agent_context(f"{label} ({agent})"):
            activity.log("started" + (" (continuing an earlier conversation)" if resume else ""), "info")
            result = asyncio.run(_run(prompt, system, cwd, model, resume, on_event, schema, tools, disallowed, max_turns))
    except Exception as exc:  # noqa: BLE001 -- recorded so a failure can be diagnosed later, then re-raised
        activity.log(f"{label} ({agent}): stopped: {type(exc).__name__}: {str(exc)[:140]}", "err", agent=f"{label} ({agent})")
        token_log.record("llm_call", f"Coding Agent ({agent}) {label}", ok=False, attempts=1, error=f"{type(exc).__name__}: {exc}"[:500])
        raise
    token_log.record("llm_call", f"Coding Agent ({agent}) {label}", ok=True, attempts=1,
                     stats={k: result.get(k) for k in ("turns", "seconds", "cost_usd")})
    return result


def _outline(steps: List[Dict[str, Any]]) -> str:
    """The steps as a numbered list; a finished or skipped one is marked so a fresh session knows the state of the code."""
    mark = {"approved": " [done]", "skipped": " [SKIPPED - not in the code]"}
    return "\n".join(f"{s['n']}. {s['title']}{mark.get(s.get('status'), '')}"
                     + (f" -- {s['detail'][:160]}" if s.get("detail") and s.get("status") is None else "") for s in steps)


MAX_CODE_LINES = 6             # fenced code lines allowed in one subtask's instructions + context
MAX_DETAIL_CHARS = 1500        # instructions: about 150-200 words
MAX_CONTEXT_CHARS = 900
MAX_DECLARATIONS = 3           # inline snippets that are declarations (a field, an annotation...), not identifiers

_DECLARATION = re.compile(r"`[^`\n]*(?:@\w|;|\s=\s|\b(?:private|public|protected|final|static|class|record|enum|interface|import|def|function|const|let|var)\s)[^`\n]*`")


def code_lines(step: Dict[str, Any]) -> int:
    """How many lines of fenced code a subtask's text contains."""
    total, inside = 0, False
    for line in (step.get("detail", "") + "\n" + step.get("context", "")).splitlines():
        if line.strip().startswith("```"):
            inside = not inside
        elif inside:
            total += 1
    return total


def too_detailed(step: Dict[str, Any]) -> List[str]:
    """Why a subtask does the coder's job (empty when it only says what and where)."""
    text = step.get("detail", "") + "\n" + step.get("context", "")
    why = []
    if code_lines(step) > MAX_CODE_LINES:
        why.append(f"it contains {code_lines(step)} lines of code")
    declarations = len(_DECLARATION.findall(re.sub(r"```.*?```", "", text, flags=re.S)))
    if declarations > MAX_DECLARATIONS:
        why.append(f"it spells out {declarations} declarations (fields, annotations...) as inline code")
    if len(step.get("detail", "")) > MAX_DETAIL_CHARS:
        why.append(f"its instructions are {len(step['detail'])} characters long (about {MAX_DETAIL_CHARS} at most)")
    if len(step.get("context", "")) > MAX_CONTEXT_CHARS:
        why.append(f"its context is {len(step['context'])} characters long (about {MAX_CONTEXT_CHARS} at most)")
    return why


def decompose(agent: str, briefing: str, cwd: str, model: Optional[str] = None, resume: Optional[str] = None,
              user_input: Optional[str] = None, on_event: Optional[Callable[[str], None]] = None,
              current_steps: Optional[List[Dict[str, Any]]] = None, memory: str = "") -> Dict[str, Any]:
    """Engineering Manager 2: explore the repository on need and break the task into exact subtasks (or, with `resume` or
    `current_steps` + `user_input`, revise them). Read-only. `agent` is the default expert; `memory` is what earlier jobs
    learned about this repository. Returns {'answer', ...}."""
    experts = f"EXPERT AGENTS (give each subtask the best one; default {agent}):\n{prompt_block()}"
    remembered = f"\n\nWHAT YOU REMEMBER ABOUT THIS REPOSITORY (from earlier jobs):\n{memory}" if memory else ""
    if resume:
        prompt = f"The user says: {user_input}\nContinue: give the complete set of subtasks (or your next question)."
    elif current_steps and user_input:
        done = "\n".join(f"{s['n']}. {s['title']} [{s.get('agent', agent)}]" for s in current_steps)
        prompt = (f"{briefing}\n\n{experts}{remembered}\n\nTHE CURRENT SUBTASKS:\n{done}\n\nThe user says: {user_input}\n"
                  "Explore the repository as needed and give the complete revised set of subtasks (or your question).")
    else:
        prompt = f"{briefing}\n\n{experts}{remembered}\n\nExplore the repository as needed and break the task into subtasks."
    system = system_prompt(agent, PLAN_RULES)
    result = _call("manager2", agent, prompt, system, cwd, model, resume, on_event, PLAN_SCHEMA, READ_ONLY, [], MANAGER_TURNS)
    result["answer"] = clean_plan(result["answer"], agent)
    heavy = {st["n"]: too_detailed(st) for st in result["answer"]["steps"] if too_detailed(st)}
    if heavy and on_event:                          # a second full planning turn costs minutes; keep the plan and say so
        on_event(f"Note: subtask(s) {', '.join(map(str, heavy))} are more detailed than asked (" +
                 "; ".join(w for why in heavy.values() for w in why)[:200] + "); kept as they are.")
    return result


def _step_text(step: Dict[str, Any]) -> str:
    out = [f"STEP {step['n']}: {step['title']}"]
    if step.get("detail"):
        out.append("Instructions:\n" + step["detail"])
    if step.get("changes"):
        out.append("Changes:\n" + "\n".join(f"- {c['action']} {c['path']}: {c['what']}" for c in step["changes"]))
    elif step.get("files"):
        out.append("Files: " + ", ".join(step["files"]))
    if step.get("context"):
        out.append("Context from the repository:\n" + step["context"])
    if step.get("tests"):
        out.append("Unit tests to write: " + step["tests"])
    else:
        out.append("Unit tests to write: for the success, failure and edge cases of this change.")
    if step.get("check"):
        out.append("Done when: " + step["check"])
    return "\n".join(out)


def step_prompt(plan_steps: List[Dict[str, Any]], step: Dict[str, Any], briefing: Optional[str], feedback: Optional[str],
                notes: str = "") -> str:
    """What the agent is told for one step. A new session also gets the task and the whole plan; a continuing one only the
    step, because the (resumed) session already holds the rest."""
    if feedback:
        return f"The user asks for changes to your work on step {step['n']}:\n{feedback}\n\nChange what they asked and keep the rest."
    this = _step_text(step)
    if briefing is None:
        skipped = [str(s["n"]) for s in plan_steps if s["n"] < step["n"] and s.get("status") == "skipped"]
        done = "Earlier steps are done and committed" + (f", except step(s) {', '.join(skipped)}, which the user skipped: that work is NOT in the code" if skipped else "")
        return f"{done}. Now carry out only this step.\n\n{this}"
    notes_text = f"\n\nREPOSITORY NOTES (from Engineering Manager 2):\n{notes}" if notes else ""
    return f"{briefing}{notes_text}\n\nTHE AGREED PLAN:\n{_outline(plan_steps)}\n\nCarry out only this step now.\n\n{this}"


def fix_prompt(report: Dict[str, Any], attempt: int, last: bool, human: str = "") -> str:
    """What the coder is told when the tests it just wrote (or the code) do not pass."""
    lines = [f"The unit tests were run (attempt {attempt}). {testrun.headline(report)}."]
    failing = [c for c in report.get("cases", []) if c["status"] == "failed"]
    if failing:
        lines.append("Failing tests:")
        lines += [f"- {c['name']}" + (f": {c['message']}" if c.get("message") else "") for c in failing[:25]]
    if report.get("build_failed") or (not failing and report.get("failed")):
        lines.append("Output (last lines):\n" + report.get("tail", ""))
    if human.strip():
        lines.append("The user's input on how to fix this (follow it first; use your own findings for anything it does not cover):\n" + human.strip())
    cmd = report.get("command") or ""
    lines.append("Fix the code (or a test that is itself wrong, but never delete, skip or weaken a test). You may run the unit tests yourself with "
                 "your shell" + (f" (`{cmd}`)" if cmd else "") + " to check your fix before you answer; they are also run again for you afterwards.")
    if last:
        lines.append("This is the LAST attempt. For every failure you cannot fix, add an entry to `unresolved` with the test's name and the real reason.")
    return "\n".join(lines)


def execute(agent: str, prompt: str, cwd: str, model: Optional[str] = None, resume: Optional[str] = None,
            on_event: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """One turn of executing a step: the first prompt of a step, a revision, or an answer to a question (build the text
    with step_prompt or pass the user's answer). Returns {'answer': cleaned result, 'session_id', ...}."""
    result = _call("step", agent, prompt, system_prompt(agent, EXEC_RULES), cwd, model, resume, on_event,
                   EXEC_SCHEMA, ALLOWED, DISALLOWED)
    result["answer"] = clean(result["answer"])
    return result
