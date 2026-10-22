"""Repository acquisition: clone or update into an isolated work directory, pin to a ref, record provenance.

Safety: git hooks and transport helpers are disabled, submodules are not fetched, nothing from the repository
is executed, and credentials travel only via an environment-supplied HTTP header (never in the URL or on disk).
"""

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import security

ANALYSIS_VERSION = "2"
SAFE_CONFIG = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
               "-c", "protocol.fd.allow=never", "-c", "core.fsmonitor=false",
               "-c", "submodule.recurse=false", "-c", "advice.detachedHead=false"]


class IngestError(RuntimeError):
    pass


@dataclass
class RepoSnapshot:
    path: Path
    url: str
    ref: Optional[str]
    branch: Optional[str]
    commit: str
    ingested_at: str
    analysis_version: str = ANALYSIS_VERSION
    ephemeral: bool = False
    warnings: List[str] = field(default_factory=list)

    def provenance(self) -> dict:
        return {"repository_url": _strip_credentials(self.url), "ref": self.ref, "branch": self.branch,
                "commit": self.commit, "ingested_at": self.ingested_at, "analysis_version": self.analysis_version}


def _strip_credentials(url: str) -> str:
    return re.sub(r"(?<=://)[^/@\s]+@", "", url)


def _env(token: Optional[str]) -> dict:
    # Use the user's own git setup (saved logins, SSH keys) exactly as a plain `git clone` would. Only prompting is disabled,
    # so a run can fail with a message but never hang waiting for input.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for var in ("GIT_ASKPASS", "SSH_ASKPASS"):     # never let an inherited helper pop up a prompt; GIT_TERMINAL_PROMPT=0 does the rest
        env.pop(var, None)
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return env


def git(args: List[str], cwd: Optional[Path] = None, token: Optional[str] = None, timeout: int = 600) -> str:
    cmd = ["git", *SAFE_CONFIG, *args]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=_env(token), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise IngestError(f"git {args[0]} timed out after {timeout}s") from e
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if token:
            msg = msg.replace(token, "[REDACTED]")
        hint = ""
        if re.search(r"could not read (Username|Password)|Authentication failed|terminal prompts disabled", msg):
            hint = (" -> git has no saved login for this repository, or the URL is wrong (GitHub reports a missing repository the "
                    "same way). Check that a plain `git clone <url>` works in your terminal; whatever login it uses is used here.")
        elif re.search(r"Write access to repository not granted|error: 403|SAML|SSO", msg):
            hint = " -> GitHub refused the credentials git used. Check that a plain `git clone <url>` works in your terminal."
        elif "Repository not found" in msg or "does not appear to be a git repository" in msg:
            hint = " -> repository not found: check the URL."
        raise IngestError(f"git {args[0]} failed: {_strip_credentials(msg)[:500]}{hint}")
    return r.stdout


def _resolve(work: Path, ref: Optional[str], token) -> tuple:
    """Return (revision to check out, branch name or None)."""
    if not ref:
        head = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], work, token).strip() \
            if _ok(["rev-parse", "--verify", "-q", "refs/remotes/origin/HEAD"], work) else "HEAD"
        return head, head.replace("origin/", "") if head != "HEAD" else None
    for cand in (f"refs/remotes/origin/{ref}", f"refs/tags/{ref}", ref):
        if _ok(["rev-parse", "--verify", "-q", f"{cand}^{{commit}}"], work):
            return cand, ref if cand.startswith("refs/remotes") else None
    raise IngestError(f"ref {ref!r} not found (branch, tag or commit)")


def _ok(args, cwd) -> bool:
    try:
        git(args, cwd)
        return True
    except IngestError:
        return False


def acquire(source: str, workdir: Path, ref: Optional[str] = None, token_env: Optional[str] = None) -> RepoSnapshot:
    """Clone `source` into `workdir` (or fetch if it is already a clone of it) and check out `ref`.

    `source` is a Git URL or a local path (a local path is cloned, never analysed in place, so the user's
    working tree and uncommitted files are untouched). `token_env` names an environment variable holding a token.
    """
    if (err := security.validate_source(source)) or (ref and (err := security.validate_ref(ref))):
        raise IngestError(err)
    token = os.environ.get(token_env) if token_env else None
    if token_env and not token:
        raise IngestError(f"environment variable {token_env} is not set")
    if Path(source).expanduser().exists():
        source = str(Path(source).expanduser().resolve())
    workdir = Path(workdir)
    if (workdir / ".git").exists():
        origin = git(["config", "--get", "remote.origin.url"], workdir).strip()
        if origin != source:
            raise IngestError(f"{workdir} is a clone of a different repository ({_strip_credentials(origin)})")
        git(["fetch", "--tags", "--prune", "--force", "origin", "+refs/heads/*:refs/remotes/origin/*"], workdir, token)
    else:
        workdir.parent.mkdir(parents=True, exist_ok=True)
        tmp = workdir.with_name(workdir.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)  # a previous interrupted clone: safe to retry
        try:
            git(["clone", "--no-checkout", "--no-recurse-submodules", "--", source, str(tmp)], token=token)
        except IngestError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        tmp.rename(workdir)
    rev, branch = _resolve(workdir, ref, token)
    git(["checkout", "--force", "--detach", rev, "--"], workdir)
    commit = git(["rev-parse", "HEAD"], workdir).strip()
    return RepoSnapshot(path=workdir, url=source, ref=ref, branch=branch, commit=commit,
                        ingested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
