"""What Engineering Manager 2 remembers about a repository, for the next job.

Plain words: when you freeze a job's subtasks, the durable things the manager learned (build and test commands,
conventions, where an example of each kind of thing lives, gotchas) are written to a small file for that repository
(runs/coding/memory/<repository>.json). The next job on the same repository starts from them, so it explores less.
Only approved work is remembered, never a plan you threw away.

Each fact records the commit it was learned at and the files it rests on. When the next job starts, a fact whose files have
changed since then is marked CHECK, so the manager re-verifies it instead of trusting it. You can read and clear the memory.
"""

import difflib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from coding import workspace as ws

MEMORY_ROOT = ws.WORK_ROOT / "memory"
MAX_FACTS = 80                      # the oldest are dropped beyond this
SAME = 0.9                          # facts this alike, about the same files, are one fact (refreshed, not repeated)


def key_for(workspace: ws.Workspace) -> str:
    """One memory per repository: the clone's folder name for a Git address, the folder's name and path for a local one."""
    if ws.is_url(workspace.source):
        return workspace.repo.name
    return f"{ws.slug(workspace.repo.name, 40)}-{hashlib.sha1(str(workspace.repo).encode()).hexdigest()[:8]}"


def _path(key: str) -> Path:
    return MEMORY_ROOT / f"{key}.json"


def load(key: str) -> Dict[str, Any]:
    try:
        return json.loads(_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"repo": "", "facts": []}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def remember(key: str, source: str, commit: str, learnings: List[Dict[str, Any]]) -> int:
    """Add the learnings (a new fact, or a refreshed one when it says the same thing). Returns how many were new."""
    data = load(key)
    data["repo"] = source
    facts: List[Dict[str, Any]] = data["facts"]
    new = 0
    for item in learnings:
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        entry = {"text": text, "files": list(item.get("files") or []), "commit": commit, "learned": time.strftime("%Y-%m-%d")}
        same = next((f for f in facts if _norm(f["text"]) == _norm(text) or (
            set(f["files"]) == set(entry["files"]) and difflib.SequenceMatcher(None, _norm(f["text"]), _norm(text)).ratio() >= SAME)), None)
        if same:
            same.update(entry)
        else:
            facts.append(entry)
            new += 1
    data["facts"] = facts[-MAX_FACTS:]
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    _path(key).write_text(json.dumps(data, indent=1), encoding="utf-8")
    return new


def forget(key: str, index: Optional[int] = None) -> None:
    """Forget one fact, or (no index) everything about the repository."""
    if index is None:
        _path(key).unlink(missing_ok=True)
        return
    data = load(key)
    if 0 <= index < len(data["facts"]):
        del data["facts"][index]
        _path(key).write_text(json.dumps(data, indent=1), encoding="utf-8")


def is_stale(workspace: ws.Workspace, fact: Dict[str, Any]) -> bool:
    """Have the files this fact rests on changed since it was learned? (Unknown commit counts as changed; a fact with no
    files cannot be checked, so it is only as old as its date.)"""
    files = fact.get("files") or []
    if not files or not fact.get("commit"):
        return False
    try:
        return bool(ws.git(workspace.repo, "diff", "--name-only", fact["commit"], workspace.base_commit, "--", *files))
    except ws.GitError:
        return True


def facts(key: str, workspace: ws.Workspace) -> List[Dict[str, Any]]:
    """The facts, each with `stale` worked out against the repository as it is now."""
    return [{**f, "stale": is_stale(workspace, f)} for f in load(key)["facts"]]


def for_prompt(key: str, workspace: ws.Workspace) -> str:
    """The memory as the manager reads it. Empty when there is none."""
    lines = []
    for f in facts(key, workspace):
        where = f" (see {', '.join(f['files'][:4])})" if f["files"] else ""
        flag = "CHECK - those files changed since" if f["stale"] else "fresh"
        lines.append(f"- [{flag}, learned {f['learned']}] {f['text']}{where}")
    return "\n".join(lines)
