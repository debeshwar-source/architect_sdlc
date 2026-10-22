"""Where a repository's knowledge base lives inside the shared library folder (e.g. knowledge_base/).

One repository -> one folder, found again on every later run:
  * the folder is named after the repository (`my-service`);
  * an existing folder is recognised by the repository address stored in its metadata, not by its name, so renaming
    the repo or running with another --ref still lands in the same folder;
  * if a different repository (or an unrelated knowledge base) already uses the name, a short hash is appended.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import List, Optional

MARKER = Path("metadata") / "source-index.json"      # present in every folder this service writes


def normalize_source(source: str) -> str:
    p = Path(source).expanduser()
    if "://" not in source and not re.match(r"^[\w.-]+@[\w.-]+:", source) and p.exists():
        return str(p.resolve())
    s = re.sub(r"(?<=://)[^/@\s]+@", "", source.strip())           # drop credentials
    s = re.sub(r"^git@([\w.-]+):", r"https://\1/", s)               # scp-style -> https
    s = re.sub(r"^ssh://git@", "https://", s)
    s = s.rstrip("/").removesuffix(".git").rstrip("/")
    scheme, sep, rest = s.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme}{sep}{host.lower()}{slash}{path}" if sep else s


def repo_name(source: str) -> str:
    last = normalize_source(source).rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9._-]+", "-", last.lower()).strip("-._") or "repository"


def is_generated_kb(path: Path) -> bool:
    return (path / MARKER).is_file()


def _stored_source(path: Path) -> Optional[str]:
    try:
        return normalize_source(json.loads((path / MARKER).read_text(encoding="utf-8"))["provenance"]["repository_url"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_generated(root: Path) -> List[Path]:
    root = Path(root)
    return sorted(p for p in root.iterdir() if p.is_dir() and is_generated_kb(p)) if root.is_dir() else []


def kb_dir_for(source: str, root: Path) -> Path:
    """Return the folder under `root` for this repository (existing one if there is one)."""
    root, ident = Path(root), normalize_source(source)
    for p in list_generated(root):
        if _stored_source(p) == ident:
            return p
    base = root / repo_name(source)
    if not base.exists():
        return base
    return root / f"{base.name}-{hashlib.sha256(ident.encode()).hexdigest()[:8]}"
