"""Checks of the agent activity log and its terminal card (activity.py, activity_ui.py). Run: pytest test_activity.py

No agent and no GitHub: the SDK's messages are built by hand and fed to the real runner, so what is checked is exactly what an agent's
steps turn into on screen.
"""

import asyncio
import json
import threading

import pytest
from streamlit.testing.v1 import AppTest

import activity
import config
import token_log
from coding import agent as coding_agent


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RUNS_ROOT", tmp_path / "runs")
    activity._mem.clear()
    activity._loaded = False
    activity._live["last"] = 0.0
    activity.detach()
    yield
    activity.detach()
    activity._mem.clear()


def lines():
    return [activity.format_line(e) for e in activity.recent()]


def test_a_line_has_a_time_an_agent_and_a_marker_for_what_kind_of_step_it_was():
    activity.log("src/A.java", "tool", agent="review (coding_java)")
    activity.log("All good", "ok", agent="review (coding_java)")
    activity.log("whatever", "bogus", agent="x")                                                  # an unknown kind is just information
    a, b, c = lines()
    assert a[8:10] == "  " and ":" in a[:8] and "review (coding_java)" in a and a.endswith("› src/A.java")
    assert b.endswith("✔ All good") and c.endswith("x" * 0 + "whatever") and "✖" not in c


def test_lines_are_cut_short_and_nothing_long_floods_the_terminal():
    activity.log("word " * 200)
    assert len(activity.recent()[0]["text"]) == activity.LINE_CHARS and activity.recent()[0]["text"].endswith("…")


def test_lines_go_to_a_file_and_come_back_after_a_restart():
    activity.log("first", agent="a")
    activity.log("second", agent="b")
    on_disk = [json.loads(l) for l in (config.RUNS_ROOT / "activity.jsonl").read_text().splitlines()]
    assert [e["text"] for e in on_disk] == ["first", "second"]
    activity._mem.clear()
    activity._loaded = False                                                                      # a new process
    assert [e["text"] for e in activity.recent()] == ["first", "second"]
    assert activity.agents() == ["a", "b"] and [e["text"] for e in activity.recent(agent="b")] == ["second"] and len(activity.recent(1)) == 1


def test_the_file_is_cut_back_to_its_newest_lines_when_it_grows_too_big(monkeypatch):
    monkeypatch.setattr(activity, "FILE_MAX_BYTES", 3000)
    monkeypatch.setattr(activity, "FILE_KEEP", 40)
    for i in range(300):
        activity.log(f"line {i}", agent="a")
    kept = (config.RUNS_ROOT / "activity.jsonl").read_text().splitlines()
    assert len(kept) < 100 and json.loads(kept[-1])["text"] == "line 299"


def test_clear_empties_the_screen_and_the_file():
    activity.log("x")
    activity.clear()
    assert activity.recent() == [] and not (config.RUNS_ROOT / "activity.jsonl").exists()


def test_logging_never_raises_even_when_it_cannot_write(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(config, "RUNS_ROOT", blocker)                                            # a "folder" that is a file: writing must fail
    activity.log("still fine")
    assert [e["text"] for e in activity.recent()] == ["still fine"]                               # kept in memory at least


def test_an_agent_context_labels_what_is_logged_inside_it_even_across_asyncio():
    async def work():
        activity.log("inside")
    with activity.agent_context("qa-verify (qa)"):
        activity.log("before")
        asyncio.run(work())
    activity.log("after")
    assert [e["agent"] for e in activity.recent()] == ["qa-verify (qa)", "qa-verify (qa)", "app"]


def test_the_pipeline_agents_report_through_the_token_ledger(monkeypatch):
    token_log.record("llm_start", "Requirement Agent", prompt_tokens=1200)
    token_log.record("llm_attempt", "Requirement Agent", attempt=1, ok=False, error="boom: bad json")
    token_log.record("llm_attempt", "Requirement Agent", attempt=2, ok=True)
    token_log.record("llm_call", "Requirement Agent", ok=True, attempts=2)
    token_log.record("llm_call", "Design Agent", ok=False, attempts=3, error="timeout")
    got = [(e["agent"], e["kind"], e["text"]) for e in activity.recent()]
    assert got == [("Requirement Agent", "info", "started (prompt about 1200 tokens)"), ("Requirement Agent", "err", "attempt 1 failed: boom: bad json"),
                   ("Requirement Agent", "ok", "attempt 2 ok"), ("Requirement Agent", "ok", "done"),
                   ("Design Agent", "err", "failed after 3 attempt(s): timeout")]


# ---------- the real runner: an agent's steps become lines ----------

def _messages():
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
    return [
        AssistantMessage(content=[TextBlock(text="I will look at the controller first."), ToolUseBlock(id="t1", name="Read", input={"file_path": "src/Ctl.java"})], model="m"),
        AssistantMessage(content=[ToolUseBlock(id="t2", name="Bash", input={"command": "python3 -m unittest -v"})], model="m"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="\n".join(f"test_{i} ... ok" for i in range(9)) + "\nRan 9 tests", is_error=False)]),
        ResultMessage(subtype="success", duration_ms=12000, duration_api_ms=9000, is_error=False, num_turns=5, session_id="s", total_cost_usd=0.42,
                      structured_output={"ok": True}),
    ]


def test_every_step_of_a_real_agent_run_becomes_a_line_with_the_agent_named(monkeypatch):
    async def fake_query(prompt, options):
        for m in _messages():
            yield m
    monkeypatch.setattr(coding_agent, "query", fake_query)
    out = coding_agent._call("qa-verify", "qa", "p", "s", "/tmp", None, None, None, coding_agent.EXEC_SCHEMA, ["Read"], [])
    assert out["answer"] == {"ok": True}
    got = [(e["agent"], e["kind"], e["text"]) for e in activity.recent()]
    who = "qa-verify (qa)"
    assert got[0] == (who, "info", "started")
    assert (who, "say", "I will look at the controller first.") in got and (who, "tool", "Read src/Ctl.java") in got
    assert (who, "tool", "Bash python3 -m unittest -v") in got
    assert [t for _, k, t in got if k == "out"] == ["test_6 ... ok", "test_7 ... ok", "test_8 ... ok", "Ran 9 tests"]      # the last few lines of output, not all of it
    assert got[-1] == (who, "ok", "finished: 5 turns, 12s, $0.42")


def test_a_failed_command_and_a_crashed_agent_show_up_as_errors(monkeypatch):
    from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

    async def fake_query(prompt, options):
        yield AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "make"})], model="m")
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="make: *** error", is_error=True)])
        raise RuntimeError("Command failed with exit code 143")
    monkeypatch.setattr(coding_agent, "query", fake_query)
    with pytest.raises(RuntimeError):
        coding_agent._call("step", "coding_java", "p", "s", "/tmp", None, None, None, coding_agent.EXEC_SCHEMA, ["Bash"], [])
    got = [(e["kind"], e["text"]) for e in activity.recent()]
    assert ("err", "make: *** error") in got and got[-1][0] == "err" and "stopped: RuntimeError: Command failed with exit code 143" in got[-1][1]
    assert len([1 for k, t in got if "exit code 143" in t]) == 1                                  # reported once, not twice


def test_what_the_agent_was_asked_is_never_logged(monkeypatch):
    async def fake_query(prompt, options):
        for m in _messages():
            yield m
    monkeypatch.setattr(coding_agent, "query", fake_query)
    coding_agent._call("review", "coding_python", "SECRET-PROMPT-TEXT token=ghp_abc", "SECRET-SYSTEM", "/tmp", None, None, None, coding_agent.EXEC_SCHEMA, ["Read"], [])
    blob = (config.RUNS_ROOT / "activity.jsonl").read_text() + " ".join(lines())
    assert "SECRET" not in blob and "ghp_" not in blob


# ---------- the live terminal ----------

def test_lines_logged_while_the_page_is_busy_redraw_the_terminal_at_once_but_not_too_often(monkeypatch):
    drawn = []
    monkeypatch.setattr(activity, "_in_page_thread", lambda: True)
    activity.attach(lambda: drawn.append(len(activity.recent())))
    activity.log("one")
    activity.log("two")                                                                           # inside the throttle window: not drawn again
    assert drawn == [1]
    activity.refresh_live(force=True)
    assert drawn == [1, 2]
    activity.detach()
    activity.log("three")
    assert drawn == [1, 2]


def test_a_worker_thread_never_touches_the_page_and_a_dead_terminal_is_dropped(monkeypatch):
    drawn = []
    activity.attach(lambda: drawn.append(1))
    t = threading.Thread(target=lambda: activity.log("from a worker"))
    t.start(); t.join()
    assert drawn == []                                                                            # no page here: the page's own loop draws these
    monkeypatch.setattr(activity, "_in_page_thread", lambda: True)
    activity.attach(lambda: (_ for _ in ()).throw(RuntimeError("the box is gone")))
    activity.log("must not raise")
    assert activity._live["render"] is None


# ---------- the card ----------

def _card():
    import activity_ui
    activity_ui.render()


def _text(at):
    return "\n".join(c.value for c in at.code)


def test_the_card_shows_what_the_agents_did_filters_by_agent_and_clears():
    at = AppTest.from_function(_card, default_timeout=20).run()
    assert not at.exception, at.exception
    assert "Nothing yet" in _text(at)
    activity.log("Read a.py", "tool", agent="review (coding_java)")
    activity.log("python3 -m pytest", "tool", agent="qa-verify (qa)")
    at = AppTest.from_function(_card, default_timeout=20).run()
    text = _text(at)
    assert "› Read a.py" in text and "› python3 -m pytest" in text and text.index("a.py") < text.index("pytest")        # oldest first, like a terminal
    at.selectbox(key="act_agent").select("qa-verify (qa)").run()
    assert "pytest" in _text(at) and "a.py" not in _text(at)
    at = _click(at, "act_clear")
    assert "Nothing yet" in _text(at) and activity.recent() == []


def _click(at, key):
    next(b for b in at.button if b.key == key).click()
    return at.run()


def test_the_card_can_show_more_lines():
    for i in range(60):
        activity.log(f"step {i}", agent="a")
    at = AppTest.from_function(_card, default_timeout=20).run()
    assert "step 59" in _text(at) and "step 35" in _text(at) and "step 34" not in _text(at)                  # the last 25
    at.selectbox(key="act_lines").select(100).run()
    assert "step 0" in _text(at) and "step 59" in _text(at)


def test_lines_logged_after_the_card_was_drawn_appear_in_it_during_the_same_page_run():
    def page():
        import activity as act
        import activity_ui
        act.detach()
        activity_ui.render()
        act.log("a long agent step, logged after the card was drawn", "tool", agent="step (coding_java)")    # the page is "busy" here
    at = AppTest.from_function(page, default_timeout=20).run()
    assert not at.exception, at.exception
    assert "a long agent step, logged after the card was drawn" in _text(at)                                  # the terminal updated live


def test_the_main_app_has_a_closed_activity_card_above_the_agents():
    at = AppTest.from_file("streamlit_app.py", default_timeout=90).run()
    heads = [b.label for b in at.button if (b.key or "").endswith("_head") and "dd_" in (b.key or "")]
    assert "▸  🖥️ Activity" in heads                                                                          # there from the start, and closed
    assert heads.index("▸  🖥️ Activity") > heads.index("▸  ➕ New requirement")
