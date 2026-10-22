"""The task board: every task of the plan with where its coding job stands.

Plain words: built from the saved jobs (runs/coding/jobs) and the plan, so it is the same after a session is reopened.
A task with no job is "not started"; otherwise its newest job says how far it got.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from coding import flow
from coding import tasks as task_list


@dataclass
class Row:
    task: task_list.CodingTask
    job: Optional[flow.Job]
    code: str            # not_started | planning | ready | in_progress | blocked | push | pr_open | merged
    label: str
    done: int = 0
    total: int = 0
    pr_url: str = ""


def job_task_key(job: flow.Job) -> str:
    t = job.d["task"]
    return f"{t['run_id']}:{t['story_id']}/T{t['index']}"


def status_of(job: Optional[flow.Job]) -> tuple:
    """(code, label, steps done, steps in total) for a job."""
    if job is None:
        return "not_started", "Not started", 0, 0
    steps = job.steps
    done = sum(1 for s in steps if s["status"] in ("approved", "skipped"))
    total = len(steps)
    phase = job.phase
    if phase in ("planning", "jira"):
        return "planning", ("Subtasks frozen; Jira decision waiting" if phase == "jira" else
                            "Engineering Manager 2 is planning" if not steps else "Subtasks waiting for your approval"), 0, total
    if phase == "executing":
        blocked = next((s for s in steps if s["status"] == "blocked"), None)
        if blocked:
            return "blocked", f"Blocked on subtask {blocked['n']}: {(job.d.get('blocked') or {}).get('reason', '')}"[:160], done, total
        if done == 0 and not any(s["status"] != "pending" for s in steps):
            return "ready", f"Subtasks ready (0 of {total} done)", 0, total
        return "in_progress", f"In progress ({done} of {total} subtasks done)", done, total
    if phase == "final":
        pushed = (job.d.get("push") or {}).get("done")
        return "push", "Pushed; pull request not opened yet" if pushed else "All subtasks done; ready to push", done, total
    if job.d.get("merged"):
        return "merged", "Merged", done, total
    return "pr_open", "Pull request open; waiting for merge", done, total


def rows(tasks: List[task_list.CodingTask]) -> List[Row]:
    newest: Dict[str, flow.Job] = {}
    for j in flow.list_jobs():                       # newest first
        newest.setdefault(job_task_key(j), j)
    out = []
    for t in tasks:
        job = newest.get(t.key)
        code, label, done, total = status_of(job)
        out.append(Row(t, job, code, label, done, total, (job.d.get("pr") or {}).get("url", "") if job else ""))
    return out
