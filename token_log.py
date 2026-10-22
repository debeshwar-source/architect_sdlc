"""Token ledger: one JSON line per event in runs/token_log.jsonl.

Plain words: records how big each prompt was and which parts of it were sent,
so you can see where tokens go and whether the trimming (skill routing,
knowledge-base slicing) is helping. Logging is best effort - it must never
break or slow a run, so every failure here is swallowed.
"""

import json
from datetime import datetime, timezone
from typing import Any

import config


def est_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token); good enough for comparing."""
    return (len(text or "") + 3) // 4


def record(event: str, agent: str, **fields: Any) -> None:
    try:
        path = config.RUNS_ROOT / "token_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "event": event, "agent": agent, **fields}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001 -- logging must never affect a run
        pass
    try:
        import activity
        activity.from_event(event, agent, fields)
    except Exception:  # noqa: BLE001 -- logging must never affect a run
        pass
