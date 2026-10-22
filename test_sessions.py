"""Checks for saving and reopening sessions.
Run: python3 test_sessions.py (or pytest)."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import session_store as ss
import test_hive as t
import test_ui as ui
from knowledge_base.loader import ApplicationDoc


def _runs(tmp):
    config.RUNS_ROOT = Path(tmp)


def _session(**over):
    state = {
        "run_started": True, "run_active": True, "run_id": "run-1", "requirement_text": "Build a todo app\nwith reminders",
        "human_in_the_loop": True, "current_node": "design", "awaiting_approval": True, "finished": False, "error": None,
        "requirement_frozen_at": 1, "chat_history_1": [{"role": "user", "content": "why?"}],
        "some_widget_state": "not saved",
        "pipeline_state": {
            "run_id": "run-1", "kb_name": "", "iteration": 1, "history": [{"stage": "requirement"}],
            "applications": [ApplicationDoc("a", Path("/x"), "c", None, None)],
            "kb_confirm": lambda p: True, "design_path": Path("/tmp/x"), "requirement": {"title": "Todo reminders"},
        },
        "completed_events": [("requirement", {"requirement": {"title": "Todo reminders", "acceptance_criteria": ["a"]}})],
    }
    state.update(over)
    return state


def test_a_session_round_trips_without_losing_anything_that_matters():
    old = config.RUNS_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _runs(tmp)
            saved = ss.save(_session())
            assert saved and saved.name == "session.json" and not list(Path(tmp).rglob("*.tmp"))
            got = ss.restore("run-1", lambda name: [])
            assert got["session"]["current_node"] == "design" and got["session"]["awaiting_approval"] is True
            assert got["session"]["requirement_frozen_at"] == 1 and got["session"]["chat_history_1"][0]["content"] == "why?"
            assert "some_widget_state" not in got["session"]                       # only the session's own state is kept
            assert got["completed_events"] == [("requirement", {"requirement": {"title": "Todo reminders", "acceptance_criteria": ["a"]}})]
            assert isinstance(got["completed_events"][0], tuple)
            state = got["pipeline_state"]
            assert state["history"] == [{"stage": "requirement"}] and state["design_path"] == "/tmp/x"
            assert "kb_confirm" not in state and state["applications"] == []        # functions dropped; documents re-read
    finally:
        config.RUNS_ROOT = old


def test_the_knowledge_base_documents_are_read_again_by_name():
    old = config.RUNS_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _runs(tmp)
            session = _session()
            session["pipeline_state"]["kb_name"] = "streamly"
            ss.save(session)
            asked = []
            got = ss.restore("run-1", lambda name: asked.append(name) or ["doc"])
            assert asked == ["streamly"] and got["pipeline_state"]["applications"] == ["doc"]
            got = ss.restore("run-1", lambda name: 1 / 0)                            # a knowledge base that is gone is not fatal
            assert got["pipeline_state"]["applications"] == []
    finally:
        config.RUNS_ROOT = old


def test_nothing_is_saved_before_a_run_starts_and_bad_files_are_ignored():
    old = config.RUNS_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _runs(tmp)
            assert ss.save({}) is None and ss.save({"run_started": False, "run_id": "x"}) is None
            (Path(tmp) / "broken").mkdir()
            (Path(tmp) / "broken" / "session.json").write_text("{not json")
            (Path(tmp) / "old").mkdir()
            (Path(tmp) / "old" / "session.json").write_text(json.dumps({"version": 99, "session": {}}))
            assert ss.read("broken") is None and ss.read("old") is None and ss.read("missing") is None
            assert ss.restore("broken", lambda n: []) is None and ss.list_sessions() == []
    finally:
        config.RUNS_ROOT = old


def test_sessions_are_listed_newest_first_with_a_title_and_what_they_wait_for():
    old = config.RUNS_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _runs(tmp)
            ss.save(_session(run_id="run-a"))
            ss.save(_session(run_id="run-b", awaiting_approval=False, finished=True, current_node=None))
            ss.save(_session(run_id="run-c", requirement_text="Just text", pipeline_state={"kb_name": ""}))
            data = json.loads(ss.path_for("run-a").read_text())
            data["saved_at"] = "2020-01-01T00:00:00+00:00"
            ss.path_for("run-a").write_text(json.dumps(data))                       # make it the oldest
            items = ss.list_sessions()
            assert [i["run_id"] for i in items][-1] == "run-a" and len(items) == 3
            by = {i["run_id"]: i for i in items}
            assert by["run-a"]["title"] == "Todo reminders" and by["run-a"]["waiting"] == ["approval"]
            assert by["run-b"]["finished"] and by["run-c"]["title"] == "Just text"
    finally:
        config.RUNS_ROOT = old


def test_a_custom_name_is_saved_listed_with_the_original_title_and_kept_when_reopened():
    old = config.RUNS_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _runs(tmp)
            ss.save(_session(run_id="run-a", session_name="  Coupon   rollout v1  "))
            ss.save(_session(run_id="run-b"))
            by = {i["run_id"]: i for i in ss.list_sessions()}
            assert by["run-a"]["title"] == "Coupon rollout v1" and by["run-a"]["named"] and by["run-a"]["original"] == "Todo reminders"
            assert by["run-b"]["title"] == "Todo reminders" and not by["run-b"]["named"] and by["run-b"]["original"] == ""      # unnamed: as before
            back = ss.restore("run-a", lambda name: [])
            assert back["session"]["session_name"] == "Coupon   rollout v1".replace("   ", " ") or back["session"]["session_name"].strip() == "Coupon   rollout v1"
    finally:
        config.RUNS_ROOT = old


def test_names_are_tidied_limited_and_one_name_cannot_belong_to_two_sessions():
    old = config.RUNS_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _runs(tmp)
            assert ss.clean_name("  a \n  b\t c ") == "a b c" and ss.clean_name("") == "" and ss.clean_name(None) == ""
            assert len(ss.clean_name("x" * 500)) == ss.NAME_MAX
            ss.save(_session(run_id="run-a", session_name="Coupon rollout v1"))
            ss.save(_session(run_id="run-b"))
            assert ss.name_taken("coupon ROLLOUT v1", "run-b")                       # another session has it (case does not matter)
            assert not ss.name_taken("Coupon rollout v1", "run-a")                    # your own name is yours
            assert not ss.name_taken("Todo reminders", "run-a")                       # an automatic title is not a name anyone holds
            assert not ss.name_taken("", "run-b") and not ss.name_taken("Something new", "run-b")
    finally:
        config.RUNS_ROOT = old


def test_closing_the_app_and_opening_it_again_resumes_at_the_same_pause():
    with t._kb_root(), ui.scripted_ai(ui.new_kb_canned()) as prompts:
        at = ui.start(text="Build a todo app", mode="Human approval")             # paused after the Requirement agent
        assert not at.exception, at.exception
        run_id = at.session_state["run_id"]
        assert ss.path_for(run_id).is_file(), "the session was not saved"
        asked = len(prompts)

        again = ui.fresh_app()                                                    # the app was closed and started again
        assert "Open" in ui.labels(again) and "Freeze requirements" not in ui.labels(again)
        again = ui.click(again, "Open")
        assert not again.exception, again.exception
        assert "Session reopened" in ui.texts(again) or "Session reopened" in " ".join(s.value for s in again.success)
        assert "Freeze requirements" in ui.labels(again)                          # the same pause, with its choices
        assert again.session_state["run_id"] == run_id and len(prompts) == asked     # nothing was run again
        again = ui.click(again, "Approve & continue")                              # and it can carry on
        assert not again.exception, again.exception
        assert "Freeze architecture" in ui.labels(again) and {a for a, _ in prompts} >= {"Design Agent"}


def test_a_fresh_start_opens_nothing_and_does_not_claim_a_session_was_reopened():
    with t._kb_root(), ui.scripted_ai(ui.new_kb_canned()):
        first = ui.fresh_app()                                                    # nothing saved yet
        assert not any("Session reopened" in x.value for x in first.success)
        at = ui.start(text="Build a todo app", mode="Human approval")             # now there is a saved session
        assert not at.exception, at.exception
        again = ui.fresh_app()                                                    # the app is closed and started again
        assert not again.exception, again.exception
        assert "run_id" not in again.session_state and "run_started" not in again.session_state
        assert not any("Session reopened" in x.value for x in again.success)      # no false "reopened" banner
        assert "Open" in ui.labels(again) and "Freeze requirements" not in ui.labels(again)   # offered, not opened


def test_deleting_a_saved_session_removes_only_its_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        old, config.RUNS_ROOT = config.RUNS_ROOT, Path(tmp)
        try:
            (Path(tmp) / "run-a").mkdir()
            (Path(tmp) / "run-a" / "design.md").write_text("keep me")
            (Path(tmp) / "run-a" / ss.FILE_NAME).write_text("{}")
            assert ss.delete("run-a") and not ss.path_for("run-a").exists()
            assert (Path(tmp) / "run-a" / "design.md").is_file()
            assert not ss.delete("run-a") and not ss.delete("../run-a") and not ss.delete("")
        finally:
            config.RUNS_ROOT = old


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
