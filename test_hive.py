"""Plain-assert checks for the hive, question shaping and work orders.
Run: python3 test_hive.py (or pytest)."""

import os
import tempfile

os.environ["CLAUDE_SDLC_ARCHITECT_MEMORY_DIR"] = tempfile.mkdtemp()
os.environ["CLAUDE_SDLC_ARCHITECT_RUNS_ROOT"] = tempfile.mkdtemp()

import config
from agents._common import MAX_OPTION_CHARS, MAX_QUESTION_CHARS, validate_clarifying_questions
from agents.implementation_spec import _default_work_order
from hive import breaker, memory


def _f(text):
    return [{"severity": "BLOCKING", "finding": text}]


def test_breaker_ladder_escalates_and_stops():
    t = "notification idempotency duplicate episode events"
    s = breaker.evaluate({}, 1, _f(t), 2)
    assert s["level"] == breaker.OK
    levels = []
    for i in (2, 3, 4):
        s = breaker.evaluate(s, i, _f(t), 2)
        levels.append(s["level"])
    assert levels == [breaker.STEER, breaker.CONSTRAIN, breaker.STOP]


def test_breaker_constrains_immediately_when_scope_grows():
    t = "notification idempotency duplicate episode events"
    s = breaker.evaluate({}, 1, _f(t), 2)
    assert breaker.evaluate(s, 2, _f(t), 4)["level"] == breaker.CONSTRAIN


def test_breaker_ok_when_findings_change():
    s = breaker.evaluate({}, 1, _f("idempotency duplicate events"), 2)
    assert breaker.evaluate(s, 2, _f("rollout ordering wrong sequence"), 2)["level"] == breaker.OK


def test_condense_is_bounded_lossless_in_count_and_keeps_pinned():
    memory.MAX_RECORDS, memory.KEEP_RECENT = 30, 10
    for i in range(45):
        memory.remember({
            "run_id": f"r{i}", "requirement_text": f"notify users episode {i}",
            "requirement": {"title": f"t{i}"}, "run_outcome": "APPROVED_RELEASED",
            "breaker": {"level": "constrain"} if i == 2 else {}, "history": [],
        })
    records = memory._load()
    assert len(records) < 30
    assert sum(r.get("runs_summarized", 1) if r.get("kind") == "summary" else 1 for r in records) == 45
    assert any(r["run_id"] == "r2" for r in records)
    assert any(r.get("kind") == "summary" for r in records)
    assert list(config.MEMORY_DIR.glob("index.jsonl.bak-*"))


def test_questions_shortened():
    r = {"clarifying_questions": [{"question": "Ship now? " + "Detail. " * 80,
                                   "options": ["A " * 100, "B"], "recommended_option": "A " * 100}]}
    validate_clarifying_questions(r, "t")
    q = r["clarifying_questions"][0]
    assert len(q["question"]) <= MAX_QUESTION_CHARS and q["question"].startswith("Ship now?")
    assert all(len(o) <= MAX_OPTION_CHARS for o in q["options"])
    assert q["recommended_option"] in q["options"]


def test_spec_work_order_defaults():
    spec = {"application": "a", "summary": "S"}
    _default_work_order(spec)
    assert spec["objective"] == "S" and spec["boundaries"] and spec["definition_of_done"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
