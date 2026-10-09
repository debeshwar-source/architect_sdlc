"""Shared blackboard: runs/<run_id>/blackboard.json.

Sections:
  decisions       what a stage settled on
  open_questions  clarifying questions not yet answered by a human
  risks           architecture risks and review findings
  human_decisions standing human notes -- re-injected into that
                  stage's prompt on every later pass, so an automated
                  Review->Architecture retry can't silently forget what
                  a human already told the stage

Every entry carries its stage, iteration and a status
(active | superseded | resolved). A stage re-running supersedes its own
earlier entries instead of deleting them, so the history stays auditable.
"""

import json
import threading
from datetime import datetime, timezone
from typing import Dict, List

import config

SECTIONS = ("decisions", "open_questions", "risks", "human_decisions")
_lock = threading.Lock()


def _path(run_id: str):
    return config.RUNS_ROOT / run_id / "blackboard.json"


def read(run_id: str) -> Dict[str, List[dict]]:
    path = _path(run_id)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {s: data.get(s, []) for s in SECTIONS}
        except json.JSONDecodeError:
            pass
    return {s: [] for s in SECTIONS}


def _write(run_id: str, board: Dict[str, List[dict]]) -> None:
    path = _path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, indent=2, default=str), encoding="utf-8")


def post(run_id: str, section: str, stage: str, text: str, iteration: int = 0, **meta) -> None:
    if section not in SECTIONS or not text:
        return
    with _lock:
        board = read(run_id)
        board[section].append(
            {
                "stage": stage,
                "iteration": iteration,
                "text": str(text),
                "status": "active",
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **meta,
            }
        )
        _write(run_id, board)


def supersede(run_id: str, stage: str, sections=("decisions", "open_questions", "risks")) -> None:
    """Marks a stage's active entries superseded -- called just before
    that stage posts its fresh output."""
    with _lock:
        board = read(run_id)
        for section in sections:
            for entry in board[section]:
                if entry["stage"] == stage and entry["status"] == "active":
                    entry["status"] = "superseded"
        _write(run_id, board)


def resolve_questions(run_id: str, stage: str) -> None:
    with _lock:
        board = read(run_id)
        for entry in board["open_questions"]:
            if entry["stage"] == stage and entry["status"] == "active":
                entry["status"] = "resolved"
        _write(run_id, board)


def human_decisions_for(run_id: str, stage: str) -> List[str]:
    return [
        e["text"]
        for e in read(run_id)["human_decisions"]
        if e["stage"] == stage and e["status"] == "active"
    ]


def format_for_prompt(run_id: str, stage: str) -> str:
    """Standing human decisions for `stage`, formatted for its prompt;
    empty string when there are none."""
    decisions = human_decisions_for(run_id, stage)
    if not decisions:
        return ""
    bullets = "\n".join(f"- {d}" for d in decisions)
    return f"""
============================================================
STANDING HUMAN DECISIONS (from this run's blackboard -- binding)
============================================================

A human reviewer already gave this direction for your stage earlier in
this run. It still applies on this pass; do not revert it, and do not
ask about it again.

{bullets}
"""
