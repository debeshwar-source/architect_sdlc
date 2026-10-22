"""GitHub side of the code review: list the pull requests waiting to be merged, read one's diff, post a review on it.

Plain words: the only place that talks to GitHub for reviews. Needs GITHUB_TOKEN (or GH_TOKEN) with read/write access to pull
requests. Nothing here runs the AI, so it is easy to test with a fake `_request`.
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API = "https://api.github.com"
MARK = "<!-- sdlc-review -->"                        # hidden in GitHub; tells a summary this app wrote from one a person wrote
EVENTS = ("COMMENT", "REQUEST_CHANGES")      # APPROVE is deliberately absent: approving a merge is a human decision made on GitHub


class GithubError(Exception):
    """GitHub refused or could not be reached; the message is fit to show to the user. `status` is the HTTP code, when there was one."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def parse_repo(text: str) -> Optional[Tuple[str, str]]:
    """'owner/name', a github.com URL (https or ssh) -> (owner, name)."""
    m = re.search(r"(?:github\.com[:/]+)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", (text or "").strip())
    return (m.group(1), m.group(2)) if m else None


def default_repo() -> str:
    """REVIEW_GITHUB_REPO, else this project's own `origin` remote; '' if neither is known."""
    wanted = os.environ.get("REVIEW_GITHUB_REPO", "").strip()
    if wanted:
        return wanted
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=Path(__file__).resolve().parent.parent,
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    found = parse_repo(url) if "github.com" in url else None
    return f"{found[0]}/{found[1]}" if found else ""


def _request(method: str, path: str, body: Optional[dict] = None, accept: str = "application/vnd.github+json") -> Any:
    if not token():
        raise GithubError("Set GITHUB_TOKEN (in .env) so I can read and comment on pull requests.")
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        hints = {401: "The token is invalid or expired.",
                 403: "The token lacks permission (pull requests: read and write), or the organization requires SSO authorization for it.",
                 404: "Either the repository name is wrong, or the token cannot see it: for a private or organization repo, give it the "
                      "`repo` scope (classic) or this repository with Pull requests + Contents access (fine-grained), and authorize it for SSO."}
        raise GithubError(f"GitHub answered {exc.code}. {hints.get(exc.code, '')} Details: {detail}", exc.code) from None
    except OSError as exc:
        raise GithubError(f"Could not reach GitHub ({exc}).") from None
    return raw if accept.endswith(".diff") else json.loads(raw or "null")


def _summary(p: Dict[str, Any]) -> Dict[str, Any]:
    return {"number": p["number"], "title": p["title"], "author": (p.get("user") or {}).get("login", "?"),
            "branch": p["head"]["ref"], "base": p["base"]["ref"], "head_sha": p["head"]["sha"], "url": p["html_url"],
            "head_repo": ((p["head"].get("repo") or {}).get("full_name") or "").lower(),
            "base_repo": ((p["base"].get("repo") or {}).get("full_name") or "").lower(),
            "body": p.get("body") or "", "draft": bool(p.get("draft")), "updated": p.get("updated_at", "")}


def list_open_prs(owner: str, name: str) -> List[Dict[str, Any]]:
    """Pull requests waiting for a merge: open and not drafts, most recently updated first."""
    data = _request("GET", f"/repos/{owner}/{name}/pulls?state=open&sort=updated&direction=desc&per_page=50")
    return [_summary(p) for p in data if not p.get("draft")]


def get_pr(owner: str, name: str, number: int) -> Dict[str, Any]:
    return _summary(_request("GET", f"/repos/{owner}/{name}/pulls/{number}"))


def get_diff(owner: str, name: str, number: int) -> str:
    return _request("GET", f"/repos/{owner}/{name}/pulls/{number}", accept="application/vnd.github.v3.diff")


def post_review(owner: str, name: str, number: int, head_sha: str, body: str, event: str,
                comments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One review holding all the comments, pinned to the commit that was reviewed. Returns {'url', 'comments': [{'path','line','gh_id'}]}:
    the review's web address and GitHub's ids of the comments just posted (needed to reply in their threads later).
    `comments`: [{'path', 'line', 'body'}] on the new side of the diff."""
    if event not in EVENTS:
        raise GithubError(f"Unsupported review type {event!r}.")
    payload = {"commit_id": head_sha, "event": event, "body": (body.rstrip() + "\n\n" + MARK) if body.strip() else "",
               "comments": [{"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]} for c in comments]}
    out = _request("POST", f"/repos/{owner}/{name}/pulls/{number}/reviews", payload)
    posted: List[Dict[str, Any]] = []
    if comments and out.get("id"):
        listed = _request("GET", f"/repos/{owner}/{name}/pulls/{number}/reviews/{out['id']}/comments?per_page=100")
        posted = [{"path": c["path"], "line": c.get("line") or c.get("original_line"), "gh_id": c["id"], "body": c.get("body", "")} for c in listed]
    return {"url": out.get("html_url", ""), "comments": posted, "review_id": out.get("id"), "review_node": out.get("node_id")}


def reply(owner: str, name: str, number: int, comment_id: int, body: str) -> Optional[int]:
    """Answer inside an existing review comment's thread. Returns the new comment's id (kept so the reply can be undone)."""
    out = _request("POST", f"/repos/{owner}/{name}/pulls/{number}/comments/{comment_id}/replies", {"body": body})
    return (out or {}).get("id")


def delete_comment(owner: str, name: str, comment_id: int) -> None:
    """Delete one review comment or reply (the token's own). Already gone is fine: undoing twice must not fail."""
    try:
        _request("DELETE", f"/repos/{owner}/{name}/pulls/comments/{comment_id}")
    except GithubError as exc:
        if exc.status != 404:
            raise


def compare_diff(owner: str, name: str, old_sha: str, new_sha: str) -> str:
    """What changed between two commits of the PR's branch (the new commits only)."""
    return _request("GET", f"/repos/{owner}/{name}/compare/{old_sha}...{new_sha}", accept="application/vnd.github.v3.diff")


def resolve_threads(owner: str, name: str, number: int, comment_ids: List[int], resolved: bool = True) -> List[int]:
    """Mark the review threads that start with these comments as resolved (or, with resolved=False, open again). Returns the comment ids
    whose thread is now in that state; a thread that cannot be changed (no permission) is skipped, since the replies in it say what is
    settled."""
    query = ("query($o:String!,$n:String!,$num:Int!){repository(owner:$o,name:$n){pullRequest(number:$num){reviewThreads(first:100)"
             "{nodes{id isResolved comments(first:1){nodes{databaseId}}}}}}}")
    data = _request("POST", "/graphql", {"query": query, "variables": {"o": owner, "n": name, "num": number}})
    nodes = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
    by_comment = {(n["comments"]["nodes"] or [{}])[0].get("databaseId"): n for n in nodes}
    done: List[int] = []
    for cid in comment_ids:
        node = by_comment.get(cid)
        if not node:
            continue
        if node["isResolved"] == resolved:
            done.append(cid)
            continue
        verb = "resolveReviewThread" if resolved else "unresolveReviewThread"
        out = _request("POST", "/graphql", {"query": f"mutation($id:ID!){{{verb}(input:{{threadId:$id}}){{thread{{isResolved}}}}}}",
                                            "variables": {"id": node["id"]}})
        if not (out or {}).get("errors"):
            done.append(cid)
    return done


def unresolved_threads(owner: str, name: str, number: int) -> List[Dict[str, Any]]:
    """The review threads on a PR that nobody has resolved yet, whoever wrote them (a person on GitHub, an earlier review, a bot):
    [{'gh_id', 'path', 'line', 'author', 'body', 'replies': [{'author', 'body', 'gh_id'}]}]. A thread on code that has since changed is
    kept, anchored on the line it had."""
    query = ("query($o:String!,$n:String!,$num:Int!){repository(owner:$o,name:$n){pullRequest(number:$num){reviewThreads(first:100)"
             "{nodes{isResolved comments(first:30){nodes{databaseId body path line originalLine author{login}}}}}}}}")
    data = _request("POST", "/graphql", {"query": query, "variables": {"o": owner, "n": name, "num": number}})
    nodes = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
    out = []
    for t in nodes:
        comments = (t.get("comments") or {}).get("nodes") or []
        if t.get("isResolved") or not comments:
            continue
        root = comments[0]
        who = lambda c: (c.get("author") or {}).get("login") or "someone"
        out.append({"gh_id": root["databaseId"], "path": root["path"], "line": root.get("line") or root.get("originalLine") or 1,
                    "author": who(root), "body": root.get("body") or "",
                    "replies": [{"author": who(c), "body": c.get("body") or "", "gh_id": c["databaseId"]} for c in comments[1:]]})
    return out


def my_login() -> str:
    """The GitHub account the token acts as (the author of everything this app posts)."""
    return str((_request("GET", "/user") or {}).get("login") or "")


def my_review_summaries(owner: str, name: str, number: int) -> List[Dict[str, Any]]:
    """The summary text (the main comment) of every review the token's account left on the PR: [{'id','node','state','body','url','ours'}].
    `ours` is True when this app wrote it (it carries the hidden marker); older ones cannot be told apart from a person's own."""
    me = my_login().lower()
    reviews = _request("GET", f"/repos/{owner}/{name}/pulls/{number}/reviews?per_page=100") or []
    out = []
    for r in reviews:
        text = (r.get("body") or "").replace(MARK, "").strip()
        if (r.get("user") or {}).get("login", "").lower() == me and text:
            out.append({"id": r["id"], "node": r.get("node_id"), "state": r.get("state", ""), "body": text, "url": r.get("html_url", ""),
                        "ours": MARK in (r.get("body") or "")})
    return out


def remove_review_summary(owner: str, name: str, number: int, review_id: int, node_id: Optional[str]) -> str:
    """Get a review's summary comment off the PR. GitHub does not let a submitted review be deleted through the plain API, so these are
    tried in turn and the first that works is reported: 'deleted', 'hidden' (collapsed as outdated, like the Hide button) or 'replaced'
    (its text swapped for a short note). Raises GithubError with the review's link if none works."""
    steps = []
    if node_id:
        steps.append(("deleted", "mutation($id:ID!){deletePullRequestReview(input:{pullRequestReviewId:$id}){pullRequestReview{id}}}"))
        steps.append(("hidden", "mutation($id:ID!){minimizeComment(input:{subjectId:$id,classifier:OUTDATED}){minimizedComment{isMinimized}}}"))
    for result, query in steps:
        try:
            out = _request("POST", "/graphql", {"query": query, "variables": {"id": node_id}})
        except GithubError:
            continue
        if not (out or {}).get("errors"):
            return result
    try:
        _request("PUT", f"/repos/{owner}/{name}/pulls/{number}/reviews/{review_id}", {"body": "_Superseded by a newer review._"})
        return "replaced"
    except GithubError:
        pass
    raise GithubError(f"GitHub would not remove this summary. Delete it on GitHub: https://github.com/{owner}/{name}/pull/{number}")


FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


def merge_info(owner: str, name: str, number: int) -> Dict[str, Any]:
    """What a person needs to see before merging: whether GitHub would allow it, the state of the checks, and which merge methods the
    repository permits. Checks that cannot be read (the token may not be allowed to) are reported as unknown, never as passing."""
    pr = _request("GET", f"/repos/{owner}/{name}/pulls/{number}")
    sha = pr["head"]["sha"]
    methods: List[str] = []
    try:
        repo = _request("GET", f"/repos/{owner}/{name}") or {}
        methods = [m for m, flag in (("merge", "allow_merge_commit"), ("squash", "allow_squash_merge"), ("rebase", "allow_rebase_merge"))
                   if repo.get(flag, True)]
    except GithubError:
        methods = ["merge", "squash", "rebase"]
    failing: List[str] = []
    pending: List[str] = []
    known = True
    try:
        for r in (_request("GET", f"/repos/{owner}/{name}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs", []):
            if r.get("status") != "completed":
                pending.append(r.get("name", "check"))
            elif r.get("conclusion") in FAILED:
                failing.append(r.get("name", "check"))
        for s in (_request("GET", f"/repos/{owner}/{name}/commits/{sha}/status") or {}).get("statuses", []):
            if s.get("state") == "pending":
                pending.append(s.get("context", "status"))
            elif s.get("state") in ("failure", "error"):
                failing.append(s.get("context", "status"))
    except GithubError:
        known = False
    return {"state": pr.get("state"), "merged": bool(pr.get("merged")), "draft": bool(pr.get("draft")), "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state") or "unknown", "head_sha": sha, "title": pr["title"], "base": pr["base"]["ref"],
            "failing": failing, "pending": pending, "checks_known": known, "methods": methods or ["merge"]}


def merge_pr(owner: str, name: str, number: int, head_sha: str, method: str) -> Dict[str, Any]:
    """Merge through GitHub's own endpoint, so branch protection, required checks and required reviews still apply (nothing is bypassed).
    `head_sha` pins the merge to the commit the person looked at: if the branch moved since, GitHub refuses."""
    if method not in ("merge", "squash", "rebase"):
        raise GithubError(f"Unsupported merge method {method!r}.")
    try:
        out = _request("PUT", f"/repos/{owner}/{name}/pulls/{number}/merge", {"merge_method": method, "sha": head_sha}) or {}
    except GithubError as exc:
        why = {405: "GitHub will not merge this pull request now (a required check or review is missing, there is a conflict, or it is a draft).",
               409: "New commits were pushed after you looked, so nothing was merged. Check the status again."}.get(exc.status, "")
        raise GithubError((why + " " if why else "") + str(exc), exc.status) from None
    return {"merged": bool(out.get("merged")), "sha": out.get("sha", ""), "message": out.get("message", "")}


def list_merged_prs(owner: str, name: str, limit: int = 20) -> List[Dict[str, Any]]:
    """The most recently merged pull requests: [{'number','title','author','branch','base','url','merged_at','merge_sha','body'}]."""
    data = _request("GET", f"/repos/{owner}/{name}/pulls?state=closed&sort=updated&direction=desc&per_page=50") or []
    out = [{"number": p["number"], "title": p["title"], "author": (p.get("user") or {}).get("login", "?"), "branch": p["head"]["ref"],
            "base": p["base"]["ref"], "url": p["html_url"], "merged_at": p["merged_at"], "merge_sha": p.get("merge_commit_sha") or "",
            "body": p.get("body") or ""} for p in data if p.get("merged_at")]
    return sorted(out, key=lambda p: p["merged_at"], reverse=True)[:limit]


def pr_files(owner: str, name: str, number: int) -> List[Dict[str, Any]]:
    """The files a pull request changed: [{'path','status','added','removed'}]."""
    data = _request("GET", f"/repos/{owner}/{name}/pulls/{number}/files?per_page=100") or []
    return [{"path": f["filename"], "status": f.get("status", ""), "added": f.get("additions", 0), "removed": f.get("deletions", 0)} for f in data]


def comment_on_pr(owner: str, name: str, number: int, body: str) -> str:
    """A plain comment in the pull request's conversation (not on a line of code). Returns its web address."""
    return (_request("POST", f"/repos/{owner}/{name}/issues/{number}/comments", {"body": body}) or {}).get("html_url", "")


def pr_status(owner: str, name: str, number: int) -> Dict[str, Any]:
    """Where a pull request stands: {'state': 'open'|'closed', 'merged': bool, 'merge_sha', 'head_sha', 'head_ref', 'base_ref', 'url', 'title'}."""
    p = _request("GET", f"/repos/{owner}/{name}/pulls/{number}")
    return {"state": p.get("state"), "merged": bool(p.get("merged")), "merge_sha": p.get("merge_commit_sha") or "", "head_sha": p["head"]["sha"],
            "head_ref": p["head"]["ref"], "base_ref": p["base"]["ref"], "url": p["html_url"], "title": p["title"]}
