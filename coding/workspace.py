"""Git side of the coding agent: get the repository, give the agent its own branch, make the pull request.

Plain words: the agent never touches your working copy. For a local repository it works in a separate
`git worktree` on a new branch; for a Git address it works in a clone. Nothing leaves the computer until
you press "Create pull request", which commits, pushes the branch and opens the PR.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import config

WORK_ROOT = config.RUNS_ROOT / "coding"


class GitError(Exception):
    pass


NETWORK = {"fetch", "clone", "push", "pull", "ls-remote"}          # the git commands that talk to a remote
NEEDS_CREDENTIALS = ("terminal prompts disabled", "could not read Username", "could not read Password", "Authentication failed")
CREDENTIALS_HELP = ("GitHub wants a login and none is available here. Set GITHUB_TOKEN in the .env file to a token that can access this "
                    "repository (classic: the `repo` scope; fine-grained: Contents read and write), then try again.")


def network_env(url: str = "") -> dict:
    """The environment for a git command that talks to a remote: never ask for a password on the terminal (this app runs in a window,
    so a prompt there looks like a freeze), and for github.com use the GitHub token so a private repository works with just the token.
    The token goes only into this one process's environment, never into a git config file or a command line."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    token = _token()
    if token and url.startswith("https://github.com/"):
        import base64
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader", GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return env


def _remote_of(cwd: Path, args: tuple) -> str:
    """The address a network command will talk to: the one in its arguments (a clone), else this repository's `origin`."""
    given = next((a for a in args if a.startswith(("http://", "https://", "git@", "ssh://"))), "")
    if given:
        return given
    proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def explain(stderr: str) -> str:
    """The git error in words a person can act on."""
    return CREDENTIALS_HELP if any(m.lower() in stderr.lower() for m in NEEDS_CREDENTIALS) else stderr.strip()


def git(cwd: Path, *args: str, check: bool = True) -> str:
    env = network_env(_remote_of(cwd, args)) if args and args[0] in NETWORK else None
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {explain(proc.stderr or proc.stdout)}")
    return proc.stdout.strip()


def is_url(source: str) -> bool:
    return bool(re.match(r"^(https?://|git@|ssh://|git://)", source.strip())) or source.strip().endswith(".git")


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "task"


def branch_name(wanted: str, title: str) -> str:
    """A safe branch name: the manager's suggestion if it is usable, else made from the title."""
    name = re.sub(r"[^A-Za-z0-9._/-]+", "-", (wanted or "").strip()).strip("-/.")
    name = re.sub(r"\.\.+|//+", "-", name)
    return name if name and not name.endswith(".lock") else f"feature/{slug(title)}"


@dataclass
class Workspace:
    repo: Path              # the repository (the user's folder, or our clone)
    tree: Path              # the worktree the agent edits
    branch: str
    base: str               # branch the PR targets
    base_commit: str
    source: str
    warning: str = ""

    def to_dict(self) -> dict:
        return {"repo": str(self.repo), "tree": str(self.tree), "branch": self.branch, "base": self.base,
                "base_commit": self.base_commit, "source": self.source, "warning": self.warning}

    @classmethod
    def from_dict(cls, d: dict) -> "Workspace":
        return cls(Path(d["repo"]), Path(d["tree"]), d["branch"], d["base"], d["base_commit"], d["source"], d.get("warning", ""))


def _unique_branch(repo: Path, name: str) -> str:
    taken = set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def resolve_branch(repo: Path, wanted: str, remote: bool) -> str:
    """The real name of the branch the user meant. Typed text is often auto-capitalised ("dev" -> "Dev"), so an exact
    match wins, then a match that ignores case; anything else is an error that lists the branches there are."""
    prefix = "refs/remotes/origin/" if remote else "refs/heads/"
    names = [n[len(prefix):] for n in git(repo, "for-each-ref", "--format=%(refname)", prefix).split() if n != prefix + "HEAD"]
    if wanted in names:
        return wanted
    loose = [n for n in names if n.lower() == wanted.lower()]
    if len(loose) == 1:
        return loose[0]
    raise GitError(f"There is no branch '{wanted}'. Branches: {', '.join(sorted(names)[:15])}{' …' if len(names) > 15 else ''}")


def prepare(source: str, task_key: str, wanted_branch: str, title: str, base: Optional[str] = None) -> Workspace:
    """Open the repository and create the agent's worktree on a fresh branch off `base`."""
    source = source.strip()
    warning = ""
    if is_url(source):
        digest = hashlib.sha1(source.encode()).hexdigest()[:8]
        repo = WORK_ROOT / "clones" / f"{slug(re.sub(r'[.]git$', '', source.rstrip('/')).split('/')[-1], 40)}-{digest}"
        if repo.is_dir() and git(repo, "rev-parse", "--git-dir", check=False) == "":
            shutil.rmtree(repo)                      # left over from a clone that failed half-way
        if repo.is_dir():
            git(repo, "fetch", "--prune", "origin")
        else:
            repo.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(["git", "clone", source, str(repo)], capture_output=True, text=True, env=network_env(source))
            if proc.returncode != 0:
                raise GitError(f"Could not clone {source}: {explain(proc.stderr)}")
        base = resolve_branch(repo, base.strip(), True) if base and base.strip() else \
            git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).replace("origin/", "") or "main"
        start = f"origin/{base}"
    else:
        repo = Path(source).expanduser().resolve()
        if not repo.is_dir() or git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
            raise GitError(f"{repo} is not a Git repository.")
        repo = Path(git(repo, "rev-parse", "--show-toplevel"))
        base = resolve_branch(repo, base.strip(), False) if base and base.strip() else git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        start = base
        if git(repo, "status", "--porcelain"):
            warning = "Your working copy has uncommitted changes; the agent starts from the last commit and does not see them."
    base_commit = git(repo, "rev-parse", start)
    branch = _unique_branch(repo, branch_name(wanted_branch, title))
    tree = WORK_ROOT / "trees" / f"{repo.name}-{slug(task_key, 60)}-{slug(branch, 30)}"
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(tree), start)
    return Workspace(repo, tree, branch, base, base_commit, source, warning)


# Files that running the project's tests leaves behind; never part of a pull request.
JUNK = [f":(exclude,glob)**/{g}" for g in ("__pycache__/**", "*.pyc", ".pytest_cache/**", "node_modules/**", ".DS_Store", "*.class")]


def changed_files(ws: Workspace, since: Optional[str] = None) -> List[str]:
    """Files that differ from `since` (default: the base), new files included. Still true after a commit."""
    git(ws.tree, "add", "-N", "--", ".", *JUNK, check=False)
    return [f for f in git(ws.tree, "diff", "--name-only", since or ws.base_commit, "--", ".", *JUNK).splitlines() if f.strip()]


def diff(ws: Workspace, since: Optional[str] = None, context: int = 4) -> str:
    """Everything that changed against `since` (default: the base), new files included."""
    git(ws.tree, "add", "-N", "--", ".", *JUNK, check=False)
    return git(ws.tree, "diff", f"-U{context}", since or ws.base_commit, "--", ".", *JUNK)


def commit_diff(ws: Workspace, sha: str, context: int = 4) -> str:
    """What one commit changed (not what came after it)."""
    return git(ws.tree, "show", "--format=", f"-U{context}", sha, "--", ".", *JUNK)


def commit_files(ws: Workspace, sha: str) -> List[str]:
    """The files one commit touched."""
    return [f for f in git(ws.tree, "show", "--format=", "--name-only", sha, "--", ".", *JUNK).splitlines() if f.strip()]


def head(ws: Workspace) -> str:
    return git(ws.tree, "rev-parse", "HEAD")


def reset_to(ws: Workspace, sha: str) -> None:
    """Throw away everything after `sha`, edits and new files included. Only ever run inside the agent's own worktree."""
    git(ws.tree, "reset", "--hard", "-q", sha)
    git(ws.tree, "clean", "-fdq")


def commits(ws: Workspace) -> List[dict]:
    """The commits on the agent's branch since the base, oldest first."""
    out = git(ws.tree, "log", "--reverse", "--format=%H%x1f%s", f"{ws.base_commit}..HEAD")
    return [{"sha": sha, "subject": subj} for sha, _, subj in (line.partition("\x1f") for line in out.splitlines())]


def squash(ws: Workspace, message: str) -> str:
    """Turn every commit since the base into one."""
    git(ws.tree, "reset", "--soft", ws.base_commit)
    return commit(ws, message)


def commit(ws: Workspace, message: str) -> str:
    git(ws.tree, "add", "-A", "--", ".", *JUNK)
    if not git(ws.tree, "diff", "--cached", "--name-only"):
        raise GitError("The agent made no changes, so there is nothing to commit.")
    git(ws.tree, "commit", "-q", "-m", message)
    return git(ws.tree, "rev-parse", "HEAD")


def github_repo(remote_url: str) -> Optional[Tuple[str, str]]:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url.strip())
    return (m.group(1), m.group(2)) if m else None


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def push(ws: Workspace, force: bool = False) -> str:
    """Push the agent's branch to origin. Forced only when `force` (the user confirmed that the remote branch is to be replaced), and then
    with a lease: refused if someone else has added commits to it since we last saw it. Returns the remote's address."""
    git(ws.tree, "push", "-u", *(["--force-with-lease"] if force else []), "origin", ws.branch)
    return git(ws.tree, "remote", "get-url", "origin")


def backup_branch(ws: Workspace, label: str) -> str:
    """Keep the current tip of the agent's branch under another name (local only), so a rewrite can always be undone."""
    name = f"backup/{ws.branch}-{label}"
    git(ws.tree, "branch", "-f", name, "HEAD")
    return name


def create_pr(ws: Workspace, title: str, body: str) -> dict:
    """Open the PR for a branch that is already pushed. Returns {'url', 'opened', 'note'}: opened is False when no token is
    set, in which case the URL is GitHub's 'compare' page where the PR can be opened with one click."""
    remote = git(ws.tree, "remote", "get-url", "origin")
    gh = github_repo(remote)
    if not gh:
        return {"url": "", "opened": False, "note": f"Branch '{ws.branch}' is on {remote}; open the pull request in your Git host."}
    owner, name = gh
    compare = f"https://github.com/{owner}/{name}/compare/{ws.base}...{ws.branch}?expand=1"
    if not _token():
        return {"url": compare, "opened": False, "note": "Set GITHUB_TOKEN to open the PR automatically; open it here:"}
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{name}/pulls", method="POST",
        data=json.dumps({"title": title, "head": ws.branch, "base": ws.base, "body": body}).encode(),
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"url": json.load(resp)["html_url"], "opened": True, "note": ""}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        return {"url": compare, "opened": False, "note": f"GitHub refused the PR ({exc.code}): {detail}"}
    except OSError as exc:
        return {"url": compare, "opened": False, "note": f"The PR could not be opened ({exc})."}


def delete_remote_branch(ws: Workspace) -> None:
    """Remove the agent's branch from origin (the undo of a push). Never touches any other branch."""
    git(ws.tree, "push", "origin", "--delete", ws.branch)


def close_pr(pr_url: str) -> None:
    """Close (not merge, not delete) a GitHub pull request: the undo of opening it. GitHub lets it be reopened. Raises GitError if it cannot."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url or "")
    if not m:
        raise GitError("This pull request is not on GitHub, so it has to be closed there.")
    if not _token():
        raise GitError("Set GITHUB_TOKEN so I can close the pull request for you, or close it on GitHub.")
    req = urllib.request.Request(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/pulls/{m.group(3)}", method="PATCH",
                                 data=json.dumps({"state": "closed"}).encode(),
                                 headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except urllib.error.HTTPError as exc:
        raise GitError(f"GitHub refused to close it ({exc.code}): {exc.read().decode(errors='replace')[:200]}") from None
    except OSError as exc:
        raise GitError(f"Could not reach GitHub ({exc}).") from None


def revert_since(ws: Workspace, sha: str, message: str) -> str:
    """Undo everything committed after `sha` with a NEW commit (history is kept, so it can be pushed normally). Returns the new commit, or
    '' when nothing was committed after `sha`. A conflict aborts and leaves the branch as it was."""
    if git(ws.tree, "rev-list", "--count", f"{sha}..HEAD") == "0":
        return ""
    proc = subprocess.run(["git", "revert", "--no-commit", f"{sha}..HEAD"], cwd=ws.tree, capture_output=True, text=True)
    if proc.returncode != 0:
        git(ws.tree, "revert", "--abort", check=False)
        raise GitError(f"The change cannot be reverted cleanly (later work depends on it): {(proc.stderr or proc.stdout).strip()[:200]}")
    return commit(ws, message)


def open_pull_request(ws: Workspace, title: str, body: str) -> dict:
    """Push the branch and open the PR in one go."""
    remote = push(ws)
    result = create_pr(ws, title, body)
    if not result["opened"] and result["note"]:
        result["note"] = f"Branch '{ws.branch}' was pushed to {remote}. " + result["note"]
    return result


def pr_state(pr_url: str) -> Optional[str]:
    """'merged', 'open' or 'closed' for a GitHub PR url; None if it cannot be told (no token, no network, not GitHub)."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url or "")
    if not m or not _token():
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/pulls/{m.group(3)}",
                                 headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return "merged" if data.get("merged") else data.get("state")


def pr_body(task, result: dict, steps: Optional[list] = None) -> str:
    """The PR description. No tool attribution lines, by the user's standing preference."""
    out = (task.assignment.get("output") or {})
    lines = []
    if task.jira_key:
        lines.append(f"Jira: {task.jira_key}")
    lines += ["## What", result.get("summary") or task.title, "", "## Task", f"{task.story_id}/T{task.index}: {task.title}"]
    if steps:
        lines += ["", "## Steps"] + [f"{s['n']}. {s['title']}" for s in steps]
    if out.get("done_checks"):
        lines += ["", "## Done when"] + [f"- {c}" for c in out["done_checks"]]
    if result.get("tests_md"):
        lines += ["", "## Unit tests", result["tests_md"]]
    if result.get("assumptions"):
        lines += ["", "## Assumptions"] + [f"- {a}" for a in result["assumptions"]]
    return "\n".join(lines)
