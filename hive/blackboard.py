"""Shared blackboard: runs/<run_id>/blackboard.json.

Plain words: a shared whiteboard for one run. Each stage can pin notes on
it (what it decided, what it's unsure about, what risks it sees), and
later stages can read them. Notes are never erased; old ones are just
marked as replaced, so you can always see how thinking evolved.

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

# The four headings of the whiteboard.
SECTIONS = ("decisions", "open_questions", "risks", "human_decisions")
# Prevents two simultaneous writers from corrupting the file.
_lock = threading.Lock()


def _path(run_id: str):
    """Where this run's whiteboard file lives."""
    return config.RUNS_ROOT / run_id / "blackboard.json"


def read(run_id: str) -> Dict[str, List[dict]]:
    """Load the whiteboard. If the file is missing or damaged, return an
    empty board rather than failing."""
    path = _path(run_id)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {s: data.get(s, []) for s in SECTIONS}
        except json.JSONDecodeError:
            pass
    return {s: [] for s in SECTIONS}


def _write(run_id: str, board: Dict[str, List[dict]]) -> None:
    """Save the whole whiteboard back to disk."""
    path = _path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, indent=2, default=str), encoding="utf-8")


def post(run_id: str, section: str, stage: str, text: str, iteration: int = 0, **meta) -> None:
    """Pin a new note under `section`. Ignored if the section name is
    unknown or the note is empty."""
    if section not in SECTIONS or not text:
        return
    with _lock:
        board = read(run_id)
        # Each note records who wrote it, which retry round it came from,
        # its text, that it's currently "active", and a timestamp.
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
    # In plain words: "this stage is about to give a new answer, so mark
    # its old notes as out of date" (they are kept, just flagged).
    with _lock:
        board = read(run_id)
        for section in sections:
            for entry in board[section]:
                if entry["stage"] == stage and entry["status"] == "active":
                    entry["status"] = "superseded"
        _write(run_id, board)


def resolve_questions(run_id: str, stage: str) -> None:
    """Mark a stage's open questions as answered (a human replied)."""
    with _lock:
        board = read(run_id)
        for entry in board["open_questions"]:
            if entry["stage"] == stage and entry["status"] == "active":
                entry["status"] = "resolved"
        _write(run_id, board)


def human_decisions_for(run_id: str, stage: str) -> List[str]:
    """The still-valid instructions a human gave to this particular stage."""
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
    # Turn the list into "- item" bullet lines, then wrap them in a block
    # of text that will be pasted into the AI's instructions, telling it
    # these human directions are binding.
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
