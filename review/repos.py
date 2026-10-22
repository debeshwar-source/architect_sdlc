"""Which GitHub repositories to look for pending pull requests in, without the person having to type one.

Plain words: the places this app already knows about: the repositories the coding agent works on (every coding job remembers its
repository and the pull request it opened), repositories reviewed before, one set in REVIEW_GITHUB_REPO, one the person added by hand,
and this project's own `origin`. Newest first, no duplicates.
"""

import json
import re
from pathlib import Path
from typing import List, Optional

from review import github, loop

_PR_URL = re.compile(r"https://github\.com/([^/]+)/([^/]+)/(?:pull|compare)/")


def _slug(text: str) -> Optional[str]:
    found = github.parse_repo(text) if text and ("github.com" in text or re.fullmatch(r"[\w.-]+/[\w.-]+", text.strip())) else None
    return f"{found[0]}/{found[1]}" if found else None


def _file() -> Path:
    return loop.STORE / "repos.json"


def remembered() -> List[str]:
    try:
        return [r for r in json.loads(_file().read_text(encoding="utf-8")) if isinstance(r, str)]
    except (OSError, ValueError):
        return []


def remember(repo: str) -> None:
    """Keep a repository the person typed, so it is listed from now on."""
    slug = _slug(repo)
    if not slug or slug.lower() in {r.lower() for r in remembered()}:
        return
    _file().parent.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps(remembered() + [slug]), encoding="utf-8")


def known_repos() -> List[str]:
    """'owner/name' for every repository worth checking: those of the newest coding jobs first, then the rest."""
    found: List[str] = []

    def add(text: Optional[str]) -> None:
        slug = _slug(text or "")
        if slug and slug.lower() not in {f.lower() for f in found}:
            found.append(slug)

    try:
        from coding import flow
        for job in flow.list_jobs():                    # newest first
            add(((job.d.get("pr") or {}).get("url") or "") if _PR_URL.match((job.d.get("pr") or {}).get("url") or "") else "")
            add((job.d.get("ws") or {}).get("source"))
    except Exception:  # noqa: BLE001 -- a broken job record must not hide the other repositories
        pass
    if loop.STORE.is_dir():
        for path in sorted(loop.STORE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name != "repos.json":
                try:
                    add("/".join(json.loads(path.read_text(encoding="utf-8"))["repo"]))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
    for r in remembered():
        add(r)
    add(github.default_repo())
    return found
