"""Write the frozen subtasks back to the task's Jira ticket (only after the user says yes).

Plain words: Jira does not let a Sub-task have Sub-tasks, and the first manager already publishes each of its tasks as a
Jira Sub-task of its Story. So the finer subtasks of Engineering Manager 2 are written into that ticket's "Steps" section
(numbered, each with its expert agent, files and check), and the ticket gets a label per expert agent. Nothing else in the
ticket is changed.
"""

from typing import Any, Dict, List, Optional, Tuple

from tools import jira_client


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def step_lines(steps: List[Dict[str, Any]]) -> List[str]:
    """Wiki-markup lines (a numbered list with nested bullets) for the steps."""
    lines = []
    for s in steps:
        agent = f" [{s['agent']}]" if s.get("agent") else ""
        lines.append(f"# *{_one_line(s['title'])}*{agent}")
        if s.get("detail"):
            lines.append(f"#* {_one_line(s['detail'])[:600]}")
        if s.get("files"):
            lines.append("#* Files: " + _one_line(", ".join(s["files"])))
        if s.get("check"):
            lines.append("#* Check: " + _one_line(s["check"]))
    return lines


def steps_markup(steps: List[Dict[str, Any]]) -> str:
    return "h3. Steps\n" + "\n".join(step_lines(steps))


def labels_for(steps: List[Dict[str, Any]]) -> List[str]:
    return sorted({f"agent-{s['agent']}" for s in steps if s.get("agent")})


def availability(task) -> Tuple[bool, str]:
    """Can the subtasks be written to Jira? (and if not, why not)"""
    if not jira_client.is_configured():
        return False, "Jira is not configured (set " + ", ".join(jira_client.missing_settings()) + "), so there is nothing to update."
    if not task.jira_key:
        return False, "This task has no Jira ticket yet (the first manager's backlog was not published), so there is nothing to update."
    return True, ""


def update(task, steps: List[Dict[str, Any]], client: Optional[Any] = None) -> Dict[str, Any]:
    """Update the task's ticket. Returns {'ok', 'key', 'note'}; never raises for a Jira problem."""
    ok, why = availability(task)
    if client is None and not ok:
        return {"ok": False, "key": task.jira_key, "note": why}
    try:
        (client or jira_client.from_env()).update_task_steps(task.jira_key, steps_markup(steps), labels_for(steps))
    except jira_client.JiraError as exc:
        return {"ok": False, "key": task.jira_key, "note": str(exc)}
    return {"ok": True, "key": task.jira_key, "note": f"{task.jira_key} now lists {len(steps)} step(s)."}


# ---------- a task moves on: PR opened, PR merged, or blocked ----------
#
# Two optional custom fields, named by their Jira ids (ask a Jira admin; they look like customfield_10042):
#   JIRA_PR_LINK_FIELD        where the pull request's address goes (a URL or text field)
#   JIRA_BLOCKED_REASON_FIELD where "this cannot be done until..." goes (a text field)
# Without them the PR is attached as a Jira web link and the reason as a comment, so nothing is lost.

import os
import re

REVIEW_WORDS = re.compile(r"review|verif|test|qa", re.I)
DONE_WORDS = re.compile(r"done|resolve|close|complete|finish", re.I)


def _field(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def pick_transition(transitions: List[Dict[str, Any]], want: str) -> Optional[Dict[str, Any]]:
    """The transition to use. 'review' = an in-between state (one named like review/QA/verify, else any 'in progress'
    state); 'done' = a finishing state. None when the ticket's workflow offers nothing suitable."""
    if want == "done":
        return next((t for t in transitions if t["category"] == "done"), None) or \
            next((t for t in transitions if DONE_WORDS.search(t["to"] or t["name"])), None)
    named = next((t for t in transitions if REVIEW_WORDS.search(t["to"] or "") and t["category"] != "done"), None)
    return named or next((t for t in transitions if t["category"] == "indeterminate"), None)


def planned_after_pr(task, pr_url: str, with_tests: bool = False) -> List[str]:
    """What 'PR opened' would do to the ticket, in words, for the permission prompt."""
    field = _field("JIRA_PR_LINK_FIELD")
    plan = [f"Move {task.jira_key} to an in-between status (such as In Review), not Done: it is Done only once the PR is merged.",
            f"Put the pull request link ({pr_url}) in " + (f"the field {field}." if field else "a web link on the ticket (no JIRA_PR_LINK_FIELD is set).")]
    if with_tests:
        plan.append("Add a comment with the unit-test results (totals per subtask, and the reason for any failure that could not be fixed).")
    return plan


def _client(client):
    return client or jira_client.from_env()


def after_pr(task, pr_url: str, client: Optional[Any] = None, tests_text: str = "") -> Dict[str, Any]:
    """PR opened: attach its link, add the unit-test results, and move the ticket to an in-between status. Never raises for a Jira problem."""
    ok, why = availability(task)
    if client is None and not ok:
        return {"ok": False, "note": why, "done": []}
    done: List[str] = []
    try:
        c = _client(client)
        field = _field("JIRA_PR_LINK_FIELD")
        if field:
            c.set_field(task.jira_key, field, pr_url)
            done.append(f"link saved in {field}")
        else:
            c.add_remote_link(task.jira_key, pr_url, "Pull request")
            done.append("link added as a web link")
        if tests_text:
            c.add_comment(task.jira_key, f"Pull request: {pr_url}\n\n{tests_text}")
            done.append("unit-test results added as a comment")
        move = pick_transition(c.get_transitions(task.jira_key), "review")
        if move:
            c.transition(task.jira_key, move["id"])
            done.append(f"moved to {move['to'] or move['name']}")
        else:
            return {"ok": False, "done": done, "note": "The link is attached, but this ticket's workflow has no in-between status to move to."}
    except jira_client.JiraError as exc:
        return {"ok": False, "done": done, "note": str(exc)}
    return {"ok": True, "done": done, "note": f"{task.jira_key}: " + "; ".join(done) + "."}


def mark_done(task, client: Optional[Any] = None) -> Dict[str, Any]:
    """PR merged: move the ticket to Done."""
    ok, why = availability(task)
    if client is None and not ok:
        return {"ok": False, "note": why}
    try:
        c = _client(client)
        transitions = c.get_transitions(task.jira_key)
        move = pick_transition(transitions, "done")
        if not move:
            return {"ok": False, "note": "This ticket's workflow offers no way to Done from its current status (it offers: "
                                          + (", ".join(t["to"] or t["name"] for t in transitions) or "nothing") + ")."}
        c.transition(task.jira_key, move["id"])
    except jira_client.JiraError as exc:
        return {"ok": False, "note": str(exc)}
    return {"ok": True, "note": f"{task.jira_key} moved to {move['to'] or move['name']}."}


def report_blocked(task, reason: str, client: Optional[Any] = None) -> Dict[str, Any]:
    """The coding agent cannot go on until something else is done: say why on the ticket."""
    ok, why = availability(task)
    if client is None and not ok:
        return {"ok": False, "note": why}
    text = f"Blocked: {reason}"
    try:
        c = _client(client)
        field = _field("JIRA_BLOCKED_REASON_FIELD")
        if field:
            c.set_field(task.jira_key, field, text)
        else:
            c.add_comment(task.jira_key, text)
    except jira_client.JiraError as exc:
        return {"ok": False, "note": str(exc)}
    return {"ok": True, "note": f"The reason was written to {task.jira_key}" + (f" ({field})." if field else " as a comment (no JIRA_BLOCKED_REASON_FIELD is set).")}


def step_comment(step: Dict[str, Any], branch: str) -> str:
    """What one committed subtask says on the ticket: what was done, the commit, and the unit-test result."""
    from coding import testrun
    lines = [f"*Subtask {step['n']}: {_one_line(step['title'])}* committed" + (f" ({step['commit'][:8]} on {branch})" if step.get("commit") else ""), "",
             _one_line(step.get("summary") or "No summary.")]
    r = testrun.step_result(step)
    if r is None:
        lines += ["", "Unit tests: none were run for this subtask."]
    else:
        lines += ["", "Unit tests: " + r["headline"] + (f" (`{r['command']}`)" if r.get("command") else "")]
        lines += [f"* NOT FIXED: {_one_line(u['test'])} - {_one_line(u['reason'])[:300]}" for u in r["unresolved"]]
        if r["rerun"]:
            lines.append(r["rerun"])
    if step.get("tests_written"):
        lines += ["", "Test files: " + ", ".join(t["file"] for t in step["tests_written"])]
    return "\n".join(lines)


def report_step(task, step: Dict[str, Any], branch: str, client: Optional[Any] = None) -> Dict[str, Any]:
    """Post a committed subtask and its unit-test report to the ticket as a comment. Never raises for a Jira problem."""
    ok, why = availability(task)
    if client is None and not ok:
        return {"ok": False, "note": why}
    try:
        _client(client).add_comment(task.jira_key, step_comment(step, branch))
    except jira_client.JiraError as exc:
        return {"ok": False, "note": str(exc)}
    return {"ok": True, "note": f"Subtask {step['n']} was posted to {task.jira_key} as a comment."}


def planned_after_branch(task, branch: str, with_tests: bool = False) -> List[str]:
    """What the final update does when no pull request was opened for us: a comment only, the status is not moved."""
    return [f"Add a comment to {task.jira_key} saying the work is pushed on branch {branch} (no pull request link yet)."
            + (" It includes the unit-test results of every subtask." if with_tests else ""),
            "The ticket's status is not changed: it moves on only once a pull request exists."]


def after_branch(task, branch: str, link: str, client: Optional[Any] = None, tests_text: str = "") -> Dict[str, Any]:
    """The branch is pushed but no pull request was opened for us: say so on the ticket, with the test results."""
    ok, why = availability(task)
    if client is None and not ok:
        return {"ok": False, "note": why, "done": []}
    text = f"Work pushed on branch {branch}." + (f" Open a pull request: {link}" if link else "") + (f"\n\n{tests_text}" if tests_text else "")
    try:
        _client(client).add_comment(task.jira_key, text)
    except jira_client.JiraError as exc:
        return {"ok": False, "note": str(exc), "done": []}
    return {"ok": True, "done": ["comment added"], "note": f"{task.jira_key}: a comment with the branch and the unit-test results was added."}
