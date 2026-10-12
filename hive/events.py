"""Append-only event log: runs/<run_id>/events.jsonl.

Plain words: a diary of the run. Every time something happens ("stage X
started", "stage X finished", "a human left a note") one line is added to
the end of a file. Lines are only ever added, never edited or removed, so
the file is a trustworthy timeline. ("JSONL" = one small JSON record per
line.)
"""

import json
import threading
from datetime import datetime, timezone
from typing import List

import config

# A lock makes sure two parts of the program never write to the file at
# the exact same moment (which could garble the lines).
_lock = threading.Lock()


def _path(run_id: str):
    """Where this run's diary file lives."""
    return config.RUNS_ROOT / run_id / "events.jsonl"


def emit(run_id: str, kind: str, stage: str = None, **data) -> dict:
    """Appends one event and returns it. Never raises -- observability
    must not be able to fail a run."""
    # One diary entry: when it happened, what kind of event, which stage,
    # plus any extra details passed in (**data).
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
            # "a" = append mode: add to the end rather than overwrite.
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
    except OSError:
        # If the disk write fails, quietly carry on: a logging problem
        # should never stop the real work.
        pass
    return event


def read(run_id: str) -> List[dict]:
    """Read the whole diary back as a list of events (oldest first)."""
    path = _path(run_id)
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip any damaged line instead of failing.
            continue
    return events
