"""Which agents the page shows, and when.

Plain words: the page starts with only "New requirement". Each later agent appears once the step before it is done, for the session that is
open (new or reopened):
    Engineering Manager 2   when the session has a release plan
    Coding agent            when EM2's subtasks are frozen (its own card, drawn by the coding screen)
    Code reviewer           when the coding agent has opened (or pushed) a pull request
    QA agent                when a pull request has been merged
"""

import json
from typing import Any, Dict, List

import config
from coding import flow
from coding import workspace as ws
from review import loop as review_loop


def _jobs(run_id: str) -> List[Any]:
    if not run_id:
        return []
    try:
        return [j for j in flow.list_jobs() if j.d["task"]["run_id"] == run_id]
    except (KeyError, OSError, ValueError):
        return []


def _merged_in_review(job: Any) -> bool:
    """A review conversation on this job's branch ended in a merge (it was merged from the Code reviewer, which the job itself never hears of)."""
    store = review_loop.STORE
    if not store.is_dir():
        return False
    found = ws.github_repo(job.d["ws"].get("source", "") or "")
    for path in store.glob("*__*.json"):
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if s.get("phase") == "merged" and s.get("branch") == job.workspace.branch and (
                not found or [x.lower() for x in s.get("repo", [])] == [found[0].lower(), found[1].lower()]):
            return True
    return False


def flags(run_id: str, has_plan: bool) -> Dict[str, bool]:
    """{'em2', 'review', 'qa'}: which of the later agents to show. (The coding agent's own card follows EM2's frozen subtasks.)"""
    jobs = _jobs(run_id)
    opened = any((j.d.get("pr") or {}).get("url") or (j.d.get("push") or {}).get("done") for j in jobs)
    merged = any(j.d.get("merged") or _merged_in_review(j) for j in jobs)
    return {"em2": bool(has_plan), "review": opened or merged, "qa": merged}


def session_tasks(plan: Dict[str, Any], run_id: str) -> List[Any]:
    """The tasks (with their subtasks) of THIS session's release plan, and nothing from any other session. No plan, no tasks."""
    if not (run_id and plan and plan.get("stories")):
        return []
    from coding import tasks as coding_tasks
    return coding_tasks.tasks_of_plan(plan, run_id, coding_tasks.jira_keys(config.RUNS_ROOT / run_id))
