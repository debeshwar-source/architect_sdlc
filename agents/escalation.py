"""Bounce limits for questions that travel agent -> Engineering Manager -> user.

Plain words: an agent that is unsure asks the manager; a manager that is unsure asks the
user. This keeps count so that can never go round in circles. Every limit is a number in
config.py, enforced here in code (not left to a prompt). When a limit is reached nothing
stalls or fails: the task proceeds on its stated assumption, is flagged `needs_review`, and
the unanswered question is attached for the reviewer.

The ledger lives in the run state (to_dict / from_dict), so a retry or resume cannot reset it.
"""

import difflib
import re
from typing import Any, Dict, List, Optional

import config
import token_log

UP = ("agent", "manager", "user")          # a question may only move up this ladder, never back down


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


class BounceLedger:
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        d = data or {}
        self.bounces: Dict[str, int] = dict(d.get("bounces", {}))
        self.reassigns: Dict[str, int] = dict(d.get("reassigns", {}))
        self.asked: Dict[str, List[str]] = {k: list(v) for k, v in d.get("asked", {}).items()}
        self.answers: Dict[str, str] = dict(d.get("answers", {}))       # normalised question -> answer
        self.user_questions: int = d.get("user_questions", 0)
        self.extra_calls: int = d.get("extra_calls", 0)

    def to_dict(self) -> Dict[str, Any]:
        return {"bounces": self.bounces, "reassigns": self.reassigns, "asked": self.asked, "answers": self.answers,
                "user_questions": self.user_questions, "extra_calls": self.extra_calls}

    # ---- asking ----

    def known_answer(self, question: str) -> Optional[str]:
        """An answer already given to this (or a near-identical) question, so nobody is asked twice."""
        n = _norm(question)
        for q, a in self.answers.items():
            if q == n or difflib.SequenceMatcher(None, q, n).ratio() >= 0.85:
                return a
        return None

    def _repeat(self, task: str, question: str) -> bool:
        n = _norm(question)
        return any(difflib.SequenceMatcher(None, n, old).ratio() >= 0.85 for old in self.asked.get(task, []))

    def allow(self, task: str, question: str, to: str, from_: str = "agent") -> bool:
        """May `question` for `task` move from `from_` up to `to`? Counts it if so. False means:
        close the task out (see close_out) instead of asking."""
        if UP.index(to) <= UP.index(from_):
            return self._hit(task, "wrong_direction", question)
        if self._repeat(task, question):
            return self._hit(task, "repeated_question", question)
        if self.bounces.get(task, 0) >= config.ESC_BOUNCES_PER_TASK:
            return self._hit(task, "bounces_per_task", question)
        if to == "user" and self.user_questions >= config.ESC_USER_QUESTIONS_PER_RELEASE:
            return self._hit(task, "user_questions_per_release", question)
        if self.extra_calls >= config.ESC_EXTRA_CALLS_PER_RELEASE:
            return self._hit(task, "extra_calls_per_release", question)
        self.bounces[task] = self.bounces.get(task, 0) + 1
        self.extra_calls += 1
        self.user_questions += to == "user"
        self.asked.setdefault(task, []).append(_norm(question))
        return True

    def allow_reassign(self, task: str) -> bool:
        if self.reassigns.get(task, 0) >= config.ESC_REASSIGNS_PER_TASK:
            return self._hit(task, "reassigns_per_task", "")
        self.reassigns[task] = self.reassigns.get(task, 0) + 1
        return True

    def record_answer(self, question: str, answer: str) -> None:
        self.answers[_norm(question)] = answer

    def _hit(self, task: str, limit: str, question: str) -> bool:
        token_log.record("bounce_limit_hit", "Escalation", task=task, limit=limit, question=question[:200])
        return False


def close_out(assignment: Dict[str, Any], question: str, limit: str = "") -> Dict[str, Any]:
    """The task proceeds on its best assumption and is flagged for the reviewer, with the
    unanswered question attached. The run is never blocked."""
    out = dict(assignment)
    out["needs_review"] = True
    out["open_question"] = question
    if limit:
        out["assumption"] = (out.get("assumption") or "proceeded on best judgement") + f" (escalation limit reached: {limit})"
    return out
