"""Saving and reopening sessions.

Plain words: while you work, the app keeps a snapshot of where you are -- the
stages that have finished, what each produced, what it is waiting for, what you
have frozen -- in the run's own folder (runs/<run id>/session.json). If the app
is closed (or crashes), you can reopen the snapshot later and carry on from the
same place. Only the snapshot is read back; nothing is re-run, so reopening costs
nothing and calls no AI.

A snapshot holds only plain data. The knowledge base's documents are not stored
in it (they are read again from the knowledge base by name), and anything that is
not plain data (functions, for example) is left out.
"""

import json
import os
from dataclasses import is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

VERSION = 1
FILE_NAME = "session.json"

# The pieces of the page's own state that make up a session (besides the pipeline state).
SESSION_KEYS = (
    "run_started", "run_active", "run_id", "requirement_text", "human_in_the_loop", "current_node",
    "awaiting_approval", "awaiting_jira", "awaiting_kb", "awaiting_confluence", "finished", "error",
    "session_name",
)
# ... and the families of keys: the freeze markers and the discussion chats.
SESSION_KEY_SUFFIXES = ("_frozen_at",)
SESSION_KEY_PREFIXES = ("chat_history_",)

# Pipeline-state entries that are not saved (re-created on reopening).
_NOT_SAVED = {"applications"}


def _plain(value: Any) -> Any:
    """`value` as plain JSON-able data: tuples and sets become lists, paths become text, and
    anything else odd becomes its text. Functions are dropped by the callers."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items() if not callable(v)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value if not callable(v)]
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return str(value)
    return str(value)


def path_for(run_id: str) -> Path:
    return config.RUNS_ROOT / run_id / FILE_NAME


def is_session_key(key: str) -> bool:
    return (key in SESSION_KEYS or key.endswith(SESSION_KEY_SUFFIXES) or key.startswith(SESSION_KEY_PREFIXES))


def snapshot(session: Dict[str, Any]) -> Dict[str, Any]:
    """The snapshot of a page's state (a dict-like of st.session_state)."""
    state = session.get("pipeline_state") or {}
    return {
        "version": VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": {k: _plain(session[k]) for k in list(session.keys()) if isinstance(k, str) and is_session_key(k)},
        "completed_events": [[node, _plain(result)] for node, result in session.get("completed_events") or []],
        "pipeline_state": {k: _plain(v) for k, v in state.items() if k not in _NOT_SAVED and not callable(v)},
    }


def save(session: Dict[str, Any]) -> Optional[Path]:
    """Write the current session to disk. Returns the file, or None when there is nothing to save
    (no run yet) or the disk refused (a failed save must never break the page)."""
    run_id = session.get("run_id")
    if not session.get("run_started") or not run_id:
        return None
    path = path_for(run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot(session), indent=1), encoding="utf-8")
        os.replace(tmp, path)               # all or nothing: a crash mid-write cannot leave half a file
        return path
    except (OSError, TypeError, ValueError):
        return None


def read(run_id: str) -> Optional[Dict[str, Any]]:
    """The saved snapshot for a run, or None if there is none or it is unreadable."""
    try:
        data = json.loads(path_for(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("session"), dict):
        return None
    return data


def restore(run_id: str, load_applications) -> Optional[Dict[str, Any]]:
    """Everything needed to put a saved session back on the page, or None if it cannot be read:

        {"session": {...page state...}, "completed_events": [(node, result), ...], "pipeline_state": {...}}

    `load_applications(kb_name)` supplies the knowledge base's documents again."""
    data = read(run_id)
    if data is None:
        return None
    state = dict(data.get("pipeline_state") or {})
    kb_name = state.get("kb_name") or ""
    try:
        state["applications"] = load_applications(kb_name) if kb_name else []
    except Exception:  # noqa: BLE001 -- a knowledge base that has since gone is not fatal: carry on without it
        state["applications"] = []
    events = [(item[0], item[1]) for item in data.get("completed_events") or [] if isinstance(item, list) and len(item) == 2]
    return {"session": dict(data["session"]), "completed_events": events, "pipeline_state": state}


def delete(run_id: str) -> bool:
    """Forget a saved session: removes only its snapshot (session.json), never the run's other
    files (documents, events). True if a snapshot was removed."""
    if not run_id or run_id != Path(run_id).name:      # a bare folder name only: no paths, no ".."
        return False
    try:
        path_for(run_id).unlink()
        return True
    except OSError:
        return False


NAME_MAX = 90


def clean_name(name: str) -> str:
    """A session name as it will be kept: one line, extra spaces gone, at most NAME_MAX characters."""
    return " ".join(str(name or "").split())[:NAME_MAX]


def name_taken(name: str, run_id: str = "") -> bool:
    """Does another saved session (not `run_id`) already carry this name? (Case does not matter.)"""
    wanted = clean_name(name).lower()
    return bool(wanted) and any((s["title"].lower() == wanted) for s in list_sessions(limit=10_000) if s["run_id"] != run_id and s["named"])


def list_sessions(limit: int = 30) -> List[Dict[str, Any]]:
    """The saved sessions, most recently saved first. Unreadable ones are skipped."""
    found = []
    root = config.RUNS_ROOT
    if not root.is_dir():
        return found
    for folder in root.iterdir():
        data = read(folder.name) if (folder / FILE_NAME).is_file() else None
        if data is None:
            continue
        session, state = data["session"], data.get("pipeline_state") or {}
        title = ((state.get("requirement") or {}).get("title")
                 or str(session.get("requirement_text") or "").strip().splitlines()[0:1] or [""])
        title = title if isinstance(title, str) else title[0]
        custom = clean_name(session.get("session_name") or "")
        found.append({
            "run_id": folder.name, "title": custom or title[:90] or "(untitled)", "named": bool(custom),
            "original": title[:90] if custom else "", "saved_at": data.get("saved_at", ""),
            "current_node": session.get("current_node"), "finished": bool(session.get("finished")),
            "error": session.get("error"),
            "waiting": [name for name in ("approval", "jira", "kb", "confluence") if session.get(f"awaiting_{name}")],
            "stages": len(data.get("completed_events") or []),
        })
    found.sort(key=lambda s: s["saved_at"], reverse=True)
    return found[:limit]
