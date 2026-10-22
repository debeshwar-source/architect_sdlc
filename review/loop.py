"""The conversation between the coding agent and the reviewer, one pull request at a time, until the comments are settled.

Plain words: after a review is posted, a round goes like this, with a person approving at each of the two gates:
    1. run_fix        the coding agent works on the PR's own branch and answers every comment        (nothing leaves this computer)
    2. approve_fix    you read its change and replies; on approval: commit, push, and the replies appear in each GitHub thread
    3. run_recheck    the reviewer looks at only the new commits and judges every thread: settled or not  (nothing posted yet)
    4. approve_recheck  you read the verdicts; on approval: replies are posted, settled threads are resolved on GitHub, and any new
                        problem the fix introduced becomes new comments
Rounds repeat for as long as you want: there is no limit, because you approve every step. The loop ends when you merge or walk away.
The state is saved after every step (runs/review/), so closing the app loses nothing. Both sides post with YOUR GitHub token, so each
message is tagged with who said it.

Comment status: open (the coding agent must act) -> fixed | disputed (the reviewer must judge) -> resolved, or back to open.
A person may also skip an open comment (skipped: set aside, nothing is posted about it) and bring it back. A round is one fix followed by
a re-check; fixing comments one at a time before the re-check is still one round. After every fix the project's own tests are run here
(not taken on the agent's word) and approval is blocked while they fail.
"""

import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config
from coding import testrun
from coding import workspace as ws
from review import fixer, github, reviewer

STORE = config.RUNS_ROOT / "review"
CODER_TAG, REVIEWER_TAG, HUMAN_TAG = "🛠️ **Coding agent:** ", "🔍 **Reviewer:** ", "🧑 **Resolved by a person:** "
OnEvent = Optional[Callable[[str], None]]


class LoopError(Exception):
    """Something the person must fix or decide; the message is fit to show."""


# ---------- saved state ----------

def _path(owner: str, name: str, number: int) -> Path:
    return STORE / f"{owner}__{name}__{number}.json".lower()


def load(owner: str, name: str, number: int) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(_path(owner, name, number).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(state: Dict[str, Any]) -> None:
    path = _path(*state["repo"], state["number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def start_from_github(owner: str, name: str, pr: Dict[str, Any], threads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Begin the conversation from review comments that are ALREADY on the pull request (written by a person or an earlier review), so the
    coding agent can address them. Only unresolved threads are taken; the replies so far are kept as the thread's history."""
    comments = []
    for i, t in enumerate(threads, start=1):
        comments.append({"id": f"c{i}", "gh_id": t["gh_id"], "path": t["path"], "line": t["line"], "severity": "suggestion",
                         "body": f"**@{t['author']}:** {t['body'].strip()}", "status": "open",
                         "thread": [{"who": "human", "text": f"@{r['author']}: {r['body'].strip()}", "gh_id": r["gh_id"]} for r in t["replies"]]})
    state = {"repo": [owner, name], "number": pr["number"], "title": pr["title"], "branch": pr["branch"], "base": pr["base"],
             "head_repo": pr.get("head_repo", ""), "base_repo": pr.get("base_repo", ""), "head_sha": pr["head_sha"], "round": 0,
             "comments": comments, "session_id": None, "fix": None, "recheck": None, "error": None, "notes": []}
    _record(state, "import", "the comments loaded from GitHub", None, {})
    _settle(state)
    save(state)
    return state


def discard(state: Dict[str, Any]) -> None:
    """Forget the saved conversation (what is already on GitHub stays)."""
    _path(*state["repo"], state["number"]).unlink(missing_ok=True)


def _take(spare: List[Dict[str, Any]], path: str, line: Any, body: str) -> Optional[Dict[str, Any]]:
    """GitHub's record of a comment we just posted: matched on the text first (it comes back unchanged), else on file and line."""
    same = lambda p: p["path"] == path
    hit = next((p for p in spare if same(p) and (p.get("body") or "").strip() == body.strip()), None) \
        or next((p for p in spare if same(p) and p["line"] == line), None)
    if hit:
        spare.remove(hit)
    return hit


def relink(state: Dict[str, Any], threads: List[Dict[str, Any]]) -> int:
    """Reconnect comments whose GitHub thread was not recorded ('untracked') to the unresolved threads now on the pull request. Returns
    how many were reconnected."""
    spare = [{"path": t["path"], "line": t["line"], "gh_id": t["gh_id"], "body": t["body"]} for t in threads
             if t["gh_id"] not in {c["gh_id"] for c in state["comments"]}]
    n = 0
    for c in state["comments"]:
        if c["status"] != "untracked":
            continue
        hit = _take(spare, c["path"], c["line"], c["body"])
        if hit:
            c["gh_id"], c["status"] = hit["gh_id"], "open"
            n += 1
    if n:
        _settle(state)
        save(state)
    return n


def start(owner: str, name: str, pr: Dict[str, Any], posted: Dict[str, Any], chosen: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Begin the conversation from a first review that was just posted: `chosen` are the comments as posted, `posted` GitHub's answer."""
    spare = list(posted["comments"])
    comments = []
    for c in chosen:
        hit = _take(spare, c["path"], c["line"], c["body"])
        comments.append({"id": c["id"], "gh_id": hit["gh_id"] if hit else None, "path": c["path"], "line": c["line"],
                         "severity": c.get("severity", "suggestion"), "body": c["body"],
                         "status": "open" if hit else "untracked", "thread": []})
    state = {"repo": [owner, name], "number": pr["number"], "title": pr["title"], "branch": pr["branch"], "base": pr["base"],
             "head_repo": pr.get("head_repo", ""), "base_repo": pr.get("base_repo", ""), "head_sha": pr["head_sha"], "round": 0,
             "comments": comments, "session_id": None, "fix": None, "recheck": None, "error": None, "notes": []}
    _record(state, "review", "the review (its comments)", None,
            {"comment_ids": [c["gh_id"] for c in comments if c["gh_id"]], "review": {"id": posted.get("review_id"), "node": posted.get("review_node")}})
    _settle(state)
    save(state)
    return state


def _settle(state: Dict[str, Any]) -> None:
    """phase: awaiting_fix | awaiting_recheck | approved (everything settled) | unlinked | merged."""
    if state.get("merged"):
        state["phase"] = "merged"
        return
    live = [c for c in state["comments"] if c["status"] not in ("untracked", "skipped")]
    if not live and any(c["status"] == "untracked" for c in state["comments"]):
        state["phase"] = "unlinked"                     # comments exist but none is tied to a GitHub thread: nothing can be done yet
    elif not live or all(c["status"] == "resolved" for c in live):
        state["phase"] = "approved"
    elif any(c["status"] == "open" for c in live):
        state["phase"] = "awaiting_fix"
    else:
        state["phase"] = "awaiting_recheck"


def open_comments(state: Dict[str, Any], *statuses: str) -> List[Dict[str, Any]]:
    return [c for c in state["comments"] if c["status"] in statuses]


def _say(c: Dict[str, Any], who: str, text: str, gh_id: Optional[int] = None) -> None:
    c["thread"].append({"who": who, "text": text, "gh_id": gh_id})


# ---------- what was done, so each step can be undone ----------
# state["actions"] is a stack, oldest first. Each entry holds a snapshot of the conversation from BEFORE it ran and a list of what it did
# outside this computer (comments written, threads closed, a commit pushed), which is exactly what its undo reverses. Only the newest
# action can be undone: a later one may rest on it.

def _snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    return {"comments": copy.deepcopy(state["comments"]), "head_sha": state["head_sha"], "round": state["round"],
            "session_id": state.get("session_id")}


def _record(state: Dict[str, Any], kind: str, label: str, snapshot: Optional[Dict[str, Any]], undo: Dict[str, Any]) -> None:
    state.setdefault("actions", []).append({"kind": kind, "label": label, "at": time.strftime("%H:%M:%S"), "snapshot": snapshot, "undo": undo})


def actions(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Newest first: what can be undone, in the order it has to be undone."""
    return list(reversed(state.get("actions") or []))


# ---------- the PR's branch, on this computer ----------

def _same_repo(w: ws.Workspace, owner: str, name: str) -> bool:
    found = ws.github_repo(ws.git(w.tree, "remote", "get-url", "origin", check=False))
    return bool(found) and (found[0].lower(), found[1].lower()) == (owner.lower(), name.lower())


def open_workspace(state: Dict[str, Any]) -> ws.Workspace:
    """A worktree on the PR's branch. If a coding job already made this branch, its worktree is used (so the code is where the agent left
    it); otherwise the repository is cloned (or fetched) and a worktree is made. Raises LoopError for a fork, which we cannot push to."""
    owner, name = state["repo"]
    branch = state["branch"]
    if state.get("head_repo") and state["head_repo"] != state.get("base_repo"):
        raise LoopError(f"This pull request comes from a fork ({state['head_repo']}); the coding agent cannot push to it. "
                        "Apply the comments there yourself.")
    from coding import flow
    for job in flow.list_jobs():
        try:
            w = job.workspace
            if w.branch == branch and w.tree.is_dir() and _same_repo(w, owner, name):
                return w
        except (KeyError, ws.GitError, OSError):
            continue
    url = f"https://github.com/{owner}/{name}.git"
    repo = ws.WORK_ROOT / "clones" / f"{name}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"
    try:
        if repo.is_dir():
            ws.git(repo, "fetch", "--prune", "origin")
        else:
            repo.parent.mkdir(parents=True, exist_ok=True)
            ws.git(repo.parent, "clone", url, str(repo))
        tree = ws.WORK_ROOT / "trees" / f"{name}-pr{state['number']}"
        if not tree.is_dir():
            tree.parent.mkdir(parents=True, exist_ok=True)
            ws.git(repo, "worktree", "add", "-B", branch, str(tree), f"origin/{branch}")
        base_commit = ws.git(repo, "merge-base", f"origin/{state['base']}", f"origin/{branch}")
    except ws.GitError as exc:
        raise LoopError(f"Could not get the branch '{branch}' from GitHub: {exc}") from None
    return ws.Workspace(repo, tree, branch, state["base"], base_commit, url)


def _bring_up_to_date(w: ws.Workspace, branch: str) -> None:
    """Take in commits somebody else pushed to the PR's branch meanwhile (only possible when the agent has no edits waiting)."""
    ws.git(w.tree, "fetch", "origin", branch, check=False)
    if ws.changed_files(w, since=ws.head(w)):
        return
    try:
        ws.git(w.tree, "merge", "--ff-only", f"origin/{branch}")
    except ws.GitError:
        raise LoopError(f"The local copy of '{branch}' and GitHub's have diverged. Resolve that in Git, then try again.") from None


# ---------- gate 1: the coding agent answers the comments ----------

def run_fix(state: Dict[str, Any], guidance: str = "", model: Optional[str] = None, on_event: OnEvent = None,
            only: Optional[List[str]] = None) -> Dict[str, Any]:
    """The coding agent addresses the open comments, or just those in `only`. Asking again (with guidance) keeps the same comments."""
    pending = state.get("fix")
    if state["phase"] != "awaiting_fix" and not pending:
        raise LoopError("There is nothing for the coding agent to do right now.")
    ids = only or (pending or {}).get("targets")
    targets = [c for c in open_comments(state, "open") if not ids or c["id"] in ids]
    if not targets:
        raise LoopError("None of those comments is open.")
    w = open_workspace(state)
    if not pending:
        _bring_up_to_date(w, state["branch"])
    pr = {"number": state["number"], "title": state["title"], "branch": state["branch"], "base": state["base"], "body": ""}
    try:
        result = fixer.run(pr, targets, str(w.tree), guidance, model, resume=state.get("session_id"), on_event=on_event)
    except Exception as exc:  # noqa: BLE001 -- shown to the person, who can try again
        state["error"] = f"{type(exc).__name__}: {exc}"
        save(state)
        raise LoopError(state["error"]) from None
    state["error"] = None
    state["session_id"] = result.get("session_id") or state.get("session_id")
    state["fix"] = {"before": pending["before"] if pending else ws.head(w), "targets": [c["id"] for c in targets], "tests": None,
                    **result["answer"]}
    if on_event:
        on_event("Running the project's tests…")
    _test_fix(state, w, result["answer"].get("test_command") or "")
    save(state)
    return state


def _test_fix(state: Dict[str, Any], w: ws.Workspace, suggested: str = "", command: str = "") -> None:
    """Run the tests here, on the agent's change, and keep the result with the fix. The command: the one given, else the one the person set
    earlier, else the agent's, else what the build files point to. Nothing is run when the agent changed no file."""
    fix = state["fix"]
    if not ws.changed_files(w, since=fix["before"]):
        fix["tests"] = {"command": "", "report": None, "note": "The coding agent changed no file, so there is nothing to test."}
        return
    command = (command or state.get("test_command") or suggested or testrun.detect_command(w.tree)).strip()
    if not command:
        fix["tests"] = {"command": "", "report": None, "note": "No test command is known for this repository; set one to run the tests."}
        return
    report = testrun.run(command, w.tree)
    kept = {k: v for k, v in report.items() if k != "cases"}
    kept["failed_cases"] = [c for c in report.get("cases") or [] if c.get("status") == "failed"][:25]
    fix["tests"] = {"command": command, "report": kept, "note": ""}


def run_tests(state: Dict[str, Any], command: str) -> Dict[str, Any]:
    """Run the tests again (after the person changed the command, or to re-check). The command is remembered for this pull request."""
    if not state.get("fix"):
        raise LoopError("The coding agent has no change waiting.")
    ok, why = testrun.validate_command(command)
    if not ok:
        raise LoopError(why)
    state["test_command"] = command.strip()
    _test_fix(state, open_workspace(state), command=command)
    save(state)
    return state


def tests_block(state: Dict[str, Any]) -> str:
    """Why the fix cannot be approved yet because of its tests ('' if it can): they ran and failed, or the build broke. Tests that could
    not be run at all do not block (the screen warns instead)."""
    report = ((state.get("fix") or {}).get("tests") or {}).get("report")
    if report and testrun.needs_fix(report):
        return "The tests fail: " + testrun.headline(report) + "."
    return ""


# ---------- a person answers and settles a comment themselves ----------

def respond_and_resolve(state: Dict[str, Any], comment_id: str, text: str) -> Dict[str, Any]:
    """The person replies in the comment's thread and marks it resolved, with no agent involved (they disagreed, or fixed it by hand). The
    reviewer's re-check does not re-judge it: a person's word settles it. Undoable (the reply is deleted, the thread reopened)."""
    c = next((c for c in state["comments"] if c["id"] == comment_id), None)
    if not c or c["status"] not in ("open", "fixed", "disputed", "skipped") or not c["gh_id"]:
        raise LoopError("That comment cannot be answered here.")
    if state.get("fix") or state.get("recheck"):
        raise LoopError("Finish or discard what is waiting for your approval first.")
    if not text.strip():
        raise LoopError("Write a reply: it is what the thread will show.")
    owner, name = state["repo"]
    before = _snapshot(state)
    try:
        rid = github.reply(owner, name, state["number"], c["gh_id"], HUMAN_TAG + text.strip())
    except github.GithubError as exc:
        raise LoopError(f"The reply could not be posted, so nothing was changed. {exc}") from None
    closed: List[int] = []
    notes: List[str] = []
    try:
        closed = github.resolve_threads(owner, name, state["number"], [c["gh_id"]])
        if not closed:
            notes.append("The thread could not be marked resolved on GitHub (your reply is there; resolve it by hand if you want it collapsed).")
    except github.GithubError as exc:
        notes.append(f"The thread was not marked resolved on GitHub ({exc}). Your reply is there.")
    _say(c, "human", text.strip(), rid)
    c["status"], c["by"] = "resolved", "human"
    _record(state, "respond", f"your response on {c['path']}:{c['line']}", before, {"reply_ids": [rid], "resolved": closed})
    state["notes"] = notes
    _settle(state)
    save(state)
    return state


# ---------- merging ----------

def mark_merged(state: Dict[str, Any], sha: str) -> Dict[str, Any]:
    """The pull request was merged from here: the conversation is finished, and nothing in it can be undone any more."""
    state["merged"] = {"sha": sha, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _settle(state)
    save(state)
    return state


def unsettled(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The comments that are not resolved: what a merge would be overriding."""
    return [c for c in state["comments"] if c["status"] in ("open", "fixed", "disputed", "skipped")]


def last_summary(state: Dict[str, Any]) -> str:
    """One plain sentence on what the newest action just did on GitHub, for the confirmation shown right after it."""
    a = (state.get("actions") or [{}])[-1]
    u = a.get("undo") or {}
    replies = len(u.get("reply_ids") or [])
    if a.get("kind") == "fix":
        rv = u.get("revert")
        return f"✅ Pushed `{rv['after'][:8]}` to `{state['branch']}`; posted {replies} repl{'y' if replies == 1 else 'ies'}." if rv \
            else f"✅ No code changed; posted {replies} repl{'y' if replies == 1 else 'ies'}."
    if a.get("kind") == "recheck":
        return (f"✅ Posted {replies} repl{'y' if replies == 1 else 'ies'}; resolved {len(u.get('resolved') or [])} thread(s)"
                + (f"; added {len(u.get('new_comment_ids') or [])} new comment(s)." if u.get("new_comment_ids") else "."))
    if a.get("kind") == "respond":
        return "✅ Replied and resolved." if u.get("resolved") else "✅ Replied."
    return ""


# ---------- setting a comment aside ----------

def skip_comment(state: Dict[str, Any], comment_id: str) -> Dict[str, Any]:
    """Set one open comment aside: the coding agent will not work on it and nothing is posted about it. Reversible."""
    c = next((c for c in state["comments"] if c["id"] == comment_id), None)
    if not c or c["status"] != "open":
        raise LoopError("Only an open comment can be skipped.")
    if state.get("fix") or state.get("recheck"):
        raise LoopError("Finish or discard what is waiting for your approval first.")
    c["status"] = "skipped"
    _settle(state)
    save(state)
    return state


def unskip_comment(state: Dict[str, Any], comment_id: str) -> Dict[str, Any]:
    c = next((c for c in state["comments"] if c["id"] == comment_id), None)
    if not c or c["status"] != "skipped":
        raise LoopError("That comment is not skipped.")
    c["status"] = "open"
    _settle(state)
    save(state)
    return state


def fix_diff(state: Dict[str, Any]) -> str:
    """What the coding agent changed since the round began (not yet committed)."""
    return ws.diff(open_workspace(state), since=state["fix"]["before"]) if state.get("fix") else ""


def approve_fix(state: Dict[str, Any], replies: Dict[str, str], message: str, allow_failing_tests: bool = False) -> Dict[str, Any]:
    """Commit and push the agent's change, then post its replies. Raises LoopError, before anything is committed or posted, if its tests
    fail (unless the person says the failures are not this change's doing) or if the push fails."""
    fix = state["fix"]
    if tests_block(state) and not allow_failing_tests:
        raise LoopError(tests_block(state) + " Fix them, or confirm the failures are not caused by this change.")
    w = open_workspace(state)
    owner, name = state["repo"]
    before = _snapshot(state)
    after = ""
    changed = bool(ws.changed_files(w, since=fix["before"]))
    try:
        if changed:
            after = ws.commit(w, message.strip() or f"Address review comments (round {state['round'] + 1})")
            ws.push(w)
    except ws.GitError as exc:
        raise LoopError(f"The change could not be pushed to GitHub: {exc}") from None
    by_id = {c["id"]: c for c in state["comments"]}
    failed, reply_ids = [], []
    for o in fix["outcomes"]:
        c, text = by_id[o["id"]], (replies.get(o["id"]) or o["reply"]).strip()
        if o["outcome"] == "needs_input" or not text:
            continue                                    # a question for the person, not for the reviewer: stays open, nothing posted
        try:
            rid = github.reply(owner, name, state["number"], c["gh_id"], CODER_TAG + text)
        except github.GithubError as exc:
            failed.append(f"{c['path']}:{c['line']} ({exc})")
            continue
        _say(c, "coder", text, rid)
        reply_ids.append(rid)
        c["status"] = "fixed" if o["outcome"] == "fixed" else "disputed"
    _record(state, "fix", f"the coding agent's fixes (round {state['round'] + 1})", before,
            {"reply_ids": reply_ids, "revert": {"before": fix["before"], "after": after} if after else None})
    state["fix"] = None
    state["notes"] = [f"The change was pushed, but these replies could not be posted: {', '.join(failed)}"] if failed else []
    _settle(state)
    save(state)
    return state


# ---------- gate 2: the reviewer judges the answers ----------

def run_recheck(state: Dict[str, Any], model: Optional[str] = None, on_event: OnEvent = None,
                only: Optional[List[str]] = None) -> Dict[str, Any]:
    """The reviewer judges the comments answered so far, or just those in `only`. A person may also send back a comment they had set aside."""
    owner, name = state["repo"]
    if only:
        threads = [c for c in open_comments(state, "fixed", "disputed", "skipped") if c["gh_id"] and c["id"] in only]
    else:
        threads = [c for c in open_comments(state, "fixed", "disputed") if c["gh_id"]]
    if not threads:
        raise LoopError("There are no answered comments to re-check.")
    pr = github.get_pr(owner, name, state["number"])
    delta = github.compare_diff(owner, name, state["head_sha"], pr["head_sha"]) if pr["head_sha"] != state["head_sha"] else ""
    full = github.get_diff(owner, name, state["number"])
    tree, note = reviewer._tree_or_note(owner, name, state["number"], on_event)
    result = reviewer.recheck(pr, threads, delta, full, next_id=len(state["comments"]) + 1, model=model, tree=tree, on_event=on_event)
    if note:
        result["notes"] = [note] + result["notes"]
    state["recheck"] = {**result, "new_head": pr["head_sha"]}
    save(state)
    return state


def approve_recheck(state: Dict[str, Any], resolved: Dict[str, bool], replies: Dict[str, str], new_comments: List[Dict[str, Any]],
                    summary: str) -> Dict[str, Any]:
    """Post the reviewer's replies, resolve the settled threads, and post any new comments as a fresh review. `resolved`/`replies` are the
    person's final choices per thread id; `new_comments` the ones they kept ({'id','path','line','body'})."""
    owner, name = state["repo"]
    rc = state["recheck"]
    before = _snapshot(state)
    by_id = {c["id"]: c for c in state["comments"]}
    notes: List[str] = []
    to_resolve: List[int] = []
    reply_ids: List[int] = []
    closed: List[int] = []
    new_ids: List[int] = []
    for v in rc["verdicts"]:
        c = by_id[v["id"]]
        text = (replies.get(v["id"]) if v["id"] in replies else v["reply"]).strip()
        if text:
            try:
                rid = github.reply(owner, name, state["number"], c["gh_id"], REVIEWER_TAG + text)
            except github.GithubError as exc:
                notes.append(f"Reply on {c['path']}:{c['line']} not posted ({exc})")
                continue
            _say(c, "reviewer", text, rid)
            reply_ids.append(rid)
        if resolved.get(v["id"], v["resolved"]):
            c["status"] = "resolved"
            to_resolve.append(c["gh_id"])
        else:
            c["status"] = "open"
    if to_resolve:
        try:
            closed.extend(github.resolve_threads(owner, name, state["number"], to_resolve))
            if len(closed) < len(to_resolve):
                notes.append("Some threads could not be marked resolved on GitHub (they are settled here, and the replies say so).")
        except github.GithubError as exc:
            notes.append(f"Threads were not marked resolved on GitHub ({exc}).")
    if new_comments:
        posted = github.post_review(owner, name, state["number"], rc["new_head"], summary.strip(), "COMMENT", new_comments)
        spare = list(posted["comments"])
        for n in new_comments:
            hit = _take(spare, n["path"], n["line"], n["body"])
            if hit:
                new_ids.append(hit["gh_id"])
            state["comments"].append({"id": n["id"], "gh_id": hit["gh_id"] if hit else None, "path": n["path"], "line": n["line"],
                                      "severity": n.get("severity", "suggestion"), "body": n["body"],
                                      "status": "open" if hit else "untracked", "thread": []})
    _record(state, "recheck", f"the reviewer's re-check (round {state['round'] + 1})", before,
            {"reply_ids": reply_ids, "resolved": closed, "new_comment_ids": new_ids})
    state["head_sha"] = rc["new_head"]
    state["round"] += 1                                 # a round is a fix AND its re-check
    state["recheck"] = None
    state["notes"] = notes
    _settle(state)
    save(state)
    return state


# ---------- clearing an earlier review ----------

def split_open_threads(owner: str, name: str, number: int) -> tuple:
    """(this app's earlier comments, everyone else's open threads), from one look at GitHub. This app's: the thread's first comment is the
    token's own and carries the reviewer's severity tag. Found on GitHub itself, so it works even when the app lost its record of them.
    The first list: [{'root', 'path', 'line', 'body', 'replies': [ids of the token's own replies in the thread]}]."""
    me = github.my_login().lower()
    tags = tuple(reviewer.BADGE.values())
    mine, others = [], []
    for t in github.unresolved_threads(owner, name, number):
        if t["author"].lower() == me and t["body"].lstrip().startswith("**") and any(tag in t["body"][:40] for tag in tags):
            mine.append({"root": t["gh_id"], "path": t["path"], "line": t["line"], "body": t["body"],
                         "replies": [r["gh_id"] for r in t["replies"] if r["author"].lower() == me]})
        else:
            others.append(t)
    return mine, others


def earlier_app_comments(owner: str, name: str, number: int) -> List[Dict[str, Any]]:
    return split_open_threads(owner, name, number)[0]


def clear_comments(owner: str, name: str, items: List[Dict[str, Any]]) -> int:
    """Delete those comments from GitHub: the token's replies first, then each thread's first comment. Returns how many were deleted.
    Someone else's reply in a thread is never touched. Safe to repeat after a failure."""
    ids = [r for it in items for r in it["replies"]] + [it["root"] for it in items]
    _delete_all(owner, name, ids)
    return len(ids)


def remove_summaries(owner: str, name: str, number: int, reviews: List[Dict[str, Any]]) -> List[str]:
    """Take the summary comment of each of these reviews off the PR. Never raises: returns one plain-words line per review saying what
    happened, since GitHub may refuse and the rest must still go ahead."""
    said = []
    for r in reviews:
        try:
            how = github.remove_review_summary(owner, name, number, r["id"], r.get("node"))
        except github.GithubError as exc:
            said.append(str(exc))
            continue
        said.append({"deleted": "A review summary was deleted.", "hidden": "A review summary was hidden (collapsed as outdated).",
                     "replaced": "A review summary could not be deleted, so its text was replaced with a short note."}[how])
    return said


def _app_summaries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Summaries this app wrote on the PR: those it recorded and those carrying its marker, whichever it can still find."""
    owner, name = state["repo"]
    known = {a["undo"]["review"]["id"]: a["undo"]["review"] for a in state.get("actions") or []
             if a["kind"] == "review" and (a["undo"].get("review") or {}).get("id")}
    try:
        for r in github.my_review_summaries(owner, name, state["number"]):
            if r["ours"]:
                known.setdefault(r["id"], {"id": r["id"], "node": r["node"]})
        live = {r["id"] for r in github.my_review_summaries(owner, name, state["number"])}
    except github.GithubError:
        return list(known.values())
    return [r for i, r in known.items() if i in live]          # one that is already gone needs nothing


def start_over(state: Dict[str, Any], delete_from_github: bool) -> Dict[str, Any]:
    """Forget this conversation and, if asked, remove from GitHub what this app put on the pull request: its comments (found on GitHub, plus
    any the record knows of that were resolved since) and its summary comments. Nothing is forgotten if a comment cannot be deleted, so it
    can be tried again; a summary GitHub refuses to remove is reported, not fatal. Returns {'deleted': n, 'notes': [...]}."""
    owner, name = state["repo"]
    removed, notes = 0, []
    if delete_from_github:
        items = earlier_app_comments(owner, name, state["number"])
        known = {i["root"] for i in items} | {r for i in items for r in i["replies"]}
        extra = [i for a in state.get("actions") or [] if a["kind"] in ("review", "recheck")
                 for i in (a["undo"].get("comment_ids") or []) + (a["undo"].get("new_comment_ids") or []) if i and i not in known]
        removed = clear_comments(owner, name, items + [{"root": i, "replies": []} for i in extra])
        notes = remove_summaries(owner, name, state["number"], _app_summaries(state))
    discard(state)
    return {"deleted": removed, "notes": notes}


# ---------- undo ----------

def discard_fix(state: Dict[str, Any]) -> Dict[str, Any]:
    """Undo the coding agent's pending work (nothing has been committed or posted): its edits are removed and it starts a fresh conversation."""
    if not state.get("fix"):
        raise LoopError("The coding agent has no change waiting.")
    ws.reset_to(open_workspace(state), state["fix"]["before"])
    state.update(fix=None, session_id=None, error=None)
    save(state)
    return state


def discard_recheck(state: Dict[str, Any]) -> Dict[str, Any]:
    """Undo the reviewer's pending re-check (nothing has been posted): it can be run again."""
    if not state.get("recheck"):
        raise LoopError("The reviewer has no re-check waiting.")
    state["recheck"] = None
    save(state)
    return state


def undo_blocker(state: Dict[str, Any]) -> str:
    """Why nothing can be undone right now ('' if the newest action can). A pending change or re-check is discarded first."""
    if state.get("merged"):
        return "The pull request is merged, so nothing can be undone."
    if state.get("fix") or state.get("recheck"):
        return "Something is waiting for your approval above: discard it first."
    if not state.get("actions"):
        return "Nothing has been done yet."
    return ""


def _delete_all(owner: str, name: str, ids: List[Optional[int]]) -> None:
    for cid in ids:
        if cid:
            try:
                github.delete_comment(owner, name, cid)
            except github.GithubError as exc:
                raise LoopError(f"A comment could not be deleted on GitHub ({exc}). Nothing else was changed; try again.") from None


def undo_last(state: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse the newest action, on this computer and on GitHub, and put the conversation back as it was before it. Safe to repeat after
    a failure: parts already reversed are not reversed twice."""
    why = undo_blocker(state)
    if why:
        raise LoopError(why)
    owner, name = state["repo"]
    action = state["actions"][-1]
    undo = action["undo"]
    if action["kind"] == "import":
        discard(state)                                  # these comments are not ours: only the local copy is forgotten
        return state
    if action["kind"] == "review":
        _delete_all(owner, name, undo["comment_ids"])
        rv = undo.get("review") or {}
        state["undo_notes"] = remove_summaries(owner, name, state["number"], [rv] if rv.get("id") else [])
        discard(state)                                  # back to the screen before any review
        return state
    if action["kind"] == "fix":
        if undo.get("revert"):
            w = open_workspace(state)
            ws.git(w.tree, "fetch", "origin", state["branch"], check=False)
            if ws.git(w.tree, "rev-parse", f"origin/{state['branch']}", check=False) != undo["revert"]["after"] or ws.head(w) != undo["revert"]["after"]:
                raise LoopError("The branch has new commits since the coding agent's push, so its fix cannot be reverted automatically.")
            try:
                ws.revert_since(w, undo["revert"]["before"], f"Revert: {action['label']}")
                ws.push(w)
            except ws.GitError as exc:
                raise LoopError(str(exc)) from None
            undo["revert"] = None                       # done: a retry after a later failure must not revert again
            save(state)
        _delete_all(owner, name, undo["reply_ids"])
    elif action["kind"] in ("recheck", "respond"):
        if undo["resolved"]:
            try:
                github.resolve_threads(owner, name, state["number"], undo["resolved"], resolved=False)
            except github.GithubError as exc:
                raise LoopError(f"Threads could not be reopened on GitHub ({exc}). Nothing else was changed; try again.") from None
            undo["resolved"] = []
            save(state)
        _delete_all(owner, name, undo["reply_ids"] + undo.get("new_comment_ids", []))
    snap = action["snapshot"]
    state.update(comments=snap["comments"], head_sha=snap["head_sha"], round=snap["round"], session_id=snap["session_id"], notes=[])
    if action["kind"] == "fix":
        state["session_id"] = None                      # the agent's memory holds edits that no longer exist
    state["actions"].pop()
    _settle(state)
    save(state)
    return state
