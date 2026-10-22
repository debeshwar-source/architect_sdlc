"""The coding job: plan -> freeze -> carry out each step -> approve -> final review -> pull request.

Plain words: this holds the job's state (saved to runs/coding/jobs/<id>/job.json after every change, so a closed
app can pick it up again) and the moves the screen offers. It contains no screen code, so it can be tested alone.

Phases:       planning (Engineering Manager 2 breaks the task down; you reconsider) -> jira (you allow the update, or not)
              -> executing (step by step) -> final (review, pull request) -> done
Step status:  pending -> running -> review (the CODE and its diff wait for you: approve, or reconsider and it is written again)
              -> running (tests written) -> testreview (the tests wait for you: approve to run them, or reconsider)
              -> running (tests run once) -> results (passed / failed and why; if some failed you may let the coder fix them and
              run again, up to MAX_FIX_ATTEMPTS rounds each time you ask, optionally with your own input)
              -> accepted (commit message waiting) -> approved (committed) | skipped;  running -> asking | blocked
After the last step: final (review the commits; push; open the pull request) -> done (PR open; later merged).
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coding import agent as coding_agent
from coding import jira_sync
from coding import memory
from coding import testrun
from coding import tasks as task_list
from coding import workspace as ws

JOBS_ROOT = ws.WORK_ROOT / "jobs"
OnEvent = Optional[Callable[[str], None]]


class Job:
    def __init__(self, data: Dict[str, Any]):
        self.d = data

    # ---- access ----
    @property
    def id(self) -> str:
        return self.d["id"]

    @property
    def task(self) -> task_list.CodingTask:
        return task_list.CodingTask(**self.d["task"])

    @property
    def workspace(self) -> ws.Workspace:
        return ws.Workspace.from_dict(self.d["ws"])

    @property
    def steps(self) -> List[Dict[str, Any]]:
        return self.d["plan"]["steps"]

    @property
    def phase(self) -> str:
        """planning | executing | final | done"""
        return self.d["phase"]

    def step(self, n: int) -> Dict[str, Any]:
        return next(s for s in self.steps if s["n"] == n)

    def current(self) -> Optional[Dict[str, Any]]:
        """The step being worked on: one waiting for you or asking, else the first pending one."""
        for s in self.steps:
            if s["status"] in ("testreview", "results", "review", "accepted", "asking", "running", "blocked"):
                return s
        return next((s for s in self.steps if s["status"] == "pending"), None)

    # ---- saving ----
    @property
    def path(self) -> Path:
        return JOBS_ROOT / self.id / "job.json"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def log(self, text: str) -> None:
        self.d["log"].append(f"{time.strftime('%H:%M:%S')} {text}")
        self.d["log"] = self.d["log"][-300:]


def steps_mode(job: "Job") -> bool:
    """Step by step (approve the code, then the tests, then the report) rather than autonomous (the agent does it all; one review at the end)."""
    return bool(job.d.get("confirm_tests"))


def step_command(job: "Job", step: Dict[str, Any]) -> str:
    """The command that runs this subtask's tests: the user's own, else the one the agent reported working, else the job's / detected."""
    if job.d.get("test_command_edited") and job.d.get("test_command"):
        return job.d["test_command"]
    return step.get("test_command") or test_command_for(job)


def step_env(job: "Job", step: Dict[str, Any]) -> Dict[str, str]:
    """Environment variables the tests need here (such as JAVA_HOME): those written in the command, else the ones the agent itself used
    the last time it ran this step's tests."""
    inline, _ = testrun.split_env(step_command(job, step))
    if inline:
        return inline
    for r in reversed(step.get("agent_runs") or []):
        found, _ = testrun.split_env(r["command"])
        if found:
            return found
    return {}


def migrate(job: "Job") -> None:
    """Bring a session saved by an older version into the current flow (idempotent; committed steps are never touched).
    The code is approved first, then the tests: a step waiting at an older stage goes back to the code approval."""
    changed = False
    for s in job.steps:
        if steps_mode(job):
            old = s["status"] == "caseplan" or (s["status"] == "testreview" and s.get("phase") != "tests") \
                or (s["status"] == "accepted" and not s.get("tests_done"))
        else:                                        # autonomous: there is one review; the stepwise stages fold into it
            old = s["status"] in ("caseplan", "testreview", "results")
        if old:
            s["status"], s["phase"], changed = "review", "code", True
    if changed:
        job.save()


def load(job_id: str) -> Optional[Job]:
    try:
        job = Job(json.loads((JOBS_ROOT / job_id / "job.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    migrate(job)
    return job


def list_jobs(task_key: Optional[str] = None) -> List[Job]:
    """Saved jobs, newest first (optionally only those of one task)."""
    if not JOBS_ROOT.is_dir():
        return []
    jobs = [j for j in (load(p.name) for p in JOBS_ROOT.iterdir() if p.is_dir()) if j]
    jobs = [j for j in jobs if task_key is None or j.d["task"]["run_id"] + ":" + j.d["task"]["story_id"] + "/T" + str(j.d["task"]["index"]) == task_key]
    return sorted(jobs, key=lambda j: (j.d["created"], len(j.id), j.id), reverse=True)           # same second: the later id (-2, -3) is newer


def _new_step(step: Dict[str, Any]) -> Dict[str, Any]:
    return {**step, "status": "pending", "start_sha": None, "commit": None, "message": "", "summary": "",
            "test_report": None, "tests_written": [], "test_cases": [], "phase": "code", "assumptions": []}


def _unique_id(base: str) -> str:
    """A job id no saved job already has (two jobs made in the same second must not share a record)."""
    out, i = base, 1
    while (JOBS_ROOT / out).exists():
        i += 1
        out = f"{base}-{i}"
    return out


def new_job(task: task_list.CodingTask, agent: str, source: str, base: str, model: str,
            subtasks: Optional[List[int]] = None, step_by_step: bool = False) -> Job:
    workspace = ws.prepare(source, task.key, (task.assignment.get("output") or {}).get("branch", ""), task.title, base or None)
    chosen = [task.subtasks[i] for i in (subtasks or []) if 0 <= i < len(task.subtasks)]
    steps = [_new_step({"n": n, "title": st["title"], "agent": agent, "detail": st.get("description", ""), "changes": [],
                        "files": [], "context": "", "tests": "", "check": ""}) for n, st in enumerate(chosen, start=1)]
    job = Job({
        "id": _unique_id(f"{ws.slug(task.key, 50)}-{time.strftime('%m%d-%H%M%S')}"), "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "task": asdict(task), "agent": agent, "model": model, "ws": workspace.to_dict(), "phase": "executing" if steps else "planning",
        "plan": {"summary": "Subtasks chosen from the first Engineering Manager's plan." if steps else "", "repo_notes": "",
                 "learnings": [], "steps": steps, "frozen": bool(steps)}, "jira": None, "memory_added": 0, "test_command": "", "question": None, "error": None,
        "plan_session": None, "exec_sessions": {}, "log": [], "pr": None, "plan_runs": 0, "confirm_tests": step_by_step})
    job.save()
    return job


# ---------- planning ----------

def plan_turn(job: Job, on_event: OnEvent = None, user_input: Optional[str] = None) -> None:
    """Ask the agent for a plan (first call), or for a revised plan / its next question after the user's input."""
    def ev(text: str) -> None:
        job.log(text)
        if on_event:
            on_event(text)

    if job.d["plan"]["frozen"]:
        raise ValueError("The plan is frozen.")
    try:
        result = coding_agent.decompose(job.d["agent"], task_list.briefing(job.task), str(job.workspace.tree), job.d["model"],
                                   resume=job.d["plan_session"], user_input=user_input, on_event=ev,
                                   current_steps=job.steps or None,
                                   memory=memory.for_prompt(memory.key_for(job.workspace), job.workspace))
    except Exception as exc:  # noqa: BLE001 -- shown to the user, who can press Continue / try again
        job.d["error"] = f"{type(exc).__name__}: {exc}"
        job.d["plan_session"] = getattr(exc, "session_id", None) or job.d["plan_session"]
        job.save()
        return
    job.d["error"] = None
    job.d["plan_session"] = result.get("session_id") or job.d["plan_session"]
    answer = result["answer"]
    if answer["status"] == "question":
        job.d["question"] = {"for": "plan", **answer["question"], "summary": answer["summary"]}
    else:
        job.d["question"] = None
        job.d["plan"]["summary"] = answer["summary"]
        job.d["plan"]["repo_notes"] = answer.get("repo_notes", "")
        job.d["plan"]["learnings"] = answer["learnings"]
        job.d["plan"]["test_command"] = answer.get("test_command", "")
        if not job.d.get("test_command_edited"):                  # the user's own command is never overwritten
            job.d["test_command"] = answer.get("test_command", "")
        job.d["plan"]["steps"] = [_new_step(s) for s in answer["steps"]]
        job.d["plan_runs"] += 1
    job.save()


def replan(job: Job, on_event: OnEvent = None) -> None:
    """Throw away the saved question, error, subtasks and the manager's conversation, and plan again from scratch.
    Only while the plan is not frozen."""
    if job.d["plan"]["frozen"]:
        raise ValueError("The plan is frozen; reopen it first.")
    job.d.update(question=None, error=None, plan_session=None)
    job.d["plan"].update(summary="", repo_notes="", learnings=[], steps=[])
    job.d["plan_runs"] += 1
    job.save()
    plan_turn(job, on_event)


def discard_plan(job: Job) -> None:
    """Undo Engineering Manager 2's planning: the subtasks, its notes and its conversation are dropped, and nothing is asked again.
    Only while the plan is not frozen."""
    if job.d["plan"]["frozen"]:
        raise ValueError("The plan is frozen; undo the freeze first.")
    job.d.update(question=None, error=None, plan_session=None, phase="planning")
    job.d["plan"].update(summary="", repo_notes="", learnings=[], steps=[])
    job.d["plan_runs"] += 1
    job.save()


def unfreeze_blocker(job: Job) -> str:
    """Why the freeze cannot be undone now ('' if it can)."""
    if any(s["status"] != "pending" for s in job.steps):
        return "A step has already been started."
    if (job.d.get("jira") or {}).get("status") == "updated":
        return "Jira already holds these subtasks, and Jira cannot be rolled back from here."
    return ""


def set_agents(job: Job, agents: Dict[int, str]) -> None:
    """The user's choice of expert for each subtask (before the plan is frozen)."""
    for n, agent in agents.items():
        if agent in coding_agent.ROLES and not job.d["plan"]["frozen"]:
            job.step(n)["agent"] = agent
    job.save()


def freeze_plan(job: Job) -> None:
    """Lock the subtasks. If the task has a Jira ticket, the next stop is the question whether to update it."""
    if not job.steps:
        raise ValueError("There is no plan to freeze.")
    job.d["plan"]["frozen"] = True
    learned = job.d["plan"].get("learnings") or []
    if learned:                                         # only approved work is remembered
        w = job.workspace
        job.d["memory_added"] = memory.remember(memory.key_for(w), w.source, w.base_commit, learned)
    can, why = jira_sync.availability(job.task)
    job.d["jira"] = {"status": "pending" if can else "unavailable", "note": why}
    job.d["phase"] = "jira" if can else "executing"
    job.save()


def update_jira(job: Job) -> None:
    """The user allowed it: write the frozen subtasks into the task's Jira ticket."""
    result = jira_sync.update(job.task, job.steps)
    job.d["jira"] = {"status": "updated" if result["ok"] else "failed", "note": result["note"]}
    if result["ok"]:
        job.d["phase"] = "executing"
    job.save()


def skip_jira(job: Job) -> None:
    job.d["jira"] = {"status": "skipped", "note": "Jira was left as it is."}
    job.d["phase"] = "executing"
    job.save()


def unfreeze_plan(job: Job) -> None:
    """Back to planning; only possible before any step has been started, and before Jira was told."""
    if any(s["status"] != "pending" for s in job.steps):
        raise ValueError("A step has already been started, so the plan can no longer be reopened.")
    if (job.d.get("jira") or {}).get("status") == "updated":
        raise ValueError("Jira has already been updated with these subtasks, so the plan can no longer be reopened.")
    job.d["plan"]["frozen"] = False
    job.d["jira"] = None
    job.d["phase"] = "planning"
    job.save()


# ---------- carrying out steps ----------

MAX_FIX_ATTEMPTS = 3          # how many times the coder is asked to fix failing tests before the rest is reported as it stands


def test_command_for(job: Job) -> str:
    """The command to run a step's tests: the step's own, else the job's, else what the repository's build files point to."""
    return job.d.get("test_command") or testrun.detect_command(job.workspace.tree)


def set_test_command(job: Job, command: str) -> None:
    """The command that runs the unit tests for every subtask (the user's word beats the manager's)."""
    job.d["test_command"] = " ".join(command.split())
    job.d["test_command_edited"] = True
    job.save()


def _apply_answer(job: Job, step: Dict[str, Any], answer: Dict[str, Any]) -> bool:
    """Record the coder's answer. Returns True when the step is done (so its tests can run); False if it asked or is blocked."""
    step["summary"], step["assumptions"] = answer["summary"], answer["assumptions"]
    step["tests_written"] = answer.get("tests_written") or step.get("tests_written", [])
    if answer["status"] == "question":
        job.d["question"] = {"for": step["n"], **answer["question"], "summary": answer["summary"]}
        step["status"] = "asking"
        return False
    if answer["status"] == "blocked":
        job.d["question"] = None
        job.d["blocked"] = {"step": step["n"], "reason": answer["blocked_reason"], "waiting_for": answer.get("waiting_for", ""), "reported": None}
        step["status"] = "blocked"
        return False
    job.d["question"] = None
    return True


def _test_and_fix(job: Job, step: Dict[str, Any], agent: str, ev: Callable[[str], None]) -> None:
    """Run the step's unit tests (by code, never by the AI). Failing: the coder fixes, up to MAX_FIX_ATTEMPTS times. Whatever
    still fails is reported with its reason."""
    sessions = job.d.setdefault("exec_sessions", {})
    command = step_command(job, step)
    tree = job.workspace.tree
    if not command:
        step["test_report"] = {"ran": False, "note": "No test command is set, so the tests were not run.", "total": 0, "passed": 0, "failed": 0,
                               "skipped": 0, "cases": [], "build_failed": False, "attempts": [], "unresolved": [], "command": ""}
        return
    attempts: List[Dict[str, Any]] = []
    said: List[Dict[str, str]] = []
    report: Dict[str, Any] = {}
    for attempt in range(1, MAX_FIX_ATTEMPTS + 2):
        ev(f"Running the unit tests (run {attempt}): {command}")
        report = testrun.run(command, tree, env=step_env(job, step))
        ev(testrun.headline(report))
        attempts.append({"run": attempt, "total": report["total"], "passed": report["passed"], "failed": report["failed"],
                         "build_failed": report["build_failed"], "ran": report["ran"]})
        job.save()
        if not testrun.needs_fix(report) or attempt > MAX_FIX_ATTEMPTS:
            break
        last = attempt == MAX_FIX_ATTEMPTS
        ev(f"Asking the coder to fix {report['failed'] or 'the build'} (fix {attempt} of {MAX_FIX_ATTEMPTS})")
        try:
            result = coding_agent.execute(agent, coding_agent.fix_prompt(report, attempt, last), str(tree), job.d["model"],
                                          resume=sessions.get(agent), on_event=ev)
        except Exception as exc:  # noqa: BLE001 -- keep the report so far; the user sees the error
            job.d["error"] = f"{type(exc).__name__}: {exc}"
            sessions[agent] = getattr(exc, "session_id", None) or sessions.get(agent)
            _note_runs(job, step, getattr(exc, "bash_runs", []), "fix")
            break
        sessions[agent] = result.get("session_id") or sessions.get(agent)
        _note_runs(job, step, result.get("bash_runs"), "fix")
        answer = result["answer"]
        said = answer.get("unresolved") or said
        if answer["status"] != "done":
            _apply_answer(job, step, answer)
            break
    step["test_report"] = {**report, "attempts": attempts, "unresolved": _unresolved(report, said)}


def _unresolved(report: Dict[str, Any], said: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Each test still failing, with the reason: the coder's own words if it gave them, else the failure message."""
    out = []
    for c in report.get("cases", []):
        if c["status"] == "failed":
            mine = next((u["reason"] for u in said if u["test"] and (u["test"] in c["name"] or c["name"] in u["test"])), "")
            out.append({"test": c["name"], "reason": mine or c.get("message") or "It failed; no message was given."})
    if report.get("build_failed") and not out:
        out.append({"test": "(the build)", "reason": next((u["reason"] for u in said), "") or testrun._build_reason(report.get("tail", ""))
                    or report.get("tail", "")[-400:]})
    if report.get("failed", 0) > len(out) and report.get("ran"):
        out.append({"test": f"{report['failed'] - len(out)} more failing test(s)", "reason": "Their names could not be read from the output."})
    return out


def _note_runs(job: Job, step: Dict[str, Any], runs: List[Dict[str, Any]], phase: str) -> None:
    """Record what the agent's shell did during a turn. A command that ran unit tests becomes a test-run record (with what it said);
    every other command is kept as a short line. Nothing is re-run; this is what the agent itself ran."""
    for r in runs or []:
        if testrun.is_test_command(r["command"]):
            step.setdefault("agent_runs", []).append(testrun.read_agent_run(r["command"], r["output"], r["is_error"], r["when"], phase,
                                                                              job.workspace.tree, r.get("ts")))
        else:
            step.setdefault("agent_commands", []).append({"when": r["when"], "phase": phase, "command": " ".join(r["command"].split())[:200],
                                                         "ok": not r["is_error"]})
    step["agent_runs"] = (step.get("agent_runs") or [])[-30:]
    step["agent_commands"] = (step.get("agent_commands") or [])[-60:]


def _run_step(job: Job, step: Dict[str, Any], prompt: str, on_event: OnEvent) -> None:
    """One coder turn. Gated (the default): code only, then the user approves it; later a turn writes the tests, then the user
    approves them. Not gated (older tests): the coder writes both, the tests run at once and failures are fixed automatically."""
    def ev(text: str) -> None:
        job.log(f"[{step['n']}] {text}")
        if on_event:
            on_event(text)

    agent = step.get("agent") or job.d["agent"]
    sessions = job.d.setdefault("exec_sessions", {})
    gated = steps_mode(job)
    code_phase = step.get("phase", "code") == "code"
    if gated and code_phase:
        prompt += coding_agent.CODE_ONLY
    elif not gated:
        prompt += coding_agent.AUTO_MODE
    step["status"] = "running"
    step["test_report"] = None
    job.save()
    try:
        result = coding_agent.execute(agent, prompt, str(job.workspace.tree), job.d["model"],
                                      resume=sessions.get(agent), on_event=ev)
    except Exception as exc:  # noqa: BLE001
        job.d["error"] = f"{type(exc).__name__}: {exc}"
        sessions[agent] = getattr(exc, "session_id", None) or sessions.get(agent)
        _note_runs(job, step, getattr(exc, "bash_runs", []), step.get("phase", "code"))
        step["status"] = "review" if ws.changed_files(job.workspace, step["start_sha"]) else "pending"      # keep what it did so far
        job.save()
        return
    job.d["error"] = None
    sessions[agent] = result.get("session_id") or sessions.get(agent)
    _note_runs(job, step, result.get("bash_runs"), step.get("phase", "code"))
    if result["answer"].get("test_command"):
        step["test_command"] = result["answer"]["test_command"]
    if _apply_answer(job, step, result["answer"]):
        if gated and code_phase:                                # the code waits for the user; no test is written yet
            step["status"] = "review"
        elif gated:                                             # the tests wait for the user; they are not run yet
            step["status"] = "testreview"
        else:                                                   # autonomous: ONE round is the agent's whole turn; then one official run, no fixing
            _run_once(job, step, ev, result["answer"].get("unresolved"))
            step["status"] = "review"
    job.save()


def _ev(job: Job, n: int, on_event: OnEvent) -> Callable[[str], None]:
    def ev(text: str) -> None:
        job.log(f"[{n}] {text}")
        if on_event:
            on_event(text)
    return ev


def _no_command_report() -> Dict[str, Any]:
    return {"ran": False, "note": "No test command is set, so the tests were not run.", "total": 0, "passed": 0, "failed": 0,
            "skipped": 0, "cases": [], "build_failed": False, "attempts": [], "unresolved": [], "command": ""}


def _run_once(job: Job, step: Dict[str, Any], ev: Callable[[str], None], said: Optional[List[Dict[str, str]]] = None) -> None:
    """Run the step's tests once (by code), and record the report. Nothing is changed or fixed. `said` is what the agent itself reported
    as still failing, with its reasons; they are shown next to the failures."""
    command = step_command(job, step)
    if not command:
        step["test_report"] = _no_command_report()
        return
    ev(f"Running the unit tests: {command}")
    report = testrun.run(command, job.workspace.tree, env=step_env(job, step))
    ev(testrun.headline(report))
    step["test_report"] = {**report, "attempts": [{"run": 1, "total": report["total"], "passed": report["passed"], "failed": report["failed"],
                                                   "build_failed": report["build_failed"], "ran": report["ran"]}],
                           "unresolved": _unresolved(report, said or [])}


def rerun_tests(job: Job, n: int, on_event: OnEvent = None) -> None:
    """Run the step's unit tests again without changing anything first (for example after fixing the environment, or to set
    the command): one run and a report, in either mode."""
    step = job.step(n)
    if step["status"] not in ("testreview", "results", "review", "accepted"):
        raise ValueError("This step has no change to test.")
    ev = _ev(job, n, on_event)
    was = step["status"]
    step["status"] = "running"
    job.save()
    _run_once(job, step, ev)
    step["status"] = "accepted" if was == "accepted" else ("results" if steps_mode(job) else "review")
    job.save()


def approve_code(job: Job, n: int, on_event: OnEvent = None) -> None:
    """The user approved the code (the diff). The coder now writes the unit tests; they wait for the user, and are not run."""
    step = job.step(n)
    if step["status"] != "review" or not steps_mode(job):
        raise ValueError("This step's code is not waiting for your approval.")
    step["phase"], step["code_approved"] = "tests", True
    job.save()
    have = ", ".join(t["file"] for t in step.get("tests_written") or [])
    _run_step(job, step, f"The user approved the code of step {n}. Now write the unit tests for exactly this change: the success path, the "
              "failure paths and the edge cases, in the repository's own test style and location (follow the test pattern the step names). "
              + (f"Tests already exist for this step ({have}): change or extend them rather than duplicating. " if have else "")
              + "Change production code only if a test cannot be written without it, and say so in your summary. List every test file in "
              "`tests_written`. You may compile the tests (for example a test-compile goal) but do NOT run the unit tests: the user approves them "
              "first.", on_event)


def revise_tests(job: Job, n: int, feedback: str, on_event: OnEvent = None) -> None:
    """The user wants different unit tests: the coder changes the tests only (not the step's code), then they wait again."""
    step = job.step(n)
    if step["status"] != "testreview":
        raise ValueError("The tests of this step are not waiting for your review.")
    _run_step(job, step, f"The user has read the unit tests you wrote for step {n} and asks for changes to the TESTS:\n{feedback}\n\n"
              "Change only the tests (add, remove or alter cases, or their data and names) and keep the rest of the step as it is. "
              "Change production code only if a requested test cannot be written without it, and say so in your summary. "
              "List the test files in `tests_written`. The tests are not run until the user approves them.", on_event)


def approve_tests(job: Job, n: int, on_event: OnEvent = None) -> None:
    """The user allowed the tests to run: they run once and the report follows (passed, failed and why). Nothing is fixed yet."""
    step = job.step(n)
    if step["status"] != "testreview":
        raise ValueError("The tests of this step are not waiting for your review.")
    step["tests_approved"] = True
    rerun_tests(job, n, on_event)


def skip_running_tests(job: Job, n: int) -> None:
    """The user does not want the tests run: the report says so, and the step goes on to the commit decision."""
    step = job.step(n)
    if step["status"] != "testreview":
        raise ValueError("The tests of this step are not waiting for your review.")
    step["test_report"] = {**_no_command_report(), "note": "You chose not to run the tests.", "command": job.d.get("test_command", "")}
    step["tests_approved"] = True
    step["status"] = "results"
    job.save()


def fix_and_rerun(job: Job, n: int, feedback: str = "", on_event: OnEvent = None) -> None:
    """The user allowed the coder to change the code so the failing tests pass (with their own input, if they gave any; else from
    the coder's own reading of the failures). Up to MAX_FIX_ATTEMPTS rounds of fix-then-run, stopping early when everything passes.
    Whatever still fails stays in the report; the user can allow another round."""
    step = job.step(n)
    report = step.get("test_report") or {}
    if step["status"] != "results" or not testrun.needs_fix(report):
        raise ValueError("There is nothing failing to fix.")
    ev = _ev(job, n, on_event)
    agent = step.get("agent") or job.d["agent"]
    sessions = job.d.setdefault("exec_sessions", {})
    command = report.get("command") or step_command(job, step)
    attempts = list(report.get("attempts") or [])
    said: List[Dict[str, str]] = []
    step["status"] = "running"
    job.save()
    for i in range(1, MAX_FIX_ATTEMPTS + 1):
        ev(f"Asking the coder to fix {report['failed'] or 'the build'} (round {i} of {MAX_FIX_ATTEMPTS})")
        try:
            result = coding_agent.execute(agent, coding_agent.fix_prompt(report, i, i == MAX_FIX_ATTEMPTS, feedback if i == 1 else ""),
                                          str(job.workspace.tree), job.d["model"], resume=sessions.get(agent), on_event=ev)
        except Exception as exc:  # noqa: BLE001 -- the report so far is kept; the user sees the error
            job.d["error"] = f"{type(exc).__name__}: {exc}"
            sessions[agent] = getattr(exc, "session_id", None) or sessions.get(agent)
            _note_runs(job, step, getattr(exc, "bash_runs", []), "fix")
            break
        sessions[agent] = result.get("session_id") or sessions.get(agent)
        _note_runs(job, step, result.get("bash_runs"), "fix")
        answer = result["answer"]
        said = answer.get("unresolved") or said
        if answer["status"] != "done":
            _apply_answer(job, step, answer)
            break
        ev(f"Running the unit tests (run {len(attempts) + 1}): {command}")
        report = testrun.run(command, job.workspace.tree, env=step_env(job, step))
        ev(testrun.headline(report))
        attempts.append({"run": len(attempts) + 1, "total": report["total"], "passed": report["passed"], "failed": report["failed"],
                         "build_failed": report["build_failed"], "ran": report["ran"]})
        step["test_report"] = {**report, "attempts": attempts, "unresolved": _unresolved(report, said)}
        job.save()
        if not testrun.needs_fix(report):
            break
    if step["status"] == "running":
        step["status"] = "results"
    job.save()


def accept_results(job: Job, n: int) -> None:
    """The user has seen the report (passing or not) and wants to go on: the commit message and the commit decision come next."""
    step = job.step(n)
    if step["status"] != "results":
        raise ValueError("This step has no test report waiting for you.")
    step["tests_done"] = True
    step["status"] = "accepted"
    job.save()


def execute_step(job: Job, n: int, on_event: OnEvent = None) -> None:
    """Carry out step `n` (the first pending one, in order)."""
    if not job.d["plan"]["frozen"]:
        raise ValueError("Freeze the plan before carrying out its steps.")
    step = job.step(n)
    first_pending = next((s for s in job.steps if s["status"] == "pending"), None)
    if step is not first_pending:
        raise ValueError(f"Step {first_pending['n'] if first_pending else '-'} comes first." if first_pending else "This step has already been started.")
    step["start_sha"] = ws.head(job.workspace)
    step["phase"] = "code"
    agent = step.get("agent") or job.d["agent"]
    fresh = not job.d.setdefault("exec_sessions", {}).get(agent)          # a new expert starts a new conversation
    briefing = task_list.briefing(job.task) if fresh else None
    prompt = coding_agent.step_prompt(job.steps, step, briefing, None, job.d["plan"].get("repo_notes", ""))
    if step.get("redo_note"):
        prompt += (f"\n\nTHIS STEP IS BEING REDONE. The user wants it done differently this time:\n{step['redo_note']}\n"
                   "Earlier attempts were removed from the working tree; do not try to recall them.")
    _run_step(job, step, prompt, on_event)


def answer_question(job: Job, answer: str, on_event: OnEvent = None) -> None:
    """The user's answer to the agent's question: during planning, or in the step that asked."""
    q = job.d["question"]
    if not q:
        return
    if q["for"] == "plan":
        plan_turn(job, on_event, user_input=answer)
        return
    step = job.step(q["for"])
    _run_step(job, step, f"The user answered your question: {answer}\nContinue this step.", on_event)


def restart_step(job: Job, n: int, on_event: OnEvent = None) -> None:
    """Code this step again from nothing: its edits are removed, any question or error is cleared, and the coder starts a
    fresh conversation (so it does not carry the old question along)."""
    step = job.step(n)
    if step["status"] in ("approved", "skipped") or not step.get("start_sha"):
        raise ValueError("Only a step that has been started and not yet committed can be restarted.")
    ws.reset_to(job.workspace, step["start_sha"])
    agent = step.get("agent") or job.d["agent"]
    job.d.setdefault("exec_sessions", {}).pop(agent, None)
    job.d.update(question=None, error=None, blocked=None)
    for key in ("test_report", "start_sha"):
        step[key] = None
    step.update(status="pending", summary="", tests_written=[], assumptions=[])
    job.save()
    execute_step(job, n, on_event)


def undo_step_blocker(job: Job, n: int) -> str:
    """Why this step's work cannot be undone now ('' if it can). Committed work can only be undone from the newest step backwards."""
    step = job.step(n)
    if step["status"] == "pending":
        return "This step has not been started."
    if job.d.get("merged"):
        return "The pull request is merged, so its commits cannot be rewritten."
    if step["status"] == "running":
        return "The agent is working on it."
    if step["status"] == "approved":                    # a commit has later work on top of it; a skip or unfinished edits do not
        later = [s["n"] for s in job.steps if s["n"] > n and s["status"] != "pending"]
        if later:
            return f"Undo step {later[-1]} first: it was done after this one."
    elif step["status"] != "skipped" and not step.get("start_sha"):
        return "This step has nothing to undo."
    return ""


def undo_step(job: Job, n: int) -> None:
    """Undo ONE step's work and nothing else: a skipped step becomes not-started, a step still in review loses its edits, a committed step
    loses its commit (the old tip is kept on a backup branch). The step is left 'not started'; nothing is run again."""
    why = undo_step_blocker(job, n)
    if why:
        raise ValueError(why)
    step = job.step(n)
    if step["status"] == "skipped":
        step["status"] = "pending"
        job.d["phase"] = "executing"
        if (job.d.get("push") or {}).get("done"):
            job.d["push"]["stale"] = True               # the remote no longer matches once this step is done again
    elif step["status"] == "approved":
        redo_from(job, n, "Undone")                    # keeps a backup, marks a pushed branch stale; no later step is started
        return
    else:
        ws.reset_to(job.workspace, step["start_sha"])
        job.d.setdefault("exec_sessions", {}).pop(step.get("agent") or job.d["agent"], None)
        job.d.update(question=None, error=None, blocked=None)
        step.update(status="pending", summary="", tests_written=[], assumptions=[], test_report=None, start_sha=None, phase="code",
                    tests_done=False, tests_approved=False)
    job.save()


def undo_push_blocker(job: Job) -> str:
    if not (job.d.get("push") or {}).get("done"):
        return "Nothing has been pushed."
    if job.d.get("merged"):
        return "The pull request is merged."
    if (job.d.get("pr") or {}).get("url"):
        return "A pull request is open on this branch: close it first."
    return ""


def undo_push(job: Job) -> None:
    """Remove the branch from the remote. The commits stay on this computer, so the push can be done again."""
    why = undo_push_blocker(job)
    if why:
        raise ValueError(why)
    ws.delete_remote_branch(job.workspace)
    job.d["push"] = None
    job.save()


def undo_pr_blocker(job: Job) -> str:
    pr = job.d.get("pr") or {}
    if job.d.get("merged"):
        return "The pull request is merged."
    if not pr.get("url"):
        return "No pull request was opened."
    if not pr.get("opened"):
        return "It was never opened by the app, so there is nothing to close here."
    return ""


def undo_pr(job: Job) -> None:
    """Close the pull request (it can be reopened on GitHub); the branch stays pushed, so another one can be opened."""
    why = undo_pr_blocker(job)
    if why:
        raise ValueError(why)
    ws.close_pr(job.d["pr"]["url"])
    job.d.update(pr=None, jira_pr=None, phase="final")
    job.save()


def revise_step(job: Job, n: int, feedback: str, on_event: OnEvent = None) -> None:
    """The user wants the step done differently: start the step over from its first commit, with the user's words."""
    step = job.step(n)
    ws.reset_to(job.workspace, step["start_sha"])
    step.update(phase="code", tests_written=[], tests_done=False, tests_approved=False)
    _run_step(job, step, coding_agent.step_prompt(job.steps, step, None, feedback) +
              "\n\n(Your earlier edits for this step were removed; redo the step with this change.)", on_event)


def reconsider(job: Job, n: int, feedback: str, on_event: OnEvent = None) -> None:
    """The user wants changes. Step by step: the step starts again from its first commit. Autonomous: the same agent session carries on
    from what is in the working tree, with the user's words: it changes the code and the tests, runs them again, and comes back to the review."""
    if steps_mode(job):
        revise_step(job, n, feedback, on_event)
        return
    step = job.step(n)
    if step["status"] not in ("review", "accepted"):
        raise ValueError("This step has no result waiting for your review.")
    step.update(phase="code", tests_done=False)
    _run_step(job, step, f"The user reviewed the result of step {n} (the code, the unit tests and their results) and asks for changes:\n{feedback}\n\n"
              "Your earlier work is still in the working tree. Make the changes, update or add the unit tests, run them again and fix "
              "what fails.", on_event)


def accept_step(job: Job, n: int) -> None:
    """The user approves the change (the diff). Next comes the commit message; nothing is committed yet."""
    step = job.step(n)
    if step["status"] != "review" or steps_mode(job):
        raise ValueError("This step has no change waiting for approval.")
    step["status"] = "accepted"
    job.save()


def approve_step(job: Job, n: int, message: str, post_to_jira: bool = False) -> None:
    """The user allows the commit, with this message. Committed locally; nothing is pushed."""
    step = job.step(n)
    if step["status"] != "accepted" and not (step["status"] == "review" and not steps_mode(job)):
        raise ValueError("This step has no change waiting for approval.")
    message = message.strip() or step["title"]
    step["message"] = message
    step["commit"] = ws.commit(job.workspace, message) if ws.changed_files(job.workspace, step["start_sha"]) else None
    step["status"] = "approved"
    job.d["question"] = None
    if post_to_jira:                                    # the user ticked the box: the subtask and its report go on the ticket
        result = jira_sync.report_step(job.task, step, job.workspace.branch)
        step["jira_posted"], step["jira_note"] = result["ok"], result["note"]
    if all(s["status"] in ("approved", "skipped") for s in job.steps):
        job.d["phase"] = "final"
    job.save()


def report_blocked(job: Job) -> None:
    """The user allowed it: say on the Jira ticket why this task is waiting."""
    b = job.d.get("blocked")
    if not b:
        return
    text = b["reason"] + (f" Waiting for: {b['waiting_for']}." if b.get("waiting_for") else "")
    b["reported"] = jira_sync.report_blocked(job.task, text)
    job.save()


def retry_blocked(job: Job, n: int, on_event: OnEvent = None) -> None:
    """Whatever the step was waiting for is done: start the step again."""
    step = job.step(n)
    if step["status"] != "blocked":
        raise ValueError("This step is not blocked.")
    if step.get("start_sha"):
        ws.reset_to(job.workspace, step["start_sha"])
    step["status"] = "pending"
    job.d["blocked"] = None
    job.save()
    execute_step(job, n, on_event)


def skip_step(job: Job, n: int) -> None:
    step = job.step(n)
    if step.get("start_sha"):
        ws.reset_to(job.workspace, step["start_sha"])
    step["status"] = "skipped"
    job.d["question"] = None
    if (job.d.get("blocked") or {}).get("step") == n:
        job.d["blocked"] = None
    if all(s["status"] in ("approved", "skipped") for s in job.steps):
        job.d["phase"] = "final"
    job.save()


# ---------- the end ----------

def final_summary(job: Job) -> Dict[str, Any]:
    w = job.workspace
    return {"commits": ws.commits(w), "files": ws.changed_files(w), "diff": ws.diff(w)}


def default_pr_title(job: Job) -> str:
    t = job.task
    return (f"{t.jira_key}: " if t.jira_key else "") + t.title


def test_file_paths(job: Job, step: Dict[str, Any]) -> List[str]:
    """The unit-test files of a subtask: those the agent listed, plus any file in the subtask's own changes that looks like a test."""
    paths = [t["file"] for t in step.get("tests_written") or [] if t.get("file")]
    try:
        if step.get("commit"):
            changed = ws.commit_files(job.workspace, step["commit"])
        elif step.get("start_sha"):
            changed = ws.changed_files(job.workspace, step["start_sha"])
        else:
            changed = []
    except ws.GitError:
        changed = []
    return paths + [f for f in changed if testrun.is_test_file(f) and f not in paths]


def tests_overview(job: Job) -> List[Dict[str, Any]]:
    """For each finished subtask: its unit-test result, as the PR and Jira report it (see testrun.step_result: one result per subtask)."""
    rows = []
    for s in job.steps:
        r = testrun.step_result(s)
        if s["status"] in ("approved", "review", "results", "accepted") and r is not None:
            rows.append({"n": s["n"], "title": s["title"], "headline": r["headline"], "ran": r["ran"], "unresolved": r["unresolved"],
                         "written": s.get("tests_written", []), "command": r["command"], "rerun": r["rerun"]})
    return rows


def tests_markdown(job: Job) -> str:
    """The unit-test report as plain Markdown (used in the pull request)."""
    lines = []
    for r in tests_overview(job):
        lines.append(f"- Step {r['n']} - {r['title']}: **{r['headline']}**")
        lines += [f"  - New test file: `{t['file']}`" + (f" - {t['description']}" if t["description"] else "") for t in r["written"]]
        lines += [f"  - NOT FIXED: `{u['test']}` - {u['reason']}" for u in r["unresolved"]]
        if r["rerun"]:
            lines.append(f"  - {r['rerun']}")
    return "\n".join(lines)


def tests_text(job: Job) -> str:
    """The same report as plain text (used in the Jira comment)."""
    lines = ["Unit tests:"]
    for r in tests_overview(job):
        lines.append(f"Step {r['n']} ({r['title']}): {r['headline']}")
        lines += [f"  not fixed: {u['test']} - {u['reason']}" for u in r["unresolved"]]
        if r["rerun"]:
            lines.append(f"  {r['rerun']}")
    return "\n".join(lines) if len(lines) > 1 else ""


def pr_description(job: Job) -> str:
    return ws.pr_body(job.task, {"summary": job.d["plan"]["summary"] or job.task.title, "tests_md": tests_markdown(job),
                                 "assumptions": [a for s in job.steps for a in s.get("assumptions", [])]},
                      steps=[s for s in job.steps if s["status"] == "approved"])


def push_branch(job: Job, squash_message: Optional[str] = None, replace_remote: bool = False) -> str:
    """The user allowed the push: optionally squash to one commit first, then push the branch. When the branch was pushed before and the
    work has since been redone, the remote branch must be REPLACED; that only happens when `replace_remote` is set (the user confirmed)."""
    w = job.workspace
    pushed_before = bool((job.d.get("push") or {}).get("done"))
    if pushed_before and not replace_remote:
        raise ws.GitError("This branch was pushed before and has since been redone. Pushing it again replaces the earlier commits on the "
                          "remote, so it needs your explicit confirmation.")
    if squash_message and len(ws.commits(w)) > 1:
        ws.squash(w, squash_message)
    if not ws.commits(w):
        raise ws.GitError("There are no commits to push.")
    remote = ws.push(w, force=pushed_before)
    job.d["push"] = {"done": True, "remote": remote, "commits": len(ws.commits(w))}
    if pushed_before:
        job.d["repushed"] = True
    job.save()
    return remote


def finish_after_repush(job: Job) -> None:
    """The branch of an already open pull request was replaced: the PR shows the new commits by itself, so there is nothing to open.
    Jira may be told (a comment), and the job is done again."""
    job.d["phase"] = "done"
    can, why = jira_sync.availability(job.task)
    job.d["jira_pr"] = {"status": "pending" if can else "unavailable", "note": why}
    job.save()


def open_pr(job: Job, title: str, body: str) -> Dict[str, Any]:
    """The user allowed the pull request (the branch is already pushed)."""
    result = ws.create_pr(job.workspace, title, body)
    job.d["pr"] = result
    job.d["phase"] = "done"                         # pushed, and the PR attempted (its note says if you must open it yourself)
    can, why = jira_sync.availability(job.task)
    job.d["jira_pr"] = {"status": "pending" if can else "unavailable", "note": why}        # no PR of ours: a comment-only update is offered
    job.save()
    return result


def raise_pr(job: Job, title: str, body: str, squash_message: Optional[str] = None) -> Dict[str, Any]:
    """Push and open the pull request in one go (the screen does the two as separate approvals)."""
    push_branch(job, squash_message)
    return open_pr(job, title, body)


def jira_report_pr(job: Job) -> None:
    """The user allowed it: with a pull request, attach its link and move the ticket to an in-between status; without one, only add a
    comment (branch and unit-test results) and leave the status alone."""
    pr = job.d.get("pr") or {}
    if pr.get("opened") and not job.d.get("repushed"):
        result = jira_sync.after_pr(job.task, pr.get("url", ""), tests_text=tests_text(job))
    else:
        result = jira_sync.after_branch(job.task, job.workspace.branch, pr.get("url", ""), tests_text=tests_text(job))
    job.d["jira_pr"] = {"status": "updated" if result["ok"] else "failed", "note": result["note"]}
    job.save()


def skip_jira_pr(job: Job) -> None:
    job.d["jira_pr"] = {"status": "skipped", "note": "Jira was left as it is."}
    job.save()


def check_merged(job: Job) -> Optional[str]:
    """Ask GitHub whether the PR has been merged. Returns 'merged', 'open', 'closed' or None (cannot tell)."""
    url = (job.d.get("pr") or {}).get("url", "")
    state = ws.pr_state(url) if url else None
    if state == "merged" and not job.d.get("merged"):
        job.d["merged"] = True
        can, why = jira_sync.availability(job.task)
        job.d["jira_done"] = {"status": "pending" if can else "unavailable", "note": why}
        job.save()
    return state


def jira_mark_done(job: Job) -> None:
    """The user allowed it: the PR is merged, so move the ticket to Done."""
    result = jira_sync.mark_done(job.task)
    job.d["jira_done"] = {"status": "updated" if result["ok"] else "failed", "note": result["note"]}
    job.save()


def skip_jira_done(job: Job) -> None:
    job.d["jira_done"] = {"status": "skipped", "note": "Jira was left as it is."}
    job.save()


def continue_after_error(job: Job, on_event: OnEvent = None) -> None:
    """After a failed turn: let the agent carry on (the same session, so it keeps what it has read and done)."""
    if job.phase == "planning":
        plan_turn(job, on_event, user_input="Carry on." if job.d["plan_session"] else None)
        return
    step = job.current()
    if step is not None:
        _run_step(job, step, "Carry on with this step.", on_event)


# ---------- redo and revamp finished work ----------

def _forget(step: Dict[str, Any], note: str = "") -> None:
    step.update(status="pending", start_sha=None, commit=None, message="", summary="", tests_written=[], test_report=None, assumptions=[],
                phase="code", tests_done=False, tests_approved=False, agent_runs=[], agent_commands=[], test_command=None,
                jira_posted=None, jira_note="", redo_note=note)


def _fresh(step: Dict[str, Any], note: str) -> Dict[str, Any]:
    out = _new_step(step)
    _forget(out, note)
    return out


def redo_from(job: Job, n: int, note: str = "") -> str:
    """Take the agent's branch back to just before step `n` and put that step and every later one back to 'not started'. This rewrites the
    agent's OWN local branch only: the old tip is kept on a backup branch first, nothing remote changes (a later push of a branch that was
    already pushed asks for its own confirmation), and a merged pull request cannot be redone this way. Returns the backup branch's name."""
    if job.d.get("merged"):
        raise ValueError("The pull request is already merged, so its commits cannot be rewritten. Revamp it as a new job instead.")
    step = job.step(n)
    if not step.get("start_sha"):
        raise ValueError(f"Step {n} was never started, so there is nothing to redo from there.")
    w = job.workspace
    stamp = time.strftime("%m%d-%H%M%S")
    backup = ws.backup_branch(w, stamp)
    job.d.setdefault("history", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "from_step": n, "note": note, "backup": backup,
                                            "commits": ws.commits(w), "pushed": bool((job.d.get("push") or {}).get("done")), "pr": job.d.get("pr")})
    ws.reset_to(w, step["start_sha"])
    for s in job.steps:
        if s["n"] >= n:
            _forget(s, note if s["n"] == n else "")
    job.d["exec_sessions"] = {}                                    # a fresh conversation: it must not build on what was removed
    job.d.update(question=None, error=None, blocked=None, phase="executing", merged=False, jira_pr=None, jira_done=None)
    if (job.d.get("push") or {}).get("done"):
        job.d["push"]["stale"] = True
    job.save()
    return backup


def revamp_job(job: Job, note: str = "") -> Job:
    """Start a new job for the same task from this job's plan, on a fresh branch off the base as it is now (so a merged change is part of
    it). Used when a pull request is merged, or when you would rather keep this job's record as it is. Raises ws.GitError if the repository
    cannot be opened."""
    w = job.workspace
    new = new_job(job.task, job.d["agent"], w.source, w.base, job.d["model"], step_by_step=steps_mode(job))
    new.d["plan"] = {**job.d["plan"], "frozen": True, "steps": [_fresh(s, note) for s in job.d["plan"]["steps"]]}
    new.d["phase"] = "executing"
    new.d["test_command"] = job.d.get("test_command", "")
    new.d["test_command_edited"] = job.d.get("test_command_edited", False)
    new.d["revamp_of"] = job.id
    new.save()
    job.d.setdefault("history", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "revamped_as": new.id, "note": note})
    job.save()
    return new
