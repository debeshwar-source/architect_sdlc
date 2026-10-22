"""What the agents are doing, as a plain running log.

Plain words: every agent (the pipeline agents, the coding agent, the reviewer, QA) writes one short line for each thing it does: a file it
reads, a command it runs and what came back, a thought it voices, when it starts and when it finishes or fails. The lines go to a file
(runs/activity.jsonl) and to a short in-memory list, and the "Activity" card on the page shows them like a terminal. Prompts, secrets and
environment variables are never logged, and every line is cut short. Logging is best effort: it must never break or slow an agent.
"""

import contextvars
import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, List, Optional

import config

LINE_CHARS = 220
KEEP = 600                                 # lines kept in memory (and re-read from the file after a restart)
FILE_MAX_BYTES = 2_000_000                 # the file is cut back to its newest lines beyond this
FILE_KEEP = 1000
THROTTLE = 0.25                            # seconds between redraws of the live terminal

_lock = threading.Lock()
_mem: Deque[Dict[str, str]] = deque(maxlen=KEEP)
_loaded = False
_agent: contextvars.ContextVar = contextvars.ContextVar("activity_agent", default="app")
_live: Dict[str, Any] = {"render": None, "last": 0.0}

PREFIX = {"tool": "› ", "say": "· ", "out": "  ⎿ ", "ok": "✔ ", "err": "✖ ", "info": ""}


def _path():
    return config.RUNS_ROOT / "activity.jsonl"


def _clip(text: Any) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= LINE_CHARS else one[:LINE_CHARS - 1] + "…"


@contextmanager
def agent_context(name: str):
    """Everything logged inside this block (in this thread) is labelled with this agent's name."""
    token = _agent.set(name)
    try:
        yield
    finally:
        _agent.reset(token)


def current() -> str:
    return _agent.get()


def _load_once() -> None:
    """After a restart, bring the newest lines of the file back so the terminal is not empty."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()[-KEEP:]
    except OSError:
        return
    for line in lines:
        try:
            e = json.loads(line)
            if isinstance(e, dict) and {"ts", "agent", "kind", "text"} <= set(e):
                _mem.append(e)
        except ValueError:
            continue


def _append_file(entry: Dict[str, str]) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if path.stat().st_size > FILE_MAX_BYTES:
            keep = path.read_text(encoding="utf-8").splitlines()[-FILE_KEEP:]
            path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 -- logging must never affect an agent
        pass


def log(text: Any, kind: str = "info", agent: Optional[str] = None) -> None:
    """Add one line. kind: tool | say | out | ok | err | info."""
    try:
        entry = {"ts": time.strftime("%H:%M:%S"), "agent": agent or current(), "kind": kind if kind in PREFIX else "info", "text": _clip(text)}
        with _lock:
            _load_once()
            _mem.append(entry)
            _append_file(entry)
        _notify()
    except Exception:  # noqa: BLE001 -- logging must never affect an agent
        pass


def from_event(event: str, agent: str, fields: Dict[str, Any]) -> None:
    """The token ledger's start / attempt / finish records, as lines (this is how the pipeline agents report). The coding-style agents are
    reported step by step by their own runner, so their ledger records are not repeated here."""
    if agent.startswith("Coding Agent"):
        return
    if event == "llm_start":
        log(f"started (prompt about {fields.get('prompt_tokens', '?')} tokens)", "info", agent)
    elif event == "llm_attempt":
        n = fields.get("attempt", "?")
        log(f"attempt {n} ok" if fields.get("ok") else f"attempt {n} failed: {str(fields.get('error') or '')[:140]}", "ok" if fields.get("ok") else "err", agent)
    elif event == "llm_call":
        log("done" if fields.get("ok") else f"failed after {fields.get('attempts', '?')} attempt(s)" + (f": {str(fields['error'])[:140]}" if fields.get("error") else ""),
            "ok" if fields.get("ok") else "err", agent)


def recent(n: int = KEEP, agent: Optional[str] = None) -> List[Dict[str, str]]:
    with _lock:
        _load_once()
        items = [e for e in _mem if agent is None or e["agent"] == agent]
    return items[-n:]


def agents() -> List[str]:
    return sorted({e["agent"] for e in recent()})


def format_line(e: Dict[str, str]) -> str:
    return f"{e['ts']}  {e['agent'][:24]:<24}  {PREFIX.get(e['kind'], '')}{e['text']}"


def clear() -> None:
    with _lock:
        _mem.clear()
        try:
            _path().unlink(missing_ok=True)
        except OSError:
            pass


# ---------- the live terminal ----------
# The page registers one redraw function; lines logged by code running in the page's own thread redraw it at once (the page cannot refresh
# itself while it is busy, so the running code does it). Lines from a worker thread are drawn by the page's own waiting loop (refresh_live).

def attach(render: Callable[[], None]) -> None:
    _live["render"] = render


def detach() -> None:
    _live["render"] = None


def _in_page_thread() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx(suppress_warning=True) is not None
    except Exception:  # noqa: BLE001 -- no Streamlit, or no page
        return False


def refresh_live(force: bool = False) -> None:
    """Redraw the live terminal now (throttled). Only does anything in the page's own thread."""
    render = _live["render"]
    if render is None or (not force and time.monotonic() - _live["last"] < THROTTLE) or not _in_page_thread():
        return
    _live["last"] = time.monotonic()
    try:
        render()
    except Exception:  # noqa: BLE001 -- a stale or closed box must not stop an agent
        _live["render"] = None


def _notify() -> None:
    refresh_live()
