"""Append-only event log: runs/<run_id>/events.jsonl."""

import json
import threading
from datetime import datetime, timezone
from typing import List

import config

_lock = threading.Lock()


def _path(run_id: str):
    return config.RUNS_ROOT / run_id / "events.jsonl"


def emit(run_id: str, kind: str, stage: str = None, **data) -> dict:
    """Appends one event and returns it. Never raises -- observability
    must not be able to fail a run."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "stage": stage,
        **data,
    }
    try:
        with _lock:
            path = _path(run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
    except OSError:
        pass
    return event


def read(run_id: str) -> List[dict]:
    path = _path(run_id)
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
