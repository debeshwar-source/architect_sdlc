"""A read-only copy of a pull request's code on this computer, so the reviewer can open files, search and run commands, as a person would.

Plain words: the PR is fetched from GitHub by its own number (`pull/<n>/head`), so this works for a branch in the repository and for a
fork alike, and it is always exactly what GitHub shows, never what the coding agent has half-edited locally. It lives in a separate
throwaway worktree (reset before every use), not in any worktree where work is done.
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Optional

from coding import workspace as ws

ROOT = ws.WORK_ROOT / "review"


class CheckoutError(Exception):
    """The code could not be fetched; the message is fit to show to the person."""


_env = ws.network_env                                    # one place decides how git is run against GitHub


def _git(cwd: Path, url: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=_env(url), timeout=600)
    if proc.returncode != 0:
        raise CheckoutError(f"git {args[0]} failed: {ws.explain(proc.stderr or proc.stdout)[:400]}")
    return proc.stdout.strip()


def prepare(owner: str, name: str, number: int, remote: Optional[str] = None) -> Path:
    """The PR's current code in a clean detached worktree; returns its folder. `remote` overrides the GitHub address (used by tests)."""
    url = remote or f"https://github.com/{owner}/{name}.git"
    repo = ROOT / "clones" / f"{name}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"
    tree = ROOT / "trees" / f"{name}-pr{number}"
    try:
        if repo.is_dir():
            _git(repo, url, "fetch", "--prune", "origin")
        else:
            repo.parent.mkdir(parents=True, exist_ok=True)
            _git(repo.parent, url, "clone", "--quiet", url, str(repo))
        _git(repo, url, "fetch", "origin", f"pull/{number}/head")
        sha = _git(repo, url, "rev-parse", "FETCH_HEAD")
        if tree.is_dir():
            _git(tree, url, "checkout", "--detach", "--force", sha)
        else:
            tree.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, url, "worktree", "add", "--detach", str(tree), sha)
        _git(tree, url, "reset", "--hard", "-q", sha)
        _git(tree, url, "clean", "-fdq")
    except (CheckoutError, subprocess.SubprocessError, OSError) as exc:
        raise CheckoutError(str(exc)) from None
    return tree


def prepare_commit(owner: str, name: str, sha: str, remote: Optional[str] = None) -> Path:
    """The repository exactly as it is at one commit (for example the merge commit of a pull request), in a clean detached worktree of
    its own. Returns its folder."""
    url = remote or f"https://github.com/{owner}/{name}.git"
    repo = ROOT / "clones" / f"{name}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"
    tree = ROOT / "trees" / f"{name}-at-{sha[:10]}"
    try:
        if repo.is_dir():
            _git(repo, url, "fetch", "--prune", "origin")
        else:
            repo.parent.mkdir(parents=True, exist_ok=True)
            _git(repo.parent, url, "clone", "--quiet", url, str(repo))
        try:
            _git(repo, url, "cat-file", "-e", f"{sha}^{{commit}}")
        except CheckoutError:
            _git(repo, url, "fetch", "origin", sha)                      # a commit no branch points at any more
        if tree.is_dir():
            _git(tree, url, "checkout", "--detach", "--force", sha)
        else:
            tree.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, url, "worktree", "add", "--detach", str(tree), sha)
        _git(tree, url, "reset", "--hard", "-q", sha)
        _git(tree, url, "clean", "-fdq")
    except (CheckoutError, subprocess.SubprocessError, OSError) as exc:
        raise CheckoutError(str(exc)) from None
    return tree


def ensure_repo(owner: str, name: str, remote: Optional[str] = None) -> Path:
    """The shared clone of a repository, fetched up to date. Returns its folder (never a place work happens: worktrees are made from it)."""
    url = remote or f"https://github.com/{owner}/{name}.git"
    repo = ROOT / "clones" / f"{name}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"
    try:
        if repo.is_dir():
            _git(repo, url, "fetch", "--prune", "origin")
        else:
            repo.parent.mkdir(parents=True, exist_ok=True)
            _git(repo.parent, url, "clone", "--quiet", url, str(repo))
    except (CheckoutError, subprocess.SubprocessError, OSError) as exc:
        raise CheckoutError(str(exc)) from None
    return repo
